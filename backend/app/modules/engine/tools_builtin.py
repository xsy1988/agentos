"""builtin 占位工具（M2-2c）—— DoD 验证用，M3 由 MCP/HTTP 通道接管真实工具。

capabilities 表登记（type=tool, category=builtin），payload 携带：
- builtin: 注册键（本文件 BUILTIN_TOOLS 的键）
- schema: OpenAI function-calling 格式的工具签名（agent bind_tools 直接用）

执行纪律（模块详细设计 §4 硬边界 2 的 2c 例外）：
builtin 工具在本进程内执行（无副作用演示函数）；真实外部能力仍必须走
capabilities 的 MCP/HTTP 通道，不允许图节点内嵌业务逻辑。

M4 补充：search_knowledge 是知识库的检索面（DB 查询，无副作用），与占位
工具同为进程内执行（模块详细设计 §2.4.2：知识库对引擎只是个工具）。
"""

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any


async def _echo(args: dict[str, Any]) -> str:
    """read 级占位：原样返回输入，验证工具链路。"""
    await asyncio.sleep(0)  # 占位异步点，保持执行器统一 await 语义
    return f"echo: {args.get('text', '')}"


async def _dangerous_demo(args: dict[str, Any]) -> str:
    """dangerous 级占位：演示高危操作确认卡片（无真实副作用）。"""
    await asyncio.sleep(0)
    return (
        f"危险操作已执行（演示，无真实副作用）：{args.get('action', 'unknown')} "
        f"target={args.get('target', '')}"
    )


async def _search_knowledge(args: dict[str, Any]) -> str:
    """知识库语义检索：pgvector 余弦 Top-K + heading_path 拼装（模块详细设计 §2.4.2）。"""
    from app.modules.knowledge.search import format_hits, search_knowledge

    query = str(args.get("query") or "").strip()
    if not query:
        return "参数错误：query 不能为空"
    folders = args.get("folders") or None
    if isinstance(folders, str):
        folders = [folders]
    k = args.get("k") or 8
    hits = await search_knowledge(query, folders, int(k))
    return format_hits(hits)


BUILTIN_TOOLS: dict[str, Callable[[dict[str, Any]], Awaitable[str]]] = {
    "echo": _echo,
    "dangerous_demo": _dangerous_demo,
    "search_knowledge": _search_knowledge,
}


def builtin_tool_schema(name: str, description: str, params: dict[str, Any]) -> dict[str, Any]:
    """构造 OpenAI function-calling 格式的工具签名（bind_tools 直接可用）。"""
    return {
        "type": "function",
        "function": {"name": name, "description": description, "parameters": params},
    }
