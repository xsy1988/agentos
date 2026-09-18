"""子任务级输入门（方案 §5 P1-4 收尾）。

与 run 级门（`test_worker_inputs.py`）的分工：run 级在调用模型前**硬失败**（拦下整个
run），step 级在装配之后、真正干活之前 **interrupt 问用户**——缺材料是多轮会话里最正常
的中间态，硬失败会连带丢掉已产出的进度。本文件覆盖三层：

1. 预检层：当前子任务的选取语义（doing 优先 / 分支与已收口步骤不算 / 回指不到声明即放行）；
2. 节点层：`input_gate` 的挂起-放行分支（含 timer 无人值守不挂起）；
3. 往返层：真 `StateGraph` + `InMemorySaver` 的 interrupt/resume（含"恢复后契约不叠加"）。

节点取自**真图**（同 `scripts/accept_v16.py` 的手法），不是复刻实现——避免"测的是副本"。
"""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command

from app.modules.engine import runtime
from app.modules.engine.runtime import EngineRuntime
from app.modules.engine.state import LoopState
from app.modules.runs.models import Run
from app.modules.tasks.models import TaskStep
from app.modules.workers import registry
from app.modules.workers.inputs import resolve_inputs
from app.modules.workers.preflight import (
    STEP_CONTRACT_HEADER,
    StepInputResolution,
    resolve_step_inputs,
    strip_step_contract,
)
from tests.support.fake_db import RoutingSession

SUB_INPUTS = [
    {
        "name": "quote_file",
        "type": "file",
        "required": True,
        "description": "待比价的报价单",
        "example": "报价单-2026Q1.xlsx",
    },
    {"name": "budget", "type": "number", "description": "预算上限"},
]


@pytest.fixture()
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """把 registry 的文件根指到临时目录，测试互不污染。"""
    monkeypatch.setattr(registry.settings, "data_dir", tmp_path)
    registry._invalidate_all()
    yield tmp_path
    registry._invalidate_all()


def _mk_worker(name: str = "报价对比", *, ref: str = "拉取报价", inputs: list[dict] | None = None):
    registry.ensure_worker(
        name,
        description="对比供应商报价",
        inputs=[{"name": "vendor", "required": True}],
        sub_workers=[
            {
                "name": ref,
                "seq": 1,
                "kind": "main",
                "playbook": "# 步骤\n拉报价\n",
                "inputs": SUB_INPUTS if inputs is None else inputs,
            }
        ],
    )


def _db(
    *,
    steps: list[TaskStep] | None = None,
    snapshots: list[dict] | None = None,
    files: dict[UUID, str] | None = None,
) -> RoutingSession:
    """只读替身：子任务列表（`scalars`）固定给 `steps`，`files` 查询给 (附件 id, 文件名)，
    其余语句（runs 快照）给 `snapshots`。"""
    pairs = [(fid, name) for fid, name in (files or {}).items()]

    def route(stmt: Any) -> list[Any] | None:
        return pairs if "files" in str(stmt) else None

    return RoutingSession(
        router=route,
        scalar_rows=list(steps or []),
        execute_rows=list(snapshots or []),
    )


def _step(
    *,
    seq: int = 1,
    name: str = "拉取报价",
    kind: str = "main",
    status: str = "doing",
    ref: str | None = "拉取报价",
) -> TaskStep:
    return TaskStep(
        id=uuid4(),
        task_id=uuid4(),
        seq=seq,
        name=name,
        kind=kind,
        status=status,
        worker_step_ref=ref,
    )


def _run(*, worker_name: str = "报价对比", task_id: Any = None, **payload: Any) -> Run:
    return Run(
        id=uuid4(),
        status="pending",
        trigger="manual",
        input={
            "worker_name": worker_name,
            "worker_version": "v1",
            "task_id": str(task_id) if task_id else None,
            **payload,
        },
        budget={"timeout_seconds": 600},
        budget_used={},
        active_ms=0,
    )


