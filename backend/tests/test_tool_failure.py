"""工具失败可信（方案 §4 P0-3）。

红线（评审判定为"假完成"根因）：工具失败**不得**被翻译成面向模型的自然语言
"结果"。覆盖四层：

1. 契约层（tool_outcome）：码表 / 适配 / 异常分类 / 连续失败计数；
2. 执行层（graph._guarded_call）：异常与失败契约一律收敛为结构化错误；
3. 真实工具：httpx 故障 → external_unavailable，非 JSON → parse_error；
4. 收尾层：审计事件带 `ok=false` + `error`；连续失败熔断 → run 落 failed
   且带结构化 `error`，**不进入终答**；验收失败不再记为达成。
"""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest

from app.modules.engine import runtime as runtime_mod
from app.modules.engine.graph import _guarded_call, build_verify_verdict
from app.modules.engine.hooks import RunContext, ToolFailureLoopError, ToolResultInfo
from app.modules.engine.hooks_impl import AuditHook
from app.modules.engine.runtime import EngineRuntime
from app.modules.engine.tool_outcome import (
    RETRYABLE_BY_CODE,
    STREAK_CODES,
    TOOL_FAILURE_CODES,
    TOOL_SOURCES,
    ToolError,
    classify_exception,
    failure,
    failure_content,
    failure_error,
    http_failure,
    next_failure_streak,
    normalize,
    success,
)
from app.modules.runs.models import Run

# ---------- 1. 契约层 ----------


def test_failure_code_table_is_closed() -> None:
    """失败码是固定枚举：自由文本只能出现在 detail。"""
    assert TOOL_FAILURE_CODES == (
        "external_unavailable",
        "external_rejected",
        "invalid_args",
        "parse_error",
        "internal_error",
        "denied_by_user",
        "await_expired",
        "await_cancelled",
        "await_unresolved",
    )
    assert set(RETRYABLE_BY_CODE) == set(TOOL_FAILURE_CODES)
    assert set(TOOL_FAILURE_CODES) >= STREAK_CODES
    with pytest.raises(ValueError):
        ToolError("boom", "自由文本码不允许")
    with pytest.raises(ValueError):
        failure_error("boom", "同样不允许")


def test_failure_source_table_is_closed() -> None:
    """source 同样封闭：builtin/mcp/external/engine，防止自由文本流入事件流。"""
    assert TOOL_SOURCES == ("builtin", "mcp", "external", "engine")
    for source in TOOL_SOURCES:
        assert ToolError("parse_error", "x", source=source).source == source
    with pytest.raises(ValueError):
        ToolError("parse_error", "x", source="weather")
    with pytest.raises(ValueError):
        failure_error("parse_error", "x", source="weather")


def test_tool_error_payload_shape() -> None:
    e = ToolError("external_unavailable", "x" * 900, source="mcp")
    payload = e.as_error()
    assert payload["code"] == "external_unavailable"
    assert payload["retryable"] is True  # 对端抖动可重试
    assert payload["source"] == "mcp"
    assert len(payload["detail"]) == 500  # 截断，避免把响应体灌进事件流
    assert ToolError("invalid_args", "缺参数").retryable is False
    assert ToolError("external_rejected", "4xx", retryable=True).retryable is True  # 可显式覆盖


def test_normalize_accepts_legacy_and_contract() -> None:
    """适配层：遗留裸返回值 = 成功（迁移期兼容）；失败契约必须带 error.code。"""
    assert normalize("杭州 晴") == ("杭州 晴", True, None)
    assert normalize({"reachable": False}) == ({"reachable": False}, True, None)  # 无 ok 键
    assert normalize(success("ok")) == ("ok", True, None)

    content, ok, error = normalize(failure("parse_error", "坏 JSON", source="mcp"))
    assert content is None and ok is False
    assert error == {
        "code": "parse_error",
        "detail": "坏 JSON",
        "retryable": True,
        "source": "mcp",
    }

    # 契约不完整（ok=false 但没给 error）：按内部错误处理，绝不当作成功
    content, ok, error = normalize({"ok": False})
    assert content is None and ok is False and error is not None
    assert error["code"] == "internal_error"


