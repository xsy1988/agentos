"""多工具能力的风险级粒度（方案 §5 P2-4）。

缺陷（已核实）：`risk_level` 落在 `capabilities` 行上，`capability_tools` 只有开关，
所以一个 MCP Server 只要有一个写工具，整包被打成 `write` 后包内**只读**工具
（列举/查询类）也会命中 tools 节点的高危确认点，用户被迫对每次只读调用点"同意"。

修法：风险级按工具解析（`resolve_tool_risk`）—— 显式声明 > 能力级 dangerous
（不削弱）> MCP 官方注解（readOnlyHint/destructiveHint）> 沿用能力级。

覆盖三层：纯函数优先级矩阵 / 装配层（`expand_capability` 不依赖 DB 与真实 Server）/
图节点层（确认门只报真正高危的那一个调用）。
"""

import asyncio
import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from langchain_core.messages import AIMessage

from app.core.config import settings
from app.modules.discovery.assembler import expand_capability, resolve_tool_risk
from app.modules.engine.hooks import RunContext, ToolResultInfo

# ---------- 1. 优先级矩阵（纯函数） ----------


@pytest.mark.parametrize(
    ("cap_risk", "hints", "expected"),
    [
        # 只读注解：write 包里的列举工具不再触发高危确认（本项的直接目标）
        ("write", {"read_only_hint": True}, "read"),
        # 破坏性注解：反向也要细化，read 包里的删库工具必须拦
        ("read", {"destructive_hint": True}, "dangerous"),
        # 无注解：沿用能力级（与细化前行为一致，保守）
        ("write", {}, "write"),
        ("read", {}, "read"),
        # 只读 + 破坏性同时给：只读优先（Server 自相矛盾时按"不拦"是危险的？
        # 否——真矛盾时能力级 dangerous 仍兜底，见下一条）
        ("write", {"read_only_hint": True, "destructive_hint": True}, "read"),
    ],
)
def test_resolve_tool_risk_from_hints(cap_risk: str, hints: dict[str, bool], expected: str) -> None:
    assert resolve_tool_risk(cap_risk=cap_risk, tool_name="t", **hints) == expected


def test_resolve_tool_risk_never_weakens_dangerous_capability() -> None:
    """能力级一旦判定危险，注解不能"洗白"——危险结论只由人显式收回。"""
    assert (
        resolve_tool_risk(cap_risk="dangerous", tool_name="list_x", read_only_hint=True)
        == "dangerous"
    )


def test_resolve_tool_risk_explicit_declaration_wins() -> None:
    """`payload.tool_risk_levels` 是管理员意志，优先于注解与能力级。"""
    declared = {"list_x": "write", "read_x": "read"}
    assert (
        resolve_tool_risk(
            cap_risk="dangerous",
            tool_name="list_x",
            declared=declared,
            read_only_hint=True,
        )
        == "write"
    )
    assert resolve_tool_risk(cap_risk="write", tool_name="read_x", declared=declared) == "read"


def test_resolve_tool_risk_ignores_illegal_declaration() -> None:
    """声明值不合法（脏数据）时忽略它，掉回注解/能力级。"""
    for bad in ("WRITE", "high", 1, None):
        assert (
            resolve_tool_risk(
                cap_risk="write",
                tool_name="list_x",
                declared={"list_x": bad},
                read_only_hint=True,
            )
            == "read"
        )


def test_resolve_tool_risk_unknown_capability_risk_falls_back_to_read() -> None:
    assert resolve_tool_risk(cap_risk="banana", tool_name="t") == "read"


# ---------- 2. 装配层（expand_capability） ----------


def _mcp_cap(**over: Any) -> dict[str, Any]:
    cap = {
        "id": str(uuid.uuid4()),
        "type": "mcp",
        "name": "发票中心",
        "risk_level": "write",
        "payload": {},
    }
    cap.update(over)
    return cap


def _tools(*items: dict[str, Any]) -> list[dict[str, Any]]:
    return list(items)


def _fake_pool(monkeypatch: pytest.MonkeyPatch, tools: list[dict[str, Any]]) -> None:
    """替换连接池的工具清单读取（不连 MCP Server、不连库）。"""
    from app.modules.capabilities.mcp_client import mcp_pool

    async def _list() -> list[dict[str, Any]]:
        return tools

    monkeypatch.setattr(mcp_pool, "list_enabled_tools", _list)