def _task_scope() -> tuple[UUID, Run]:
    """一个挂上了子任务的 run（task_id 一致才好查当前步骤）。"""
    task_id = uuid4()
    return task_id, _run(task_id=task_id)


# ---------- 1. 预检层：当前子任务选取 ----------


def test_resolve_step_inputs_skips_without_task_or_worker(root: Path) -> None:
    _mk_worker()
    # 无 task_id（通用 run）/ 无 worker_name：不做子任务级判定
    assert asyncio.run(resolve_step_inputs(_run(), db=_db())) is None
    assert asyncio.run(resolve_step_inputs(_run(task_id=uuid4()), db=_db())) is None


def test_current_step_query_filters_open_main_steps(root: Path) -> None:
    """筛选在 SQL 里（kind='main' 且 doing/pending，按 seq 升序）。

    白盒但必要：把谓词写进查询是"不选中已收口步骤/支线"的唯一保证，假库无法替它背书。
    """
    _mk_worker()
    _, run = _task_scope()
    db = _db(steps=[])
    assert asyncio.run(resolve_step_inputs(run, db=db)) is None
    (stmt,) = db.statements
    text = str(stmt)
    assert "task_steps.kind = " in text
    assert "task_steps.status IN (" in text
    assert "ORDER BY task_steps.seq ASC" in text
    assert "task_steps.task_id = " in text
    # 候选为空（没有未收口的主线步骤）→ 放行，不拦
    assert asyncio.run(resolve_step_inputs(run, db=_db(steps=[]))) is None


def test_current_step_prefers_doing_then_smallest_seq(root: Path) -> None:
    """doing 优先；无 doing 时取 seq 最小的 pending（planner 前第一步还是 pending）。"""
    _mk_worker()
    _, run = _task_scope()
    # 假库回放 seq 升序的候选：第一条 pending、第二条 doing
    pending = _step(seq=1, status="pending")
    doing = _step(seq=2, status="doing", name="拉取报价")
    res = asyncio.run(resolve_step_inputs(run, db=_db(steps=[pending, doing])))
    assert res is not None and res.step_id == doing.id
    # 只有 pending 时取最小 seq（先到的在前）
    first, second = _step(seq=1, status="pending"), _step(seq=2, status="pending")
    res = asyncio.run(resolve_step_inputs(run, db=_db(steps=[first, second])))
    assert res is not None and res.step_id == first.id


def test_resolve_step_inputs_skips_steps_without_declared_inputs(root: Path) -> None:
    """回指不到子任务（模型新增步骤）/ 子任务没声明输入 → 放行。"""
    _mk_worker()
    _, run = _task_scope()
    llm_added = _step(ref=None)
    assert asyncio.run(resolve_step_inputs(run, db=_db(steps=[llm_added]))) is None
    unknown_ref = _step(ref="不存在的子任务")
    assert asyncio.run(resolve_step_inputs(run, db=_db(steps=[unknown_ref]))) is None
    registry.ensure_worker(
        "无输入",
        sub_workers=[{"name": "干活", "seq": 1, "kind": "main", "playbook": "# 干活\n"}],
    )
    no_inputs = _step(ref="干活")
    res = asyncio.run(
        resolve_step_inputs(_run(worker_name="无输入", task_id=uuid4()), db=_db(steps=[no_inputs]))
    )
    assert res is None


def test_resolve_step_inputs_blocks_and_reports_missing(root: Path) -> None:
    _mk_worker()
    _, run = _task_scope()
    step = _step()
    res = asyncio.run(resolve_step_inputs(run, db=_db(steps=[step])))
    assert res is not None
    assert res.ok is False and res.missing_names == ["quote_file"]
    assert res.step_name == "拉取报价" and res.sub_ref == "拉取报价"
    # 文案不骗人：不能说"没调用模型"（此时模型可能已经跑过若干轮）
    assert "从暂停点继续" in res.message and "未消耗模型调用" not in res.message
    assert res.contract.startswith(STEP_CONTRACT_HEADER)
    body = res.payload()
    assert body["step_id"] == str(step.id) and body["missing"] == ["quote_file"]
    assert [i["name"] for i in body["inputs"]] == ["quote_file", "budget"]
    assert [i["provided"] for i in body["inputs"]] == [False, False]
    assert body["inputs"][0]["label"] and body["inputs"][0]["example"] == "报价单-2026Q1.xlsx"


