# Qwen-Image Web UI 设计

日期：2026-09-24
状态：已确认，待转实现计划

## 目标

为现有的 Qwen-Image-2.1 网关（`gateway/`，OpenAI 兼容 + 异步任务）提供一个浏览器界面，
覆盖**文生图**与**图像编辑**两个核心诉求，用 `docker compose` 部署，**对外端口 8090**。

非目标（明确不做）：

- 用户系统 / 鉴权（本服务定位内网可信环境，网关本身也无鉴权）
- 局部重绘（mask）—— 后端 pipeline 不支持，见「UI 契约」一节
- 提示词的模型侧改写 / 提示词工程辅助（不做「优化提示词」这类需要再调一次模型的功能）
- 图片编辑后处理（裁剪、调色等）—— 生成与编辑已足够

## 现状（设计前提）

| 事实 | 出处 |
|---|---|
| 网关发布 `0.0.0.0:8090`，纯 CPU FastAPI | `docker-compose.yml` |
| 后端发布 `127.0.0.1:8091`，跑 diffusers `QwenImage21Pipeline`，独占一张 H100 | `docker-compose.yml`、`backend/server.py` |
| 图片 URL 由 `PUBLIC_BASE_URL`（默认 `http://localhost:8090`）派生 | `gateway/app/config.py`、`gateway/app/tasks.py:persist_images` |
| `parse_size` 要求每条边 ∈ [256, 4096] 且 **是 32 的倍数** | `gateway/app/models.py` |
| `GET /v1/tasks` 只返回 `{task_id, kind, status, created_at, finished_at}`，**不含图片** | `gateway/app/routes/tasks.py:list_tasks` |
| `POST /v1/tasks` 按 Content-Type 分流：JSON=文生图，multipart=编辑 | `gateway/app/routes/tasks.py:submit_task` |
| 参考图字段名 `image`（网关也接受 `image[]`） | `gateway/app/config.py:edit_image_field`、`gateway/app/edit_form.py` |
| 后端**无 mask 参数**、**无 guidance_scale**、**无 size**（拆 width/height） | `backend/server.py` 模块 docstring |
| 多参考图 + `output_resolution ≥ 768` 退化为「近似返回最后一张参考图」 | README「已知限制」 |
| 本机 docker daemon **不走代理**，拉基础镜像不稳；`nginx:1.27-alpine` 本地已有 | README「排障」、`docker images` 实测 |
| 本项目**不是 git 仓库** | `git rev-parse` 实测 |

## 架构

```
浏览器 → 宿主机 :8090 ← 全栈唯一的对外端口
   │
   ▼
web  (nginx:1.27-alpine)          ← 新增
   ├─ /            → /usr/share/nginx/html  静态单页
   ├─ /web-healthz → 200（nginx 自身探针，不依赖 gateway）
   ├─ /v1/*        → proxy → gateway:8090
   ├─ /files/*     → proxy → gateway:8090
   └─ /healthz, /docs, /openapi.json, /redoc → proxy → gateway:8090
                        （只在 compose 网络内可达）
gateway  :8090（容器内）  ← 不发布任何宿主机端口（现在有，要删）
backend  :8091（容器内）  ← 不发布任何宿主机端口（现在有，要删）
```

**宿主机上只剩一个 listener：8090（nginx）**。网关与后端都只在 compose 网络的内部
端口上监听。落地后的判据是「**8090 存在、8091 不存在**」（按存在性判断，不按行数 ——
理由见冒烟第 10 条）。

两个服务都要删 `ports`：网关删掉 `0.0.0.0:8090:8090`，后端删掉 `127.0.0.1:8091:8091`。
后端那个虽然只绑回环、不算「对外」，但删掉后「全栈只有一个 listener」才是可断言的事实，
排障时也不必再逐个判断哪个端口是谁的 —— 一致性本身就是价值。

### 为什么是独立 nginx 容器

