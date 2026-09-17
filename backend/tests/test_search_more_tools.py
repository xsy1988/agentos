"""同轮多次 search_more_tools 的增量合并（方案 §5 P1-7）。

缺陷：`run_search_more_tools` 原先返回**整体缓存**，tools 节点用单个
`updated_cache` 变量承接 → 同一轮内两次检索时，后一次把前一次的结果覆盖掉，
模型看到的"已加入可用工具列表"与实际 capability_cache 不一致。

覆盖三层：
1. 契约层 `merge_tools_cache`：按 name 去重、保持既有顺序、不丢其它键；
2. 执行层 `run_search_more_tools`：只回**新增**工具，已存在的工具不重复返回；
3. 集成：tools 节点一次 AI 消息里的两次检索，两次命中都在最终缓存中。
"""

import asyncio
from typing import Any
from uuid import uuid4

import pytest

from app.modules.discovery.assembler import merge_tools_cache, run_search_more_tools
from app.modules.engine.hooks import RunContext


def _item(name: str) -> dict[str, Any]:
    """最小可装配工具条目（与 expand_capability 输出同形）。"""
    return {
        "name": name,
        "schema": {"type": "function", "function": {"name": name, "description": f"{name} 工具"}},
        "kind": "tool",
        "risk_level": "read",
        "source": "builtin",
    }


# ---------- 1. 契约层 ----------


def test_merge_tools_cache_dedupes_by_name_and_keeps_order() -> None:
    """既有顺序保持；同名只保留一次（首次者胜）；其余键原样保留。"""
    cache = {"tools": [_item("a"), _item("b")], "skills": ["s1"]}
    merged = merge_tools_cache(cache, [_item("b"), _item("c"), _item("a"), _item("d")])
    assert [t["name"] for t in merged["tools"]] == ["a", "b", "c", "d"]
    assert merged["skills"] == ["s1"]


def test_merge_tools_cache_tolerates_empty_inputs() -> None:
    """空缓存 / 空增量 / None 都不得抛异常（节点入口 state 可能是空 dict）。"""
    assert merge_tools_cache({}, [])["tools"] == []
    assert [t["name"] for t in merge_tools_cache({}, [_item("x")])["tools"]] == ["x"]
    assert merge_tools_cache({"tools": None}, [_item("x")])["tools"] == [_item("x")]
    assert merge_tools_cache({"tools": [_item("x")]}, [])["tools"] == [_item("x")]


# ---------- 2. 执行层：增量语义 ----------


def _patch_retriever(
    monkeypatch: pytest.MonkeyPatch, mapping: dict[str, list[dict[str, Any]]]
) -> None:
    from app.modules.discovery import assembler as asm_mod
    from app.modules.discovery import retriever as retr_mod

    async def _retrieve(query: str, agent_id: str, **kw: Any) -> dict[str, Any]:
        return {"semantic": list(mapping.get(query) or [])}

    async def _expand(cap: dict[str, Any], source: str) -> list[dict[str, Any]]:
        return [_item(cap["name"])]

    monkeypatch.setattr(retr_mod, "retrieve_capabilities", _retrieve)
    monkeypatch.setattr(asm_mod, "expand_capability", _expand)


def test_run_search_more_tools_returns_increment_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """返回的是增量 `{"new_tools": [...]}`，且已存在的工具不再重复返回。"""
    _patch_retriever(
        monkeypatch,
        {"q1": [{"name": "a", "type": "tool"}], "q2": [{"name": "a", "type": "tool"},
                                                      {"name": "b", "type": "tool"}]},
    )
    cache: dict[str, Any] = {"tools": [_item("seed")]}

    _, d1 = asyncio.run(run_search_more_tools({"query": "q1"}, "a1", cache))
    assert [t["name"] for t in d1["new_tools"]] == ["a"]
    assert "tools" not in d1  # 不再是整体缓存

    cache = merge_tools_cache(cache, d1["new_tools"])
    text, d2 = asyncio.run(run_search_more_tools({"query": "q2"}, "a1", cache))
    assert [t["name"] for t in d2["new_tools"]] == ["b"]  # a 已在缓存，不重复
    assert "a" not in text

    cache = merge_tools_cache(cache, d2["new_tools"])
    assert [t["name"] for t in cache["tools"]] == ["seed", "a", "b"]


