"""流水线编排：五级漏斗 + 全链路硬超时 + 输出预算 + 指标汇总。

编排纪律（每一条都对应一个真实故障模式）：
1. **阶段结果即时回填** `SearchResult`——硬超时打断时能交出已完成阶段的部分结果，而不是空手；
2. **每层独立降级**——任何一层挂掉都退回上一层结果并如实标记，绝不静默、绝不抛异常；
3. **硬超时 25s** < 平台冒烟单用例 30s ≪ 探活熔断窗口 180s（一次慢搜索不该把能力摘掉）；
4. 缓存只在**全阶段**（无消融）时读写，否则评测的消融对照会被缓存洗成同一份数字。
"""

import asyncio
import time
from typing import Any

import httpx

from websearch import __version__
from websearch.cache import (
    LAST_UNRESPONSIVE,
    RECALL_DIST,
    SEARCH_DEGRADED,
    SEARCH_TOTAL,
    AsyncStore,
    query_key,
    query_ttl,
)
from websearch.config import Settings
from websearch.extract import extract_docs, fetch_page
from websearch.format import clamp_max_chars, clamp_max_results
from websearch.fuse import rerank_shift, rrf_fuse
from websearch.recall import recall
from websearch.rerank import ping as reranker_ping
from websearch.rerank import rerank_docs
from websearch.types import STAGES, Doc, SearchParams, SearchResult, StageTiming
from websearch.understand import build_routes, is_evergreen, is_fresh_intent

_START = time.monotonic()


def _ms(t0: float) -> int:
    return int((time.monotonic() - t0) * 1000)


def _squash(text: str, limit: int) -> str:
    flat = " ".join(str(text or "").split())
    return flat[:limit]


def mark_degraded(result: SearchResult, reason: str) -> None:
    cur = result.flags.get("degraded", "")
    result.flags["degraded"] = f"{cur},{reason}" if cur else reason


def _doc_from_dict(raw: dict[str, Any]) -> Doc:
    return Doc(
        url=str(raw.get("url") or ""),
        norm_url=str(raw.get("norm_url") or raw.get("url") or ""),
        title=str(raw.get("title") or ""),
        snippet=str(raw.get("snippet") or ""),
        date=str(raw.get("date") or ""),
        site=str(raw.get("site") or ""),
        engines=[str(e) for e in (raw.get("engines") or [])],
        routes={str(k): int(v) for k, v in (raw.get("routes") or {}).items()},
        score=float(raw.get("score") or 0.0),
        rerank_score=raw.get("rerank_score"),
        passages=[str(p) for p in (raw.get("passages") or [])],
        passage_source=str(raw.get("passage_source") or "none"),
    )


def _result_from_cache(payload: dict[str, Any], params: SearchParams) -> SearchResult:
    """缓存命中 → 复原 SearchResult（format 是纯函数，重排渲染结果与首次一致）。"""
    raw = payload.get("result") or {}
    result = SearchResult(params=params)
    result.docs = [_doc_from_dict(d) for d in (raw.get("results") or [])]
    result.window = [
        _doc_from_dict({"url": u, "norm_url": u}) for u in (raw.get("window_urls") or [])
    ]
    result.route_names = [str(r) for r in (raw.get("routes") or [])]
    result.route_lists = {str(k): list(v) for k, v in (raw.get("route_lists") or {}).items()}
    result.timings = [
        StageTiming(str(t.get("stage")), int(t.get("ms") or 0), str(t.get("detail") or ""))
        for t in (raw.get("timings") or [])
    ]
    result.flags = {str(k): str(v) for k, v in (raw.get("flags") or {}).items()}
    result.flags["cache"] = "hit"
    # degraded 标记刻意**不抹除**：缓存里存的就是当时降级产出的内容（例如只抽到 2/3 篇），
    # 命中后把它说成完好是对外说谎。重复计数的问题在 `_record` 里解决，不在这里掩盖
    result.errors = [str(e) for e in (raw.get("errors") or [])]
    result.unresponsive_engines = [str(e) for e in (raw.get("unresponsive_engines") or [])]
    result.available = bool(raw.get("available", True))
    result.message = str(raw.get("message") or "")
    result.stats = dict(raw.get("stats") or {})
    result.stats["cached_elapsed_ms"] = int(raw.get("elapsed_ms") or 0)
    return result


