"""OpenAI 兼容的 images 接口（同步）。

同步请求也走一遍任务落盘 —— 路径与异步任务完全一致，
好处是所有产出都受同一套 TTL 清扫与审计覆盖，没有"同步图永久滞留"的漏洞。
"""

from __future__ import annotations

import asyncio
import base64
import logging
import time
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from ..backend import BackendError
from ..edit_form import FormError, parse_edit_form
from ..models import ImageGenerationRequest, SizeError, normalize_generation
from ..tasks import persist_images

log = logging.getLogger(__name__)
router = APIRouter()

# 同步请求取不到并发名额时等多久再加放弃。扩散单次 10~30s，
# 等一个名额比直接失败合理，但也不能无限等 —— 超过就让调用方重试。
_SLOT_WAIT_S = 60.0


async def _acquire_slot(request: Request) -> asyncio.Semaphore:
    sem: asyncio.Semaphore = request.app.state.runner.semaphore
    try:
        await asyncio.wait_for(sem.acquire(), timeout=_SLOT_WAIT_S)
    except asyncio.TimeoutError:
        raise HTTPException(
            429,
            f"服务繁忙：等待 {int(_SLOT_WAIT_S)}s 仍未取得并发名额，请稍后重试"
            "（或改用异步任务接口 POST /v1/tasks）",
        ) from None
    return sem


def _build_response(
    images_meta: list[dict[str, Any]],
    raw: list[bytes],
    response_format: str,
) -> dict[str, Any]:
    data = []
    for meta, blob in zip(images_meta, raw):
        if response_format == "url":
            data.append({"url": meta["url"], "revised_prompt": None})
        else:
            data.append(
                {"b64_json": base64.b64encode(blob).decode("ascii"), "revised_prompt": None}
            )
    return {"created": int(time.time()), "data": data}


async def _run_sync(
    request: Request,
    kind: str,
    payload: dict[str, Any],
    notes: list[str],
    response_format: str,
    run_backend,
) -> dict[str, Any]:
    store = request.app.state.store
    task_id = await store.create(kind, {"payload": payload, "notes": notes, "sync": True})
    await store.set_running(task_id)

    sem = await _acquire_slot(request)
    try:
        result = await run_backend()
    except BackendError as exc:
        await store.set_failed(task_id, exc.detail)
        raise HTTPException(exc.status_code, exc.detail) from exc
    except Exception as exc:  # noqa: BLE001
        log.exception("同步请求 %s 失败", task_id)
        await store.set_failed(task_id, f"{type(exc).__name__}: {exc}")
        raise HTTPException(500, "内部错误") from exc
    finally:
        sem.release()

    # 网关自己的纠正说明 + 后端上报的说明，合并后一路透给调用方
    all_notes = notes + result.notes
    images_meta = await persist_images(
        request.app.state.settings, store, task_id, payload.get("output_format", "png"), result
    )
    await store.set_succeeded(task_id, images_meta, all_notes, result.timings)

    resp = _build_response(images_meta, [i.data for i in result.images], response_format)
    if all_notes:
        # 让调用方知道我们改过他的参数（例如透明背景把 jpeg 纠正成了 png，
        # 或多参考图时自动降低了 output_resolution）
        resp["x_notes"] = all_notes
    return resp


@router.post("/v1/images/generations")
async def generations(request: Request, body: ImageGenerationRequest):
    try:
        payload, notes = normalize_generation(body)
    except SizeError as exc:
        raise HTTPException(400, str(exc)) from exc

    backend = request.app.state.backend
    return await _run_sync(
        request,
        "generation",
        payload,
        notes,
        body.response_format,
        lambda: backend.generate(payload),
    )


@router.post("/v1/images/edits")
async def edits(request: Request):
    form = await request.form()
    try:
        parsed = await parse_edit_form(form, request.app.state.settings)
    except FormError as exc:
        raise HTTPException(exc.status_code, exc.detail) from exc

    backend = request.app.state.backend
    return await _run_sync(
        request,
        "edit",
        parsed.payload,
        parsed.notes,
        "b64_json",  # 后端 edits 接口只支持 b64_json
        lambda: backend.edit(parsed.fields, parsed.files),
    )