def test_resolve_step_inputs_uses_session_values(root: Path) -> None:
    """取值与 run 级门同一命名空间：会话内按输入名累积，附件按声明顺序顶替 file。"""
    _mk_worker()
    task_id, _ = _task_scope()
    fid = uuid4()
    run = _run(task_id=task_id, inputs={"budget": 500}, attachment_ids=[str(fid)])
    res = asyncio.run(resolve_step_inputs(run, db=_db(steps=[_step()], files={fid: "报价单.xlsx"})))
    assert res is not None and res.ok is True
    assert res.values == {"quote_file": "报价单.xlsx", "budget": "500"}
    assert "已提供：报价单.xlsx" in res.contract


def test_resolve_step_inputs_accumulates_across_session(root: Path) -> None:
    """跨轮累积：第一轮给的报价单，后面子任务不该再要一遍。"""
    _mk_worker()
    task_id = uuid4()
    base = datetime.now(UTC) - timedelta(minutes=10)
    history = [
        {"worker_name": "报价对比", "inputs": {"quote_file": "第一轮.xlsx"}},
        {"worker_name": "报价对比", "inputs": {"budget": "800"}},
    ]
    run = _run(task_id=task_id)
    run.conversation_id = uuid4()
    run.created_at = base + timedelta(minutes=5)
    res = asyncio.run(resolve_step_inputs(run, db=_db(steps=[_step()], snapshots=history)))
    assert res is not None and res.ok is True
    assert res.values == {"quote_file": "第一轮.xlsx", "budget": "800"}


# ---------- 2. 契约段替换（恢复重放不叠加） ----------


def test_strip_step_contract_removes_previous_segment() -> None:
    base = "你是采购助手"
    text = f"{base}\n\n{STEP_CONTRACT_HEADER}（拉取报价）\n- 已提供：a.xlsx\n"
    assert strip_step_contract(text) == base
    assert strip_step_contract(base) == base  # 没注入过 → 原样返回
    # 连着注入两次也只剩一份（先剥再拼）
    twice = f"{text}\n\n{STEP_CONTRACT_HEADER}（拉取报价）\n- 已提供：b.xlsx\n"
    assert strip_step_contract(twice) == base


# ---------- 3. 节点层：真图 input_gate ----------


def _real_node(name: str) -> Any:
    """取真图节点的可调用体（编译会包成 PregelNode 取不到原函数）。"""
    from app.modules.engine import graph as graph_mod

    original = StateGraph.compile
    StateGraph.compile = lambda self, *a, **kw: self  # type: ignore[method-assign]
    try:
        builder = graph_mod.build_graph(SimpleNamespace(saver=InMemorySaver()))
    finally:
        StateGraph.compile = original
    return builder.nodes[name].runnable.afunc


def _gate_contract(*, ok: bool, resolution: StepInputResolution) -> dict[str, Any]:
    return {"ok": ok, "contract": resolution.contract, "payload": resolution.payload()}


def _resolution(**values: str) -> StepInputResolution:
    """用真声明 + 真判定算一次子任务预检结论。

    附件留空：附件路径由 `test_resolve_step_inputs_uses_session_values` 覆盖，图层的用例
    统一用显式取值，避免"到底是被顶替了还是真给了"看不出差别。
    """
    wdef = registry.get_def("报价对比", "v1")
    assert wdef is not None
    sub = wdef.find_sub("拉取报价")
    assert sub is not None
    provided = dict(values)
    resolved, missing = resolve_inputs(sub.inputs, provided, [])
    return StepInputResolution(
        step_id=uuid4(),
        step_name="拉取报价",
        sub_ref="拉取报价",
        specs=list(sub.inputs),
        values=resolved,
        missing=missing,
    )


