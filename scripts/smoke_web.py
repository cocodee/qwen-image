#!/usr/bin/env python3
"""Web 前端与 nginx 反代的端到端冒烟验证。

    python3 scripts/smoke_web.py
    python3 scripts/smoke_web.py --base http://other-host:8090

与 smoke.py 的分工：
  - smoke.py      验证网关 API 本身
  - smoke_web.py  验证**经 nginx 的代理路径**（静态托管、反代透传、上传上限、端口约束）

复用 smoke.py 的 Check 断言框架 —— 包括它的核心教训：Check 对象必须由调用方创建
并传入，否则断言失败会被外层吞掉、断言框架自己会说谎。
"""

from __future__ import annotations

import argparse
import io
import math
import subprocess
import sys
import time
from pathlib import Path

import httpx
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))
from smoke import Check, Failure  # noqa: E402

TIMEOUT = httpx.Timeout(600.0, connect=10.0)


def synth_png(side: int, noisy: bool = False, height: int | None = None) -> bytes:
    """造合成图。noisy=True 时用噪点让它压不小，用来测上传上限。

    `height` 不给就是正方形；给了就造 side×height。输出尺寸由**最后一张**
    参考图的长宽比推导，所以测尺寸推导必须能造非方形图。
    """
    import random

    w, h = side, height if height is not None else side
    if noisy:
        im = Image.new("RGB", (w, h))
        im.putdata([(random.randint(0, 255),) * 3 for _ in range(w * h)])
    else:
        from PIL import ImageDraw

        im = Image.new("RGB", (w, h), "white")
        ImageDraw.Draw(im).rectangle((w * 0.2, h * 0.2, w * 0.8, h * 0.8), fill="red")
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


def _predict_output_size(output_resolution: int, ref_w: int, ref_h: int) -> tuple[int, int]:
    """按 QwenImage21Pipeline 的公式独立算一遍输出尺寸，当预言机用。

    公式抄自 diffusers 的 `pipeline_qwenimage21.py`
    （`calculate_dimensions(output_resolution**2, image[-1].w / image[-1].h)`，
    再各自取整到 vae_scale_factor*2 = 32 的倍数）。

    这里**刻意不复用** `web/site/js/resolution.js` —— 前端那个是待验证对象，
    拿被测代码当预言机等于没测。两边各写一遍，谁漂了这条断言都会红。
    """
    ratio = ref_w / ref_h
    width = math.sqrt(output_resolution * output_resolution * ratio)
    height = width / ratio
    return round(width / 32) * 32, round(height / 32) * 32


def check_static(c: Check, client: httpx.Client) -> None:
    r = client.get("/")
    c.ok(r.status_code == 200, f"首页非 200: {r.status_code}")
    c.ok("text/html" in r.headers.get("content-type", ""),
         f"首页 content-type 不是 html: {r.headers.get('content-type')}")
    c.ok('id="app"' in r.text, "首页里没有挂载点 #app —— 是不是还在发占位页？")
    c.note(f"首页 {len(r.text)} 字节")

    r = client.get("/web-healthz")
    c.ok(r.status_code == 200, f"/web-healthz 非 200: {r.status_code}")

    for path, expect in (("/css/app.css", "css"), ("/js/app.js", "javascript"),
                         ("/js/api.js", "javascript"), ("/js/poll.js", "javascript"),
                         # 编辑页的分辨率预测靠它。没进镜像的话编辑页整页挂掉。
                         ("/js/resolution.js", "javascript")):
        r = client.get(path)
        ct = r.headers.get("content-type", "")
        c.ok(r.status_code == 200, f"{path} 非 200: {r.status_code}")
        c.ok(expect in ct, f"{path} content-type 应含 {expect}，实际 {ct}")