`web` 镜像**只做两个 COPY，没有任何 RUN 步骤** —— 构建过程不碰网络。在本机 docker
daemon 不走代理、拉基础镜像不稳定的前提下，这是决定性的：本方案不需要下载任何东西
（`nginx:1.27-alpine` 已在本地镜像列表中）。

同时 `README.md` 里全部 `curl localhost:8090/v1/...` 示例经反代**一个字都不用改**，
`PUBLIC_BASE_URL` 也保持 `http://localhost:8090` 不动。

### 为什么两个服务都不发布端口

网关现在的 `0.0.0.0:8090:8090` 与后端的 `127.0.0.1:8091:8091` 都要**整条删掉**，
而不是把网关改成 `127.0.0.1:8092:8090`。理由：

- 唯一的入口就是 nginx。留第二扇门，等于让「对外端口只有 8090」这句话变成假的 ——
  宿主机上任何进程（含其他容器）都还能直连网关，绕过 nginx 的 `client_max_body_size`
  与超时约束
- 网关本身**无鉴权**（README 明确），少一个暴露面就少一个
- 后端那个虽然只绑回环、不算「对外」，但一起删掉后「宿主机只有 8090」才成为
  **可断言的事实**（见冒烟第 10 条），排障时也不必逐个判断端口归属

**代价**：排障时不能再从宿主机直接 `curl`。改用 compose 网络内部执行。

两个容器的工具情况（已实测，**照抄前先看清楚**）：

| 容器 | 基础镜像 | 可用工具（实测） |
|---|---|---|
| `web` | `nginx:1.27-alpine` | `curl`（**真二进制** 8.12.1）、`wget` / `nc` / `netstat`（BusyBox）；**无** `ss` |
| `gateway` | `python:3.12-slim` | **无** `curl` 也**无** `wget`，只能用 `python` |
| `backend` | CUDA 基座 | `python` 可用 |

> `nginx:alpine` 里 **`curl` 是有的**（不少人以为 Alpine 系只有 BusyBox 工具集，
> 但 nginx 官方镜像显式装了 curl）。排障优先用它 —— `-i` 能同时给出状态行与响应头，
> 这正是区分「nginx 还是网关」时要看的东西；`wget -qO-` 只给 body。

```bash
# 从 web 打网关：看状态行 + 头，判断是否 nginx 层出错
docker compose exec web curl -si http://gateway:8090/healthz | head -5

# gateway 镜像里没有 curl/wget —— 必须用 python
docker compose exec gateway python -c "import urllib.request as u; print(u.urlopen('http://127.0.0.1:8090/healthz').read().decode())"

# 从网关打后端，区分「后端问题还是网关问题」
docker compose exec gateway python -c "import urllib.request as u; print(u.urlopen('http://backend:8091/healthz').read().decode())"
```

区分故障层次：`web` 里能打通 `gateway:8090` 而浏览器打不通 8090，问题在 nginx；
`gateway` 里打不通 `backend:8091`，问题在后端。

### nginx 必须显式设置的项

| 配置 | 值 | 不设的后果 |
|---|---|---|
| `client_max_body_size` | `200m` | 编辑上传 2MB 图就被 413 拒（默认 1MB）—— 而这是核心功能 |
| `proxy_read_timeout` | **`700s`** | 默认 60s 会掐断。**必须大于网关自身的最坏耗时**：等并发名额 60s (`_SLOT_WAIT_S`) + 后端生成 600s (`REQUEST_TIMEOUT_S`，来自 compose 而非 `config.py` 的 300 默认值) = 660s。取 600s 会让 nginx 先返回 **HTML 504**，把网关的 JSON 错误体盖掉 |
| `proxy_send_timeout` | **`700s`** | 同上 |
| 静态资源 `location ~ \.` | `try_files $uri =404` | 若回退成 index.html，缺失的 `.js`/`.json` 会返回 HTML+200，浏览器报的是看不懂的 MIME 错误 |
| `/v1/` 与 `/files/` 的 `^~` | 必须 | 少了它，上面那条正则会抢走 `/files/{id}/img_000.png`，**所有图片显示全挂** |
| `proxy_request_buffering` | `off` | 10 张图（最多 160MB）全落 nginx 磁盘再转发 |
| `proxy_buffering` | `off` | 图片响应经 nginx 中转落盘再发 |
| `add_header Cache-Control "no-cache"` | on `/` | 改一行 JS 刷新看不到效果（本项目无构建、无内容哈希） |
| `proxy_set_header Host/X-Real-IP/X-Forwarded-*` | 透传 | 网关侧看不到真实来源 |

