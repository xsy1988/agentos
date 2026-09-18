"""Worker 输入契约与预检门（方案 §5 P1-4）。

红线：必需输入缺失时，**模型与图都不该被调用**——不许把「缺输入」翻译成
「让模型自己猜/自己问」。覆盖四层：

1. 契约层（inputs）：声明解析/落盘 round-trip/调用方取值归一化/取值判定与缺失计算；
2. 注册层（registry）：create/ensure/publish/子任务全链路 inputs 落盘 + 非法声明拒绝；
3. 预检层（preflight）：取值只来自确定性事实（显式取值 / 会话附件 / 历史快照累积）；
4. 执行层（runtime）：缺失 → 结构化 `missing_inputs` 失败且图未调用；齐备 → 契约进固定区。
"""

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from app.modules.engine import runtime as runtime_mod
from app.modules.engine.runtime import EngineRuntime
from app.modules.runs.models import Run
from app.modules.workers import preflight, registry
from app.modules.workers.inputs import (
    MAX_INPUTS,
    MAX_VALUE_CHARS,
    InputContractError,
    InputSpec,
    dump_input_specs,
    missing_inputs_text,
    parse_input_specs,
    render_input_contract,
    resolve_inputs,
    sanitize_provided_inputs,
)
from app.modules.workers.preflight import RunInputResolution, resolve_run_inputs
from app.modules.workers.registry import WorkerError
from tests.support.fake_db import RecordingSession, RoutingSession

SPECS_RAW = [
    {
        "name": "quote_file",
        "type": "file",
        "required": True,
        "description": "待比价的报价单",
        "example": "报价单-2026Q1.xlsx",
    },
    {"name": "budget", "type": "number", "required": False, "description": "预算上限"},
]


@pytest.fixture()
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """把 registry 的文件根指到临时目录，测试互不污染。"""
    monkeypatch.setattr(registry.settings, "data_dir", tmp_path)
    registry._invalidate_all()
    yield tmp_path
    registry._invalidate_all()


def _mk_worker(name: str = "报价对比", *, inputs: list[dict] | None = None) -> None:
    registry.create_worker(name, description="对比供应商报价", inputs=inputs or SPECS_RAW)


def _specs() -> list[InputSpec]:
    return parse_input_specs(SPECS_RAW, scope="Worker「报价对比」")


# ---------- 1. 契约层 ----------


def test_parse_input_specs_defaults_and_dump_roundtrip() -> None:
    specs = parse_input_specs([{"name": "topic"}], scope="W")
    assert specs == [InputSpec("topic", "text", False, "", "")]
    # 只落非空字段；type 缺省补 text；round-trip 稳定
    reparse = parse_input_specs(dump_input_specs(specs), scope="W")
    assert dump_input_specs(reparse) == dump_input_specs(specs)
    assert dump_input_specs(specs) == [{"name": "topic", "type": "text"}]
    assert parse_input_specs(None, scope="W") == []


@pytest.mark.parametrize(
    "raw, keyword",
    [
        ("quote_file", "必须是列表"),
        ([{"name": "ok"}, {"name": "ok"}], "重复"),
        ([{**SPECS_RAW[1], "type": "money"}], "不支持"),
        ([{"name": "Quote_File"}], "非法"),
        ([{"name": "x", "unknown_field": 1}], "未知字段"),
        ([{"name": "x", "required": "yes"}], "必须是布尔值"),
        ([{"name": "x", "description": "长" * 501}], "超过 500"),
        ([{"name": "x", "example": "长" * 201}], "超过 200"),
        (["not-a-mapping"], "键值结构"),
        ([{"name": "x"}] * (MAX_INPUTS + 1), f"最多 {MAX_INPUTS}"),
    ],
)
def test_parse_input_specs_rejects_bad_declarations(raw: object, keyword: str) -> None:
    with pytest.raises(InputContractError, match=keyword):
        parse_input_specs(raw, scope="Worker「报价对比」")


