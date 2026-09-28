# Qwen-Image-2.1 图像生成 API 服务

对内提供 Qwen-Image-2.1 的文生图与图像编辑能力，OpenAI 兼容接口 + 异步任务接口。

> **许可提示**：Qwen-Image-2.1 采用 **Qwen Research License Agreement**（仅限研究与评估，
> **禁止商用**）。本服务定位为内部研发与评测用途，请勿对外提供商用服务。
> 服务启动时会在日志中打印同样的提示。

---

## 架构

```
浏览器 / OpenAI SDK / curl
      │
      ▼
宿主机 :8090        ← 全栈唯一的对外端口
      │
      ▼
web  :80   nginx，静态单页 + 反向代理           ← 新增
  ├─ /            前端单页（生成 / 编辑 / 画廊）
  ├─ /web-healthz nginx 自身探针
  └─ /v1 /files /healthz /docs  → 反代
      │
      ▼
gateway  :8090   FastAPI，纯 CPU（不发布宿主机端口）
  POST /v1/images/generations   同步
  POST /v1/images/edits         同步（multipart）
  POST /v1/tasks                异步 submit → 202
  GET  /v1/tasks/{id}           轮询
  DELETE /v1/tasks/{id}         取消（只改状态，不删图）
  DELETE /v1/tasks[?ids=a,b]    清空：删行 + 删图（不可撤销；有活跃任务时 409）
  GET  /v1/models · /healthz · /files/{id}/{name}
      │
      ▼
backend  :8091   diffusers 跑 QwenImage21Pipeline，独占一张 H100（不发布宿主机端口）
      │
      ▼
    H100 (GPU_ID)
```

**只有 8090 一个端口对外。** 网关与后端都只监听容器内的端口，不发布到宿主机。
验证：`ss -ltn | grep -E ':8090|:8091'` 应当只出现 8090。
`make smoke-web` 的 `host-ports` 检查就在断言这条约束 —— 谁把 `ports` 加回来，它会红。

排障时不能从宿主机直接 curl 网关，改用 compose 网络内部：

```bash
docker compose exec web curl -si http://gateway:8090/healthz | head -5
# gateway 镜像里没有 curl/wget（python:3.12-slim 基座），只能用 python
docker compose exec gateway python -c "import urllib.request as u; print(u.urlopen('http://127.0.0.1:8090/healthz').read().decode())"
```

**为什么中间要有一层网关？** 推理后端是第三方框架，不该去 fork 它。网关把异步任务、
OpenAI 兼容的缝隙填补（`response_format=url`、多参考图字段规整、透明背景与格式校验）
隔离出来；同时后端是可替换的 —— 本项目就换过一次（见下方「为什么是 diffusers」），
网关以上一行没改。

### 为什么是 diffusers 而不是 SGLang

原计划用 SGLang，**实测后被 CUDA 版本结构性堵死**，这是依赖互斥而非配置问题：

```
torch==2.13.0+cu129  依赖  cuda-bindings>=12.9.4,<13
sglang (Qwen-Image-2.1 支持 commit f9c2791)  依赖  cuda-python>=13.0
cuda-python>=13.0    要求  cuda-bindings>=13.0.0,<13.x
```

`torch 2.13.0` **没有 cu128 构建**（cu128 索引止于 2.11.0），而它的 cu129 构建又要求
`cuda-bindings<13` —— 与 sglang 的要求正好对撞。唯一出路是 cu130，即**驱动 ≥580**；
本机是 570.86.15（CUDA 12.8），升级需重启，会杀掉机器上 15 个在跑的容器
（含他人已运行数月的服务）。

diffusers 这条路依赖干净得多 —— 只要 `torch>=2.6` + `transformers`，没有 `cuda-python`，
torch 用 **cu128** 构建，与本机驱动精确匹配。

---

## 快速开始

```bash
cd /root/data1/kdi/workspace/qwen-image

make pull-weights     # 首次必做：下载 33GB 权重，约 40~60 分钟
make up               # 构建并启动，自动挑一张空闲 GPU
make status           # 看服务与 GPU 占用
make smoke            # 端到端验证
```

后端加载 33GB 权重约需 10 秒（实测），之后：

```bash
curl -s localhost:8090/healthz | jq
```

`make up GPU_ID=3` 可显式指定显卡。

### 前端

启动后浏览器打开 <http://localhost:8090/>，三个页签：

| 页签 | 用途 |
|---|---|
| 生成 | 文生图。支持尺寸预设、提示词模板、负向提示词、种子、张数、透明背景 |
| 编辑 | 图像编辑。拖拽 1~10 张参考图 + 指令 + 输出分辨率（自动 / 512 / 768 / 1024 / 1536） |
| 画廊 | 历史任务与产出，可下载、取消、只看自己提交的 |

