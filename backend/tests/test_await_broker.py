"""外部等待由平台持有（方案 §4 P0-4 / §12 M9-b）。

不变量覆盖四层：
1. 策略与凭据层：灰度开关、内置工具分类、幂等键、回调 token/地址；
2. 服务层（假会话）：登记幂等、出网标记、CAS 落定、超时巡检、唤醒投递；
3. 图节点层：tools 节点把派发类调用拆成 pending_awaits、await_gate 单笔消费与
   granted/expired/cancelled 三分支回填（恢复一律以 DB 行为准）；
4. 运行时层：巡检唤醒、统一暂停出口分派、落定事件、恢复守卫。

单测不连库：ORM 的 `update().returning()` / `greatest()+EXTRACT(epoch)` 由假会话记录
语句形态断言（真实 PG 行为由 verify-e2e 覆盖）。
"""

import asyncio
import contextlib
import json
import time
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError

from app.core.config import settings
from app.modules.awaits import policy as await_policy
from app.modules.awaits.models import AWAIT_STATUSES, AWAIT_TERMINAL_STATUSES, AwaitBroker
from app.modules.awaits.schemas import AwaitResolveIn
from app.modules.awaits.service import (
    _expired_row_values,
    brief,
    callback_url,
    cancel,
    cancel_run_awaits,
    enqueue_resume,
    ensure_await,
    expire_due,
    list_awaits,
    make_callback_token,
    make_idempotency_key,
    mark_notified,
    resolve,
    resume_payload,
    verify_callback_token,
)
from app.modules.engine.hooks import RunContext, ToolResultInfo
from app.modules.engine.runtime import EngineRuntime
from app.modules.open_api.router import resolve_await
from app.modules.runs.models import Run
from tests.support.fake_db import FakeSession

T0 = datetime(2026, 9, 17, 3, 0, 0, tzinfo=UTC)


def _integrity_error() -> IntegrityError:
    return IntegrityError("INSERT INTO await_broker", {}, Exception("duplicate key"))


def _row(**over: Any) -> AwaitBroker:
    """构造等待行（默认 waiting、900s 后超时）。"""
    await_id = over.pop("id", uuid.uuid4())
    created = over.pop("created_at", T0)
    return AwaitBroker(
        id=await_id,
        run_id=over.pop("run_id", uuid.uuid4()),
        tool_name=over.pop("tool_name", "procurement_trigger"),
        idempotency_key=over.pop("idempotency_key", "k" * 32),
        callback_token=over.pop("callback_token", make_callback_token(str(await_id))),
        status=over.pop("status", "waiting"),
        deadline_at=over.pop("deadline_at", created + timedelta(seconds=900)),
        attempts=over.pop("attempts", 0),
        waited_ms=over.pop("waited_ms", 0),
        created_at=created,
        updated_at=created,
        **over,
    )


def _run(**over: Any) -> Run:
    return Run(
        id=over.pop("id", uuid.uuid4()),
        status=over.pop("status", "running"),
        trigger="manual",
        input={},
        budget={},
        budget_used={},
        active_ms=0,
        **over,
    )


# ---------- 1. 策略与凭据层 ----------


def test_policy_gate_is_closed_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """灰度开关默认关闭：外部服务实现回调契约前行为与旧版一致。"""
    assert await_policy.await_enabled() is False
    assert await_policy.is_model_hidden_builtin("procurement_status") is False

    monkeypatch.setattr(settings, "await_external_enabled", True)
    assert await_policy.await_enabled() is True
    assert await_policy.is_model_hidden_builtin("procurement_status") is True
    assert await_policy.is_model_hidden_builtin("procurement_trigger") is False
    assert await_policy.is_model_hidden_builtin(None) is False


def test_policy_covers_platform_owned_builtins() -> None:
    """派发类工具由平台接管等待；轮询类工具被取代。"""
    assert await_policy.is_dispatch_builtin("procurement_trigger") is True
    assert await_policy.is_dispatch_builtin("procurement_status") is False
    assert await_policy.is_dispatch_builtin(None) is False
    assert AWAIT_STATUSES == ("waiting", "granted", "expired", "cancelled")
    assert AWAIT_TERMINAL_STATUSES == ("granted", "expired", "cancelled")


def test_idempotency_key_is_argument_fingerprint() -> None:
    """同参数（与键序无关）= 同一笔外部请求；异参数必须区分。"""
    key = make_idempotency_key({"text": "报价单 A", "report_ref": None})
    assert key == make_idempotency_key({"report_ref": None, "text": "报价单 A"})
    assert len(key) == 32 and all(c in "0123456789abcdef" for c in key)
    assert key != make_idempotency_key({"text": "报价单 B"})
    assert make_idempotency_key({}) == make_idempotency_key({})


def test_callback_token_is_scoped_and_verifiable() -> None:
    """回调凭据绑定单笔等待，且换 secret 即失效。"""
    aid = str(uuid.uuid4())
    token = make_callback_token(aid)
    assert token == make_callback_token(aid) and len(token) == 32
    assert verify_callback_token(aid, token) is True
    assert verify_callback_token(aid, token[:-1] + ("0" if token[-1] != "0" else "1")) is False
    assert verify_callback_token(aid, None) is False
    assert verify_callback_token(str(uuid.uuid4()), token) is False
    assert verify_callback_token(aid, token, secret="other-secret") is False


def test_callback_url_has_single_slash(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "platform_base_url", "https://agent.example.com/")
    assert callback_url("a1") == "https://agent.example.com/api/v1/open/awaits/a1/resolve"


