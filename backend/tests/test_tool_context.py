"""P1-10 工具执行上下文：签名适配、幂等键口径、两条通道的下发与节点级注入。

覆盖：
1. `accepts_context` 判定（老签名 / 新签名 / 关键字-only / `*args` / 不可自省对象）；
2. `tool_idempotency_key` 确定性、参数键序无关、run 间隔离；
3. `call_builtin` 对新老签名分别注入/不注入、注册键缺失抛 `ToolError`、工具内部
   `TypeError` **不被**适配层吞掉（这是拒绝 try/except TypeError 方案的回归护栏）；
4. tools 节点级：ctx-aware builtin 拿到 `run_id` / `task_id` / `step_id` / 幂等键；
5. mcp 通道：上下文走 `tools/call` 的 `_meta`，不进 args；
6. 外部等待派发（P0-4）：回调凭据经上下文下发，历史 args 通道保留。
"""

import asyncio
from datetime import UTC, datetime, timedelta
from functools import partial
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from app.modules.engine.tool_context import (
    CONTEXT_META_KEY,
    ToolContext,
    accepts_context,
    tool_idempotency_key,
)
from app.modules.engine.tools_builtin import ToolError, call_builtin

# ---------- 1. 签名自省 ----------


async def _legacy(args: dict[str, Any]) -> str:
    return "legacy"


async def _modern(args: dict[str, Any], ctx: ToolContext) -> str:
    return f"{ctx.run_id}"


async def _keyword_only(args: dict[str, Any], *, ctx: ToolContext) -> str:
    return "keyword-only"


async def _var_positional(*args: Any) -> str:
    return "varargs"


def test_accepts_context_signatures() -> None:
    assert accepts_context(_legacy) is False
    assert accepts_context(_modern) is True
    # 关键字-only 的 ctx 不是"第 2 个位置参数"，注入会失败 → 按老签名处理
    assert accepts_context(_keyword_only) is False
    assert accepts_context(_var_positional) is False
    # 不可自省的可调用对象/非可调用对象：按老签名处理，不抛错
    assert accepts_context(object()) is False
    # partial 绑掉第一个参数后只剩 ctx → 不再视为需要注入（避免重复注入）
    assert accepts_context(partial(_modern, {})) is False


# ---------- 2. 幂等键口径 ----------


def test_idempotency_key_is_deterministic_and_order_insensitive() -> None:
    run = "11111111-1111-1111-1111-111111111111"
    k1 = tool_idempotency_key(run, "web_search", {"q": "abc", "n": 3})
    k2 = tool_idempotency_key(run, "web_search", {"n": 3, "q": "abc"})
    assert k1 == k2, "参数键序不应影响幂等键"
    assert k1.startswith(f"{run}:")
    assert len(k1.split(":", 1)[1]) == 32
    # 工具名 / 参数 / run 任一变化 → 键变化（不同调用不互撞）
    assert tool_idempotency_key(run, "web_other", {"q": "abc", "n": 3}) != k1
    assert tool_idempotency_key(run, "web_search", {"q": "abd", "n": 3}) != k1
    assert tool_idempotency_key("22222222-2222-2222-2222-222222222222", "web_search", {}) != (
        tool_idempotency_key(run, "web_search", {})
    )


def test_idempotency_key_tolerates_unserializable_args() -> None:
    """怪值（不可 JSON 序列化）不能把整次调用的凭据弄丢，也不该抛错。"""
    key = tool_idempotency_key("r1", "t", {"obj": object()})
    assert key.startswith("r1:")
    assert tool_idempotency_key("r1", "t", None).startswith("r1:")


# ---------- 3. call_builtin 分派 ----------


