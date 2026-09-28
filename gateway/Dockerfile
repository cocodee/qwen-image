# 网关只是个 FastAPI 进程，不碰 GPU，用小镜像即可。
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# 先装依赖再拷代码，让改代码时不必重装依赖层
COPY pyproject.toml ./
RUN pip install --no-cache-dir \
      "fastapi>=0.115" \
      "uvicorn[standard]>=0.30" \
      "httpx>=0.27" \
      "pydantic>=2.7" \
      "pydantic-settings>=2.3" \
      "python-multipart>=0.0.9" \
      "pillow>=10.3"

COPY app ./app

# 以非 root 运行；data/ 由 compose bind-mount 进来，构建时只建目录
RUN useradd -r -u 10001 -m gateway \
 && mkdir -p /app/data \
 && chown -R gateway:gateway /app
USER gateway

EXPOSE 8090

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8090/healthz', timeout=4).status==200 else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8090"]