def test_brief_hides_callback_token() -> None:
    row = _row()
    view = brief(row)
    assert "callback_token" not in view
    assert view["tool"] == "procurement_trigger" and view["status"] == "waiting"
    assert view["await_id"] == str(row.id) and view["run_id"] == str(row.run_id)


def test_resume_payload_shape() -> None:
    """唤醒载荷是中断恢复值形态：带 kind 便于 runtime 区分"等外部"与"等用户"。"""
    row = _row(status="granted", resolved_at=T0 + timedelta(milliseconds=1500))
    payload = resume_payload(row, status="granted", payload={"score": 1})
    assert payload["kind"] == "await" and payload["status"] == "granted"
    assert payload["await_id"] == str(row.id) and payload["tool"] == row.tool_name
    assert payload["waited_ms"] == 1500
    assert payload["deadline_at"] == row.deadline_at.isoformat()
    assert payload["payload"] == {"score": 1}


def test_expired_row_values_marks_timeout() -> None:
    values = _expired_row_values(T0)
    assert values["status"] == "expired" and values["resolved_at"] == T0
    assert values["error"] == {
        "code": "await_expired",
        "message": "外部流程超时未回传结果",
    }
    # waited_ms 由库内 now - created_at 计算（跨进程重启也不依赖本地时钟）
    assert not isinstance(values["waited_ms"], int)


# ---------- 2. 服务层 ----------


def test_ensure_await_creates_waiting_row(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "await_default_timeout_seconds", 30)
    db = FakeSession(scalar_rows=[None])
    run_id = uuid.uuid4()
    row, created = asyncio.run(
        ensure_await(
            db,
            run_id=run_id,
            tool_name="procurement_trigger",
            idempotency_key="k" * 32,
            payload_in={"args": {"text": "A"}},
        )
    )
    assert created is True and row.status == "waiting" and row.attempts == 0
    assert db.added == [row] and db.commits == 1
    assert verify_callback_token(str(row.id), row.callback_token) is True
    delta = (row.deadline_at - datetime.now(UTC)).total_seconds()
    assert 28 <= delta <= 31, f"应按 settings 默认超时（30s）建单，实际 {delta:.1f}s"
    assert row.payload_in == {"args": {"text": "A"}}


def test_ensure_await_reuses_existing_row() -> None:
    """命中既有行直接复用：created=False，不新增行、不提交（防同参重放出网）。"""
    existing = _row()
    db = FakeSession(scalar_rows=[existing])
    row, created = asyncio.run(
        ensure_await(
            db,
            run_id=existing.run_id,
            tool_name=existing.tool_name,
            idempotency_key=existing.idempotency_key,
        )
    )
    assert row is existing and created is False
    assert db.added == [] and db.commits == 0


def test_ensure_await_recovers_from_unique_violation() -> None:
    """并发竞态：复合唯一约束兜底，回滚后复用对方已登记的行。"""
    again = _row()
    db = FakeSession(scalar_rows=[None, again], commit_error=_integrity_error())
    row, created = asyncio.run(
        ensure_await(
            db,
            run_id=again.run_id,
            tool_name=again.tool_name,
            idempotency_key=again.idempotency_key,
        )
    )
    assert row is again and created is False
    assert db.rollbacks == 1


def test_ensure_await_reraises_when_race_row_vanished() -> None:
    db = FakeSession(scalar_rows=[None, None], commit_error=_integrity_error())
    with pytest.raises(IntegrityError):
        asyncio.run(
            ensure_await(
                db,
                run_id=uuid.uuid4(),
                tool_name="procurement_trigger",
                idempotency_key="k" * 32,
            )
        )
    assert db.rollbacks == 1


def test_mark_notified_records_first_dispatch() -> None:
    row = _row()
    db = FakeSession()
    asyncio.run(mark_notified(db, row, at=T0, response={"task_id": "P-1"}))
    assert row.notified_at == T0
    assert row.payload_in == {"dispatch_response": {"task_id": "P-1"}}

    # 重放：首次出网时间与首派发结果都不被覆盖
    asyncio.run(mark_notified(db, row, at=T0 + timedelta(hours=1)))
    assert row.notified_at == T0
    assert row.payload_in == {"dispatch_response": {"task_id": "P-1"}}
    assert db.commits == 2


def test_resolve_rejects_non_terminal_status() -> None:
    with pytest.raises(ValueError):
        asyncio.run(resolve(FakeSession(), uuid.uuid4(), status="waiting"))


def test_resolve_missing_row() -> None:
    row, won = asyncio.run(resolve(FakeSession(get_rows=[None]), uuid.uuid4()))
    assert row is None and won is False


def test_resolve_cas_win_and_loss() -> None:
    """CAS 落败（回调/超时先到）不得重复唤醒 run。"""
    row = _row()
    db = FakeSession(get_rows=[row], scalar_rows=[row.id])
    resolved, won = asyncio.run(resolve(db, row.id, status="granted"))
    assert resolved is row and won is True and db.refreshed == [row]

    other = _row()
    db2 = FakeSession(get_rows=[other], scalar_rows=[None])
    _, won2 = asyncio.run(resolve(db2, other.id, status="granted"))
    assert won2 is False


def test_cancel_is_cas() -> None:
    row = _row()
    assert asyncio.run(cancel(FakeSession(scalar_rows=[row.id]), row)) is True
    assert asyncio.run(cancel(FakeSession(scalar_rows=[None]), _row())) is False


