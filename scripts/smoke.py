#!/usr/bin/env python3
"""Qwen-Image-2.1 服务的端到端冒烟验证。

    python3 scripts/smoke.py                  # 常规项
    python3 scripts/smoke.py --base http://host:8090
    python3 scripts/smoke.py --include-backpressure   # 额外跑队列背压（会真跑几十次生成）

每项独立断言、失败不中断，最后汇总。退出码非 0 表示有失败项。

注意实现上的一个坑（曾经踩过）：Check 对象必须由调用方创建并传进来，
不能让每个 check 函数自己 new 一个 —— 那样断言失败抛出的异常会被外层
捕获，而外层的 Check.results 依然是空的，all([]) 恒为 True，
结果是**无论怎么都报 PASS**。断言框架本身说谎，比没有断言更危险。
"""

from __future__ import annotations

import argparse
import base64
import io
import subprocess
import sys
import time
from typing import Any, Callable

import httpx
from PIL import Image, ImageDraw

TIMEOUT = httpx.Timeout(600.0, connect=10.0)


# --------------------------------------------------------------------------
# 断言框架
# --------------------------------------------------------------------------

class Failure(AssertionError):
    pass


class Check:
    """一次检查的断言与观察记录。

    `ok()` 的 msg 描述的是**失败情形**，所以只在失败时展示 ——
    否则通过时会把"没有任何 GPU 占用超过 20GB"这种话当成正常信息打出来，
    读起来像反了。
    """

    def __init__(self, name: str) -> None:
        self.name = name
        self.failures: list[str] = []
        self.notes: list[str] = []
        self.assertions = 0

    def ok(self, cond: bool, msg: str) -> None:
        self.assertions += 1
        if not cond:
            self.failures.append(msg)
            raise Failure(msg)

    def note(self, msg: str) -> None:
        self.notes.append(msg)

    @property
    def passed(self) -> bool:
        # 一条断言都没跑过说明这项根本没被验证，不能算通过
        return self.assertions > 0 and not self.failures

    def render(self) -> str:
        head = "PASS" if self.passed else "FAIL"
        lines = [f"[{head}] {self.name}  ({self.assertions} 项断言)"]
        lines += [f"       - {n}" for n in self.notes]
        lines += [f"       ! {f}" for f in self.failures]
        return "\n".join(lines)


def decode_image(resp_json: dict[str, Any]) -> tuple[Image.Image, dict[str, Any]]:
    data = resp_json["data"]
    if not data:
        raise Failure("响应 data 为空")
    item = data[0]
    if item.get("b64_json"):
        raw = base64.b64decode(item["b64_json"])
    elif item.get("url"):
        raw = httpx.get(item["url"], timeout=TIMEOUT).content
    else:
        raise Failure(f"响应既无 b64_json 也无 url: {sorted(item.keys())}")
    return Image.open(io.BytesIO(raw)), {"raw": raw, **item}


def synth_ref(color: str, shape: str, size: int = 512) -> bytes:
    """造一张参考图。用合成图而不是真照片，是为了让冒烟脚本自包含、不依赖外部素材。"""
    im = Image.new("RGB", (size, size), "white")
    d = ImageDraw.Draw(im)
    if shape == "circle":
        d.ellipse((size * 0.2, size * 0.2, size * 0.8, size * 0.8), fill=color)
    else:
        d.rectangle((size * 0.2, size * 0.2, size * 0.8, size * 0.8), fill=color)
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


# --------------------------------------------------------------------------
# 各项检查（每个都接收外部传入的 Check）
# --------------------------------------------------------------------------

def check_health(c: Check, client: httpx.Client, model: str) -> None:
    r = client.get("/healthz")
    c.note(f"GET /healthz -> {r.status_code}")
    body = r.json()
    c.ok(r.status_code == 200, f"/healthz 非 200: {body}")
    c.ok(body.get("backend") == "ok", f"后端不可达: {body.get('backend')}")
    c.note(f"队列 {body.get('queue_pending')}/{body.get('queue_capacity')}，并发上限 {body.get('max_concurrency')}")

    r = client.get("/v1/models")
    ids = [m["id"] for m in r.json().get("data", [])]
    c.ok(model in ids, f"/v1/models 不含 {model}，实际 {ids}")