## 前端结构

原生 ES 模块，**零构建步骤、零 CDN 依赖**（内网离线可用）。

```
web/
├── Dockerfile            # FROM nginx:1.27-alpine，无 RUN
├── nginx.conf
└── site/
    ├── index.html        # 三个 tab：生成 / 编辑 / 画廊
    ├── css/app.css
    └── js/
        ├── api.js        # 网关封装：submitGeneration / submitEdit / getTask / listTasks / cancelTask
        ├── errors.js     # 归一化 {error:{message}} / {detail} / 纯文本 / 网络异常 → ClientError
        ├── poll.js       # 带退避的轮询器，AbortController 可控
        ├── presets.js    # 尺寸预设 / 提示词模板 / 常用负向提示词（纯数据，用户明确要求）
        ├── store.js      # localStorage：表单参数、我提交的 task_id、详情缓存
        ├── views/
        │   ├── generate.js
        │   ├── edit.js
        │   └── gallery.js
        └── app.js        # hash 路由、toast、公共渲染
```

选 ES 模块而非单文件：`presets.js` 是纯数据、`api.js` 是纯 IO、每个 view 各自独立，
可以单独改动而不牵动其他部分。

### 相对路径取图（重要）

任务响应里 `images[].url` 是按 `PUBLIC_BASE_URL` 拼的**绝对地址**
（`gateway/app/tasks.py:persist_images` 中用 `settings.public_base_url` 拼接）。
从局域网 IP 访问时该地址指向 `localhost` 而裂图。

**前端一律用 `images[].file` + `task_id` 自行拼相对路径** `/files/{task_id}/{file}`。
这样不管从 `localhost:8090` 还是 `192.168.x.x:8090` 访问都正确。

**同一个坑还有第二处**：202 响应里的 `status_url` 也是用 `public_base_url` 拼的
（`gateway/app/routes/tasks.py:submit_task`）。前端轮询必须自己拼相对路径
`/v1/tasks/{task_id}`，**不要用 `status_url`** —— 否则从局域网访问时轮询会打到
用户自己机器的 localhost。

### 尺寸预设（已实测校验）

`gateway/app/models.py:parse_size` 要求每条边是 **32 的倍数**。实测：

| 预设 | 结果 |
|---|---|
| `1024x1024` `1024x768` `768x1024` `1344x768` `768x1344` `1536x1024` `1024x1536` `512x512` `512x768` | ✅ 通过校验 |
| `1280x720`（h%32=16）、`720x1280`（w%32=16） | ❌ **会被 400 拒**，不收录 |

预设库只收录左列的值。自定义输入框按同一规则做前置校验。

## UI 契约 —— 不提供无效控件

这是本设计的重点。项目 README 用大篇幅记录了上游能力边界，**前端绝不能用看起来
能用、实际被后端忽略的控件去骗使用者**。以下每一条都对应一个具体的上游事实：

