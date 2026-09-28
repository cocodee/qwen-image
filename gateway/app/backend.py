"""后端适配层。

把「调 SGLang 出图」这件事收敛到一个窄接口后面。若哪天 SGLang 这条路走不通，
换 `DiffusersBackend` 即可，网关其余部分（路由、任务队列、存储）完全不动。
"""

from __future__ import annotations

import base64
import hashlib
import io
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx
from PIL import Image


@dataclass
class BackendImage:
    data: bytes
    width: int
    height: int
    mode: str
    sha256: str

    @classmethod
    def from_bytes(cls, raw: bytes) -> "BackendImage":
        try:
            with Image.open(io.BytesIO(raw)) as im:
                im.load()
                width, height, mode = im.width, im.height, im.mode
        except Exception as exc:  # noqa: BLE001 - 后端给了非图片内容，如实上报
            raise BackendError(
                502, f"后端返回的内容不是合法图片: {exc}"
            ) from exc
        return cls(
            data=raw,
            width=width,
            height=height,
            mode=mode,
            sha256=hashlib.sha256(raw).hexdigest(),
        )


@dataclass
class BackendResult:
    images: list[BackendImage]
    timings: dict[str, float] = field(default_factory=dict)
    # 后端对参数做的调整说明（例如多参考图时自动降低 output_resolution）。
    # 必须一路透传给调用方 —— 静默改参数比报错更让人困惑。
    notes: list[str] = field(default_factory=list)


class BackendError(Exception):
    """带 HTTP 语义的后端错误。路由层据此决定回什么状态码。"""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


class Backend(Protocol):
    async def healthy(self) -> bool: ...

    async def list_models(self) -> dict[str, Any]: ...

    async def generate(self, payload: dict[str, Any]) -> BackendResult: ...

    async def edit(
        self,
        fields: dict[str, str],
        files: list[tuple[str, tuple[str, bytes, str]]],
    ) -> BackendResult: ...


def _extract_error(resp: httpx.Response) -> str:
    try:
        body = resp.json()
    except Exception:  # noqa: BLE001
        return resp.text[:500] or f"HTTP {resp.status_code}"
    if isinstance(body, dict):
        for key in ("error", "detail", "message"):
            if key in body:
                val = body[key]
                if isinstance(val, dict) and "message" in val:
                    return str(val["message"])
                return str(val)
    return str(body)[:500]


def _decode_payload(body: dict[str, Any]) -> list[BackendImage]:
    data = body.get("data")
    if not isinstance(data, list) or not data:
        raise BackendError(502, f"后端响应缺少 data 数组: {str(body)[:300]}")

    images: list[BackendImage] = []
    for item in data:
        b64 = item.get("b64_json")
        if not b64:
            raise BackendError(
                502,
                "后端未返回 b64_json（网关固定请求 b64_json；"
                f"实际字段: {sorted(item.keys())}）",
            )
        try:
            raw = base64.b64decode(b64, validate=True)
        except Exception as exc:  # noqa: BLE001
            raise BackendError(502, f"后端返回的 base64 无法解码: {exc}") from exc
        images.append(BackendImage.from_bytes(raw))
    return images


class SGLangBackend:
    """SGLang (sglang-diffusion) 的 OpenAI 兼容 images 接口。"""

    def __init__(self, base_url: str, client: httpx.AsyncClient, timeout_s: float) -> None:
        self.base_url = base_url.rstrip("/")
        self.client = client
        self.timeout = httpx.Timeout(timeout_s, connect=10.0)

    async def healthy(self) -> bool:
        try:
            resp = await self.client.get(f"{self.base_url}/v1/models", timeout=5.0)
            return resp.status_code == 200
        except httpx.HTTPError:
            return False

    async def list_models(self) -> dict[str, Any]:
        resp = await self.client.get(f"{self.base_url}/v1/models", timeout=10.0)
        if resp.status_code != 200:
            raise BackendError(resp.status_code, _extract_error(resp))
        return resp.json()

    async def generate(self, payload: dict[str, Any]) -> BackendResult:
        body = {**payload, "response_format": "b64_json"}
        started = time.perf_counter()
        try:
            resp = await self.client.post(
                f"{self.base_url}/v1/images/generations", json=body, timeout=self.timeout
            )
        except httpx.TimeoutException as exc:
            raise BackendError(504, f"后端生成超时（{self.timeout.read}s）") from exc
        except httpx.HTTPError as exc:
            raise BackendError(502, f"连接后端失败: {exc}") from exc

        elapsed = time.perf_counter() - started
        if resp.status_code >= 400:
            raise BackendError(resp.status_code, _extract_error(resp))

        body = resp.json()
        images = _decode_payload(body)
        return BackendResult(
            images=images,
            timings={"backend_s": round(elapsed, 3)},
            notes=[str(n) for n in body.get("x_notes", [])],
        )

    async def edit(
        self,
        fields: dict[str, str],
        files: list[tuple[str, tuple[str, bytes, str]]],
    ) -> BackendResult:
        """图像编辑必须走 multipart —— 后端对 JSON body 会直接重置连接。

        `files` 里同一个字段名可以重复出现（SGLang 用重复的 `image` 传多张参考图），
        所以这里传的是 list 而不是 dict。
        """
        data = {**fields, "response_format": "b64_json"}
        started = time.perf_counter()
        try:
            resp = await self.client.post(
                f"{self.base_url}/v1/images/edits",
                data=data,
                files=files,
                timeout=self.timeout,
            )
        except httpx.TimeoutException as exc:
            raise BackendError(504, f"后端编辑超时（{self.timeout.read}s）") from exc
        except httpx.HTTPError as exc:
            raise BackendError(502, f"连接后端失败: {exc}") from exc

        elapsed = time.perf_counter() - started
        if resp.status_code >= 400:
            raise BackendError(resp.status_code, _extract_error(resp))

        body = resp.json()
        images = _decode_payload(body)
        return BackendResult(
            images=images,
            timings={"backend_s": round(elapsed, 3)},
            notes=[str(n) for n in body.get("x_notes", [])],
        )


class DiffusersBackend:
    """回退方案占位。

    计划风险 #1 的最终退路：若 SGLang 在本机彻底走不通，就用 diffusers
    直接跑 `QwenImage21Pipeline`（diffusers PR #14804 已合并）。
    网关以上全部不变，只需实现下面这几个方法。
    """

    def __init__(self, *_: Any, **__: Any) -> None:
        raise NotImplementedError(
            "DiffusersBackend 尚未实现。启用前请先确认 SGLang 路径确实不可用"
            "（见计划文档的『风险与回退』第 1 条）。"
        )
