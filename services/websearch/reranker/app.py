"""reranker 容器：cross-encoder 精排服务（BAAI/bge-reranker-v2-m3，CPU 推理）。

为什么单独一个容器：torch + transformers 是重依赖，按设计方案 §7.3 永不进平台核心进程；
模型常驻内存（约 2.2GB）也让「每次搜索现加载模型」这种荒谬延迟不可能发生。

工程要点：
- `torch.set_num_threads(min(8, cpu))`：CPU 上线程给太多反而因同步开销变慢
- `asyncio.Semaphore(1)` **串行**推理：CPU 上并发推理只会互相拖慢，排队反而总时延更低
- 推理跑在 `asyncio.to_thread`，事件循环不被阻塞 → `/health` 永远能秒回（平台探活靠它）
- 分批 + **按长度分桶**的动态 padding：真实 pair 大多远短于 max_length，按最长的那条 padding
  才不浪费算力；同批长度接近，CPU 上省下的就是实打实的秒数
- **软预算 deadline**：超时预算用完就停止给剩余文档打分，已打的分照常返回（`partial=true`）。
  剩余的都是 RRF 序尾部，牺牲它们是最划算的降级——比整条查询失败好得多。

实测成本模型（arm64 / 10 核 / threads=8 / batch=8，`BAAI/bge-reranker-v2-m3`）：

    128 token ≈ 0.18s/对      251 token ≈ 0.53s/对      359 token ≈ 0.55~0.89s/对
    50 对 × 512 token = 44.4s（远超全链路 25s 硬超时）

所以 `max_length` 开放为**每请求可调**：调用方按「这一层值多少秒」自己决定精度，
而不是被服务端的单一默认值绑死。batch=8 实测全面优于 batch=16。
"""

import asyncio
import os
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import torch
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from transformers import AutoModelForSequenceClassification, AutoTokenizer

MODEL_ID = os.getenv("MODEL_ID", "BAAI/bge-reranker-v2-m3")
MAX_LENGTH = int(os.getenv("MAX_LENGTH", "256"))
BATCH_SIZE = int(os.getenv("BATCH_SIZE", "8"))
BUDGET_S = float(os.getenv("RERANK_BUDGET_S", "12"))
DEVICE = os.getenv("DEVICE", "cpu")
NUM_THREADS = int(os.getenv("NUM_THREADS", "0")) or min(8, os.cpu_count() or 2)
PRELOAD = os.getenv("PRELOAD", "true").lower() not in ("0", "false", "no")

torch.set_num_threads(NUM_THREADS)

_state: dict[str, Any] = {
    "loaded": False,
    "loading": False,
    "model": None,
    "tokenizer": None,
    "error": "",
    "warmup_ms": 0,
    "requests": 0,
    "pairs": 0,
}
_lock = asyncio.Semaphore(1)  # CPU 推理串行化

# 长度分桶宽度（字符）。中文约 0.63 token/字符，96 字符 ≈ 60 token 一档
_BUCKET_CHARS = int(os.getenv("BUCKET_CHARS", "96"))


class RerankRequest(BaseModel):
    query: str = Field(description="用户查询")
    documents: list[str] = Field(default_factory=list, description="候选文档文本（已按融合序排列）")
    top_n: int = Field(default=10, ge=1, le=100)
    budget_s: float = Field(default=0.0, ge=0.0, le=60.0, description="0=用服务端默认预算")
    max_length: int = Field(
        default=0,
        ge=0,
        le=512,
        description="token 截断上限，0=用服务端默认；成本随长度约平方增长，按层分别设更划算",
    )


def _load_model() -> None:
    """同步加载 + 预热（在 lifespan 里用 to_thread 调）。"""
    _state["loading"] = True
    t0 = time.monotonic()
    try:
        tokenizer = AutoTokenizer.from_pretrained(MODEL_ID)
        model = AutoModelForSequenceClassification.from_pretrained(MODEL_ID)
        model.to(DEVICE)
        model.eval()
        _state["tokenizer"] = tokenizer
        _state["model"] = model
        # 预热一次：首次前向会触发算子编译/内存池分配，冷启动那一下可能比稳态慢数倍
        tw = time.monotonic()
        _infer([("warmup", "模型预热：忽略这一条")], MAX_LENGTH)
        _state["warmup_ms"] = int((time.monotonic() - tw) * 1000)
        _state["loaded"] = True
        _state["error"] = ""
        print(
            f"[reranker] loaded {MODEL_ID} on {DEVICE} in {int((time.monotonic() - t0) * 1000)}ms "
            f"(threads={NUM_THREADS}, warmup={_state['warmup_ms']}ms)",
            flush=True,
        )
    except Exception as e:  # noqa: BLE001 —— 加载失败要让 /health 如实报错，而不是进程退出
        _state["error"] = f"{type(e).__name__}: {e}"[:300]
        print(f"[reranker] load failed: {_state['error']}", flush=True)
    finally:
        _state["loading"] = False


def _infer(pairs: list[tuple[str, str]], max_length: int) -> list[float]:
    """一批 pair → sigmoid 分数（0~1，与片段阈值同量纲）。"""
    model = _state["model"]
    tokenizer = _state["tokenizer"]
    if model is None or tokenizer is None:
        return []
    with torch.inference_mode():
        inputs = tokenizer(
            [q for q, _ in pairs],
            [d for _, d in pairs],
            truncation=True,
            max_length=max_length,
            padding=True,
            return_tensors="pt",
        ).to(DEVICE)
        logits = model(**inputs).logits.view(-1).float()
        return torch.sigmoid(logits).tolist()


