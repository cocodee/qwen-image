"""Diffusers 后端服务 —— 在 SGLang 路线被 CUDA 13 堵死后启用的替代实现。

对外暴露与 SGLang 相同的 OpenAI 兼容 images 接口，所以网关那层完全不用改。

参数映射依据 `QwenImage21Pipeline.__call__` 的真实签名（diffusers main，
`src/diffusers/pipelines/qwenimage21/pipeline_qwenimage21.py`）：

    prompt, image, negative_prompt, true_cfg_scale, height, width,
    num_inference_steps, num_images_per_prompt, generator, output_type,
    output_resolution, use_kv_cache

几个与 SGLang 语义不同、必须显式处理的地方：

  * **没有 `background` 参数** —— 透明背景靠 prompt 驱动（见 _RGBA_PREFIX）。
  * **没有 `mask` 参数** —— 本 pipeline 不支持 inpaint，收到 mask 一律 400，
    不做静默丢弃。
  * **没有 `size`** —— 拆成 width/height。
  * **没有 `guidance_scale`** —— 只有 true_cfg_scale（默认 1.0）。
  * 参考图 RGBA 会在视觉编码器前被合成到白底（pipeline 第 266 行），这是
    模型训练时的约定，不是 bug。
"""

from __future__ import annotations

import base64
import io
import logging
import os
import threading
import time
from typing import Any

import torch
from fastapi import FastAPI, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse
from PIL import Image

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)-7s %(message)s")
log = logging.getLogger("diffusers-backend")

MODEL_PATH = os.getenv("MODEL_PATH", "/models/Qwen-Image-2.1")
MODEL_ID = os.getenv("MODEL_ID", "Qwen/Qwen-Image-2.1")
TORCH_DTYPE = os.getenv("TORCH_DTYPE", "bfloat16")
CPU_OFFLOAD = os.getenv("CPU_OFFLOAD", "0") == "1"
DEFAULT_STEPS = int(os.getenv("DEFAULT_STEPS", "40"))
DEFAULT_RESOLUTION = int(os.getenv("DEFAULT_RESOLUTION", "1024"))
MAX_REF_IMAGES = int(os.getenv("MAX_REF_IMAGES", "10"))
# 多参考图走这个分辨率。>=768 时上游 pipeline 会忽略编辑指令、近似返回最后
# 一张参考图；512 实测正常。实测数据见 README「已知限制」。
MULTI_REF_RESOLUTION = int(os.getenv("MULTI_REF_RESOLUTION", "512"))
# output_resolution 的允许区间。它既决定输出边长，又决定**每张**参考图被
# resize 到多少像素（约 output_resolution²）—— 所以显存开销随参考图张数线性放大。
# 没有上界的话，调用方一个很大的值就能把这张共享卡打到 OOM，把别人的任务一起拖垮。
MIN_OUTPUT_RESOLUTION = int(os.getenv("MIN_OUTPUT_RESOLUTION", "256"))
MAX_OUTPUT_RESOLUTION = int(os.getenv("MAX_OUTPUT_RESOLUTION", "2048"))

# 模型卡推荐的透明背景提示词前缀。pipeline 没有 background 开关，
# 透明与否完全由提示词引导 —— 所以这里必须改写 prompt，而不是传参。
_RGBA_PREFIX = (
    "This is an RGBA image with transparency. "
    "The image has an alpha channel and the background is transparent. "
)

IMAGE_MIME = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "webp": "image/webp"}

_pipe = None
# diffusers 没有批处理引擎，并发调用只会互相抢显存并拖慢彼此。
# 网关那边已经限流，这把锁是最后一道保险。
_gpu_lock = threading.Lock()


def load_pipeline() -> None:
    global _pipe
    from diffusers import QwenImage21Pipeline

    dtype = {"bfloat16": torch.bfloat16, "float16": torch.float16, "float32": torch.float32}[TORCH_DTYPE]
    log.info("加载 %s (dtype=%s, cpu_offload=%s)", MODEL_PATH, TORCH_DTYPE, CPU_OFFLOAD)
    started = time.time()
    pipe = QwenImage21Pipeline.from_pretrained(MODEL_PATH, torch_dtype=dtype)
    if CPU_OFFLOAD:
        pipe.enable_model_cpu_offload()
    else:
        pipe = pipe.to("cuda")
    _pipe = pipe
    log.info("加载完成，耗时 %.1fs；显存占用 %.1f GB", time.time() - started, torch.cuda.memory_allocated() / 1e9)


def get_pipe():
    if _pipe is None:
        raise HTTPException(503, "模型尚未加载完成")
    return _pipe


app = FastAPI(title="Qwen-Image-2.1 Diffusers Backend")


@app.on_event("startup")
def _startup() -> None:
    load_pipeline()


@app.get("/healthz")
def healthz():
    return {"status": "ok" if _pipe is not None else "loading", "model": MODEL_ID}


