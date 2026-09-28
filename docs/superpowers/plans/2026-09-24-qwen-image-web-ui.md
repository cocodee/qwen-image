# Qwen-Image Web UI 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为 Qwen-Image-2.1 网关提供一个支持文生图与图像编辑的浏览器界面，用 `docker compose` 部署，对外只有 8090 一个端口。

**Architecture:** 新增一个 `web` 容器（`nginx:1.27-alpine`）持有宿主机 8090，托管静态单页并把 `/v1`、`/files`、`/healthz` 反代给现有网关。网关与后端**都撤掉宿主机端口发布**，只留在 compose 网络内。前端是零构建的原生 ES 模块，纯逻辑用 `node --test` 做单元测试，跨进程的代理契约用 `scripts/smoke_web.py` 端到端验证。

**Tech Stack:** nginx 1.27-alpine（静态 + 反代）、原生 ES modules（无构建、无 CDN）、Node 24 内置测试运行器、Python 3 + httpx + Pillow（冒烟脚本）

**Spec:** `docs/superpowers/specs/2026-09-24-web-ui-design.md`

---

## ⚠️ 动手前必读

### 本仓库没有 git

用户明确选择不初始化 git。所以**每个任务以「验证检查点」收尾，没有 commit 步骤**。
出错只能靠改回来，改动前先想清楚。任务之间不要并行改同一个文件。

### 前端没有构建步骤，但**有**单元测试

`node --test` 在 Node 24 上可用。**调用形式必须是引号 glob**：

```bash
node --test 'web/tests/*.test.mjs'      # ✅ 从仓库根跑，可用（实测 pass 1 / fail 0）
node --test web/tests                   # ❌ 目录形式在 Node 24 上是坏的（实测 pass 0 / fail 1）
```

测试文件放 `web/tests/`（**不在** `web/site/` 里），因为 nginx 只发 `site/`，
测试代码不该被浏览器取到。

### 服务已在运行，改端口要重建容器

当前 `gateway` 占着宿主机 8090。Task 1 会删掉它的 `ports`。
实测：删掉 `ports` 后 `docker compose up -d` 会输出 `Container ... Recreated` 并真的释放端口，
**不需要** `--force-recreate`。若真撞上 `bind: address already in use`，重跑一次 `up -d`。

> ⚠️ **不要重启 docker daemon** —— 未开 Live Restore，重启会杀掉机器上全部容器（含他人服务）。

### 本轮踩过的坑（别重复）

| 坑 | 事实 |
|---|---|
| 用 `--include='*.py'` 之类白名单做 grep | 会**整份漏掉无扩展名的 `Dockerfile`**。审计一律用 `--exclude-dir` 做减法 |
| 以为 `nginx:alpine` 只有 BusyBox | 实测装了**真的 `curl` 8.12.1**（无 `ss`） |
| 以为 `EXPOSE 8091` 会发布端口 | `EXPOSE` 只是元数据，**不发布任何东西**。删 `docker-compose.yml` 的 `ports` 才生效 |

---

## 文件结构

**新增：**

```
web/
├── Dockerfile                  # nginx:1.27-alpine，刻意无 RUN 步骤（构建不碰网络）
├── nginx.conf                  # 静态托管 + 反代
└── site/                       # ← 只有这些会被浏览器取到
    ├── index.html
    ├── css/app.css
    └── js/
        ├── errors.js           # 错误归一化（纯函数，有测试）
        ├── presets.js          # 尺寸/提示词/负向词数据（纯数据，有测试）
        ├── api.js              # 网关封装（URL 拼接 + fetch，有测试）
        ├── store.js            # localStorage 封装（有测试）
        ├── poll.js             # 轮询器（有测试）
        ├── ui.js               # toast、h()、notesBlock()、humanMs/Bytes（无测试）
        ├── views/generate.js
        ├── views/edit.js
        ├── views/gallery.js
        └── app.js              # hash 路由 + 装配
web/tests/                      # ← 不对外，node --test 专用
├── errors.test.mjs
├── presets.test.mjs
├── api.test.mjs
├── store.test.mjs
└── poll.test.mjs
scripts/smoke_web.py
docs/superpowers/plans/2026-09-24-qwen-image-web-ui.md   # 本文件
```

**修改：**

| 文件 | 改动 |
|---|---|
| `docker-compose.yml` | 删 `gateway.ports`（第 74 行附近）与 `backend.ports`（第 34 行）；新增 `web` 服务 |
| `scripts/smoke.py` | 修 `check_negatives` 里那条**恒真**的穿越断言（Task 2 · 缺陷 A） |
| `gateway/app/tasks.py` | 修异步任务 notes 被丢弃（Task 2 · 缺陷 B）—— `_notes_for` 读错了列 + `_process` 不合并 `result.notes` |
| `Makefile` | 加 `smoke-web` / `test-web` / `logs-web` 目标 |
| `README.md` | 更新架构图与端口说明；记录两处既有缺陷 |

> ⚠️ **规格里「本次唯一改动既有代码的地方，就是 `scripts/smoke.py`」这句话已经不成立。**
> 现在是**两个文件**（`scripts/smoke.py` + `gateway/app/tasks.py`）。
> 规格 §影响面与回滚 与 §未决/已知风险 需要同步更新 —— 见 Task 13 Step 5b。

**设计约束（每个任务都要遵守）：**

1. **所有 API 调用用相对路径。** 响应里的 `images[].url` 和 `status_url` 都是按
   `PUBLIC_BASE_URL` 拼的绝对地址，从局域网访问会指向 localhost 而裂图。
   一律用 `images[].file` + task_id 自己拼。
2. **不做无效控件。** 不做 mask 上传、不暴露 `guidance_scale`。
3. **`x_notes` / `notes` 必须渲染出来。** 网关对我们参数的每次纠正都要可见。
4. **尺寸每条边是 32 的倍数。** `1280x720` 这类**会被后端 400 拒**。

---

## Task 1: 端口交接 + nginx 骨架（最高风险，先做）

把这一步放最前面：它动的是**正在运行的服务的端口**，失败了后面全白搭。
做完这一步，`http://<主机>:8090/` 会返回一个占位页，而网关 API 经反代照旧可用
（README 里所有 `curl localhost:8090/v1/...` 示例不变）。

**Files:**
- Create: `web/Dockerfile`
- Create: `web/nginx.conf`
- Create: `web/site/index.html`（占位，Task 3 会替换）
- Modify: `docker-compose.yml`（删两个 `ports`，加 `web` 服务）

- [ ] **Step 1: 写 nginx 配置**

Create `web/nginx.conf`:

```nginx
# Qwen-Image Web UI —— 静态托管 + 反向代理
#
# ⚠️ 与 gateway/app/config.py 的硬耦合：
#   client_max_body_size 必须 >= max_upload_bytes(16MB) × max_ref_images(10) = 160MB
#   改一边必须改另一边。
worker_processes auto;
events { worker_connections 1024; }

http {
  include       /etc/nginx/mime.types;
  default_type  application/octet-stream;
  sendfile      on;
  tcp_nopush    on;
  server_tokens off;

  access_log /dev/stdout;
  error_log  /dev/stderr warn;

  # 10 张参考图 × 16MB = 160MB 理论峰值，留到 200m。
  # 默认值是 1m —— 不设的话上传一张 2MB 的图就被 413 拒，而这是核心功能。
  client_max_body_size 200m;
  client_body_timeout  300s;

  # 大文件上传直接流式转发，不在 nginx 落盘
  proxy_request_buffering off;
  # 图片响应也直接透传
  proxy_buffering         off;
  proxy_http_version      1.1;
  proxy_set_header Host              $host;
  proxy_set_header X-Real-IP         $remote_addr;
  proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
  proxy_set_header X-Forwarded-Proto $scheme;

  gzip on;
  gzip_types text/css application/javascript application/json image/svg+xml;
  gzip_min_length 1024;

  upstream gateway {
    # 端口不能省 —— 写成裸 http://gateway 会打到 80 端口，每个请求都 502。
    # 网关容器内监听 8090（uvicorn --port 8090 / EXPOSE 8090）。
    #
    # ⚠️ 这个名字只在 **nginx 启动时解析一次**，之后一直用那个 IP。
    # 若重建 gateway 后 Docker 给它分了**不同的** IP，nginx 会继续往旧 IP 打，
    # 所有 /v1 /files /healthz 请求 502 —— 而且因为 /web-healthz 是本地探针，
    # web 容器**仍报 healthy**，不会给出任何信号。
    # 实测：`--force-recreate gateway` 后 IP 未变（172.22.0.3 → 172.22.0.3），
    # compose 通常复用同一地址；但这不是 Docker 的保证。
    # **万一遇到「web 健康但所有 API 502」，先 `docker compose restart web`。**
    server gateway:8090;
  }

  server {
    listen 80;
    server_name _;

    # ⚠️ 这条必须排在 `location /` 前面，且**必须用 `~ \.`（含点即命中），
    # 不要改成列扩展名的白名单**。列白名单的话，任何没列进去的扩展名
    # （.json / .txt / .wasm / .webmanifest …）都会落到下面的 `location /`，
    # 被回退成 index.html + HTTP 200 —— 浏览器把 HTML 当 JS/JSON 解析，报的是
    # 「Expected a JavaScript module script but the server responded with MIME
    # type text/html」，一条完全指不到根因的错误。
    #
    # 含点的路径一律按**真文件**处理，缺失就是真 404；不需要维护扩展名清单。
    # 实测：/nope.js /nope.json /nope.txt /nope.wasm 全部 404；
    #       /js/app.js /css.css /index.html 正常 200。
    location ~ \. {
      root /usr/share/nginx/html;
      try_files $uri =404;
      add_header Cache-Control "no-cache";
    }

    # 无扩展名的路径交给 index.html，这样直接输 /generate 之类的友好地址也能进应用。
    # 注：本应用用的是 **hash 路由**（#/generate），服务端实际只会被请求 "/"，
    # 这条回退主要是为了用户手输地址时不至于撞 404。
    location / {
      root /usr/share/nginx/html;
      index index.html;
      try_files $uri /index.html;
      add_header Cache-Control "no-cache";
    }

    # nginx 自身探针，不依赖 gateway。健康检查用它而不是 /healthz，
    # 否则 gateway 挂了会把 web 也一起标成 unhealthy。
    location = /web-healthz {
      access_log off;
      default_type text/plain;
      return 200 "ok\n";
    }

    # ⚠️ proxy_pass 不带 URI 后缀才能保留 /v1 前缀。
    # 误写成 http://gateway/; 会把 /v1 吃掉，网关全部 404。
    #
    # ⚠️⚠️ `^~` 不能省！nginx 的匹配顺序是：先找最长前缀，**再依次试正则 location**，
    # 正则命中就覆盖前缀。上面那条 `location ~ \.` 会匹配
    # /files/{task_id}/img_000.png —— 于是取图请求被静态规则接走，先在本地找文件，
    # 找不到就 404。**所有图片显示全挂。** 实测确认过（无 `^~` 时该请求返回 404，
    # 有 `^~` 时正确回源到网关）。
    location ^~ /v1/ {
      proxy_pass http://gateway;
      # ⚠️ 必须 **大于** 网关自己的最坏耗时，否则 nginx 会先超时并返回 **HTML 504**，
      # 把网关那个 OpenAI 形状的 JSON 错误彻底盖掉
      # （gateway/app/main.py 的异常处理器专门做这件事，被盖掉就白做了）。
      #
      # 推导（都是实测值）：
      #   等并发名额  _SLOT_WAIT_S = 60s
      #               gateway/app/routes/openai_images.py:27
      #   + 后端生成  REQUEST_TIMEOUT_S = 600s
      #               来自 docker-compose.yml:76 的 ${REQUEST_TIMEOUT_S:-600}
      #               （注意：gateway/app/config.py:24 自己的默认值是 **300**。
      #                600 是 compose 给的。容器内 printenv 实测为 600。）
      #   = 最坏 660s，期间网关**一个字节都不发给 nginx**
      # 所以 600s 是错的（会和网关打平甚至先触发）。取 700s 留出余量。
      # 若日后调大 REQUEST_TIMEOUT_S，这里要同步 +100s。
      proxy_read_timeout 700s;
      proxy_send_timeout 700s;
    }

    # 同样必须有 `^~`：取图的文件名带 .png/.jpg/.webp 扩展名
    location ^~ /files/ {
      proxy_pass http://gateway;
      proxy_read_timeout 60s;
    }

    location = /healthz      { proxy_pass http://gateway; }
    location = /openapi.json { proxy_pass http://gateway; }
    location /docs           { proxy_pass http://gateway; }
    location = /redoc        { proxy_pass http://gateway; }
  }
}
```

- [ ] **Step 2: 写 Dockerfile**

Create `web/Dockerfile`:

```dockerfile
# 静态站点 + 反向代理。
# 刻意**没有任何 RUN 步骤** —— 构建过程不需要网络。本机 docker daemon 不走代理、
# 拉基础镜像不稳（见 README「排障」），不联网就不会踩坑。
FROM nginx:1.27-alpine

# 覆盖默认配置（默认配置里没有反代，也没有 200m 的上传上限）
COPY nginx.conf /etc/nginx/nginx.conf

# 站点文件。注意只 COPY site/ —— web/tests/ 不对外。
COPY site/ /usr/share/nginx/html/

EXPOSE 80

# 探 nginx 自己，不探 gateway
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
  CMD wget -q -O- http://127.0.0.1/web-healthz >/dev/null 2>&1 || exit 1
```

- [ ] **Step 3: 写占位页（Task 3 会替换）**

Create `web/site/index.html`:

```html
<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Qwen-Image 工作台</title>
</head>
<body>
  <main id="app">
    <h1>Qwen-Image 工作台</h1>
    <p>nginx 反代已就绪。前端尚未部署（Task 3）。</p>
  </main>
</body>
</html>
```

- [ ] **Step 4: 改 docker-compose.yml —— 删两条 ports**

删掉 `backend` 服务里的这两行（约第 32-34 行）：

```yaml
    ports:
      - "127.0.0.1:8091:8091"     # 不对外，只给 gateway
```

删掉 `gateway` 服务里的这两行（约第 73-74 行）：

```yaml
    ports:
      - "0.0.0.0:8090:8090"
```

> 两个服务都删。后端那个虽然只绑回环、不算「对外」，但一起删掉后
> 「宿主机只有 8090」才成为可断言的事实（Task 12 的检查 10 就在断它）。
> `backend/Dockerfile.diffusers` 里的 `EXPOSE 8091` **保留** —— 它只是元数据，
> 不发布任何端口。

- [ ] **Step 5: 在 docker-compose.yml 末尾加 web 服务**

追加（与 `gateway` 同级，注意 YAML 缩进）：

```yaml
  # ------------------------------------------------------------------
  # Web 前端：静态单页 + 反向代理。全栈唯一对外端口。
  # ------------------------------------------------------------------
  web:
    build:
      context: ./web
    image: qwen-image-web:local
    ports:
      - "0.0.0.0:8090:80"
    depends_on:
      gateway:
        # 同 gateway 对 backend 的策略：不等健康检查，先起来。
        # 网关没就绪时页面能加载，只是 API 报错，比整个栈卡住更有用。
        condition: service_started
    restart: unless-stopped
```

- [ ] **Step 6: 构建并启动**

```bash
cd /root/data1/kdi/workspace/qwen-image
docker compose up -d web
```

Expected: 输出里包含 `Container qwen-image-web-1  Built` / `Started`，
以及 `Container qwen-image-gateway-1  Recreated`（因为它的 `ports` 变了，
`web` 的 `depends_on: gateway` 会把它一并处理）。

> **为什么是 `up -d web`（不带 `--build`）**：`web` 镜像此刻还不存在，
> `up` 本来就会构建它。而带了 `--build` 反而会**构建整条依赖闭包** ——
> 实测 `up -d --build web` 在 backend→gateway→web 的链上会打印
> `backend Built` / `gateway Built` / `web Built`，也就是会去碰那个 18.4GB 的
> CUDA 基座（要联网走代理，很慢），而本次根本没改后端。
> 不带 `--build` 则只构建缺失的 `web` 镜像，backend/gateway 原样复用。
>
> **注意 Task 3/9/10/11/12 用的是 `--build web`** —— 那时 `web` 镜像已存在、
> 只有 `site/` 变了，`--build` 才是必需的。两处语境不同，别照抄。
>
> **万一 gateway 没被重建**（`ss -ltn` 里 8090 仍属于它，见 Step 7）：
> 补跑一次 `docker compose up -d`（不带服务名）即可。Step 7 的端口检查就是用来
> 发现这种情况的 —— **别跳过 Step 7**。

- [ ] **Step 7: ⭐ 验证端口交接（本任务的核心）**

```bash
echo "--- 宿主机 listener：应存在 8090、不存在 8091 ---"
ss -ltn | grep -E ':8090|:8091'
```
Expected: 只有一行 `0.0.0.0:8090`，**没有 8091**。

```bash
echo "--- 三个容器都在跑 ---"
docker compose ps
```
Expected: `backend` / `gateway` / `web` 三个 service 都是 `Up`，
且 **backend 和 gateway 的 PORTS 列是空的**，web 是 `0.0.0.0:8090->80/tcp`。

```bash
echo "--- nginx 自身探针 ---"
curl -s -o /dev/null -w '%{http_code} %{content_type}\n' http://localhost:8090/web-healthz
```
Expected: `200 text/plain`

- [ ] **Step 8: ⭐ 验证反代没有吃掉 /v1 前缀**

```bash
echo "--- /healthz 经反代 → 应是 gateway 的 JSON ---"
curl -s http://localhost:8090/healthz
```
Expected: `{"status":"ok","backend":"ok","backend_url":"http://backend:8091",...}`
若返回 HTML 或 404 → `proxy_pass` 写错了（多了 URI 后缀），回 Step 1 检查。

```bash
echo "--- 静态页 ---"
curl -s -o /dev/null -w '%{http_code} %{content_type}\n' http://localhost:8090/
```
Expected: `200 text/html`

```bash
echo "--- 未知 API 路由 → 应是 gateway 的 JSON 而非 nginx 的 HTML 404 ---"
curl -s -i http://localhost:8090/v1/definitely-not-a-route | head -6
```
Expected: `404`，`content-type: application/json`，body `{"detail":"Not Found"}`。
若是 nginx 的 HTML → 反代规则没生效。

```bash
echo "--- 缺失的静态资源必须是真 404，不能回退成 index.html ---"
for p in /js/definitely-missing.js /css/nope.css /nope.png; do
  printf '  %-28s -> ' "$p"
  curl -s -o /dev/null -w '%{http_code} %{content_type}\n' "http://localhost:8090$p"
done
```
Expected: 三行**全是 `404`**。
若出现 `200 text/html` → 反代/静态规则把缺失资源回退成了 index.html，
浏览器会把 HTML 当 JS 解析并报出看不懂的 MIME 错误。回 Step 1 检查
`location ~* \.(js|css|...)$` 那条规则是否漏了。

- [ ] **Step 9: 验证既有冒烟（回归）**

```bash
python3 scripts/smoke.py 2>&1 | tail -20
```
Expected: **8 / 9 通过，`negatives` FAIL** —— 具体是
`! 路径穿越应被拒，实际 200`。其余 8 项全绿。

