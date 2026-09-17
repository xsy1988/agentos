#!/usr/bin/env python3
"""逐引擎探针：把「哪些搜索引擎在这个网络环境下真的能用」变成一张表。

## 为什么需要它

`unresponsive_engines` 只告诉你「谁报错了」，不告诉你「谁在出垃圾」。实测最坑的引擎
恰恰不报错：它稳定返回 10 条结果，只是内容与查询无关（本机 bing 对中文查询返回
CRAN 下载页、cn.ubuntu.com 首页）。这种「活着的垃圾源」比死掉的引擎更坏——它占 rank 1、
拿 RRF 最高票，把候选窗口的名额挤给无关页，只能靠精排一层层筛掉。

所以判据是**相关性**而不是「有没有返回」：top5 里有几条提到查询关键词。

## 用法

    uv run python scripts/probe_engines.py                    # 探默认清单（3 条代表性查询）
    uv run python scripts/probe_engines.py --category news    # 探新闻类目
    uv run python scripts/probe_engines.py --list             # 列出 SearXNG 认识的全部引擎名
    uv run python scripts/probe_engines.py --engines sogou,yep --query "向量数据库 是什么"

## 一个必须知道的口径限制

探针只发 `engines=`、**不发 `categories=`**：实测两者同时发时 SearXNG 以 categories 为准、
engines 被忽略（`recall.py` 里也是因此二选一）。这意味着探的是「这个引擎本身通不通、
出的数对不对」，不是「它在某个 category 下的表现」。news 类目要按 `--category news` 探。
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.parse
import urllib.request
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

DEFAULT_BASE = "http://127.0.0.1:8201"

# 代表性查询 + 判相关的关键词。三条覆盖三种意图（中文技术 / 中文概念 / 英文实体），
# 只测一条很容易得出偏的结论——bing 就是「英文查询正常、中文查询出垃圾」。
CASES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("PostgreSQL VACUUM 回收空间", ("postgresql", "vacuum", "postgres")),
    ("向量数据库 是什么", ("向量", "vector", "数据库")),
    ("bge-reranker-v2-m3 max length", ("reranker", "bge", "baai")),
)
NEWS_CASE: tuple[str, tuple[str, ...]] = ("OpenAI 最新发布", ("openai", "gpt", "模型"))

# 默认探测清单：本仓库 settings.yml 里出现过的引擎 + 几个常见的备选
GENERAL_ENGINES: tuple[str, ...] = (
    "sogou", "360search", "naver", "yandex", "yahoo", "yep", "mwmbl", "baidu", "quark",
    "wikipedia", "hackernews", "bing", "google", "duckduckgo", "brave", "mojeek",
    "seznam", "wiby", "tusksearch",
)
NEWS_ENGINES: tuple[str, ...] = (
    "bing news", "naver news", "yahoo news", "sogou wechat", "reuters", "wikinews",
    "google news", "duckduckgo news", "mojeek news", "brave.news",
)


def _get(url: str, timeout: float) -> dict[str, Any]:
    with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310 - 本地固定 http
        data: dict[str, Any] = json.load(resp)
        return data


@dataclass
class Probe:
    """一个引擎在一条查询上的表现。`hits` 是判据，`n` 只是参考。"""

    engine: str
    n: int = 0
    hits: int = 0
    status: str = ""
    engines: list[str] = field(default_factory=list)
    titles: list[str] = field(default_factory=list)


def probe(base: str, engine: str, query: str, keywords: Sequence[str], timeout: float) -> Probe:
    """单发一个引擎（只给 engines=，不给 categories=）。"""
    qs = urllib.parse.urlencode({"q": query, "format": "json", "pageno": 1, "engines": engine})
    try:
        data = _get(f"{base}/search?{qs}", timeout)
    except Exception as e:  # noqa: BLE001 - 探针要把任何失败都如实记下来
        return Probe(engine=engine, status=f"ERR {type(e).__name__}: {str(e)[:60]}")
    results = [r for r in (data.get("results") or []) if isinstance(r, dict)]
    unresp = data.get("unresponsive_engines") or []
    hits = 0
    for item in results[:5]:
        blob = " ".join(str(item.get(k) or "") for k in ("title", "url", "content")).lower()
        if any(kw and kw.lower() in blob for kw in keywords):
            hits += 1
    counts = Counter(str(e) for r in results for e in (r.get("engines") or []))
    return Probe(
        engine=engine,
        n=len(results),
        hits=hits,
        status="ok" if results else (f"EMPTY {unresp}" if unresp else "EMPTY"),
        engines=sorted(counts),
        titles=[str(r.get("title") or "")[:50] for r in results[:3]],
    )


def list_engines(base: str, timeout: float) -> int:
    """打印 SearXNG 认识的全部引擎（按 category 分组），供挑清单用。"""
    cfg = _get(f"{base}/config?format=json", timeout)
    engines = [e for e in (cfg.get("engines") or []) if isinstance(e, dict)]
    print(f"SearXNG 认识 {len(engines)} 个引擎")
    for cat in ("general", "news", "it", "science"):
        names = sorted(str(e.get("name")) for e in engines if cat in (e.get("categories") or []))
        print(f"\n[{cat}] {len(names)} 个:\n  " + ", ".join(names))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="逐引擎探测 SearXNG 的可用性与相关性")
    ap.add_argument("--base-url", default=DEFAULT_BASE, help="SearXNG 地址（默认 %(default)s）")
    ap.add_argument("--engines", default="", help="逗号分隔的引擎名；默认用内置清单")
    ap.add_argument("--query", default="", help="只测这一条查询（默认用内置的 3 条）")
    ap.add_argument("--category", choices=("general", "news"), default="general")
    ap.add_argument("--timeout", type=float, default=25.0)
    ap.add_argument("--list", action="store_true", help="列出 SearXNG 全部引擎名后退出")
    args = ap.parse_args(argv)
    base = args.base_url.rstrip("/")

    if args.list:
        return list_engines(base, args.timeout)

    builtin = NEWS_ENGINES if args.category == "news" else GENERAL_ENGINES
    engines = tuple(e.strip() for e in args.engines.split(",") if e.strip()) or builtin
    if args.query:
        # 自定义查询时没关键词可判，只能报「有没有出数」；相关得自己看标题
        cases: tuple[tuple[str, tuple[str, ...]], ...] = ((args.query, ()),)
    else:
        cases = (NEWS_CASE,) if args.category == "news" else CASES

    usable: list[str] = []
    garbage: list[str] = []
    dead: list[str] = []
    for query, keywords in cases:
        head = f"查询: {query}    判相关的关键词: {keywords or '（未给，请看标题自行判断）'}"
        print(f"\n{'=' * 76}\n{head}\n{'=' * 76}")
        for eng in engines:
            got = probe(base, eng, query, keywords, args.timeout)
            mark = "★" if got.hits >= 2 else ("·" if got.hits >= 1 else "✗")
            print(f"{mark} {eng:<16} n={got.n:<3} 相关={got.hits}  {got.status}")
            for t in got.titles:
                print(f"      {t}")
            if not keywords:
                continue  # 没给关键词就没有相关性判据，不能拿它分类
            if got.hits >= 2:
                if eng not in usable:
                    usable.append(eng)
            elif got.n > 0:
                if eng not in garbage:
                    garbage.append(eng)
            elif eng not in dead:
                dead.append(eng)

    print(f"\n{'-' * 76}\n结论（★ 至少一条查询里 top5 有 ≥2 条相关）")
    print(f"  建议启用  ({len(usable)}): {', '.join(usable) or '无'}")
    print(f"  出数但不相关 ({len(garbage)}): {', '.join(garbage) or '无'}")
    print("      ↑ 这类要关：活着的垃圾源比死掉的引擎更坏（它占 rank 1、拿 RRF 最高票）")
    print(f"  不可用    ({len(dead)}): {', '.join(dead) or '无'}")
    print("把「建议启用」写进 searxng/settings.yml（disabled: false），其余 disabled: true。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