def test_sanitize_provided_inputs_normalizes_and_rejects() -> None:
    assert sanitize_provided_inputs(None) == {}
    assert sanitize_provided_inputs({"budget": 0}) == {"budget": "0"}
    assert sanitize_provided_inputs({"ok": True, "name": "  张三  ", "empty": None}) == {
        "ok": "true",
        "name": "张三",
    }
    with pytest.raises(InputContractError, match="必须是对象"):
        sanitize_provided_inputs(["quote_file"])
    with pytest.raises(InputContractError, match="非法"):
        sanitize_provided_inputs({"坏 名字": "x"})
    with pytest.raises(InputContractError, match="只接受文本"):
        sanitize_provided_inputs({"topic": {"a": 1}})
    with pytest.raises(InputContractError, match="超过"):
        sanitize_provided_inputs({"topic": "长" * (MAX_VALUE_CHARS + 1)})


def test_resolve_inputs_precedence_and_missing() -> None:
    specs = _specs()
    values, missing = resolve_inputs(specs, {"quote_file": "q.xlsx", "budget": "100"})
    assert values == {"quote_file": "q.xlsx", "budget": "100"} and missing == []

    # 附件顶替 file 类型；未声明键忽略（契约外输入不参与判定）
    values, missing = resolve_inputs(specs, {"未声明": "x"}, ["报价单.xlsx"])
    assert values == {"quote_file": "报价单.xlsx"} and missing == []

    # 显式取值优先于附件；必填缺失被报出
    values, missing = resolve_inputs(specs, {"quote_file": "显式.xlsx"}, ["附件.xlsx"])
    assert values == {"quote_file": "显式.xlsx"} and missing == []
    values, missing = resolve_inputs(specs, {}, [])
    assert values == {} and [s.name for s in missing] == ["quote_file"]

    # 空串不算提供；可选缺失不进缺失清单
    values, missing = resolve_inputs(specs, {"quote_file": "   "}, [])
    assert values == {} and [s.name for s in missing] == ["quote_file"]


def _file_specs(*names: str) -> list[InputSpec]:
    return parse_input_specs(
        [{"name": n, "type": "file", "required": True} for n in names], scope="W"
    )


def test_resolve_inputs_multi_file_shares_attachment() -> None:
    specs = _file_specs("quote_file", "contract_file", "budget_sheet")

    # 附件充足：按声明顺序一对一顶替，不共享
    values, missing = resolve_inputs(specs, {}, ["a.pdf", "b.pdf", "c.xlsx"])
    assert values == {"quote_file": "a.pdf", "contract_file": "b.pdf", "budget_sheet": "c.xlsx"}
    assert missing == []

    # 附件不足：剩余的 file 输入共享第一个附件（用户只附了一件材料）
    values, missing = resolve_inputs(specs, {}, ["a.pdf"])
    assert values == {
        "quote_file": "a.pdf",
        "contract_file": "a.pdf",
        "budget_sheet": "a.pdf",
    }
    assert missing == []

    # 部分充足：先一对一分发，用尽后共享第一个附件
    values, missing = resolve_inputs(specs, {}, ["a.pdf", "b.pdf"])
    assert values == {
        "quote_file": "a.pdf",
        "contract_file": "b.pdf",
        "budget_sheet": "a.pdf",
    }
    assert missing == []

    # 显式取值不消费附件，附件留给其余 file 输入
    values, missing = resolve_inputs(specs, {"quote_file": "显式.pdf"}, ["a.pdf"])
    assert values == {"quote_file": "显式.pdf", "contract_file": "a.pdf", "budget_sheet": "a.pdf"}
    assert missing == []

    # 一件附件都没有：仍报缺失（共享不凭证空附件兜底）
    values, missing = resolve_inputs(specs, {}, [])
    assert values == {} and [s.name for s in missing] == [
        "quote_file",
        "contract_file",
        "budget_sheet",
    ]