> **这里与计划初稿的预期相反，实测更正：**初稿说 `negatives` 会「假通过」。
> 实际是**变成可见的 FAIL**。原因值得理解，因为它说明了改动前后的差别：
>
> - `scripts/smoke.py:355` 发的是 `client.get("/files/../../etc/passwd")`，
>   而 **httpx 在客户端就把点段规范化掉了**，线上实际发出的是 `GET /etc/passwd`。
> - **改动前**：`/etc/passwd` 打到网关，是个未知路由 → FastAPI 404 →
>   断言 `status_code in (400, 404)` 因此通过。这就是「假通过」。
> - **改动后**：`/etc/passwd` 打到 nginx。若静态回退规则写错成
>   `try_files ... /index.html`，它会返回 index.html + **200**，断言就失败了。
>
> 换句话说这是个**好信号**：恒真的断言变成了会红的断言。Task 2 会把它修对
> （换成 3 段探针 `/files/short/n.png` + 断言 `== 400`），修完这里会重新全绿。
>
> **不是安全回归**：用 `curl --path-as-is` 直接发未规范化的
> `/files/../../etc/passwd`，以及 `%2e%2e`、`..%2f` 等编码变体，nginx 一律返回
> **400**，`/etc/passwd` 的内容一个字节都没泄露。那个 200 只是 339 字节的占位页。

- [ ] **Step 10: 检查点**

无 git，所以这里没有 commit。确认以下全部成立再进 Task 2：

- [ ] `ss -ltn` 只有 8090，无 8091
- [ ] `docker compose port gateway 8090` → `:0`；`docker compose port backend 8091` → `:0`
      （`docker compose ps` 的 PORTS 列会显示裸的 `8090/tcp`，那是 EXPOSE 元数据渲染，
      **不代表发布** —— 用 `docker compose port` 或 `ss` 判断才准）
- [ ] `curl localhost:8090/` → 200 text/html
- [ ] `curl localhost:8090/healthz` → gateway 的 JSON
- [ ] `curl localhost:8090/v1/definitely-not-a-route` → JSON 404，非 HTML
- [ ] 缺失的静态资源（如 `/js/nope.js`）→ **404**，不是 200 text/html
- [ ] `python3 scripts/smoke.py` → 8/9，唯一失败项是 `negatives`（Task 2 修）

---

## Task 2: 修两处既有缺陷（都是本设计暴露出来的，不是它引入的）

本任务改两个文件，两处都是**既有缺陷**：

| 缺陷 | 位置 | 为什么必须修 |
|---|---|---|
| A. 穿越断言恒真 | `scripts/smoke.py:check_negatives` | Task 12 要靠冒烟脚本验证代理路径，不能留一条永远通过的断言充数 |
| B. 异步任务的 notes 永远是空的 | `gateway/app/tasks.py` | 前端**只走异步路径**，不修的话「服务端对参数的说明」永远显示不出来 —— 包括多参考图的 512 降级说明、以及 jpg+透明背景被纠正为 png 这类只有服务端知道的纠正 |

> **这推翻了规格里「本次唯一改动既有代码的地方，就是 `scripts/smoke.py`」的说法。**
> 规格 §影响面与回滚 需要同步更新（见 Task 13 Step 5b）。

### ⚠️ 本任务改完代码必须 `up -d --build gateway`，**不能** `restart`

`gateway` 的**源码是 COPY 进镜像的**，容器里没有任何源码挂载（实测）：

```bash
$ docker inspect qwen-image-gateway-1 --format '{{range .Mounts}}{{.Source}} -> {{.Destination}}{{println}}{{end}}'
/root/data1/kdi/workspace/qwen-image/data -> /app/data      # 只有 data，没有源码
$ docker inspect qwen-image-gateway-1 --format '{{json .Config.Cmd}}'
["uvicorn","app.main:app","--host","0.0.0.0","--port","8090"]   # 没有 --reload
```

`gateway/Dockerfile:21` 就是 `COPY app ./app`。

所以 **`docker compose restart gateway` 只会拿同一份旧镜像重启**，你改的 `tasks.py`
根本不会生效。后面的验证步骤（Step 9 / Step 10）会因此失败，让人去追一个
**根本不存在的 bug** —— 代码是对的，是运行的不是那份代码。必须重建：

```bash
docker compose up -d --build gateway
```

这次重建很快且**不联网**：`python:3.12-slim` 本地已有，`pip install` 那层命中缓存，
只有 `COPY app ./app` 这一层发生变化。

> 与 Task 1 的区别：那里 `web` 镜像还不存在，`up` 本来就会构建它；
> 这里镜像已存在但源码变了，`--build` 是**必须的**。两处不要照抄。

### 缺陷 B 的证据（已实测）

```bash
python3 - <<'EOF'
import sqlite3
conn = sqlite3.connect("data/tasks.db")
for tid, kind, nj in conn.execute(
        "SELECT id, kind, notes_json FROM tasks WHERE status='succeeded' ORDER BY created_at DESC LIMIT 8"):
    print(f"{tid[:8]}  {kind:<11} notes_json={nj!r}")
EOF
```

Expected（修复前的现状）：

```
d1677bdb  edit        notes_json='["检测到 2 张参考图，output_resolution 已自动设为 512。…"]'  ← 同步请求，有 notes
94f4ee5f  edit        notes_json='[]'   ← 异步，notes 被丢掉
fc944cf2  generation  notes_json='[]'
...
```

根因两条，缺一不可：

1. `TaskRunner._notes_for()` 读的是 `notes_json` 列，但提交时 notes 被存进了
   `request_json.notes`（`store.create()` 的 INSERT 只写
   `(id, kind, status, request_json, created_at)`）—— **读错了列**。
2. `TaskRunner._process()` 只取 `_notes_for()` 的结果，**从不合并 `result.notes`** ——
   后端上报的说明（512 降级）因此丢弃。

同步路径是对的（`gateway/app/routes/openai_images.py` 里 `all_notes = notes + result.notes`），
所以 `scripts/smoke.py` 的 `check_multi_ref` 走同步接口、能看到 notes，
这个 bug 在既有测试里被**掩盖**了。

**Files:**
- Modify: `scripts/smoke.py`（`check_negatives` 内，约第 300 行）

- [ ] **Step 1: 先证明现断言确实是假的**

```bash
cd /root/data1/kdi/workspace/qwen-image
echo "--- 现有探针(5 段, --path-as-is 直连网关, 完全不过代理) ---"
curl -s --path-as-is 'http://127.0.0.1:8090/files/../../etc/passwd'; echo
echo "--- httpx 在客户端把它改写成什么 ---"
python3 -c "
import httpx
print(httpx.Request('GET','http://localhost:8090/files/../../etc/passwd').url)"
```
Expected: 前者返回 `{"detail":"Not Found"}`（route-miss，因为 5 段路径匹配不上 3 段路由）；
后者打印 `http://localhost:8090/etc/passwd` —— `files/` 前缀**根本没离开客户端**。
两个理由都说明这条断言与网关的防护无关。

- [ ] **Step 2: 确认新探针能真的打到防护**

```bash
echo "--- 3 段 + 非法 task_id（'short' 只有 5 字符，不满足 _SAFE_ID{8,64}）---"
curl -s --path-as-is 'http://127.0.0.1:8090/files/short/n.png'; echo
```
Expected: `{"error":{"message":"非法的路径参数","type":"invalid_request_error","code":400}}`

- [ ] **Step 3: 替换断言**

在 `scripts/smoke.py` 的 `check_negatives` 里找到：

```python
    r = client.get("/files/../../etc/passwd")
    c.ok(r.status_code in (400, 404), f"路径穿越应被拒，实际 {r.status_code}")
```

替换为：

```python
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
```

- [ ] **Step 4: 验证修好了**

```bash
python3 scripts/smoke.py 2>&1 | grep -A6 '^\[.*\] negatives'
```
Expected: `[PASS] negatives  (N 项断言)`，且断言数比修之前**多 1**（原来 1 条，现在 2 条）。

- [ ] **Step 5: 确认它现在真的会失败（证明不是换了个恒真的写法）**

```bash
python3 - <<'EOF'
import re, pathlib
p = pathlib.Path("gateway/app/routes/ops.py")
src = p.read_text()
# 临时放开 task_id 校验，模拟"防护失效"
patched = src.replace("if not _SAFE_ID.match(task_id) or not _SAFE_NAME.match(name):",
                      "if False:")
p.write_text(patched)
print("已临时禁用防护校验")
EOF
docker compose up -d --build gateway >/dev/null && sleep 4
python3 scripts/smoke.py 2>&1 | grep -A4 '^\[.*\] negatives'
```
Expected: `[FAIL] negatives` —— 证明断言有效。

**必须立刻改回来：**

```bash
python3 - <<'EOF'
import pathlib
p = pathlib.Path("gateway/app/routes/ops.py")
src = p.read_text()
p.write_text(src.replace("if False:",
                         "if not _SAFE_ID.match(task_id) or not _SAFE_NAME.match(name):"))
print("已还原")
EOF
docker compose up -d --build gateway >/dev/null && sleep 4
python3 scripts/smoke.py 2>&1 | grep -A4 '^\[.*\] negatives'
```
Expected: 回到 `[PASS]`。

> 这一步是本次唯一会临时改动运行中服务的操作，两步之间不要插入别的动作。
> `gateway/app/routes/ops.py` 必须与改动前**逐字节一致** —— 还原后可用
> `grep -n 'if False' gateway/app/routes/ops.py` 确认无残留。

---

### 缺陷 B：异步任务的 notes 永远是空的

**Files:**
- Modify: `gateway/app/tasks.py`（`TaskRunner._notes_for` 与 `_process`）

- [ ] **Step 6: 记录当前行为作为对照**

```bash
cd /root/data1/kdi/workspace/qwen-image
python3 - <<'EOF'
import httpx, io
from PIL import Image, ImageDraw

def ref(color):
    im = Image.new("RGB", (512, 512), "white")
    ImageDraw.Draw(im).ellipse((102, 102, 409, 409), fill=color)
    b = io.BytesIO(); im.save(b, format="PNG"); return b.getvalue()

# 异步多参考图编辑 —— 前端走的就是这条路
r = httpx.post("http://localhost:8090/v1/tasks",
               data={"prompt": "combine them", "num_inference_steps": 20},
               files=[("image", ("a.png", ref("red"), "image/png")),
                      ("image", ("b.png", ref("blue"), "image/png"))], timeout=120)
tid = r.json()["task_id"]
import time
for _ in range(60):
    s = httpx.get(f"http://localhost:8090/v1/tasks/{tid}").json()
    if s["status"] in ("succeeded", "failed"): break
    time.sleep(2)
print("status:", s["status"])
print("notes :", s.get("notes"))
print("尺寸  :", s["images"][0]["width"] if s.get("images") else None)
EOF
```
Expected（**修复前**）：

```
status: succeeded
notes : []          ← 空的！后端的 512 降级说明被丢了
尺寸  : 512
```

尺寸确实是 512（后端降级生效了），但 `notes` 是空 —— 这就是缺陷。

- [ ] **Step 7: 修 `_notes_for` 读错的列**

在 `gateway/app/tasks.py` 找到：

```python
    async def _notes_for(self, job: Job) -> list[str]:
        row = await self.store.get(job.task_id)
        if not row:
            return []
        import json

        try:
            return json.loads(row.get("notes_json") or "[]")
        except json.JSONDecodeError:
            return []
```

替换为：

```python
    async def _notes_for(self, job: Job) -> list[str]:
        """取提交时存下的 notes。

        注意存储位置：`store.create()` 的 INSERT 只写
        (id, kind, status, request_json, created_at)，**不写 notes_json** ——
        提交时的 notes 在 request_json.notes 里。这里以前读的是 notes_json，
        于是永远返回空列表，网关自己的纠正说明（如 jpeg+透明被纠正为 png）
        在异步路径上被静默丢弃。
        """
        row = await self.store.get(job.task_id)
        if not row:
            return []
        try:
            request = json.loads(row.get("request_json") or "{}")
        except json.JSONDecodeError:
            return []
        notes = request.get("notes")
        return [str(n) for n in notes] if isinstance(notes, list) else []
```

把 `import json` 移到文件顶部的 import 区（`from __future__ import annotations` 之后），
与其他 import 放在一起。

- [ ] **Step 8: 在 `_process` 里合并后端上报的 notes**

在 `gateway/app/tasks.py` 的 `TaskRunner._process` 里找到：

```python
        notes = await self._notes_for(job)
        await self.store.set_succeeded(job.task_id, images, notes, result.timings)
```

替换为：

```python
        # 网关自己的纠正说明 + 后端上报的说明，合并后一起存。
        # 只取前者会把「多参考图自动降到 512」这类后端才知道的说明丢掉。
        # 同步路径一直是这么做的（见 routes/openai_images.py 的 all_notes），
        # 异步路径以前漏了。
        notes = await self._notes_for(job) + [str(n) for n in result.notes]
        await self.store.set_succeeded(job.task_id, images, notes, result.timings)
```

- [ ] **Step 9: 重启网关并验证修好了**

```bash
cd /root/data1/kdi/workspace/qwen-image
docker compose up -d --build gateway && sleep 4
python3 - <<'EOF'
import httpx, io, time
from PIL import Image, ImageDraw

def ref(color):
    im = Image.new("RGB", (512, 512), "white")
    ImageDraw.Draw(im).ellipse((102, 102, 409, 409), fill=color)
    b = io.BytesIO(); im.save(b, format="PNG"); return b.getvalue()

r = httpx.post("http://localhost:8090/v1/tasks",
               data={"prompt": "combine them", "num_inference_steps": 20},
               files=[("image", ("a.png", ref("red"), "image/png")),
                      ("image", ("b.png", ref("blue"), "image/png"))], timeout=120)
tid = r.json()["task_id"]
for _ in range(60):
    s = httpx.get(f"http://localhost:8090/v1/tasks/{tid}").json()
    if s["status"] in ("succeeded", "failed"): break
    time.sleep(2)
print("status:", s["status"])
print("notes :", s.get("notes"))
assert s["status"] == "succeeded", s
assert s.get("notes"), "notes 仍然为空 —— 修复没生效"
assert any("512" in n for n in s["notes"]), f"notes 里没有 512 降级说明: {s['notes']}"
print("✅ 异步任务的 notes 已带上后端的 512 降级说明")
EOF
```
Expected: 末尾打印 `✅ 异步任务的 notes 已带上后端的 512 降级说明`

- [ ] **Step 10: 验证网关自己的纠正说明也能传出来**

jpeg + 透明背景会被网关纠正为 png 并记一条 note。验证它在**异步**路径上也可见：

```bash
python3 - <<'EOF'
import httpx, time
r = httpx.post("http://localhost:8090/v1/tasks", json={
    "prompt": "a simple red circle sticker",
    "size": "512x512", "num_inference_steps": 20,
    "background": "transparent", "output_format": "jpeg",   # jpeg 无 alpha → 网关应纠正为 png
})
tid = r.json()["task_id"]
for _ in range(60):
    s = httpx.get(f"http://localhost:8090/v1/tasks/{tid}").json()
    if s["status"] in ("succeeded", "failed"): break
    time.sleep(2)
print("status:", s["status"], "| notes:", s.get("notes"))
assert s.get("notes"), "网关的格式纠正说明没传出来"
assert any("png" in n for n in s["notes"]), f"notes 里没有格式纠正说明: {s['notes']}"
print("✅ 网关自己的纠正说明在异步路径上可见")
EOF
```
Expected: 末尾打印 `✅ 网关自己的纠正说明在异步路径上可见`

- [ ] **Step 11: 回归 —— 既有冒烟必须仍然全绿**

```bash
python3 scripts/smoke.py 2>&1 | tail -8
```
Expected: 全部通过。特别注意 `async` 一项 —— 它是用异步接口跑的，
改动后应当仍 PASS（`check_async` 只在 notes 非空时记录，不强制断言）。

- [ ] **Step 12: 检查点**

- [ ] 缺陷 A 修好：`negatives` PASS 且断言数为 2
- [ ] 缺陷 A 的临时改动已还原（`grep -c 'if False' gateway/app/routes/ops.py` → `0`）
- [ ] 缺陷 B 修好：异步多参考图任务的 `notes` 非空且含「512」
- [ ] 网关自己的格式纠正说明在异步路径上可见
- [ ] `python3 scripts/smoke.py` 全绿

---

## Task 3: 前端骨架 —— index.html / app.css / app.js 三 tab 路由

**Files:**
- Create: `web/site/index.html`（替换 Task 1 的占位页）
- Create: `web/site/css/app.css`
- Create: `web/site/js/app.js`

- [ ] **Step 1: 写 index.html**

Create `web/site/index.html`:

```html
<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Qwen-Image 工作台</title>
  <link rel="stylesheet" href="/css/app.css">
</head>
<body>
  <header class="topbar">
    <h1 class="brand">Qwen-Image<span>工作台</span></h1>
    <nav class="tabs">
      <a href="#/generate" data-tab="generate">生成</a>
      <a href="#/edit" data-tab="edit">编辑</a>
      <a href="#/gallery" data-tab="gallery">画廊</a>
    </nav>
    <div class="health" id="health" title="服务状态">
      <span class="dot" data-state="unknown"></span>
      <span class="health-text">连接中…</span>
    </div>
  </header>

  <main id="app"></main>

  <div class="toasts" id="toasts" aria-live="polite"></div>

  <footer class="notice">
    Qwen-Image-2.1 采用 Qwen Research License Agreement（仅限研究/评估，禁止商用）。
    本服务定位内网研发用途。
  </footer>

  <script type="module" src="/js/app.js"></script>
</body>
</html>
```

- [ ] **Step 2: 写 app.css**

Create `web/site/css/app.css`:

```css
:root {
  --bg: #0f1115;
  --panel: #171a21;
  --panel-2: #1e222b;
  --line: #2a2f3a;
  --fg: #e6e9ef;
  --fg-dim: #99a1b3;
  --accent: #6ea8fe;
  --ok: #4ade80;
  --warn: #fbbf24;
  --err: #f87171;
  --radius: 10px;
}

* { box-sizing: border-box; }

body {
  margin: 0;
  background: var(--bg);
  color: var(--fg);
  font: 14px/1.6 system-ui, -apple-system, "Segoe UI", "PingFang SC",
        "Hiragino Sans GB", "Microsoft YaHei", sans-serif;
}

/* ---------- 顶栏 ---------- */
.topbar {
  position: sticky; top: 0; z-index: 10;
  display: flex; align-items: center; gap: 24px;
  padding: 12px 20px;
  background: var(--panel);
  border-bottom: 1px solid var(--line);
}
.brand { margin: 0; font-size: 16px; font-weight: 600; white-space: nowrap; }
.brand span { color: var(--fg-dim); font-weight: 400; margin-left: 6px; }

.tabs { display: flex; gap: 4px; flex: 1; }
.tabs a {
  padding: 6px 14px; border-radius: var(--radius);
  color: var(--fg-dim); text-decoration: none;
}
.tabs a:hover { background: var(--panel-2); color: var(--fg); }
.tabs a.active { background: var(--panel-2); color: var(--accent); }

.health { display: flex; align-items: center; gap: 6px; color: var(--fg-dim); font-size: 12px; }
.dot { width: 8px; height: 8px; border-radius: 50%; background: var(--fg-dim); }
.dot[data-state="ok"]    { background: var(--ok); }
.dot[data-state="warn"]  { background: var(--warn); }
.dot[data-state="error"] { background: var(--err); }

/* ---------- 布局 ---------- */
#app { padding: 20px; max-width: 1400px; margin: 0 auto; }

.cols { display: grid; grid-template-columns: minmax(320px, 420px) 1fr; gap: 20px; align-items: start; }
@media (max-width: 900px) { .cols { grid-template-columns: 1fr; } }

.panel {
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: var(--radius);
  padding: 16px;
}
.panel h2 { margin: 0 0 12px; font-size: 14px; color: var(--fg-dim); font-weight: 600; }

/* ---------- 表单 ---------- */
label.field { display: block; margin-bottom: 12px; }
label.field > span { display: block; margin-bottom: 4px; color: var(--fg-dim); font-size: 12px; }

input[type="text"], input[type="number"], textarea, select {
  width: 100%; padding: 8px 10px;
  background: var(--panel-2); color: var(--fg);
  border: 1px solid var(--line); border-radius: 8px;
  font: inherit;
}
textarea { resize: vertical; min-height: 90px; }
input:focus, textarea:focus, select:focus { outline: none; border-color: var(--accent); }

button {
  padding: 8px 16px; border-radius: 8px; border: 1px solid var(--line);
  background: var(--panel-2); color: var(--fg); font: inherit; cursor: pointer;
}
button:hover:not(:disabled) { border-color: var(--accent); }
button:disabled { opacity: .5; cursor: not-allowed; }
button.primary { background: var(--accent); border-color: var(--accent); color: #0b1020; font-weight: 600; }
button.danger  { color: var(--err); }

.row { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }

/* ---------- 提示 ---------- */
.hint { color: var(--fg-dim); font-size: 12px; margin: 4px 0 0; }
.note {
  margin: 8px 0; padding: 8px 10px; border-radius: 8px; font-size: 12px;
  background: rgba(110, 168, 254, .08); border-left: 3px solid var(--accent);
}
.note.warn { background: rgba(251, 191, 36, .08); border-left-color: var(--warn); }
.note.err  { background: rgba(248, 113, 113, .08); border-left-color: var(--err); }
.note ul { margin: 4px 0 0; padding-left: 18px; }

/* ---------- 结果区 ---------- */
.results { display: grid; grid-template-columns: repeat(auto-fill, minmax(240px, 1fr)); gap: 12px; }
.card { background: var(--panel-2); border: 1px solid var(--line); border-radius: var(--radius); overflow: hidden; }
.card img { display: block; width: 100%; height: auto; background: #0a0c10; }
.card .meta { padding: 8px 10px; font-size: 11px; color: var(--fg-dim); }
.card .meta a { color: var(--accent); }
.card.expired { display: flex; align-items: center; justify-content: center;
                min-height: 160px; color: var(--fg-dim); font-size: 12px; padding: 16px; text-align: center; }

/* ---------- 进度 ---------- */
.progress { display: flex; align-items: center; gap: 10px; margin: 12px 0; }
.spinner {
  width: 16px; height: 16px; border: 2px solid var(--line);
  border-top-color: var(--accent); border-radius: 50%;
  animation: spin .8s linear infinite;
}
@keyframes spin { to { transform: rotate(360deg); } }
.status-line { font-size: 13px; }
.status-line b { color: var(--accent); }

/* ---------- 上传 ---------- */
.drop {
  border: 1.5px dashed var(--line); border-radius: var(--radius);
  padding: 20px; text-align: center; color: var(--fg-dim); cursor: pointer;
}
.drop.over { border-color: var(--accent); color: var(--fg); background: rgba(110,168,254,.06); }
.thumbs { display: flex; gap: 8px; flex-wrap: wrap; margin-top: 10px; }
.thumb { position: relative; width: 72px; height: 72px; border-radius: 8px; overflow: hidden; border: 1px solid var(--line); }
.thumb img { width: 100%; height: 100%; object-fit: cover; }
.thumb button {
  position: absolute; top: 2px; right: 2px; padding: 0 5px; font-size: 12px;
  line-height: 16px; background: rgba(0,0,0,.65); border-color: transparent;
}

/* ---------- 画廊 ---------- */
.task-list { display: flex; flex-direction: column; gap: 10px; }
.task { background: var(--panel-2); border: 1px solid var(--line); border-radius: var(--radius); padding: 12px; }
.task-head { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; font-size: 12px; color: var(--fg-dim); }
.badge { padding: 1px 8px; border-radius: 999px; font-size: 11px; border: 1px solid var(--line); }
.badge.succeeded { color: var(--ok);   border-color: rgba(74,222,128,.4); }
.badge.failed    { color: var(--err);  border-color: rgba(248,113,113,.4); }
.badge.running   { color: var(--accent); border-color: rgba(110,168,254,.4); }
.badge.queued, .badge.canceled { color: var(--fg-dim); }

/* ---------- toast ---------- */
.toasts { position: fixed; right: 20px; bottom: 20px; display: flex; flex-direction: column; gap: 8px; z-index: 50; max-width: 420px; }
.toast {
  padding: 10px 14px; border-radius: 8px; background: var(--panel);
  border: 1px solid var(--line); border-left: 3px solid var(--accent);
  font-size: 13px; box-shadow: 0 6px 24px rgba(0,0,0,.4);
}
.toast.error { border-left-color: var(--err); }
.toast.warn  { border-left-color: var(--warn); }
.toast.ok    { border-left-color: var(--ok); }

.notice { padding: 16px 20px; color: var(--fg-dim); font-size: 12px; text-align: center; }
```

- [ ] **Step 3: 写 app.js 骨架**

Create `web/site/js/app.js`:

```js
// 应用装配：hash 路由、健康指示、toast。
// 各 view 模块导出 render(root, ctx)；ctx 由这里注入，方便日后替换。
import { createApi } from './api.js';
import { createStore } from './store.js';
import { createToasts } from './ui.js';
import { renderGenerate } from './views/generate.js';
import { renderEdit } from './views/edit.js';
import { renderGallery } from './views/gallery.js';

const api = createApi();
const store = createStore(window.localStorage);
const toasts = createToasts(document.getElementById('toasts'));

const ROUTES = {
  generate: renderGenerate,
  edit: renderEdit,
  gallery: renderGallery,
};

const ctx = { api, store, toasts };

function currentTab() {
  const m = /^#\/(\w+)/.exec(location.hash);
  return m && m[1] in ROUTES ? m[1] : 'generate';
}

let cleanup = null;

function render() {
  const tab = currentTab();

  // 切走时让上一个 view 有机会停掉轮询
  if (typeof cleanup === 'function') { cleanup(); cleanup = null; }

  for (const a of document.querySelectorAll('.tabs a')) {
    a.classList.toggle('active', a.dataset.tab === tab);
  }

  const root = document.getElementById('app');
  root.replaceChildren();
  try {
    cleanup = ROUTES[tab](root, ctx) ?? null;
  } catch (err) {
    console.error(err);
    root.textContent = `页面渲染失败：${err.message}`;
  }
}

async function refreshHealth() {
  const dot = document.querySelector('#health .dot');
  const text = document.querySelector('#health .health-text');
  try {
    const h = await api.health();
    const ok = h.status === 'ok';
    dot.dataset.state = ok ? 'ok' : 'warn';
    const queued = h.queue_pending ?? 0;
    text.textContent = ok
      ? `就绪${queued ? ` · 排队 ${queued}` : ''}`
      : '后端未就绪';
    if (h.queue_capacity) text.title = `队列 ${queued}/${h.queue_capacity}，并发上限 ${h.max_concurrency}`;
  } catch {
    dot.dataset.state = 'error';
    text.textContent = '服务不可达';
  }
}

window.addEventListener('hashchange', render);
render();
refreshHealth();
setInterval(refreshHealth, 15000);
```

- [ ] **Step 4: 写 ui.js（toast + 小组件，无单元测试）**

Create `web/site/js/ui.js`:

```js
// 与业务无关的 DOM 小工具。无构建，直接用 DOM API。

export function createToasts(container) {
  return {
    show(message, kind = 'info', ttlMs = 6000) {
      const el = document.createElement('div');
      el.className = `toast ${kind}`;
      el.textContent = message;
      container.append(el);
      setTimeout(() => el.remove(), ttlMs);
      return el;
    },
    ok(m) { return this.show(m, 'ok'); },
    warn(m) { return this.show(m, 'warn', 9000); },
    error(m) { return this.show(m, 'error', 12000); },
  };
}

/** 创建元素。attr 里 `text` 设文本，`html` 设 innerHTML（仅用于我们自己的模板字符串）。 */
export function h(tag, attrs = {}, children = []) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v == null || v === false) continue;
    if (k === 'text') el.textContent = v;
    else if (k === 'html') el.innerHTML = v;
    else if (k === 'class') el.className = v;
    else if (k === 'dataset') Object.assign(el.dataset, v);
    else if (k.startsWith('on')) el.addEventListener(k.slice(2).toLowerCase(), v);
    else el.setAttribute(k, v);
  }
  for (const c of [].concat(children)) {
    if (c == null || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

/** 渲染 notes 列表。网关/后端对我们参数的纠正必须可见 —— 不吞。 */
export function notesBlock(notes) {
  if (!notes || !notes.length) return null;
  const ul = h('ul');
  for (const n of notes) ul.append(h('li', { text: n }));
  return h('div', { class: 'note warn' }, [h('b', { text: '服务端对参数的说明：' }), ul]);
}

export function errorBlock(err) {
  if (!err) return null;
  const cls = err.kind === 'noop' ? 'note' : 'note err';
  return h('div', { class: cls, text: err.message });
}

/** 1560000 ms -> "26 分钟" */
export function humanMs(ms) {
  if (!ms || ms < 1000) return `${Math.round(ms || 0)} 毫秒`;
  if (ms < 60000) return `${(ms / 1000).toFixed(1)} 秒`;
  return `${(ms / 60000).toFixed(1)} 分钟`;
}

export function humanBytes(n) {
  if (n == null) return '';
  if (n < 1024) return `${n} B`;
  if (n < 1048576) return `${(n / 1024).toFixed(0)} KB`;
  return `${(n / 1048576).toFixed(1)} MB`;
}
```

- [ ] **Step 5: 加 `health()` 到 api.js 的临时占位**

Task 6 会写完整的 `api.js`。为了让本步骤可验证，先建一个最小版本
（Create `web/site/js/api.js`），Task 6 会用完整实现覆盖它：

```js
import { ClientError, toClientError } from './errors.js';

export function createApi({ fetchImpl = globalThis.fetch, base = '' } = {}) {
  async function getJson(path) {
    let resp;
    try { resp = await fetchImpl(base + path); }
    catch (e) { throw new ClientError(`网络错误：${e.message}`, { kind: 'network' }); }
    if (!resp.ok) throw await toClientError(resp);
    return resp.json();
  }
  return { health: () => getJson('/healthz') };
}
```

同理 Create `web/site/js/errors.js`（Task 4 会用完整实现覆盖）：

```js
export class ClientError extends Error {
  constructor(message, { status = 0, kind = 'server' } = {}) {
    super(message);
    this.name = 'ClientError';
    this.status = status;
    this.kind = kind;
  }
}
export async function toClientError(resp) {
  return new ClientError(`HTTP ${resp.status}`, { status: resp.status });
}
```

Create `web/site/js/store.js` 最小版（Task 7 覆盖）：

```js
export function createStore(storage) {
  return {
    myTasks: () => { try { return JSON.parse(storage.getItem('qwi:mine') || '[]'); } catch { return []; } },
  };
}
```

Create 三个 view 的最小版（Task 9-11 覆盖），例如 `web/site/js/views/generate.js`：

```js
export function renderGenerate(root) {
  root.textContent = '生成页（Task 9 实现）';
}
```

`edit.js` / `gallery.js` 同构，只是文案不同。

- [ ] **Step 6: 重建并验证**

```bash
cd /root/data1/kdi/workspace/qwen-image
docker compose build web && docker compose up -d web
sleep 2
echo "--- 首页 ---"
curl -s -o /dev/null -w '%{http_code} %{content_type}\n' http://localhost:8090/
echo "--- 静态资源 MIME ---"
for f in /css/app.css /js/app.js /js/ui.js /js/api.js /js/errors.js; do
  printf '  %-18s ' "$f"
  curl -s -o /dev/null -w '%{http_code} %{content_type}\n' "http://localhost:8090$f"
done
echo "--- 测试目录不应可访问 ---"
curl -s -o /dev/null -w '  /tests/ -> %{http_code}\n' http://localhost:8090/tests/
```

Expected:
- 首页 `200 text/html`
- `app.css` → `200 text/css`
- 各 `.js` → `200`，content_type 含 `javascript`
- `/tests/` → **200 且返回 index.html**（`try_files` 的回退行为），
  但 `web/tests/` **没有被 COPY 进镜像**，所以取不到任何测试文件

- [ ] **Step 7: 检查点**

- [ ] 首页 200 text/html
- [ ] `app.css` content_type 是 `text/css`
- [ ] 各 JS 200
- [ ] 浏览器打开 `http://<主机>:8090/` 三个 tab 可切换，无 console 报错
- [ ] 右上角健康点变绿（`/healthz` 通）

---

## Task 4: `errors.js` —— 错误归一化（TDD）

规格 §错误处理 的核心：四种错误体形状必须都吃得下，且 **422 的 pydantic `repr`
必须美化**，不能把 pydantic 官网 URL 甩给用户。

**Files:**
- Create: `web/tests/errors.test.mjs`
- Modify: `web/site/js/errors.js`（覆盖 Task 3 的占位）

- [ ] **Step 1: 写失败的测试**

Create `web/tests/errors.test.mjs`:

```js
import { test, describe } from 'node:test';
import assert from 'node:assert/strict';
import { ClientError, kindFor, prettifyPydantic, toClientError } from '../site/js/errors.js';

function resp(status, body, contentType = 'application/json') {
  return new Response(typeof body === 'string' ? body : JSON.stringify(body), {
    status,
    headers: { 'content-type': contentType },
  });
}

describe('kindFor', () => {
  const cases = [
    [429, 'busy'], [507, 'oom'], [409, 'noop'], [404, 'expired'],
    [400, 'bad-request'], [422, 'bad-request'], [500, 'server'], [502, 'server'],
  ];
  for (const [status, kind] of cases) {
    test(`${status} -> ${kind}`, () => assert.equal(kindFor(status), kind));
  }
});

describe('prettifyPydantic', () => {
  test('美化实测的 422 原文，不泄露 pydantic URL', () => {
    // 这是从运行中的网关 curl 出来的真实线格式
    const raw = "[{'type': 'missing', 'loc': ('prompt',), 'msg': 'Field required', " +
                "'input': {}, 'url': 'https://errors.pydantic.dev/2.13/v/missing'}]";
    const out = prettifyPydantic(raw);
    assert.equal(out, 'prompt：必填项缺失');
    assert.ok(!out.includes('errors.pydantic.dev'), '不得把 pydantic 文档 URL 展示给用户');
    assert.ok(!out.includes('{'), '不得残留字典字面量');
  });

  test('多个字段错误用分号连接', () => {
    const raw = "[{'loc': ('prompt',), 'msg': 'Field required'}, " +
                "{'loc': ('n',), 'msg': 'Field required'}]";
    assert.equal(prettifyPydantic(raw), 'prompt：必填项缺失；n：必填项缺失');
  });

  test('嵌套 loc 用点号连接', () => {
    assert.equal(prettifyPydantic("[{'loc': ('body', 'n'), 'msg': 'Field required'}]"),
                 'body.n：必填项缺失');
  });

  test('非 pydantic 的普通消息原样返回', () => {
    assert.equal(prettifyPydantic('height=720 必须是 32 的倍数'),
                 'height=720 必须是 32 的倍数');
  });
});

describe('toClientError', () => {
  test('400 的中文原文不被改动（规格明确豁免）', async () => {
    const e = await toClientError(resp(400, {
      error: { message: 'height=720 必须是 32 的倍数', type: 'invalid_request_error', code: 400 },
    }));
    assert.equal(e.message, 'height=720 必须是 32 的倍数');
    assert.equal(e.kind, 'bad-request');
    assert.equal(e.status, 400);
  });

  test('422 的 pydantic repr 被美化', async () => {
    const raw = "[{'type': 'missing', 'loc': ('prompt',), 'msg': 'Field required', " +
                "'input': {}, 'url': 'https://errors.pydantic.dev/2.13/v/missing'}]";
    const e = await toClientError(resp(422, { error: { message: raw, code: 422 } }));
    assert.equal(e.message, 'prompt：必填项缺失');
    assert.equal(e.kind, 'bad-request');
  });

  test('409 终态 -> noop，文案中性', async () => {
    const e = await toClientError(resp(409, {
      error: { message: '任务已处于终态 succeeded，无法取消', code: 409 },
    }));
    assert.equal(e.kind, 'noop');
    assert.equal(e.message, '任务已处于终态 succeeded，无法取消');
  });

  test('429 -> busy，用我们自己的文案（后端那条太干）', async () => {
    const e = await toClientError(resp(429, { error: { message: '队列已满（32），请稍后重试' } }));
    assert.equal(e.kind, 'busy');
    assert.ok(e.message.includes('队列') || e.message.includes('繁忙'));
  });

  test('404 任务/文件不存在 -> expired', async () => {
    const e = await toClientError(resp(404, {
      error: { message: '任务不存在或已过期', code: 404 },
    }));
    assert.equal(e.kind, 'expired');
    assert.equal(e.message, '任务不存在或已过期');
  });

  test('route-miss 的裸 {"detail":...} 也吃得下', async () => {
    const e = await toClientError(resp(404, { detail: 'Not Found' }));
    assert.equal(e.status, 404);
    assert.ok(e.message);
  });

  test('detail 是数组时不崩', async () => {
    const e = await toClientError(resp(422, { detail: [{ msg: 'bad', loc: ['n'] }] }));
    assert.ok(typeof e.message === 'string' && e.message.length > 0);
  });

  test('nginx 的 HTML 错误体不会把 HTML 甩给用户', async () => {
    const html = '<html><head><title>502 Bad Gateway</title></head><body><h1>502</h1></body></html>';
    const e = await toClientError(resp(502, html, 'text/html'));
    assert.equal(e.kind, 'server');
    assert.ok(!e.message.includes('<'), `消息里不该有 HTML: ${e.message}`);
    assert.ok(e.message.includes('502'));
  });

  test('空 body 不崩', async () => {
    const e = await toClientError(new Response('', { status: 500 }));
    assert.equal(e.kind, 'server');
    assert.ok(e.message.length > 0);
  });
});

describe('ClientError', () => {
  test('是 Error 的实例，带 status/kind', () => {
    const e = new ClientError('x', { status: 400, kind: 'bad-request' });
    assert.ok(e instanceof Error);
    assert.equal(e.name, 'ClientError');
    assert.equal(e.status, 400);
    assert.equal(e.kind, 'bad-request');
  });
});
```

- [ ] **Step 2: 跑测试确认失败**

```bash
cd /root/data1/kdi/workspace/qwen-image
node --test 'web/tests/*.test.mjs'
```
Expected: FAIL —— `prettifyPydantic` / `kindFor` 未导出（Task 3 的占位只有 `ClientError`）。

