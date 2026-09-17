"""MCP Server（Streamable HTTP）：平台看到的 3 个工具。

工具面刻意收敛到 3 个——平台 `tool_budget` 默认 8，现有 16 个能力在抢名额，
每多一个工具就多占一个位。三个刚好覆盖「搜 → 深挖 → 自证可用」：

    web_search   主入口：返回已排序去重、带证据片段的文本
    web_fetch    单页深挖：模型想读某一条原文时用
    search_meta  流水线自报：组件可达性/模型是否加载/延迟分位/命中率（排障也靠它）

description 面向**语义命中**撰写：平台 discovery 用 embedding 检索能力，
覆盖「联网搜索 / 网页搜索 / 查资料 / 最新信息 / 新闻 / 官方文档 / 外部资料」等真实表述，
工具才会在该出现的会话里出现。

阶段消融开关只开在 REST（`?stages=recall,fusion`），不进 MCP 工具面。
"""

import json
from dataclasses import dataclass
from typing import Annotated, Any

from mcp.server.fastmcp import FastMCP
from pydantic import Field

from websearch import __version__
from websearch.cache import AsyncStore, Store
from websearch.config import Settings
from websearch.format import format_fetch, format_text
from websearch.pipeline import Pipeline
from websearch.rest import register_rest
from websearch.types import SearchParams

_INSTRUCTIONS = """自建网页搜索服务（五级质量漏斗）：查询理解 → SearXNG 多路召回
→ URL 归一化 + 加权 RRF 融合 → cross-encoder 精排 → 正文抽取 + 片段级二次精排。
返回结果已排序、已去重、带证据片段，可直接引用；末尾 meta 行如实报告各阶段状态与降级。
零按次查询成本，全部本地/自托管组件。"""

_TOOL_SEARCH = (
    "联网搜索网页并返回已排序去重的结果（自建搜索服务，零按次成本）。"
    "适用：查资料、找最新信息/新闻/进展、查官方文档与规范、核实事实、"
    "获取训练数据之外的外部资料、按站点检索。"
    "返回每条结果的标题/URL/站点/日期 + 与问题最相关的正文片段，可直接引用作答。"
    "结果末尾的 meta 行报告流水线状态（rerank/extract/cache/degraded），"
    "若显示不可用请如实告知用户而不是编造。"
)
_TOOL_FETCH = (
    "抓取单个网页的正文并按 question 精选最相关片段（自建抽取，支持 JS 渲染页）。"
    "适用：web_search 命中某条结果后需要读原文细节、核对具体数字/参数/条款、"
    "用户直接给出 URL 要求总结或提取信息。"
)
_TOOL_META = (
    "报告网页搜索流水线自身的健康状况：各组件（SearXNG/精排模型/抓取器）可达性、"
    "重排模型是否已加载、各阶段延迟分位、缓存命中率、无响应引擎清单、当前调参。"
    "适用：搜索结果异常或质量下降时排障，以及回答「搜索服务现在能用吗」。"
)


@dataclass
class Bundle:
    """MCP server + 流水线 + 配置（测试可直接拿 pipeline 打桩）。"""

    mcp: FastMCP
    pipeline: Pipeline
    cfg: Settings


def build(cfg: Settings | None = None, pipeline: Pipeline | None = None) -> Bundle:
    """装配 server：注册 3 个工具 + REST 路由（同一个 Starlette app、同一个 lifespan）。"""
    cfg = cfg or Settings()
    if pipeline is None:
        store = Store(cfg.cache_path)
        store.init()  # 同步建表：进程启动期一次性动作，不进事件循环
        pipeline = Pipeline(cfg, AsyncStore(store, cfg))

    mcp = FastMCP(
        name="websearch",
        instructions=_INSTRUCTIONS,
        streamable_http_path="/mcp",
        host=cfg.host,
        port=cfg.port,
        stateless_http=False,
    )

    @mcp.tool(description=_TOOL_SEARCH)
    async def web_search(
        query: Annotated[str, Field(description="搜索关键词或自然语言问题（中英文均可）")],
        max_results: Annotated[
            int, Field(description="返回结果条数，1-10，默认 8", ge=1, le=10)
        ] = 8,
        extract: Annotated[
            bool,
            Field(
                description=(
                    "是否抓取正文并精选证据片段（默认 true，质量最好但更慢）；"
                    "只要标题+摘要时传 false，延迟可降到 1-3 秒"
                )
            ),
        ] = True,
        freshness: Annotated[
            str,
            Field(
                description=(
                    "时效范围：auto（按查询自动判断，默认）|day（一天内）|week（一周内）"
                    "|month（一月内）|any（不限，缓存更久）"
                )
            ),
        ] = "auto",
        site: Annotated[
            str, Field(description="限定站点域名，如 github.com；留空表示不限")
        ] = "",
        lang: Annotated[
            str, Field(description="结果语言偏好：zh|en|all；留空由查询自动判断")
        ] = "",
        max_chars: Annotated[
            int,
            Field(
                description="输出字符预算，默认 6000，硬上限 12000（超预算按序截断并标记）",
                ge=500,
                le=12000,
            ),
        ] = 6000,
    ) -> str:
        params = SearchParams(
            query=query,
            max_results=max_results,
            extract=extract,
            freshness=freshness,
            site=site,
            lang=lang,
            max_chars=max_chars,
        )
        result = await pipeline.search(params)
        return format_text(result, cfg)

    @mcp.tool(description=_TOOL_FETCH)
    async def web_fetch(
        url: Annotated[str, Field(description="要抓取的网页完整 URL（http/https）")],
        question: Annotated[
            str,
            Field(
                description="想从这页回答的问题；给了就会精选最相关片段，留空则返回正文前段"
            ),
        ] = "",
        max_chars: Annotated[
            int, Field(description="正文字符预算，默认 4000，硬上限 12000", ge=500, le=12000)
        ] = 4000,
    ) -> str:
        data: dict[str, Any] = await pipeline.fetch(url, question, max_chars)
        return format_fetch(data)

    @mcp.tool(description=_TOOL_META)
    async def search_meta() -> str:
        meta = await pipeline.meta()
        meta["tools"] = ["web_search", "web_fetch", "search_meta"]
        return json.dumps(meta, ensure_ascii=False, indent=2)

    register_rest(mcp, pipeline, cfg)
    bundle = Bundle(mcp=mcp, pipeline=pipeline, cfg=cfg)
    mcp.websearch_bundle = bundle  # type: ignore[attr-defined]  # 便于测试与调试取回
    return bundle


def main() -> None:
    """容器入口：`python -m websearch.mcp_server`。"""
    bundle = build()
    cfg = bundle.cfg
    print(  # noqa: T201 —— 容器启动日志，需要直出 stdout
        f"[websearch] v{__version__} MCP+REST on {cfg.host}:{cfg.port} "
        f"(searxng={cfg.searxng_url} reranker={cfg.reranker_url} crawler={cfg.crawler_url} "
        f"extractor={cfg.extractor})",
        flush=True,
    )
    bundle.mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
