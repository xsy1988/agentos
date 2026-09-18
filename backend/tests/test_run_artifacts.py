"""结果一等公民（方案 §4 P0-5）：结果信封 + 产物表 + 卡片事件 + 历史重放。

覆盖四条不变量：
1. **任意终态都有信封**：`_finalize` 是终态唯一出口，result 绝不 NULL，
   且 schema/outcome/metrics 字段齐全；
2. **长结果先外置**：超阈值文本落 `run_artifacts`，信封只留「引用行 + 预览」，
   外置失败降级为内联（产物是优化手段，不能反过来毁掉结果）；
3. **卡片可重放**：结果卡以 `card` 事件进事件流，从事件流重建即等于实时结果；
4. **压缩保真**：L1 折叠只压正文，产物引用行原样保留（Q-05 核心回归）。
"""

import asyncio
import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from langchain_core.messages import AnyMessage, ToolMessage

from app.core.config import settings
from app.modules.engine import artifacts as artifacts_mod
from app.modules.engine import runtime as runtime_mod
from app.modules.engine.runtime import EngineRuntime
from app.modules.runs.models import Run
from tests.support.fake_db import RecordingSession

ENVELOPE_KEYS = {"schema", "outcome", "text", "cards", "artifacts", "metrics"}
METRIC_KEYS = {
    "elapsed_ms",
    "active_ms",
    "iterations",
    "tool_calls",
    "input_tokens",
    "output_tokens",
}


# ---------- 测试替身 ----------


class _FakeBackend:
    """只实现 P0-5 用到的产物接口；`fail=True` 模拟产物存储不可用。"""

    def __init__(self) -> None:
        self.rows: dict[str, dict] = {}
        self.fail = False

    async def save_artifact(
        self,
        run_id: str,
        *,
        kind: str,
        name: str | None,
        mime: str | None,
        size: int,
        storage: str,
        payload: object,
        idempotency_key: str | None = None,
        task_id: str | None = None,
        step_id: str | None = None,
    ) -> dict:
        if self.fail:
            raise RuntimeError("artifact store down")
        key = idempotency_key or uuid4().hex
        if key in self.rows:  # 幂等：同内容重复外置只落一行
            return self.rows[key]
        row = {
            "id": str(uuid4()),
            "run_id": run_id,
            "kind": kind,
            "name": name,
            "mime": mime,
            "size": size,
            "storage": storage,
            "payload": payload,
            "task_id": task_id,
            "step_id": step_id,
        }
        self.rows[key] = row
        return row

    async def list_artifacts(self, run_id: str) -> list[dict]:
        return [r for r in self.rows.values() if r["run_id"] == run_id]


class _Hooks:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def on_run_end(self, ctx: object, status: str, result: dict) -> None:
        self.calls.append((status, result))


class _Rig:
    def __init__(self, run: Run, rt: EngineRuntime, db: RecordingSession, events: list) -> None:
        self.run = run
        self.rt = rt
        self.db = db
        self.events = events

    def types(self) -> list[str]:
        return [e[0] for e in self.events]

    def payload(self, event_type: str) -> dict:
        return next(p for t, p in self.events if t == event_type)


def _rig(monkeypatch: pytest.MonkeyPatch, *, input_payload: dict | None = None) -> _Rig:
    rt = EngineRuntime()
    run = Run(
        id=uuid4(),
        status="running",
        trigger="manual",
        input=input_payload if input_payload is not None else {},
        conversation_id=None,  # 定时任务路径：终态顺带落通知，不依赖会话
        budget={},
        budget_used={},
        active_ms=0,
        started_at=datetime.now(UTC),
        deadline_at=None,
    )
    db = RecordingSession(run)
    monkeypatch.setattr(runtime_mod, "session_factory", lambda: db)
    rt.backend = _FakeBackend()  # type: ignore[assignment]
    rt.hooks = _Hooks()
    events: list[tuple[str, dict]] = []

    async def _emit(run_id: str, event_type: str, payload: dict) -> int:
        events.append((event_type, payload))
        return len(events)

    monkeypatch.setattr(rt, "emit_event", _emit)
    ctx = rt._build_ctx(run)
    ctx.budget.update({"iterations": 4, "tool_calls": 9, "input_tokens": 120, "output_tokens": 340})
    return _Rig(run, rt, db, events)


# ---------- 1. 任意终态都有信封 ----------