| 决定 | 依据 |
|---|---|
| 编辑页**不提供 mask 上传**，并直接写明「不支持局部重绘」 | 后端 `QwenImage21Pipeline` 无 mask 参数，收到 mask 明确返 400 (`backend/server.py:edits`)。给出控件等于承诺局部重绘 |
| **不暴露 `guidance_scale`** | diffusers 后端只读 `true_cfg_scale` (`backend/server.py:_generate`)，`guidance_scale` 是死参数 |
| `true_cfg_scale ≠ 1` 且负向提示词为空 → **前置警告** | README：上游会静默退化成 cfg=1，等于白设 |
| **提供 `output_resolution` 控件**（自动 / 512 / 768 / 1024 / 1536），默认「自动」 | 该参数既决定输出像素预算，也决定每张参考图的缩放目标，不给控件就只能吃后端默认值。**「自动」必须不发该字段**，由后端按张数决定（单图 1024 / 多图 512），否则今天的策略会被冻进每一次请求 |
| 分辨率**自动档 + 参考图 ≥2 张**时明示「后端会降到 512」 | 上游在 `output_resolution ≥ 768` 时退化成「近似原样返回最后一张参考图」。后端已自动降到 512 (`backend/server.py:edits`)，但必须让使用者知道 |
| 分辨率**显式 ≥768 且参考图 ≥2 张**时给出**强警告**（「可能比 512 更差」，非报错） | 同上；此时是调用方自选进入退化区间，后端**不会**替他改参数（只做 `256~2048` 区间校验），所以只能由前端说清后果 |
| 分辨率控件下方**实时显示预计输出尺寸**，并说明「不一定是正方形」 | `QwenImage21Pipeline.__call__`：输出按**最后一张**参考图的长宽比、以 `output_resolution²` 像素为预算推导，两边各自取整到 `vae_scale_factor*2 = 32` 的倍数。公式与预测函数见 `web/site/js/resolution.js`，由 `web/tests/resolution.test.mjs` + `scripts/smoke_web.py:check_output_resolution` 双向钉住 |
| **`x_notes` / `notes` 原样渲染**在结果区 | 网关/后端对我们参数的每次纠正都要可见 (`gateway/app/routes/tasks.py:_to_status`) |
| 取消按钮文案：「取消排队 / 丢弃结果（后端当次仍会跑完）」 | README：取消不彻底，diffusers 没有取消接口。不能写「已终止生成」 |
| 画廊图片加载失败 → 显示「已过期（24h TTL）」，不显示破图 | 任务与图片有 `TASK_TTL_HOURS=24` 的 TTL |
| 并发上限 1、队列深度 32 的**背压状态可视化** | `/healthz` 的 `queue_pending` / `queue_capacity` 直接展示 |

## 数据流

```
生成：表单 → 前端校验（size 32 倍数 / steps 整数 / seed 整数 / n≤10）
     → POST /v1/tasks (JSON) → 202 {task_id}
     → 轮询 GET /v1/tasks/{id} → succeeded
     → 用 images[].file + task_id 拼相对路径渲染 → 下载按钮

编辑：拖拽/选择 1~10 张 → 预检（格式 / 单张 ≤16MB / 总数 ≤10，与网关同值）
     → 选分辨率（默认「自动」= 该字段不进 FormData）
     → 读最后一张参考图的尺寸 → 算预计输出 W×H 并显示（纯函数见 resolution.js）
     → FormData，参考图用**重复的** `image` 字段
     → POST /v1/tasks (multipart) → 同上
```

统一走**异步任务**而非同步端点：同步端点取不到并发名额时只等 60s 就 429
(`gateway/app/routes/openai_images.py:_SLOT_WAIT_S`)，而队列深度有 32。
轮询还能拿到 `queued`/`running`/`succeeded` 状态与 `timings`。

### 画廊数据获取

`GET /v1/tasks` 不含图片，因此画廊需要「列表 + 逐个详情」：

1. `GET /v1/tasks?limit=50` 渲染列表骨架（含状态、时间）
2. 对终端状态（`succeeded`）的任务，以**并发 4** 拉 `GET /v1/tasks/{id}` 取图片
3. 详情缓存进 localStorage，切换 tab 不重复拉
4. 图片加载失败 → 标记「已过期（24h TTL）」

### 画廊可见范围 ⚠️

本服务**无鉴权**，`GET /v1/tasks` 返回**所有**任务，不只当前浏览器提交的。
画廊会展示他人的产出。