def test_expand_capability_splits_risk_per_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    cap = _mcp_cap()
    _fake_pool(
        monkeypatch,
        _tools(
            {
                "capability_id": uuid.UUID(cap["id"]),
                "tool_name": "list_invoices",
                "description": "列出全部发票",
                "input_schema": {"type": "object"},
                "read_only_hint": True,
            },
            {
                "capability_id": uuid.UUID(cap["id"]),
                "tool_name": "delete_invoice",
                "description": "删除发票",
                "input_schema": {"type": "object"},
                "destructive_hint": True,
            },
            {
                "capability_id": uuid.UUID(cap["id"]),
                "tool_name": "update_invoice",
                "description": "改发票",
                "input_schema": {"type": "object"},
            },
        ),
    )
    out = asyncio.run(expand_capability(cap, "pinned"))
    # 暴露名唯一化保持不变（mcp__{server}__{tool}）
    assert [t["name"] for t in out] == [
        "mcp__发票中心__list_invoices",
        "mcp__发票中心__delete_invoice",
        "mcp__发票中心__update_invoice",
    ]
    assert [t["risk_level"] for t in out] == ["read", "dangerous", "write"]


def test_expand_capability_declared_override_beats_hints(monkeypatch: pytest.MonkeyPatch) -> None:
    cap = _mcp_cap(payload={"tool_risk_levels": {"list_invoices": "dangerous"}})
    _fake_pool(
        monkeypatch,
        _tools(
            {
                "capability_id": uuid.UUID(cap["id"]),
                "tool_name": "list_invoices",
                "description": "",
                "input_schema": {"type": "object"},
                "read_only_hint": True,
            }
        ),
    )
    out = asyncio.run(expand_capability(cap, "pinned"))
    assert out[0]["risk_level"] == "dangerous"


def test_expand_capability_tolerates_dirty_payload_declaration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cap = _mcp_cap(payload={"tool_risk_levels": ["list_invoices"]})  # 类型不对
    _fake_pool(
        monkeypatch,
        _tools(
            {
                "capability_id": uuid.UUID(cap["id"]),
                "tool_name": "list_invoices",
                "description": "",
                "input_schema": {"type": "object"},
                "read_only_hint": True,
            }
        ),
    )
    assert asyncio.run(expand_capability(cap, "pinned"))[0]["risk_level"] == "read"


def test_expand_capability_single_tool_capability_unchanged() -> None:
    """单工具能力（type=tool）零变化：风险级仍取能力级，回归不破。"""
    cap = {
        "id": str(uuid.uuid4()),
        "type": "tool",
        "name": "report",
        "risk_level": "write",
        "payload": {"builtin": "procurement_report", "schema": {"type": "function"}},
    }
    out = asyncio.run(expand_capability(cap, "pinned"))
    assert [t["risk_level"] for t in out] == ["write"]


# ---------- 3. 图节点层（确认门） ----------


class _FakeHooks:
    def __init__(self) -> None:
        self.calls: list[Any] = []
        self.results: list[ToolResultInfo] = []

    async def on_tool_call(self, ctx: Any, req: Any) -> None:
        self.calls.append(req)

    async def on_tool_result(self, ctx: Any, info: ToolResultInfo) -> None:
        self.results.append(info)


class _Interrupt:
    """按序消费的 interrupt 假件。"""

    def __init__(self, answers: list[Any]) -> None:
        self.answers = list(answers)
        self.values: list[Any] = []

    def __call__(self, value: Any) -> Any:
        self.values.append(value)
        return self.answers.pop(0) if self.answers else None


def _graph(monkeypatch: pytest.MonkeyPatch, builtins: dict[str, Any]) -> Any:
    from langgraph.graph.state import StateGraph

    from app.modules.engine import graph as graph_mod
    from app.modules.engine import tools_builtin

    monkeypatch.setattr(StateGraph, "compile", lambda self, *a, **kw: self)
    monkeypatch.setattr(tools_builtin, "BUILTIN_TOOLS", builtins, raising=False)
    monkeypatch.setattr(settings, "await_external_enabled", False, raising=False)

    hooks = _FakeHooks()
    ctxs: dict[str, RunContext] = {}

    def _get_run_ctx(run_id: str, *a: object, **kw: object) -> RunContext:
        return ctxs.setdefault(run_id, RunContext(run_id, None, "a1"))

    async def _save_long_output(run_id: str, content: Any, *, name: str | None = None) -> Any:
        return content

    rt = SimpleNamespace(
        saver=None,
        hooks=hooks,
        _run_ctx=ctxs,
        backend=None,
        get_run_ctx=_get_run_ctx,
        save_long_output=_save_long_output,
    )
    return graph_mod.build_graph(rt)