def test_render_contract_marks_shared_attachment() -> None:
    specs = _file_specs("quote_file", "contract_file")
    # 各自独立附件：不标注
    text = render_input_contract(specs, {"quote_file": "a.pdf", "contract_file": "b.pdf"})
    assert "共用同一附件" not in text

    text = render_input_contract(specs, {"quote_file": "a.pdf", "contract_file": "a.pdf"})
    assert "  已提供：a.pdf（与 contract_file 共用同一附件）" in text
    assert "  已提供：a.pdf（与 quote_file 共用同一附件）" in text

    text = render_input_contract(specs, resolve_inputs(specs, {}, ["a.pdf"])[0])
    assert text.count("共用同一附件") == 2


def test_render_contract_and_missing_text() -> None:
    specs = _specs()
    assert render_input_contract([], {}) == ""
    text = render_input_contract(specs, {"quote_file": "q.xlsx"})
    assert "输入契约" in text
    assert "- quote_file（file，必填）：待比价的报价单（示例：报价单-2026Q1.xlsx）" in text
    assert "  已提供：q.xlsx" in text and "  未提供（可选）" in text

    hint = missing_inputs_text([specs[0]])
    assert "调用模型前拦截" in hint and "quote_file" in hint and "附带该文件" in hint


# ---------- 2. 注册层 ----------


def test_create_worker_persists_inputs(root: Path) -> None:
    _mk_worker()
    wdef = registry.get_def("报价对比", "v1")
    assert wdef is not None
    assert [s.name for s in wdef.inputs] == ["quote_file", "budget"]
    assert wdef.inputs[0].type == "file" and wdef.inputs[0].required is True
    # 落盘 → 重新解析一致（round-trip）
    content = registry.read_version_file("报价对比", "v1", "WORKER.md")
    assert "inputs:" in content
    assert parse_input_specs(dump_input_specs(wdef.inputs), scope="W") == wdef.inputs


def test_create_worker_rejects_bad_inputs(root: Path) -> None:
    with pytest.raises(WorkerError, match="不支持"):
        _mk_worker(inputs=[{"name": "x", "type": "money"}])
    # 拒绝即不留半成品目录
    assert registry.get_meta("报价对比") is None


def test_write_worker_md_validates_inputs(root: Path) -> None:
    _mk_worker()
    bad = "---\nname: 报价对比\ninputs:\n  - name: quote_file\n    type: 未知\n---\n\n正文\n"
    with pytest.raises(WorkerError, match="不支持"):
        registry.write_version_file("报价对比", "v1", "WORKER.md", bad)
    sub_bad = (
        "---\nname: 拉取报价\nkind: main\n"
        "inputs:\n  - name: quote_file\n    required: 1\n---\n\n正文\n"
    )
    with pytest.raises(WorkerError, match="布尔值"):
        registry.write_version_file("报价对比", "v1", "sub_workers/拉取报价/WORKER.md", sub_bad)


def test_create_sub_worker_persists_inputs(root: Path) -> None:
    _mk_worker()
    registry.create_sub_worker(
        "报价对比",
        "v1",
        "拉取报价",
        inputs=[{"name": "quote_file", "type": "file", "required": True}],
    )
    wdef = registry.get_def("报价对比", "v1")
    assert wdef is not None
    sub = wdef.find_sub("拉取报价")
    assert sub is not None and [s.name for s in sub.inputs] == ["quote_file"]
    with pytest.raises(WorkerError, match="重复"):
        registry.create_sub_worker(
            "报价对比", "v1", "另一子任务", inputs=[{"name": "a"}, {"name": "a"}]
        )