@app.get("/v1/models")
def list_models():
    if _pipe is None:
        raise HTTPException(503, "模型尚未加载完成")
    return {
        "object": "list",
        "data": [{"id": MODEL_ID, "object": "model", "created": int(time.time()), "owned_by": "local"}],
    }


# --------------------------------------------------------------------------
# 核心生成
# --------------------------------------------------------------------------

def _parse_size(size: str | None) -> tuple[int | None, int | None]:
    if not size:
        return None, None
    try:
        w, h = (int(v) for v in size.lower().split("x"))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"size 必须形如 'WxH'，收到 {size!r}") from exc
    return w, h


def _validate_output_resolution(value: int) -> int:
    """越界一律 400，不钳制后继续跑（钳制会让调用方以为参数生效了）。

    上界不是洁癖：该值同时是**每张**参考图的 resize 目标面积（约 value²），
    所以总开销随参考图张数线性放大。放一个 4096 进来等于让一张共享卡上跑的
    这个进程去申请几十 GB 激活值 —— 结果是 OOM，而且会把同卡的其它作业一起拖死。
    """
    if not (MIN_OUTPUT_RESOLUTION <= value <= MAX_OUTPUT_RESOLUTION):
        raise HTTPException(
            400,
            f"output_resolution 必须在 {MIN_OUTPUT_RESOLUTION}~{MAX_OUTPUT_RESOLUTION} 之间，"
            f"收到 {value}。该值既决定输出边长，也决定每张参考图的缩放目标面积，"
            f"无上界会直接把共享 GPU 打到 OOM。",
        )
    return value


def _apply_transparency(prompt: str, background: str | None) -> str:
    if (background or "").lower() != "transparent":
        return prompt
    if prompt.lstrip().startswith("This is an RGBA image"):
        return prompt  # 调用方自己写了，别重复
    return _RGBA_PREFIX + prompt


def _encode(images: list[Image.Image], output_format: str, compression: int | None) -> list[str]:
    fmt = (output_format or "png").lower()
    pil_fmt = {"jpg": "JPEG", "jpeg": "JPEG", "webp": "WEBP", "png": "PNG"}.get(fmt)
    if pil_fmt is None:
        raise HTTPException(400, f"不支持的 output_format: {output_format!r}")

    out = []
    for img in images:
        if fmt in ("jpg", "jpeg") and img.mode == "RGBA":
            # JPEG 没有 alpha 通道。网关已经会拦这种情况，这里是第二道防线。
            raise HTTPException(400, "JPEG 不支持透明背景，请改用 png 或 webp")
        buf = io.BytesIO()
        kwargs: dict[str, Any] = {}
        if compression is not None and pil_fmt in ("JPEG", "WEBP"):
            kwargs["quality"] = max(1, min(100, int(compression)))
        img.save(buf, format=pil_fmt, **kwargs)
        out.append(base64.b64encode(buf.getvalue()).decode("ascii"))
    return out


def _generate(
    prompt: str,
    images: list[Image.Image] | None,
    width: int | None,
    height: int | None,
    steps: int,
    true_cfg_scale: float,
    negative_prompt: str | None,
    seed: int | None,
    n: int,
    output_format: str,
    compression: int | None,
    output_resolution: int,
) -> list[str]:
    pipe = get_pipe()
    generator = None
    if seed is not None:
        generator = torch.Generator(device="cuda").manual_seed(int(seed))

    kwargs: dict[str, Any] = {
        "prompt": prompt,
        "num_inference_steps": steps,
        "true_cfg_scale": true_cfg_scale,
        "num_images_per_prompt": n,
        "output_type": "pil",
        "output_resolution": output_resolution,
    }
    if negative_prompt:
        kwargs["negative_prompt"] = negative_prompt
    if generator is not None:
        kwargs["generator"] = generator
    if width:
        kwargs["width"] = width
    if height:
        kwargs["height"] = height
    if images:
        kwargs["image"] = images if len(images) > 1 else images[0]

    with _gpu_lock, torch.inference_mode():
        result = pipe(**kwargs)

    return _encode(result.images, output_format, compression)


def _read_upload(raw: bytes) -> Image.Image:
    try:
        return Image.open(io.BytesIO(raw)).convert("RGB")
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"参考图无法解析为图片: {exc}") from exc


# --------------------------------------------------------------------------
# OpenAI 兼容接口
# --------------------------------------------------------------------------