def check_proxy(c: Check, client: httpx.Client) -> None:
    """反代是否真的把请求交给了网关（而不是被 nginx 自己处理）。"""
    r = client.get("/healthz")
    c.ok(r.status_code == 200, f"/healthz 非 200: {r.status_code}")
    body = r.json()
    c.ok("backend" in body and "status" in body,
         f"/healthz 不是网关的响应形状: {str(body)[:200]}")
    c.note(f"网关状态 {body.get('status')}，排队 {body.get('queue_pending')}")

    # 未知 API 路由必须返回**网关的** JSON，而不是 nginx 自己的 HTML 404。
    # 判别式是 content-type —— Starlette 的 route-miss 404 绕过 openai_error_shape，
    # body 里没有 error 键（实测是 {"detail":"Not Found"}），按 error 断言会失败。
    r = client.get("/v1/definitely-not-a-route")
    ct = r.headers.get("content-type", "")
    c.ok(r.status_code == 404, f"未知路由应 404，实际 {r.status_code}")
    c.ok("application/json" in ct,
         f"未知路由的 content-type 不是 JSON（说明 nginx 自己回了 404，反代没生效）: {ct}")
    c.ok(r.json() == {"detail": "Not Found"},
         f"未知路由的 body 形状变了: {r.text[:200]}")
    c.note("反代透传确认：404 来自网关而非 nginx")


def check_guard(c: Check, client: httpx.Client) -> None:
    """路径参数校验经代理仍生效。

    探针用 3 段路径 + 非法 task_id。**不能用 /files/../../etc/passwd** ——
    那是 5 段，匹配不上 3 段的路由，返回的是 route-miss 404，
    与 ops.py 的校验无关（详见 spec 风险 3）。
    """
    r = client.get("/files/short/n.png")   # task_id="short" 仅 5 字符，不满足 _SAFE_ID{8,64}
    c.ok(r.status_code == 400, f"非法 task_id 应 400，实际 {r.status_code}: {r.text[:200]}")
    msg = r.json().get("error", {}).get("message", "")
    c.ok("非法的路径参数" in msg, f"400 但消息不对: {msg}")
    c.note("路径参数校验经代理生效")


def check_generate_roundtrip(c: Check, client: httpx.Client) -> None:
    """经代理提交 → 轮询 → 取图，全链路。"""
    r = client.post("/v1/tasks", json={
        "prompt": "a small green cube on a white background",
        "size": "512x512", "num_inference_steps": 20, "seed": 1234,
    })
    c.ok(r.status_code == 202, f"提交应 202，实际 {r.status_code}: {r.text[:300]}")
    body = r.json()
    task_id = body["task_id"]
    c.note(f"task_id={task_id}")

    # status_url 是按 PUBLIC_BASE_URL 拼的绝对地址。前端不该用它（见 spec），
    # 但冒烟里可以用它验证网关自报的地址是可达的。
    c.ok(body.get("status_url", "").startswith("http"),
         f"status_url 形状异常: {body.get('status_url')}")

    deadline = time.time() + 300
    state = None
    while time.time() < deadline:
        state = client.get(f"/v1/tasks/{task_id}").json()
        if state["status"] in ("succeeded", "failed", "canceled"):
            break
        time.sleep(2)

    c.ok(state and state["status"] == "succeeded",
         f"任务最终状态 {state and state['status']}，未成功")
    c.ok(bool(state.get("images")), "succeeded 但没有 images")

    img = state["images"][0]
    c.ok(img["width"] == 512 and img["height"] == 512,
         f"尺寸 {img['width']}x{img['height']}，期望 512x512")

    # ⭐ 关键：用 file + task_id 拼**相对**路径取图（这正是前端该做的）
    blob = client.get(f"/files/{task_id}/{img['file']}")
    c.ok(blob.status_code == 200, f"经代理取图失败: {blob.status_code}")
    got = Image.open(io.BytesIO(blob.content))
    got.verify()
    c.note(f"取回 {len(blob.content)} 字节，可解析为图片")


def check_upload_limit(c: Check, client: httpx.Client) -> None:
    """~2MB 参考图经代理不被 413 拒 —— 直接验证 nginx 的 client_max_body_size。

    默认值是 1m。这一条是本次改动最容易踩的坑：不设的话编辑功能直接残废。
    """
    blob = synth_png(900, noisy=True)
    size_mb = len(blob) / 1048576
    c.note(f"参考图 {size_mb:.2f} MB")
    c.ok(size_mb > 1.5, f"合成图只有 {size_mb:.2f} MB，测不出 1m 的默认限制")

    r = client.post(
        "/v1/tasks",
        data={"prompt": "add a small star in the corner", "num_inference_steps": 20},
        files=[("image", ("big.png", blob, "image/png"))],
    )
    c.ok(r.status_code != 413,
         f"被 nginx 以 413 拒了 —— client_max_body_size 没生效"
         f"（默认 1m，本测试用图 {size_mb:.2f}MB）")
    c.ok(r.status_code == 202, f"提交应 202，实际 {r.status_code}: {r.text[:300]}")