def test_cancel_run_awaits_counts_flipped_rows() -> None:
    db = FakeSession(execute_rows=[[(uuid.uuid4(),), (uuid.uuid4(),)]])
    assert asyncio.run(cancel_run_awaits(db, uuid.uuid4())) == 2
    assert db.commits == 1
    stmt, params = db.executed[0]
    assert params is None and str(stmt).startswith("UPDATE await_broker")
    assert "await_broker.status = " in str(stmt)


def test_expire_due_noop_when_nothing_due() -> None:
    db = FakeSession(scalars_rows=[[]])
    assert asyncio.run(expire_due(db)) == []
    # 只发一条 UPDATE：无到期行时不再查行
    assert db.scalars_calls == 1 and db.commits == 0


def test_expire_due_returns_flipped_rows() -> None:
    row = _row(status="expired")
    db = FakeSession(scalars_rows=[[row.id], [row]])
    assert asyncio.run(expire_due(db)) == [row]
    stmt = str(db.statements[0])
    assert stmt.startswith("UPDATE await_broker") and "deadline_at <=" in stmt


def test_enqueue_resume_writes_inbox_event_and_notifies() -> None:
    """唤醒投递 = INSERT inbox_events(resume) + pg_notify（与状态 CAS 同事务）。"""
    row = _row(
        status="granted",
        resolved_at=T0 + timedelta(milliseconds=1500),
        payload_out={"score": 1},
    )
    db = FakeSession()
    asyncio.run(enqueue_resume(db, row, status="granted"))
    assert len(db.executed) == 2
    insert, params = db.executed[0]
    assert "INSERT INTO inbox_events" in str(insert)
    assert params is not None and params["rid"] == row.run_id
    body = json.loads(params["p"])
    assert body["kind"] == "await" and body["status"] == "granted"
    assert body["payload"] == {"score": 1}
    assert "pg_notify" in str(db.executed[1][0])


def test_list_awaits_passes_through_rows() -> None:
    row = _row()
    db = FakeSession(scalars_rows=[[row]])
    assert asyncio.run(list_awaits(db, run_id=row.run_id, status="waiting")) == [row]
    stmt = str(db.statements[0])
    assert "await_broker.run_id = " in stmt and "await_broker.status = " in stmt


# ---------- 3. 图节点层 ----------


class _FakeHooks:
    def __init__(self) -> None:
        self.calls: list[Any] = []
        self.results: list[ToolResultInfo] = []

    async def on_tool_call(self, ctx: Any, req: Any) -> None:
        self.calls.append(req)

    async def on_tool_result(self, ctx: Any, info: ToolResultInfo) -> None:
        self.results.append(info)


class _FakeAwaits:
    """假 backend 的等待接口：内存行表 + 出网/撤销记录。"""

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}
        self.ensured: list[dict[str, Any]] = []
        self.notified: list[tuple[str, Any]] = []
        self.cancelled: list[tuple[str, str | None]] = []

    def _match(self, run_id: str, tool_name: str, key: str) -> dict[str, Any] | None:
        for row in self.rows.values():
            if (row["run_id"], row["tool_name"], row["idempotency_key"]) == (
                run_id,
                tool_name,
                key,
            ):
                return row
        return None

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
        self.ensured.append(
            {
                "run_id": run_id,
                "tool_name": tool_name,
                "idempotency_key": idempotency_key,
                "builtin": builtin,
                "capability_id": capability_id,
                "args": args,
            }
        )
        existing = self._match(run_id, tool_name, idempotency_key)
        if existing is not None:
            return dict(existing)
        row: dict[str, Any] = {
            "await_id": str(uuid.uuid4()),
            "run_id": run_id,
            "tool_name": tool_name,
            "builtin": builtin,
            "args": args or {},
            "idempotency_key": idempotency_key,
            "callback_token": "token-" + idempotency_key[:8],
            "status": "waiting",
            "deadline_at": (T0 + timedelta(seconds=900)).isoformat(),
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
        row = self.rows[await_id]
        row["notified_at"] = datetime.now(UTC).isoformat()
        if response is not None:
            row["dispatch_response"] = response

    async def cancel_await(self, await_id: str, *, reason: str | None = None) -> bool:
        self.cancelled.append((await_id, reason))
        self.rows[await_id]["status"] = "cancelled"
        return True


class _Interrupts:
    """假 interrupt：记录载荷并模拟"落定期排到 DB 后再唤醒"。"""

    def __init__(self, awaits: _FakeAwaits | None = None, on_call: Any = None) -> None:
        self.values: list[Any] = []
        self.awaits = awaits
        self.on_call = on_call

    def __call__(self, value: Any) -> None:
        self.values.append(value)
        if self.on_call is not None:
            self.on_call(value)
        return None


class _InterruptSteps:
    """按序消费的 interrupt 假件：每次调用返回下一个预置答案。"""

    def __init__(self, answers: list[Any]) -> None:
        self.answers = list(answers)
        self.values: list[Any] = []

    def __call__(self, value: Any) -> Any:
        self.values.append(value)
        return self.answers.pop(0) if self.answers else None


def _graph(monkeypatch: pytest.MonkeyPatch, *, builtins: dict[str, Any], awaits: Any) -> Any:
    """构建未编译的图（免 DB/LLM），返回 (builder, hooks)。"""
    from langgraph.graph.state import StateGraph

    from app.modules.engine import graph as graph_mod
    from app.modules.engine import tools_builtin

    monkeypatch.setattr(StateGraph, "compile", lambda self, *a, **kw: self)
    monkeypatch.setattr(tools_builtin, "BUILTIN_TOOLS", builtins, raising=False)

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
        backend=awaits,
        get_run_ctx=_get_run_ctx,
        save_long_output=_save_long_output,
    )
    builder = graph_mod.build_graph(rt)
    return builder, hooks