def test_classify_exception_maps_to_codes() -> None:
    assert classify_exception(json.JSONDecodeError("x", "y", 0), source="mcp").code == "parse_error"
    assert classify_exception(KeyError("daily")).code == "parse_error"
    assert classify_exception(TimeoutError()).code == "external_unavailable"
    assert classify_exception(ConnectionError("refused")).code == "external_unavailable"
    assert classify_exception(OSError("down")).code == "external_unavailable"
    assert classify_exception(httpx.ConnectError("boom")).code == "external_unavailable"
    assert classify_exception(httpx.ConnectError("boom"), source="mcp").source == "mcp"
    assert classify_exception(RuntimeError("内部炸了")).code == "internal_error"


def test_http_failure_splits_unavailable_and_rejected() -> None:
    assert http_failure(500, "500").code == "external_unavailable"
    assert http_failure(429, "限流").code == "external_unavailable"
    assert http_failure(404, "无此资源").code == "external_rejected"
    resp = httpx.Response(503, request=httpx.Request("GET", "http://x"))
    err = classify_exception(httpx.HTTPStatusError("503", request=resp.request, response=resp))
    assert err.code == "external_unavailable"


def test_next_failure_streak_only_counts_transient_codes() -> None:
    """只有"对端不可用/解析失败"计连续；入参错由模型自纠，不该熔断。"""
    s = 0
    for _ in range(3):
        s = next_failure_streak(s, "external_unavailable")
    assert s == 3
    assert next_failure_streak(3, "invalid_args") == 0
    assert next_failure_streak(3, None) == 0  # 成功清零
    assert next_failure_streak(1, "parse_error") == 2
    assert next_failure_streak(1, "external_rejected") == 0


def test_failure_content_is_structured_not_natural_language() -> None:
    """注入模型的失败载荷必须是可机读 JSON，并明确"这不是结果"。"""
    err = failure_error("external_unavailable", "天气服务不可用")
    payload = json.loads(failure_content("query_weather", err))
    assert payload["ok"] is False
    assert payload["tool"] == "query_weather"
    assert payload["error"]["code"] == "external_unavailable"
    assert "不是业务结果" in payload["hint"]


# ---------- 2. 执行层：异常一律收敛为结构化失败 ----------


def test_guarded_call_success_paths() -> None:
    async def _legacy() -> str:
        return "晴"

    async def _contract() -> dict:
        return success("晴")

    assert asyncio.run(_guarded_call(_legacy(), source="builtin")) == ("晴", True, None)
    assert asyncio.run(_guarded_call(_contract(), source="builtin")) == ("晴", True, None)


def test_guarded_call_never_returns_natural_language_on_failure() -> None:
    """回归红线：抛异常的工具必须得到 error 段，而不是一句"服务不可用，请稍后重试"。"""

    async def _boom() -> str:
        raise httpx.ConnectError("connection refused")

    async def _tool_error() -> str:
        raise ToolError("invalid_args", "city 不能为空")

    content, ok, error = asyncio.run(_guarded_call(_boom(), source="builtin"))
    assert content is None and ok is False
    assert error is not None and error["code"] == "external_unavailable"

    content, ok, error = asyncio.run(_guarded_call(_tool_error(), source="builtin"))
    assert content is None and ok is False
    assert error is not None and error["code"] == "invalid_args"


# ---------- 3. 真实工具：故障分类落到位 ----------


def _patch_httpx_client(monkeypatch: pytest.MonkeyPatch, exc: Exception) -> None:
    class _Client:
        def __init__(self, *a: object, **kw: object) -> None: ...

        async def __aenter__(self) -> "_Client":
            return self

        async def __aexit__(self, *exc_info: object) -> bool:
            return False

        async def request(self, *a: object, **kw: object) -> object:
            raise exc

        async def get(self, *a: object, **kw: object) -> object:
            raise exc

    monkeypatch.setattr(httpx, "AsyncClient", _Client)