def test_call_builtin_injects_only_ctx_aware_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.modules.engine import tools_builtin

    seen: list[Any] = []

    async def _old(args: dict[str, Any]) -> str:
        seen.append(("old", dict(args)))
        return "old-ok"

    async def _new(args: dict[str, Any], ctx: ToolContext) -> str:
        seen.append(("new", ctx))
        return "new-ok"

    monkeypatch.setattr(tools_builtin, "BUILTIN_TOOLS", {"old": _old, "new": _new}, raising=False)
    ctx = ToolContext(run_id="r1", task_id="t1")

    assert asyncio.run(call_builtin("old", {"a": 1}, ctx)) == "old-ok"
    assert asyncio.run(call_builtin("new", {"a": 1}, ctx)) == "new-ok"
    # 无上下文（老调用点）时不注入，老签名照常工作
    assert asyncio.run(call_builtin("old", {"a": 1})) == "old-ok"

    assert seen[0] == ("old", {"a": 1})
    assert seen[1] == ("new", ctx)
    assert seen[2] == ("old", {"a": 1})


def test_call_builtin_missing_registry_key_raises_tool_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.modules.engine import tools_builtin

    monkeypatch.setattr(tools_builtin, "BUILTIN_TOOLS", {}, raising=False)
    with pytest.raises(ToolError) as ei:
        asyncio.run(call_builtin("nope", {}))
    assert ei.value.code == "internal_error"


def test_call_builtin_does_not_swallow_tool_typeerror(monkeypatch: pytest.MonkeyPatch) -> None:
    """适配层若用 try/except TypeError 兜老签名，这里会变成"成功"——护栏在此。"""
    from app.modules.engine import tools_builtin

    async def _buggy(args: dict[str, Any], ctx: ToolContext) -> str:
        raise TypeError("工具内部真的写错了")

    monkeypatch.setattr(tools_builtin, "BUILTIN_TOOLS", {"buggy": _buggy}, raising=False)
    with pytest.raises(TypeError):
        asyncio.run(call_builtin("buggy", {}, ToolContext(run_id="r1")))


# ---------- 4. tools 节点级注入 ----------


class _FakeHooks:
    def __init__(self) -> None:
        self.results: list[Any] = []

    async def on_tool_call(self, ctx: object, req: object) -> None: ...

    async def on_tool_result(self, ctx: object, info: Any) -> None:
        self.results.append(info)


def _tools_node(
    monkeypatch: pytest.MonkeyPatch, tools: dict[str, Any], *, backend: Any = None
) -> tuple[Any, _FakeHooks, str]:
    """取出编译后的 tools 节点函数（免 DB/LLM），注入假 builtin 工具表与假后端。"""
    from langgraph.graph.state import StateGraph

    from app.modules.engine import graph as graph_mod
    from app.modules.engine import tools_builtin
    from app.modules.engine.hooks import RunContext

    monkeypatch.setattr(StateGraph, "compile", lambda self, *a, **kw: self)
    monkeypatch.setattr(tools_builtin, "BUILTIN_TOOLS", tools, raising=False)

    hooks = _FakeHooks()
    ctxs: dict[str, RunContext] = {}

    def _get_run_ctx(run_id: str, *a: object, **kw: object) -> RunContext:
        return ctxs.setdefault(run_id, RunContext(run_id, None, "a1"))

    async def _save_long_output(run_id: str, content: Any, *, name: str) -> Any:
        return content

    rt = SimpleNamespace(
        saver=None,
        hooks=hooks,
        _run_ctx=ctxs,
        backend=backend,
        get_run_ctx=_get_run_ctx,
        save_long_output=_save_long_output,
    )
    builder = graph_mod.build_graph(rt)
    return builder.nodes["tools"].runnable.afunc, hooks, str(uuid4())


def _builtin_state(run_id: str, name: str, args: dict[str, Any] | None = None) -> dict:
    from langchain_core.messages import AIMessage

    return {
        "messages": [
            AIMessage(content="", tool_calls=[{"name": name, "args": args or {}, "id": "c1"}])
        ],
        "capability_cache": {
            "tools": [{"name": name, "kind": "tool", "builtin": name, "risk_level": "read"}]
        },
        "budget_state": {},
    }


def _mcp_state(run_id: str, name: str, cap_id: str) -> dict:
    from langchain_core.messages import AIMessage

    return {
        "messages": [AIMessage(content="", tool_calls=[{"name": name, "args": {}, "id": "c1"}])],
        "capability_cache": {
            "tools": [
                {
                    "name": name,
                    "kind": "mcp",
                    "capability_id": cap_id,
                    "tool_name": "remote_tool",
                    "risk_level": "read",
                }
            ]
        },
        "budget_state": {},
    }