def test_run_search_more_tools_reports_when_nothing_new(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """全部命中都已装配 → 增量为空，文本明确"没有新工具"（不谎报已加入）。"""
    _patch_retriever(monkeypatch, {"q": [{"name": "a", "type": "tool"}]})
    cache = {"tools": [_item("a")]}
    text, delta = asyncio.run(run_search_more_tools({"query": "q"}, "a1", cache))
    assert delta == {"new_tools": []}
    assert text == "未检索到新的可用工具。"


# ---------- 3. 集成：tools 节点同轮两次检索 ----------


class _FakeHooks:
    async def on_tool_call(self, ctx: object, req: object) -> None: ...

    async def on_tool_result(self, ctx: object, info: object) -> None: ...


def _tools_node(monkeypatch: pytest.MonkeyPatch) -> tuple[Any, str]:
    from types import SimpleNamespace

    from langgraph.graph.state import StateGraph

    from app.modules.engine import graph as graph_mod

    monkeypatch.setattr(StateGraph, "compile", lambda self, *a, **kw: self)
    ctxs: dict[str, RunContext] = {}

    def _get_run_ctx(run_id: str, *a: object, **kw: object) -> RunContext:
        return ctxs.setdefault(run_id, RunContext(run_id, None, "a1"))

    async def _save(run_id: str, content: str, name: str | None = None) -> str:
        return content  # 短观察：原样进上下文（长输出的产物化在 test_run_artifacts 覆盖）

    rt = SimpleNamespace(
        saver=None,
        hooks=_FakeHooks(),
        _run_ctx=ctxs,
        backend=None,
        get_run_ctx=_get_run_ctx,
        save_long_output=_save,
    )
    return graph_mod.build_graph(rt).nodes["tools"].runnable.afunc, str(uuid4())


def _state(run_id: str, queries: list[str]) -> dict[str, Any]:
    from langchain_core.messages import AIMessage

    meta = {
        "name": "search_more_tools",
        "schema": {"type": "function", "function": {"name": "search_more_tools"}},
        "kind": "meta",
        "risk_level": "read",
        "source": "meta",
    }
    return {
        "messages": [
            AIMessage(
                content="",
                tool_calls=[
                    {"name": "search_more_tools", "args": {"query": q}, "id": f"c{i}"}
                    for i, q in enumerate(queries)
                ],
            )
        ],
        "capability_cache": {"tools": [meta]},
        "budget_state": {},
    }


def _config(run_id: str) -> dict[str, Any]:
    return {"configurable": {"run_id": run_id, "agent_id": "a1"}}


def test_tools_node_accumulates_two_searches_in_same_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同一轮两次检索：两次命中都在最终缓存里（回归 P1-7 覆盖缺陷）。"""
    _patch_retriever(
        monkeypatch,
        {"q1": [{"name": "a", "type": "tool"}], "q2": [{"name": "b", "type": "tool"}]},
    )
    node, run_id = _tools_node(monkeypatch)

    out = asyncio.run(node(_state(run_id, ["q1", "q2"]), _config(run_id)))

    names = [t["name"] for t in out["capability_cache"]["tools"]]
    assert names == ["search_more_tools", "a", "b"]
    for msg in out["messages"]:
        assert "已加入可用工具列表" in msg.content


def test_tools_node_does_not_write_cache_when_nothing_new(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """两次检索都没有新工具 → 不写 capability_cache（避免无谓的检查点差异）。"""
    _patch_retriever(monkeypatch, {"q1": [], "q2": []})
    node, run_id = _tools_node(monkeypatch)

    out = asyncio.run(node(_state(run_id, ["q1", "q2"]), _config(run_id)))

    assert "capability_cache" not in out
    assert all("未检索到新的可用工具。" in m.content for m in out["messages"])