def test_ensure_and_publish_persist_inputs(root: Path) -> None:
    registry.ensure_worker(
        "采购",
        description="d1",
        inputs=SPECS_RAW,
        sub_workers=[
            {
                "name": "确认需求",
                "seq": 1,
                "kind": "main",
                "playbook": "# 步骤\n",
                "inputs": [{"name": "budget", "type": "number", "required": True}],
            }
        ],
    )
    wdef = registry.get_def("采购")
    assert wdef is not None and [s.name for s in wdef.inputs] == ["quote_file", "budget"]
    sub = wdef.find_sub("确认需求")
    assert sub is not None and [s.name for s in sub.inputs] == ["budget"]

    # 幂等 seed：已存在不覆盖（含 inputs 被用户改过的情形）
    registry.ensure_worker("采购", description="d2", inputs=[])
    wdef = registry.get_def("采购")
    assert wdef is not None and len(wdef.inputs) == 2

    registry.publish_version(
        "采购", description="d3", inputs=[{"name": "vendor", "required": True}]
    )
    latest = registry.get_def("采购")
    assert latest is not None and [s.name for s in latest.inputs] == ["vendor"]
    assert latest.version != wdef.version


def test_ensure_worker_rejects_bad_sub_inputs(root: Path) -> None:
    with pytest.raises(WorkerError, match="未知字段"):
        registry.ensure_worker(
            "采购",
            sub_workers=[{"name": "确认需求", "seq": 1, "inputs": [{"name": "a", "typo": 1}]}],
        )


# ---------- 3. 预检层 ----------


def _db(snapshots: list[dict], files: dict[object, str] | None = None) -> RoutingSession:
    """只读替身：`files` 查询给 (附件 id, 文件名)，其余语句（runs 快照）给 `snapshots`。"""
    files = files or {}
    pairs = [(fid, name) for fid, name in files.items()]

    def route(stmt: object) -> list[object] | None:
        return pairs if "files" in str(stmt) else None

    return RoutingSession(router=route, execute_rows=list(snapshots))


def _run(*, payload: dict, conversation_id: object = None, created_at: object = None) -> Run:
    return Run(
        id=uuid4(),
        status="pending",
        trigger="manual",
        input=payload,
        budget={"timeout_seconds": 600},
        budget_used={},
        active_ms=0,
        conversation_id=conversation_id,
        created_at=created_at,
    )


def test_resolve_run_inputs_skips_when_no_contract(root: Path) -> None:
    _mk_worker()
    # 无 worker_name（通用任务）
    assert asyncio.run(resolve_run_inputs(_run(payload={}), db=_db([]))) is None
    # 未声明 inputs 的 Worker
    registry.create_worker("通用")
    run = _run(payload={"worker_name": "通用", "worker_version": "v1"})
    assert asyncio.run(resolve_run_inputs(run, db=_db([]))) is None


def test_resolve_run_inputs_blocks_and_reports_missing(root: Path) -> None:
    _mk_worker()
    run = _run(payload={"worker_name": "报价对比", "worker_version": "v1"})
    res = asyncio.run(resolve_run_inputs(run, db=_db([])))
    assert res is not None and res.ok is False
    assert res.missing_names == ["quote_file"]
    assert res.worker_name == "报价对比"
    # 契约文本照常渲染，但 ok=False 时不会注入（runtime 只在放行时透传）
    assert "# 输入契约" in res.contract
    assert "quote_file" in res.message
    assert [i["provided"] for i in res.inputs_payload()] == [False, False]


def test_resolve_run_inputs_uses_explicit_values_and_attachments(root: Path) -> None:
    _mk_worker()
    fid = uuid4()
    run = _run(
        payload={
            "worker_name": "报价对比",
            "worker_version": "v1",
            "inputs": {"budget": 500},
            "attachment_ids": [str(fid)],
        }
    )
    res = asyncio.run(resolve_run_inputs(run, db=_db([run.input], {fid: "报价单.xlsx"})))
    assert res is not None and res.ok is True
    assert res.values == {"quote_file": "报价单.xlsx", "budget": "500"}
    assert "已提供：报价单.xlsx" in res.contract


