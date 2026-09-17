"""输出层单测：预算钳制与截断、meta 自报、片段渲染、fetch 文本、REST JSON。

这一层的唯一硬承诺是**不突破 max_chars**：平台引擎不截断工具结果（graph 里直接
`ToolMessage(str(content))`），而上下文压缩阈值是 24000 字符——服务端多吐的每一个字符
都会直接挤掉会话历史。所以预算相关的断言写成不等式，而不是「大概差不多」。

第二条承诺是**不自相矛盾**：`chars=` 自报的长度要等于实际长度；「预算不够只给一条」
要和「上游没搜到」在文本上分得开；降级了就要在 meta 里说出来。
"""

from websearch.config import Settings
from websearch.format import (
    clamp_max_chars,
    clamp_max_results,
    format_fetch,
    format_text,
    meta_line,
    to_json,
)
from websearch.types import STAGES, Doc, SearchParams, SearchResult, StageTiming

_CFG = Settings(_env_file=None)


def _doc(
    idx: int,
    *,
    passages: list[str] | None = None,
    snippet: str = "",
    title: str | None = None,
) -> Doc:
    return Doc(
        url=f"https://example.com/p{idx}?utm_source=x",
        norm_url=f"https://example.com/p{idx}",
        title=title if title is not None else f"T{idx} 向量数据库的索引结构与实现",
        snippet=snippet,
        date="2026-03-01",
        site="example.com",
        engines=["google", "bing"],
        routes={"original": idx},
        score=1.0 / idx,
        rerank_score=0.9,
        passages=list(passages or []),
        passage_source="trafilatura" if passages else "snippet",
    )


def _result(
    docs: list[Doc],
    *,
    query: str = "向量数据库的索引结构",
    max_chars: int = 6000,
    available: bool = True,
    message: str = "",
    stats: dict[str, object] | None = None,
    flags: dict[str, str] | None = None,
    errors: list[str] | None = None,
    unresponsive: list[str] | None = None,
    elapsed_ms: int = 8400,
) -> SearchResult:
    return SearchResult(
        params=SearchParams(query=query, max_chars=max_chars),
        docs=docs,
        window=docs,
        route_names=["original", "keyword"],
        timings=[StageTiming(name="recall", elapsed_ms=1200)],
        stats=dict(stats or {}),
        flags=dict(flags or {}),
        errors=list(errors or []),
        unresponsive_engines=list(unresponsive or []),
        available=available,
        message=message,
        elapsed_ms=elapsed_ms,
    )


# ---------------------------------------------------------------- 预算钳制
def test_clamp_max_chars_bounds() -> None:
    assert clamp_max_chars(0, _CFG) == _CFG.max_chars_default
    assert clamp_max_chars(-100, _CFG) == _CFG.max_chars_default
    assert clamp_max_chars(3000, _CFG) == 3000
    assert clamp_max_chars(999_999, _CFG) == _CFG.max_chars_hard_cap


def test_clamp_max_chars_has_a_floor() -> None:
    """下限不是溺爱调用方：预算小到装不下「一条结果 + meta」时，输出会变成 0 条，
    而模型分不清「预算不够」和「上游没搜到」——这两种情况需要的后续动作完全相反。"""
    assert clamp_max_chars(10, _CFG) == _CFG.max_chars_floor
    assert _CFG.max_chars_floor < _CFG.max_chars_default < _CFG.max_chars_hard_cap


def test_clamp_max_results_bounds() -> None:
    assert clamp_max_results(0, _CFG) == _CFG.max_results_default
    assert clamp_max_results(3, _CFG) == 3
    assert clamp_max_results(999, _CFG) == _CFG.max_results_hard_cap


# ---------------------------------------------------------------- 不可用 / 无结果
def test_unavailable_is_distinguishable_from_no_results() -> None:
    out = format_text(_result([], available=False, message="SearXNG 未响应"), _CFG)
    assert "暂不可用" in out and "SearXNG 未响应" in out
    assert "未搜到" not in out  # 服务挂了不能说成没有结果，那会让模型停止重试


def test_unavailable_does_not_raise_with_partial_state() -> None:
    """降级路径不许抛异常：run 崩掉的代价远大于一次搜索拿不到结果。"""
    out = format_text(_result([_doc(1)], available=False, message=""), _CFG)
    assert "暂不可用" in out