def _scoring_plan(pairs: list[tuple[str, str]]) -> list[int]:
    """给出打分顺序：先按长度分桶，桶间按「桶内最靠前的原序」排。

    两个目标必须同时满足：
    - 同批长度接近 → `padding=True` 只补到本批最长，不让短文档跟着长文档白算
    - RRF 头部先被打分 → 超预算时 partial 牺牲的是长尾，而不是排序靠前的候选

    单纯按长度排序会破坏第二条（长度与相关性无关），所以用「桶内最小索引」做桶序。
    """
    buckets: dict[int, list[int]] = {}
    for i, (_, doc) in enumerate(pairs):
        buckets.setdefault(len(doc) // _BUCKET_CHARS, []).append(i)
    groups = sorted(buckets.values(), key=min)
    return [i for g in groups for i in g]


def _score_all(
    pairs: list[tuple[str, str]], deadline: float, max_length: int
) -> tuple[list[float | None], bool]:
    """分批打分；超预算即停。返回与 `pairs` **等长且保序**的分数表，未打分处为 None。

    保序很关键：打分顺序被 `_scoring_plan` 打乱了，如果直接返回分数列表，
    调用方拿到的 index 就全部错位——那是一种不会报错、只会默默把错的结果排到前面的 bug。
    """
    scores: list[float | None] = [None] * len(pairs)
    plan = _scoring_plan(pairs)
    partial = False
    for start in range(0, len(plan), BATCH_SIZE):
        # 第一批无条件跑：宁可超一点预算也要交出东西，空手而归对上层没任何价值
        if start and time.monotonic() > deadline:
            partial = True
            break
        idxs = plan[start : start + BATCH_SIZE]
        for i, s in zip(idxs, _infer([pairs[i] for i in idxs], max_length), strict=False):
            scores[i] = s
    return scores, partial


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:  # noqa: ARG001
    """模型加载丢给后台任务，**不阻塞 startup**。

    在 lifespan 里 await 加载 = uvicorn 直到加载完才开始 accept：首次要下 2.2GB 模型，
    这几分钟里 `/health` 根本连不上，容器被判 unhealthy、流水线一路降级还查不出原因。
    改成后台任务后 `/health` 立刻能秒回 `status=loading`，进度对上游如实可见。
    """
    task: asyncio.Task[None] | None = None
    if PRELOAD:
        task = asyncio.create_task(asyncio.to_thread(_load_model), name="reranker-preload")
    yield
    if task is not None and not task.done():
        task.cancel()  # 关机时不等模型下完


app = FastAPI(title="websearch-reranker", version="0.1.0", lifespan=lifespan)


@app.get("/health")
async def health() -> dict[str, Any]:
    """秒回（不等推理）：平台/流水线的降级判断全靠它。"""
    return {
        "status": "ok" if _state["loaded"] else ("loading" if _state["loading"] else "unavailable"),
        "model": MODEL_ID,
        "loaded": bool(_state["loaded"]),
        "loading": bool(_state["loading"]),
        "device": DEVICE,
        "threads": NUM_THREADS,
        "max_length": MAX_LENGTH,
        "max_length_cap": 512,
        "batch_size": BATCH_SIZE,
        "bucket_chars": _BUCKET_CHARS,
        "budget_s": BUDGET_S,
        "warmup_ms": _state["warmup_ms"],
        "requests": _state["requests"],
        "pairs_scored": _state["pairs"],
        "error": _state["error"],
    }


@app.post("/rerank")
async def rerank(req: RerankRequest) -> Any:
    """返回类型故意用 Any：未加载时要直接回 Response(503)，不能被 response_model 校验拦下。"""
    if not _state["loaded"]:
        # 真 503（而非 200 里夹个错）：curl 就能看出来，客户端据此走「回退 RRF 序」降级路径
        return JSONResponse(
            {
                "results": [],
                "model": MODEL_ID,
                "loaded": False,
                "loading": bool(_state["loading"]),
                "error": _state["error"] or "model not loaded yet",
            },
            status_code=503,
        )
    # 返回的 index 必须是**调用方原始列表**的下标，不能先过滤再打分：
    # 一旦上游传了空文档，整个排序就会默默平移几位，而且不报任何错
    keep = [i for i, d in enumerate(req.documents) if isinstance(d, str) and d.strip()]
    if not keep or not req.query.strip():
        return {"results": [], "model": MODEL_ID, "loaded": True, "partial": False}

    pairs = [(req.query, req.documents[i]) for i in keep]
    eff_len = max(32, min(512, req.max_length or MAX_LENGTH))
    budget = req.budget_s or BUDGET_S
    deadline = time.monotonic() + budget
    t0 = time.monotonic()
    async with _lock:
        scores, partial = await asyncio.to_thread(_score_all, pairs, deadline, eff_len)
    elapsed = int((time.monotonic() - t0) * 1000)

    ranked = sorted(
        ((keep[p], float(s)) for p, s in enumerate(scores) if s is not None),
        key=lambda x: -x[1],
    )[: max(1, req.top_n)]
    scored = sum(1 for s in scores if s is not None)
    _state["requests"] += 1
    _state["pairs"] += scored
    return {
        "results": [{"index": i, "score": round(s, 4)} for i, s in ranked],
        "model": MODEL_ID,
        "loaded": True,
        "scored": scored,
        "candidates": len(req.documents),
        "partial": partial or scored < len(keep),
        "max_length": eff_len,
        "elapsed_ms": elapsed,
    }