def _node(builder: Any, name: str) -> Any:
    return builder.nodes[name].runnable.afunc


def _route(builder: Any, node: str) -> Any:
    return next(iter(builder.branches[node].values())).path.func


def _config(run_id: str) -> dict:
    return {"configurable": {"run_id": run_id, "agent_id": "a1"}}


def _ai_call(name: str, args: dict[str, Any], call_id: str = "c1") -> Any:
    from langchain_core.messages import AIMessage

    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": call_id}])


def _tool_state(run_id: str, *, name: str, args: dict[str, Any], risk: str) -> dict:
    return {
        "messages": [_ai_call(name, args)],
        "capability_cache": {
            "tools": [{"name": name, "kind": "builtin", "builtin": name, "risk_level": risk}]
        },
        "budget_state": {},
    }


def test_tools_node_defers_dispatch_builtin(monkeypatch: pytest.MonkeyPatch) -> None:
    """开关打开且已授权：派发类调用不在 tools 节点出网，改由 await_gate 挂起。"""
    monkeypatch.setattr(settings, "await_external_enabled", True)
    executed: list[dict[str, Any]] = []

    async def _trigger(args: dict[str, Any]) -> dict:
        executed.append(args)
        return {"status": "accepted"}

    awaits = _FakeAwaits()
    builder, hooks = _graph(monkeypatch, builtins={"procurement_trigger": _trigger}, awaits=awaits)
    interrupt = _InterruptSteps(["approved"])
    monkeypatch.setattr("app.modules.engine.graph.interrupt", interrupt)

    run_id = str(uuid.uuid4())
    state = _tool_state(run_id, name="procurement_trigger", args={"text": "A"}, risk="write")
    out = asyncio.run(_node(builder, "tools")(state, _config(run_id)))

    assert out["messages"] == [] and executed == []
    assert hooks.calls == [] and hooks.results == []
    assert out["pending_awaits"] == [
        {
            "id": "c1",
            "name": "procurement_trigger",
            "args": {"text": "A"},
            "builtin": "procurement_trigger",
            "capability_id": None,
            "risk_level": "write",
        }
    ]
    # 高危确认点仍先于拆分：写级工具要先拿到用户授权
    assert interrupt.values[0]["reason"] == "high_risk_tool"
    assert _route(builder, "tools")(out) == "await_gate"


def test_tools_node_executes_when_gate_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """开关关闭：行为与旧版完全一致（正常执行、无等待拆分）。"""
    monkeypatch.setattr(settings, "await_external_enabled", False)
    executed: list[dict[str, Any]] = []

    async def _trigger(args: dict[str, Any]) -> str:
        executed.append(args)
        return "accepted"

    awaits = _FakeAwaits()
    builder, hooks = _graph(monkeypatch, builtins={"procurement_trigger": _trigger}, awaits=awaits)
    interrupt = _InterruptSteps(["approved"])
    monkeypatch.setattr("app.modules.engine.graph.interrupt", interrupt)

    run_id = str(uuid.uuid4())
    state = _tool_state(run_id, name="procurement_trigger", args={"text": "A"}, risk="write")
    out = asyncio.run(_node(builder, "tools")(state, _config(run_id)))

    assert executed == [{"text": "A"}] and awaits.ensured == []
    assert out["pending_awaits"] == []
    assert len(out["messages"]) == 1 and out["messages"][0].content == "accepted"
    assert hooks.results[0].ok is True
    assert _route(builder, "tools")(out) == "agent"


def test_tools_node_does_not_defer_denied_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """用户拒绝高危写调用：不得拆分等待（不能以"等外部"绕过拒绝）。"""
    monkeypatch.setattr(settings, "await_external_enabled", True)
    executed: list[dict[str, Any]] = []

    async def _trigger(args: dict[str, Any]) -> str:
        executed.append(args)
        return "accepted"

    awaits = _FakeAwaits()
    builder, _hooks = _graph(monkeypatch, builtins={"procurement_trigger": _trigger}, awaits=awaits)
    monkeypatch.setattr("app.modules.engine.graph.interrupt", _InterruptSteps(["rejected"]))

    run_id = str(uuid.uuid4())
    state = _tool_state(run_id, name="procurement_trigger", args={"text": "A"}, risk="write")
    out = asyncio.run(_node(builder, "tools")(state, _config(run_id)))

    assert out["pending_awaits"] == [] and executed == [] and awaits.ensured == []
    payload = json.loads(out["messages"][0].content)
    assert payload["ok"] is False and payload["error"]["code"] == "denied_by_user"