全部走**异步任务接口**（`POST /v1/tasks` + 轮询），避免长请求被浏览器或代理掐断。

**不支持 mask 局部重绘** —— 后端 pipeline 没有 mask 参数，页面上刻意不提供该控件。

**多参考图在「自动」档会降到 512 分辨率**：上游在 `output_resolution ≥ 768` 时会忽略
编辑指令、近似原样返回最后一张参考图。页面选中 2 张以上时会明确提示，并实时显示预计
输出尺寸（= 最后一张参考图的长宽比 × `output_resolution²` 像素，取整到 32 的倍数，
**不一定是正方形**）。分辨率控件默认「自动」—— 不往请求里带这个字段，由后端决定；
显式选 ≥768 且挂了 ≥2 张参考图时，页面会给出「这是已知退化区间」的质量提示（不是报错）。

前端是零构建的原生 ES 模块，无 CDN 依赖（内网离线可用）：

```bash
make test-web        # 纯逻辑单元测试（node --test）
make smoke-web       # 经 nginx 的端到端验证
```

---

## 调用示例

### 文生图

```bash
curl -s localhost:8090/v1/images/generations \
  -H 'Content-Type: application/json' \
  -d '{"prompt":"a red apple on a wooden table","size":"1024x1024",
       "num_inference_steps":40,"seed":42}' \
  | jq -r '.data[0].b64_json' | base64 -d > apple.png
```

实测：1024×1024 / 40 步约 **8~10 秒**。

### 透明背景

```bash
curl -s localhost:8090/v1/images/generations \
  -H 'Content-Type: application/json' \
  -d '{"prompt":"a cartoon dragon sticker","size":"1024x1024",
       "background":"transparent"}' \
  | jq -r '.data[0].b64_json' | base64 -d > dragon.png
python3 -c "from PIL import Image; im=Image.open('dragon.png'); print(im.mode, im.getchannel('A').getextrema())"
# 期望: RGBA (0, 255) —— alpha 最小值为 0 才说明真的透明
```

> **注意**：模型**总是**输出带 alpha 通道的 RGBA 图。普通文生图的 alpha 范围是
> `(253, 255)`，即"有通道但没有透明像素"。所以判断透明是否生效**必须看 alpha 最小值
> 是否为 0**，只检查 `mode == "RGBA"` 会得到假阳性。
>
> `background=transparent` 的实现方式是给提示词加前缀（该 pipeline 没有 background
> 参数，透明完全由提示词驱动），用的是模型卡推荐的措辞。

透明背景会自动把 `output_format` 纠正为 `png`（jpeg 无 alpha 通道），
纠正结果在响应的 `x_notes` 字段里说明。

### 图像编辑

```bash
# 单参考图（标准用法，1024 分辨率下可靠）
curl -s localhost:8090/v1/images/edits \
  -F "prompt=add a blue circle next to the red square" \
  -F "image=@ref.png" \
  -F "num_inference_steps=40" | jq -r '.data[0].b64_json' | base64 -d > edited.png

# 多参考图
curl -s localhost:8090/v1/images/edits \
  -F "prompt=combine the red square and the blue circle into one composition" \
  -F "image=@ref_a.png" -F "image=@ref_b.png" \
  -F "num_inference_steps=40" | jq -r '.data[0].b64_json' | base64 -d > combined.png
```

**必须用 multipart**。最多 10 张参考图。

```bash
# 显式指定输出分辨率（256~2048，越界 400）。不传 = 后端按参考图张数自动选。
curl -s localhost:8090/v1/images/edits \
  -F "prompt=add a blue circle next to the red square" \
  -F "image=@ref.png" \
  -F "output_resolution=1024" \
  -F "num_inference_steps=40" | jq -r '.data[0].b64_json' | base64 -d > edited.png
```

`output_resolution` 同时决定输出的像素预算和**每张**参考图被缩放到多大，所以它不只是一
个输出尺寸开关 —— 多参考图时给大了会退化，见「已知限制」。输出尺寸按**最后一张**参考图
的长宽比从该预算推导，再取整到 32 的倍数，**不一定是正方形**。

### 异步任务

```bash
TASK=$(curl -s localhost:8090/v1/tasks -H 'Content-Type: application/json' \
  -d '{"prompt":"a lighthouse at dusk","size":"1024x1024","seed":3}' | jq -r .task_id)

curl -s localhost:8090/v1/tasks/$TASK | jq '{status,images}'
curl -s -X DELETE localhost:8090/v1/tasks/$TASK | jq
```