def test_query_weather_http_error_becomes_structured_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.modules.engine.tools_builtin import _query_weather

    _patch_httpx_client(monkeypatch, httpx.ConnectError("connection refused"))
    content, ok, error = asyncio.run(
        _guarded_call(_query_weather({"city": "杭州"}), source="builtin")
    )
    assert content is None and ok is False
    assert error is not None and error["code"] == "external_unavailable"
    # 面向模型的载荷不含"可用/请稍后重试"式的自然语言结论
    assert json.loads(failure_content("query_weather", error))["ok"] is False


def test_query_weather_non_json_becomes_parse_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """对端返回 HTML 错误页：parse_error（可重试），不是"天气很好"。"""
    from app.modules.engine.tools_builtin import _query_weather

    class _GeoResp:
        status_code = 200

        def raise_for_status(self) -> None: ...

        def json(self) -> dict:
            return {"results": [{"name": "杭州", "latitude": 30.2, "longitude": 120.1}]}

    class _Resp:
        status_code = 200
        text = "<html>502 Bad Gateway</html>"

        def json(self) -> dict:
            raise json.JSONDecodeError("Expecting value", self.text, 0)

    class _Client:
        def __init__(self, *a: object, **kw: object) -> None: ...

        async def __aenter__(self) -> "_Client":
            return self

        async def __aexit__(self, *exc_info: object) -> bool:
            return False

        async def get(self, url: str, **kw: object) -> object:
            return _GeoResp() if "geocoding" in url else _Resp()

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    content, ok, error = asyncio.run(
        _guarded_call(_query_weather({"city": "杭州"}), source="builtin")
    )
    assert content is None and ok is False
    assert error is not None and error["code"] == "parse_error"


def test_procurement_http_error_becomes_structured_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """历史缺陷复现：采购服务下线时曾返回中文自然语言，模型据此宣称"已完成"。"""
    from app.modules.engine import tools_builtin

    _patch_httpx_client(monkeypatch, httpx.ConnectError("connection refused"))
    monkeypatch.setattr(tools_builtin, "_PROCUREMENT_BASE", "http://127.0.0.1:9", raising=False)
    content, ok, error = asyncio.run(
        _guarded_call(tools_builtin._procurement_status({"task_id": "t-1"}), source="builtin")
    )
    assert content is None and ok is False
    assert error is not None and error["code"] == "external_unavailable"


# ---------- 4. 审计事件与收尾 ----------


def test_tool_result_event_carries_ok_false_and_error() -> None:
    """前端/统计按 `ok=false` + `error.code` 消费，不再靠猜文案。"""
    events: list[tuple[str, dict]] = []
    hook = AuditHook(lambda run_id, et, payload: _emit(events, run_id, et, payload))
    ctx = RunContext(str(uuid4()), None, str(uuid4()))
    err = failure_error("external_unavailable", "天气服务不可用", source="builtin")
    info = ToolResultInfo(
        name="query_weather",
        ok=False,
        content=failure_content("query_weather", err),
        elapsed_ms=12,
        args_snapshot={"city": "杭州"},
        error=err,
    )
    asyncio.run(hook.on_tool_result(ctx, info))
    event_type, payload = events[0]
    assert event_type == "tool_result"
    assert payload["ok"] is False
    assert payload["tool"] == "query_weather"
    assert payload["error"]["code"] == "external_unavailable"


async def _emit(events: list[tuple[str, dict]], run_id: str, event_type: str, payload: dict) -> int:
    events.append((event_type, payload))
    return len(events)