class Pipeline:
    """一次搜索的完整编排。cfg/store 注入（便于评测替换与单测）。"""

    def __init__(self, cfg: Settings, store: AsyncStore | None = None):
        self.cfg = cfg
        self.store = store

    # ------------------------------------------------------------------ 主入口
    async def search(self, params: SearchParams) -> SearchResult:
        cfg = self.cfg
        params.max_results = clamp_max_results(params.max_results, cfg)
        params.max_chars = clamp_max_chars(params.max_chars, cfg)
        params.query = params.query.strip()
        result = SearchResult(params=params)
        result.flags["cache"] = "miss"
        if not params.query:
            result.available = False
            result.message = "query 不能为空"
            return result

        fresh = is_fresh_intent(params.query, params.freshness)
        evergreen = is_evergreen(params.query)
        cacheable = tuple(params.stages) == STAGES
        key = query_key(params)
        if cacheable and self.store is not None:
            hit = await self.store.get_query(key)
            if hit:
                cached = _result_from_cache(hit, params)
                cached.elapsed_ms = 0  # 首次耗时留在 stats.cached_elapsed_ms（评测可分辨真假延迟）
                await self._record(cached, fresh=fresh)
                return cached

        t0 = time.monotonic()
        try:
            await asyncio.wait_for(
                self._run(params, result, fresh=fresh, start=t0), timeout=cfg.pipeline_hard_timeout
            )
        except TimeoutError:
            mark_degraded(result, "hard_timeout")
            result.errors.append(
                f"全链路超 {cfg.pipeline_hard_timeout:.0f}s 被截断，以下为已完成阶段的部分结果"
            )
        except Exception as e:  # noqa: BLE001 —— 编排层兜底：绝不让 run 崩在工具调用上
            mark_degraded(result, "pipeline_error")
            result.available = False
            result.message = f"{type(e).__name__}: {e}"[:200]
        result.elapsed_ms = _ms(t0)

        if cacheable and self.store is not None and result.docs and result.available:
            ttl = query_ttl(params, cfg, fresh=fresh, evergreen=evergreen)
            await self.store.put_query(key, {"result": result.to_dict()}, ttl)
        await self._record(result, fresh=fresh)
        return result

    async def _record(self, result: SearchResult, *, fresh: bool) -> None:
        if self.store is None:
            return
        await self.store.bump(SEARCH_TOTAL)
        # 缓存命中不重复计入降级次数：那一次降级已经在首次运行时计过了
        if result.flags.get("degraded") and result.flags.get("cache") != "hit":
            await self.store.bump(SEARCH_DEGRADED)
        if result.flags.get("cache") == "hit":
            return  # 命中不计延迟样本，否则 p50/p95 被 0ms 洗成虚快
        await self.store.add_latency("pipeline", result.elapsed_ms)
        for t in result.timings:
            await self.store.add_latency(t.name, t.elapsed_ms)
        raw = int((result.stats or {}).get("raw") or 0)
        if raw:
            await self.store.bump(RECALL_DIST, raw)
        if result.unresponsive_engines:
            await self.store.set_value(LAST_UNRESPONSIVE, result.unresponsive_engines[:10])

    # ------------------------------------------------------------------ 五级漏斗
    def _budget(self, start: float, cap: float, share: float) -> float:
        """本阶段可用预算 = min(配置上限, 硬超时剩余 × 份额)，并有保底。

        没有这一层：各阶段上限相加（召回 10 + 精排 15 + 抽取 12 = 37s）远超硬超时 25s，
        慢查询会直接被截断成「什么都没做」；按剩余时间分配则总能交出已完成阶段的结果。
        """
        remaining = self.cfg.pipeline_hard_timeout - (time.monotonic() - start)
        return max(self.cfg.min_stage_budget, min(cap, remaining * share))

    async def _run(
        self, params: SearchParams, result: SearchResult, *, fresh: bool, start: float
    ) -> None:
        cfg = self.cfg

        # ---- 1. 查询理解 ----
        t0 = time.monotonic()
        routes = build_routes(
            params.query, freshness=params.freshness, site=params.site, settings=cfg
        )
        if not params.stage_enabled("understand"):
            routes = routes[:1]  # 消融：只跑原查询一路
        result.route_names = [r.name for r in routes]
        result.stats["routes_detail"] = [
            {
                "name": r.name,
                "query": r.query,
                "weight": r.weight,
                "time_range": r.time_range,
                "categories": r.categories,
                "reason": r.reason,
            }
            for r in routes
        ]
        result.timings.append(
            StageTiming("understand", _ms(t0), ",".join(r.name for r in routes) or "none")
        )

        # ---- 2. 召回 ----
        recall_budget = self._budget(start, cfg.recall_total_budget, cfg.recall_budget_share)
        outcome = await recall(routes, params, cfg, budget=recall_budget)
        result.errors.extend(outcome.errors)
        result.unresponsive_engines = outcome.unresponsive_engines
        # 在 early return 之前写：召回全空时这个值是空列表，正是「掐断」最硬的证据
        result.stats["contributing_engines"] = outcome.contributing_engines
        result.timings.append(
            StageTiming(
                "recall",
                outcome.elapsed_ms,
                f"{len(outcome.ok_routes)}/{len(routes)}路有结果 raw={outcome.raw_count} "
                f"budget={recall_budget:.1f}s",
            )
        )
        if not outcome.ok_routes:
            result.available = False
            detail = outcome.errors[0][:100] if outcome.errors else "所有引擎无响应"
            result.message = f"SearXNG 未返回可用结果（{detail}）"
            mark_degraded(result, "recall_empty")
            return
        if len(outcome.ok_routes) < len(routes):
            mark_degraded(result, "partial_routes")

        # ---- 3. 融合（URL 归一化 + 加权 RRF + 近重复合并）----
        t0 = time.monotonic()
        if params.stage_enabled("fusion"):
            fused = rrf_fuse(outcome.results, cfg)
            result.window = fused.window
            result.route_lists = fused.route_lists
            result.route_names = fused.route_names or result.route_names
            result.stats.update(fused.stats)
            detail = f"raw={fused.stats.get('raw')} dedup={fused.stats.get('after_dedup')}"
        else:
            first = outcome.ok_routes[0]
            result.window = first.docs[: max(1, cfg.candidate_window)]
            result.route_lists = {first.route.name: [d.norm_url for d in first.docs]}
            result.stats.update(
                {
                    "raw": outcome.raw_count,
                    "unique_urls": len(first.docs),
                    "after_dedup": len(result.window),
                    "window": len(result.window),
                    "routes": 1,
                    "route_agreement": 0.0,
                    "fusion": "ablated",
                }
            )
            detail = f"消融：只用 {first.route.name} 一路"
        result.timings.append(StageTiming("fusion", _ms(t0), detail))
        docs = list(result.window)
        if not docs:
            result.available = False
            result.message = "融合后无候选（引擎返回的结果全部被归一化/垃圾过滤丢弃）"
            return

        # ---- 4. 重排（cross-encoder，50 → N）----
        if params.stage_enabled("rerank"):
            rerank_budget = self._budget(start, cfg.rerank_timeout, cfg.rerank_budget_share)
            docs, status, elapsed = await rerank_docs(
                params.query, docs, cfg, timeout=rerank_budget
            )
            result.flags["rerank"] = status
            result.stats["rerank_shift"] = rerank_shift(result.window[:10], docs)
            result.stats["rerank_budget_s"] = round(rerank_budget, 2)
            # 低置信丢弃数从窗口反推：被剔掉的候选仍带着自己那份 rerank_score（同一批对象），
            # 所以不必为了一个计数去改 rerank_docs 的返回签名
            kept = {id(d) for d in docs}
            dropped_low = sum(
                1
                for d in result.window
                if d.rerank_score is not None
                and d.rerank_score < cfg.rerank_score_threshold
                and id(d) not in kept
            )
            if dropped_low:
                result.stats["rerank_low_confidence_dropped"] = dropped_low
            result.timings.append(
                StageTiming("rerank", elapsed, f"status={status} budget={rerank_budget:.1f}s")
            )
            if status != "ok":
                mark_degraded(result, f"rerank_{status}")
            if not docs and status == "ok":
                # 精排判定「一条都不相关」。format_text 会输出「未搜到」，这里补上**为何**：
                # 不写的话模型无从判断是该换关键词，还是服务坏了
                result.errors.append(
                    f"召回 {len(result.window)} 条候选，但精排分数全部低于阈值 "
                    f"{cfg.rerank_score_threshold}，判定为无相关结果"
                )
        else:
            docs = docs[: max(1, cfg.rerank_top_n)]
            result.flags["rerank"] = "ablated"
            result.timings.append(StageTiming("rerank", 0, "消融：沿用 RRF 序"))

        docs = docs[: params.max_results]

        # ---- 5. 抽取 + 片段级二次精排 ----
        if params.extract and params.stage_enabled("extract"):
            extract_budget = self._budget(start, cfg.extract_total_budget, cfg.extract_budget_share)
            out = await extract_docs(
                params.query, docs, cfg, self.store, fresh=fresh, budget=extract_budget
            )
            result.flags["extract"] = (
                f"{out.extracted}/{out.attempted}" if out.status != "disabled" else "disabled"
            )
            result.stats.update(
                {
                    "extracted": out.extracted,
                    "extract_attempted": out.attempted,
                    "extract_status": out.status,
                    "extract_sources": out.sources,
                }
            )
            result.errors.extend(out.errors)
            result.timings.append(
                StageTiming("extract", out.elapsed_ms, f"{out.status} budget={extract_budget:.1f}s")
            )
            if out.status in ("snippets_only", "disabled"):
                mark_degraded(result, f"extract_{out.status}")
            elif out.status == "partial":
                mark_degraded(result, "extract_partial")
        else:
            result.flags["extract"] = "skipped"
            result.timings.append(StageTiming("extract", 0, "未启用抽取，片段=引擎摘要"))
            for d in docs:
                if d.snippet and not d.passages:
                    d.passages = [_squash(d.snippet, cfg.passage_chars)]
                    d.passage_source = "snippet"

        result.docs = docs

    # ------------------------------------------------------------------ 单页抓取
    async def fetch(self, url: str, question: str = "", max_chars: int = 0) -> dict[str, Any]:
        cfg = self.cfg
        limit = max_chars if max_chars > 0 else cfg.max_chars_default
        limit = min(limit, cfg.max_chars_hard_cap)
        try:
            return await asyncio.wait_for(
                fetch_page(url, question, limit, cfg, self.store),
                timeout=cfg.pipeline_hard_timeout,
            )
        except TimeoutError:
            return {
                "url": url,
                "available": False,
                "message": f"抓取超 {cfg.pipeline_hard_timeout:.0f}s 被中断",
                "source": "none",
                "passages": [],
            }

    # ------------------------------------------------------------------ 探活与自报
    async def _ping_searxng(self) -> dict[str, Any]:
        t0 = time.monotonic()
        base = self.cfg.searxng_url.rstrip("/")
        async with httpx.AsyncClient(timeout=3.0) as client:
            for path in ("/healthz", "/config"):
                try:
                    resp = await client.get(base + path)
                    if resp.status_code == 200:
                        return {"available": True, "probe": path, "latency_ms": _ms(t0)}
                except httpx.HTTPError as e:
                    return {"available": False, "reason": f"{type(e).__name__}"[:80]}
        return {"available": False, "reason": "no_probe_ok"}

    async def _ping_crawler(self) -> dict[str, Any]:
        t0 = time.monotonic()
        headers = (
            {"Authorization": f"Bearer {self.cfg.crawler_token}"} if self.cfg.crawler_token else {}
        )
        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                base = self.cfg.crawler_url.rstrip("/")
                resp = await client.get(base + "/health", headers=headers)
                if resp.status_code != 200:
                    return {"available": False, "reason": f"HTTP {resp.status_code}"}
                data: dict[str, Any] = dict(resp.json()) if resp.content else {}
                data.update({"available": True, "latency_ms": _ms(t0)})
                return data
        except (httpx.HTTPError, ValueError) as e:
            return {"available": False, "reason": f"{type(e).__name__}"[:80]}

    async def components(self) -> dict[str, Any]:
        """三上游组件可达性（并发探活，单个慢不拖累整体）。"""
        searxng, reranker, crawler = await asyncio.gather(
            self._ping_searxng(), reranker_ping(self.cfg), self._ping_crawler()
        )
        return {"searxng": searxng, "reranker": reranker, "crawler": crawler}

    async def health(self) -> dict[str, Any]:
        """`GET /health`：平台 seed 用它决定能力 enabled；探活必须快（3s 内）。"""
        comps = await self.components()
        ok = bool(comps["searxng"].get("available"))
        payload: dict[str, Any] = {
            "status": "ok" if ok else "degraded",
            "service": "websearch",
            "version": __version__,
            "uptime_s": int(time.monotonic() - _START),
            "components": comps,
            "config": {
                "extractor": self.cfg.extractor,
                "candidate_window": self.cfg.candidate_window,
                "rerank_top_n": self.cfg.rerank_top_n,
                "extract_top_n": self.cfg.extract_top_n,
                "pipeline_hard_timeout": self.cfg.pipeline_hard_timeout,
                "max_chars_default": self.cfg.max_chars_default,
            },
        }
        if not ok:
            payload["message"] = "SearXNG 不可达：搜索会如实降级为不可用，而不是抛异常"
        return payload

    async def meta(self) -> dict[str, Any]:
        """`search_meta` 工具 / `GET /metrics`：流水线自报。"""
        comps = await self.components()
        snap = await self.store.snapshot() if self.store is not None else {}
        degraded: list[str] = []
        if not comps["searxng"].get("available"):
            degraded.append("searxng")
        if not comps["reranker"].get("available"):
            degraded.append("reranker")
        if not comps["crawler"].get("available"):
            degraded.append("crawler")
        return {
            "service": "websearch",
            "version": __version__,
            "available": bool(comps["searxng"].get("available")),
            "components": comps,
            "degraded_components": degraded,
            "reranker_model": (comps.get("reranker") or {}).get("model", ""),
            "reranker_loaded": bool((comps.get("reranker") or {}).get("loaded")),
            "pipeline": {
                "rrf_k": self.cfg.rrf_k,
                "route_weights": self.cfg.weight_map,
                "candidate_window": self.cfg.candidate_window,
                "rerank_top_n": self.cfg.rerank_top_n,
                # 下面这几个是 CPU 上的成本旋钮（成本 ≈ pairs × tokens²），排障必看
                "rerank_candidates": self.cfg.rerank_candidates,
                "rerank_max_length": self.cfg.rerank_max_length,
                "rerank_score_threshold": self.cfg.rerank_score_threshold,
                "extract_top_n": self.cfg.extract_top_n,
                "passage_candidates": self.cfg.passage_candidates,
                "passage_max_length": self.cfg.passage_max_length,
                "passage_score_threshold": self.cfg.passage_score_threshold,
                "extractor": self.cfg.extractor,
                "passages_per_doc": self.cfg.passages_per_doc,
                "chunk_size": self.cfg.chunk_size,
                "chunk_overlap": self.cfg.chunk_overlap,
                "max_chars_default": self.cfg.max_chars_default,
                "max_chars_hard_cap": self.cfg.max_chars_hard_cap,
                "pipeline_hard_timeout": self.cfg.pipeline_hard_timeout,
                "route_llm": self.cfg.route_llm,
            },
            "metrics": snap,
            "last_unresponsive_engines": (snap.get("extra") or {}).get(LAST_UNRESPONSIVE, []),
            "uptime_s": int(time.monotonic() - _START),
        }