def check_t2i(c: Check, client: httpx.Client, model: str) -> None:
    t0 = time.time()
    r = client.post(
        "/v1/images/generations",
        json={
            "model": model,
            "prompt": "a red apple on a wooden table, studio lighting",
            "size": "1024x1024",
            "num_inference_steps": 40,
            "seed": 42,
            "response_format": "b64_json",
        },
    )
    c.ok(r.status_code == 200, f"HTTP {r.status_code}: {r.text[:300]}")
    img, _ = decode_image(r.json())
    c.ok(img.size == (1024, 1024), f"尺寸为 {img.size}，期望 (1024, 1024)")
    c.ok(img.mode in ("RGB", "RGBA"), f"图像 mode 异常: {img.mode}")
    c.note(f"耗时 {time.time() - t0:.1f}s，mode={img.mode}")


def check_transparent(c: Check, client: httpx.Client, model: str) -> None:
    r = client.post(
        "/v1/images/generations",
        json={
            "model": model,
            "prompt": "a cartoon dragon sticker, flat colors",
            "size": "1024x1024",
            "num_inference_steps": 40,
            "seed": 7,
            "background": "transparent",
            "response_format": "b64_json",
        },
    )
    c.ok(r.status_code == 200, f"HTTP {r.status_code}: {r.text[:300]}")
    img, _ = decode_image(r.json())
    c.ok(img.mode == "RGBA", f"mode={img.mode}，透明输出应为 RGBA")

    # 关键：不能只看「有没有 alpha 通道」。实测普通文生图的 alpha 范围是
    # (253,255) —— 也有 alpha 通道，但没有任何透明像素。
    # 必须确认 alpha 最小值真的是 0，才说明透明确实生效了。
    alpha_min, alpha_max = img.getchannel("A").getextrema()
    c.note(f"alpha 取值范围 [{alpha_min}, {alpha_max}]")
    c.ok(
        alpha_min == 0,
        f"没有任何全透明像素（alpha 最小值 {alpha_min}）—— 透明背景未生效",
    )
    if "x_notes" in r.json():
        c.note(f"网关提示: {r.json()['x_notes']}")


def _hue_counts(img: Image.Image) -> dict[str, int]:
    """按色相粗分类，用来判断输出里到底出现了哪些颜色。"""
    import colorsys
    from collections import Counter

    def cls(p):
        r, g, b = [v / 255 for v in p]
        h, s, v = colorsys.rgb_to_hsv(r, g, b)
        if s < 0.25 or v < 0.2:
            return "neutral"
        deg = h * 360
        if deg < 20 or deg > 340:
            return "red"
        if 200 < deg < 260:
            return "blue"
        return "other"

    return dict(Counter(cls(p) for p in img.resize((128, 128)).getdata()))


def _mean_abs_diff(a: Image.Image, b: Image.Image, side: int = 64) -> float:
    """全图比较。

    不能只取 tobytes() 的前若干字节 —— 那只是图像顶部的一条，而合成图的
    顶部往往是白底，两张图在那里几乎一致，断言就等于没比。
    """
    ax = a.convert("RGB").resize((side, side)).tobytes()
    bx = b.convert("RGB").resize((side, side)).tobytes()
    return sum(abs(x - y) for x, y in zip(ax, bx)) / len(ax)