def test_finalize_writes_envelope(monkeypatch: pytest.MonkeyPatch) -> None:
    """done 路径：result 为 v1 信封，字段齐全，且状态/时间戳一并落库。"""
    rig = _rig(monkeypatch)
    asyncio.run(rig.rt._finalize(str(rig.run.id), "done", "已完成采购对比"))

    result = rig.run.result
    assert set(result) >= ENVELOPE_KEYS
    assert result["schema"] == "run_result/v1"
    assert result["outcome"] == "done"
    assert result["text"] == "已完成采购对比"
    assert result["cards"] == [] and result["artifacts"] == []
    assert set(result["metrics"]) == METRIC_KEYS
    assert result["metrics"]["iterations"] == 4
    assert result["metrics"]["tool_calls"] == 9
    assert isinstance(result["metrics"]["elapsed_ms"], int)

    assert rig.run.status == "done"
    assert rig.run.finished_at is not None
    assert rig.run.paused_at is None
    assert rig.run.budget_used == {
        "iterations": 4,
        "tool_calls": 9,
        "input_tokens": 120,
        "output_tokens": 340,
    }


def test_finalize_keeps_cards_argument(monkeypatch: pytest.MonkeyPatch) -> None:
    """`cards` 入参通道：过程卡将来迁移到 card 事件前，先由信封承载。"""
    rig = _rig(monkeypatch)
    card = {"card_type": "plan_review", "payload": {"plan": [{"seq": 1, "text": "查价"}]}}
    asyncio.run(rig.rt._finalize(str(rig.run.id), "done", "终答", cards=[card]))

    assert rig.run.result["cards"] == [card]
    assert rig.payload("card")["payload"]["cards"] == [card]  # 重放同源


def test_finalize_always_emits_card_then_terminal_status(monkeypatch: pytest.MonkeyPatch) -> None:
    """卡片先进事件流（与事件序对齐），终态 run_status 再带 outcome/result_ref。"""
    rig = _rig(monkeypatch)
    asyncio.run(rig.rt._finalize(str(rig.run.id), "done", "终答"))

    assert rig.types() == ["card", "run_status"]
    card = rig.payload("card")
    assert card["card_type"] == "result"
    assert card["artifact_id"] is None
    # 历史重放：card 事件里的信封即落库信封，逐字段相等（无损）
    assert card["payload"] == rig.run.result

    status = rig.payload("run_status")
    assert status["status"] == "done"
    assert status["outcome"] == "done"
    assert status["result_ref"]["outcome"] == "done"
    assert status["result_ref"]["text_chars"] == len("终答")
    assert status["result_ref"]["artifact_ids"] == []
    assert "deadline_at" in status  # 时长字段与 P0-1 账本同一来源


def test_finalize_failure_paths_never_leave_result_null(monkeypatch: pytest.MonkeyPatch) -> None:
    """失败/取消也有信封：outcome 与 reason 由调用路径显式给定。"""
    cases = [
        ("failed", "failed", "timeout", {"partial": True}),
        ("cancelled", "failed", "aborted", {"partial": True}),
        ("failed", "failed", "budget_exceeded", {"partial": True, "gate": "max_iterations"}),
    ]
    for status, outcome, reason, extra in cases:
        rig = _rig(monkeypatch)
        asyncio.run(
            rig.rt._finalize(
                str(rig.run.id), status, "半截结论", outcome=outcome, reason=reason, extra=extra
            )
        )
        result = rig.run.result
        assert result is not None and result["schema"] == "run_result/v1"
        assert result["outcome"] == outcome
        assert result["reason"] == reason
        assert result["text"] == "半截结论"
        assert result["metrics"]["elapsed_ms"] >= 0  # 未空跑时长检查
        assert rig.run.status == status


