"""能力归属分层检索的纯逻辑单测（ADR-28，不依赖 DB / LLM）。

retriever.order_by_scope：主任务域 → 通用任务集 → 全局，层内保持向量距离序，
模板步骤 capability_hint 点名的域内能力提到最前。
"""

from app.modules.discovery.retriever import order_by_scope


def _cap(cid: str, name: str) -> dict:
    return {"id": cid, "name": name, "type": "tool"}


def test_domain_first_then_common_then_global() -> None:
    """全局高分能力不能挤掉域内能力：分层优先于语义得分。"""
    cands = [_cap("g1", "web_search"), _cap("c1", "get_user"), _cap("d1", "parse_quote")]
    out = order_by_scope(cands, domain_ids={"d1"}, common_ids={"c1"}, k=8)
    assert [c["id"] for c in out] == ["d1", "c1", "g1"]
    assert [c["scope"] for c in out] == ["task_domain", "task_common", "global"]


def test_layer_keeps_distance_order() -> None:
    """层内保持传入顺序（即向量距离序）。"""
    cands = [_cap("d1", "a"), _cap("d2", "b"), _cap("c1", "c"), _cap("c2", "d")]
    out = order_by_scope(cands, domain_ids={"d1", "d2"}, common_ids={"c1", "c2"}, k=8)
    assert [c["id"] for c in out] == ["d1", "d2", "c1", "c2"]


def test_hint_promotes_named_capability_within_domain() -> None:
    """模板步骤点名的能力在域内提权，但不排除其它域内能力。"""
    cands = [_cap("d1", "other_quote_tool"), _cap("d2", "quote_parser")]
    out = order_by_scope(
        cands, domain_ids={"d1", "d2"}, common_ids=set(), hints=("quote_parser",), k=8
    )
    assert [c["id"] for c in out] == ["d2", "d1"]


def test_hint_does_not_pull_global_forward() -> None:
    """hint 只在域内提权：不属于本主任务的能力即便被点名也不越过通用任务集。"""
    cands = [_cap("d1", "parse_quote"), _cap("c1", "get_user"), _cap("g1", "named_outside")]
    out = order_by_scope(
        cands,
        domain_ids={"d1"},
        common_ids={"c1"},
        hints=("named_outside",),
        k=8,
    )
    assert [c["id"] for c in out] == ["d1", "c1", "g1"]


def test_k_trims_after_reordering() -> None:
    """截断发生在重排之后（放大候选的意义所在）。"""
    cands = [_cap("g1", "a"), _cap("g2", "b"), _cap("d1", "c"), _cap("d2", "d")]
    out = order_by_scope(cands, domain_ids={"d1", "d2"}, common_ids=set(), k=2)
    assert [c["id"] for c in out] == ["d1", "d2"]


def test_no_scope_ids_degrades_to_distance_order() -> None:
    """无归属信息（如 timer 无任务的 run）时退化为纯语义序，行为不回归。"""
    cands = [_cap("x1", "a"), _cap("x2", "b")]
    out = order_by_scope(cands, domain_ids=set(), common_ids=set(), k=8)
    assert [c["id"] for c in out] == ["x1", "x2"]
    assert {c["scope"] for c in out} == {"global"}