def check_async_notes(c: Check, client: httpx.Client) -> None:
    """异步任务的 notes 必须带上后端的说明（多参考图 512 降级）。

    前端**只走异步路径**，所以这条断言直接决定「服务端对参数的说明」能不能显示出来。
    它盯的是 Task 2 缺陷 B —— 修之前异步任务的 notes 恒为 []。
    """
    a = synth_png(512)
    b = synth_png(512)
    r = client.post(
        "/v1/tasks",
        data={"prompt": "combine them into one composition", "num_inference_steps": 20},
        files=[("image", ("a.png", a, "image/png")), ("image", ("b.png", b, "image/png"))],
    )
    c.ok(r.status_code == 202, f"提交应 202，实际 {r.status_code}: {r.text[:200]}")
    task_id = r.json()["task_id"]

    deadline = time.time() + 300
    state = None
    while time.time() < deadline:
        state = client.get(f"/v1/tasks/{task_id}").json()
        if state["status"] in ("succeeded", "failed", "canceled"):
            break
        time.sleep(2)

    c.ok(state and state["status"] == "succeeded", f"任务未成功: {state and state['status']}")

    # 后端对多参考图会自动降到 512，这个事实**只有服务端知道**，必须经 notes 传出来
    if state.get("images"):
        c.ok(state["images"][0]["width"] == 512,
             f"多参考图应降到 512，实际 {state['images'][0]['width']}")

    notes = state.get("notes") or []
    c.note(f"notes = {notes}")
    c.ok(bool(notes), "notes 为空 —— 异步路径把后端说明丢了（Task 2 缺陷 B 回归）")
    c.ok(any("512" in n for n in notes),
         f"notes 里没有 512 降级说明: {notes}")