def _as_builtin(entry: dict[str, Any]) -> dict[str, Any]:
    """把装配结果转成 builtin 执行通道——只换执行方式，**风险级保持装配产物不变**。"""
    return {**entry, "kind": "builtin", "builtin": entry["tool_name"]}


def _state(run_id: str, tools: list[dict[str, Any]], calls: list[tuple[str, str]]) -> dict:
    messages = [
        AIMessage(
            content="",
            tool_calls=[{"name": name, "args": {"text": name}, "id": cid} for name, cid in calls],
        )
    ]
    return {
        "messages": messages,
        "capability_cache": {"tools": tools},
        "budget_state": {},
    }


def _config(run_id: str) -> dict:
    return {"configurable": {"run_id": run_id, "agent_id": "a1"}}


def test_confirm_gate_reports_only_destructive_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    """一轮里既有只读列举又有破坏性删除：确认卡只列删除，只读调用直接执行。"""
    cap = _mcp_cap()
    _fake_pool(
        monkeypatch,
        _tools(
            {
                "capability_id": uuid.UUID(cap["id"]),
                "tool_name": "list_invoices",
                "description": "",
                "input_schema": {"type": "object"},
                "read_only_hint": True,
            },
            {
                "capability_id": uuid.UUID(cap["id"]),
                "tool_name": "delete_invoice",
                "description": "",
                "input_schema": {"type": "object"},
                "destructive_hint": True,
            },
        ),
    )
    entries = [_as_builtin(t) for t in asyncio.run(expand_capability(cap, "pinned"))]
    run_id = str(uuid.uuid4())

    async def _ok(args: dict[str, Any]) -> str:
        return f"done:{args['text']}"

    builder = _graph(monkeypatch, {"list_invoices": _ok, "delete_invoice": _ok})
    interrupt = _Interrupt(["approved"])
    monkeypatch.setattr("app.modules.engine.graph.interrupt", interrupt)

    node = builder.nodes["tools"].runnable.afunc
    out = asyncio.run(
        node(
            _state(
                run_id,
                entries,
                [("mcp__发票中心__list_invoices", "c1"), ("mcp__发票中心__delete_invoice", "c2")],
            ),
            _config(run_id),
        )
    )

    assert len(interrupt.values) == 1
    payload = interrupt.values[0]["payload"]
    assert interrupt.values[0]["reason"] == "high_risk_tool"
    assert payload["calls"] == [
        {"name": "mcp__发票中心__delete_invoice", "args": {"text": "mcp__发票中心__delete_invoice"}}
    ]
    assert payload["risk_levels"] == {"mcp__发票中心__delete_invoice": "dangerous"}
    assert sorted(m.content for m in out["messages"]) == [
        "done:mcp__发票中心__delete_invoice",
        "done:mcp__发票中心__list_invoices",
    ]


def test_confirm_gate_silent_for_read_only_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    """整轮只读：一次确认都不弹（这正是 P2-4 要修的体验）。"""
    cap = _mcp_cap()
    _fake_pool(
        monkeypatch,
        _tools(
            {
                "capability_id": uuid.UUID(cap["id"]),
                "tool_name": "list_invoices",
                "description": "",
                "input_schema": {"type": "object"},
                "read_only_hint": True,
            }
        ),
    )
    entries = [_as_builtin(t) for t in asyncio.run(expand_capability(cap, "pinned"))]
    run_id = str(uuid.uuid4())

    async def _ok(args: dict[str, Any]) -> str:
        return "listed"

    builder = _graph(monkeypatch, {"list_invoices": _ok})
    interrupt = _Interrupt(["rejected"])  # 若被调用会直接否决 → 结果可见
    monkeypatch.setattr("app.modules.engine.graph.interrupt", interrupt)

    node = builder.nodes["tools"].runnable.afunc
    out = asyncio.run(
        node(_state(run_id, entries, [("mcp__发票中心__list_invoices", "c1")]), _config(run_id))
    )

    assert interrupt.values == []
    assert [m.content for m in out["messages"]] == ["listed"]