- [ ] **Step 3: 写实现**

Create（覆盖）`web/site/js/errors.js`:

```js
// 把网关的错误体归一化成 ClientError。
//
// 错误体形状**不统一**（均为实测线格式）：
//   - 400/404/409/422/429 经 HTTPException  → {"error":{"message","type","code"}}
//   - 500/507 同上外壳，但 type 是 "server_error"
//   - 404 路由未命中 → {"detail":"Not Found"}（Starlette 绕过我们的错误处理器）
//   - nginx 层 → HTML
// 所以探测顺序必须是 error.message -> detail -> 兜底文案。

export class ClientError extends Error {
  constructor(message, { status = 0, kind = 'server', raw = null } = {}) {
    super(message);
    this.name = 'ClientError';
    this.status = status;
    this.kind = kind;
    this.raw = raw;
  }
}

/** HTTP 状态 -> 语义 kind。kind 驱动 UI 文案与样式。 */
export function kindFor(status) {
  if (status === 429) return 'busy';
  if (status === 507) return 'oom';
  if (status === 409) return 'noop';       // 已终态、无需取消 —— 是正常结果不是错误
  if (status === 404) return 'expired';
  if (status === 400 || status === 422) return 'bad-request';
  return 'server';
}

const PYDANTIC_RE = /'loc':\s*\(([^)]*)\),\s*'msg':\s*'([^']*)'/g;

// pydantic 的常见 msg 是英文，翻一下；没收录的原样保留
const MSG_ZH = {
  'Field required': '必填项缺失',
  'Input should be a valid integer': '必须是整数',
  'Input should be a valid number': '必须是数字',
  'Input should be a valid string': '必须是字符串',
  'Input should be greater than or equal to 1': '必须 >= 1',
};

/**
 * 把 pydantic 错误列表的 Python repr 美化成中文短语。
 *
 * 实测 422 的 message 长这样（注意还带着 pydantic 文档站的 URL）：
 *   [{'type': 'missing', 'loc': ('prompt',), 'msg': 'Field required',
 *    'input': {}, 'url': 'https://errors.pydantic.dev/2.13/v/missing'}]
 * 那是给开发者看的。直接展示比不展示更糟。
 */
export function prettifyPydantic(message) {
  if (typeof message !== 'string' || !message.includes("'loc':")) return message;

  const parts = [];
  for (const m of message.matchAll(PYDANTIC_RE)) {
    const field = m[1]
      .split(',')
      .map((s) => s.trim().replace(/^['"]|['"]$/g, ''))
      .filter(Boolean)
      .join('.');
    const msg = MSG_ZH[m[2]] || m[2];
    parts.push(field ? `${field}：${msg}` : msg);
  }
  return parts.length ? parts.join('；') : message;
}

/** kind -> 兜底文案（后端没给可读 message 时用）。 */
function fallbackMessage(kind, status) {
  switch (kind) {
    case 'busy':        return '服务繁忙（队列已满或并发占满），请稍后重试';
    case 'oom':         return '显存不足：请降低尺寸或步数后重试';
    case 'noop':        return '任务已结束，无需取消';
    case 'expired':     return '任务或图片不存在、已过期（保留 24 小时）';
    case 'bad-request': return '请求参数不合法';
    default:            return status ? `服务端错误（HTTP ${status}）` : '网络错误';
  }
}

function detailToText(detail) {
  if (typeof detail === 'string') return detail;
  if (Array.isArray(detail)) {
    const parts = detail
      .map((d) => (d && typeof d === 'object' ? d.message || d.msg : String(d)))
      .filter(Boolean);
    return parts.join('；');
  }
  return null;
}

/** 从 Response 造 ClientError。永不抛异常 —— 解析失败也要给出可读消息。 */
export async function toClientError(resp) {
  const status = resp.status;
  const kind = kindFor(status);

  let raw = '';
  try { raw = await resp.text(); } catch { /* body 读不出来也不能崩 */ }

  const contentType = (resp.headers.get('content-type') || '').toLowerCase();
  let message = null;

  if (raw && contentType.includes('json')) {
    try {
      const body = JSON.parse(raw);
      if (body && typeof body === 'object') {
        if (body.error && typeof body.error === 'object') message = body.error.message ?? null;
        else if (body.error) message = String(body.error);
        if (!message && 'detail' in body) message = detailToText(body.detail);
      }
    } catch { /* 声明 JSON 但不是 JSON —— 走兜底 */ }
  }

  if (typeof message !== 'string' || !message.trim()) {
    message = fallbackMessage(kind, status);
  } else if (status === 422) {
    // 只有 422 需要美化：400 的 message 是后端精心写过的中文。
    // 这条豁免是刻意的，见规格 §错误处理。
    message = prettifyPydantic(message);
  }

  // nginx 之类的 HTML 错误体绝不能整段展示
  if (message.includes('<') && message.includes('>') && message.length > 200) {
    message = fallbackMessage(kind, status);
  }

  return new ClientError(message.trim(), { status, kind, raw });
}
```

- [ ] **Step 4: 跑测试确认通过**

```bash
node --test 'web/tests/*.test.mjs'
```
Expected: `ℹ pass <全部> / ℹ fail 0`

- [ ] **Step 5: 检查点**

- [ ] `node --test 'web/tests/*.test.mjs'` → fail 0
- [ ] 其中「美化实测的 422 原文」一条 PASS（它用的是从运行中网关 curl 出来的真实字符串）

---

## Task 5: `presets.js` —— 尺寸预设与提示词模板（TDD）

尺寸校验必须与网关的 `parse_size` 一致：**每条边 256–4096 且是 32 的倍数**。
测试里要有一条**回归护栏**：`1280x720` / `720x1280` 必须被判非法（它们 `%32 != 0`，
放进预设就会被后端 400 拒）。

**Files:**
- Create: `web/tests/presets.test.mjs`
- Create: `web/site/js/presets.js`

- [ ] **Step 1: 写失败的测试**

Create `web/tests/presets.test.mjs`:

```js
import { test, describe } from 'node:test';
import assert from 'node:assert/strict';
import {
  SIZE_PRESETS, PROMPT_TEMPLATES, NEGATIVE_PRESETS,
  validateSize, parseSize,
} from '../site/js/presets.js';

// 与 gateway/app/models.py:parse_size 保持一致的约束
const MIN_SIDE = 256, MAX_SIDE = 4096, MULTIPLE = 32;

describe('validateSize', () => {
  test('合法尺寸返回 null', () => {
    for (const s of ['1024x1024', '512x512', '1536x1024', '256x256', '4096x4096']) {
      assert.equal(validateSize(s), null, `${s} 应该合法`);
    }
  });

  test('形如 WxH 的格式校验', () => {
    for (const bad of ['1024', '1024*1024', 'axb', '', '1024x', 'x1024']) {
      assert.ok(validateSize(bad), `${bad} 应该被拒`);
    }
  });

  test('超出范围被拒', () => {
    assert.ok(validateSize('128x128').includes('256') || validateSize('128x128').includes('范围'));
    assert.ok(validateSize('8192x1024'));
  });

  test('⭐ 回归护栏：1280x720 / 720x1280 必须被拒（%32 != 0）', () => {
    assert.ok(validateSize('1280x720'), '720 % 32 = 16，网关会 400，前端必须拦住');
    assert.ok(validateSize('720x1280'), '720 % 32 = 16，网关会 400，前端必须拦住');
  });

  test('错误文案与网关同义（提到 32 的倍数）', () => {
    assert.ok(validateSize('1000x1000').includes('32'));
  });
});

describe('SIZE_PRESETS', () => {
  test('不为空且每项形状完整', () => {
    assert.ok(SIZE_PRESETS.length >= 4);
    for (const p of SIZE_PRESETS) {
      assert.ok(p.value && p.label, `预设缺字段: ${JSON.stringify(p)}`);
    }
  });

  test('⭐ 每一个预设都必须通过 validateSize —— 预设本身不能是会被 400 拒的值', () => {
    for (const p of SIZE_PRESETS) {
      assert.equal(validateSize(p.value), null, `预设 ${p.value}（${p.label}）不合法`);
    }
  });

  test('每一个预设都能被 parseSize 解析回原值', () => {
    for (const p of SIZE_PRESETS) {
      const { width, height } = parseSize(p.value);
      assert.ok(width % MULTIPLE === 0 && height % MULTIPLE === 0);
      assert.ok(width >= MIN_SIDE && width <= MAX_SIDE);
      assert.ok(height >= MIN_SIDE && height <= MAX_SIDE);
    }
  });

  test('value 唯一', () => {
    const vals = SIZE_PRESETS.map((p) => p.value);
    assert.equal(new Set(vals).size, vals.length);
  });

  test('不含已知不合法值（1280x720 / 720x1280）', () => {
    const vals = SIZE_PRESETS.map((p) => p.value);
    assert.ok(!vals.includes('1280x720'));
    assert.ok(!vals.includes('720x1280'));
  });
});

describe('PROMPT_TEMPLATES', () => {
  test('每项字段完整', () => {
    assert.ok(PROMPT_TEMPLATES.length >= 4);
    for (const t of PROMPT_TEMPLATES) {
      assert.ok(t.id && t.name && t.prompt, `模板缺字段: ${JSON.stringify(t)}`);
      assert.ok(t.category, `模板 ${t.id} 缺 category`);
    }
  });

  test('id 唯一', () => {
    const ids = PROMPT_TEMPLATES.map((t) => t.id);
    assert.equal(new Set(ids).size, ids.length);
  });

  test('模板自带的 size（若有）必须合法', () => {
    for (const t of PROMPT_TEMPLATES) {
      if (t.size) assert.equal(validateSize(t.size), null, `模板 ${t.id} 的 size 不合法`);
    }
  });

  test('模板自带的 trueCfgScale（若有）必须在合理区间', () => {
    for (const t of PROMPT_TEMPLATES) {
      if (t.trueCfgScale != null) {
        assert.ok(t.trueCfgScale >= 1 && t.trueCfgScale <= 10,
                  `模板 ${t.id} 的 trueCfgScale=${t.trueCfgScale} 越界`);
      }
    }
  });

  test('⭐ 凡是 trueCfgScale !== 1 的模板，必须自带 negative 前缀', () => {
    // 上游行为：true_cfg_scale > 1 时若不提供 negative_prompt，
    // pipeline 会警告并以 cfg=1 实际运行 —— 等于白设。模板不能挖这个坑。
    for (const t of PROMPT_TEMPLATES) {
      if (t.trueCfgScale != null && t.trueCfgScale !== 1) {
        assert.ok(t.negative, `模板 ${t.id} 设了 trueCfgScale=${t.trueCfgScale} 却没给 negative`);
      }
    }
  });
});

describe('NEGATIVE_PRESETS', () => {
  test('非空且每一项有 label 与 value', () => {
    assert.ok(NEGATIVE_PRESETS.length >= 1);
    for (const n of NEGATIVE_PRESETS) {
      assert.ok(n.label && n.value);
    }
  });
});
```

- [ ] **Step 2: 跑测试确认失败**

```bash
node --test 'web/tests/*.test.mjs'
```
Expected: FAIL —— `Cannot find module '../site/js/presets.js'`

- [ ] **Step 3: 写实现**

Create `web/site/js/presets.js`:

```js
// 预设数据与尺寸校验。纯数据 + 纯函数，无 DOM 依赖。
//
// ⚠️ 尺寸约束必须与 gateway/app/models.py:parse_size 一致：
//   每条边 256..4096 且是 32 的倍数。
//   常见的 1280x720 / 720x1280 **不满足**（720 % 32 = 16），会被网关 400 拒，
//   所以预设库里不能出现它们 —— 测试里有专门的回归护栏盯这条。

export const SIZE_MIN = 256;
export const SIZE_MAX = 4096;
export const SIZE_MULTIPLE = 32;

const SIZE_RE = /^(\d{1,5})x(\d{1,5})$/;

/**
 * 校验 "WxH"。合法返回 null，非法返回中文错误文案。
 * 文案与网关保持同义（网关原文：「height=720 必须是 32 的倍数」）。
 */
export function validateSize(size) {
  const m = SIZE_RE.exec(String(size ?? '').trim());
  if (!m) return "尺寸必须形如「宽x高」，例如 1024x1024";

  const width = Number(m[1]);
  const height = Number(m[2]);

  for (const [name, v] of [['宽', width], ['高', height]]) {
    if (v < SIZE_MIN || v > SIZE_MAX) {
      return `${name} ${v} 超出范围，需在 ${SIZE_MIN}–${SIZE_MAX} 之间`;
    }
    if (v % SIZE_MULTIPLE !== 0) {
      return `${name} ${v} 必须是 ${SIZE_MULTIPLE} 的倍数（如 ${
        Math.round(v / SIZE_MULTIPLE) * SIZE_MULTIPLE}）`;
    }
  }
  return null;
}

export function parseSize(size) {
  const m = SIZE_RE.exec(String(size ?? '').trim());
  if (!m) throw new Error(`尺寸格式错误: ${size}`);
  return { width: Number(m[1]), height: Number(m[2]) };
}

/**
 * 尺寸预设。全部经过 validateSize 验证（测试会逐个断言）。
 * 刻意不含 1280x720 / 720x1280 —— 它们 %32 != 0。
 */
export const SIZE_PRESETS = [
  { label: '正方形 1024',   value: '1024x1024' },
  { label: '横版 1024×768', value: '1024x768' },
  { label: '竖版 768×1024', value: '768x1024' },
  { label: '宽屏 1344×768', value: '1344x768' },
  { label: '竖长 768×1344', value: '768x1344' },
  { label: '大横 1536×1024', value: '1536x1024' },
  { label: '大竖 1024×1536', value: '1024x1536' },
  { label: '小方 512×512',  value: '512x512' },
  { label: '小竖 512×768',  value: '512x768' },
];

export const SIZE_HINT = '每条边需为 32 的倍数，范围 256–4096';

/**
 * 提示词模板。
 *
 * ⚠️ 凡是给了 trueCfgScale !== 1 的模板，**必须**同时给 negative ——
 * 上游 pipeline 在 true_cfg_scale > 1 而无 negative_prompt 时会警告并退化成 cfg=1，
 * 等于白设。测试里有断言盯这条。
 */
export const PROMPT_TEMPLATES = [
  {
    id: 'photo-portrait',
    category: '摄影写实',
    name: '人像摄影',
    prompt: '专业人像摄影，柔和的侧逆光，浅景深，85mm 定焦，皮肤质感细腻，中性灰背景',
    size: '768x1024',
    trueCfgScale: 4,
    negative: '模糊, 畸变, 多余的手指, 变形的脸, 水印, 文字, 低分辨率',
  },
  {
    id: 'photo-product',
    category: '摄影写实',
    name: '产品图',
    prompt: '电商产品摄影，纯白无缝背景，柔光箱布光，主体居中，商业级细节，微距质感',
    size: '1024x1024',
    trueCfgScale: 4,
    negative: '杂乱背景, 阴影过硬, 反光过曝, 水印, 文字, 品牌 logo',
  },
  {
    id: 'photo-landscape',
    category: '摄影写实',
    name: '风光大片',
    prompt: '广角风光摄影，黄金时刻的暖调光线，云层细节丰富，前景有引导线，超广角透视',
    size: '1536x1024',
    trueCfgScale: 4,
    negative: '过曝, 灰雾, 模糊, 畸变, 水印, 文字',
  },
  {
    id: 'illustration-anime',
    category: '插画动漫',
    name: '日系动画风',
    prompt: '日系动画风格插画，干净的线条，赛璐璐上色，明亮的天空背景，青春氛围',
    size: '768x1024',
    trueCfgScale: 1,
  },
  {
    id: 'illustration-flat',
    category: '插画动漫',
    name: '扁平矢量',
    prompt: '扁平化矢量插画，有限的配色 palette，几何形状，无渐变，留白充足',
    size: '1024x1024',
    trueCfgScale: 1,
  },
  {
    id: 'render-3d',
    category: '3D 渲染',
    name: '3D 卡通角色',
    prompt: '3D 渲染的卡通角色，柔和的全局光照，圆润的造型，次表面散射皮肤，C4D 质感',
    size: '1024x1024',
    trueCfgScale: 4,
    negative: '写实恐怖, 失真, 噪点, 水印, 文字',
  },
  {
    id: 'render-archviz',
    category: '3D 渲染',
    name: '建筑可视化',
    prompt: '建筑效果图渲染，下午的斜射阳光，混凝土与玻璃材质，广角，干净构图',
    size: '1536x1024',
    trueCfgScale: 4,
    negative: '人物杂乱, 过度曝光, 噪点, 水印, 文字',
  },
  {
    id: 'sticker-transparent',
    category: '图标贴纸',
    name: '透明贴纸',
    prompt: '可爱的卡通贴纸设计，扁平配色，粗描边，主体完整居中，四周留白充足',
    size: '1024x1024',
    background: 'transparent',
    trueCfgScale: 1,
  },
  {
    id: 'icon-app',
    category: '图标贴纸',
    name: 'App 图标',
    prompt: '现代 iOS 风格应用图标，圆角方形，简洁的几何主体，柔和渐变背景，居中构图',
    size: '1024x1024',
    trueCfgScale: 1,
  },
  {
    id: 'poster-type',
    category: '海报排版',
    name: '海报主视觉',
    prompt: '电影海报风格主视觉，强烈的中心构图，戏剧性光线，大面积负空间留白供排版',
    size: '768x1344',
    trueCfgScale: 4,
    negative: '文字, 水印, 签名, 杂乱元素, 低对比度',
  },
];

/** 常用负向提示词片段，点一下追加到输入框。 */
export const NEGATIVE_PRESETS = [
  { label: '通用质量', value: '低分辨率, 模糊, 噪点, 过曝, 畸变, 水印, 文字, 签名' },
  { label: '人像',     value: '多余的手指, 四肢畸形, 面部扭曲, 不对称的眼睛, 塑料感皮肤' },
  { label: '产品',     value: '杂乱背景, 阴影过硬, 反光过曝, 品牌 logo, 文字' },
  { label: '风景',     value: '灰雾, 过饱和, 假天空, 重复纹理, 人工感' },
];
```

- [ ] **Step 4: 跑测试确认通过**

```bash
node --test 'web/tests/*.test.mjs'
```
Expected: `ℹ fail 0`

- [ ] **Step 5: 检查点**

- [ ] `node --test 'web/tests/*.test.mjs'` → fail 0
- [ ] 「⭐ 回归护栏：1280x720 / 720x1280 必须被拒」 PASS
- [ ] 「⭐ 每一个预设都必须通过 validateSize」 PASS

---

## Task 6: `api.js` —— 网关封装（TDD）

**最关键的一条约束：所有 URL 用相对路径。** 响应里的 `images[].url` 与 `status_url`
都是按 `PUBLIC_BASE_URL` 拼的绝对地址，从局域网访问会指向 localhost 而裂图。

**Files:**
- Create: `web/tests/api.test.mjs`
- Modify: `web/site/js/api.js`（覆盖 Task 3 的占位）

- [ ] **Step 1: 写失败的测试**

Create `web/tests/api.test.mjs`:

```js
import { test, describe } from 'node:test';
import assert from 'node:assert/strict';
import { createApi } from '../site/js/api.js';
import { ClientError } from '../site/js/errors.js';

/** 记录调用并按脚本返回响应的假 fetch */
function fakeFetch(handler) {
  const calls = [];
  const fn = async (url, init) => {
    calls.push({ url, init });
    return handler(url, init, calls.length - 1);
  };
  fn.calls = calls;
  return fn;
}

const json = (body, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });

describe('URL 构造 —— 必须全是相对路径', () => {
  test('imageUrl 返回相对路径，且不含主机名', () => {
    const api = createApi({ fetchImpl: fakeFetch(() => json({})) });
    const u = api.imageUrl('6e27c6ee-f303-42f9-9679-45e2f3cbd231', 'img_000.png');
    assert.equal(u, '/files/6e27c6ee-f303-42f9-9679-45e2f3cbd231/img_000.png');
    assert.ok(u.startsWith('/'), '必须是相对路径');
    assert.ok(!/https?:/i.test(u), '绝不能带主机名');
  });

  test('imageUrl 对文件名做 URL 编码', () => {
    const api = createApi({ fetchImpl: fakeFetch(() => json({})) });
    assert.ok(api.imageUrl('abc12345', 'a b.png').includes('a%20b.png'));
  });

  test('taskUrl 是相对的', () => {
    const api = createApi({ fetchImpl: fakeFetch(() => json({})) });
    assert.equal(api.taskUrl('abc12345'), '/v1/tasks/abc12345');
  });

  test('⭐ 不使用服务端返回的绝对 url / status_url', async () => {
    const ff = fakeFetch(() =>
      json({
        task_id: 'abc12345', status: 'succeeded',
        status_url: 'http://localhost:8090/v1/tasks/abc12345',
        images: [{
          file: 'img_000.png',
          url: 'http://localhost:8090/files/abc12345/img_000.png',
        }],
      }));
    const api = createApi({ fetchImpl: ff });
    const task = await api.getTask('abc12345');
    // 渲染时应当用 imageUrl(taskId, file)，而不是 task.images[0].url
    const src = api.imageUrl(task.task_id, task.images[0].file);
    assert.ok(!/^https?:/i.test(src), `取图 src 必须是相对路径，实际 ${src}`);
  });
});

describe('submitGeneration', () => {
  test('POST /v1/tasks，JSON body，只带非空字段', async () => {
    const ff = fakeFetch(() => json({ task_id: 't1', status: 'queued' }, 202));
    const api = createApi({ fetchImpl: ff });
    await api.submitGeneration({
      prompt: 'a cat', size: '1024x1024', num_inference_steps: 40,
      seed: 42, negative_prompt: '', n: 1, output_format: 'png',
    });
    const [call] = ff.calls;
    assert.equal(call.url, '/v1/tasks');
    assert.equal(call.init.method, 'POST');
    const body = JSON.parse(call.init.body);
    assert.equal(body.prompt, 'a cat');
    assert.equal(body.size, '1024x1024');
    assert.equal(body.num_inference_steps, 40);
    assert.equal(body.seed, 42);
    assert.ok(!('negative_prompt' in body), '空字符串字段不该发出去');
  });

  test('0 是合法的 seed，不能被当成空值丢掉', async () => {
    const ff = fakeFetch(() => json({ task_id: 't1' }, 202));
    const api = createApi({ fetchImpl: ff });
    await api.submitGeneration({ prompt: 'x', seed: 0 });
    assert.equal(JSON.parse(ff.calls[0].init.body).seed, 0);
  });
});

describe('submitEdit', () => {
  test('multipart，参考图用重复的 image 字段', async () => {
    const ff = fakeFetch(() => json({ task_id: 't2', status: 'queued' }, 202));
    const api = createApi({ fetchImpl: ff });
    const a = new File([new Uint8Array([1, 2, 3])], 'a.png', { type: 'image/png' });
    const b = new File([new Uint8Array([4, 5, 6])], 'b.png', { type: 'image/png' });
    await api.submitEdit('combine them', [a, b], { num_inference_steps: 40 });

    const [call] = ff.calls;
    assert.equal(call.url, '/v1/tasks');
    assert.equal(call.init.method, 'POST');
    const fd = call.init.body;
    assert.ok(fd instanceof FormData, 'body 必须是 FormData');
    assert.equal(fd.get('prompt'), 'combine them');
    assert.equal(fd.getAll('image').length, 2, '两张参考图应是两个同名字段');
    assert.equal(fd.get('num_inference_steps'), '40');
  });

  test('不手动设置 Content-Type（让浏览器带 boundary）', async () => {
    const ff = fakeFetch(() => json({ task_id: 't2' }, 202));
    const api = createApi({ fetchImpl: ff });
    await api.submitEdit('x', [new File([new Uint8Array([1])], 'a.png')]);
    const ct = ff.calls[0].init.headers?.['Content-Type'];
    assert.ok(!ct, `multipart 不能手动设 Content-Type，实际 ${ct}`);
  });

  test('空值字段不追加进 FormData', async () => {
    const ff = fakeFetch(() => json({ task_id: 't2' }, 202));
    const api = createApi({ fetchImpl: ff });
    await api.submitEdit('x', [new File([new Uint8Array([1])], 'a.png')],
                         { negative_prompt: '', seed: null });
    const fd = ff.calls[0].init.body;
    assert.equal(fd.get('negative_prompt'), null);
    assert.equal(fd.get('seed'), null);
  });
});

describe('错误路径', () => {
  test('429 抛 ClientError，kind 为 busy', async () => {
    const api = createApi({
      fetchImpl: fakeFetch(() => json({ error: { message: '队列已满（32）', code: 429 } }, 429)),
    });
    await assert.rejects(
      () => api.getTask('t1'),
      (e) => e instanceof ClientError && e.kind === 'busy' && e.status === 429,
    );
  });

  test('网络异常抛 kind 为 network 的 ClientError', async () => {
    const api = createApi({
      fetchImpl: async () => { throw new TypeError('Failed to fetch'); },
    });
    await assert.rejects(
      () => api.getTask('t1'),
      (e) => e instanceof ClientError && e.kind === 'network',
    );
  });
});

describe('其它端点', () => {
  test('listTasks 带上 limit', async () => {
    const ff = fakeFetch(() => json({ object: 'list', data: [] }));
    const api = createApi({ fetchImpl: ff });
    await api.listTasks(25);
    assert.equal(ff.calls[0].url, '/v1/tasks?limit=25');
  });

  test('cancelTask 用 DELETE', async () => {
    const ff = fakeFetch(() => json({ task_id: 't1', status: 'canceled' }));
    const api = createApi({ fetchImpl: ff });
    await api.cancelTask('t1');
    assert.equal(ff.calls[0].init.method, 'DELETE');
    assert.equal(ff.calls[0].url, '/v1/tasks/t1');
  });

  test('health 走 /healthz', async () => {
    const ff = fakeFetch(() => json({ status: 'ok', backend: 'ok' }));
    const api = createApi({ fetchImpl: ff });
    const h = await api.health();
    assert.equal(ff.calls[0].url, '/healthz');
    assert.equal(h.status, 'ok');
  });
});
```

- [ ] **Step 2: 跑测试确认失败**

```bash
node --test 'web/tests/*.test.mjs'
```
Expected: FAIL —— `api.imageUrl is not a function`（Task 3 的占位只有 `health`）。

- [ ] **Step 3: 写实现**

Create（覆盖）`web/site/js/api.js`:

```js
// 网关 API 封装。
//
// ⚠️ 本模块的核心约束：**所有请求都用相对路径**。
// 网关响应里的 images[].url 与 status_url 是按 PUBLIC_BASE_URL 拼的绝对地址
// （gateway/app/tasks.py:persist_images 用 settings.public_base_url 拼接），
// 从局域网 IP 访问时它们指向 localhost 而裂图。
// 所以取图一律用 imageUrl(taskId, file)，轮询一律用 taskUrl(taskId)。

import { ClientError, toClientError } from './errors.js';

/** 空字符串 / null / undefined 视为「未提供」。注意 0 和 false 是有效值。 */
function isBlank(v) {
  return v === '' || v === null || v === undefined;
}

/** 去掉空值字段，但保留 0 与 false。 */
function prune(obj) {
  const out = {};
  for (const [k, v] of Object.entries(obj)) {
    if (!isBlank(v)) out[k] = v;
  }
  return out;
}

export function createApi({ fetchImpl = globalThis.fetch, base = '' } = {}) {
  async function request(path, init) {
    let resp;
    try {
      resp = await fetchImpl(base + path, init);
    } catch (err) {
      // fetch 只在网络层失败时抛（DNS、连接被拒、CORS）。HTTP 错误码走 !resp.ok。
      throw new ClientError(`网络错误：${err.message}`, { kind: 'network' });
    }
    if (!resp.ok) throw await toClientError(resp);
    if (resp.status === 204) return null;
    return resp.json();
  }

  return {
    /** 相对取图路径。不要用服务端返回的 url 字段。 */
    imageUrl(taskId, file) {
      return `/files/${encodeURIComponent(taskId)}/${encodeURIComponent(file)}`;
    },

    /** 相对轮询路径。不要用服务端返回的 status_url 字段。 */
    taskUrl(taskId) {
      return `/v1/tasks/${encodeURIComponent(taskId)}`;
    },

    health: () => request('/healthz'),
    listModels: () => request('/v1/models'),

    /** 文生图。参数里的空值会被剔除，0/false 保留。 */
    submitGeneration(params) {
      return request('/v1/tasks', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(prune(params)),
      });
    },

    /**
     * 图像编辑。参考图用**重复的** `image` 字段（网关 config.edit_image_field='image'，
     * 同时接受 'image[]'）。
     * 不要手动设 Content-Type —— 必须让浏览器自己带 multipart 的 boundary。
     */
    submitEdit(prompt, files, params = {}) {
      const fd = new FormData();
      fd.append('prompt', prompt);
      for (const f of files) fd.append('image', f, f.name);
      for (const [k, v] of Object.entries(prune(params))) fd.append(k, String(v));
      return request('/v1/tasks', { method: 'POST', body: fd });
    },

    getTask: (taskId) => request(`/v1/tasks/${encodeURIComponent(taskId)}`),
    listTasks: (limit = 50) => request(`/v1/tasks?limit=${encodeURIComponent(limit)}`),
    cancelTask: (taskId) =>
      request(`/v1/tasks/${encodeURIComponent(taskId)}`, { method: 'DELETE' }),
  };
}
```

- [ ] **Step 4: 跑测试确认通过**

```bash
node --test 'web/tests/*.test.mjs'
```
Expected: `ℹ fail 0`

- [ ] **Step 5: 检查点**

- [ ] `node --test 'web/tests/*.test.mjs'` → fail 0
- [ ] 「⭐ 不使用服务端返回的绝对 url / status_url」 PASS
- [ ] 「multipart，参考图用重复的 image 字段」 PASS

---

## Task 7: `store.js` —— localStorage 封装（TDD）

**Files:**
- Create: `web/tests/store.test.mjs`
- Modify: `web/site/js/store.js`（覆盖 Task 3 的占位）

- [ ] **Step 1: 写失败的测试**

Create `web/tests/store.test.mjs`:

```js
import { test, describe } from 'node:test';
import assert from 'node:assert/strict';
import { createStore } from '../site/js/store.js';

/** 内存版 Storage，行为对齐 localStorage */
function memStorage(initial = {}) {
  const map = new Map(Object.entries(initial));
  return {
    getItem: (k) => (map.has(k) ? map.get(k) : null),
    setItem: (k, v) => map.set(k, String(v)),
    removeItem: (k) => map.delete(k),
    get length() { return map.size; },
    key: (i) => [...map.keys()][i] ?? null,
    _map: map,
  };
}

describe('表单参数持久化', () => {
  test('存了能取回来', () => {
    const s = createStore(memStorage());
    s.saveForm('generate', { prompt: 'a cat', size: '1024x1024' });
    assert.deepEqual(s.loadForm('generate', {}), { prompt: 'a cat', size: '1024x1024' });
  });

  test('没存过返回默认值', () => {
    const s = createStore(memStorage());
    assert.deepEqual(s.loadForm('nope', { a: 1 }), { a: 1 });
  });

  test('不同表单名互不干扰', () => {
    const s = createStore(memStorage());
    s.saveForm('generate', { prompt: 'g' });
    s.saveForm('edit', { prompt: 'e' });
    assert.equal(s.loadForm('generate', {}).prompt, 'g');
    assert.equal(s.loadForm('edit', {}).prompt, 'e');
  });

  test('存储里是坏 JSON 时返回默认值而不是抛异常', () => {
    const s = createStore(memStorage({ 'qwi:form:generate': '{不是 JSON' }));
    assert.deepEqual(s.loadForm('generate', { ok: true }), { ok: true });
  });
});

describe('我提交过的任务', () => {
  test('记住并列出', () => {
    const s = createStore(memStorage());
    s.rememberTask('t1', { kind: 'generation' });
    const mine = s.myTasks();
    assert.equal(mine.length, 1);
    assert.equal(mine[0].id, 't1');
    assert.equal(mine[0].kind, 'generation');
  });

  test('最新的在最前', () => {
    const s = createStore(memStorage());
    s.rememberTask('t1', {});
    s.rememberTask('t2', {});
    assert.deepEqual(s.myTasks().map((t) => t.id), ['t2', 't1']);
  });

  test('同一个 id 重复记住不会产生重复项，且会移到最前', () => {
    const s = createStore(memStorage());
    s.rememberTask('t1', { n: 1 });
    s.rememberTask('t2', {});
    s.rememberTask('t1', { n: 2 });
    const ids = s.myTasks().map((t) => t.id);
    assert.deepEqual(ids, ['t1', 't2']);
    assert.equal(s.myTasks()[0].n, 2, '应当更新为新数据');
  });

  test('有上限，不会无限增长', () => {
    const s = createStore(memStorage());
    for (let i = 0; i < 260; i++) s.rememberTask(`t${i}`, {});
    assert.ok(s.myTasks().length <= 200, `实际 ${s.myTasks().length}`);
  });

  test('存储里是坏 JSON 时返回空数组', () => {
    const s = createStore(memStorage({ 'qwi:mine': 'garbage' }));
    assert.deepEqual(s.myTasks(), []);
  });

  test('isMine 能判断', () => {
    const s = createStore(memStorage());
    s.rememberTask('t1', {});
    assert.equal(s.isMine('t1'), true);
    assert.equal(s.isMine('t9'), false);
  });
});

describe('任务详情缓存', () => {
  test('存了能取回来', () => {
    const s = createStore(memStorage());
    s.cacheTask('t1', { status: 'succeeded', images: [{ file: 'a.png' }] });
    assert.equal(s.cacheTask === undefined ? null : s.getCachedTask('t1').status, 'succeeded');
  });

  test('没缓存返回 null', () => {
    const s = createStore(memStorage());
    assert.equal(s.getCachedTask('t1'), null);
  });
});

describe('存储不可用时不能拖垮页面', () => {
  test('setItem 抛异常（配额满/隐私模式）时静默失败', () => {
    const s = createStore({
      getItem: () => null,
      setItem: () => { throw new DOMException('QuotaExceededError'); },
      removeItem: () => {},
    });
    assert.doesNotThrow(() => s.saveForm('generate', { prompt: 'x' }));
    assert.doesNotThrow(() => s.rememberTask('t1', {}));
  });

  test('storage 整个是 null 时也不崩', () => {
    const s = createStore(null);
    assert.doesNotThrow(() => s.saveForm('generate', { prompt: 'x' }));
    assert.deepEqual(s.myTasks(), []);
    assert.deepEqual(s.loadForm('generate', { d: 1 }), { d: 1 });
  });
});
```

- [ ] **Step 2: 跑测试确认失败**

```bash
node --test 'web/tests/*.test.mjs'
```
Expected: FAIL —— `s.saveForm is not a function`

- [ ] **Step 3: 写实现**

Create（覆盖）`web/site/js/store.js`:

```js
// localStorage 封装。所有键带 qwi: 前缀，避免和同域其他东西打架。
//
// 两条纪律：
//   1. 任何读都不得抛异常 —— 存储里的坏 JSON 只该退回默认值；
//   2. 任何写都不得抛异常 —— 配额满是隐私模式的常态，不能因此拖垮页面。

const PREFIX = 'qwi:';
const MAX_MINE = 200;

export function createStore(storage) {
  function read(key, fallback) {
    try {
      const raw = storage?.getItem(PREFIX + key);
      if (raw == null) return fallback;
      return JSON.parse(raw);
    } catch {
      return fallback;
    }
  }

  function write(key, value) {
    try {
      storage?.setItem(PREFIX + key, JSON.stringify(value));
    } catch {
      // 配额满 / 隐私模式 —— 静默失败。持久化是锦上添花，不是功能前提。
    }
  }

  return {
    loadForm: (name, fallback = {}) => read(`form:${name}`, fallback),
    saveForm: (name, value) => write(`form:${name}`, value),

    /** 本浏览器提交过的任务，最新的在前。用于画廊的「只看我的」。 */
    myTasks: () => {
      const list = read('mine', []);
      return Array.isArray(list) ? list : [];
    },

    rememberTask(taskId, meta = {}) {
      const rest = this.myTasks().filter((t) => t && t.id !== taskId);
      rest.unshift({ id: taskId, ...meta });
      write('mine', rest.slice(0, MAX_MINE));
    },

    // 注意：**不能**写成箭头函数。箭头函数不绑定 this，会去外层
    // createStore 作用域里找 —— 那是 undefined（ES module 是严格模式），
    // 调用即抛 TypeError。必须用方法简写才能拿到对象自己的 this。
    isMine(taskId) {
      return this.myTasks().some((t) => t && t.id === taskId);
    },

    getCachedTask: (taskId) => read(`task:${taskId}`, null),
    cacheTask: (taskId, value) => write(`task:${taskId}`, value),
  };
}
```

- [ ] **Step 4: 跑测试确认通过**

```bash
node --test 'web/tests/*.test.mjs'
```
Expected: `ℹ fail 0`

- [ ] **Step 5: 检查点**

- [ ] `node --test 'web/tests/*.test.mjs'` → fail 0
- [ ] 「setItem 抛异常时静默失败」与「storage 整个是 null 时也不崩」PASS

---

## Task 8: `poll.js` —— 轮询器（TDD）

任务要跑 10–30 秒，且网关并发上限只有 1、队列深 32。轮询必须有退避、
能容忍瞬时网络抖动、能超时、能被切页中止。

**Files:**
- Create: `web/tests/poll.test.mjs`
- Create: `web/site/js/poll.js`

- [ ] **Step 1: 写失败的测试**

Create `web/tests/poll.test.mjs`:

```js
import { test, describe } from 'node:test';
import assert from 'node:assert/strict';
import { pollTask, TERMINAL } from '../site/js/poll.js';
import { ClientError } from '../site/js/errors.js';

/** 假 sleep：不真的等，把间隔记下来 */
function fakeSleep(log) {
  return async (ms, signal) => {
    if (signal?.aborted) throw new ClientError('已中止', { kind: 'noop' });
    log.push(ms);
  };
}

/** 按顺序吐出状态的假 fetchTask */
function seqFetchTask(states) {
  let i = 0;
  const fn = async () => states[Math.min(i++, states.length - 1)];
  fn.calls = () => i;
  return fn;
}

describe('TERMINAL', () => {
  test('包含三个终态', () => {
    assert.deepEqual([...TERMINAL].sort(), ['canceled', 'failed', 'succeeded']);
  });
});

describe('pollTask', () => {
  test('跑到 succeeded 就返回', async () => {
    const log = [];
    const task = await pollTask('t1', {
      fetchTask: seqFetchTask([
        { status: 'queued' }, { status: 'running' }, { status: 'succeeded', images: [] },
      ]),
      sleep: fakeSleep(log),
    });
    assert.equal(task.status, 'succeeded');
    assert.equal(log.length, 2, '应当 sleep 两次（queued 后、running 后）');
  });

  test('每次都回调 onUpdate', async () => {
    const seen = [];
    await pollTask('t1', {
      fetchTask: seqFetchTask([{ status: 'queued' }, { status: 'succeeded' }]),
      onUpdate: (s) => seen.push(s.status),
      sleep: fakeSleep([]),
    });
    assert.deepEqual(seen, ['queued', 'succeeded']);
  });

  test('failed 状态作为正常返回值，不抛异常', async () => {
    const task = await pollTask('t1', {
      fetchTask: seqFetchTask([{ status: 'failed', error: '显存不足' }]),
      sleep: fakeSleep([]),
    });
    assert.equal(task.status, 'failed');
    assert.equal(task.error, '显存不足');
  });

  test('⭐ 间隔递增（有退避）', async () => {
    const log = [];
    await pollTask('t1', {
      fetchTask: seqFetchTask([
        { status: 'queued' }, { status: 'queued' }, { status: 'queued' },
        { status: 'queued' }, { status: 'succeeded' },
      ]),
      intervalMs: 1000, maxIntervalMs: 10000, sleep: fakeSleep(log),
    });
    assert.ok(log.length >= 3, `sleep 次数太少: ${log.length}`);
    assert.equal(log[0], 1000);
    assert.ok(log[1] > log[0], `第二次间隔应更长: ${JSON.stringify(log)}`);
    assert.ok(log[2] > log[1], `第三次间隔应更长: ${JSON.stringify(log)}`);
  });

  test('间隔不超过 maxIntervalMs', async () => {
    const log = [];
    await pollTask('t1', {
      fetchTask: seqFetchTask([
        ...Array(12).fill({ status: 'queued' }), { status: 'succeeded' },
      ]),
      intervalMs: 1000, maxIntervalMs: 4000, sleep: fakeSleep(log),
    });
    assert.ok(Math.max(...log) <= 4000, `有间隔超出上限: ${JSON.stringify(log)}`);
  });

  test('⭐ 瞬时网络错误不中断，连续 3 次后给警告', async () => {
    let call = 0;
    const warnings = [];
    const task = await pollTask('t1', {
      fetchTask: async () => {
        call++;
        if (call <= 3) throw new ClientError('网络错误', { kind: 'network' });
        return { status: 'succeeded' };
      },
      onUpdate: (s) => { if (s.warning) warnings.push(s.warning); },
      sleep: fakeSleep([]),
    });
    assert.equal(task.status, 'succeeded', '网络抖动后应当继续轮询直到成功');
    assert.equal(warnings.length, 1, `应当恰好警告一次，实际 ${warnings.length}`);
    assert.ok(warnings[0].includes('3'));
  });

  test('非瞬时错误（如 404 过期）立刻抛出，不重试', async () => {
    let call = 0;
    await assert.rejects(
      () => pollTask('t1', {
        fetchTask: async () => {
          call++;
          throw new ClientError('任务不存在或已过期', { kind: 'expired', status: 404 });
        },
        sleep: fakeSleep([]),
      }),
      (e) => e.kind === 'expired',
    );
    assert.equal(call, 1, '不该重试');
  });

  test('⭐ 超时后抛出，消息里有分钟数', async () => {
    await assert.rejects(
      () => pollTask('t1', {
        fetchTask: async () => ({ status: 'queued' }),
        timeoutMs: 5000, intervalMs: 1000, maxIntervalMs: 1000,
        sleep: fakeSleep([]),
      }),
      (e) => e instanceof ClientError && /超时/.test(e.message),
    );
  });

  test('signal 已中止时立刻抛，且不调 fetchTask', async () => {
    const ctl = new AbortController();
    ctl.abort();
    let called = false;
    await assert.rejects(
      () => pollTask('t1', {
        fetchTask: async () => { called = true; return { status: 'queued' }; },
        signal: ctl.signal,
        sleep: fakeSleep([]),
      }),
    );
    assert.equal(called, false);
  });

  test('轮询途中被中止会退出', async () => {
    const ctl = new AbortController();
    let n = 0;
    await assert.rejects(
      () => pollTask('t1', {
        fetchTask: async () => {
          n++;
          if (n === 2) ctl.abort();
          return { status: 'queued' };
        },
        signal: ctl.signal,
        sleep: async () => {},
      }),
    );
    assert.ok(n <= 3, `中止后不该继续轮询，实际调用了 ${n} 次`);
  });
});
```

- [ ] **Step 2: 跑测试确认失败**

```bash
node --test 'web/tests/*.test.mjs'
```
Expected: FAIL —— `Cannot find module '../site/js/poll.js'`

- [ ] **Step 3: 写实现**

Create `web/site/js/poll.js`:

```js
// 任务轮询器。
//
// 设计要点：
//   - **用「已等待毫秒数」累加计时，而不是 Date.now()** —— 这样测试注入一个假 sleep
//     就能确定性地验证超时，不需要 mock 时钟。
//   - 瞬时错误（网络、5xx）容忍并退避；语义错误（404 过期、400 参数）立刻抛出。
//   - 连续失败达到阈值时通过 onUpdate 发一条 warning 让 UI 显示，
//     但**不中断轮询** —— 任务在服务端还在跑，放弃就等于把结果丢了。

import { ClientError } from './errors.js';

export const TERMINAL = new Set(['succeeded', 'failed', 'canceled']);

const RETRYABLE_KINDS = new Set(['network', 'server']);
const WARN_AFTER_FAILURES = 3;

function defaultSleep(ms, signal) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(resolve, ms);
    signal?.addEventListener('abort', () => {
      clearTimeout(timer);
      reject(new ClientError('已中止', { kind: 'noop' }));
    }, { once: true });
  });
}

/**
 * 轮询到终态。
 *
 * @returns 终态的 task 对象（**status 为 failed 时也正常返回**，由调用方决定怎么展示；
 *          只有「问不到状态」这种情况才抛异常）。
 */
export async function pollTask(taskId, {
  fetchTask,
  onUpdate,
  signal,
  intervalMs = 1000,
  maxIntervalMs = 10000,
  backoff = 1.5,
  timeoutMs = 30 * 60 * 1000,
  sleep = defaultSleep,
} = {}) {
  let waited = 0;
  let interval = intervalMs;
  let failures = 0;

  for (;;) {
    if (signal?.aborted) throw new ClientError('已取消等待', { kind: 'noop' });

    let state = null;
    try {
      state = await fetchTask(taskId);
      failures = 0;
    } catch (err) {
      if (!RETRYABLE_KINDS.has(err?.kind)) throw err;   // 语义错误立刻上抛
      failures++;
      if (failures === WARN_AFTER_FAILURES) {
        onUpdate?.({
          warning: `连续 ${failures} 次轮询失败，仍在重试：${err.message}`,
        });
      }
    }

    if (state && typeof state === 'object') {
      onUpdate?.(state);
      if (TERMINAL.has(state.status)) return state;
    }

    if (waited >= timeoutMs) {
      throw new ClientError(
        `等待超时（${Math.round(timeoutMs / 60000)} 分钟仍未完成）`,
        { kind: 'server' },
      );
    }

    await sleep(interval, signal);
    waited += interval;
    interval = Math.min(Math.round(interval * backoff), maxIntervalMs);
  }
}
```

- [ ] **Step 4: 跑测试确认通过**

```bash
node --test 'web/tests/*.test.mjs'
```
Expected: `ℹ fail 0`

- [ ] **Step 5: 跑全部测试**

```bash
node --test 'web/tests/*.test.mjs' 2>&1 | tail -5
```
Expected: `ℹ pass <N> / ℹ fail 0`，N 至少 60。

- [ ] **Step 6: 检查点**

- [ ] 全部单测通过，fail 0
- [ ] 「⭐ 间隔递增（有退避）」与「⭐ 瞬时网络错误不中断」PASS

---

## Task 9: 生成页

**Files:**
- Create: `web/site/js/views/generate.js`

- [ ] **Step 1: 写实现**

Create `web/site/js/views/generate.js`:

```js
// 文生图页。
import { h, notesBlock, errorBlock, humanMs } from '../ui.js';
import { pollTask } from '../poll.js';
import { SIZE_PRESETS, SIZE_HINT, PROMPT_TEMPLATES, NEGATIVE_PRESETS, validateSize }
  from '../presets.js';

const FORM = 'generate';

export function renderGenerate(root, { api, store, toasts }) {
  const saved = store.loadForm(FORM, {});
  const ctl = new AbortController();

  // ---------- 表单状态 ----------
  const state = {
    prompt: saved.prompt ?? '',
    size: saved.size ?? '1024x1024',
    num_inference_steps: saved.num_inference_steps ?? 40,
    true_cfg_scale: saved.true_cfg_scale ?? 1,
    negative_prompt: saved.negative_prompt ?? '',
    seed: saved.seed ?? '',
    n: saved.n ?? 1,
    output_format: saved.output_format ?? 'png',
    background: saved.background ?? 'auto',
  };
  const persist = () => store.saveForm(FORM, state);

  // ---------- 结果区 ----------
  const statusEl = h('div');
  const resultsEl = h('div', { class: 'results' });

  function renderResults(task) {
    statusEl.replaceChildren();
    resultsEl.replaceChildren();

    if (task.status === 'failed') {
      statusEl.append(h('div', {
        class: 'note err',
        text: `生成失败：${task.error || '未知错误'}`,
      }));
      return;
    }
    if (task.status === 'canceled') {
      statusEl.append(h('div', {
        class: 'note',
        text: '任务已取消。注意：后端那一次生成仍会跑完 —— diffusers 没有取消接口。',
      }));
      return;
    }
    if (task.status !== 'succeeded') return;

    const ms = task.timings?.backend_s ? ` · 后端耗时 ${humanMs(task.timings.backend_s * 1000)}` : '';
    statusEl.append(h('div', {
      class: 'note',
      text: `完成${ms} · ${task.images?.length ?? 0} 张`,
    }));

    const notes = notesBlock(task.notes);
    if (notes) statusEl.append(notes);

    for (const img of task.images ?? []) {
      // ⭐ 用 file + task_id 拼相对路径，不用 img.url（那是 PUBLIC_BASE_URL 拼的绝对地址）
      const src = api.imageUrl(task.task_id, img.file);
      const image = h('img', { src, alt: '生成结果', loading: 'lazy' });
      const card = h('div', { class: 'card' }, [
        image,
        h('div', { class: 'meta' }, [
          `${img.width}×${img.height} · ${img.mode}`,
          ' · ',
          h('a', { href: src, download: img.file, text: '下载' }),
        ]),
      ]);
      // 图片有 24h TTL，过期后 404 —— 显示「已过期」而不是破图
      image.addEventListener('error', () => {
        resultsEl.replaceChild(
          h('div', { class: 'card expired', text: '图片已过期（保留 24 小时）' }),
          card,
        );
      });
      resultsEl.append(card);
    }
  }

  // ---------- 提交 ----------
  let running = false;

  async function submit() {
    if (running) return;
    const err = validateSize(state.size);
    if (err) { toasts.error(err); return; }
    if (!state.prompt.trim()) { toasts.error('请填写提示词'); return; }

    // 上游行为：true_cfg_scale > 1 而无 negative_prompt 时会静默退化成 cfg=1
    if (Number(state.true_cfg_scale) > 1 && !state.negative_prompt.trim()) {
      toasts.warn('true_cfg_scale > 1 但未填负向提示词 —— 上游会忽略它并按 cfg=1 运行。'
                  + '请补上负向提示词，或把 true_cfg_scale 设回 1。');
    }

    running = true;
    submitBtn.disabled = true;
    statusEl.replaceChildren(h('div', { class: 'progress' }, [
      h('div', { class: 'spinner' }),
      h('div', { class: 'status-line', text: '正在提交…' }),
    ]));
    resultsEl.replaceChildren();

    try {
      const { task_id } = await api.submitGeneration({
        prompt: state.prompt,
        size: state.size,
        num_inference_steps: Number(state.num_inference_steps),
        true_cfg_scale: Number(state.true_cfg_scale),
        negative_prompt: state.negative_prompt.trim() || undefined,
        seed: state.seed === '' ? undefined : Number(state.seed),
        n: Number(state.n),
        output_format: state.output_format,
        background: state.background,
      });
      store.rememberTask(task_id, { kind: 'generation', prompt: state.prompt.slice(0, 80) });
      const line = statusEl.querySelector('.status-line');

      const task = await pollTask(task_id, {
        fetchTask: api.getTask,
        signal: ctl.signal,
        onUpdate: (s) => {
          if (s.warning) { toasts.warn(s.warning); return; }
          if (line && s.status) line.textContent = `状态：${s.status}…`;
          if (s.status === 'queued') line.textContent = '排队中…（并发上限 1）';
        },
      });
      renderResults(task);
    } catch (e) {
      if (e?.kind === 'noop') return;            // 切页中止，静默
      statusEl.replaceChildren(errorBlock(e));
      toasts.error(e.message);
    } finally {
      running = false;
      submitBtn.disabled = false;
    }
  }

  // ---------- 控件 ----------
  const promptEl = h('textarea', {
    placeholder: '描述你想要的画面…',
    value: state.prompt,
    onInput: (e) => { state.prompt = e.target.value; persist(); },
  });

  const sizeSelect = h('select', {
    onChange: (e) => { state.size = e.target.value; sizeInput.value = e.target.value; persist(); },
  });
  for (const p of SIZE_PRESETS) {
    sizeSelect.append(h('option', { value: p.value, text: `${p.label}`, selected: p.value === state.size }));
  }
  const sizeInput = h('input', {
    type: 'text', value: state.size, class: 'hint',
    onInput: (e) => { state.size = e.target.value; persist(); },
  });

  const tmplSelect = h('select', {
    onChange: (e) => {
      const t = PROMPT_TEMPLATES.find((x) => x.id === e.target.value);
      if (!t) return;
      state.prompt = t.prompt;
      if (t.size) state.size = t.size;
      if (t.trueCfgScale != null) state.true_cfg_scale = t.trueCfgScale;
      if (t.background) state.background = t.background;
      if (t.negative && !state.negative_prompt.trim()) state.negative_prompt = t.negative;
      persist();
      rerenderInputs();
    },
  });
  tmplSelect.append(h('option', { value: '', text: '— 选择提示词模板 —' }));
  for (const t of PROMPT_TEMPLATES) {
    tmplSelect.append(h('option', { value: t.id, text: `[${t.category}] ${t.name}` }));
  }

  const stepsEl = h('input', {
    type: 'number', min: 1, max: 150, value: state.num_inference_steps,
    onInput: (e) => { state.num_inference_steps = e.target.value; persist(); },
  });
  const cfgEl = h('input', {
    type: 'number', min: 1, max: 10, step: 0.5, value: state.true_cfg_scale,
    onInput: (e) => { state.true_cfg_scale = e.target.value; persist(); },
  });
  const seedEl = h('input', {
    type: 'text', value: state.seed, placeholder: '留空则随机',
    onInput: (e) => { state.seed = e.target.value; persist(); },
  });
  const nEl = h('input', {
    type: 'number', min: 1, max: 10, value: state.n,
    onInput: (e) => { state.n = e.target.value; persist(); },
  });
  const fmtEl = h('select', {
    onChange: (e) => { state.output_format = e.target.value; persist(); },
  });
  for (const f of ['png', 'jpg', 'webp']) {
    fmtEl.append(h('option', { value: f, text: f, selected: f === state.output_format }));
  }
  const bgEl = h('select', {
    onChange: (e) => { state.background = e.target.value; persist(); },
  });
  bgEl.append(h('option', { value: 'auto', text: '不透明', selected: state.background === 'auto' }));
  bgEl.append(h('option', {
    value: 'transparent', text: '透明背景', selected: state.background === 'transparent',
  }));

  const negativeEl = h('textarea', {
    value: state.negative_prompt,
    placeholder: '不希望出现的元素…',
    onInput: (e) => { state.negative_prompt = e.target.value; persist(); },
  });

  const negPresetRow = h('div', { class: 'row' });
  for (const p of NEGATIVE_PRESETS) {
    negPresetRow.append(h('button', {
      type: 'button', class: 'hint', text: `+ ${p.label}`,
      onClick: () => {
        state.negative_prompt = state.negative_prompt.trim()
          ? `${state.negative_prompt.trim()}, ${p.value}` : p.value;
        negativeEl.value = state.negative_prompt;
        persist();
      },
    }));
  }

  const submitBtn = h('button', { class: 'primary', text: '生成', onClick: submit });

  function rerenderInputs() {
    promptEl.value = state.prompt;
    sizeInput.value = state.size;
    sizeSelect.value = state.size;
    stepsEl.value = state.num_inference_steps;
    cfgEl.value = state.true_cfg_scale;
    negativeEl.value = state.negative_prompt;
    bgEl.value = state.background;
  }

  // ---------- 装配 ----------
  const form = h('div', { class: 'panel' }, [
    h('h2', { text: '文生图' }),
    h('label', { class: 'field' }, [h('span', { text: '提示词模板' }), tmplSelect]),
    h('label', { class: 'field' }, [h('span', { text: '提示词' }), promptEl]),
    h('label', { class: 'field' }, [
      h('span', { text: '尺寸' }), sizeSelect,
      h('div', { class: 'row' }, [sizeInput, h('span', { class: 'hint', text: SIZE_HINT })]),
    ]),
    h('div', { class: 'row' }, [
      h('label', { class: 'field', style: 'flex:1' }, [
        h('span', { text: '步数' }), stepsEl,
      ]),
      h('label', { class: 'field', style: 'flex:1' }, [
        h('span', { text: 'true_cfg_scale' }), cfgEl,
      ]),
      h('label', { class: 'field', style: 'flex:1' }, [
        h('span', { text: '张数' }), nEl,
      ]),
    ]),
    h('div', { class: 'row' }, [
      h('label', { class: 'field', style: 'flex:1' }, [h('span', { text: '种子' }), seedEl]),
      h('button', {
        type: 'button', class: 'hint', text: '随机',
        onClick: () => {
          state.seed = String(Math.floor(Math.random() * 2 ** 31));
          seedEl.value = state.seed; persist();
        },
      }),
    ]),
    h('div', { class: 'row' }, [
      h('label', { class: 'field', style: 'flex:1' }, [h('span', { text: '格式' }), fmtEl]),
      h('label', { class: 'field', style: 'flex:1' }, [h('span', { text: '背景' }), bgEl]),
    ]),
    h('label', { class: 'field' }, [h('span', { text: '负向提示词' }), negativeEl, negPresetRow]),
    h('p', {
      class: 'hint',
      text: '真实 CFG 参数是 true_cfg_scale（本后端没有 guidance_scale）。'
          + '它 > 1 时必须同时填负向提示词，否则上游会当成 1 处理。',
    }),
    h('div', { class: 'row' }, [submitBtn]),
  ]);

  root.append(h('div', { class: 'cols' }, [
    form,
    h('div', { class: 'panel' }, [h('h2', { text: '结果' }), statusEl, resultsEl]),
  ]));

  rerenderInputs();

  // 切页时中止轮询。服务端任务不受影响，回来还能从画廊看到。
  return () => ctl.abort();
}
```