def _config(run_id: str, **extra: Any) -> dict:
    return {"configurable": {"run_id": run_id, "agent_id": "a1", **extra}}


def test_tools_node_injects_run_id_and_idempotency_key(monkeypatch: pytest.MonkeyPatch) -> None:
    got: list[ToolContext] = []

    async def _ctx_tool(args: dict[str, Any], ctx: ToolContext) -> str:
        got.append(ctx)
        return "ok"

    node, _, run_id = _tools_node(monkeypatch, {"ctx_tool": _ctx_tool})
    asyncio.run(node(_builtin_state(run_id, "ctx_tool", {"q": "x"}), _config(run_id, task_id="t1")))

    assert len(got) == 1
    ctx = got[0]
    assert ctx.run_id == run_id
    assert ctx.task_id == "t1"
    assert ctx.idempotency_key == tool_idempotency_key(run_id, "ctx_tool", {"q": "x"})
    # 非等待路径不下发回调凭据
    assert ctx.callback_token is None and ctx.callback_url is None


def test_tools_node_resolves_step_id_once_via_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """config 未给 step_id 时按 run↔step 绑定查一次；同轮多次调用复用同一结果。"""
    calls: list[str] = []
    got: list[ToolContext] = []

    async def _step_id_for_run(run_id: str) -> str | None:
        calls.append(run_id)
        return "step-1"

    async def _ctx_tool(args: dict[str, Any], ctx: ToolContext) -> str:
        got.append(ctx)
        return "ok"

    backend = SimpleNamespace(step_id_for_run=_step_id_for_run)
    node, _, run_id = _tools_node(monkeypatch, {"ctx_tool": _ctx_tool}, backend=backend)
    state = _builtin_state(run_id, "ctx_tool")
    from langchain_core.messages import AIMessage

    state["messages"] = [
        AIMessage(
            content="",
            tool_calls=[
                {"name": "ctx_tool", "args": {}, "id": "c1"},
                {"name": "ctx_tool", "args": {"i": 2}, "id": "c2"},
            ],
        )
    ]
    asyncio.run(node(state, _config(run_id)))

    assert [c.step_id for c in got] == ["step-1", "step-1"]
    assert calls == [run_id], "同轮工具调用只查一次步骤绑定"


def test_tools_node_prefers_config_step_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """config 显式给出 step_id 时不再查库（测试替身无 step_id_for_run 也不报错）。"""

    async def _step_id_for_run(run_id: str) -> str | None:  # pragma: no cover - 不应被调用
        raise AssertionError("不该查库")

    got: list[ToolContext] = []

    async def _ctx_tool(args: dict[str, Any], ctx: ToolContext) -> str:
        got.append(ctx)
        return "ok"

    backend = SimpleNamespace(step_id_for_run=_step_id_for_run)
    node, _, run_id = _tools_node(monkeypatch, {"ctx_tool": _ctx_tool}, backend=backend)
    asyncio.run(node(_builtin_state(run_id, "ctx_tool"), _config(run_id, step_id="step-x")))
    assert [c.step_id for c in got] == ["step-x"]


def test_tools_node_without_backend_step_lookup_still_calls_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """后端没有 step_id_for_run（如测试替身）→ step_id 为 None，不影响工具执行。"""
    got: list[ToolContext] = []

    async def _ctx_tool(args: dict[str, Any], ctx: ToolContext) -> str:
        got.append(ctx)
        return "ok"

    node, _, run_id = _tools_node(monkeypatch, {"ctx_tool": _ctx_tool})
    asyncio.run(node(_builtin_state(run_id, "ctx_tool"), _config(run_id)))
    assert [c.step_id for c in got] == [None]


