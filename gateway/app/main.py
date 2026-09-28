"""Qwen-Image-2.1 网关服务入口。

在 SGLang 后端之上补两样它没有的东西：
  1. 异步任务层（submit -> poll），避免长请求被客户端超时掐断；
  2. OpenAI 兼容的缝隙填补 —— `response_format=url`、多 `image[]` 字段规整、
     透明背景与输出格式的一致性校验。
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from .backend import SGLangBackend
from .config import get_settings
from .routes import ops
from .routes import openai_images
from .routes import tasks as tasks_routes
from .store import TaskStore
from .tasks import TaskRunner

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
log = logging.getLogger("gateway")

LICENSE_NOTICE = (
    "Qwen-Image-2.1 采用 Qwen Research License Agreement（仅限研究/评估，禁止商用）。"
    "本服务定位为内部研发与评测，请勿对外提供商用服务。"
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    settings.ensure_dirs()

    client = httpx.AsyncClient()
    backend = SGLangBackend(settings.backend_base_url, client, settings.request_timeout_s)
    store = TaskStore(settings.tasks_db, settings.outputs_dir, settings.uploads_dir)
    runner = TaskRunner(store, backend, settings)

    app.state.settings = settings
    app.state.backend = backend
    app.state.store = store
    app.state.runner = runner

    await runner.start()

    log.info("后端: %s", settings.backend_base_url)
    log.info("数据目录: %s", settings.data_dir.resolve())
    log.warning(LICENSE_NOTICE)

    try:
        yield
    finally:
        await runner.stop()
        await client.aclose()
        log.info("网关已关闭")


app = FastAPI(
    title="Qwen-Image-2.1 Gateway",
    description=LICENSE_NOTICE,
    version="0.1.0",
    lifespan=lifespan,
)


@app.exception_handler(HTTPException)
async def openai_error_shape(request: Request, exc: HTTPException):
    """把 FastAPI 的 {"detail": ...} 包成 OpenAI 的 {"error": {...}}。

    OpenAI SDK 在非 2xx 时会去读 body 里的 error 字段，形状不对会抛解析异常
    盖掉真正的错误信息 —— 那样调用方就只能看到"未知错误"。
    """
    detail = exc.detail
    message = detail if isinstance(detail, str) else str(detail)
    return JSONResponse(
        {
            "error": {
                "message": message,
                "type": "invalid_request_error" if exc.status_code < 500 else "server_error",
                "code": exc.status_code,
            }
        },
        status_code=exc.status_code,
        headers=getattr(exc, "headers", None),
    )


app.include_router(ops.router)
app.include_router(openai_images.router)
app.include_router(tasks_routes.router)


@app.get("/")
async def root():
    return {
        "service": "qwen-image-2.1 gateway",
        "docs": "/docs",
        "endpoints": [
            "POST /v1/images/generations",
            "POST /v1/images/edits",
            "POST /v1/tasks",
            "GET  /v1/tasks/{task_id}",
            "DELETE /v1/tasks/{task_id}",   # 取消
            "DELETE /v1/tasks",             # 清空（删行 + 删图）
            "GET  /v1/models",
            "GET  /healthz",
            "GET  /files/{task_id}/{name}",
        ],
        "license": LICENSE_NOTICE,
    }
