# SAF-SFT-Gateway 单一项目入口。
# 两个子项目依赖各自独立：SAF 用 uv，网关用 venv + pip。

SHELL := /usr/bin/env bash
SAF_DIR := science-agent-factory
GW_DIR := sft-gateway
GW_VENV := $(GW_DIR)/.venv
GW_PY := $(GW_VENV)/bin/python
GW_CLI := $(GW_VENV)/bin/sft-gateway

.DEFAULT_GOAL := help
.PHONY: help install install-saf install-gateway gateway-serve gateway-health \
        saf-run saf-status test test-saf test-gateway lint lint-saf lint-gateway check clean

help:
	@printf '%s\n' \
	  'SAF-SFT-Gateway' \
	  '' \
	  '  make install          安装两个子项目（SAF: uv sync；网关: venv + pip）' \
	  '  make gateway-serve    前台启动 SFT Gateway（默认 0.0.0.0:8100）' \
	  '  make gateway-health   健康检查网关' \
	  '  make saf-run SEED=... 运行 SAF（需先配置 configs/local.yaml）' \
	  '  make test             运行两个子项目测试' \
	  '  make lint             运行两个子项目静态检查' \
	  '  make check            lint + test' \
	  '  make clean            清理虚拟环境与缓存'

install: install-saf install-gateway

install-saf:
	cd $(SAF_DIR) && uv sync --extra dev

install-gateway:
	python3 -m venv $(GW_VENV)
	$(GW_PY) -m pip install --upgrade pip
	$(GW_PY) -m pip install -e './$(GW_DIR)[test]'

gateway-serve:
	$(GW_CLI) serve --host 0.0.0.0 --port 8100

gateway-health:
	curl --fail http://127.0.0.1:8100/health

saf-run:
	cd $(SAF_DIR) && uv run saf --config configs/local.yaml run $(SEED)

saf-status:
	cd $(SAF_DIR) && uv run saf --config configs/local.yaml status $(JOB)

test: test-saf test-gateway

test-saf:
	cd $(SAF_DIR) && uv run pytest -q

test-gateway:
	cd $(GW_DIR) && .venv/bin/python -m pytest -q

lint: lint-saf lint-gateway

lint-saf:
	cd $(SAF_DIR) && uv run ruff check src tests

lint-gateway:
	cd $(GW_DIR) && .venv/bin/python -m compileall -q sft_gateway

check: lint test

clean:
	rm -rf $(GW_VENV) $(SAF_DIR)/.pytest_cache $(SAF_DIR)/.ruff_cache
	find . -type d -name __pycache__ -prune -exec rm -rf {} +