def check_edit(c: Check, client: httpx.Client, model: str) -> None:
    """单参考图编辑 —— 这是模型文档里的标准用法，在 1024 分辨率下可靠。"""
    ref = synth_ref("red", "square")
    t0 = time.time()
    r = client.post(
        "/v1/images/edits",
        data={
            "model": model,
            "prompt": "add a blue circle next to the red square",
            "num_inference_steps": 40,
            "seed": 11,
        },
        files=[("image", ("ref.png", ref, "image/png"))],
    )
    c.ok(r.status_code == 200, f"HTTP {r.status_code}: {r.text[:300]}")
    img, _ = decode_image(r.json())
    out = img.convert("RGB")
    hues = _hue_counts(out)
    c.note(f"耗时 {time.time() - t0:.1f}s，尺寸 {img.size}，色相分布 {hues}")

    c.ok(hues.get("red", 0) > 200, f"输出里没有红色，未保留参考图的红方: {hues}")
    c.ok(hues.get("blue", 0) > 200, f"输出里没有蓝色，编辑指令（加蓝圆）未生效: {hues}")
    c.ok(
        _mean_abs_diff(out, Image.open(io.BytesIO(ref))) > 10,
        "输出与参考图几乎相同，编辑可能没生效（只是把输入回吐）",
    )


def check_multi_ref(c: Check, client: httpx.Client, model: str) -> None:
    """多参考图编辑。

    注意：上游 pipeline 在 output_resolution >= 768 时会退化成"近似原样返回
    最后一张参考图"。后端因此在多图且调用方未指定分辨率时自动降到 512。
    这条断言就是盯住这个行为的 —— 若哪天上游修好了，这里会提示可以放宽。
    """
    ref_a = synth_ref("red", "square")
    ref_b = synth_ref("blue", "circle")
    t0 = time.time()
    r = client.post(
        "/v1/images/edits",
        data={
            "model": model,
            "prompt": "combine the red square and the blue circle into one composition",
            "num_inference_steps": 40,
            "seed": 11,
        },
        files=[
            ("image", ("ref_a.png", ref_a, "image/png")),
            ("image", ("ref_b.png", ref_b, "image/png")),
        ],
    )
    c.ok(r.status_code == 200, f"HTTP {r.status_code}: {r.text[:300]}")
    img, _ = decode_image(r.json())
    out = img.convert("RGB")
    hues = _hue_counts(out)
    c.note(f"耗时 {time.time() - t0:.1f}s，尺寸 {img.size}，色相分布 {hues}")

    if "x_notes" in r.json():
        for n in r.json()["x_notes"]:
            c.note(f"网关提示: {n[:110]}…")

    for name, raw in (("参考图A", ref_a), ("参考图B", ref_b)):
        d = _mean_abs_diff(out, Image.open(io.BytesIO(raw)))
        c.ok(d > 10, f"输出与{name}几乎相同（全图平均像素差 {d:.2f}），编辑未生效")

    c.ok(hues.get("red", 0) > 200, f"输出里没有红色，参考图A未参与合成: {hues}")
    c.ok(hues.get("blue", 0) > 200, f"输出里没有蓝色，参考图B未参与合成: {hues}")


def check_async(c: Check, client: httpx.Client, model: str) -> None:
    r = client.post(
        "/v1/tasks",
        json={
            "model": model,
            "prompt": "a lighthouse at dusk, long exposure",
            "size": "1024x1024",
            "num_inference_steps": 40,
            "seed": 3,
        },
    )
    c.ok(r.status_code == 202, f"提交应返回 202，实际 {r.status_code}: {r.text[:200]}")
    body = r.json()
    task_id = body["task_id"]
    c.note(f"task_id={task_id} status={body['status']}")
    c.ok(body["status"] == "queued", f"初始状态应为 queued，实际 {body['status']}")

    deadline = time.time() + 300
    last = None
    while time.time() < deadline:
        s = client.get(f"/v1/tasks/{task_id}").json()
        last = s["status"]
        if s["status"] in ("succeeded", "failed", "canceled"):
            break
        time.sleep(2)

    c.ok(last == "succeeded", f"任务最终状态 {last}，未成功")
    s = client.get(f"/v1/tasks/{task_id}").json()
    c.ok(bool(s.get("images")), "succeeded 但没有 images")
    url = s["images"][0]["url"]
    c.note(f"结果 URL {url}  sha256={s['images'][0]['sha256'][:16]}…")

    blob = client.get(url)
    c.ok(blob.status_code == 200, f"取图返回 {blob.status_code}")
    Image.open(io.BytesIO(blob.content)).verify()  # 真的能当图片打开才算数
    c.note(f"取回 {len(blob.content)} 字节，可正常解析为图片")

    # 取消一个排队中的任务
    r2 = client.post("/v1/tasks", json={"prompt": "something slow", "seed": 1})
    tid2 = r2.json()["task_id"]
    d = client.delete(f"/v1/tasks/{tid2}")
    c.ok(d.status_code in (200, 409), f"取消失败: {d.status_code} {d.text[:200]}")
    if d.status_code == 200:
        c.note(f"取消 {tid2} -> {d.json()['status']}")