def test_build_verify_verdict_never_marks_failure_as_achieved() -> None:
    """验收异常 → passed=False（可观测 verify_error）；仅回环耗尽才强制放行。"""
    err = failure_error("external_unavailable", "验收模型不可用", source="engine")
    v = build_verify_verdict(None, verify_error=err, retries=0)
    assert v["passed"] is False
    assert v["achieved"] is False
    assert v["verify_error"] == err
    assert v["exhausted"] is False

    # 回环耗尽：放行（避免死循环），但明确标注 exhausted 且 passed 仍为 False
    v = build_verify_verdict(None, verify_error=err, retries=2)
    assert v["achieved"] is True and v["exhausted"] is True and v["passed"] is False

    # 验收员没吐 JSON：parse_error，按未达成处理
    v = build_verify_verdict("验收通过！", verify_error=None, retries=0)
    assert v["passed"] is False
    assert v["verify_error"] is not None and v["verify_error"]["code"] == "parse_error"

    # 正常达成 / 正常不达成
    assert build_verify_verdict('{"achieved": true}', verify_error=None, retries=0)["passed"]
    nope = build_verify_verdict(
        '{"achieved": false, "feedback": "少了数据"}', verify_error=None, retries=0
    )
    assert nope["passed"] is False and nope["feedback"] == "少了数据"
    assert nope["achieved"] is False and nope["verify_error"] is None


# ---------- 5. tools 节点：连续失败熔断（集成） ----------


class _FakeHooks:
    def __init__(self) -> None:
        self.results: list[ToolResultInfo] = []

    async def on_tool_call(self, ctx: object, req: object) -> None: ...

    async def on_tool_result(self, ctx: object, info: ToolResultInfo) -> None:
        self.results.append(info)


def _tools_node(monkeypatch: pytest.MonkeyPatch, tools: dict) -> tuple[object, _FakeHooks, str]:
    """取出编译后的 tools 节点函数（免 DB/LLM），并注入假 builtin 工具表。"""
    from types import SimpleNamespace

    from langgraph.graph.state import StateGraph

    from app.modules.engine import graph as graph_mod
    from app.modules.engine import tools_builtin

    monkeypatch.setattr(StateGraph, "compile", lambda self, *a, **kw: self)
    monkeypatch.setattr(tools_builtin, "BUILTIN_TOOLS", tools, raising=False)

    hooks = _FakeHooks()
    ctxs: dict[str, RunContext] = {}

    def _get_run_ctx(run_id: str, *a: object, **kw: object) -> RunContext:
        return ctxs.setdefault(run_id, RunContext(run_id, None, "a1"))

    rt = SimpleNamespace(
        saver=None, hooks=hooks, _run_ctx=ctxs, backend=None, get_run_ctx=_get_run_ctx
    )
    builder = graph_mod.build_graph(rt)
    return builder.nodes["tools"].runnable.afunc, hooks, str(uuid4())


def _state(run_id: str, name: str, *, budget_state: dict | None = None) -> dict:
    from langchain_core.messages import AIMessage

    return {
        "messages": [AIMessage(content="", tool_calls=[{"name": name, "args": {}, "id": "c1"}])],
        "capability_cache": {
            "tools": [{"name": name, "kind": "tool", "builtin": name, "risk_level": "read"}]
        },
        "budget_state": budget_state or {},
    }


def _config(run_id: str) -> dict:
    return {"configurable": {"run_id": run_id, "agent_id": "a1"}}


def test_tools_node_breaks_loop_on_consecutive_transient_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """连续 3 次 external_unavailable → 抛熔断，且失败载荷始终是 JSON 信封。"""

    async def _down(args: dict) -> str:
        raise httpx.ConnectError("connection refused")

    node, hooks, run_id = _tools_node(monkeypatch, {"fake_down": _down})
    state = _state(run_id, "fake_down")

    for expected_streak in (1, 2):
        out = asyncio.run(node(state, _config(run_id)))
        assert out["budget_state"]["tool_failure_streak"] == expected_streak
        payload = json.loads(out["messages"][0].content)
        assert payload["ok"] is False and payload["error"]["code"] == "external_unavailable"
        state["budget_state"] = out["budget_state"]

    with pytest.raises(ToolFailureLoopError) as exc:
        asyncio.run(node(state, _config(run_id)))
    assert exc.value.code == "external_unavailable"
    assert exc.value.source == "builtin"
    assert exc.value.tools == ["fake_down"] * 3
    assert hooks.results[-1].ok is False and hooks.results[-1].error is not None


