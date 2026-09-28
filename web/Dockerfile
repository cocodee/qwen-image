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
