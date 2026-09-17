# reranker：cross-encoder 精排容器（BAAI/bge-reranker-v2-m3，CPU 推理）
#
# 为什么独立镜像：torch + transformers 约 2.5GB 依赖、模型常驻约 2.2GB 内存。
# 把它关在这里，search-api 才能保持「秒级冷启动 + 百兆常驻」，平台核心进程更是零重依赖
# （设计方案 §7.3）。缺它不阻塞：流水线自动跳过精排、沿用 RRF 序并标记 rerank=skipped。
FROM python:3.12-slim

# torch 的 CPU wheel 源随架构不同（x86_64 有 +cpu 变体、aarch64 直接用 PyPI 默认包），
# 所以交给构建参数：装不上就回落 PyPI。绝不装 CUDA 变体——本服务只跑 CPU，白占 2GB。
ARG TORCH_INDEX_URL=https://download.pytorch.org/whl/cpu
# 模型下载源：国内网络默认走镜像站（compose 可覆盖成 https://huggingface.co）
ARG HF_ENDPOINT=https://hf-mirror.com

# HF_HOME=/models：模型缓存落命名卷，不烤进镜像
# TRANSFORMERS_NO_ADVISORY_WARNINGS / TOKENIZERS_PARALLELISM：只留我们自己的状态行，
# 并避免 tokenizer fork 告警刷屏（单 worker 不会真 fork，但告警很扰人）
# HF_ENDPOINT 取上面的构建参数作为默认值，compose 在运行时可再覆盖
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HF_HOME=/models \
    HF_HUB_DISABLE_TELEMETRY=1 \
    TRANSFORMERS_NO_ADVISORY_WARNINGS=1 \
    TOKENIZERS_PARALLELISM=false \
    HF_ENDPOINT=${HF_ENDPOINT}

WORKDIR /app

# libgomp1：torch 的 OpenMP 运行时（slim 镜像不带，缺了会 `libgomp.so.1: cannot open shared object file`）
# curl：healthcheck
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 curl \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir torch --index-url "${TORCH_INDEX_URL}" \
    || pip install --no-cache-dir torch

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py ./

# 模型缓存卷挂载点：不烤进镜像（2.2GB 会让构建与分发都难受），
# 首次启动下载一次后由命名卷 reranker_models 持久化
RUN mkdir -p /models
VOLUME ["/models"]

EXPOSE 8000

# /health 在「加载中」也返回 200（status=loading）：模型加载是后台任务，不阻塞 uvicorn
# 开始 accept，所以服务启动几秒内就能探活。模型是否就绪看响应里的 loaded 字段
# （search_meta 会如实报出来），而不是看容器健康状态。
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/health || exit 1

# 单 worker：模型只加载一份，并发由 app.py 里的 asyncio.Semaphore(1) 串行化
# （CPU 上并发推理只会互相抢线程，排队反而总时延更低）
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
