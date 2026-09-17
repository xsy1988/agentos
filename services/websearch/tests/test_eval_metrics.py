"""评测指标的手算对照（DoD 5：「metrics nDCG-MRR 手算对照」）。

这里刻意**不复现实现**（不写 `sum((2**g-1)/log2(i+2))` 当断言）——那样只会让测试
和实现一起错。所有期望值都是纸上算出来的常数，算式写在注释里。

分级 DCG 用标准式：DCG = Σ (2^g − 1) / log2(rank + 1)，rank 从 1 起。
    log2(2)=1        log2(3)=1.5849625    log2(4)=2        log2(5)=2.3219281
"""

from __future__ import annotations

import math

import pytest

from eval.metrics import (
    Judgment,
    bare_domain,
    dcg,
    grade_of,
    ideal_dcg,
    matched_spans,
    mean,
    mrr,
    ndcg_at_k,
    parse_judgments,
    percentile,
    recall_at_k,
    route_agreement,
    span_hit,
)

# 三条标注：一条精确 URL（grade 2）、一条权威域名（grade 1）、一条只相关（grade 1）
J = [
    Judgment(grade=2, url="https://example.com/paper"),
    Judgment(grade=1, domain="docs.example.org"),
    Judgment(grade=1, url="https://blog.example.net/post"),
]


# ------------------------------------------------------------------ DCG / nDCG
def test_dcg_matches_hand_computation() -> None:
    """[2,1,1] → 3/log2(2) + 1/log2(3) + 1/log2(4) = 3 + 0.6309298 + 0.5 = 4.1309298"""
    assert dcg([2, 1, 1]) == pytest.approx(4.1309297536, abs=1e-9)


def test_dcg_of_all_zero_is_zero() -> None:
    assert dcg([0, 0, 0]) == 0.0


def test_dcg_grade_gain_is_exponential_not_linear() -> None:
    """2^g − 1 的用意：grade 2 值 3 分而 grade 1 值 1 分。

    「直接回答问题」比「沾边」值钱得多，线性增益（g 本身）表达不出这个差距。
    """
    assert dcg([2]) == pytest.approx(3.0)
    assert dcg([1]) == pytest.approx(1.0)


def test_dcg_position_discount() -> None:
    """同样 grade 1，第 1 位值 1.0、第 2 位值 0.6309、第 3 位值 0.5。"""
    assert dcg([1]) == pytest.approx(1.0)
    assert dcg([0, 1]) == pytest.approx(1 / math.log2(3))
    assert dcg([0, 0, 1]) == pytest.approx(0.5)


def test_ideal_dcg_sorts_judgments_desc_and_caps_at_k() -> None:
    """IDCG = 把标注按 grade 降序排满前 k 位 → [2,1,1] = 4.1309298。

    每条标注只占**一个**理想位次：domain 标注实际可能匹配多篇，但没人逐篇确认过，
    多给位次等于凭空假设「本该有更多相关页」，把 nDCG 压到无法解释。
    """
    assert ideal_dcg(J) == pytest.approx(4.1309297536, abs=1e-9)
    assert ideal_dcg(J, k=1) == pytest.approx(3.0)  # 只留 grade 2 那条
    assert ideal_dcg(J, k=2) == pytest.approx(3.0 + 1 / math.log2(3))


def test_ndcg_perfect_order_is_one() -> None:
    urls = [
        "https://example.com/paper",
        "https://docs.example.org/a",
        "https://blog.example.net/post",
    ]
    assert ndcg_at_k(urls, J) == pytest.approx(1.0)


def test_ndcg_hand_computed_for_a_shuffled_order() -> None:
    """实测口径：[grade2, 无关, grade1] → DCG = 3/1 + 0 + 1/log2(4) = 3.5
    nDCG = 3.5 / 4.1309298 = 0.8473（第三条 grade1 没进前 3，一点分都没拿到）"""
    urls = [
        "https://example.com/paper",
        "https://spam.example/irrelevant",
        "https://docs.example.org/a",
    ]
    assert ndcg_at_k(urls, J) == 0.8473


def test_ndcg_at_k_ignores_everything_beyond_k() -> None:
    """第 11 位的相关页对 nDCG@10 没有贡献——Agent 只读前一屏，这就是 @10 的意义。"""
    urls = ["https://spam.example/x"] * 10 + ["https://example.com/paper"]
    assert ndcg_at_k(urls, J, k=10) == 0.0
    assert ndcg_at_k(urls, J, k=11) > 0.0


def test_ndcg_without_judgments_is_zero_not_one() -> None:
    """IDCG=0 时必须返回 0.0：返回 1.0 会把「没标注」洗成满分，平均分立刻失真。"""
    assert ndcg_at_k(["https://example.com/paper"], []) == 0.0
    assert ndcg_at_k([], J) == 0.0


