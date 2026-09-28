"""运维与静态产出：健康检查、模型列表、取图。"""

from __future__ import annotations

import re
import time

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse

router = APIRouter()

_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SAFE_ID = re.compile(r"^[A-Za-z0-9-]{8,64}$")

_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}


@router.get("/healthz")
async def healthz(request: Request):
    backend_ok = await request.app.state.backend.healthy()
    runner = request.app.state.runner
    payload = {
        "status": "ok" if backend_ok else "degraded",
        "backend": "ok" if backend_ok else "unreachable",
        "backend_url": request.app.state.settings.backend_base_url,
        "model": request.app.state.settings.model_id,
        "queue_pending": runner.pending,
        "queue_capacity": request.app.state.settings.max_queue_depth,
        "max_concurrency": request.app.state.settings.max_concurrency,
    }
    return JSONResponse(payload, status_code=200 if backend_ok else 503)


@router.get("/v1/models")
async def list_models(request: Request):
    """返回本网关对外提供的模型（OpenAI list 形状）。

    只报我们自己这一个模型 id —— 后端内部跑的是什么与调用方无关，
    而且调用方拿这个 id 去调用才有意义。
    """
    return {
        "object": "list",
        "data": [
            {
                "id": request.app.state.settings.model_id,
                "object": "model",
                "created": int(time.time()),
                "owned_by": "local",
            }
        ],
    }


@router.get("/files/{task_id}/{name}")
async def get_file(task_id: str, name: str, request: Request):
    if not _SAFE_ID.match(task_id) or not _SAFE_NAME.match(name):
        raise HTTPException(400, "非法的路径参数")

    store = request.app.state.store
    path = store.output_dir(task_id) / name
    # 二次确认：即便正则放行，也绝不允许跳出 outputs/{task_id}/
    if not path.resolve().is_relative_to(store.output_dir(task_id).resolve()):
        raise HTTPException(400, "非法的路径参数")
    if not path.is_file():
        raise HTTPException(404, "文件不存在或已过期")

    return FileResponse(path, media_type=_MIME.get(path.suffix.lower(), "application/octet-stream"))