@app.post("/v1/images/generations")
async def generations(request: Request):
    body = await request.json()
    prompt = body.get("prompt")
    if not prompt or not str(prompt).strip():
        raise HTTPException(422, "缺少必填字段 'prompt'")

    width, height = _parse_size(body.get("size"))
    prompt = _apply_transparency(str(prompt), body.get("background"))
    output_resolution = _validate_output_resolution(
        int(body.get("output_resolution", DEFAULT_RESOLUTION))
    )

    try:
        b64s = _generate(
            prompt=prompt,
            images=None,
            width=width,
            height=height,
            steps=int(body.get("num_inference_steps", DEFAULT_STEPS)),
            true_cfg_scale=float(body.get("true_cfg_scale", 1.0)),
            negative_prompt=body.get("negative_prompt"),
            seed=body.get("seed"),
            n=int(body.get("n", 1)),
            output_format=body.get("output_format", "png"),
            compression=body.get("output_compression"),
            output_resolution=output_resolution,
        )
    except HTTPException:
        raise
    except torch.cuda.OutOfMemoryError as exc:
        torch.cuda.empty_cache()
        raise HTTPException(507, f"显存不足，请降低尺寸或步数: {exc}") from exc

    return {"created": int(time.time()), "data": [{"b64_json": b, "revised_prompt": None} for b in b64s]}


@app.post("/v1/images/edits")
async def edits(
    image: list[UploadFile] = [],
    prompt: str = Form(...),
    size: str | None = Form(None),
    n: int = Form(1),
    num_inference_steps: int = Form(DEFAULT_STEPS),
    true_cfg_scale: float = Form(1.0),
    negative_prompt: str | None = Form(None),
    seed: int | None = Form(None),
    output_format: str = Form("png"),
    output_compression: int | None = Form(None),
    output_resolution: int | None = Form(None),
    background: str | None = Form(None),
    mask: UploadFile | None = None,
):
    if not prompt or not prompt.strip():
        raise HTTPException(422, "缺少必填字段 'prompt'")
    if not image:
        raise HTTPException(422, "至少需要一张参考图")

    # 本 pipeline 没有 mask 参数 —— 不支持 inpaint。
    # 明确报错而不是静默忽略：调用方以为局部编辑生效了才是最糟的结果。
    if mask is not None and getattr(mask, "filename", None):
        raise HTTPException(
            400,
            "当前 Diffusers 后端不支持 mask 局部编辑"
            "（QwenImage21Pipeline 无 mask 参数）。请改用整图编辑，或走 SGLang 后端。",
        )

    if len(image) > MAX_REF_IMAGES:
        raise HTTPException(400, f"参考图最多 {MAX_REF_IMAGES} 张，收到 {len(image)} 张")

    # 越界在**读图之前**就拒掉：省得为了一个注定 400 的请求去解码一堆参考图。
    if output_resolution is not None:
        output_resolution = _validate_output_resolution(output_resolution)

    refs = [_read_upload(await f.read()) for f in image]
    width, height = _parse_size(size)
    prompt = _apply_transparency(prompt, background)

    notes: list[str] = []
    # 多参考图在 output_resolution >= 768 时会退化成"原样返回最后一张参考图"
    # —— 上游 pipeline 的行为，实测可复现，加 <image1>/<image2> 标记也无效。
    # 512 则正常。调用方没显式指定时替它选一个能工作的值，并如实说明，
    # 而不是悄悄改掉参数让它看起来"本该如此"。
    # 显式传了值就照用，**只做区间校验**（见 _validate_output_resolution），
    # 不因为"这个值大概率出图难看"去覆盖调用方的选择 —— 那是质量判断，不是错误。
    if output_resolution is None:
        if len(refs) > 1:
            output_resolution = MULTI_REF_RESOLUTION
            notes.append(
                f"检测到 {len(refs)} 张参考图，调用方未指定 output_resolution，"
                f"已自动设为 {MULTI_REF_RESOLUTION}"
                f"（允许范围 {MIN_OUTPUT_RESOLUTION}~{MAX_OUTPUT_RESOLUTION}）。"
                f"上游 pipeline 在「多参考图 + output_resolution >= 768」时会忽略编辑指令、"
                f"近似原样返回最后一张参考图（实测数据见 README「已知限制」）——"
                f"这是出图变差，不是报错。确实需要更高分辨率时，显式传 output_resolution "
                f"即可覆盖本次的 {MULTI_REF_RESOLUTION}，但那就是上述退化区间，请先自行校验效果。"
            )
        else:
            output_resolution = DEFAULT_RESOLUTION

    try:
        b64s = _generate(
            prompt=prompt,
            images=refs,
            width=width,
            height=height,
            steps=num_inference_steps,
            true_cfg_scale=true_cfg_scale,
            negative_prompt=negative_prompt,
            seed=seed,
            n=n,
            output_format=output_format,
            compression=output_compression,
            output_resolution=output_resolution,
        )
    except HTTPException:
        raise
    except torch.cuda.OutOfMemoryError as exc:
        torch.cuda.empty_cache()
        raise HTTPException(507, f"显存不足，请降低尺寸或步数: {exc}") from exc

    resp = {"created": int(time.time()), "data": [{"b64_json": b, "revised_prompt": None} for b in b64s]}
    if notes:
        resp["x_notes"] = notes
    return resp


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8091")))
