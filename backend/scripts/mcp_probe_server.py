"""验收用的本地最小 MCP Server（stdio）：只服务 `accept_v16.py -g mcp` 的对账。

为什么不复用产品里的能力：验收要的是**真实协议对端**——真实子进程、真实 `list_tools`
注解、真实 `tools/call` 的 `_meta`、真实 TCP 取件。用一个 60 行的 Server 当第三方，
平台侧一行 stub 都不能有，结论才作数。

三个工具刻意覆盖注解的三条分支（方案 §5 P2-4 的风险映射靠 Server 自己声明）：

- `quote_lookup`  → `readOnlyHint=True`（能力级是 write，只读工具不该被反复高危确认）
- `quote_purge`   → `destructiveHint=True`
- `quote_submit`  → **不带任何注解**（缺省不猜：沿用能力级，绝不按规范默认值推导）

`file_pickup` 是外部服务那一侧：拿派发载荷里的取件模板，经**真实 socket** 拉文件字节
（不是 ASGITransport，也不是把 URL 交回平台代取），口令/凭据都按契约自己带。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

import httpx
from mcp.server.fastmcp import Context, FastMCP
from mcp.types import ToolAnnotations

mcp = FastMCP("accept-v16-probe")


def _agentos_context(ctx: Context) -> dict[str, Any]:
    """取出平台经 `_meta.agentos` 下发的执行上下文（平台不认识的字段不会到这里）。"""
    meta = getattr(ctx.request_context, "meta", None)
    if meta is None:
        return {}
    payload: Any = getattr(meta, "model_extra", None)
    if payload is None:
        payload = meta if isinstance(meta, dict) else {}
    raw = (payload or {}).get("agentos")
    return raw if isinstance(raw, dict) else {}


def _json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def quote_lookup(q: str, ctx: Context) -> str:
    """只读查询：回显收到的 `_meta.agentos`，用来对账平台到底下发了什么。"""
    return _json({"tool": "quote_lookup", "q": q, "context": _agentos_context(ctx)})


@mcp.tool(annotations=ToolAnnotations(destructiveHint=True))
async def quote_purge(target: str) -> str:
    """破坏性操作（注解声明 destructiveHint）。"""
    return _json({"tool": "quote_purge", "target": target})


@mcp.tool()
async def quote_submit(payload: str) -> str:
    """写操作但**不带注解**：风险级必须沿用能力级，平台不得按规范默认值猜。"""
    return _json({"tool": "quote_submit", "payload": payload})


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def file_pickup(
    await_callback: dict[str, Any],
    file_id: str,
    api_key: str,
    token_override: str | None = None,
    drop_token: bool = False,
) -> str:
    """按派发载荷取件：`files_url` 模板替换 `{file_id}`，凭据走 `X-Callback-Token`。

    返回状态码/字节数/摘要，由平台侧断言——**不**把文件内容交回平台，
    这样"取件是否真的成功"只由这条真实 HTTP 往返决定。
    """
    url = str(await_callback.get("files_url") or "").replace("{file_id}", file_id)
    token = token_override
    if token is None and not drop_token:
        token = str(await_callback.get("token") or "")
    headers = {"X-API-Key": api_key}
    if token:
        headers["X-Callback-Token"] = token
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(url, headers=headers)
    return _json(
        {
            "tool": "file_pickup",
            "status": resp.status_code,
            "bytes": len(resp.content),
            "sha256": hashlib.sha256(resp.content).hexdigest(),
            "nosniff": resp.headers.get("x-content-type-options"),
            "content_disposition": resp.headers.get("content-disposition"),
            "detail": _detail(resp),
        }
    )


def _detail(resp: httpx.Response) -> str:
    try:
        body = resp.json()
    except ValueError:
        return ""
    return str(body.get("detail") or "") if isinstance(body, dict) else ""


if __name__ == "__main__":
    mcp.run("stdio")