def test_tools_node_mcp_context_goes_to_meta_not_args(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """mcp 通道：上下文走 `_meta`（多塞 args 会撞 Server 的 inputSchema 校验）。"""
    from app.modules.capabilities.mcp_client import mcp_pool

    captured: list[dict[str, Any]] = []

    async def _fake_call_tool(
        cap_id: Any, tool_name: str, args: dict[str, Any], meta: dict[str, Any] | None = None
    ) -> str:
        captured.append({"cap_id": cap_id, "tool_name": tool_name, "args": args, "meta": meta})
        return "remote-ok"

    monkeypatch.setattr(mcp_pool, "call_tool", _fake_call_tool)
    node, _, run_id = _tools_node(monkeypatch, {})
    cap_id = str(uuid4())
    asyncio.run(
        node(_mcp_state(run_id, "remote_tool", cap_id), _config(run_id, task_id="t1", step_id="s1"))
    )

    assert len(captured) == 1
    call = captured[0]
    assert call["args"] == {} and "_meta" not in call["args"] and "agentos" not in call["args"]
    assert call["meta"] == {
        CONTEXT_META_KEY: {
            "run_id": run_id,
            "task_id": "t1",
            "step_id": "s1",
            "idempotency_key": tool_idempotency_key(run_id, "remote_tool", {}),
        }
    }


class _FakeAwaits:
    """最小等待接口替身：只覆盖派发路径用到的方法（P0-4 契约面）。"""

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}
        self.notified: list[tuple[str, Any]] = []

    async def ensure_await(
        self,
        *,
        run_id: str,
        tool_name: str,
        idempotency_key: str,
        builtin: str,
        capability_id: str | None = None,
        args: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        row: dict[str, Any] = {
            "await_id": str(uuid4()),
            "run_id": run_id,
            "tool_name": tool_name,
            "builtin": builtin,
            "args": args or {},
            "idempotency_key": idempotency_key,
            "callback_token": "tok-" + idempotency_key[:8],
            "status": "waiting",
            "deadline_at": (datetime.now(UTC) + timedelta(seconds=900)).isoformat(),
            "notified_at": None,
            "payload_out": None,
            "error": None,
            "waited_ms": 0,
        }
        self.rows[row["await_id"]] = row
        return dict(row)

    async def get_await(self, await_id: str) -> dict[str, Any] | None:
        row = self.rows.get(await_id)
        return dict(row) if row is not None else None

    async def mark_await_dispatched(self, await_id: str, *, response: Any = None) -> None:
        self.notified.append((await_id, response))
        self.rows[await_id]["notified_at"] = datetime.now(UTC).isoformat()

    async def cancel_await(self, await_id: str, *, reason: str | None = None) -> bool:
        return True


def test_await_gate_injects_callback_credential_through_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P0-4 的回传凭据经执行上下文下发：只认上下文的新签名工具也拿得到。"""
    from langgraph.graph.state import StateGraph

    from app.modules.engine import graph as graph_mod
    from app.modules.engine import tools_builtin
    from app.modules.engine.hooks import RunContext

    got: list[tuple[dict[str, Any], ToolContext]] = []

    async def _trigger(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        got.append((dict(args), ctx))
        return {"task_id": "P-1"}

    awaits = _FakeAwaits()
    monkeypatch.setattr(StateGraph, "compile", lambda self, *a, **kw: self)
    monkeypatch.setattr(
        tools_builtin, "BUILTIN_TOOLS", {"procurement_trigger": _trigger}, raising=False
    )
    monkeypatch.setattr(graph_mod, "interrupt", lambda value: None)  # 挂起由调度层接管

    ctxs: dict[str, RunContext] = {}

    def _get_run_ctx(run_id: str, *a: object, **kw: object) -> RunContext:
        return ctxs.setdefault(run_id, RunContext(run_id, None, "a1"))

    async def _save_long_output(run_id: str, content: Any, *, name: str | None = None) -> Any:
        return content

    rt = SimpleNamespace(
        saver=None,
        hooks=_FakeHooks(),
        _run_ctx=ctxs,
        backend=awaits,
        get_run_ctx=_get_run_ctx,
        save_long_output=_save_long_output,
    )
    node = graph_mod.build_graph(rt).nodes["await_gate"].runnable.afunc

    run_id = str(uuid4())
    state = {
        "pending_awaits": [
            {
                "id": "c1",
                "name": "procurement_trigger",
                "args": {"text": "A"},
                "builtin": "procurement_trigger",
                "capability_id": None,
                "risk_level": "write",
            }
        ]
    }
    asyncio.run(node(state, _config(run_id, task_id="t1")))

    assert len(got) == 1
    args, ctx = got[0]
    await_id = next(iter(awaits.rows))
    row = awaits.rows[await_id]
    assert ctx.run_id == run_id and ctx.task_id == "t1"
    assert ctx.await_id == await_id
    assert ctx.callback_url is not None and ctx.callback_url.endswith(
        f"/api/v1/open/awaits/{await_id}/resolve"
    )
    assert ctx.callback_token == row["callback_token"]
    # 等待路径的幂等键沿用 awaits 的"纯参数指纹"口径（与回调外发 body 一致）
    assert ctx.idempotency_key == row["idempotency_key"]
    # 历史通道（args）仍在，老签名工具零改动可用
    assert args["await_callback"]["await_id"] == await_id


def test_mcp_connection_forwards_meta_to_session_call() -> None:
    """连接层把上下文原样交给 SDK 的 `session.call_tool(..., meta=)`（序列化为 `params._meta`）。"""
    from app.modules.capabilities.mcp_client import McpConnection

    captured: list[dict[str, Any]] = []

    class _Session:
        async def call_tool(self, name: str, args: dict[str, Any], **kw: Any) -> SimpleNamespace:
            captured.append({"name": name, "args": args, **kw})
            return SimpleNamespace(isError=False, content=[SimpleNamespace(text="remote-ok")])

    async def _scenario() -> str:
        conn = McpConnection(uuid4(), "websearch", {})
        conn.session = _Session()  # type: ignore[assignment]
        conn._owner = asyncio.create_task(asyncio.sleep(3600))  # 视为会话存活
        try:
            return await conn.call_tool("search", {"q": "x"}, ToolContext(run_id="r1").as_meta())
        finally:
            conn._owner.cancel()

    assert asyncio.run(_scenario()) == "remote-ok"
    assert captured == [
        {
            "name": "search",
            "args": {"q": "x"},
            "meta": {CONTEXT_META_KEY: {"run_id": "r1"}},
        }
    ]


# ---------- 5. 上下文载荷 ----------


def test_await_callback_prefers_context_over_legacy_args() -> None:
    """派发工具取回传地址：上下文优先，掉回 args 的历史通道，都没有则不下发（不等）。"""
    from app.modules.engine import tools_builtin

    legacy = {
        "await_callback": {
            "await_id": "aw0",
            "url": "http://legacy",
            "token": "old",
            "idempotency_key": "k0",
        }
    }
    ctx = ToolContext(
        run_id="r1",
        idempotency_key="k1",
        callback_token="tk",
        await_id="aw1",
        callback_url="http://ctx",
    )
    assert tools_builtin._await_callback(legacy, ctx) == {
        "await_id": "aw1",
        "url": "http://ctx",
        "token": "tk",
        "idempotency_key": "k1",
    }
    assert tools_builtin._await_callback(legacy, None)["url"] == "http://legacy"
    assert tools_builtin._await_callback({}, None) is None
    # 上下文缺 url（半截凭据）→ 掉回历史通道，而不是下发不可用的地址
    assert (
        tools_builtin._await_callback(legacy, ToolContext(run_id="r1", await_id="aw1"))["url"]
        == "http://legacy"
    )


def test_as_meta_drops_empty_fields() -> None:
    ctx = ToolContext(run_id="r1", idempotency_key="k")
    assert ctx.as_meta() == {CONTEXT_META_KEY: {"run_id": "r1", "idempotency_key": "k"}}
    full = ToolContext(
        run_id="r1",
        task_id="t1",
        step_id="s1",
        idempotency_key="k",
        callback_token="tok",
        await_id="aw",
        callback_url="http://cb",
    )
    assert full.as_meta() == {
        CONTEXT_META_KEY: {
            "run_id": "r1",
            "task_id": "t1",
            "step_id": "s1",
            "idempotency_key": "k",
            "callback_token": "tok",
            "await_id": "aw",
            "callback_url": "http://cb",
        }
    }