def test_tools_node_does_not_break_loop_on_invalid_args(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """入参错误不计连续（模型可自纠）：三次 invalid_args 不熔断，但仍是结构化失败。"""

    async def _need_arg(args: dict) -> str:
        raise ToolError("invalid_args", "缺参数")

    node, _, run_id = _tools_node(monkeypatch, {"fake_arg": _need_arg})
    state = _state(run_id, "fake_arg")

    for _ in range(3):
        out = asyncio.run(node(state, _config(run_id)))
        assert out["budget_state"]["tool_failure_streak"] == 0
        assert json.loads(out["messages"][0].content)["error"]["code"] == "invalid_args"
        state["budget_state"] = out["budget_state"]


def test_tools_node_records_denied_by_user_as_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """高危工具被用户拒绝：denied_by_user（不重试），工具绝不执行。"""
    called: list[str] = []

    async def _write(args: dict) -> str:
        called.append("write")
        return "不该走到这里"

    node, hooks, run_id = _tools_node(monkeypatch, {"fake_write": _write})
    from app.modules.engine import graph as graph_mod

    monkeypatch.setattr(graph_mod, "interrupt", lambda payload: "rejected")
    state = _state(run_id, "fake_write")
    state["capability_cache"]["tools"][0]["risk_level"] = "write"

    out = asyncio.run(node(state, _config(run_id)))
    payload = json.loads(out["messages"][0].content)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "denied_by_user"
    assert payload["error"]["retryable"] is False
    assert hooks.results[0].ok is False
    assert called == []  # 拒绝后不得产生副作用
    assert next_failure_streak(0, "denied_by_user") == 0  # 确定性失败不触发熔断


# ---------- 6. run 层：连续失败熔断 ----------


def test_run_error_payload_has_fixed_keys() -> None:
    """run 级 error 事件 payload 固定 {code, detail, source, retryable, phase}。"""
    err = runtime_mod._run_error(httpx.ConnectError("down"), phase="resume")
    assert err == {
        "code": "ConnectError",
        "detail": "down",
        "phase": "resume",
        "retryable": True,
        "source": "engine",
    }
    result = runtime_mod.timing.result_envelope(
        outcome="failed",
        text="部分输出",
        reason="tool_failure_loop",
        metrics={"elapsed_ms": 1},
        extra={"partial": False},
    )
    assert result["schema"] == "run_result/v1"
    assert result["outcome"] == "failed" and result["partial"] is False
    assert result["reason"] == "tool_failure_loop"
    assert result["text"] == "部分输出"


class _FakeDB:
    def __init__(self, run: Run) -> None:
        self.run = run

    async def __aenter__(self) -> "_FakeDB":
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def get(self, model: object, pk: object) -> Run:
        return self.run

    async def commit(self) -> None: ...


def _new_run() -> Run:
    return Run(
        id=uuid4(),
        status="running",
        trigger="manual",
        input={},
        budget={"timeout_seconds": 600},
        budget_used={},
        active_ms=0,
    )


def _install(monkeypatch: pytest.MonkeyPatch, rt: EngineRuntime, run: Run) -> None:
    async def _load(run_id: str) -> Run:
        return run

    monkeypatch.setattr(rt, "_load_run", _load)
    monkeypatch.setattr(runtime_mod, "session_factory", lambda: _FakeDB(run))


def test_finalize_tool_failure_persists_failure_envelope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """熔断收尾：result 为 failed 信封（非 partial）、error.code=tool_failure_loop。"""
    rt = EngineRuntime()
    run = _new_run()
    run.started_at = datetime.now(UTC) - timedelta(seconds=5)
    run.deadline_at = datetime.now(UTC) + timedelta(seconds=595)
    run_id = str(run.id)
    _install(monkeypatch, rt, run)
    rt._build_ctx(run)

    events: list[tuple[str, dict]] = []
    finalized: list[tuple[str, str, dict, bool, dict | None]] = []

    async def _emit(run_id: str, event_type: str, payload: dict) -> int:
        return await _emit_into(events, run_id, event_type, payload)

    async def _finalize(
        run_id: str,
        status: str,
        text: str,
        *,
        outcome: str | None = None,
        reason: str | None = None,
        extra: dict | None = None,
        cards: list | None = None,
        achieved: bool = True,
        error=None,
    ) -> None:
        finalized.append(
            (
                run_id,
                status,
                {"text": text, "outcome": outcome, "reason": reason, "extra": extra},
                achieved,
                error,
            )
        )

    monkeypatch.setattr(rt, "emit_event", _emit)
    monkeypatch.setattr(rt, "_finalize", _finalize)

    err = ToolFailureLoopError(
        "external_unavailable",
        "同一 run 内连续 3 次工具失败（阈值 3）：query_weather, procurement_status",
        source="builtin",
        tools=["query_weather", "procurement_status", "query_weather"],
    )
    asyncio.run(rt._finalize_tool_failure(run_id, err, phase="tools", thread_id=None))

    assert [e[0] for e in events] == ["error"]
    payload = events[0][1]
    assert payload["code"] == "tool_failure_loop"
    assert payload["phase"] == "tools"
    assert payload["retryable"] is True
    assert payload["failure_code"] == "external_unavailable"
    assert payload["tools"] == ["query_weather", "procurement_status", "query_weather"]

    _, status, kwargs, achieved, error = finalized[0]
    assert status == "failed" and achieved is False
    # 信封由 _finalize 组装（test_run_artifacts.py 覆盖），此处断言接线参数
    assert kwargs["outcome"] == "failed" and kwargs["extra"]["partial"] is False
    assert kwargs["reason"] == "tool_failure_loop"
    assert error is not None and error["code"] == "tool_failure_loop"


async def _emit_into(
    events: list[tuple[str, dict]], run_id: str, event_type: str, payload: dict
) -> int:
    events.append((event_type, payload))
    return len(events)


class _RaisingGraph:
    def __init__(self, exc: BaseException) -> None:
        self.exc = exc
        self.called = False

    async def ainvoke(self, payload: object, config: object) -> dict:
        self.called = True
        raise self.exc


def test_process_run_routes_tool_failure_loop_to_failure_finalize(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """熔断异常必须走专用收尾（结构完整），不能被通用 except 吞成裸 {code, detail}。"""
    rt = EngineRuntime()
    run = _new_run()
    run.status = "pending"
    run_id = str(run.id)
    _install(monkeypatch, rt, run)
    rt.graph = _RaisingGraph(  # type: ignore[assignment]
        ToolFailureLoopError(
            "parse_error", "连续解析失败", source="external", tools=["a", "b", "a"]
        )
    )

    seen: list[str] = []

    async def _rec(run_id: str, e: ToolFailureLoopError, *, phase: str, thread_id=None) -> None:
        seen.append(f"{phase}:{e.code}")

    async def _noop_set(run_id: str, status: str, error: dict | None = None) -> dict:
        seen.append(f"status:{status}")
        return {}

    async def _emit(run_id: str, event_type: str, payload: dict) -> int:
        return 0

    monkeypatch.setattr(rt, "_finalize_tool_failure", _rec)
    monkeypatch.setattr(rt, "_set_run_status", _noop_set)
    monkeypatch.setattr(rt, "emit_event", _emit)
    from app.modules.engine.hooks_impl import build_default_chain

    rt.hooks = build_default_chain(_emit)

    asyncio.run(rt._process_run(run_id))
    assert seen == ["status:running", "tools:parse_error"]