def check_output_resolution(c: Check, client: httpx.Client) -> None:
    """output_resolution 必须真的能端到端传下去；越界必须被 400 拦住。

    盯的是两个回归：
      1. 网关 `edit_form.PASSTHROUGH_FORM_FIELDS` 漏了 output_resolution ——
         传了等于没传，2 张参考图恒为 512。更糟的是后端还会回一句「如确需高
         分辨率，请显式传 output_resolution」—— 唯一对外的入口根本传不进去，
         那句说明是空头支票。修之前实测：显式传 1024 出图仍是 512×512。
      2. 后端不设上界 —— 该值同时是**每张**参考图的缩放目标面积，开销随张数
         线性放大，越界直通共享 GPU 的 OOM。

    最后一张参考图刻意用非方形（768×512）：一条断言同时验证「显式值生效了」
    和「输出尺寸按最后一张参考图的长宽比推导，不是正方形」。

    自包含：只造自己这一个任务，跑完按 id 删掉（照 purge 的做法）。
    """
    t0 = time.time()
    ref_a = synth_png(512)               # 方图，第一张
    ref_b = synth_png(768, height=512)   # 3:2，**最后一张** —— 尺寸由它决定
    expected = _predict_output_size(1024, 768, 512)

    # ---- 1. 显式 output_resolution=1024 必须真的到达 pipeline ----
    r = client.post(
        "/v1/tasks",
        data={
            "prompt": "combine them into one composition",
            "num_inference_steps": 20,
            "output_resolution": 1024,
        },
        files=[("image", ("a.png", ref_a, "image/png")),
               ("image", ("b.png", ref_b, "image/png"))],
    )
    c.ok(r.status_code == 202, f"提交应 202，实际 {r.status_code}: {r.text[:300]}")
    mine = r.json()["task_id"]
    c.note(f"task_id={mine}，2 张参考图 + 显式 output_resolution=1024")

    state = _wait_terminal(client, mine)
    c.ok(state.get("status") == "succeeded",
         f"任务未成功: {state.get('status')} / {state.get('error')}")
    c.ok(bool(state.get("images")), "succeeded 但没有 images")

    img = (state.get("images") or [{}])[0]
    got = (img.get("width"), img.get("height"))
    notes = state.get("notes") or []
    c.note(f"输出 {got[0]}×{got[1]}，期望 {expected[0]}×{expected[1]}；notes={notes}")

    c.ok(got != (512, 512),
         f"显式 output_resolution=1024 没生效，输出仍是 {got[0]}×{got[1]} —— "
         f"网关 PASSTHROUGH_FORM_FIELDS 是不是又漏了 output_resolution？")
    c.ok(got == expected,
         f"输出 {got[0]}×{got[1]} 与按最后一张参考图(768×512) + 1024 推导的 "
         f"{expected[0]}×{expected[1]} 不符")
    c.ok(got[0] != got[1], f"输出是正方形 {got}，没有按参考图长宽比推导（前端的实时预览也会错）")
    c.ok(not any("自动设为" in n for n in notes),
         f"显式传了 output_resolution，却仍在报「已自动设为」: {notes}")

    # ---- 2. 越界必须 400（走同步接口：异步只会把任务标 failed，不返回 400）----
    # 上界
    r = client.post(
        "/v1/images/edits",
        data={"prompt": "x", "num_inference_steps": 1, "output_resolution": 9999},
        files=[("image", ("a.png", ref_a, "image/png"))],
    )
    c.ok(r.status_code == 400,
         f"越界 output_resolution=9999 应 400，实际 {r.status_code}: {r.text[:200]}")
    msg = r.json().get("error", {}).get("message", "")
    c.ok("2048" in msg and "256" in msg, f"400 但没说明允许范围: {msg}")
    c.note(f"9999 -> 400: {msg[:90]}")

    # 下界（只有上界是不够的）
    r = client.post(
        "/v1/images/edits",
        data={"prompt": "x", "num_inference_steps": 1, "output_resolution": 64},
        files=[("image", ("a.png", ref_a, "image/png"))],
    )
    c.ok(r.status_code == 400,
         f"越界 output_resolution=64 应 400，实际 {r.status_code}: {r.text[:200]}")

    # ---- 3. 收尾：删掉本检查造的任务 ----
    # 同步接口连失败请求也会落一行（openai_images._run_sync 的设计），
    # 上面两次 400 探针各留了一行 failed。只认「本次开始之后 + 错误文本点名
    # output_resolution」的行，绝不碰历史数据。
    stale = []
    for row in client.get("/v1/tasks", params={"limit": 20}).json().get("data", []):
        if row["status"] != "failed" or (row.get("created_at") or 0) < t0:
            continue
        detail = client.get(f"/v1/tasks/{row['task_id']}").json()
        if "output_resolution" in (detail.get("error") or ""):
            stale.append(row["task_id"])

    c.ok(_wait_idle(client), "等待队列清空超时，无法清理本检查创建的任务")
    doomed = [mine, *stale]
    r = client.delete("/v1/tasks", params={"ids": ",".join(doomed)})
    c.ok(r.status_code == 200 and r.json().get("deleted") == len(doomed),
         f"收尾清空失败: {r.status_code} {r.text[:200]}（期望删 {len(doomed)} 个）")
    c.note(f"已清理 {len(doomed)} 个任务（1 个生成 + {len(stale)} 个越界探针）")


def check_cancel_terminal(c: Check, client: httpx.Client) -> None:
    """对已终态的任务发 DELETE 必须是 409 —— 前端要把它当正常结果而非报错。"""
    rows = client.get("/v1/tasks", params={"limit": 20}).json().get("data", [])
    done = [t for t in rows if t["status"] == "succeeded"]
    if not done:
        c.ok(False, "近 20 个任务里没有 succeeded 的，无法测 409（先跑前面的生成检查）")
        return

    r = client.delete(f"/v1/tasks/{done[0]['task_id']}")
    c.ok(r.status_code == 409, f"对已终态任务取消应 409，实际 {r.status_code}: {r.text[:200]}")
    msg = r.json().get("error", {}).get("message", "")
    c.ok("终态" in msg, f"409 但消息里没有「终态」: {msg}")
    c.note(f"409 文案: {msg}")


def _wait_terminal(client: httpx.Client, task_id: str, timeout: float = 300.0) -> dict:
    """轮询到终态。超时返回最后一次观察到的状态（调用方自行断言）。"""
    deadline = time.time() + timeout
    state: dict = {}
    while time.time() < deadline:
        state = client.get(f"/v1/tasks/{task_id}").json()
        if state.get("status") in ("succeeded", "failed", "canceled"):
            return state
        time.sleep(2)
    return state