**决定**：默认显示「全部」，同时提供「只看我提交的」切换 —— 用 localStorage 记录
本浏览器提交过的 `task_id` 集合。这是内网研发工具的合理默认值。

## 错误处理

统一成 `ClientError {status, kind, message}`，`kind` 驱动文案：

| 情形 | kind | 文案 / 行为 |
|---|---|---|
| 429 队列满 / 并发占满 | `busy` | 服务繁忙，稍后重试 —— **不清空表单** |
| 507 显存不足 | `oom` | 降低尺寸或步数 |
| 400 size 非法 | `bad-request` | 回显后端原文（实测消息可读：`height=720 必须是 32 的倍数`） |
| **409 任务已处于终态** | `noop` | **不是错误**：样式为中性提示「任务已结束，无需取消」 |
| 任务 404 / 图片 404 | `expired` | 任务不存在或已过期（24h） |
| 轮询连续失败 3 次 | `network` | 顶部警告条 + 指数退避继续轮询，上限 10s |
| 5xx | `server` | 内部错误 + 可复制的 task_id |

**409 必须单独一行**：`DELETE /v1/tasks/{id}` 对任何已处于终态的任务都返回
409（`gateway/app/store.py:167` 抛 `TaskNotCancelable`）。取消按钮双击、或任务
刚好在这一刻成功，都会走到这里 —— 这是**正常结果**，不能渲染成红色报错。
`scripts/smoke.py` 自己就容忍 409（`d.status_code in (200, 409)`）。

错误体的**形状**（以下均为实测线格式，不是推断）：

| HTTP | 实际 body |
|---|---|
| 400 / 409 / 422 / 429（经 `HTTPException`） | `{"error":{"message":"<文本>","type":"invalid_request_error","code":<n>}}` |
| 500 / 507（经 `HTTPException`） | 同上外壳，但 `type` 是 **`"server_error"`** —— `gateway/app/main.py:88` 按 `status_code >= 500` 分流 |
| **404 任务/文件不存在**（`expired` kind） | `error` 外壳 + 可读中文：`{"error":{"message":"任务不存在或已过期",…,"code":404}}` |
| **404 路由未命中** | `{"detail":"Not Found"}` —— Starlette 的 route-miss **绕过**了 `openai_error_shape`，是**另一种形状** |
| nginx 层 | HTML |

注意 **404 有两种形状**，别混：任务/文件不存在的 404 走 `HTTPException`，带 `error` 外壳和
可读中文（这才是 `expired` kind 要解析的）；路由未命中的 404 是裸 `{"detail":...}`。
前者是运行时会遇到的，后者只在冒烟测试 7 里当代理探针用。

两个由此推出的实现要求：

1. **422 的 `message` 必须美化后再展示**。实测原文是：
   ```
   [{'type': 'missing', 'loc': ('prompt',), 'msg': 'Field required',
    'input': {}, 'url': 'https://errors.pydantic.dev/2.13/v/missing'}]
   ```
   这是给开发者看的，不是给使用者看的。`errors.js` 需从中提取 `msg` 与 `loc`
   拼成「prompt：必填项缺失」。*「回显后端原文」这条规则**不适用于 422*** ——
   400 的消息是精心写过的中文，422 的不是。
2. **`errors.js` 必须按 `error.message` → `detail` → 纯文本 的顺序探测**，
   且 `detail` 可能是字符串也可能是列表。不能假定三种情形共用一种形状。

切 tab / 关页面**不取消**服务端任务 —— 回来能接着看到结果（任务在服务端继续跑）。

## 测试与验收

新增 `scripts/smoke_web.py`，沿用 `scripts/smoke.py` 的 `Check` 断言框架
（包括它的核心教训：Check 对象必须由调用方创建并传入，否则断言框架自己会说谎）。

经 **nginx** 打端到端：