def test_domain_annotation_contributes_at_most_one_point() -> None:
    """只标了 domain+grade1 的条目即便排第一也只有 1.0 的 DCG 贡献（grade2 是 3.0）。

    这是不完备标注的代价，必须与「标注覆盖率」一起读——否则会误以为排序很差。
    """
    only_domain = [Judgment(grade=1, domain="docs.example.org")]
    assert ndcg_at_k(["https://docs.example.org/x"], only_domain) == pytest.approx(1.0)
    assert ideal_dcg(only_domain) == pytest.approx(1.0)


# ------------------------------------------------------------------ MRR
def test_mrr_is_reciprocal_of_first_relevant_rank() -> None:
    urls = ["https://spam/x", "https://spam/y", "https://example.com/paper"]
    assert mrr(urls, J) == 0.3333  # 1/3


def test_mrr_first_position_is_one() -> None:
    assert mrr(["https://docs.example.org/a"], J) == 1.0


def test_mrr_is_zero_when_nothing_relevant() -> None:
    assert mrr(["https://spam/x", "https://spam/y"], J) == 0.0


def test_mrr_counts_grade_one_too() -> None:
    """MRR 只看「第一条相关的有多靠前」，grade 1 与 2 同权——它和 nDCG 是两件事。"""
    assert mrr(["https://blog.example.net/post"], J) == 1.0


# ------------------------------------------------------------------ recall@k
def test_recall_is_binary_per_query() -> None:
    """单条 query 的 recall@50 是二值：窗口里有 ≥1 个已标注相关项就是 1.0。
    聚合后才成为「命中率」——这也是为什么它的绝对值受标注完备性影响极大。"""
    assert recall_at_k(["https://spam/x", "https://example.com/paper"], J) == 1.0
    assert recall_at_k(["https://spam/x", "https://spam/y"], J) == 0.0


def test_recall_respects_the_window_cutoff() -> None:
    """相关项落在第 51 位 → recall@50 = 0（窗口就是 50，落外面等于没召回）。"""
    urls = ["https://spam/x"] * 50 + ["https://example.com/paper"]
    assert recall_at_k(urls, J, k=50) == 0.0
    assert recall_at_k(urls, J, k=51) == 1.0


def test_recall_on_empty_window_is_zero() -> None:
    assert recall_at_k([], J) == 0.0


# ------------------------------------------------------------------ 匹配规则
def test_grade_of_matches_exact_url() -> None:
    assert grade_of("https://example.com/paper", J) == 2


def test_grade_of_matches_domain_and_subdomains() -> None:
    assert grade_of("https://docs.example.org/any/path", J) == 1
    assert grade_of("https://sub.docs.example.org/x", J) == 1


def test_grade_of_does_not_match_lookalike_domains() -> None:
    """后缀匹配的经典陷阱：`notdocs.example.org` 与 `docs.example.org.evil.com`
    都不该命中 `docs.example.org`。少了那个点就会把钓鱼站判成权威源。"""
    assert grade_of("https://notdocs.example.org/x", J) == 0
    assert grade_of("https://docs.example.org.evil.com/x", J) == 0


def test_grade_of_takes_the_highest_when_rules_overlap() -> None:
    """同一文档同时命中 url(grade2) 与 domain(grade1) → 取 2。"""
    j = [
        Judgment(grade=1, domain="example.com"),
        Judgment(grade=2, url="https://example.com/paper"),
    ]
    assert grade_of("https://example.com/paper", j) == 2


def test_annotations_are_normalized_to_the_pipelines_ruler() -> None:
    """yaml 里写 `https://X.com/a/` 而流水线给出 `https://x.com/a` 必须匹配上，
    否则标注永远假阴性——而且不报错，只会让指标静默变低。"""
    j = parse_judgments([{"url": "https://Example.COM/a/?utm_source=x", "grade": 2}])
    assert grade_of("https://example.com/a", j) == 2


def test_bare_domain_rescues_a_pasted_url() -> None:
    """标注时很容易顺手把整条 URL 粘进 domain 字段；不纠正就永远匹配不上。"""
    assert bare_domain("https://GitHub.com/docs/x?a=1") == "github.com"
    assert bare_domain("docs.example.org") == "docs.example.org"
    assert bare_domain("  Sub.Example.COM.  ") == "sub.example.com"
    assert bare_domain("") == ""


# ------------------------------------------------------------------ 标注校验
def test_parse_judgments_accepts_the_documented_shape() -> None:
    got = parse_judgments([{"url": "https://a.com/x", "grade": 2, "note": "已确认"}])
    assert got == [Judgment(grade=2, url="https://a.com/x", domain="", note="已确认")]


def test_parse_judgments_defaults_grade_to_one() -> None:
    got = parse_judgments([{"domain": "a.com"}])
    assert got[0].grade == 1 and got[0].domain == "a.com"