def test_no_results_reports_reason_and_unresponsive_engines() -> None:
    out = format_text(
        _result(
            [],
            query="一个非常冷门的查询",
            errors=["route original failed: timeout"],
            unresponsive=["google", "brave"],
            # 召回基底健康（四路在出数）→ 走「未搜到」分支，本用例测的是诊断信息不丢
            stats={"contributing_engines": ["sogou", "yandex", "yahoo", "naver"]},
        ),
        _CFG,
    )
    assert "未搜到" in out and "一个非常冷门的查询" in out
    assert "timeout" in out
    assert "google" in out and "brave" in out
    assert out.startswith("未搜到") or "meta:" in out


def test_thin_recall_leads_with_degradation_not_no_results() -> None:
    """召回基底太薄时，「未搜到」是一句关于互联网的错误陈述。

    实测场景：5 个引擎被 SearXNG 集体挂起，只剩 bing 一路出**无关**结果，精排如实判为
    全不相关 → 0 条。若这里还说「未搜到与 X 相关的结果」，模型会据此回答「网上没这方面的
    资料」。所以首句必须是降级说明（模型往往只读第一句），且要明确否认「等于没资料」。
    """
    out = format_text(
        _result(
            [],
            query="Kubernetes Pod CrashLoopBackOff 排查步骤",
            errors=["召回 41 条候选，但精排分数全部低于阈值 0.05"],
            unresponsive=["brave", "duckduckgo", "google", "google cse", "mojeek"],
            stats={"contributing_engines": ["bing"]},
        ),
        _CFG,
    )
    assert out.startswith("网页搜索降级")
    assert "只有 1 个引擎给出了结果（bing）" in out
    assert "5 个搜索引擎无响应" in out
    assert "不等于" in out and "召回被掐断" in out
    assert "未搜到" not in out
    # 降级说明不能把诊断信息挤掉：原因与引擎清单仍需在输出里
    assert "低于阈值" in out and "unresponsive_engines:" in out


def test_thin_recall_is_degraded_even_when_no_engine_reported_an_error() -> None:
    """旧判据（数无响应引擎）漏掉的正是这个用例：没有任何引擎报错，但只有一路在出数。

    实测最坑的引擎不报错：本机 bing 对中文查询稳定返回 10 条**无关**结果（查
    「什么是向量数据库」得 CRAN 下载页），它永远不会出现在 unresponsive_engines 里。
    所以判据必须是「有几个引擎真的在出数」，而不是「有几个引擎报了错」。
    """
    out = format_text(
        _result([], query="什么是向量数据库", stats={"contributing_engines": ["bing"]}),
        _CFG,
    )
    assert out.startswith("网页搜索降级") and "未搜到" not in out


def test_healthy_recall_base_makes_no_results_an_honest_statement() -> None:
    """阈值边界：6 路引擎都在出数、精排仍判全不相关 → 「未搜到」就是实话。

    不能因为有一两个引擎挂起（baidu/quark 被验证码挂起是常态）就改口说降级：
    那会让模型对一个真实存在的「没相关资料」结论无休止地重试。
    """
    out = format_text(
        _result(
            [],
            query="冷门查询",
            unresponsive=["baidu", "quark"],
            stats={"contributing_engines": ["sogou", "yandex", "yahoo", "naver", "yep", "mwmbl"]},
        ),
        _CFG,
    )
    assert out.startswith("未搜到")
    assert "网页搜索降级" not in out
    assert "unresponsive_engines: baidu,quark" in out  # 挂起的引擎仍要如实上报


# ---------------------------------------------------------------- 预算截断
def test_output_never_exceeds_budget() -> None:
    """硬承诺：任何情况下 len(输出) ≤ max_chars。"""
    docs = [_doc(i, passages=["内容" * 250 for _ in range(3)]) for i in range(1, 6)]
    for budget in (800, 1200, 3000, 6000):
        result = _result(docs, max_chars=budget)
        out = format_text(result, _CFG)
        assert len(out) <= clamp_max_chars(budget, _CFG), f"budget={budget} 时输出 {len(out)} 字符"