- [ ] **Step 2: 重建**

```bash
cd /root/data1/kdi/workspace/qwen-image
docker compose build web && docker compose up -d web && sleep 2
curl -s -o /dev/null -w '%{http_code}\n' http://localhost:8090/js/views/generate.js
```
Expected: `200`

- [ ] **Step 3: ⭐ 端到端验证一次真实生成**

浏览器打开 `http://<主机>:8090/#/generate`，填提示词
`a red apple on a wooden table, studio lighting`，尺寸 `1024x1024`，步数 40，
点「生成」。

Expected:
- 状态行依次显示「排队中…」→「状态：running…」
- 10–30 秒后出现 1 张 1024×1024 的图
- 结果区上方显示「完成 · 后端耗时 X 秒 · 1 张」
- 下载链接可用
- 右上角排队数变化后回落

- [ ] **Step 4: 验证透明背景与 x_notes 渲染**

选「透明背景」+ 提示词 `a cartoon dragon sticker, flat colors`，生成。

Expected: 出图为 PNG，且结果区出现**服务端说明块**（若有 notes）。
用命令行独立确认 alpha 真的透明：

```bash
python3 - <<'EOF'
import httpx, io
from PIL import Image
r = httpx.get("http://localhost:8090/v1/tasks", params={"limit": 1}).json()
tid = r["data"][0]["task_id"]
t = httpx.get(f"http://localhost:8090/v1/tasks/{tid}").json()
print("status:", t["status"], "| notes:", t.get("notes"))
if t.get("images"):
    blob = httpx.get(f"http://localhost:8090{t['images'][0]['url'].split('8090',1)[1]}").content
    im = Image.open(io.BytesIO(blob))
    print("mode:", im.mode, "| alpha 范围:", im.getchannel("A").getextrema())
    assert im.getchannel("A").getextrema()[0] == 0, "alpha 最小值不是 0 —— 透明未生效"
    print("✅ 透明生效（alpha 最小值为 0）")
EOF
```
Expected: `alpha 最小值 0` —— **只看 `mode == "RGBA"` 会得到假阳性**，
普通文生图的 alpha 范围是 (253,255)，也有通道但没有透明像素。

- [ ] **Step 5: 检查点**

- [ ] 浏览器里能成功生成一张图并下载
- [ ] 透明背景输出的 alpha 最小值为 0（命令行确认）
- [ ] `x_notes` / `notes` 非空时在页面上可见
- [ ] `true_cfg_scale=4` 且负向提示词为空时弹出警告

---

## Task 10: 编辑页

**Files:**
- Create: `web/site/js/views/edit.js`

- [ ] **Step 1: 写实现**

Create `web/site/js/views/edit.js`:

```js
// 图像编辑页。
//
// ⚠️ 刻意不提供 mask 上传控件 —— 后端 QwenImage21Pipeline 没有 mask 参数，
// 收到 mask 会明确返回 400（backend/server.py:edits）。给出控件就等于承诺局部重绘。
import { h, notesBlock, errorBlock, humanMs, humanBytes } from '../ui.js';
import { pollTask } from '../poll.js';

// 注意：这里**不** import validateSize —— 编辑接口不传 size，
// 分辨率由后端按参考图数量决定（单图 1024 / 多图 512）。
const FORM = 'edit';
const MAX_REFS = 10;          // 与 gateway config.max_ref_images 一致
const MAX_UPLOAD = 16 * 1024 * 1024;  // 与 config.max_upload_bytes 一致

export function renderEdit(root, { api, store, toasts }) {
  const saved = store.loadForm(FORM, {});
  const ctl = new AbortController();

  const state = {
    prompt: saved.prompt ?? '',
    num_inference_steps: saved.num_inference_steps ?? 40,
    true_cfg_scale: saved.true_cfg_scale ?? 1,
    negative_prompt: saved.negative_prompt ?? '',
    seed: saved.seed ?? '',
    output_format: saved.output_format ?? 'png',
  };
  const persist = () => store.saveForm(FORM, state);

  /** @type {File[]} */
  let files = [];

  const statusEl = h('div');
  const resultsEl = h('div', { class: 'results' });
  const thumbsEl = h('div', { class: 'thumbs' });
  const multiRefNote = h('div');

  function renderThumbs() {
    thumbsEl.replaceChildren();
    files.forEach((f, i) => {
      thumbsEl.append(h('div', { class: 'thumb' }, [
        h('img', { src: URL.createObjectURL(f), alt: f.name }),
        h('button', {
          type: 'button', text: '×', title: '移除',
          onClick: () => { files.splice(i, 1); renderThumbs(); renderMultiRefNote(); },
        }),
      ]));
    });
  }

  function renderMultiRefNote() {
    multiRefNote.replaceChildren();
    if (files.length >= 2) {
      // 上游限制：多参考图 + output_resolution >= 768 会退化成"近似返回最后一张"
      multiRefNote.append(h('div', {
        class: 'note warn',
        text: `已选 ${files.length} 张参考图。上游 pipeline 在 output_resolution ≥ 768 时`
            + '会忽略编辑指令、近似原样返回最后一张参考图，所以后端会自动降到 512 分辨率。'
            + '需要高分辨率的双图合成时请分开做。',
      }));
    }
  }

  function addFiles(list) {
    for (const f of list) {
      if (files.length >= MAX_REFS) { toasts.error(`参考图最多 ${MAX_REFS} 张`); break; }
      if (!f.type.startsWith('image/')) { toasts.error(`${f.name} 不是图片`); continue; }
      if (f.size > MAX_UPLOAD) {
        toasts.error(`${f.name} 有 ${humanBytes(f.size)}，超过单张 16MB 上限`);
        continue;
      }
      files.push(f);
    }
    renderThumbs();
    renderMultiRefNote();
  }

  // ---------- 提交 ----------
  let running = false;

  async function submit() {
    if (running) return;
    if (!state.prompt.trim()) { toasts.error('请填写编辑指令'); return; }
    if (!files.length) { toasts.error('至少需要一张参考图'); return; }

    if (Number(state.true_cfg_scale) > 1 && !state.negative_prompt.trim()) {
      toasts.warn('true_cfg_scale > 1 但未填负向提示词 —— 上游会忽略它并按 cfg=1 运行。');
    }

    running = true;
    submitBtn.disabled = true;
    statusEl.replaceChildren(h('div', { class: 'progress' }, [
      h('div', { class: 'spinner' }),
      h('div', { class: 'status-line', text: '正在上传并提交…' }),
    ]));
    resultsEl.replaceChildren();

    try {
      const { task_id } = await api.submitEdit(state.prompt, files, {
        num_inference_steps: Number(state.num_inference_steps),
        true_cfg_scale: Number(state.true_cfg_scale),
        negative_prompt: state.negative_prompt.trim() || undefined,
        seed: state.seed === '' ? undefined : Number(state.seed),
        output_format: state.output_format,
      });
      store.rememberTask(task_id, { kind: 'edit', prompt: state.prompt.slice(0, 80) });
      const line = statusEl.querySelector('.status-line');

      const task = await pollTask(task_id, {
        fetchTask: api.getTask,
        signal: ctl.signal,
        onUpdate: (s) => {
          if (s.warning) { toasts.warn(s.warning); return; }
          if (line && s.status) {
            line.textContent = s.status === 'queued' ? '排队中…（并发上限 1）' : `状态：${s.status}…`;
          }
        },
      });

      statusEl.replaceChildren();
      resultsEl.replaceChildren();

      if (task.status === 'failed') {
        statusEl.append(h('div', { class: 'note err', text: `编辑失败：${task.error || '未知错误'}` }));
        return;
      }
      if (task.status !== 'succeeded') return;

      const ms = task.timings?.backend_s ? ` · 后端耗时 ${humanMs(task.timings.backend_s * 1000)}` : '';
      statusEl.append(h('div', { class: 'note', text: `完成${ms}` }));
      const notes = notesBlock(task.notes);
      if (notes) statusEl.append(notes);

      for (const img of task.images ?? []) {
        const src = api.imageUrl(task.task_id, img.file);
        const image = h('img', { src, alt: '编辑结果', loading: 'lazy' });
        const card = h('div', { class: 'card' }, [
          image,
          h('div', { class: 'meta' }, [
            `${img.width}×${img.height} · ${img.mode}`, ' · ',
            h('a', { href: src, download: img.file, text: '下载' }),
          ]),
        ]);
        image.addEventListener('error', () => {
          resultsEl.replaceChild(
            h('div', { class: 'card expired', text: '图片已过期（保留 24 小时）' }), card);
        });
        resultsEl.append(card);
      }
    } catch (e) {
      if (e?.kind === 'noop') return;
      statusEl.replaceChildren(errorBlock(e));
      toasts.error(e.message);
    } finally {
      running = false;
      submitBtn.disabled = false;
    }
  }

  // ---------- 控件 ----------
  const fileInput = h('input', {
    type: 'file', accept: 'image/*', multiple: true, style: 'display:none',
    onChange: (e) => { addFiles([...e.target.files]); e.target.value = ''; },
  });

  const drop = h('div', {
    class: 'drop',
    onClick: () => fileInput.click(),
    onDragover: (e) => { e.preventDefault(); drop.classList.add('over'); },
    onDragleave: () => drop.classList.remove('over'),
    onDrop: (e) => {
      e.preventDefault();
      drop.classList.remove('over');
      addFiles([...e.dataTransfer.files]);
    },
  }, [
    h('div', { text: '拖拽图片到此处，或点击选择' }),
    h('div', { class: 'hint', text: `最多 ${MAX_REFS} 张，单张 ≤ 16MB` }),
  ]);

  const promptEl = h('textarea', {
    placeholder: '例如：把背景换成黄昏的海边',
    value: state.prompt,
    onInput: (e) => { state.prompt = e.target.value; persist(); },
  });

  const stepsEl = h('input', {
    type: 'number', min: 1, max: 150, value: state.num_inference_steps,
    onInput: (e) => { state.num_inference_steps = e.target.value; persist(); },
  });
  const cfgEl = h('input', {
    type: 'number', min: 1, max: 10, step: 0.5, value: state.true_cfg_scale,
    onInput: (e) => { state.true_cfg_scale = e.target.value; persist(); },
  });
  const seedEl = h('input', {
    type: 'text', value: state.seed, placeholder: '留空则随机',
    onInput: (e) => { state.seed = e.target.value; persist(); },
  });
  const negativeEl = h('textarea', {
    value: state.negative_prompt, placeholder: '不希望出现的元素…',
    onInput: (e) => { state.negative_prompt = e.target.value; persist(); },
  });

  const submitBtn = h('button', { class: 'primary', text: '开始编辑', onClick: submit });

  const form = h('div', { class: 'panel' }, [
    h('h2', { text: '图像编辑' }),
    h('div', { class: 'note', text: '本后端不支持 mask 局部重绘（pipeline 没有 mask 参数，'
      + '传了会明确报错）。编辑是整图重绘，请用指令描述要改什么。' }),
    fileInput, drop, thumbsEl, multiRefNote,
    h('label', { class: 'field' }, [h('span', { text: '编辑指令' }), promptEl]),
    h('div', { class: 'row' }, [
      h('label', { class: 'field', style: 'flex:1' }, [h('span', { text: '步数' }), stepsEl]),
      h('label', { class: 'field', style: 'flex:1' }, [h('span', { text: 'true_cfg_scale' }), cfgEl]),
      h('label', { class: 'field', style: 'flex:1' }, [h('span', { text: '种子' }), seedEl]),
    ]),
    h('label', { class: 'field' }, [h('span', { text: '负向提示词' }), negativeEl]),
    h('div', { class: 'row' }, [submitBtn]),
  ]);

  root.append(h('div', { class: 'cols' }, [
    form,
    h('div', { class: 'panel' }, [h('h2', { text: '结果' }), statusEl, resultsEl]),
  ]));

  renderThumbs();
  return () => ctl.abort();
}
```

> 注意：`validateSize` 在这个文件里 import 了但未使用？**不要**保留未使用的 import ——
> 实现时若用不到就删掉这一行。编辑接口不传 size（后端自己按参考图数量定分辨率）。

- [ ] **Step 2: 重建并验证静态可访问**

```bash
cd /root/data1/kdi/workspace/qwen-image
docker compose build web && docker compose up -d web && sleep 2
curl -s -o /dev/null -w '%{http_code}\n' http://localhost:8090/js/views/edit.js
```
Expected: `200`

- [ ] **Step 3: ⭐ 端到端验证单参考图编辑**

造一张合成参考图（避免依赖外部素材，与 `smoke.py` 的做法一致）：

```bash
python3 - <<'EOF'
from PIL import Image, ImageDraw
im = Image.new("RGB", (512, 512), "white")
d = ImageDraw.Draw(im)
d.rectangle((102, 102, 409, 409), fill="red")
im.save("/tmp/ref_red_square.png")
print("已生成 /tmp/ref_red_square.png")
EOF
```

浏览器打开 `http://<主机>:8090/#/edit`，拖入该图，指令填
`add a blue circle next to the red square`，步数 40，点「开始编辑」。

Expected: 10–20 秒后出图，图里同时有红色方块与蓝色圆形。

- [ ] **Step 4: ⭐ 端到端验证多参考图（触发 512 降级提示）**

再造一张蓝圆，同时拖入两张图，指令填
`combine the red square and the blue circle into one composition`。

Expected:
- 选中 2 张后**立即出现黄色提示块**，说明会降到 512
- 出图尺寸为 `512×512`
- 结果区显示服务端 notes（后端自动降到 512 的说明）

命令行独立确认：

```bash
python3 - <<'EOF'
import httpx
t = httpx.get("http://localhost:8090/v1/tasks", params={"limit": 1}).json()["data"][0]["task_id"]
d = httpx.get(f"http://localhost:8090/v1/tasks/{t}").json()
img = d["images"][0]
print("尺寸:", img["width"], "×", img["height"])
print("notes:", d.get("notes"))
assert img["width"] == 512, f"多参考图应降到 512，实际 {img['width']}"
assert any("512" in n for n in d.get("notes") or []), "缺少自动降低分辨率的说明"
print("✅ 多参考图降级与说明均正确")
EOF
```
Expected: 断言全过。

- [ ] **Step 5: 验证上传上限落到了 nginx（防 413 回归）**

```bash
python3 - <<'EOF'
import io, httpx
from PIL import Image
import random
# 造一张 ~2MB 的 PNG（随机噪点不好压缩）
im = Image.new("RGB", (900, 900))
im.putdata([(random.randint(0,255),)*3 for _ in range(900*900)])
buf = io.BytesIO(); im.save(buf, format="PNG")
blob = buf.getvalue()
print(f"参考图大小 {len(blob)/1048576:.2f} MB")
r = httpx.post("http://localhost:8090/v1/tasks",
               data={"prompt": "add a small star", "num_inference_steps": 20},
               files=[("image", ("big.png", blob, "image/png"))], timeout=60)
print("提交状态码:", r.status_code)
assert r.status_code == 202, f"被拒了 —— nginx client_max_body_size 没生效？{r.text[:300]}"
print("✅ 2MB 上传经代理未被 413 拒")
EOF
```
Expected: `202`。若为 `413` → 回 Task 1 Step 1 检查 `client_max_body_size`。

- [ ] **Step 6: 检查点**

- [ ] 单参考图编辑成功
- [ ] 多参考图：选 2 张时出现降级提示，出图为 512×512，notes 可见
- [ ] 2MB 参考图经代理返回 202（不是 413）
- [ ] 页面上**没有** mask 上传控件

---

## Task 11: 画廊页

**Files:**
- Create: `web/site/js/views/gallery.js`

- [ ] **Step 1: 写实现**

Create `web/site/js/views/gallery.js`:

```js
// 画廊。GET /v1/tasks 只返回任务骨架、不含图片，所以需要「列表 + 逐个详情」。
//
// ⚠️ 本服务无鉴权，/v1/tasks 返回**所有**任务，不只本浏览器提交的。
// 默认显示全部，提供「只看我提交的」筛选（task_id 存在 localStorage）。
import { h, notesBlock, humanMs } from '../ui.js';

const DETAIL_CONCURRENCY = 4;   // 别一次打 50 个请求
const DETAIL_LIMIT = 24;        // 首屏只取这么多任务的详情

const STATUS_TEXT = {
  queued: '排队中', running: '生成中', succeeded: '完成',
  failed: '失败', canceled: '已取消',
};

function fmtTime(epochSec) {
  if (!epochSec) return '';
  const d = new Date(epochSec * 1000);
  const pad = (n) => String(n).padStart(2, '0');
  return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

/** 有界并发映射。 */
async function mapLimit(items, limit, fn) {
  const out = new Array(items.length);
  let next = 0;
  async function worker() {
    while (next < items.length) {
      const i = next++;
      try { out[i] = await fn(items[i], i); } catch (e) { out[i] = { error: e }; }
    }
  }
  await Promise.all(Array.from({ length: Math.min(limit, items.length) }, worker));
  return out;
}

export function renderGallery(root, { api, store, toasts }) {
  let onlyMine = store.loadForm('gallery', { onlyMine: false }).onlyMine;
  let aborted = false;

  const listEl = h('div', { class: 'task-list' });
  const statusEl = h('div');

  async function load() {
    statusEl.replaceChildren(h('div', { class: 'progress' }, [
      h('div', { class: 'spinner' }),
      h('div', { class: 'status-line', text: '正在加载任务列表…' }),
    ]));
    listEl.replaceChildren();

    try {
      const res = await api.listTasks(50);
      if (aborted) return;

      let items = res.data ?? [];
      if (onlyMine) items = items.filter((t) => store.isMine(t.task_id));

      if (!items.length) {
        statusEl.replaceChildren(h('div', {
          class: 'note',
          text: onlyMine ? '本浏览器还没有提交过任务。' : '还没有任何任务。',
        }));
        return;
      }

      statusEl.replaceChildren(h('div', {
        class: 'note',
        text: `${items.length} 个任务${onlyMine ? '（只看我提交的）' : '（全部 —— 本服务无鉴权，'
          + '这里会显示所有人的任务）'}。图片保留 24 小时。`,
      }));

      // 先渲染骨架（状态与时间立刻可见）
      const nodes = new Map();
      for (const t of items) {
        const badge = h('span', {
          class: `badge ${t.status}`,
          text: STATUS_TEXT[t.status] ?? t.status,
        });
        const node = h('div', { class: 'task' }, [
          h('div', { class: 'task-head' }, [
            badge,
            h('span', { text: t.kind === 'edit' ? '编辑' : '生成' }),
            h('span', { text: fmtTime(t.created_at) }),
            h('span', { class: 'hint', text: t.task_id.slice(0, 12) }),
            h('button', {
              type: 'button', class: 'hint', text: '取消',
              onClick: async (e) => {
                e.target.disabled = true;
                try {
                  await api.cancelTask(t.task_id);
                  toasts.ok('已请求取消。注意：后端那一次生成仍会跑完。');
                  load();
                } catch (err) {
                  // 409 = 任务已终态。这是正常结果，不是错误。
                  if (err.kind === 'noop') toasts.show(err.message, 'info');
                  else toasts.error(err.message);
                  e.target.disabled = false;
                }
              },
            }),
          ]),
          h('div', { class: 'results' }),
        ]);
        nodes.set(t.task_id, node);
        listEl.append(node);
      }

      // 只对终态且前 DETAIL_LIMIT 个拉详情（有界并发）
      const wanted = items.filter((t) => t.status === 'succeeded').slice(0, DETAIL_LIMIT);
      await mapLimit(wanted, DETAIL_CONCURRENCY, async (t) => {
        if (aborted) return;
        // 命中本地缓存就不重复拉
        let detail = store.getCachedTask(t.task_id);
        if (!detail || detail.status !== 'succeeded') {
          detail = await api.getTask(t.task_id);
          store.cacheTask(t.task_id, detail);
        }
        if (aborted) return;
        const holder = nodes.get(t.task_id)?.querySelector('.results');
        if (!holder) return;
        holder.replaceChildren();

        const notes = notesBlock(detail.notes);
        if (notes) holder.append(notes);

        for (const img of detail.images ?? []) {
          const src = api.imageUrl(t.task_id, img.file);
          const image = h('img', { src, alt: '结果', loading: 'lazy' });
          const card = h('div', { class: 'card' }, [
            image,
            h('div', { class: 'meta' }, [
              `${img.width}×${img.height}`, ' · ',
              h('a', { href: src, download: img.file, text: '下载' }),
            ]),
          ]);
          image.addEventListener('error', () => {
            holder.replaceChild(
              h('div', { class: 'card expired', text: '已过期（保留 24 小时）' }), card);
          });
          holder.append(card);
        }
      });
    } catch (e) {
      if (aborted) return;
      statusEl.replaceChildren(h('div', { class: 'note err', text: `加载失败：${e.message}` }));
    }
  }

  const onlyMineBox = h('input', {
    type: 'checkbox', checked: onlyMine, style: 'width:auto',
    onChange: (e) => {
      onlyMine = e.target.checked;
      store.saveForm('gallery', { onlyMine });
      load();
    },
  });

  root.append(h('div', { class: 'panel' }, [
    h('h2', { text: '历史任务' }),
    h('div', { class: 'row' }, [
      h('label', { class: 'row' }, [onlyMineBox, h('span', { text: '只看我提交的' })]),
      h('button', { type: 'button', text: '刷新', onClick: load }),
      h('span', {
        class: 'hint',
        text: '⚠️ 本服务无鉴权，「全部」包含其他人提交的任务。',
      }),
    ]),
    statusEl,
    listEl,
  ]));

  load();
  return () => { aborted = true; };
}
```