def test_resolve_run_inputs_accumulates_across_session(root: Path) -> None:
    """跨轮累积：第一轮给的取值，后续轮次不该被拦（历史快照时间升序合并）。"""
    _mk_worker()
    base = datetime.now(UTC) - timedelta(minutes=10)
    history = [
        {"worker_name": "报价对比", "inputs": {"quote_file": "第一轮.xlsx"}},
        {"worker_name": "报价对比", "inputs": {"budget": "800"}},
    ]
    run = _run(
        payload={"worker_name": "报价对比", "worker_version": "v1"},
        conversation_id=uuid4(),
        created_at=base + timedelta(minutes=5),
    )
    res = asyncio.run(resolve_run_inputs(run, db=_db(history)))
    assert res is not None and res.ok is True
    assert res.values == {"quote_file": "第一轮.xlsx", "budget": "800"}


def test_resolve_run_inputs_survives_unflushed_run(root: Path) -> None:
    """本 run 尚未进查询结果（未 flush）时，兜底追加自己的取值。"""
    _mk_worker()
    run = _run(
        payload={
            "worker_name": "报价对比",
            "worker_version": "v1",
            "inputs": {"quote_file": "本次.xlsx"},
        },
        conversation_id=uuid4(),
        created_at=datetime.now(UTC),
    )
    res = asyncio.run(resolve_run_inputs(run, db=_db([])))
    assert res is not None and res.ok is True and res.values["quote_file"] == "本次.xlsx"


def test_resolve_run_inputs_tolerates_missing_attachment(root: Path) -> None:
    """附件已删 → 跳过（不阻断其余输入判定），仍报缺失。"""
    _mk_worker()
    run = _run(
        payload={
            "worker_name": "报价对比",
            "worker_version": "v1",
            "attachment_ids": [str(uuid4())],
        }
    )
    res = asyncio.run(resolve_run_inputs(run, db=_db([run.input], {})))
    assert res is not None and res.ok is False and res.missing_names == ["quote_file"]


def test_resolution_ok_property() -> None:
    spec = _specs()[0]
    res = RunInputResolution(
        worker_name="w", worker_version="v1", specs=[spec], values={}, missing=[], contract="c"
    )
    assert res.ok is True and res.missing_names == []


# ---------- 4. 执行层 ----------


class _Stop(Exception):
    """停在图入口的哨兵异常（避免跑完整个收尾链）。"""


class _RecordingGraph:
    def __init__(self) -> None:
        self.called = False
        self.config: dict | None = None

    async def ainvoke(self, payload: object, config: dict) -> dict:
        self.called = True
        self.config = config
        raise _Stop()


def _install(monkeypatch: pytest.MonkeyPatch, rt: EngineRuntime, run: Run) -> None:
    async def _load(run_id: str) -> Run:
        return run

    monkeypatch.setattr(rt, "_load_run", _load)
    monkeypatch.setattr(runtime_mod, "session_factory", lambda: RecordingSession(get_row=run))


def _resolution(*, ok: bool) -> RunInputResolution:
    specs = _specs()
    missing = [] if ok else [specs[0]]
    return RunInputResolution(
        worker_name="报价对比",
        worker_version="v1",
        specs=specs,
        values={"quote_file": "q.xlsx"},
        missing=missing,
        contract=render_input_contract(specs, {"quote_file": "q.xlsx"}),
    )