def check_openai_sdk(c: Check, base: str, model: str) -> None:
    try:
        from openai import OpenAI
    except ImportError:
        c.note("未安装 openai 包，跳过（pip install openai 后可跑）")
        return

    client = OpenAI(base_url=f"{base}/v1", api_key="not-needed")
    resp = client.images.generate(
        model=model,
        prompt="a single orange on a white plate",
        size="1024x1024",
        n=1,
        response_format="b64_json",
    )
    c.ok(bool(resp.data), "SDK 返回 data 为空")
    raw = base64.b64decode(resp.data[0].b64_json)
    img = Image.open(io.BytesIO(raw))
    c.ok(img.size == (1024, 1024), f"SDK 取回尺寸 {img.size}")
    c.note(f"官方 openai 包调用成功（{len(raw)} 字节）")


def check_negatives(c: Check, client: httpx.Client) -> None:
    r = client.post("/v1/images/generations", json={"prompt": "x", "size": "abc"})
    c.ok(r.status_code == 400, f"非法 size 应 400，实际 {r.status_code}")

    r = client.post("/v1/images/generations", json={"prompt": "x", "size": "1000x1000"})
    c.ok(r.status_code == 400, f"非 32 倍数的 size 应 400，实际 {r.status_code}")

    r = client.post("/v1/images/generations", json={})
    c.ok(r.status_code == 422, f"缺 prompt 应 422，实际 {r.status_code}")

    eleven = [("image", (f"r{i}.png", synth_ref("green", "square", 64), "image/png")) for i in range(11)]
    r = client.post("/v1/images/edits", data={"prompt": "merge"}, files=eleven)
    c.ok(r.status_code == 400, f"11 张参考图应 400（上限 10），实际 {r.status_code}")

    r = client.get("/v1/tasks/deadbeefdeadbeefdeadbeefdeadbeef")
    c.ok(r.status_code == 404, f"不存在任务应 404，实际 {r.status_code}")

    # 探针不能用 "/files/../../etc/passwd"。它从来没能验证网关的防护，有三层原因：
    #   1) 5 段路径 vs 路由 /files/{task_id}/{name} 的 3 段 —— 永远匹配不上，
    #      返回的是 route-miss 404（用 curl --path-as-is 直连网关同样如此，与代理无关）；
    #   2) httpx 在客户端就把 .. 规范化掉，"files/" 前缀根本不会发出去；
    #   3) 旧断言 `in (400, 404)` 对 route-miss 404 恒真 —— 所以它必然通过。
    # 用 3 段路径 + 非法 task_id 才能真正打到 ops.py:62 的 _SAFE_ID 校验。
    r = client.get("/files/short/n.png")
    c.ok(r.status_code == 400, f"非法 task_id 应 400，实际 {r.status_code}: {r.text[:200]}")
    c.ok(
        "非法的路径参数" in r.json().get("error", {}).get("message", ""),
        f"返回了 400 但消息不对（是不是没走到 ops.py 的校验？）: {r.text[:200]}",
    )

    # mask 局部编辑：Diffusers 的 QwenImage21Pipeline 没有 mask 参数，
    # 后端必须明确拒绝 —— 静默忽略会让调用方以为局部编辑生效了。
    r = client.post(
        "/v1/images/edits",
        data={"prompt": "replace the sky"},
        files=[
            ("image", ("r.png", synth_ref("blue", "square", 256), "image/png")),
            ("mask", ("m.png", synth_ref("white", "circle", 256), "image/png")),
        ],
    )
    c.ok(
        r.status_code == 400,
        f"带 mask 应 400（本后端不支持 inpaint），实际 {r.status_code}",
    )
    c.note(f"mask 被正确拒绝: {r.json().get('error', {}).get('message', '')[:60]}")