def _wait_idle(client: httpx.Client, timeout: float = 300.0) -> bool:
    """等到近 50 条里没有 queued/running —— 清空接口只要有活跃任务就 409。

    upload-limit 那条检查提交后**不等**它跑完，所以轮到这里可能还有任务在跑；
    活跃任务一定是最新的几条，limit=50 的窗口足够。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        rows = client.get("/v1/tasks", params={"limit": 50}).json().get("data", [])
        if not any(t["status"] in ("queued", "running") for t in rows):
            return True
        time.sleep(2)
    return False


def check_purge(c: Check, client: httpx.Client) -> None:
    """清空画廊：DELETE /v1/tasks[?ids=] 真的删行 + 删文件；有活跃任务时 409。

    ⚠️ 自包含：**只删本检查自己造的两个 task_id**，绝不碰其它检查依赖的数据
    （尤其是 data/tasks.db 里那批「文件已被手工删掉」的历史行 —— 那是使用场景，
    不是要修的东西）。位置也必须排在 cancel-terminal **之后**：那条需要一个
    已 succeeded 的任务，被我们删掉就没得测了。

    断言失败时可能残留一两个终态任务（等 TTL 清扫），这是刻意的取舍 ——
    为了清理而吞掉断言异常，就变成断言框架自己说谎了。
    """
    # ---- 1. 造一个自己的任务，等它 succeeded ----
    payload = {
        "prompt": "a red circle on a white background",
        "size": "512x512", "num_inference_steps": 20, "seed": 7,
    }
    r = client.post("/v1/tasks", json=payload)
    c.ok(r.status_code == 202, f"提交应 202，实际 {r.status_code}: {r.text[:300]}")
    mine = r.json()["task_id"]
    c.note(f"待清空 task_id={mine}")

    state = _wait_terminal(client, mine)
    c.ok(state.get("status") == "succeeded", f"任务未成功: {state.get('status')}")
    c.ok(bool(state.get("images")), "succeeded 但没有 images")
    name = state["images"][0]["file"]

    blob = client.get(f"/files/{mine}/{name}")
    c.ok(blob.status_code == 200, f"清空前取图应 200，实际 {blob.status_code}")

    # 清空接口只要库里还有活跃任务就 409（这正是它的设计），所以先等队列空
    c.ok(_wait_idle(client), "等待队列清空超时，无法验证按 id 清空")

    # ---- 2. 按 id 清空：行和图片文件都得没 ----
    r = client.delete("/v1/tasks", params={"ids": mine})
    c.ok(r.status_code == 200, f"按 id 清空应 200，实际 {r.status_code}: {r.text[:300]}")
    res = r.json()
    c.ok(res.get("deleted") == 1, f"deleted 应为 1，实际 {res}")
    c.ok(res.get("files_removed", 0) >= 1, f"files_removed 应 >= 1，实际 {res}")
    c.ok(res.get("bytes_freed", 0) > 0, f"bytes_freed 应 > 0，实际 {res}")
    c.note(f"按 id 清空返回 {res}")

    r = client.get(f"/v1/tasks/{mine}")
    c.ok(r.status_code == 404, f"清空后查任务应 404，实际 {r.status_code}: {r.text[:200]}")
    r = client.get(f"/files/{mine}/{name}")
    c.ok(r.status_code == 404,
         f"任务没了但图片还在（{name}）—— 文件没跟着行一起删，实际 {r.status_code}")

    # ---- 3. 显式 `ids=` 是**空选择**，删 0 个，绝不能退化成全清 ----
    r = client.delete("/v1/tasks", params={"ids": ""})
    c.ok(r.status_code == 200, f"ids= 应 200，实际 {r.status_code}: {r.text[:200]}")
    c.ok(r.json().get("deleted") == 0, f"显式 ids= 应删 0 个，实际 {r.json()}")
    rows = client.get("/v1/tasks", params={"limit": 5}).json().get("data", [])
    c.ok(bool(rows), "显式 ids= 把整库清空了 —— 空参数绝不能退化成「全清」")
    c.note("显式 ids= 返回 0 且库未被清空")

    # ---- 4. 有任务在排队/生成中 -> 409，且一个都不删 ----
    r = client.post("/v1/tasks", json=payload)
    c.ok(r.status_code == 202, f"提交应 202，实际 {r.status_code}: {r.text[:300]}")
    busy = r.json()["task_id"]

    r = client.delete("/v1/tasks")   # 不带 ids = 全清
    c.ok(r.status_code == 409,
         f"有活跃任务时全清应 409，实际 {r.status_code}: {r.text[:300]}")
    msg = r.json().get("error", {}).get("message", "")
    c.ok("排队或生成中" in msg, f"409 但消息不对: {msg}")
    c.note(f"409 文案: {msg}")

    r = client.get(f"/v1/tasks/{busy}")
    c.ok(r.status_code == 200, f"409 之后任务却不见了，实际 {r.status_code}")

    # ---- 5. 收尾：等它跑完，只删自己这一个 ----
    state = _wait_terminal(client, busy)
    c.ok(state.get("status") in ("succeeded", "failed"),
         f"收尾任务未结束: {state.get('status')}")
    r = client.delete("/v1/tasks", params={"ids": busy})
    c.ok(r.status_code == 200 and r.json().get("deleted") == 1,
         f"收尾清空失败: {r.status_code} {r.text[:200]}")
    c.note("已清理本检查创建的两个任务")


def check_host_ports(c: Check) -> None:
    """宿主机应只有 8090 一个 listener，没有 8091。

    ⚠️ 本检查读的是**执行本脚本这台机器**的 listener 表。用 --base 指向其他主机时
    它测的是错的机器 —— 那种情况下应当跳过而不是假装通过。
    """
    try:
        out = subprocess.run(["ss", "-ltn"], capture_output=True, text=True, check=True).stdout
    except Exception as exc:  # noqa: BLE001
        c.ok(False, f"无法执行 ss: {exc}")
        return

    has_8090 = ":8090" in out
    has_8091 = ":8091" in out
    c.ok(has_8090, "宿主机上没有 8090 —— nginx 没起来？")
    c.ok(not has_8091,
         "宿主机上出现了 8091 —— 后端/网关的 ports 发布被加回来了，"
         "这违反「对外只使用 8090」的约束")
    c.note("宿主机 listener: 8090 存在，8091 不存在")

    # 顺带看 compose 自己的状态
    try:
        ps = subprocess.run(
            ["docker", "compose", "ps", "--format", "{{.Service}}\t{{.Ports}}"],
            capture_output=True, text=True, check=True, cwd=Path(__file__).resolve().parent.parent,
        ).stdout.strip()
        c.note("compose 端口: " + " | ".join(ps.splitlines()))
    except Exception as exc:  # noqa: BLE001
        c.note(f"（跳过 docker compose ps: {exc}）")


CHECKS = [
    ("static", check_static),
    ("proxy", check_proxy),
    ("traversal-guard", check_guard),
    ("generate-roundtrip", check_generate_roundtrip),
    ("async-notes", check_async_notes),
    # 排在 upload-limit **之前**：upload-limit 提交完不等它跑，会留一个活跃任务，
    # 而本检查收尾时要等队列空才能按 id 清空。
    ("output-resolution", check_output_resolution),
    ("upload-limit", check_upload_limit),
    ("cancel-terminal", check_cancel_terminal),
    # 必须在 cancel-terminal 之后：那条依赖一个已 succeeded 的任务，
    # 而本检查会删任务；它也自己保证只删自己造的那两个 id。
    ("purge", check_purge),
]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base", default="http://localhost:8090", help="web 入口地址")
    ap.add_argument("--skip-host-ports", action="store_true",
                    help="跳过宿主机端口检查（--base 指向远程主机时必须跳过）")
    args = ap.parse_args()

    is_local = any(h in args.base for h in ("localhost", "127.0.0.1", "0.0.0.0"))
    todo = list(CHECKS)
    if args.skip_host_ports or not is_local:
        print(f"[跳过] host-ports（--base={args.base} 不是本机，测 listener 无意义）\n")
    else:
        todo.append(("host-ports", check_host_ports))

    results: list[Check] = []
    with httpx.Client(base_url=args.base, timeout=TIMEOUT) as client:
        for name, fn in todo:
            c = Check(name)
            try:
                if fn is check_host_ports:
                    fn(c)
                else:
                    fn(c, client)
            except Failure:
                pass  # ok() 已记录
            except Exception as exc:  # noqa: BLE001
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
