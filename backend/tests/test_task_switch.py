"""新主任务检测的纯逻辑单测（ADR-27 软提示，不依赖 DB / embedding）。

tasks.service.pick_task_switch：从 (id, name, icon, 相似度) 候选中挑出
高置信、非当前主任务、非通用任务集的最佳命中；无命中返回 None。
"""

from app.modules.tasks.service import TASK_SWITCH_THRESHOLD, pick_task_switch


def _cand(cid: str, name: str, sim: float, icon: str | None = None) -> tuple:
    return (cid, name, icon, sim)


def test_picks_highest_above_threshold() -> None:
    """多个命中时取相似度最高的那个。"""
    scored = [_cand("a", "报价对比", 0.81), _cand("b", "合同审查", 0.92), _cand("c", "闲杂", 0.40)]
    best = pick_task_switch(scored, current_type_id=None, common_type_id=None)
    assert best is not None
    assert best[0] == "b"
    assert best[1] == "合同审查"


def test_below_threshold_returns_none() -> None:
    """全部低于阈值 → 不软提示（避免误报打断用户）。"""
    scored = [_cand("a", "报价对比", TASK_SWITCH_THRESHOLD - 0.01), _cand("b", "合同审查", 0.30)]
    assert pick_task_switch(scored, current_type_id=None, common_type_id=None) is None


def test_excludes_current_task_type() -> None:
    """命中即当前主任务时不提示（同一任务的多轮消息不该被建议新开）。"""
    scored = [_cand("cur", "报价对比", 0.95), _cand("other", "合同审查", 0.60)]
    assert pick_task_switch(scored, current_type_id="cur", common_type_id=None) is None


def test_excludes_common_task_type() -> None:
    """通用任务集永不被建议为新主任务（它是兜底域，不是业务 Worker）。"""
    scored = [_cand("common", "通用任务", 0.99), _cand("biz", "报价对比", 0.80)]
    best = pick_task_switch(scored, current_type_id=None, common_type_id="common")
    assert best is not None
    assert best[0] == "biz"


def test_current_is_common_still_suggests_business() -> None:
    """当前会话是通用任务、消息命中业务 Worker → 建议新开（典型新主任务场景）。"""
    scored = [_cand("biz", "供应商报价对比", 0.88)]
    best = pick_task_switch(scored, current_type_id="common", common_type_id="common")
    assert best is not None
    assert best[0] == "biz"


def test_empty_candidates_returns_none() -> None:
    """无业务 Worker（仅通用任务集）时不提示。"""
    assert pick_task_switch([], current_type_id=None, common_type_id=None) is None


def test_threshold_boundary_is_inclusive() -> None:
    """恰好等于阈值算命中（>= 语义）。"""
    scored = [_cand("a", "报价对比", TASK_SWITCH_THRESHOLD)]
    best = pick_task_switch(scored, current_type_id=None, common_type_id=None)
    assert best is not None
    assert best[0] == "a"