def test_await_gate_dispatches_once_then_returns_granted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """正常路径：登记 → 出网一次（注入回调凭据）→ interrupt 挂起 → 按 DB 回填成功观察。"""
    seen: list[dict[str, Any]] = []

    async def _trigger(args: dict[str, Any]) -> dict:
        seen.append(args)
        return {"task_id": "P-1"}

    awaits = _FakeAwaits()
    builder, hooks = _graph(monkeypatch, builtins={"procurement_trigger": _trigger}, awaits=awaits)

    def _on_interrupt(value: Any) -> None:
        await_id = str(value["payload"]["await_id"])
        row = awaits.rows[await_id]
        row["status"] = "granted"
        row["payload_out"] = {"score": 1}
        row["waited_ms"] = 4200

    interrupt = _Interrupts(awaits, on_call=_on_interrupt)
    monkeypatch.setattr("app.modules.engine.graph.interrupt", interrupt)

    run_id = str(uuid.uuid4())
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
    out = asyncio.run(_node(builder, "await_gate")(state, _config(run_id)))

    assert len(awaits.ensured) == 1
    ensured = awaits.ensured[0]
    assert ensured["run_id"] == run_id and ensured["builtin"] == "procurement_trigger"
    assert ensured["idempotency_key"] == make_idempotency_key({"text": "A"})
    await_id = next(iter(awaits.rows))
    assert awaits.notified == [(await_id, {"task_id": "P-1"})]

    # 回调凭据随派发注入（外部服务据此回传）
    callback = seen[0]["await_callback"]
    assert callback["await_id"] == await_id
    assert callback["url"].endswith(f"/api/v1/open/awaits/{await_id}/resolve")
    assert callback["token"] == awaits.rows[await_id]["callback_token"]
    assert callback["idempotency_key"] == ensured["idempotency_key"]

    assert interrupt.values[0]["reason"] == "external_await"
    payload = interrupt.values[0]["payload"]
    assert payload["kind"] == "await" and payload["tool"] == "procurement_trigger"
    assert payload["args"] == {"text": "A"} and payload["await_id"] == await_id
    assert payload["idempotency_key"] == ensured["idempotency_key"]

    body = json.loads(out["messages"][0].content)
    assert body["status"] == "resolved" and body["result"] == {"score": 1}
    assert body["waited_ms"] == 4200
    assert out["pending_awaits"] == [] and out["messages"][0].tool_call_id == "c1"
    assert hooks.results[-1].ok is True and hooks.results[-1].elapsed_ms == 4200


def test_await_gate_skips_dispatch_on_replay(monkeypatch: pytest.MonkeyPatch) -> None:
    """重放/重启：已出网（notified_at 已设）不再派发，恢复仍按 DB 结果回填。"""
    seen: list[dict[str, Any]] = []

    async def _trigger(args: dict[str, Any]) -> dict:
        seen.append(args)
        return {"task_id": "P-1"}

    awaits = _FakeAwaits()
    builder, hooks = _graph(monkeypatch, builtins={"procurement_trigger": _trigger}, awaits=awaits)
    run_id = str(uuid.uuid4())
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
    # 预置：上一段已登记并出网
    row = asyncio.run(
        awaits.ensure_await(
            run_id=run_id,
            tool_name="procurement_trigger",
            idempotency_key=make_idempotency_key({"text": "A"}),
            builtin="procurement_trigger",
            args={"text": "A"},
        )
    )
    asyncio.run(awaits.mark_await_dispatched(row["await_id"], response={"task_id": "P-1"}))
    awaits.ensured.clear()
    awaits.notified.clear()

    interrupt = _Interrupts(
        awaits,
        on_call=lambda value: awaits.rows[str(value["payload"]["await_id"])].update(
            status="granted", payload_out={"score": 9}, waited_ms=300
        ),
    )
    monkeypatch.setattr("app.modules.engine.graph.interrupt", interrupt)

    out = asyncio.run(_node(builder, "await_gate")(state, _config(run_id)))

    assert seen == [] and awaits.notified == [] and hooks.calls == []
    assert len(awaits.ensured) == 1  # 仍查（幂等登记），但不重复出网
    body = json.loads(out["messages"][0].content)
    assert body["status"] == "resolved" and body["result"] == {"score": 9}


def test_await_gate_cancels_wait_when_dispatch_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """派发失败：撤销等待行、按结构化失败回填、不 interrupt（不留悬挂行）。"""
    from app.modules.engine.tool_outcome import ToolError

    async def _trigger(args: dict[str, Any]) -> str:
        raise ToolError("external_unavailable", "连接被拒")

    awaits = _FakeAwaits()
    builder, hooks = _graph(monkeypatch, builtins={"procurement_trigger": _trigger}, awaits=awaits)
    interrupt = _Interrupts(awaits)
    monkeypatch.setattr("app.modules.engine.graph.interrupt", interrupt)

    run_id = str(uuid.uuid4())
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
    out = asyncio.run(_node(builder, "await_gate")(state, _config(run_id)))

    assert interrupt.values == [] and awaits.notified == []
    assert awaits.cancelled == [(next(iter(awaits.rows)), "external_unavailable")]
    body = json.loads(out["messages"][0].content)
    assert body["ok"] is False and body["error"]["code"] == "external_unavailable"
    assert out["pending_awaits"] == [] and hooks.results[-1].ok is False


def test_await_gate_reports_missing_builtin(monkeypatch: pytest.MonkeyPatch) -> None:
    """builtin 注册键缺失（部署漂移）：按 internal_error 回填并撤销等待行。"""
    awaits = _FakeAwaits()
    builder, hooks = _graph(monkeypatch, builtins={}, awaits=awaits)
    monkeypatch.setattr("app.modules.engine.graph.interrupt", _Interrupts(awaits))

    run_id = str(uuid.uuid4())
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
    out = asyncio.run(_node(builder, "await_gate")(state, _config(run_id)))
    body = json.loads(out["messages"][0].content)
    assert body["error"]["code"] == "internal_error"
    assert awaits.cancelled == [(next(iter(awaits.rows)), "internal_error")]


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("expired", "await_expired"),
        ("cancelled", "await_cancelled"),
        ("waiting", "await_unresolved"),
    ],
)
def test_await_gate_maps_terminal_status_to_failure_code(
    monkeypatch: pytest.MonkeyPatch, status: str, expected: str
) -> None:
    """超时/撤销/未知状态都对模型是结构化失败，且不重试（重试=重新派发外部流程）。"""
    awaits = _FakeAwaits()

    async def _trigger(args: dict[str, Any]) -> dict:
        return {"status": "accepted"}

    builder, hooks = _graph(monkeypatch, builtins={"procurement_trigger": _trigger}, awaits=awaits)

    def _settle(value: Any) -> None:
        awaits.rows[str(value["payload"]["await_id"])]["status"] = status

    monkeypatch.setattr("app.modules.engine.graph.interrupt", _Interrupts(awaits, on_call=_settle))

    run_id = str(uuid.uuid4())
    state = {
        "pending_awaits": [
            {
                "id": "c1",
                "name": "procurement_trigger",
                "args": {},
                "builtin": "procurement_trigger",
                "capability_id": None,
                "risk_level": "write",
            }
        ]
    }
    out = asyncio.run(_node(builder, "await_gate")(state, _config(run_id)))
    body = json.loads(out["messages"][0].content)
    assert body["ok"] is False and body["error"]["code"] == expected
    assert body["error"]["retryable"] is False
    assert hooks.results[-1].ok is False