编辑任务也支持异步：`POST /v1/tasks` 带 multipart body 即可，字段与 `/v1/images/edits` 相同。

### OpenAI SDK

```python
from openai import OpenAI
client = OpenAI(base_url="http://localhost:8090/v1", api_key="not-needed")

resp = client.images.generate(
    model="Qwen/Qwen-Image-2.1",
    prompt="a single orange on a white plate",
    size="1024x1024",
    response_format="b64_json",
)
```

---

## 已知限制

### 多参考图在 ≥768 分辨率下退化为"复制最后一张"

**实测数据**（2 张参考图：红方 + 蓝圆，指令 "combine … into one composition"）：

| output_resolution | 输出中的红色像素 | 与参考图B的全图差 | 结果 |
|---|---|---|---|
| 1024（默认） | 0 | 4.02 | ❌ 近似原样返回参考图 B |
| 768 | 0 | 2.40 | ❌ 同上 |
| **512** | **1254** | 14.82 | ✅ 两张都用上了 |
| 768 + 提示词写 `<image1>/<image2>` | 0 | 2.76 | ❌ 标记也救不回来 |

这是上游 `QwenImage21Pipeline` 的行为，不是本服务的代码问题（单参考图在 1024 下正常）。

**本服务的处理**：多参考图且调用方未指定 `output_resolution` 时，自动降到 **512**，
并在响应的 `x_notes` 里明确说明。不会静默改参数 —— 调用方有权知道我们动了什么。

**这个值现在可以覆盖**：显式传 `output_resolution`（multipart 字段，或编辑页的分辨率
控件）时后端照用，**只做区间校验，不做质量判断**。代价就是上表那一行 —— 多参考图 +
≥768 时输出会退化成"近似原样返回最后一张参考图"，两张图都用不上。这是**出图变差，
不是报错**：任务照常成功，只是结果不是你要的。编辑页在「显式 ≥768 + ≥2 张参考图」
时会给出这个提示；选「自动」（不发该字段）则不会出现，因为后端会替你兜底。

**取值区间 `256~2048`**，越界返回 **400**（消息里带允许范围）。上界不是洁癖：该值同时
是**每张**参考图的缩放目标面积（约 `output_resolution²`），显存开销随参考图张数线性
放大，无上界时一个很大的值就能把这张共享卡打到 OOM，顺带拖垮同卡的其它作业。

### 其他

- **不支持 mask 局部编辑**：`QwenImage21Pipeline` 没有 mask 参数。后端收到 mask 会
  **明确返回 400**，而不是静默忽略 —— 调用方以为局部编辑生效了才是最糟的结果。
- **任务取消不彻底**：取消 `running` 任务时网关会标记 `canceled` 并丢弃结果，
  但**后端那一次生成仍会跑完**（diffusers 没有取消接口）。如实记录，不谎报。
- **无批处理**：diffusers 没有引擎级批处理，`MAX_CONCURRENCY` 默认 1（串行）。
  并发只会互抢显存、拖慢彼此。需要吞吐就加副本，每副本独占一卡。
- **无鉴权**：仅适合内网可信环境。
- **`true_cfg_scale > 1` 必须同时提供 `negative_prompt`**，否则 pipeline 会警告并
  以 cfg=1 实际运行（等于白设）。这是上游行为。

### 已修复的两个既有缺陷（2026-09-24 发现）

**异步任务的 `notes` 曾被静默丢弃。** `TaskRunner._notes_for()` 读的是 `notes_json`
列，但提交时 notes 存在 `request_json.notes` 里（`store.create()` 的 INSERT 不写
`notes_json`）；且 `_process()` 从不合并后端上报的 `result.notes`。结果是
`POST /v1/tasks` 的产物**永远带不出「多参考图已自动降到 512」这类说明**。
同步接口一直是对的（`routes/openai_images.py` 里 `all_notes = notes + result.notes`），
所以用同步接口写的冒烟测试掩盖了它。**前端只走异步路径，所以这个缺陷对 Web UI 是致命的。**
`scripts/smoke_web.py` 的 `async-notes` 检查现在盯着它。

**`scripts/smoke.py` 的路径穿越断言曾恒真。** 探针 `/files/../../etc/passwd` 是
5 段路径，而路由是 3 段 —— 永远匹配不上，返回的是 route-miss 404；且 `httpx` 在
客户端就把 `..` 规范化掉，`files/` 前缀根本没离开客户端。断言 `in (400, 404)`
因此必然通过，与网关的防护无关。已改为 3 段探针 `/files/short/n.png`，
断言收紧为 `== 400` 且消息含「非法的路径参数」。