def _gate_graph() -> Any:
    """input_gate → probe → END（probe 记录固定区，便于断言注入结果）。"""
    gate = _real_node("input_gate")

    async def probe(state: LoopState) -> dict:
        return {"plan_ref": "probed"}

    g = StateGraph(LoopState)
    g.add_node("input_gate", gate)
    g.add_node("probe", probe)
    g.add_edge(START, "input_gate")
    g.add_edge("input_gate", "probe")
    g.add_edge("probe", END)
    return g.compile(checkpointer=InMemorySaver())


def _config(contract: dict[str, Any] | None, *, trigger: str = "manual") -> dict[str, Any]:
    return {
        "configurable": {
            "thread_id": str(uuid4()),
            "run_id": str(uuid4()),
            "trigger": trigger,
            "step_input_contract": contract,
        }
    }


def test_gate_passes_through_without_contract(root: Path) -> None:
    """绝大多数 run 没有子任务输入声明：节点必须完全无副作用（不改固定区）。"""
    graph = _gate_graph()
    out = asyncio.run(
        graph.ainvoke({"protected_context": {"system_prompt": "原样"}}, _config(None))
    )
    assert out["plan_ref"] == "probed"
    assert out["protected_context"]["system_prompt"] == "原样"


def test_gate_injects_contract_when_satisfied(root: Path) -> None:
    _mk_worker()
    graph = _gate_graph()
    res = _resolution(quote_file="报价单.xlsx", budget="800")
    out = asyncio.run(
        graph.ainvoke(
            {"protected_context": {"system_prompt": "你是采购助手"}},
            _config(_gate_contract(ok=True, resolution=res)),
        )
    )
    prompt = out["protected_context"]["system_prompt"]
    assert prompt.startswith("你是采购助手")
    assert prompt.count(STEP_CONTRACT_HEADER) == 1
    assert "已提供：报价单.xlsx" in prompt


def test_gate_interrupts_then_resumes_without_stacking_contract(root: Path) -> None:
    """真 interrupt/resume 往返：挂起 → 补输入 → 契约只注入一份（重放不叠加）。"""
    _mk_worker()
    graph = _gate_graph()
    missing = _resolution()
    cfg = _config(_gate_contract(ok=False, resolution=missing))

    first = asyncio.run(
        graph.ainvoke({"protected_context": {"system_prompt": "你是采购助手"}}, cfg)
    )
    # 挂起：interrupt 载荷就是前端补输入卡的数据源
    assert first.get("__interrupt__")
    value = first["__interrupt__"][0].value
    assert value["reason"] == "input_required"
    assert value["payload"]["missing"] == ["quote_file"]
    assert [i["name"] for i in value["payload"]["inputs"]] == ["quote_file", "budget"]
    # 节点在 interrupt 处停住，probe 未执行
    assert first.get("plan_ref") is None
    snapshot = asyncio.run(graph.aget_state(cfg))
    assert snapshot.next == ("input_gate",)

    # 恢复：configurable 是新事实（runtime 每次进入图都从 DB 重算）
    ready = _resolution(quote_file="补的.xlsx", budget="800")
    cfg2 = {**cfg, "configurable": {**cfg["configurable"]}}
    cfg2["configurable"]["step_input_contract"] = _gate_contract(ok=True, resolution=ready)
    final = asyncio.run(graph.ainvoke(Command(resume=""), cfg2))
    assert final["plan_ref"] == "probed"
    prompt = final["protected_context"]["system_prompt"]
    assert prompt.count(STEP_CONTRACT_HEADER) == 1
    assert "补的.xlsx" in prompt

    # 再恢复一次（重放同一检查点）：契约仍只有一份，绝不复利增长
    replay_cfg = {**cfg, "configurable": {**cfg["configurable"]}}
    replay_cfg["configurable"]["step_input_contract"] = _gate_contract(ok=True, resolution=ready)
    replayed = asyncio.run(graph.ainvoke(Command(resume=""), replay_cfg))
    assert replayed["protected_context"]["system_prompt"].count(STEP_CONTRACT_HEADER) == 1


