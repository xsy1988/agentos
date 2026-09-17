# search-api：五级流水线编排 + MCP Server(/mcp) + REST(/search /fetch /health /metrics)
#
# 刻意「瘦」：torch 与 playwright/chromium 都不在这个镜像里（设计方案 §7.3 —— 重依赖只在
# 容器隔离区，且各自独立成容器）。这里只有 httpx / trafilatura / jieba / mcp，
# 冷启动秒级、常驻内存百兆级，才配得上「每次工具调用都走它」的定位。
FROM python:3.12-slim

# UV_LINK_MODE=copy：overlayfs 上硬链接会退化成整份复制并报警告
# UV_PROJECT_ENVIRONMENT：把 .venv 固定在 /app/.venv，CMD 直接引用
# CACHE_DIR=/data：sqlite 三表（query_cache/url_cache/metrics）落挂载点，跨重启保留
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    CACHE_DIR=/data

WORKDIR /app

# trafilatura 走 lxml（有 manylinux/aarch64 wheel，无需编译器）；curl 供 healthcheck
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir uv

# 依赖层与代码层分开缓存：只改 src 时不重装依赖
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src ./src
RUN uv sync --frozen --no-dev

RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 8200

# healthcheck 只探 REST /health（不触发搜索，避免探活本身吃满召回预算）；
# start_period 给足冷启动：jieba 首次分词要建词典索引（约 1~2s）
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8200/health || exit 1

CMD ["/app/.venv/bin/python", "-m", "websearch.mcp_server"]
