"""请求/响应 schema 与参数规整。

设计要点：OpenAI 客户端会发一堆我们并不使用的字段（`quality`、`style`…），
所以请求模型用 `extra="allow"` 收下，再由 `passthrough()` 挑出真正要转发给后端的扩散参数。
"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

# 转发给后端的扩散扩展参数（SGLang 的 vllm-omni 风格扩展）
PASSTHROUGH_KEYS = (
    "negative_prompt",
    "num_inference_steps",
    "guidance_scale",
    "true_cfg_scale",
    "seed",
    "output_compression",
)

OUTPUT_FORMATS = ("png", "jpg", "jpeg", "webp")
_ALPHA_CAPABLE = ("png", "webp")  # webp 支持 alpha，jpeg 不支持

_SIZE_RE = re.compile(r"^(\d{1,5})x(\d{1,5})$")
_MIN_SIDE, _MAX_SIDE = 256, 4096


class SizeError(ValueError):
    """size 不合法，应由路由层翻成 HTTP 400。"""


def parse_size(size: str) -> tuple[int, int]:
    """把 OpenAI 的 "WxH" 解析成 (w, h) 并做基本约束校验。

    注意：SGLang 后端实际接受的取值域仍需实测确认（见计划风险 #5）。
    这里只拦明显非法的输入，让后端去拒绝它不支持的分辨率，
    我们再把后端的 4xx 原样透传，而不是假装自己知道全部合法值。
    """
    m = _SIZE_RE.match(size.strip())
    if not m:
        raise SizeError(f"size 必须形如 'WxH'，收到 {size!r}")
    w, h = int(m.group(1)), int(m.group(2))
    for name, v in (("width", w), ("height", h)):
        if not (_MIN_SIDE <= v <= _MAX_SIDE):
            raise SizeError(f"{name} 需在 {_MIN_SIDE}..{_MAX_SIDE}，收到 {v}")
        if v % 32:
            raise SizeError(f"{name}={v} 必须是 32 的倍数")
    return w, h


class ImageGenerationRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    prompt: str = Field(min_length=1)
    model: str | None = None  # 收下并回显；不参与路由（后端只跑一个模型）
    n: int = Field(1, ge=1, le=10)
    size: str | None = None
    response_format: Literal["b64_json", "url"] = "b64_json"
    user: str | None = None
    output_format: str | None = None
    background: str | None = None

    @field_validator("output_format")
    @classmethod
    def _check_format(cls, v: str | None) -> str | None:
        if v is not None and v.lower() not in OUTPUT_FORMATS:
            raise ValueError(f"output_format 只能是 {OUTPUT_FORMATS}，收到 {v!r}")
        return v.lower() if v else None

    def passthrough(self) -> dict[str, Any]:
        """挑出要转发给后端的扩展参数，丢掉 OpenAI 专有但后端不认的字段。"""
        out: dict[str, Any] = {}
        for key in PASSTHROUGH_KEYS:
            val = getattr(self, key, None)
            if val is None:
                val = (self.model_extra or {}).get(key)
            if val is not None:
                out[key] = val
        return out


def normalize_generation(req: ImageGenerationRequest) -> tuple[dict[str, Any], list[str]]:
    """把请求规整成后端能吃的 JSON body。

    返回 (payload, notes)。notes 记录我们对用户参数做的纠正，
    会原样回给调用方 —— 静默改参数比报错更让人困惑。
    """
    notes: list[str] = []
    payload: dict[str, Any] = {"prompt": req.prompt, "n": req.n}
    payload.update(req.passthrough())

    if req.size:
        parse_size(req.size)  # 仅校验，非法则抛 SizeError
        payload["size"] = req.size

    output_format = req.output_format or "png"
    background = (req.background or "auto").lower()

    if background == "transparent":
        payload["background"] = "transparent"
        if output_format not in _ALPHA_CAPABLE:
            notes.append(
                f"output_format 已从 {output_format} 纠正为 png："
                "透明背景需要 alpha 通道，而该格式不支持"
            )
            output_format = "png"
    else:
        # 只用 OpenAI 自己的词汇（auto/opaque），不映射成某个后端的私有取值 ——
        # 后端负责解释它。当前后端把"非 transparent"一律当作不透明处理。
        payload["background"] = "opaque"

    payload["output_format"] = output_format
    return payload, notes


class ImageObject(BaseModel):
    b64_json: str | None = None
    url: str | None = None
    revised_prompt: str | None = None


class ImagesResponse(BaseModel):
    """与 OpenAI /v1/images/generations 的响应形状一致。

    刻意不带额外顶层字段：OpenAI SDK 用 pydantic 解析这个响应，
    多余字段在严格版本下会报错。诊断信息放在异步任务接口里。
    """

    created: int
    data: list[ImageObject]


class TaskSubmitResponse(BaseModel):
    task_id: str
    status: str
    kind: str
    status_url: str


class TaskImage(BaseModel):
    file: str
    url: str
    width: int
    height: int
    mode: str
    sha256: str
    bytes: int


class TaskStatusResponse(BaseModel):
    task_id: str
    kind: str
    status: str
    created_at: float
    started_at: float | None = None
    finished_at: float | None = None
    images: list[TaskImage] | None = None
    error: str | None = None
    notes: list[str] = Field(default_factory=list)
    timings: dict[str, float] = Field(default_factory=dict)