def test_gate_skips_interrupt_for_timer_trigger(root: Path) -> None:
    """timer 触发无人值守：缺输入不挂起（对齐 ADR-16 的 confirm_plan 策略），只留日志。"""
    _mk_worker()
    graph = _gate_graph()
    res = _resolution()
    out = asyncio.run(
        graph.ainvoke(
            {"protected_context": {"system_prompt": "你是采购助手"}},
            _config(_gate_contract(ok=False, resolution=res), trigger="timer"),
        )
    )
    assert out["plan_ref"] == "probed"  # 没挂起，跑完了
    # 缺输入的契约不注入（模型不该看到"已提供"的半份事实）
    assert STEP_CONTRACT_HEADER not in out["protected_context"]["system_prompt"]


# ---------------------------------------------------------------- 4. 恢复侧落库
# 挂起解决后，补输入必须先落库再恢复：`_invoke_and_finalize` 每次进图都从 DB 重算预检，
# 只走内存的话重放 `input_gate` 会再次判缺失 —— 用户补了也永远过不去。


def _run_row(run_id: UUID, inputs: Any = None) -> Run:
    run = Run(
        id=run_id, conversation_id=uuid4(), agent_id=uuid4(), status="paused_awaiting_confirm"
    )
    run.input = {"task_id": str(uuid4()), "inputs": inputs} if inputs is not None else {}
    return run


def test_persist_resume_inputs_merges_over_existing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """合并语义：同名后写覆盖，异名保留（会话内取值是累积的）。"""
    from tests.support.fake_db import FakeSession

    run_id = uuid4()
    row = _run_row(run_id, inputs={"vendor": "甲", "budget": "300"})
    db = FakeSession(get_rows=[row])
    monkeypatch.setattr(runtime, "session_factory", lambda: db)

    # 只用到模块级 session_factory，不必跑 __init__（那会拉起真 DB 与后台任务）
    rt = EngineRuntime.__new__(EngineRuntime)
    asyncio.run(
        rt._persist_resume_inputs(str(run_id), {"budget": "800", "quote_file": "补的.xlsx"})
    )

    assert row.input["inputs"] == {"vendor": "甲", "budget": "800", "quote_file": "补的.xlsx"}
    assert db.commits == 1


def test_persist_resume_inputs_is_noop_without_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """空取值直接返回：不落库、不写空 dict（否则会把会话里既有取值洗掉）。"""
    from tests.support.fake_db import FakeSession

    run_id = uuid4()
    row = _run_row(run_id, inputs={"vendor": "甲"})
    db = FakeSession(get_rows=[row])
    monkeypatch.setattr(runtime, "session_factory", lambda: db)

    rt = EngineRuntime.__new__(EngineRuntime)
    asyncio.run(rt._persist_resume_inputs(str(run_id), None))
    assert db.commits == 0
    assert row.input["inputs"] == {"vendor": "甲"}


def test_step_gate_payload_shape(root: Path) -> None:
    """configurable 载荷必须是 JSON-safe 的三件套，缺一份节点就没法判断挂不挂。"""
    _mk_worker()
    res = _resolution()
    gate = runtime._step_gate_payload(res)
    assert gate is not None
    assert set(gate) == {"ok", "contract", "payload"}
    assert gate["ok"] is False
    assert gate["payload"]["missing"] == ["quote_file"]
    json.dumps(gate)  # 过不了序列化就送不进 configurable
    assert runtime._step_gate_payload(None) is None
