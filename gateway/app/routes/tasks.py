"""异步任务接口。

扩散单次要 10~30 秒，同步接口容易被客户端超时掐断。这里提供
submit -> poll 的形态：提交立即返回 task_id，客户端自己掌握节奏。

`POST /v1/tasks` 按 Content-Type 分流：
  - `application/json`      -> 文生图任务
  - `multipart/form-data`   -> 图像编辑任务（与 /v1/images/edits 同样的字段）
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from ..edit_form import FormError, parse_edit_form
from ..models import ImageGenerationRequest, SizeError, normalize_generation
from ..store import TaskNotCancelable, TaskNotFound
from ..tasks import QueueFull

router = APIRouter()


def _to_status(row: dict[str, Any]) -> dict[str, Any]:
    result = json.loads(row["result_json"]) if row.get("result_json") else None
    try:
        notes = json.loads(row.get("notes_json") or "[]")
    except (json.JSONDecodeError, TypeError):
        notes = []
    return {
        "task_id": row["id"],
        "kind": row["kind"],
        "status": row["status"],
        "created_at": row["created_at"],
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
        "images": result["images"] if result else None,
        "timings": result.get("timings", {}) if result else {},
        "error": row["error"],
        "notes": notes,
    }


@router.post("/v1/tasks", status_code=202)
async def submit_task(request: Request):
    settings = request.app.state.settings
    runner = request.app.state.runner
    content_type = request.headers.get("content-type", "")

    if "multipart/form-data" in content_type:
        form = await request.form()
        try:
            parsed = await parse_edit_form(form, settings)
        except FormError as exc:
            raise HTTPException(exc.status_code, exc.detail) from exc
        try:
            task_id = await runner.submit_edit(
                parsed.payload, parsed.fields, parsed.files, parsed.notes
            )
        except QueueFull as exc:
            raise HTTPException(429, str(exc)) from exc
        kind = "edit"
    else:
        try:
            body = await request.json()
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(400, f"请求体不是合法 JSON: {exc}") from exc
        try:
            req = ImageGenerationRequest.model_validate(body)
        except ValidationError as exc:
            raise HTTPException(422, exc.errors()) from exc
        try:
            payload, notes = normalize_generation(req)
        except SizeError as exc:
            raise HTTPException(400, str(exc)) from exc
        try:
            task_id = await runner.submit_generation(payload, notes)
        except QueueFull as exc:
            raise HTTPException(429, str(exc)) from exc
        kind = "generation"

    base = settings.public_base_url.rstrip("/")
    return JSONResponse(
        {
            "task_id": task_id,
            "status": "queued",
            "kind": kind,
            "status_url": f"{base}/v1/tasks/{task_id}",
        },
        status_code=202,
    )


@router.get("/v1/tasks")
async def list_tasks(request: Request, limit: int = 50):
    limit = max(1, min(limit, 500))
    rows = await request.app.state.store.list_recent(limit)
    return {
        "object": "list",
        "data": [
            {
                "task_id": r["id"],
                "kind": r["kind"],
                "status": r["status"],
                "created_at": r["created_at"],
                "finished_at": r["finished_at"],
            }
            for r in rows
        ],
    }


@router.get("/v1/tasks/{task_id}")
async def get_task(task_id: str, request: Request):
    row = await request.app.state.store.get(task_id)
    if row is None:
        raise HTTPException(404, "任务不存在或已过期")
    return _to_status(row)


@router.delete("/v1/tasks")
async def purge_tasks(request: Request, ids: str | None = None):
    """一键清空画廊：**物理删除**任务行 + 图片文件（不可撤销）。

    与 `DELETE /v1/tasks/{task_id}` 完全不是一回事 —— 那个是**取消**（只改状态，
    图还在，见 cancel_task 与 README）。这个是真的删。

    范围：
      - 不传 `ids`       -> 所有终态任务（全库）
      - `ids=a,b,c`      -> 只删这些 id 里的终态任务
      - `ids=`（显式空） -> 空选择，删 0 个（**不**退化成全清）

    只要有任务在排队或生成中就 409 拒绝，一个都不删。理由：删掉行之后 worker
    还会继续跑完并往 outputs/{task_id}/ 写文件，于是凭空多出一个「有目录没行」
    的孤儿 —— 正好是本接口要收拾的那种不一致，不能自己再制造一份。
    """
    store = request.app.state.store

    if ids is None:
        task_ids: list[str] | None = None
    else:
        # 逗号分隔，逐段 strip 后丢掉空白项：`ids=a,,b, ` 与 `ids=a,b` 等价。
        task_ids = [part for part in (p.strip() for p in ids.split(",")) if part]
        if not task_ids:
            # 显式传了 `ids=` 却解析不出一项 = 点名了零个任务。删 0 个并返回零，
            # **绝不**退化成「全清」：前端一个拼错的参数不该抹掉整个画廊。
            # 这里也不做 409 检查 —— 什么都不删的操作没有拒绝的理由，
            # 用户点「清空我的」而本浏览器恰好没有任务时，不该看到一个错误。
            return {"deleted": 0, "files_removed": 0, "bytes_freed": 0}

    active = await store.count_active()
    if active:
        raise HTTPException(
            409, f"还有 {active} 个任务在排队或生成中，请等它们结束后再清空"
        )

    return await store.purge(task_ids)


@router.delete("/v1/tasks/{task_id}")
async def cancel_task(task_id: str, request: Request):
    try:
        status = await request.app.state.store.cancel(task_id)
    except TaskNotFound as exc:
        raise HTTPException(404, "任务不存在或已过期") from exc
    except TaskNotCancelable as exc:
        raise HTTPException(409, str(exc)) from exc

    row = await request.app.state.store.get(task_id)
    return {
        "task_id": task_id,
        "status": status,
        "detail": row["error"] if row else None,
    }