def test_await_gate_consumes_one_wait_per_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    """一批多笔等待由自环边逐笔消费：每次最多 interrupt 一次（与 runtime 一一对应）。"""

    async def _trigger(args: dict[str, Any]) -> dict:
        return {"task_id": "P-1"}

    awaits = _FakeAwaits()
    builder, hooks = _graph(monkeypatch, builtins={"procurement_trigger": _trigger}, awaits=awaits)
    interrupt = _Interrupts(
        awaits,
        on_call=lambda value: awaits.rows[str(value["payload"]["await_id"])].update(
            status="granted"
        ),
    )
    monkeypatch.setattr("app.modules.engine.graph.interrupt", interrupt)

    run_id = str(uuid.uuid4())
    first = {
        "id": "c1",
        "name": "procurement_trigger",
        "args": {"text": "A"},
        "builtin": "procurement_trigger",
        "capability_id": None,
        "risk_level": "write",
    }
    second = {**first, "id": "c2", "args": {"text": "B"}}
    out = asyncio.run(
        _node(builder, "await_gate")({"pending_awaits": [first, second]}, _config(run_id))
    )

    assert len(interrupt.values) == 1 and len(awaits.ensured) == 1
    assert len(out["messages"]) == 1 and out["messages"][0].tool_call_id == "c1"
    assert out["pending_awaits"] == [second]
    route = _route(builder, "await_gate")
    assert route(out) == "await_gate"
    assert route({"pending_awaits": []}) == "agent"


def test_await_gate_noop_without_pending(monkeypatch: pytest.MonkeyPatch) -> None:
    awaits = _FakeAwaits()
    builder, hooks = _graph(monkeypatch, builtins={}, awaits=awaits)
    interrupt = _Interrupts(awaits)
    monkeypatch.setattr("app.modules.engine.graph.interrupt", interrupt)

    out = asyncio.run(_node(builder, "await_gate")({}, _config(str(uuid.uuid4()))))
    assert out == {"pending_awaits": []}
    assert awaits.ensured == [] and interrupt.values == [] and hooks.results == []


def test_expand_capability_hides_superseded_polling_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """平台持有等待后，轮询工具从模型可见列表消失（含检索后门）。"""
    from app.modules.discovery.assembler import expand_capability

    cap = {
        "id": str(uuid.uuid4()),
        "name": "procurement_status",
        "type": "tool",
        "risk_level": "read",
        "payload": {"builtin": "procurement_status", "schema": {"type": "object"}},
    }
    monkeypatch.setattr(settings, "await_external_enabled", True)
    assert asyncio.run(expand_capability(cap, "seed")) == []

    monkeypatch.setattr(settings, "await_external_enabled", False)
    assert len(asyncio.run(expand_capability(cap, "seed"))) == 1


# ---------- 4. 运行时层 ----------


def _settle_snapshot(*interrupt_values: Any) -> Any:
    tasks = [SimpleNamespace(interrupts=[SimpleNamespace(value=v) for v in interrupt_values])]
    return SimpleNamespace(tasks=tasks)