def test_parse_judgments_rejects_unknown_grade() -> None:
    """grade 只支持 1/2。写成 3 或 0 会让 IDCG 与 DCG 一起变形，指标再也无法解释。"""
    with pytest.raises(ValueError, match="grade 只支持"):
        parse_judgments([{"domain": "a.com", "grade": 3}])


def test_parse_judgments_rejects_non_integer_grade() -> None:
    with pytest.raises(ValueError, match="grade 必须是整数"):
        parse_judgments([{"domain": "a.com", "grade": "high"}])


def test_parse_judgments_rejects_entry_without_target() -> None:
    with pytest.raises(ValueError, match="必须给 url 或 domain"):
        parse_judgments([{"grade": 2}])


def test_parse_judgments_rejects_non_mapping_entry() -> None:
    with pytest.raises(ValueError, match="必须是映射"):
        parse_judgments(["https://a.com/x"])


def test_parse_judgments_rejects_empty_relevant() -> None:
    """没有标注的条目产不出任何指标——空着放行等于让这条 query 永远 0 分且无人察觉。"""
    with pytest.raises(ValueError, match="relevant 不能为空"):
        parse_judgments([])


# ------------------------------------------------------------------ 多路一致率
def test_route_agreement_counts_urls_seen_by_multiple_routes() -> None:
    routes = {"original": ["a", "b", "c"], "phrase": ["b", "c", "d"]}
    # top4 里 b、c 两路都有，a、d 只有一路 → 2/4
    assert route_agreement(["a", "b", "c", "d"], routes) == 0.5


def test_route_agreement_is_zero_for_a_single_route() -> None:
    """单路时它按定义恒为 0，**不是质量下降**。调用方应把这一列标成 n/a
    （eval.run 就是这么做的：单路时记 None，聚合时排除）。"""
    assert route_agreement(["a", "b"], {"original": ["a", "b"]}) == 0.0


def test_route_agreement_on_empty_top_is_zero() -> None:
    assert route_agreement([], {"original": ["a"], "phrase": ["a"]}) == 0.0


# ------------------------------------------------------------------ 答案串
def test_matched_spans_ignores_whitespace_and_case() -> None:
    """答案串常被排版拆开（`8 192 tokens`、全角空格），按原文子串匹配会假阴性。"""
    assert matched_spans(["max length is 8 192"], ["8192"]) == ["8192"]
    assert matched_spans(["Too   Many   Requests"], ["too many requests"]) == ["too many requests"]


def test_matched_spans_returns_the_hits_for_manual_review() -> None:
    """返回命中项而不只是布尔：假阳性得靠人眼复查（短数字串可能落进更长的数字里）。"""
    got = matched_spans(["8192 和 568M 参数"], ["8192", "568M", "9999"])
    assert got == ["8192", "568M"]


def test_span_hit_is_true_when_any_span_matches() -> None:
    assert span_hit(["端口 6379"], ["5432", "6379"]) is True
    assert span_hit(["端口 5432"], ["6379"]) is False


def test_span_hit_on_empty_inputs() -> None:
    assert span_hit([], ["6379"]) is False
    assert span_hit(["端口 6379"], []) is False
    assert matched_spans(["端口 6379"], ["", "  "]) == []  # 空串不许命中一切


def test_span_hit_joins_passages_across_documents() -> None:
    """片段召回率是**整条 query 级**的判定：多篇片段拼在一起找答案串。"""
    assert span_hit(["第一段没答案", "第二段有 6379"], ["6379"]) is True


# ------------------------------------------------------------------ 聚合与分位
def test_mean_skips_none_and_returns_zero_for_empty() -> None:
    assert mean([1.0, 0.0, None]) == 0.5
    assert mean([]) == 0.0
    assert mean([None, None]) == 0.0


def test_mean_rounds_to_four_decimals() -> None:
    assert mean([1 / 3]) == 0.3333


def test_percentile_uses_nearest_rank() -> None:
    """最近邻法（不插值）：idx = round(pct/100 × (n−1))，与服务端 /metrics 同一口径。

    手算：n=4 时 p50 → round(0.5×3)=round(1.5)=**2**（Python 的 round 在 .5 时取偶，
    1.5 → 2）→ 第 3 小 = 30；n=3 时 p50 → round(0.5×2)=1 → 20，就是直觉上的中位数。
    两个 n 的差别正好说明它是最近邻而不是插值：插值下 n=4 的 p50 会是 25。
    """
    assert percentile([10, 20, 30, 40], 50) == 30
    assert percentile([10, 20, 30], 50) == 20
    assert percentile([10, 20, 30, 40], 95) == 40  # round(0.95×3)=3 → 最后一个
    assert percentile([40, 10, 30, 20], 50) == 30  # 先排序，输入顺序不影响
    assert percentile([], 50) == 0
    assert percentile([7], 50) == 7  # n=1：idx 被钳在 0
