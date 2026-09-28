#!/usr/bin/env bash
# 预取 Qwen/Qwen-Image-2.1 权重（约 33GB）。
#
# 走 ModelScope。选它是因为实测（2026-09-23，本机，单流 30MB range）：
#   ModelScope            12.06 MB/s   <-- 最快，33GB 约 46 分钟
#   HF 直连 8 路并行       8.96 MB/s
#   hf-mirror              5.55 MB/s   且对 /api 与 /resolve 均 308 跳回 huggingface.co，
#                                      纯跳转不代理，hf download 的元数据校验会失败
#   HF 直连 单路           4.04 MB/s
#
#   SOURCE=modelscope|hf   默认 modelscope
#
# 可重复执行：两个后端都支持断点续传，中断后重跑同一条命令即可。
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MODEL_ID="${MODEL_ID:-Qwen/Qwen-Image-2.1}"
SOURCE="${SOURCE:-modelscope}"

export HF_HOME="${HF_HOME:-$REPO_ROOT/models/huggingface}"
DEST="$HF_HOME/${MODEL_ID##*/}"
NEED_GB=40

echo "SOURCE   = $SOURCE"
echo "MODEL_ID = $MODEL_ID"
echo "DEST     = $DEST"

mkdir -p "$HF_HOME"

# 空间检查：磁盘已 91% 占用，必须提前拦住
avail_gb=$(df -BG --output=avail "$HF_HOME" | tail -1 | tr -dc '0-9')
echo "可用空间 = ${avail_gb}G (需要 >= ${NEED_GB}G)"
if [ "$avail_gb" -lt "$NEED_GB" ]; then
    echo "错误：空间不足，至少需要 ${NEED_GB}G" >&2
    exit 1
fi

case "$SOURCE" in
    modelscope)
        echo "---- 开始下载 (ModelScope) ----"
        modelscope download --model "$MODEL_ID" --local_dir "$DEST"
        ;;
    hf)
        # 直连 HF。注意不要设 HF_ENDPOINT=hf-mirror.com —— 见文件头说明。
        export HF_ENDPOINT="${HF_ENDPOINT:-https://huggingface.co}"
        echo "---- 开始下载 (HF 直连, HF_ENDPOINT=$HF_ENDPOINT) ----"
        hf download "$MODEL_ID" --local-dir "$DEST" --max-workers 8
        ;;
    *)
        echo "错误：SOURCE 只能是 modelscope 或 hf，收到 '$SOURCE'" >&2
        exit 1
        ;;
esac

echo "---- 下载完成，校验 ----"
du -sh "$DEST"
echo "safetensors 分片数: $(find "$DEST" -name '*.safetensors' | wc -l)  (预期 7)"
echo
echo "提示：若某条路失败，换另一条重跑（断点续传，不会重下已完成的文件）："
echo "  SOURCE=hf $0"