1. `GET /` → 200 且 `Content-Type: text/html`
2. `GET /web-healthz` → 200
3. `GET /healthz` 经代理 → gateway 的 JSON（含 `status` / `backend` 字段）
4. `POST /v1/tasks` 经代理 → 轮询 → `succeeded`
5. 用 `images[].file` 拼 `/files/{task_id}/{name}` 取回，**PIL 能打开**
6. **~2MB 参考图走 `/v1/tasks` multipart 不被 413 拒** ← 验证 `client_max_body_size`
7. `GET /v1/definitely-not-a-route` → 404 且 **`Content-Type: application/json`**，
   body 为 `{"detail":"Not Found"}` ← 证明反代透传的是 gateway 的响应而非自己的 HTML 404。
   *断言的判别式是 **content-type**，不是 body 形状* —— Starlette 的 route-miss 404
   绕过 `openai_error_shape`，body 里**没有** `error` 键，按 `error` 断言会失败（已实测）
8. `GET /js/app.js` → 200 且 `Content-Type` 是 JavaScript
9. `DELETE /v1/tasks/{已成功的任务 id}` → 409，且错误消息含「终态」
   ← 锁定 §错误处理 里那条「正常结果、不该报红」的契约
10. **宿主机 listener 检查**：`ss -ltn` 里**应存在 8090、不存在 8091**
    ← 这直接断言「对外只使用 8090」这条硬约束。若哪天有人手滑给网关加回 `ports`，
    这里会红

    这项检查**只在 `smoke_web.py` 跑在部署宿主机上时才有意义**：它读的是
    **本机**的 listener 表。带 `--base http://其他主机:8090` 时会静默地测错机器。
    实现时按本机检查并说明这一前提；若日后宿主机上出现无关容器占用 8091，
    需把检查收窄到 compose 项目自己的发布端口

第 6、7、10 条是本设计里会真出问题的地方，所以单独断言。第 10 条尤其重要 ——
它是唯一能自动发现「端口约束被回退」的检查，人工 `make status` 不会报错。

**第 10 条怎么写**：断言写成「**8090 存在、8091 不存在**」，不要写成「8090 恰好一行」。
本机实测两者都只有一行（`daemon.json` 未开 IPv6，docker-proxy 只绑 IPv4），
但按行数断言会把「环境是否开 IPv6」这个无关变量绑进测试 —— 换台机器就可能红。
按「存在性」断言则在哪都对。

`make smoke` 的默认 `--base http://localhost:8090` 不变 —— 从此请求会**经过 nginx**，
所以它顺带验证了代理路径上的基础连通性（健康检查、文生图、编辑、异步任务、取图）。
但**不要**因此认为它覆盖了穿越防护：见 §未决/已知风险 第 3 条，那条断言是假通过，
本设计要求一并修掉。

新增 Makefile 目标：

```
make smoke-web      跑 scripts/smoke_web.py
make logs-web       跟踪 web 容器日志
```

## 影响面与回滚

| 变化 | 说明 |
|---|---|
| 网关端口 | `ports: ["0.0.0.0:8090:8090"]` **整条删除** |
| 后端端口 | `ports: ["127.0.0.1:8091:8091"]` **整条删除** |
| 对外监听 | 从「网关占 8090、后端占 8091」变为**宿主机只有 8090（nginx）** |
| `PUBLIC_BASE_URL` | **不改**，仍是 `http://localhost:8090` |
| `make smoke` | 命令不变，但从此走代理路径（`--base http://localhost:8090` 现在指向 nginx） |
| `scripts/smoke.py` | **要改一处既有断言**：穿越探针 `in (400, 404)` 恒真、从未验证过防护，改为 `/files/../x.png` + `== 400`（见风险 3） |
| 前端新增 | 全新目录 `web/`，不触碰 `gateway/` 与 `backend/` 任何代码 |

**本次改动既有代码的地方有两处**（都是本设计暴露出来的既有缺陷，不是它引入的）：