def check_backpressure(c: Check, client: httpx.Client) -> None:
    health = client.get("/healthz").json()
    depth = health["queue_capacity"]
    c.note(f"queue_capacity={depth}，将提交 {depth + 5} 个任务")

    codes: dict[int, int] = {}
    for i in range(depth + 5):
        r = client.post("/v1/tasks", json={"prompt": f"backpressure probe {i}", "seed": i})
        codes[r.status_code] = codes.get(r.status_code, 0) + 1

    c.note(f"状态码分布: {codes}")
    c.ok(429 in codes, f"未观察到 429 —— 队列没有拒绝而是无限堆积: {codes}")
    c.ok(500 not in codes, f"出现 500，背压应当是 429 而非内部错误: {codes}")


def check_resources(c: Check) -> None:
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=index,memory.used,memory.total,utilization.gpu",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
    except Exception as exc:  # noqa: BLE001
        c.ok(False, f"无法查询 nvidia-smi: {exc}")
        return

    rows = []
    for line in out.splitlines():
        idx, used, total, util = (int(x) for x in line.split(","))
        rows.append((idx, used, total, util))
        c.note(f"GPU{idx}: {used:>6}/{total} MiB  util={util}%")

    loaded = [r for r in rows if r[1] > 20000]
    c.ok(len(loaded) >= 1, "没有任何 GPU 显存占用超过 20GB —— 后端模型似乎没加载")
    c.note(f"占用 >20GB 的卡: {[r[0] for r in loaded]}（含他人任务），请人工确认包含本服务的 GPU_ID")


# --------------------------------------------------------------------------

# (显示名, 是否需要 client, 是否需要 model)
CHECKS: list[tuple[str, str]] = [
    ("health", "check_health"),
    ("t2i", "check_t2i"),
    ("transparent", "check_transparent"),
    ("edit", "check_edit"),
    ("edit-multi-ref", "check_multi_ref"),
    ("async", "check_async"),
    ("openai-sdk", "check_openai_sdk"),
    ("negatives", "check_negatives"),
    ("resources", "check_resources"),
]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base", default="http://localhost:8090", help="网关地址")
    ap.add_argument("--model", default="Qwen/Qwen-Image-2.1")
    ap.add_argument(
        "--include-backpressure",
        action="store_true",
        help="额外跑队列背压检查（会真提交几十次生成，较慢且占用 GPU）",
    )
    args = ap.parse_args()

    todo = list(CHECKS)
    if args.include_backpressure:
        todo.append(("backpressure", "check_backpressure"))

    results: list[Check] = []
    with httpx.Client(base_url=args.base, timeout=TIMEOUT) as client:
        for name, fname in todo:
            c = Check(name)
            fn = globals()[fname]
            try:
                if fname in ("check_openai_sdk",):
                    fn(c, args.base, args.model)
                elif fname == "check_resources":
                    fn(c)
                elif fname in ("check_negatives", "check_backpressure"):
                    fn(c, client)
                else:
                    fn(c, client, args.model)
            except Failure:
                pass  # ok() 已经把失败信息记进 failures 了
            except Exception as exc:  # noqa: BLE001 - 连接失败等非断言异常也要计入失败
                c.failures.append(f"{type(exc).__name__}: {exc}")
            results.append(c)
            print(c.render(), flush=True)
            print(flush=True)

    failed = [c for c in results if not c.passed]
    print("=" * 60)
    if failed:
        print(f"通过 {len(results) - len(failed)}/{len(results)}，失败:")
        for c in failed:
            print(f"  - {c.name}")
        return 1
    print(f"全部 {len(results)} 项通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
