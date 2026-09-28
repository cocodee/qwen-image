#!/usr/bin/env python3
"""挑一张空闲 GPU，把标识打印到 stdout。

**默认输出 GPU UUID 而不是 index。** 这不是洁癖 —— 用 index 会出错：

    docker 的 `device_ids: ["1"]` 里的 "1" 是 nvidia-container-runtime
    自己枚举顺序下的第 2 个设备，而这个顺序与 `nvidia-smi` 的 index
    并不保证一致（本机实测：声明 "1" 结果落在了 nvidia-smi 的 2 号卡上）。
    于是"挑一张空闲卡"的保护会被架空 —— 挑的是 A 卡，跑的是 B 卡。
    UUID 没有这个歧义。

判据（见 README「GPU 选择」）：
  1. 候选 = utilization == 0 且 memory.used <= IDLE_MAX_MIB
  2. 候选内取最小 index（顺序稳定，便于复现）
  3. 无候选则报错退出 —— 本机 5/6/7 号卡跑着他人的训练任务，
     静默退化到「挑剩余显存最大的卡」会直接干扰别人的作业，宁可失败。
"""

from __future__ import annotations

import argparse
import subprocess
import sys

IDLE_MAX_MIB = 1024
QUERY = "index,uuid,memory.used,memory.total,utilization.gpu"


def read_gpus() -> list[dict]:
    try:
        out = subprocess.run(
            ["nvidia-smi", f"--query-gpu={QUERY}", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    except FileNotFoundError:
        sys.exit("pick_gpu: 找不到 nvidia-smi")
    except subprocess.CalledProcessError as exc:
        sys.exit(f"pick_gpu: nvidia-smi 执行失败: {exc.stderr.strip()}")

    gpus: list[dict] = []
    for line in out.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) != 5:
            continue
        try:
            idx, used, total, util = (int(p) for p in (parts[0], parts[2], parts[3], parts[4]))
        except ValueError:
            continue  # 跳过 N/A（如 MIG 或部分占用场景）
        gpus.append({"idx": idx, "uuid": parts[1], "used": used, "total": total, "util": util})
    if not gpus:
        sys.exit("pick_gpu: nvidia-smi 未返回任何可用 GPU")
    return gpus


def pick(gpus: list[dict], idle_max_mib: int) -> dict | None:
    idle = [g for g in gpus if g["util"] == 0 and g["used"] <= idle_max_mib]
    return min(idle, key=lambda g: g["idx"]) if idle else None


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--idle-max-mib",
        type=int,
        default=IDLE_MAX_MIB,
        help=f"视为空闲的显存占用上限，默认 {IDLE_MAX_MIB}",
    )
    ap.add_argument(
        "--format",
        choices=("uuid", "index"),
        default="uuid",
        help="输出 UUID（默认，无歧义）还是 nvidia-smi 的 index（仅供人看）",
    )
    ap.add_argument(
        "--explain",
        action="store_true",
        help="把每张卡的判定过程打到 stderr（stdout 仍只输出选定值）",
    )
    args = ap.parse_args()

    gpus = read_gpus()
    chosen = pick(gpus, args.idle_max_mib)

    if args.explain:
        print("idx  used/total MiB   util%   verdict   uuid", file=sys.stderr)
        for g in gpus:
            verdict = "PICKED" if chosen and g["idx"] == chosen["idx"] else (
                "idle" if g["util"] == 0 and g["used"] <= args.idle_max_mib else "busy"
            )
            print(
                f"{g['idx']:>3}  {g['used']:>6}/{g['total']:<6}  {g['util']:>5}   {verdict:<9} {g['uuid']}",
                file=sys.stderr,
            )

    if chosen is None:
        busy = ", ".join(f"GPU{g['idx']}({g['util']}%, {g['used']}MiB)" for g in gpus)
        sys.exit(
            "pick_gpu: 没有完全空闲的 GPU，拒绝自动选卡。\n"
            f"  当前: {busy}\n"
            "  如需强制指定，显式传 GPU_ID=<uuid 或 index>（请先确认该卡确实可用）。"
        )

    print(chosen["uuid"] if args.format == "uuid" else chosen["idx"])


if __name__ == "__main__":
    main()