1. `scripts/smoke.py` —— 修一条恒真的穿越断言（见风险 3）
2. `gateway/app/tasks.py` —— 修异步任务的 notes 被丢弃：`_notes_for` 读的是
   `notes_json` 列，但提交时的 notes 存在 `request_json.notes` 里；同时 `_process`
   从不合并后端上报的 `result.notes`。后果是前端**只走异步路径**，
   「服务端对参数的说明」永远显示不出来 —— 包括多参考图的 512 降级说明。
   同步路径一直是对的，所以既有冒烟测试掩盖了它。

（`docker-compose.yml` 的两条 `ports` 与 `Makefile` 的新增目标不算改代码。）
它是既有缺陷，不是本设计引入的 —— 但既然本设计把「代理路径的验证」当成交付内容之一，
就不能一边宣称覆盖、一边留一条恒真的断言在那儿充数。

**回滚**：删掉 compose 里的 `web` 服务，把两条 `ports` 加回去，`docker compose up -d`。
前端目录留着不影响任何东西。

### 全仓 `8091` 引用的处置（已 grep，共 9 处）

只有 **1 处**需要删。其余 8 处都是**容器内寻址**，删掉宿主机端口映射不影响它们：

| 位置 | 处置 | 理由 |
|---|---|---|
| `docker-compose.yml:34` `ports: ["127.0.0.1:8091:8091"]` | **删除** | 这就是要拿掉的宿主机发布 |
| `docker-compose.yml:51` 健康检查探 `127.0.0.1:8091` | 保留 | 在**容器内**跑，走容器回环，与宿主发布无关 |
| `docker-compose.yml:71` `BACKEND_BASE_URL=http://backend:8091` | 保留 | compose 网络内按服务名寻址 |
| `backend/Dockerfile.diffusers:81` `PORT=8091` | 保留 | 容器内监听端口 |
| `backend/Dockerfile.diffusers:83` `EXPOSE 8091` | 保留 | `EXPOSE` 只是元数据，**不等于**发布到宿主机 |
| `backend/Dockerfile.diffusers:85` 健康检查探 `127.0.0.1:8091` | 保留 | 同 compose:51 |
| `backend/server.py:327` `port=8091` | 保留 | 容器内监听端口 |
| `gateway/app/config.py:16` 默认值 `http://localhost:8091` | 保留 | 仅容器外直跑网关时生效，compose 里被 env 覆盖 |
| `README.md:26` 架构图里的 `backend :8091` | 保留 | 描述的是容器内端口，本来就对 |

**最容易误判的是 `EXPOSE 8091`**：它只写进镜像元数据、供 `-P` 与人工阅读用，
本身**不发布任何端口**。删掉 `docker-compose.yml` 的 `ports` 才是真正生效的动作。

`Makefile` **不含** `8091`/`8092` 引用（`make status` 用 `docker compose ps`，自动反映端口；
`make gate` 只跑 `docker run`，不碰端口）—— 两者都不受本次改动影响。

> **grep 方法提醒**：`Dockerfile.diffusers` 无扩展名，用
> `grep --include='*.py' --include='*.yml' ...` 这类白名单会**整份漏掉它**
> （本设计初稿就这么漏了 3 处）。要么不带 `--include`，要么用 `--exclude-dir` 做减法。
> 审计类 grep 一律用减法，白名单只适合已知文件类型齐全的场景。

> **端口交接已实测**：删掉 `ports` 后 `docker compose up -d` 会输出
> `Container qwen-image-gateway-1  Recreated` 并真的释放宿主机端口
> （一次性 compose 实测：改前宿主机 18099 有 1 个 listener，改后为 0）。
> 所以「网关让位、nginx 接管 8090」不需要手工 `--force-recreate`。
>
> 落地时若真遇到 8090 被占（`bind: address already in use`），重跑一次
> `docker compose up -d` 即可 —— 此时旧网关容器已被换掉。这条不作为已知风险记录，
> 只是给首次部署留个应对。

## 未决 / 已知风险

