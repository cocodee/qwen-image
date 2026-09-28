"""解析 /v1/images/edits 的 multipart 表单。

单独成模块是因为同步接口（`POST /v1/images/edits`）和异步接口
（`POST /v1/tasks` 带 multipart body）用的是同一套解析规则 ——
两份实现迟早会漂移。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from starlette.datastructures import FormData

from .config import Settings
from .models import SizeError, parse_size

# 原样转发给后端的字段（multipart 里只能传字符串）
#
# ⚠️ 这里漏字段 = 该参数被静默丢弃。`output_resolution` 曾经就是这样：后端在
# 多参考图时会回一句「如确需高分辨率，请显式传 output_resolution」，而唯一
# 对外的入口根本传不进去 —— 说明是空头支票。加字段前先确认后端真的认它。
PASSTHROUGH_FORM_FIELDS = (
    "negative_prompt",
    "num_inference_steps",
    "guidance_scale",
    "true_cfg_scale",
    "seed",
    "output_compression",
    "output_resolution",
)

_ALPHA_CAPABLE = ("png", "webp")


class FormError(Exception):
    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


@dataclass
class ParsedEdit:
    fields: dict[str, str]
    files: list[tuple[str, tuple[str, bytes, str]]]
    notes: list[str] = field(default_factory=list)
    payload: dict[str, Any] = field(default_factory=dict)


async def parse_edit_form(form: FormData, settings: Settings) -> ParsedEdit:
    prompt = form.get("prompt")
    if not prompt or not str(prompt).strip():
        raise FormError(422, "缺少必填字段 'prompt'")

    # 参考图：同时接受 OpenAI 的 `image` 与部分客户端用的 `image[]`
    refs = [v for key in ("image", "image[]") for v in form.getlist(key)]
    refs = [r for r in refs if getattr(r, "filename", None)]
    if not refs:
        raise FormError(422, "至少需要一张参考图（字段名 'image' 或 'image[]'）")
    if len(refs) > settings.max_ref_images:
        raise FormError(400, f"参考图最多 {settings.max_ref_images} 张，收到 {len(refs)} 张")

    output_format = str(form.get("output_format", "png")).lower()
    background = str(form.get("background", "auto")).lower()
    notes: list[str] = []

    if background == "transparent" and output_format not in _ALPHA_CAPABLE:
        notes.append(f"output_format 已从 {output_format} 纠正为 png：透明背景需要 alpha 通道")
        output_format = "png"

    fields: dict[str, str] = {
        "prompt": str(prompt),
        "output_format": output_format,
        "background": "transparent" if background == "transparent" else "opaque",
    }
    for key in ("size", "n", "model"):
        val = form.get(key)
        if val is not None and str(val).strip():
            fields[key] = str(val)
    for key in PASSTHROUGH_FORM_FIELDS:
        val = form.get(key)
        if val is not None and str(val).strip():
            fields[key] = str(val)

    if "size" in fields:
        try:
            parse_size(fields["size"])
        except SizeError as exc:
            raise FormError(400, str(exc)) from exc

    files: list[tuple[str, tuple[str, bytes, str]]] = []
    for idx, ref in enumerate(refs):
        content = await ref.read()
        if len(content) > settings.max_upload_bytes:
            raise FormError(
                413,
                f"第 {idx + 1} 张参考图超过 {settings.max_upload_bytes // 1048576}MB 上限",
            )
        files.append(
            (
                settings.edit_image_field,
                (ref.filename or f"ref{idx}.png", content, ref.content_type or "image/png"),
            )
        )

    mask = form.get("mask")
    if mask is not None and getattr(mask, "filename", None):
        content = await mask.read()
        files.append(
            (
                settings.edit_mask_field,
                (mask.filename or "mask.png", content, mask.content_type or "image/png"),
            )
        )

    return ParsedEdit(
        fields=fields,
        files=files,
        notes=notes,
        payload={"output_format": output_format, "n_files": len(files)},
    )
