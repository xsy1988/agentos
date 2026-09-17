"""评测入口：跑金标集出全指标表 + 消融对照表，并可落 JSON 供 `eval.compare` 做回归门禁。

    uv run python -m eval.run                       # 四种消融全跑（默认）
    uv run python -m eval.run --ablate none         # 只跑全流水线
    uv run python -m eval.run --intent tech,fact    # 只跑部分切片
    uv run python -m eval.run --json eval/reports/baseline.json

## 消融口径

`--ablate X` = **关掉 X 及其之后的所有阶段**（REST 的 `stages` 是前缀闭合的）。
于是四种配置正好构成一条递增的漏斗，相邻两行的差就是那一层的边际贡献：

    fusion   → 只剩单路召回（融合关掉后候选窗口 = 第一路的结果）
    rerank   → 多路召回 + 归一化 + 加权 RRF
    extract  → 再加 cross-encoder 精排
    none     → 再加正文抽取与片段级精排（全流水线）

这直接验证计划里那两条工程判断：「召回决定上限」看 fusion→rerank 的 recall@50 变化，
「重排决定体感」看 rerank→extract 的 nDCG@10/MRR 变化。

## 两个必须绕开的坑

1. **缓存会把指标洗成虚高**。全流水线（`stages == STAGES`）的结果是可缓存的，
   跑第二遍会直接短路：`elapsed_ms` 变 0、结果冻结在上一次。改了流水线代码而缓存没清，
   回归门禁就再也发现不了退化。所以默认每轮开跑前清 `query_cache`（`--reset`）。
   只清 query_cache 不清 url_cache：抽取到的正文与融合/精排的改动无关，
   清掉它只会让每轮重抽上百个页面。
2. **消融配置天然不进缓存**（`cacheable = stages == STAGES`），所以它们的耗时是真耗时；
   而全流水线若命中缓存，首次耗时留在 `stats.cached_elapsed_ms` 里 —— 延迟一律取
   `elapsed_ms or cached_elapsed_ms`，否则缓存命中的 0ms 会把 p50 拉到不真实的低位。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any

import httpx
import yaml

if __package__ in (None, ""):  # 允许 `python eval/run.py` 直跑，不必强制 -m
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from eval.metrics import (  # noqa: E402
    matched_spans,
    mean,
    mrr,
    ndcg_at_k,
    parse_judgments,
    percentile,
    recall_at_k,
    route_agreement,
)
from websearch.cache import Store  # noqa: E402
from websearch.types import STAGES  # noqa: E402

HERE = Path(__file__).resolve().parent
QUERIES_YAML = HERE / "queries.yaml"
DEFAULT_DB = HERE.parent / "data" / "websearch.db"

# 消融名 → 实际下发的 stages（前缀闭合，与 REST 的 parse_stages 同语义）
ABLATIONS: dict[str, tuple[str, ...]] = {
    "fusion": ("understand", "recall"),
    "rerank": ("understand", "recall", "fusion"),
    "extract": ("understand", "recall", "fusion", "rerank"),
    "none": STAGES,
}
# 漏斗顺序（打印对照表用）：从最残缺到最完整
ORDER = ["fusion", "rerank", "extract", "none"]
LABEL = {
    "fusion": "单路召回",
    "rerank": "+多路 RRF 融合",
    "extract": "+cross-encoder 精排",
    "none": "+抽取与片段精排（全流水线）",
}
TOP_K = 10
WINDOW_K = 50


def load_queries(path: Path, intents: set[str] | None, limit: int) -> list[dict[str, Any]]:
    rows = yaml.safe_load(path.read_text())
    if not isinstance(rows, list):
        raise SystemExit(f"{path} 顶层必须是列表")
    out = []
    for row in rows:
        if not isinstance(row, dict) or not row.get("id") or not row.get("query"):
            raise SystemExit(f"{path} 有条目缺 id/query：{row!r}")
        # 标注不合规就当场炸（parse_judgments），不要拖到聚合出一堆 0 分才发现
        row["_judgments"] = parse_judgments(row.get("relevant"))
        row["_spans"] = [str(s) for s in (row.get("answer_spans") or [])]
        row["_volatile"] = bool(row.get("volatile", False))
        if intents and str(row.get("intent", "")) not in intents:
            continue
        out.append(row)
    return out[:limit] if limit > 0 else out


def clear_query_cache(db: Path, mode: str) -> str:
    """清缓存。返回一行人话说明（打不出来会让人以为跑的是新鲜结果）。"""
    if mode == "none":
        return "缓存未清（--reset none）：全流水线那一档可能读到上一轮的冻结结果"
    if not db.exists():
        return f"缓存库不存在（{db}）：跳过清理，若服务在别处则结果可能来自缓存"
    tables = ("query_cache", "url_cache", "metrics") if mode == "all" else ("query_cache",)
    store = Store(db)
    try:
        store.init()
        store.reset(tables)
    finally:
        store.close()
    return f"已清 {'/'.join(tables)}（{db}）"


def _latency(data: dict[str, Any]) -> int:
    """真延迟：命中缓存时 elapsed_ms=0，首次耗时在 stats.cached_elapsed_ms。"""
    ms = int(data.get("elapsed_ms") or 0)
    if ms:
        return ms
    return int((data.get("stats") or {}).get("cached_elapsed_ms") or 0)


def score_one(row: dict[str, Any], data: dict[str, Any]) -> dict[str, Any]:
    """单条 query 的原始响应 → 一行指标。"""
    results = data.get("results") or []
    top_urls = [str(d.get("norm_url") or "") for d in results][:TOP_K]
    window_urls = [str(u) for u in (data.get("window_urls") or [])]
    judgments = row["_judgments"]
    spans = row["_spans"]
    route_lists = data.get("route_lists") or {}
    single_route = len(route_lists) < 2

    passages = [p for d in results for p in (d.get("passages") or [])]
    hits = matched_spans(passages, spans) if spans else []
    stats = data.get("stats") or {}
    # 真正在出数的引擎：优先用流水线 stats 里的召回层并集（不受精排/抽取影响，
    # 召回全空时也有值），回退到 top10 的 engines 字段。这是**效果侧**信号，
    # 比「几个引擎报无响应」靠得住——剩下那个引擎可能活着但只在出垃圾。
    engines = [str(e) for e in (stats.get("contributing_engines") or [])] or sorted(
        {e for d in results[:TOP_K] for e in (d.get("engines") or [])}
    )
    return {
        "id": row["id"],
        "intent": row.get("intent", ""),
        "volatile": row["_volatile"],
        "available": bool(data.get("available")),
        "recall": recall_at_k(window_urls, judgments, WINDOW_K),
        "ndcg": ndcg_at_k(top_urls, judgments, TOP_K),
        "mrr": mrr(top_urls, judgments),
        # 单路时 route_agreement 按定义恒为 0，那不是质量下降 → 记 None，聚合时排除
        "agreement": None if single_route else route_agreement(top_urls, route_lists),
        "span_hit": (bool(hits) if spans else None),
        "spans_matched": hits,
        "window": len(window_urls),
        "routes": len(route_lists),
        "results": len(top_urls),
        "elapsed_ms": _latency(data),
        "chars": int(data.get("output_chars") or 0),
        "rerank_shift": stats.get("rerank_shift"),
        "dropped_low": stats.get("rerank_low_confidence_dropped", 0),
        "flags": data.get("flags") or {},
        "unresponsive": data.get("unresponsive_engines") or [],
        "engines": engines,
        "errors": data.get("errors") or [],
        "top_urls": top_urls[:5],
    }


def aggregate(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """一组 query 的指标 → 汇总。span_hit 的分母只含**有 answer_spans** 的条目。"""
    lat = [r["elapsed_ms"] for r in rows if r["elapsed_ms"]]
    with_spans = [r for r in rows if r["span_hit"] is not None]
    agree = [r["agreement"] for r in rows if r["agreement"] is not None]
    return {
        "n": len(rows),
        "recall_at_50": mean([r["recall"] for r in rows]),
        "ndcg_at_10": mean([r["ndcg"] for r in rows]),
        "mrr": mean([r["mrr"] for r in rows]),
        "route_agreement": mean(agree) if agree else None,
        "span_hit_rate": mean([1.0 if r["span_hit"] else 0.0 for r in with_spans])
        if with_spans
        else None,
        "span_queries": len(with_spans),
        "available_rate": mean([1.0 if r["available"] else 0.0 for r in rows]),
        "p50_ms": percentile(lat, 50),
        "p95_ms": percentile(lat, 95),
        "avg_window": mean([float(r["window"]) for r in rows]),
        "avg_results": mean([float(r["results"]) for r in rows]),
        "avg_chars": mean([float(r["chars"]) for r in rows]),
        "degraded": sum(1 for r in rows if r["flags"].get("degraded")),
        "cost_per_query": 0.0,  # 自托管、全本地 CPU：按次查询成本恒为 0
    }


def summarize(name: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    stable = [r for r in rows if not r["volatile"]]
    by_intent: dict[str, Any] = {}
    for intent in sorted({r["intent"] for r in rows}):
        group = [r for r in rows if r["intent"] == intent]
        by_intent[intent] = aggregate(group)
    return {
        "ablate": name,
        "stages": list(ABLATIONS[name]),
        "overall": aggregate(rows),
        # 门禁口径：只算非时效条目（时效查询结果天天变，拿它卡阈值等于天天误报）
        "stable": aggregate(stable),
        "by_intent": by_intent,
        "queries": rows,
    }


async def fetch_one(
    client: httpx.AsyncClient,
    sem: asyncio.Semaphore,
    base_url: str,
    row: dict[str, Any],
    stages: tuple[str, ...],
    timeout: float,
) -> dict[str, Any]:
    body = {
        "query": row["query"],
        "max_results": TOP_K,
        "extract": True,  # 是否真抽取由 stages 决定（pipeline 里两个条件都要满足）
        "freshness": row.get("freshness", "auto"),
        "max_chars": 8000,
        "stages": list(stages),
    }
    async with sem:
        t0 = time.monotonic()
        try:
            resp = await client.post(f"{base_url}/search", json=body, timeout=timeout)
            data = resp.json()
        except Exception as e:  # noqa: BLE001 —— 评测要的是「跑完并如实报告」，不是崩掉
            wall = int((time.monotonic() - t0) * 1000)
            data = {
                "available": False,
                "errors": [f"{type(e).__name__}: {e}"],
                "elapsed_ms": wall,
                "results": [],
                "window_urls": [],
                "route_lists": {},
            }
    scored = score_one(row, data)
    print(f"  [{row['id']}] ndcg={scored['ndcg']:.3f} recall={scored['recall']:.0f} "
          f"{scored['elapsed_ms']}ms window={scored['window']}", flush=True)
    return scored


async def run_config(
    base_url: str, name: str, queries: list[dict[str, Any]], concurrency: int, timeout: float
) -> dict[str, Any]:
    stages = ABLATIONS[name]
    print(f"\n=== 消融 {name}（{LABEL[name]}）stages={','.join(stages)} ===", flush=True)
    sem = asyncio.Semaphore(concurrency)
    limits = httpx.Limits(max_connections=concurrency + 2)
    async with httpx.AsyncClient(limits=limits) as client:
        rows = await asyncio.gather(
            *(fetch_one(client, sem, base_url, q, stages, timeout) for q in queries)
        )
    return summarize(name, list(rows))


async def probe_cache(
    base_url: str, queries: list[dict[str, Any]], timeout: float
) -> dict[str, Any]:
    """缓存层单独量：同一查询连打两次，第二次必须短路。

    为什么不从主跑分里读命中率：主跑分前刚清了 query_cache，命中率按构造就是 0，
    量不出任何东西。缓存是漏斗的第五层，得有自己的探针。
    """
    picks = [q for q in queries if not q["_volatile"]][:3]
    if not picks:
        return {"probed": 0}
    body_of = lambda q: {  # noqa: E731 —— 三行 lambda 比一个只被这里用的函数清楚
        "query": q["query"],
        "max_results": 3,
        "extract": False,
        "freshness": q.get("freshness", "auto"),
        "max_chars": 800,
    }
    hits = 0
    cold_ms: list[int] = []
    warm_ms: list[int] = []
    async with httpx.AsyncClient() as client:
        for q in picks:
            body = body_of(q)
            first = (await client.post(f"{base_url}/search", json=body, timeout=timeout)).json()
            second = (await client.post(f"{base_url}/search", json=body, timeout=timeout)).json()
            cold_ms.append(_latency(first))
            warm_ms.append(int(second.get("elapsed_ms") or 0))
            if (second.get("flags") or {}).get("cache") == "hit":
                hits += 1
    return {
        "probed": len(picks),
        "hit_rate": round(hits / len(picks), 4),
        "cold_p50_ms": percentile(cold_ms, 50),
        "warm_p50_ms": percentile(warm_ms, 50),
        "speedup": round(percentile(cold_ms, 50) / max(1, percentile(warm_ms, 50)), 1),
    }


def upstream_health(configs: dict[str, Any]) -> dict[str, Any]:
    """本轮上游到底健康不健康——它直接决定这批数字能不能当基线。

    实测教训：一轮评测里多个引擎被 SearXNG 集体挂起（Suspended: too many requests /
    CAPTCHA），只剩一两路在出与查询无关的页面，于是 nDCG@10 直接归零。那不是流水线退化，
    是召回被掐断——但如果不把它标出来，这批零分会被当成基线写进仓库，
    之后每次真实跑分都「大幅优于基线」，门禁从此失效。

    **判据只有一个：真正在出数的引擎 ≤ 2 → 降级**（效果侧）。
    为什么不用「无响应引擎 ≥ N」当触发条件：那是间接信号，而且在本项目把 google/ddg/
    brave/mojeek 这些常年挂起的引擎 disabled 掉之后反而容易假阳：跑一大轮时
    baidu/quark/sogou/360search 被验证码轮流挂起很常见，但只要 naver/yandex/yahoo/yep
    还在出数（contributing ≥ 3），召回基底就是好的，不该把这轮当成无效。
    无响应清单仍然采集并打印（信息），但不再单独把一批合法基线判死。
    """
    unresponsive: set[str] = set()
    contributing: set[str] = set()
    for cfg in configs.values():
        for q in cfg["queries"]:
            unresponsive.update(q.get("unresponsive") or [])
            contributing.update(q.get("engines") or [])
    reasons = []
    if len(contributing) <= 2:
        names = ",".join(sorted(contributing)) or "无"
        reasons.append(f"只有 {len(contributing)} 个引擎真正在出数：{names}")
        if unresponsive:
            more = ",".join(sorted(unresponsive))
            reasons.append(f"（另有 {len(unresponsive)} 个引擎无响应：{more}）")
    return {
        "degraded": bool(reasons),
        "reasons": reasons,
        "unresponsive_engines": sorted(unresponsive),
        "contributing_engines": sorted(contributing),
    }


def _fmt(v: Any, spec: str = ".3f", na: str = "n/a") -> str:
    if v is None:
        return na
    return format(v, spec) if isinstance(v, float) else str(v)


def print_metrics_table(configs: dict[str, Any]) -> None:
    print("\n" + "=" * 92)
    print("全指标表（消融对照：相邻两行之差 = 那一层的边际贡献）")
    print("=" * 92)
    head = f"{'阶段':<26}{'recall@50':>10}{'nDCG@10':>9}{'MRR':>7}{'多路一致':>9}" \
           f"{'片段命中':>9}{'p50ms':>7}{'p95ms':>7}{'窗口':>6}{'字符':>6}"
    print(head)
    print("-" * 92)
    for name in ORDER:
        if name not in configs:
            continue
        m = configs[name]["stable"]
        label = LABEL[name]
        # 中文宽度对齐：按显示宽度补空格（len() 会把汉字算成 1）
        pad = max(0, 26 - sum(2 if ord(c) > 0x2E80 else 1 for c in label))
        print(
            f"{label}{' ' * pad}"
            f"{_fmt(m['recall_at_50']):>10}{_fmt(m['ndcg_at_10']):>9}{_fmt(m['mrr']):>7}"
            f"{_fmt(m['route_agreement']):>9}{_fmt(m['span_hit_rate']):>9}"
            f"{m['p50_ms']:>7}{m['p95_ms']:>7}{_fmt(m['avg_window'], '.1f'):>6}"
            f"{_fmt(m['avg_chars'], '.0f'):>6}"
        )
    print("-" * 92)
    print("口径：recall@50=窗口内存在已标注相关项的 query 占比；nDCG/MRR 只算 top10；")
    print("      多路一致 = top10 中出现在 ≥2 路的占比（单路档按定义为 n/a，不是 0）；")
    print("      片段命中 = 输出片段含 answer_spans 的占比（分母只含标了答案串的条目）；")
    print("      本表只统计**非时效**条目（门禁口径）；含时效的完整数据在 JSON 里。")


def print_intent_table(configs: dict[str, Any], name: str) -> None:
    cfg = configs.get(name)
    if not cfg:
        return
    print(f"\n按意图切片（消融={name}）")
    print(f"{'intent':<10}{'n':>4}{'recall@50':>11}{'nDCG@10':>9}{'MRR':>7}{'片段命中':>10}{'p95ms':>8}")
    for intent, m in cfg["by_intent"].items():
        print(
            f"{intent:<10}{m['n']:>4}{_fmt(m['recall_at_50']):>11}{_fmt(m['ndcg_at_10']):>9}"
            f"{_fmt(m['mrr']):>7}{_fmt(m['span_hit_rate']):>10}{m['p95_ms']:>8}"
        )


def print_worst(cfg: dict[str, Any], n: int = 6) -> None:
    """把最差的几条打出来：平均分掩盖的从来都是这几条。"""
    rows = sorted(cfg["queries"], key=lambda r: (r["ndcg"], r["recall"]))[:n]
    print(f"\n最差的 {len(rows)} 条（消融={cfg['ablate']}）")
    for r in rows:
        why = []
        if not r["available"]:
            why.append("上游不可用")
        if r["recall"] == 0.0:
            why.append(f"窗口 {r['window']} 条里没有已标注相关项")
        elif r["ndcg"] < 0.3:
            why.append("相关项进了窗口但排在 10 名开外")
        if r["span_hit"] is False:
            why.append("片段里没找到答案串")
        if r["flags"].get("degraded"):
            why.append(f"降级={r['flags']['degraded']}")
        print(f"  {r['id']:<10} ndcg={r['ndcg']:.3f} mrr={r['mrr']:.2f} "
              f"recall={r['recall']:.0f} window={r['window']:<3} {'；'.join(why) or '—'}")


async def main() -> int:
    ap = argparse.ArgumentParser(description="自建网页搜索服务评测")
    ap.add_argument("--ablate", default="all",
                    help="all（默认，四档全跑）或 none|fusion|rerank|extract 之一/逗号列表")
    ap.add_argument("--base-url", default="http://localhost:8204",
                    help="search-api 的 REST 根地址（默认 8204，见 docker-compose 端口说明）")
    ap.add_argument("--queries", default=str(QUERIES_YAML))
    ap.add_argument("--intent", default="", help="只跑这些意图切片，逗号分隔")
    ap.add_argument("--limit", type=int, default=0, help="只跑前 N 条（快速冒烟用）")
    ap.add_argument("--concurrency", type=int, default=3,
                    help="并发查询数。别调太高：精排在 CPU 上串行，并发只会互相拖慢")
    ap.add_argument("--timeout", type=float, default=90.0, help="单条查询的 HTTP 超时")
    ap.add_argument("--reset", default="query", choices=["query", "all", "none"],
                    help="开跑前清哪些缓存表（默认只清 query_cache，理由见模块 docstring）")
    ap.add_argument("--db", default=str(DEFAULT_DB), help="缓存库路径（bind mount 到宿主的那份）")
    ap.add_argument("--json", default="", help="把报告写成 JSON（compare.py 的输入）")
    ap.add_argument("--no-cache-probe", action="store_true", help="跳过缓存层探针")
    args = ap.parse_args()

    if args.ablate == "all":
        names = list(ORDER)
    else:
        names = [n.strip() for n in args.ablate.split(",") if n.strip()]
        bad = [n for n in names if n not in ABLATIONS]
        if bad:
            raise SystemExit(f"未知的消融档：{bad}（可选 all/none/fusion/rerank/extract）")
        names = [n for n in ORDER if n in names]  # 按漏斗顺序跑，缓存/日志才好读

    intents = {s.strip() for s in args.intent.split(",") if s.strip()} or None
    queries = load_queries(Path(args.queries), intents, args.limit)
    if not queries:
        raise SystemExit("没有匹配的查询条目")

    async with httpx.AsyncClient() as client:
        try:
            health = (await client.get(f"{args.base_url}/health", timeout=10.0)).json()
        except Exception as e:  # noqa: BLE001
            raise SystemExit(
                f"连不上 {args.base_url}/health：{e}\n"
                "先起栈：docker compose --profile search up -d（或 bin/agentos search up）"
            ) from e
    comps = health.get("components") or {}
    print(f"服务 {args.base_url} status={health.get('status')} "
          + " ".join(f"{k}={'ok' if v.get('available') else 'DOWN'}" for k, v in comps.items()))
    print(f"金标集 {len(queries)} 条（意图：{','.join(sorted({q['intent'] for q in queries}))}）")
    print(clear_query_cache(Path(args.db), args.reset))

    configs: dict[str, Any] = {}
    for name in names:
        configs[name] = await run_config(
            args.base_url, name, queries, args.concurrency, args.timeout
        )

    cache_probe = {} if args.no_cache_probe else await probe_cache(args.base_url, queries, 30.0)
    async with httpx.AsyncClient() as client:
        server_metrics = (await client.get(f"{args.base_url}/metrics", timeout=10.0)).json()

    print_metrics_table(configs)
    print_intent_table(configs, "none" if "none" in configs else names[-1])
    print_worst(configs["none"] if "none" in configs else configs[names[-1]])

    if cache_probe:
        print(f"\n缓存层探针：{cache_probe['probed']} 条查询各打两次 → "
              f"命中率={cache_probe['hit_rate']:.0%}，"
              f"冷 p50={cache_probe['cold_p50_ms']}ms / 热 p50={cache_probe['warm_p50_ms']}ms"
              f"（{cache_probe['speedup']}×）")

    upstream = upstream_health(configs)
    print(f"\n上游引擎：无响应 {len(upstream['unresponsive_engines'])} 个，"
          f"真正在出数 {len(upstream['contributing_engines'])} 个"
          f"（{','.join(upstream['contributing_engines']) or '无'}）")
    if upstream["degraded"]:
        # 必须吼出来：降级窗口里的指标全是噪声，把它当基线写进仓库会让门禁永久失效
        print("\n" + "!" * 92)
        print("⚠ 上游被掐断，本轮指标不可用作基线：")
        for reason in upstream["reasons"]:
            print(f"    - {reason}")
        print("  此时 recall/nDCG 量的是「只剩一两个引擎时的世界」，不是流水线的质量。")
        print("  处理：等引擎退避期过去（通常 1~10 分钟）后重跑；降并发也能少触发限流。")
        print("  报告仍会写出（便于事后复盘），但已标 upstream.degraded=true，"
              "compare.py 会拒绝把它提升为基线。")
        print("!" * 92)

    report = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "base_url": args.base_url,
        "queries_file": str(Path(args.queries).resolve()),
        "query_count": len(queries),
        "health_components": {k: bool(v.get("available")) for k, v in comps.items()},
        "upstream": upstream,
        "cache_probe": cache_probe,
        "server_metrics": server_metrics,
        "configs": configs,
    }
    if args.json:
        out = Path(args.json)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, ensure_ascii=False, indent=1))
        tag = "（上游降级，不能当基线）" if upstream["degraded"] else ""
        print(f"\n报告已写入 {out}（{out.stat().st_size // 1024} KB）{tag}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