def test_truncation_marks_meta_and_drops_the_tail() -> None:
    """截断按序进行：保排序、不保数量，并在 meta 里如实说 dropped/truncated。"""
    docs = [_doc(i, passages=["内容" * 175]) for i in range(1, 6)]
    titles = [d.title for d in docs]
    result = _result(docs, max_chars=1200)
    out = format_text(result, _CFG)

    present = [t for t in titles if t in out]
    assert 0 < len(present) < len(titles)  # 确实截断了，但没截成 0 条
    assert present == titles[: len(present)]  # 留下的一定是排序最靠前的 K 条
    assert "truncated=yes" in out
    assert f"dropped={len(titles) - len(present)}" in out
    assert out.index(present[0]) < out.index(present[-1])  # 顺序不变


def test_tiny_budget_still_yields_one_result() -> None:
    """预算被钳到下限后，第一条必须还在（哪怕被裁短）：0 条等于把话说反。"""
    docs = [_doc(i, passages=["内容" * 250 for _ in range(3)]) for i in range(1, 4)]
    out = format_text(_result(docs, max_chars=10), _CFG)
    assert "1." in out and "T1" in out
    assert len(out) <= _CFG.max_chars_floor


def test_no_truncation_flag_when_everything_fits() -> None:
    out = format_text(_result([_doc(i, passages=["短片段"]) for i in range(1, 4)]), _CFG)
    assert "truncated" not in out
    assert "dropped=" not in out


# ---------------------------------------------------------------- 自报一致性
def test_reported_chars_equals_actual_length() -> None:
    """meta 里的 chars= 必须等于返回文本的真实长度（含 meta 行自己）。

    自报长度是评测算输出预算的唯一依据，差一位就是数据不可信。
    """
    for budget in (800, 1500, 6000):
        docs = [_doc(i, passages=["内容" * 200]) for i in range(1, 6)]
        result = _result(docs, max_chars=budget)
        out = format_text(result, _CFG)
        assert f"chars={len(out)}" in out
        assert result.output_chars == len(out)


def test_long_query_in_head_is_clipped() -> None:
    """回显查询也要计入预算：一条 500 字的 query 不该把结果挤掉。"""
    docs = [_doc(i, passages=["内容" * 175]) for i in range(1, 4)]
    out = format_text(_result(docs, query="很" * 500, max_chars=1200), _CFG)
    assert len(out) <= 1200
    assert "…" in out.splitlines()[0]


# ---------------------------------------------------------------- meta 行
def test_meta_line_reports_every_stage() -> None:
    result = _result(
        [_doc(1)],
        stats={
            "routes": 3,
            "raw": 120,
            "after_dedup": 76,
            "window": 50,
            "route_agreement": 0.83,
            "extracted": 3,
            "extract_attempted": 3,
        },
        flags={"rerank": "ok", "extract": "3/3", "cache": "miss"},
    )
    line = meta_line(result)
    for expect in (
        "meta: ",
        "routes=3",
        "recall=120",
        "dedup=76",
        "window=50",
        "agreement=0.83",
        "rerank=ok",
        "extract=3/3",
        "cache=miss",
        "elapsed=8.4s",
    ):
        assert expect in line, expect


def test_meta_line_composes_extract_flag_when_not_counted() -> None:
    """flags 里没有「已完成/尝试」形式时，用 stats 拼出来并附上状态，不丢信息。"""
    result = _result(
        [_doc(1)],
        stats={"extracted": 2, "extract_attempted": 3},
        flags={"extract": "skipped"},
    )
    assert "extract=2/3:skipped" in meta_line(result)


def test_meta_line_surfaces_degradation_and_unresponsive_engines() -> None:
    """降级必须写在脸上：静默降级会让「结果质量差」变成查不出原因的玄学。"""
    result = _result(
        [_doc(1)],
        flags={"rerank": "unavailable", "degraded": "rerank_unavailable"},
        unresponsive=["google", "brave", "mojeek"],
        errors=["route extra failed"],
    )
    line = meta_line(result)
    assert "rerank=unavailable" in line
    assert "degraded=rerank_unavailable" in line
    assert "unresponsive=google,brave,mojeek" in line
    assert "errors=1" in line