def test_invoke_gate_blocks_before_graph(monkeypatch: pytest.MonkeyPatch) -> None:
    """缺必需输入 → 图不调用、模型不调用，落结构化 missing_inputs。"""
    rt = EngineRuntime()
    run = _run(payload={"worker_name": "报价对比", "worker_version": "v1"})
    run.started_at = datetime.now(UTC) - timedelta(seconds=5)
    run.deadline_at = datetime.now(UTC) + timedelta(seconds=595)
    _install(monkeypatch, rt, run)
    rt.graph = _RecordingGraph()  # type: ignore[assignment]

    async def _resolve(run: Run, *, db: object = None) -> RunInputResolution:
        return _resolution(ok=False)

    monkeypatch.setattr(preflight, "resolve_run_inputs", _resolve)

    events: list[tuple[str, dict]] = []
    finalized: list[tuple[str, str, str, dict]] = []

    async def _emit(run_id: str, event_type: str, payload: dict) -> int:
        events.append((event_type, payload))
        return len(events)

    async def _finalize(run_id: str, status: str, text: str, **kwargs: object) -> None:
        finalized.append((run_id, status, text, kwargs))

    monkeypatch.setattr(rt, "emit_event", _emit)
    monkeypatch.setattr(rt, "_finalize", _finalize)
    monkeypatch.setattr(rt, "_close_segment", lambda run, now: None)

    asyncio.run(rt._invoke_and_finalize(str(run.id), "t", "a", input_payload={"messages": []}))

    assert rt.graph.called is False  # type: ignore[attr-defined]
    assert [e[0] for e in events] == ["error"]
    payload = events[0][1]
    assert payload["code"] == "missing_inputs" and payload["phase"] == "pre_invoke"
    assert payload["retryable"] is False and payload["missing"] == ["quote_file"]
    assert payload["source"] == "worker" and payload["worker"] == "报价对比"
    assert payload["inputs"][1]["name"] == "budget"
    assert finalized and finalized[0][1] == "failed"
    assert finalized[0][3]["reason"] == "missing_inputs"
    assert finalized[0][3]["error"]["missing"] == ["quote_file"]
    assert finalized[0][3]["achieved"] is False


def test_invoke_gate_passes_contract_to_graph(monkeypatch: pytest.MonkeyPatch) -> None:
    """齐备 → 契约文本进 configurable.input_contract（由 context_assembly 注入固定区）。"""
    rt = EngineRuntime()
    run = _run(payload={"worker_name": "报价对比", "worker_version": "v1"})
    run.started_at = datetime.now(UTC) - timedelta(seconds=5)
    run.deadline_at = datetime.now(UTC) + timedelta(seconds=595)
    _install(monkeypatch, rt, run)
    graph = _RecordingGraph()
    rt.graph = graph  # type: ignore[assignment]

    async def _resolve(run: Run, *, db: object = None) -> RunInputResolution:
        return _resolution(ok=True)

    monkeypatch.setattr(preflight, "resolve_run_inputs", _resolve)

    with pytest.raises(_Stop):
        asyncio.run(rt._invoke_and_finalize(str(run.id), "t", "a", input_payload={"messages": []}))

    assert graph.called is True
    contract = graph.config["configurable"]["input_contract"]  # type: ignore[index]
    assert contract and "quote_file（file，必填）" in contract


def test_invoke_gate_skips_resume_command(monkeypatch: pytest.MonkeyPatch) -> None:
    """确认/等待恢复（Command）不设门：执行已过半，拦下来只会丢进度。"""
    from langgraph.types import Command

    rt = EngineRuntime()
    run = _run(payload={"worker_name": "报价对比", "worker_version": "v1"})
    run.started_at = datetime.now(UTC) - timedelta(seconds=5)
    run.deadline_at = datetime.now(UTC) + timedelta(seconds=595)
    _install(monkeypatch, rt, run)
    graph = _RecordingGraph()
    rt.graph = graph  # type: ignore[assignment]

    async def _resolve(run: Run, *, db: object = None) -> RunInputResolution:
        return _resolution(ok=False)

    monkeypatch.setattr(preflight, "resolve_run_inputs", _resolve)

    with pytest.raises(_Stop):
        asyncio.run(
            rt._invoke_and_finalize(
                str(run.id), "t", "a", input_payload=Command(resume={"answer": "approved"})
            )
        )
    assert graph.called is True