1. **`/docs`（Swagger UI）依赖 CDN**：FastAPI 的 Swagger UI 从 jsdelivr 加载 JS/CSS，
   内网无外网时 `/docs` 会白屏。反代仍保留（便于有外网时调试），但**不作为交付面**，
   文档入口以前端页面内的「API 说明」为准。
2. **`proxy_pass` 的端口与 URI 后缀，两处都容易写错**。正确写法是
   `location /v1/ { proxy_pass http://gateway:8090; }`：
   - **端口不能省**。网关容器内监听 8090（`uvicorn --port 8090`、`EXPOSE 8090`），
     写成裸 `http://gateway` 会打到 80 端口，每个请求都 502。
   - **不能带 URI 后缀**。若误写成 `proxy_pass http://gateway:8090/;`，
     `/v1` 前缀会被替掉，网关全部 404。

   冒烟检查 3/4/7 就是盯这两处的。
3. **`scripts/smoke.py` 的路径穿越断言一直在「假通过」—— 这是既有缺陷，与本次改动无关**。

   探针是 `GET /files/../../etc/passwd`。它**从来没能验证网关的防护**，原因有三层，
   而且**没有一层与 nginx 有关**（初稿曾误归因给 nginx 的 `..` 规范化，那是错的）：

   | 层 | 事实 |
   |---|---|
   | URL 形状 | 该路径是 **5 段**，而路由是 `/files/{task_id}/{name}` 共 **3 段** —— 永远匹配不上，返回 route-miss 404。用 `curl --path-as-is` **直连网关**（完全不经代理）同样是 `{"detail":"Not Found"}` |
   | 客户端 | `httpx`（`smoke.py` 用的）在**发出前**就把它改写成 `http://localhost:8090/etc/passwd` —— `files/` 前缀根本没离开客户端 |
   | 断言 | `status_code in (400, 404)` 对 route-miss 404 恒真 |

   三者叠加，断言必然通过，与网关 `gateway/app/routes/ops.py:62` 的 `_SAFE_ID` / `_SAFE_NAME`
   校验**毫无关系**。所以「`make smoke` 会自动多验一层代理」这句话**不成立**。

   **能真正打到防护的是 3 段路径**（实测 `GET /files/../healthz` →
   `400 {"error":{"message":"非法的路径参数"}}`，此时 `task_id=".."` 过不了 `_SAFE_ID`）。

   **本设计的处置**：`scripts/smoke_web.py` 用 `/files/../x.png` 作穿越探针，
   并**顺带修正 `scripts/smoke.py` 里那条既有断言**（改探针 + 收紧断言为
   `status_code == 400 且 message 含「非法的路径参数」`）。
   不修的话，它还会继续以假通过的形式掩盖防护失效。

   > 走 nginx 时 `..` 确实会被规范化 —— 但那只是让**已经恒真**的断言多一个恒真的理由，
   > 不是它假通过的原因。诊断到这一层才不至于修错地方。
4. **`client_max_body_size 200m` 的依据**：`max_upload_bytes` 是单张 16MB、最多 10 张，
   理论峰值 160MB，故留 200m。若日后调大 `max_upload_bytes`，此处要同步。**这个耦合要写进注释**
   —— 两处分别定义在 `gateway/app/config.py` 和 `web/nginx.conf`，改一处不知道要改另一处。

   > 已知但**未修**的相关问题（Task 1 质量评审提出，刻意延后）：`gateway/python:3.12-slim`
   > 容器没有内存上限，且 `edit_form.py` 的 `await ref.read()` 是先整份读进内存再判大小，
   > `mask` 那一支压根没有大小校验。本次改动**改善了**这一点（改动前网关直接对外、
   > 完全没有上限，200m 是新增的边界），但要根治需要在 compose 给 `gateway` 加
   > `mem_limit` 并在网关侧做总请求大小校验。
5. **异步任务 notes 曾被静默丢弃（已修）**：见 §影响面与回滚。
   修好后 `scripts/smoke_web.py` 的 `async-notes` 检查会盯住它 ——
   前端依赖 notes 显示服务端纠正，回归了必须立刻发现。