def test_verify_not_achieved_marks_partial_without_changing_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验收未达成：run 仍是 done（状态机不动），只有结果语义降为 partial。"""
    rig = _rig(monkeypatch)
    asyncio.run(rig.rt._finalize(str(rig.run.id), "done", "自评未过", achieved=False))

    assert rig.run.status == "done"
    assert rig.run.result["outcome"] == "partial"
    assert rig.run.result["reason"] == "verify_not_achieved"


# ---------- 2. 长结果先外置 ----------


def test_finalize_externalizes_long_text(monkeypatch: pytest.MonkeyPatch) -> None:
    """超阈值终答：落 run_artifacts，信封 text = 引用行 + 预览，artifacts 带 id。"""
    monkeypatch.setattr(settings, "artifact_inline_max_chars", 40)
    monkeypatch.setattr(settings, "artifact_preview_chars", 20)
    rig = _rig(monkeypatch)
    long_text = "采购报价对比清单：" + "甲供应商报价 128 万；" * 20

    asyncio.run(rig.rt._finalize(str(rig.run.id), "done", long_text))

    envelope = rig.run.result
    assert len(envelope["artifacts"]) == 1
    brief = envelope["artifacts"][0]
    assert set(brief) == {"id", "kind", "name", "mime", "size"}
    assert brief["kind"] == "text"
    assert brief["size"] == len(long_text)
    assert len(envelope["text"]) < len(long_text)
    assert envelope["text"].startswith(f"[artifact id={brief['id']} kind=text")
    assert "完整内容见上方产物引用" in envelope["text"]

    rows = asyncio.run(rig.rt.backend.list_artifacts(str(rig.run.id)))
    assert len(rows) == 1
    assert rows[0]["payload"] == {"text": long_text}  # 正文在表里，不在信封
    assert rig.payload("card")["artifact_id"] == brief["id"]
    assert rig.payload("run_status")["result_ref"]["artifact_ids"] == [brief["id"]]


def test_externalize_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    """同一 run 内同内容重复外置（重放/重试）只落一行。"""
    monkeypatch.setattr(settings, "artifact_inline_max_chars", 10)
    rig = _rig(monkeypatch)
    text = "同样的长结果" * 10

    first = asyncio.run(rig.rt._externalize(str(rig.run.id), text, name="报告"))
    second = asyncio.run(rig.rt._externalize(str(rig.run.id), text, name="报告"))
    assert first is not None and second is not None
    assert first[1]["id"] == second[1]["id"]
    assert len(asyncio.run(rig.rt.backend.list_artifacts(str(rig.run.id)))) == 1


def test_externalize_records_task_and_step_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    """产物归属（P0-5 收尾）：上下文里的任务/子任务随外置写进 run_artifacts。

    归属是任务级产物视图与 step 归组的唯一依据，必须由运行上下文写入而非调用方手工传参。
    """
    monkeypatch.setattr(settings, "artifact_inline_max_chars", 10)
    rig = _rig(monkeypatch)
    ctx = rig.rt.get_run_ctx(str(rig.run.id))
    ctx.task_id = "11111111-1111-4111-8111-111111111111"
    ctx.step_id = "22222222-2222-4222-8222-222222222222"

    asyncio.run(rig.rt._externalize(str(rig.run.id), "很长的结果" * 5, name="报告"))

    row = asyncio.run(rig.rt.backend.list_artifacts(str(rig.run.id)))[0]
    assert row["task_id"] == ctx.task_id
    assert row["step_id"] == ctx.step_id


def test_externalize_without_scope_leaves_it_null(monkeypatch: pytest.MonkeyPatch) -> None:
    """无归属（定时任务/未挂任务的 run）：留 NULL，且不影响产物本身落地。"""
    monkeypatch.setattr(settings, "artifact_inline_max_chars", 10)
    rig = _rig(monkeypatch)

    asyncio.run(rig.rt._externalize(str(rig.run.id), "很长的结果" * 5, name="报告"))

    row = asyncio.run(rig.rt.backend.list_artifacts(str(rig.run.id)))[0]
    assert row["task_id"] is None and row["step_id"] is None
    assert row["kind"] == "text"


def test_externalize_failure_degrades_to_inline(monkeypatch: pytest.MonkeyPatch) -> None:
    """产物存储不可用：结果降级为内联，绝不因外置失败而丢结果。"""
    monkeypatch.setattr(settings, "artifact_inline_max_chars", 10)
    rig = _rig(monkeypatch)
    rig.rt.backend.fail = True  # type: ignore[attr-defined]
    text = "很长的结果" * 20

    asyncio.run(rig.rt._finalize(str(rig.run.id), "done", text))

    assert rig.run.result["text"] == text
    assert rig.run.result["artifacts"] == []
    assert rig.run.result["schema"] == "run_result/v1"


def test_save_long_output_keeps_short_content_and_externalizes_long(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """工具观察：未超阈值原样返回（不产生噪音产物），超阈值才外置。"""
    monkeypatch.setattr(settings, "artifact_inline_max_chars", 50)
    monkeypatch.setattr(settings, "artifact_preview_chars", 10)
    rig = _rig(monkeypatch)
    run_id = str(rig.run.id)

    short = asyncio.run(rig.rt.save_long_output(run_id, "接口返回 ok", name="procurement_status"))
    assert short == "接口返回 ok"
    assert asyncio.run(rig.rt.backend.list_artifacts(run_id)) == []

    long_text = json.dumps({"rows": [{"供应商": f"供应商{i}", "金额": i} for i in range(30)]})
    content = asyncio.run(rig.rt.save_long_output(run_id, long_text, name="procurement_status"))
    assert isinstance(content, str) and content.startswith("[artifact id=")
    rows = asyncio.run(rig.rt.backend.list_artifacts(run_id))
    assert len(rows) == 1
    assert rows[0]["name"] == "procurement_status"
    assert rows[0]["kind"] == "json"  # JSON 结构化内容按 json 存，前端按结构渲染
    assert rows[0]["payload"] == json.loads(long_text)


def test_save_long_output_failure_does_not_block_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    """外置失败时工具观察保持内联——产物是优化手段，不能阻断工具调用。"""
    monkeypatch.setattr(settings, "artifact_inline_max_chars", 10)
    rig = _rig(monkeypatch)
    rig.rt.backend.fail = True  # type: ignore[attr-defined]
    text = "长观察" * 50

    assert asyncio.run(rig.rt.save_long_output(str(rig.run.id), text, name="t")) == text


# ---------- 3. 压缩保真（Q-05 核心回归） ----------


def test_fold_brief_keeps_artifact_ref_line() -> None:
    """L1 折叠：正文被截断，引用行逐字保留，可凭 id 找回完整内容。"""
    ref = '[artifact id=6f1a kind=text name="采购报价对比" size=9000]'
    body = "甲供应商报价 128 万；" * 40
    brief = artifacts_mod.fold_brief(f"{ref}\n{body}", keep_chars=120)

    assert ref in brief
    assert len(brief) < len(body)
    refs = artifacts_mod.parse_artifact_refs(brief)
    assert refs == [{"id": "6f1a", "kind": "text", "name": "采购报价对比", "size": "9000"}]


def test_compact_l1_preserves_refs_in_tool_message(monkeypatch: pytest.MonkeyPatch) -> None:
    """上下文压缩后仍能定位产物：真跑 L1 折叠，引用行在折叠结果里完好。"""
    from app.modules.discovery import assembler

    monkeypatch.setattr(settings, "context_compact_threshold", 10_000)
    ref = '[artifact id=ab12 kind=json name="报价明细" size=12345]'
    messages: list[AnyMessage] = [
        ToolMessage(
            content=f"{ref}\n{json.dumps({'rows': list(range(8000))}, ensure_ascii=False)}",
            tool_call_id="c0",
            id="m0",
        ),
        *[ToolMessage(content="x" * 3000, tool_call_id=f"c{i}", id=f"m{i}") for i in range(1, 5)],
    ]
    emitted: list[tuple[str, dict]] = []

    async def _emit(event_type: str, payload: dict) -> None:
        emitted.append((event_type, payload))

    ops = asyncio.run(assembler.compact_messages(None, messages, _emit))
    assert ops is not None and emitted[0][0] == "context_compacted"

    folded = next(op for op in ops if getattr(op, "id", None) == "m0")
    assert ref in str(folded.content)  # 引用行逐字保留
    assert folded.content.count("…") == 1  # 正文确实被折叠
    assert len(str(folded.content)) < 300
    assert artifacts_mod.parse_artifact_refs(str(folded.content))[0]["id"] == "ab12"
    # 未折叠的最近观察不受影响
    assert [op for op in ops if getattr(op, "id", None) == "m4"] == []


def test_ref_line_parses_quoted_name_and_size() -> None:
    """引用行是唯一的跨层契约：写出后必须能被解析回定位所需字段。"""
    line = artifacts_mod.ref_line({"id": "x-1", "kind": "json", "name": "报价清单", "size": 42})
    assert line == '[artifact id=x-1 kind=json name="报价清单" size=42]'
    refs = artifacts_mod.parse_artifact_refs(line)
    assert refs == [{"id": "x-1", "kind": "json", "name": "报价清单", "size": "42"}]
    assert artifacts_mod.parse_artifact_refs("无引用") == []
    # 名称里的引号不破坏解析（内部引号被替换，字段边界仍然成立）
    messy = artifacts_mod.ref_line({"id": "y", "kind": "text", "name": 'a"b', "size": 1})
    assert artifacts_mod.parse_artifact_refs(messy)[0]["id"] == "y"


# ---------- 4. 历史重放 ----------


def test_result_card_replay_matches_live_render(monkeypatch: pytest.MonkeyPatch) -> None:
    """重放契约：只凭 run_events（GET /runs/{id}/events）即可重建结果卡，无需回拉 run。"""
    monkeypatch.setattr(settings, "artifact_inline_max_chars", 20)
    rig = _rig(monkeypatch)
    text = "历史 run 的长结果" * 10
    asyncio.run(rig.rt._finalize(str(rig.run.id), "done", text))

    # 事件流按 seq 升序（emit 顺序即 seq 顺序）：重放侧只看到 event_type + payload
    replayed = [
        (seq, event_type, payload)
        for seq, (event_type, payload) in enumerate(rig.events, start=1)
        if event_type == "card"
    ]
    cards = [p for _, _, p in replayed if p["card_type"] == "result"]
    assert len(cards) == 1
    replayed_envelope = cards[0]["payload"]
    # 无损：重建出的信封与实时落库结果一致，产物引用可直接去 /artifacts/{id} 取正文
    assert replayed_envelope == rig.run.result
    assert replayed_envelope["artifacts"][0]["id"] == cards[0]["artifact_id"]
    assert replayed_envelope["outcome"] == "done"