- [ ] **Step 2: 重建并验证**

```bash
cd /root/data1/kdi/workspace/qwen-image
docker compose build web && docker compose up -d web && sleep 2
curl -s -o /dev/null -w '%{http_code}\n' http://localhost:8090/js/views/gallery.js
```
Expected: `200`

- [ ] **Step 3: ⭐ 浏览器验证**

打开 `http://<主机>:8090/#/gallery`。

Expected:
- 列出之前的任务（含 Task 9/10 产出的），状态徽章颜色正确
- 成功的任务下方显示缩略图，点「下载」可存
- 勾「只看我提交的」后列表变短（只剩本浏览器提交的）
- 点「刷新」重新加载
- 对一个已完成的任务点「取消」→ 出现**中性提示**「任务已处于终态…」而不是红色报错

- [ ] **Step 4: 检查点**

- [ ] 画廊列出任务并显示图片
- [ ] 「只看我提交的」筛选有效
- [ ] 取消一个已成功的任务 → 中性提示，不是红色 error
- [ ] 他人任务在「全部」下可见，且有明确说明

---

## Task 12: `scripts/smoke_web.py` —— 经代理的端到端验证

**Files:**
- Create: `scripts/smoke_web.py`

- [ ] **Step 1: 写脚本**

Create `scripts/smoke_web.py`:

```python
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
import subprocess
import sys
import time
from pathlib import Path

import httpx
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))
from smoke import Check, Failure  # noqa: E402

TIMEOUT = httpx.Timeout(600.0, connect=10.0)


def synth_png(side: int, noisy: bool = False) -> bytes:
    """造合成图。noisy=True 时用噪点让它压不小，用来测上传上限。"""
    import random

    if noisy:
        im = Image.new("RGB", (side, side))
        im.putdata([(random.randint(0, 255),) * 3 for _ in range(side * side)])
    else:
        from PIL import ImageDraw

        im = Image.new("RGB", (side, side), "white")
        ImageDraw.Draw(im).rectangle((side * 0.2, side * 0.2, side * 0.8, side * 0.8), fill="red")
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


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
                         ("/js/api.js", "javascript"), ("/js/poll.js", "javascript")):
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
    ("upload-limit", check_upload_limit),
    ("cancel-terminal", check_cancel_terminal),
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
```

- [ ] **Step 2: 跑起来**

```bash
cd /root/data1/kdi/workspace/qwen-image
python3 scripts/smoke_web.py
```
Expected: 全部 8 项 PASS（`static` / `proxy` / `traversal-guard` / `generate-roundtrip` /
`async-notes` / `upload-limit` / `cancel-terminal` / `host-ports`）。
生成类检查会真跑三次扩散（约 40~60 秒）。

- [ ] **Step 3: ⭐ 验证第 6 条检查真的会失败（证明它有效）**

```bash
cd /root/data1/kdi/workspace/qwen-image
cp web/nginx.conf /tmp/nginx.conf.bak
sed -i 's/^  client_max_body_size 200m;/  client_max_body_size 1m;/' web/nginx.conf
docker compose build web >/dev/null && docker compose up -d web >/dev/null && sleep 2
python3 scripts/smoke_web.py 2>&1 | grep -A4 'upload-limit'
```
Expected: `[FAIL] upload-limit` —— 被 413 拒。

**必须改回来：**

```bash
cp /tmp/nginx.conf.bak web/nginx.conf
docker compose build web >/dev/null && docker compose up -d web >/dev/null && sleep 2
python3 scripts/smoke_web.py 2>&1 | grep -A4 'upload-limit'
grep -n 'client_max_body_size' web/nginx.conf   # 确认是 200m
```
Expected: 回到 `[PASS]`，且 grep 输出 `200m`。

- [ ] **Step 4: 检查点**

- [ ] `python3 scripts/smoke_web.py` 全部 PASS
- [ ] 第 3 步的临时改动已还原（`grep 'client_max_body_size' web/nginx.conf` 是 200m）
- [ ] `rm -f /tmp/nginx.conf.bak`

---

## Task 13: Makefile 与 README

**Files:**
- Modify: `Makefile`
- Modify: `README.md`

- [ ] **Step 1: 加 Makefile 目标**

在 `Makefile` 的 `.PHONY` 行加入 `smoke-web test-web logs-web`：

```make
.PHONY: help up down restart ps logs logs-backend logs-gateway logs-web build pull-weights gate smoke smoke-web test-web shell-backend shell-gateway shell-web status
```

在 `help` 目标里加三行：

```make
	@echo "  make smoke-web      经 nginx 的前端/反代端到端验证"
	@echo "  make test-web       前端纯逻辑单元测试（node --test）"
	@echo "  make logs-web       跟踪 web 容器日志"
```

追加目标实现：

```make
# 前端纯逻辑单元测试。⚠️ 必须是引号 glob —— 
# `node --test web/tests`（目录形式）在 Node 24 上是坏的（实测 pass 0 / fail 1）。
test-web:
	node --test 'web/tests/*.test.mjs'

# 经 nginx 的端到端验证。会真跑三次扩散，约 40~60 秒。
smoke-web:
	python3 scripts/smoke_web.py

logs-web:
	$(COMPOSE) logs -f --tail=100 web
```

- [ ] **Step 2: 验证 Makefile 目标可用**

```bash
cd /root/data1/kdi/workspace/qwen-image
make test-web 2>&1 | tail -4
```
Expected: `ℹ pass <N>` / `ℹ fail 0`

```bash
make help | tail -8
```
Expected: 列出 `smoke-web` / `test-web` / `logs-web`

- [ ] **Step 3: 更新 README 的架构图**

在 `README.md` 里把开头那张架构图：

```
客户端 (OpenAI SDK / curl)
      │
      ▼
gateway  :8090   FastAPI，纯 CPU
  ...
      │
      ▼
backend  :8091   diffusers 跑 QwenImage21Pipeline，独占一张 H100
      │
      ▼
    H100 (GPU_ID)
```

替换为：

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
  DELETE /v1/tasks/{id}         取消
  GET  /v1/models · /healthz · /files/{id}/{name}
      │
      ▼
backend  :8091   diffusers 跑 QwenImage21Pipeline，独占一张 H100（不发布宿主机端口）
      │
      ▼
    H100 (GPU_ID)
```

在架构图下方补一段：

```markdown
**只有 8090 一个端口对外。** 网关与后端都只监听容器内的端口，不发布到宿主机。
验证：`ss -ltn | grep -E ':8090|:8091'` 应当只出现 8090。
`make smoke-web` 的第 10 项检查就在断言这条约束 —— 谁把 `ports` 加回来，它会红。

排障时不能从宿主机直接 curl 网关，改用 compose 网络内部：

```bash
docker compose exec web curl -si http://gateway:8090/healthz | head -5
# gateway 镜像里没有 curl/wget（python:3.12-slim 基座），只能用 python
docker compose exec gateway python -c "import urllib.request as u; print(u.urlopen('http://127.0.0.1:8090/healthz').read().decode())"
```
```

- [ ] **Step 4: 在 README 的「快速开始」里加前端说明**

```markdown
### 前端

启动后浏览器打开 <http://localhost:8090/>，三个页签：

| 页签 | 用途 |
|---|---|
| 生成 | 文生图。支持尺寸预设、提示词模板、负向提示词、种子、张数、透明背景 |
| 编辑 | 图像编辑。拖拽 1~10 张参考图 + 指令 |
| 画廊 | 历史任务与产出，可下载、取消、只看自己提交的 |

全部走**异步任务接口**（`POST /v1/tasks` + 轮询），避免长请求被浏览器或代理掐断。

**不支持 mask 局部重绘** —— 后端 pipeline 没有 mask 参数，页面上刻意不提供该控件。

**多参考图会自动降到 512 分辨率**：上游在 `output_resolution ≥ 768` 时会忽略编辑指令、
近似原样返回最后一张参考图。页面选中 2 张以上时会明确提示。

前端是零构建的原生 ES 模块，无 CDN 依赖（内网离线可用）：

```bash
make test-web        # 纯逻辑单元测试（node --test）
make smoke-web       # 经 nginx 的端到端验证
```
```

- [ ] **Step 5: 在 README 的「配置」表里补 nginx 相关**

在配置表末尾追加：

| 变量 | 默认 | 说明 |
|---|---|---|
| — | `200m` | nginx `client_max_body_size`（`web/nginx.conf`）。**必须 ≥ `max_upload_bytes` × `max_ref_images`**，改一边要改另一边 |

- [ ] **Step 5b: ⭐ 同步更新规格文档**

规格里有两处说法已被本次实现推翻，必须改，否则文档会与代码矛盾：

**（1）规格 §影响面与回滚** 里有：

```markdown
**本次唯一改动既有代码的地方，就是 `scripts/smoke.py` 那一条断言。**
```

改为：

```markdown
**本次改动既有代码的地方有两处**（都是本设计暴露出来的既有缺陷，不是它引入的）：

1. `scripts/smoke.py` —— 修一条恒真的穿越断言（见风险 3）
2. `gateway/app/tasks.py` —— 修异步任务的 notes 被丢弃：`_notes_for` 读的是
   `notes_json` 列，但提交时的 notes 存在 `request_json.notes` 里；同时 `_process`
   从不合并后端上报的 `result.notes`。后果是前端**只走异步路径**，
   「服务端对参数的说明」永远显示不出来 —— 包括多参考图的 512 降级说明。
   同步路径一直是对的，所以既有冒烟测试掩盖了它。
```

**（2）规格 §未决/已知风险** 末尾追加一条：

```markdown
5. **异步任务 notes 曾被静默丢弃（已修）**：见 §影响面与回滚。
   修好后 `scripts/smoke_web.py` 的 `async-notes` 检查会盯住它 ——
   前端依赖 notes 显示服务端纠正，回归了必须立刻发现。
```

- [ ] **Step 5c: 在 README 的「已知限制」里记录两处既有缺陷**

在 `README.md` 的「已知限制」章节末尾追加：

```markdown
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
```

- [ ] **Step 5d: 更新计划文档里「唯一改动」的自述**

本计划文档开头的 `⚠️ 动手前必读` 一节与「修改」表格已说明是两处改动。
规格（Step 5b）与本计划若仍有出入，以本计划为准。

- [ ] **Step 6: 最终全量验证**

```bash
cd /root/data1/kdi/workspace/qwen-image
echo "=========== 1. 单元测试 ==========="
make test-web 2>&1 | tail -3
echo
echo "=========== 2. 服务状态与端口 ==========="
docker compose ps
echo "--- 宿主机 listener ---"
ss -ltn | grep -E ':8090|:8091'
echo
echo "=========== 3. 网关冒烟（回归，经代理） ==========="
make smoke 2>&1 | tail -6
echo
echo "=========== 4. Web 端到端 ==========="
make smoke-web 2>&1 | tail -12
```

Expected:
- 单元测试 `fail 0`
- `docker compose ps` 三服务 Up，gateway/backend 的 PORTS 为空
- `ss -ltn` 只有 8090
- `make smoke` 全绿（含修好的 `negatives`）
- `make smoke-web` 全部 PASS

- [ ] **Step 7: 检查点**

- [ ] `make test-web` / `make smoke` / `make smoke-web` 三者皆绿
- [ ] `ss -ltn` 只有 8090
- [ ] README 架构图与端口说明已更新
- [ ] 浏览器手测三个页签无 console 报错

---

## 验收清单

全部完成后逐条核对：

**端口**

- [ ] `ss -ltn | grep -E ':8090|:8091'` 只有 8090
- [ ] `docker compose ps` 里 gateway / backend 的 PORTS 列为空
- [ ] `curl localhost:8090/` → 200 text/html
- [ ] `curl localhost:8090/healthz` → gateway 的 JSON
- [ ] 浏览器 `http://<主机>:8090/` 可用

**功能**

- [ ] 生成：能出图、能下载、透明背景 alpha 最小值为 0
- [ ] 编辑：单图正常；多图给出 512 降级提示且 notes 可见
- [ ] 画廊：列表、缩略图、下载、只看我的、取消（409 显示为中性提示）
- [ ] 切页不中断服务端任务；切回来能从画廊看到结果

**诚实性（规格 §UI 契约）**

- [ ] 编辑页**没有** mask 上传控件
- [ ] 页面**没有** `guidance_scale` 输入
- [ ] `true_cfg_scale ≠ 1` 且负向提示词为空 → 有警告
- [ ] 参考图 ≥2 张 → 有 512 降级提示
- [ ] `x_notes` / `notes` 会渲染出来
- [ ] 取消按钮文案准确（不谎称已终止后端生成）
- [ ] 过期的图显示「已过期」而不是破图

**测试**

- [ ] `make test-web` → fail 0
- [ ] `make smoke` → 全绿，且 `negatives` 断言数为 2（修好的）
- [ ] `make smoke-web` → 全部 8 项 PASS
- [ ] `grep -c 'if False' gateway/app/routes/ops.py` → 0（Task 2 缺陷 A 的临时改动无残留）
- [ ] `grep 'client_max_body_size' web/nginx.conf` → 200m

**既有缺陷修复（Task 2）**

- [ ] 缺陷 A（`scripts/smoke.py`）：穿越断言不再恒真 —— `negatives` PASS 且断言数为 2
- [ ] 缺陷 B（`gateway/app/tasks.py`）：`_notes_for` 读 `request_json.notes` 而非 `notes_json`，
      且 `_process` 合并了 `result.notes`；异步多参考图任务的 `notes` 非空
      （`make smoke-web` 的 `async-notes` PASS）
- [ ] 规格 `docs/superpowers/specs/2026-09-24-web-ui-design.md` 里
      「本次唯一改动既有代码的地方」一句已改为两处（Task 13 Step 5b）
- [ ] README「已知限制」里有这两处缺陷的记录（Task 13 Step 5c）

---

## 已知事项（不在本次范围）

1. **`/docs` 依赖 CDN**：FastAPI 的 Swagger UI 从 jsdelivr 加载资源，内网无外网时会白屏。
   反代保留（有外网时可用），但不作为交付面。
2. **取消不彻底**：取消 `running` 任务时网关标记 `canceled` 并丢弃结果，
   但后端那一次生成仍会跑完（diffusers 没有取消接口）。UI 文案已如实说明。
3. **无鉴权**：画廊的「全部」包含他人任务。UI 有明确提示。
4. **无按服务细分的端口检查**：Task 12 的 host-ports 检查读的是整机 listener 表，
   若日后宿主机上出现无关容器占用 8091 会误报。

### Task 1 质量评审提出、**刻意延后**的项（都是既有问题，不是本次引入）

5. **网关没有内存上限，且 `mask` 字段从不做大小检查。**
   `client_max_body_size 200m` 允许单个请求把约 200MB 读进网关内存
   （`gateway/app/edit_form.py` 的 `await ref.read()` 先读后判；`mask` 那一支
   压根没有大小校验），而 compose 里 `gateway` 服务没有 `mem_limit`。
   本机是共享机器（另有 15 个他人容器），并发几个大上传会造成主机内存压力。
   **注意：本次改动是在改善它** —— 改动前网关直接 `0.0.0.0:8090` 对外、
   完全没有上限，200m 是**新增**的边界，不是回归。
   真要修需要在 compose 给 `gateway` 加内存上限 + 在网关侧做总请求大小校验。
6. **`worker_processes auto` 在本机（192 核）会起 192 个空闲 worker。**
   容器 RSS 134MiB vs 网关 53MiB。不是危害（宿主 1.9TiB 内存）但是浪费。
   一行改成固定小值即可。
7. **`/etc/nginx/conf.d/default.conf` 仍在镜像里**（继承自基础镜像）。
   当前无害——我们的 `nginx.conf` 只 `include mime.types`，没有 include `conf.d/`。
   但如果日后有人加上惯例的 `include conf.d/*.conf`，它自带的 `listen 80`
   会与我们的 server 冲突。基础镜像的 `/usr/share/nginx/html/50x.html`
   也仍可访问（`/50x.html` → 200）。
   Dockerfile 的「无 `RUN`」约束使得 `RUN rm` 这条常规修法不可用。
8. **`proxy_buffering off` 的理由最弱。** 本 API 没有流式响应，开着缓冲更省资源
   （关掉后 nginx 要等客户端读完才能释放上游连接）。影响有限
   （生成名额在响应序列化前就已释放），保留是因为它也确实无害。
9. **静态资源正则 `location ~ \.` 是个较宽的网。** 任何**未来的**、
   不在 `/v1/` 或 `/files/` 下、且路径含点的路由都会被它吞掉。
   新增这类路由时必须加 `^~` 或精确匹配。目前没有这样的路由。