def test_pause_dispatches_external_await_to_waiting_external(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """统一暂停出口：external_await → waiting_external，其余仍走待确认。"""
    from app.modules.engine import runtime as runtime_mod

    rt = EngineRuntime()
    run = _run(status="running", id=uuid.uuid4())
    rows: list[FakeSession] = []

    def _factory() -> FakeSession:
        db = FakeSession(get_rows=[run])
        rows.append(db)
        return db

    monkeypatch.setattr(runtime_mod, "session_factory", _factory)
    events: list[tuple[str, dict[str, Any]]] = []

    async def _emit(run_id: str, event_type: str, payload: dict[str, Any]) -> int:
        events.append((event_type, payload))
        return 1

    monkeypatch.setattr(rt, "emit_event", _emit)

    run_id = str(run.id)
    rt._open_segment(run, "running", T0)
    # 伪造本执行段已跑了约 5s：收段后应累加进 active_ms
    clock = rt._clock(run_id)
    clock.segment_started_at = datetime.now(UTC) - timedelta(seconds=5)
    snapshot = _settle_snapshot(
        {
            "reason": "external_await",
            "payload": {"await_id": "a1", "tool": "procurement_trigger", "deadline_at": "T"},
        }
    )
    asyncio.run(rt._pause(run_id, snapshot))

    assert run.status == "waiting_external" and run.paused_at is not None
    assert rows[0].commits == 1
    # 执行段被收段：5s 执行时长入账，且等待期间不再增长（等待不计入 active_ms）
    assert run.active_ms >= 4900
    frozen = rt._timing_payload(run)["active_ms"]
    time.sleep(0.02)
    assert rt._timing_payload(run)["active_ms"] == frozen
    assert [e[0] for e in events] == ["await_started", "run_status"]
    await_event = events[0][1]
    assert await_event["await_id"] == "a1" and await_event["deadline_at"] == "T"
    # 等待超时点与 run 时长截止点语义不同，必须分别可见
    assert await_event["run_deadline_at"] == run.deadline_at.isoformat()
    assert events[1][1]["status"] == "waiting_external"
    assert events[1][1]["reason"] == "external_await"


def test_pause_keeps_confirmation_path(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.modules.engine import runtime as runtime_mod

    rt = EngineRuntime()
    run = _run(status="running")
    monkeypatch.setattr(runtime_mod, "session_factory", lambda: FakeSession(get_rows=[run]))

    async def _emit(run_id: str, event_type: str, payload: dict[str, Any]) -> int:
        return 1

    monkeypatch.setattr(rt, "emit_event", _emit)
    asyncio.run(
        rt._pause(str(run.id), _settle_snapshot({"reason": "high_risk_tool", "payload": {}}))
    )
    assert run.status == "paused_awaiting_confirm"


def test_interrupt_value_tolerates_unknown_shape() -> None:
    rt = EngineRuntime()
    assert rt._interrupt_value(_settle_snapshot()) == {"reason": "unknown", "payload": {}}
    assert rt._interrupt_value(_settle_snapshot("plain-string"))["reason"] == "unknown"
    assert rt._interrupt_value(_settle_snapshot({"reason": "x"}))["reason"] == "x"


def test_emit_await_outcome_splits_expired_from_resolved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """granted/cancelled 走 await_resolved，超时单独成类，方便前端区分。"""
    rt = EngineRuntime()
    events: list[tuple[str, dict[str, Any]]] = []

    async def _emit(run_id: str, event_type: str, payload: dict[str, Any]) -> int:
        events.append((event_type, payload))
        return 1

    async def _timing(run_id: str) -> dict[str, Any]:
        return {"elapsed_ms": 1, "active_ms": 1, "deadline_at": "R"}

    monkeypatch.setattr(rt, "emit_event", _emit)
    monkeypatch.setattr(rt, "_timing_payload_for", _timing)
    base = {"await_id": "a1", "tool": "t", "waited_ms": 900, "deadline_at": "D"}

    asyncio.run(rt._emit_await_outcome("r1", {**base, "status": "granted"}))
    asyncio.run(rt._emit_await_outcome("r1", {**base, "status": "cancelled"}))
    asyncio.run(rt._emit_await_outcome("r1", {**base, "status": "expired"}))

    assert [e[0] for e in events] == [
        "await_resolved",
        "await_resolved",
        "await_expired",
    ]
    assert events[0][1]["status"] == "granted" and events[1][1]["status"] == "cancelled"
    assert events[2][1]["await_id"] == "a1" and events[2][1]["run_deadline_at"] == "R"
    # `deadline_at` 必须是等待超时点（不得被 timing_payload 的 run 截止点覆盖）
    assert [e[1]["deadline_at"] for e in events] == ["D", "D", "D"]
    assert [e[1]["waited_ms"] for e in events] == [900, 900, 900]


def test_resume_run_guard_allows_waiting_external(monkeypatch: pytest.MonkeyPatch) -> None:
    """等待态可被唤醒恢复；其它非暂停状态一律忽略（防误投唤醒）。"""
    rt = EngineRuntime()
    run = _run(status="waiting_external")
    invoked: list[Any] = []

    async def _load(run_id: str) -> Run:
        return run

    async def _set_status(run_id: str, status: str, *a: object, **kw: object) -> dict:
        run.status = status
        return {}

    async def _emit(run_id: str, event_type: str, payload: dict[str, Any]) -> int:
        return 1

    async def _await_outcome(run_id: str, answer: dict[str, Any]) -> None: ...

    async def _invoke(
        run_id: str, thread_id: str, agent_id: str, *, input_payload: Any = None
    ) -> None:
        invoked.append(input_payload)

    monkeypatch.setattr(rt, "_load_run", _load)
    monkeypatch.setattr(rt, "_set_run_status", _set_status)
    monkeypatch.setattr(rt, "emit_event", _emit)
    monkeypatch.setattr(rt, "_emit_await_outcome", _await_outcome)
    monkeypatch.setattr(rt, "_invoke_and_finalize", _invoke)

    answer = {"kind": "await", "await_id": "a1", "status": "granted"}
    asyncio.run(rt._resume_run(str(run.id), answer))
    assert len(invoked) == 1 and run.status == "running"

    run.status = "done"
    asyncio.run(rt._resume_run(str(run.id), answer))
    assert len(invoked) == 1, "非暂停/等待态不得被唤醒恢复"


def test_cancel_run_awaits_swallows_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """收尾撤销失败不得影响 run 落库（返回 0 并记录日志）。"""
    from app.modules.engine import runtime as runtime_mod

    rt = EngineRuntime()

    def _factory() -> Any:
        raise RuntimeError("db down")

    monkeypatch.setattr(runtime_mod, "session_factory", _factory)
    assert asyncio.run(rt._cancel_run_awaits(str(uuid.uuid4()))) == 0


def test_reconcile_orphans_never_kills_waiting_external(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """V9：重启对账只收殓 `running`，等待外部回调的 run 不得被"重启即失败"误杀。"""
    from app.modules.engine import runtime as runtime_mod
    from app.modules.engine.runtime import TERMINAL_RUN_STATUSES
    from app.modules.runs.models import RUN_STATUSES
    from app.modules.tasks.router import NON_TERMINAL_RUN_STATUSES

    db = FakeSession()
    opened: list[FakeSession] = []

    def _factory() -> FakeSession:
        opened.append(db)
        return db

    monkeypatch.setattr(runtime_mod, "session_factory", _factory)
    asyncio.run(EngineRuntime()._reconcile_orphans())

    assert len(opened) == 1 and db.commits == 1
    sql = str(db.executed[0][0])
    assert "status = 'running'" in sql.replace("\n", " ")
    assert "waiting_external" not in sql
    # 注册表三处同步：等待态是「非终态」，不得混进终态集合
    assert "waiting_external" in RUN_STATUSES
    assert "waiting_external" not in TERMINAL_RUN_STATUSES
    assert "waiting_external" in NON_TERMINAL_RUN_STATUSES


def test_await_sweeper_expires_due_waits_and_wakes_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """巡检：把到期等待翻 expired 并投递 resume（不落库则唤醒无门）。"""
    from app.modules.engine import runtime as runtime_mod

    monkeypatch.setattr(settings, "await_sweep_interval_seconds", 0)
    row = _row(status="expired", resolved_at=T0 + timedelta(milliseconds=1500))
    sessions: list[FakeSession] = []

    def _factory() -> FakeSession:
        first = not sessions
        db = FakeSession(scalars_rows=[[str(row.id)], [row]] if first else [[], []])
        sessions.append(db)
        return db

    monkeypatch.setattr(runtime_mod, "session_factory", _factory)
    rt = EngineRuntime()

    async def _run() -> None:
        task = asyncio.create_task(rt._await_sweeper())
        await asyncio.sleep(0.05)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    asyncio.run(_run())

    first = sessions[0]
    assert first.commits == 1 and len(first.executed) == 2
    insert, params = first.executed[0]
    assert "INSERT INTO inbox_events" in str(insert)
    assert params is not None and params["rid"] == row.run_id
    body = json.loads(params["p"])
    assert body["kind"] == "await" and body["status"] == "expired"
    assert "pg_notify" in str(first.executed[1][0])
    # 无到期行时不提交（不产生空事务）
    assert any(s.commits == 0 for s in sessions[1:])


def test_await_sweeper_survives_iteration_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """单轮巡检异常不得退出巡检（否则超时唤醒永久失效）。"""
    from app.modules.engine import runtime as runtime_mod

    monkeypatch.setattr(settings, "await_sweep_interval_seconds", 0)
    row = _row(status="expired", resolved_at=T0)
    sessions: list[FakeSession] = []

    def _factory() -> FakeSession:
        if not sessions:
            sessions.append(FakeSession(fail_on="scalars"))
        elif len(sessions) == 1:
            sessions.append(FakeSession(scalars_rows=[[str(row.id)], [row]]))
        else:
            sessions.append(FakeSession(scalars_rows=[[], []]))
        return sessions[-1]

    monkeypatch.setattr(runtime_mod, "session_factory", _factory)
    rt = EngineRuntime()

    async def _run() -> None:
        task = asyncio.create_task(rt._await_sweeper())
        await asyncio.sleep(0.05)
        assert not task.done(), "巡检异常后必须继续运行"
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    asyncio.run(_run())
    assert len(sessions) >= 2 and sessions[1].commits == 1


# ---------- 5. 回调入口 ----------


def _granted(row: Any) -> None:
    """模拟 ORM refresh 后从 DB 取回已落定行（resolve() 只发 UPDATE，不改内存对象）。"""
    row.status = "granted"
    row.payload_out = {"score": 1}
    row.waited_ms = 300


def test_resolve_endpoint_rejects_bad_token() -> None:
    row = _row()
    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            resolve_await(
                row.id, AwaitResolveIn(callback_token="forged"), FakeSession(get_rows=[row])
            )
        )
    assert exc.value.status_code == 403


def test_resolve_endpoint_404_for_unknown_await() -> None:
    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            resolve_await(
                uuid.uuid4(),
                AwaitResolveIn(callback_token="x"),
                FakeSession(get_rows=[None]),
            )
        )
    assert exc.value.status_code == 404


def test_resolve_endpoint_rejects_idempotency_mismatch() -> None:
    row = _row()
    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            resolve_await(
                row.id,
                AwaitResolveIn(callback_token=row.callback_token, idempotency_key="another-key"),
                FakeSession(get_rows=[row]),
            )
        )
    assert exc.value.status_code == 409


def test_resolve_endpoint_resolves_and_wakes_run() -> None:
    row = _row()
    db = FakeSession(
        get_rows=[row, row],
        scalar_rows=[row.id],
        on_refresh=_granted,
    )
    out = asyncio.run(
        resolve_await(
            row.id,
            AwaitResolveIn(
                callback_token=row.callback_token,
                idempotency_key=row.idempotency_key,
                payload={"score": 1},
            ),
            db,
        )
    )
    assert out.resumed is True and out.status == "granted"
    assert str(out.run_id) == str(row.run_id)
    assert db.commits == 1 and len(db.executed) == 2
    assert "INSERT INTO inbox_events" in str(db.executed[0][0])
    assert json.loads(db.executed[0][1]["p"])["payload"] == {"score": 1}


def test_resolve_endpoint_is_idempotent_on_replay() -> None:
    """重复回调：CAS 落败即只回既有状态，不再投递第二次唤醒。"""
    row = _row()
    db = FakeSession(get_rows=[row, row], scalar_rows=[None])
    out = asyncio.run(resolve_await(row.id, AwaitResolveIn(callback_token=row.callback_token), db))
    assert out.resumed is False
    assert db.executed == [] and db.commits == 1