---

## 配置

全部通过 `.env`（从 `.env.example` 复制）配置，无需改代码。

| 变量 | 默认 | 说明 |
|---|---|---|
| `GPU_ID` | 自动选卡 | GPU UUID。`make up` 会自动写入 `.env` |
| `MAX_CONCURRENCY` | 1 | 同步请求与异步 worker **共享**的并发上限 |
| `MAX_QUEUE_DEPTH` | 32 | 队列上限，满了返回 429 而不是无限堆积 |
| `REQUEST_TIMEOUT_S` | 600 | 单次生成超时 |
| `TASK_TTL_HOURS` | 24 | 结果与图片的保留时长 |
| `PUBLIC_BASE_URL` | `http://localhost:8090` | `response_format=url` 时的地址前缀 |
| `TORCH_DTYPE` | `bfloat16` | 实测显存占用 32.5GB |
| `CPU_OFFLOAD` | 0 | 置 1 分步卸载到 CPU，显存不够时救急 |
| `DEFAULT_RESOLUTION` | 1024 | 尺寸未指定时的边长 |
| `DATA_UID` / `DATA_GID` | 1001 | 网关容器以宿主机 `./data` 属主的 uid 运行 |
| — | `200m` | nginx `client_max_body_size`（`web/nginx.conf`）。**必须 ≥ `max_upload_bytes` × `max_ref_images`**，改一边要改另一边 |

### GPU 选择

`scripts/pick_gpu.py` 按「完全空闲优先」挑卡：要求 `utilization == 0 且 memory.used <= 1GB`，
候选内取最小 index。**没有任何卡满足时脚本直接报错退出**，不会退化为「挑剩余显存最大的卡」
—— 本机 5/6/7 号卡上有他人的训练任务，自动挑错卡会直接干扰别人的作业。

```bash
python3 scripts/pick_gpu.py --explain   # 看每张卡的判定过程
```

> **为什么用 GPU UUID 而不是 index**：`docker` 的 `device_ids` 里 index 走的是
> nvidia-container-runtime 自己的枚举顺序，与 `nvidia-smi` 的 index **不保证一致**。
> 本机实测：声明 `device_ids: ["1"]` 结果容器落在了 `nvidia-smi` 的 **2 号卡**上。
> 于是"挑一张空闲卡"的保护被架空 —— 挑的是 A 卡，跑的是 B 卡。UUID 没有这个歧义。

---

## 排障

**网关起不来，日志报 `Permission denied: '/app/data/outputs'`**
容器内用户 uid 与宿主机 `./data` 属主不一致。用 `id -u` / `id -g` 查出宿主机 uid，
设到 `.env` 的 `DATA_UID` / `DATA_GID`。

**后端 OOM**
按序尝试：调低 `DEFAULT_RESOLUTION` → 调低请求里的 `num_inference_steps` →
设 `CPU_OFFLOAD=1`（明显变慢但能跑）。

**权重下载慢或中断**
下载脚本默认走 ModelScope（实测 12MB/s，最快）。回退链：

```bash
SOURCE=hf ./scripts/pull_weights.sh    # HF 直连
```

> `hf-mirror.com` **不可用于 `hf download`**：它对 `/api` 与 `/resolve` 均返回
> 308 跳回 `huggingface.co`，是纯跳转不代理，`huggingface_hub` 的元数据校验会因此
> 报 "Distant resource does not seem to be on huggingface.co"。实测速度也最慢
> （5.55 MB/s vs ModelScope 12.06 MB/s）。

两者都支持断点续传，中断后重跑同一条命令即可。

**docker 构建拉不动基础镜像**
本机 docker daemon **不走代理**（systemd 服务不继承 shell 的 `http_proxy`），
直连 Docker Hub / 国内镜像源都不稳定。本项目的 compose 已配 `build.network: host` +
代理 build args，让容器内的 apt/pip/uv 走宿主机代理。

⚠️ **不要重启 docker daemon** —— `Live Restore` 未开启，重启会杀掉机器上全部 15 个容器
（含他人已运行数月的服务）。

---

## 实测基准

硬件：单张 NVIDIA H100 80GB HBM3，CUDA 12.8，torch 2.11.0+cu128。

| 项目 | 数值 |
|---|---|
| 权重加载 | 9.7 秒 |
| 常驻显存 | 32.5 GB（模型）/ 约 42 GB（含运行期激活） |
| 文生图 1024² / 40 步 | 8.3 ~ 9.9 秒 |
| 单图编辑 1024² / 40 步 | 10.4 秒 |
| 多图编辑 512² / 40 步 | 5.3 秒 |