def test_meta_line_defaults_are_honest_when_stats_missing() -> None:
    """没有统计数据时报 0 与 '-'，而不是编一个好看的数字。"""
    line = meta_line(_result([_doc(1)]))
    assert "recall=0" in line
    assert "rerank=-" in line
    assert "cache=miss" in line


# ---------------------------------------------------------------- 单条渲染
def test_passage_rendering_limits_and_clipping() -> None:
    """每篇最多渲染 3 段、单段超长按 passage_chars 截断并标省略号。"""
    passages = [f"第{i}段" + "内容" * 250 for i in range(1, 6)]
    out = format_text(_result([_doc(1, passages=passages)]), _CFG)
    assert "第1段" in out and "第3段" in out
    assert "第4段" not in out and "第5段" not in out  # 超出 3 段的证据不进输出
    assert out.count("▸") == 3
    assert "…" in out  # 单段被 passage_chars 截断


def test_falls_back_to_snippet_when_no_passages() -> None:
    """没抽到正文就用引擎摘要兜底：留空片段等于让模型只看到标题。"""
    out = format_text(_result([_doc(1, snippet="这是引擎给的摘要")]), _CFG)
    assert "这是引擎给的摘要" in out


def test_missing_title_and_date_render_placeholders() -> None:
    out = format_text(_result([_doc(1, title="", snippet="摘要")]), _CFG)
    assert "(无标题)" in out
    # 无日期时不留空的 "| " 尾巴
    assert "example.com\n" in out or out.count("|") >= 1


# ---------------------------------------------------------------- format_fetch
def test_format_fetch_unavailable() -> None:
    out = format_fetch({"available": False, "message": "403 Forbidden", "url": "https://x.com/a"})
    assert "抓取失败" in out and "403 Forbidden" in out and "https://x.com/a" in out


def test_format_fetch_renders_passages_before_full_text() -> None:
    """精选片段放在正文之前：模型读到预算耗尽时，先看到的应该是最相关的证据。"""
    out = format_fetch(
        {
            "available": True,
            "url": "https://x.com/a",
            "site": "x.com",
            "source": "crawl4ai",
            "chars_total": 12345,
            "chunks": 30,
            "elapsed_ms": 2100,
            "truncated": True,
            "passages": ["最相关的片段一", "次相关的片段二"],
            "text": "这里是完整正文。",
        }
    )
    assert out.startswith("网页正文：https://x.com/a")
    assert "source=crawl4ai" in out and "chars=12345" in out and "chunks=30" in out
    assert "elapsed=2.1s" in out and "truncated=yes" in out
    assert "▸ 最相关的片段一" in out
    assert out.index("最相关的片段一") < out.index("这里是完整正文。")


def test_format_fetch_without_passages_skips_that_section() -> None:
    out = format_fetch(
        {"available": True, "url": "https://x.com/a", "source": "trafilatura", "text": "正文"}
    )
    assert "与问题最相关的片段" not in out
    assert "正文：" in out


# ---------------------------------------------------------------- REST JSON
def test_to_json_carries_evaluation_inputs() -> None:
    """评测脚本靠 JSON 拿候选窗口与各路排名：这些字段不在文本输出里，只能从这里走。"""
    docs = [_doc(1), _doc(2)]
    result = _result(docs)
    result.route_lists = {"original": ["https://example.com/p1"], "keyword": ["https://example.com/p2"]}
    data = to_json(result, _CFG)

    assert data["query"] == "向量数据库的索引结构"
    assert data["stage_enabled"] == list(STAGES)
    assert data["max_chars"] == _CFG.max_chars_default
    assert data["window_urls"] == ["https://example.com/p1", "https://example.com/p2"]
    assert data["route_lists"]["keyword"] == ["https://example.com/p2"]
    assert data["timings"] == [{"stage": "recall", "ms": 1200, "detail": ""}]
    first = data["results"][0]
    assert first["rerank_score"] == 0.9
    assert first["passage_source"] == "snippet"
    assert first["norm_url"] == "https://example.com/p1"
    assert "utm_source" not in first["norm_url"]  # 归一化后的 URL 才是去重与标注的键


def test_to_json_reflects_clamped_budget() -> None:
    data = to_json(_result([_doc(1)], max_chars=999_999), _CFG)
    assert data["max_chars"] == _CFG.max_chars_hard_cap
