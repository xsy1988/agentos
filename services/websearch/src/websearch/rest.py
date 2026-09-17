"""REST 面：`/search` `/fetch` `/health` `/metrics`（与 MCP `/mcp` 同端口同进程）。

为什么不用 FastAPI 独立 app 再 mount：MCP 的 `StreamableHTTPSessionManager` 必须由 Starlette
的 lifespan 拉起，而**被 mount 的子应用的 lifespan 不会被父应用调用** → 会话管理器永不启动、
`/mcp` 直接报错。所以走 FastMCP 的 `custom_route`（同一个 ASGI app、同一个 lifespan），
路由体用 Starlette 原生 `Request`/`JSONResponse`（FastAPI 本就是 Starlette 超集，无需额外依赖）。

REST 独有的能力：**阶段消融**（`stages=recall,fusion`）——调试与评测用，刻意不进 MCP 工具面，
避免污染 Agent 看到的工具签名。
"""

import json
from typing import Any

from mcp.server.fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from websearch import __version__
from websearch.config import Settings
from websearch.format import format_fetch, format_text, to_json
from websearch.pipeline import Pipeline
from websearch.types import STAGES, SearchParams


def parse_stages(raw: Any) -> tuple[str, ...]:
    """`stages=recall,fusion` → 该阶段及其之前的全部阶段。

    前缀闭合是必要的：否则会出现「跳过召回的重排」这种无意义的组合。
    """
    if raw is None:
        return STAGES
    if isinstance(raw, (list, tuple)):
        names = [str(x).strip().lower() for x in raw]
    else:
        names = [p.strip().lower() for p in str(raw).split(",")]
    names = [n for n in names if n in STAGES]
    if not names:
        return STAGES
    last = max(STAGES.index(n) for n in names)
    return STAGES[: last + 1]


def _first(data: dict[str, Any], *keys: str, default: Any = None) -> Any:
    for k in keys:
        if k in data and data[k] is not None:
            return data[k]
    return default


async def _read_body(request: Request) -> dict[str, Any]:
    """GET 用 query string，POST 用 JSON body（也接受 form 的 q=）。"""
    if request.method == "GET":
        return {k: v[-1] for k, v in request.query_params.multi_items()}
    try:
        payload = await request.json()
    except (json.JSONDecodeError, ValueError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    merged = {k: v[-1] for k, v in request.query_params.multi_items()}
    merged.update(payload)
    return merged


def _to_params(data: dict[str, Any], cfg: Settings) -> SearchParams:
    def as_int(val: Any, fallback: int) -> int:
        try:
            return int(val)
        except (TypeError, ValueError):
            return fallback

    def as_bool(val: Any, fallback: bool) -> bool:
        if isinstance(val, bool):
            return val
        if val is None:
            return fallback
        return str(val).strip().lower() in ("1", "true", "yes", "y", "on")

    query = str(_first(data, "query", "q", default="") or "")
    return SearchParams(
        query=query,
        max_results=as_int(_first(data, "max_results"), cfg.max_results_default),
        extract=as_bool(_first(data, "extract"), True),
        freshness=str(_first(data, "freshness", default="auto") or "auto").lower(),
        site=str(_first(data, "site", default="") or ""),
        lang=str(_first(data, "lang", default="") or ""),
        max_chars=as_int(_first(data, "max_chars"), cfg.max_chars_default),
        stages=parse_stages(_first(data, "stages")),
    )


def register_rest(mcp: FastMCP, pipeline: Pipeline, cfg: Settings) -> None:
    """把 REST 路由挂到 FastMCP 的 Starlette app 上（custom_route 不需要鉴权）。"""

    @mcp.custom_route("/", methods=["GET"])
    async def root(request: Request) -> Response:  # noqa: ARG001
        return JSONResponse(
            {
                "service": "websearch",
                "version": __version__,
                "endpoints": {
                    "mcp": "/mcp",
                    "search": "POST|GET /search",
                    "fetch": "POST|GET /fetch",
                    "health": "GET /health",
                    "metrics": "GET /metrics",
                },
                "pipeline": list(STAGES),
                "docs": "docs/自建网页搜索服务.md",
            }
        )

    @mcp.custom_route("/health", methods=["GET"])
    async def health(request: Request) -> Response:  # noqa: ARG001
        return JSONResponse(await pipeline.health())

    @mcp.custom_route("/metrics", methods=["GET"])
    async def metrics(request: Request) -> Response:  # noqa: ARG001
        return JSONResponse(await pipeline.meta())

    @mcp.custom_route("/search", methods=["POST", "GET"])
    async def search(request: Request) -> Response:
        data = await _read_body(request)
        params = _to_params(data, cfg)
        if not params.query:
            return JSONResponse(
                {"available": False, "message": "缺少 query 参数"}, status_code=400
            )
        result = await pipeline.search(params)
        # 先算文本再序列化：`format_text` 会回填 `result.output_chars`（预算截断后的真实长度），
        # 倒过来则 JSON 里的 output_chars 永远是 0
        text = format_text(result, cfg)
        payload = to_json(result, cfg)
        payload["text"] = text
        return JSONResponse(payload)

    @mcp.custom_route("/fetch", methods=["POST", "GET"])
    async def fetch(request: Request) -> Response:
        data = await _read_body(request)
        url = str(_first(data, "url", "u", default="") or "")
        if not url:
            return JSONResponse({"available": False, "message": "缺少 url 参数"}, status_code=400)
        question = str(_first(data, "question", "q", default="") or "")
        max_chars = _first(data, "max_chars", default=0)
        try:
            limit = int(max_chars)
        except (TypeError, ValueError):
            limit = 0
        out = await pipeline.fetch(url, question, limit)
        out["text_format"] = format_fetch(out)
        return JSONResponse(out)
