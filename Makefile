SHELL := /bin/bash
COMPOSE := docker compose

.DEFAULT_GOAL := help
.PHONY: help up down restart ps logs logs-backend logs-gateway logs-web build pull-weights gate smoke smoke-web test-web shell-backend shell-gateway shell-web status

help:
	@echo "Qwen-Image-2.1 服务"
	@echo
	@echo "  make pull-weights   预取 33GB 权重（首次必做，约 40 分钟）"
	@echo "  make gate           验证基座镜像在本机 CUDA 上能跑（2 分钟 go/no-go）"
	@echo "  make up             构建并启动（自动挑一张空闲 GPU）"
	@echo "  make up GPU_ID=3    指定 GPU 启动"
	@echo "  make smoke          端到端冒烟验证"
	@echo "  make smoke-web      经 nginx 的前端/反代端到端验证"
	@echo "  make test-web       前端纯逻辑单元测试（node --test）"
	@echo "  make logs-web       跟踪 web 容器日志"
	@echo "  make logs           跟踪全部日志"
	@echo "  make status         服务与 GPU 占用一览"
	@echo "  make down           停止"
	@echo

# GPU_ID 走 shell 层兜底，而不是 make 的 $(shell)：
# 这样 make down / make logs 不会因为「没有空闲卡」而整个报错。
# 命令行传的 GPU_ID 会被 make 自动导出，所以 `make up GPU_ID=3` 优先级最高。
up:
	@GPU_ID="$${GPU_ID:-$$(python3 scripts/pick_gpu.py)}"; \
	 if [ -z "$$GPU_ID" ]; then echo "未取到 GPU_ID" >&2; exit 1; fi; \
	 echo "==> 使用 GPU $$GPU_ID"; \
	 touch .env; \
	 if grep -q '^GPU_ID=' .env; then \
	   sed -i "s/^GPU_ID=.*/GPU_ID=$$GPU_ID/" .env; \
	 else \
	   echo "GPU_ID=$$GPU_ID" >> .env; \
	 fi; \
	 echo "   已写入 .env，后续 docker compose 命令可直接使用"; \
	 $(COMPOSE) up -d --build

down:
	$(COMPOSE) down

restart: down up

ps:
	$(COMPOSE) ps

status:
	@$(COMPOSE) ps
	@echo
	@echo "--- GPU 占用（本服务应只动一张卡）---"
	@nvidia-smi --query-gpu=index,memory.used,memory.total,utilization.gpu --format=csv

logs:
	$(COMPOSE) logs -f --tail=100

logs-backend:
	$(COMPOSE) logs -f --tail=100 backend

logs-gateway:
	$(COMPOSE) logs -f --tail=100 gateway

logs-web:
	$(COMPOSE) logs -f --tail=100 web

build:
	$(COMPOSE) build

pull-weights:
	./scripts/pull_weights.sh

# CUDA 兼容闸（构建后跑）。
# GPU 可用性没法在 docker build 里断言 —— BuildKit 的 RUN 步骤拿不到 GPU，
# 所以必须构建后单独验。这一关决定后面所有事能不能进行下去。
#
# 背景：torch 用的是 cu128 构建，与本机驱动 570.86.15 精确匹配。
# （原计划的 SGLang 路线要求 cuda-python>=13 → cu130 → 驱动 >=580，被此闸拦下。）
gate:
	@GPU_ID="$${GPU_ID:-$$(python3 scripts/pick_gpu.py)}"; \
	 echo "==> 在 GPU $$GPU_ID 上验证 qwen-image-backend:local"; \
	 docker run --rm --gpus "device=$$GPU_ID" qwen-image-backend:local python -c "\
import sys, torch; \
print('torch', torch.__version__); \
print('built for CUDA', torch.version.cuda); \
print('cuda_available', torch.cuda.is_available()); \
print('device', torch.cuda.get_device_name(0) if torch.cuda.is_available() else 'N/A'); \
from diffusers import QwenImage21Pipeline; \
print('QwenImage21Pipeline OK'); \
sys.exit(0 if torch.cuda.is_available() else 1)"

smoke:
	python3 scripts/smoke.py

# 前端纯逻辑单元测试。⚠️ 必须是引号 glob ——
# `node --test web/tests`（目录形式）在 Node 24 上是坏的（实测 pass 0 / fail 1）。
test-web:
	node --test 'web/tests/*.test.mjs'

# 经 nginx 的端到端验证。会真跑三次扩散。
# 实测 H100 热模型下约 14 秒；冷启动或模型在忙时会明显更久。
smoke-web:
	python3 scripts/smoke_web.py

shell-backend:
	$(COMPOSE) exec backend bash

shell-web:
	$(COMPOSE) exec web sh

shell-gateway:
	$(COMPOSE) exec gateway bash
