"""抽取层单测：中文二元组分词、词覆盖预筛、片段级两级漏斗、退回摘要的诚实性。

这一层的核心判断是「引擎摘要常常恰好错过真正回答问题的那一段」，所以要在片段级别
再选一次。而 CPU 上 cross-encoder 每一对都是几百毫秒，一页几十块全送上去就是十几秒——
**预筛因此不是可有可无的优化，而是让这一层能存在的前提**。

分词方式在这里是质量参数：中文按标点切会让整句成为一个词元，query 与片段几乎不可能
字面全同，词覆盖分恒为 0，片段选择就退化成「取前两块」——而技术页的前两块通常是导航与导语。
"""

import asyncio
from typing import Any

from websearch.chunk import chunk_text
from websearch.config import Settings
from websearch.extract import (
    ExtractOutcome,
    _prefilter,
    _squash,
    _tokenize,
    lexical_scores,
    pick_passages,
)
from websearch.types import Doc

_ANSWER = "BGE-Reranker-v2-m3 的参数量为 568M，最大上下文长度是 8192 个 token。"
_NAV = "首页 文档 博客 关于我们 联系方式 搜索 登录 注册 " * 4
_NOISE = "这一段讲的是无关的推荐位与广告内容，没有任何值得引用的信息。" * 2
_QUERY = "BGE-Reranker-v2-m3 的参数量与最大上下文长度"


def _cfg(**kw: Any) -> Settings:
    base: dict[str, Any] = {
        "chunk_size": 80,
        "chunk_overlap": 0,
        "passages_per_doc": 2,
        "passage_candidates": 4,
        "passage_score_threshold": 0.30,
        "passage_chars": 400,
    }
    base.update(kw)
    return Settings(_env_file=None, **base)


def _fake_cfg(fake: Any, **kw: Any) -> Settings:
    return _cfg(reranker_url=fake.cfg.reranker_url, **kw)


def _doc(snippet: str = "引擎给的摘要：bge-reranker-v2-m3 模型卡") -> Doc:
    return Doc(
        url="https://huggingface.co/BAAI/bge-reranker-v2-m3",
        norm_url="https://huggingface.co/BAAI/bge-reranker-v2-m3",
        title="BGE Reranker v2 m3",
        snippet=snippet,
    )


def _page() -> str:
    """一页典型技术页：导航在前、答案在中后段、中间夹无关推荐位。"""
    return f"{_NAV}\n\n{_NOISE}\n\n{_ANSWER}\n\n{_NOISE}"


# ---------------------------------------------------------------- 分词
def test_tokenize_cjk_uses_character_bigrams() -> None:
    """中文按字符二元组切：零成本、不需要分词模型，且能让「参数量」与「参数」部分重叠。"""
    assert _tokenize("向量数据库") == {"向量", "量数", "数据", "据库"}


def test_tokenize_single_cjk_char_kept() -> None:
    """单字段落只有一个字时也要留下，否则这类片段词覆盖分恒为 0。"""
    assert _tokenize("库") == {"库"}


def test_tokenize_keeps_latin_entities_intact() -> None:
    """型号名、API 名必须整词保留：连字符与下划线不是分词边界。

    把 `bge-reranker-v2-m3` 拆成 bge / reranker / v2 / m3 会让它匹配上一堆无关页面；
    把 `max_length` 拆成 max / length 更是直接丢掉了判别力最强的那部分。
    """
    assert "bge-reranker-v2-m3" in _tokenize("BGE-Reranker-v2-m3")
    assert "max_length" in _tokenize("设置 max_length 参数")
    assert "context_compact_threshold" in _tokenize("context_compact_threshold=24000")


def test_tokenize_drops_single_latin_chars_and_splits_on_punctuation() -> None:
    assert _tokenize("a b cd") == {"cd"}  # 单字母是纯噪声
    toks = _tokenize("向量，数据库")
    assert "向量" in toks and "数据" in toks
    assert "量数" not in toks  # 标点是真实边界，不跨它拼二元组


def test_tokenize_empty() -> None:
    assert _tokenize("") == set()
    assert _tokenize("   ") == set()


# ---------------------------------------------------------------- 词覆盖分
def test_lexical_scores_ranks_relevant_above_unrelated() -> None:
    scores = lexical_scores("向量数据库", ["向量数据库的索引结构详解", "今天天气很好适合出门"])
    assert scores[0] > scores[1]
    assert scores[1] == 0.0


def test_lexical_scores_penalises_very_short_passages() -> None:
    """覆盖率为主、长度做轻微加权：只有一两个词的片段信息量不足，不该与整段并列。"""
    short, long_ = lexical_scores(
        "向量数据库", ["向量数据库", "向量数据库的索引结构与实现细节说明"]
    )
    assert short < long_


def test_lexical_scores_degenerate_inputs() -> None:
    assert lexical_scores("", ["任意内容"]) == [0.0]
    assert lexical_scores("向量数据库", []) == []
    assert lexical_scores("向量数据库", ["！！！"]) == [0.0]  # 无词元的片段


def test_lexical_scores_are_deterministic_and_rounded() -> None:
    a = lexical_scores(_QUERY, [_ANSWER, _NAV])
    b = lexical_scores(_QUERY, [_ANSWER, _NAV])
    assert a == b
    assert all(round(x, 4) == x for x in a)


# ---------------------------------------------------------------- 预筛
def test_prefilter_surfaces_the_answer_chunk_from_the_tail() -> None:
    """预筛的真实价值：答案在中后段的技术页里，「取前 N 块」必然漏掉它。"""
    chunks = chunk_text(_page(), 80, 0)
    answer_idx = next(i for i, c in enumerate(chunks) if "8192" in c)
    assert answer_idx > 1, "测试前提：答案不该落在前两块（否则这条断言证明不了任何事）"
    assert answer_idx in _prefilter(_QUERY, chunks, 4)


def test_prefilter_returns_ascending_indices_within_n() -> None:
    chunks = chunk_text(_page(), 80, 0)
    picked = _prefilter(_QUERY, chunks, 3)
    assert picked == sorted(picked)  # 升序：片段按原文顺序读才连贯
    assert len(picked) <= 3
    assert all(0 <= i < len(chunks) for i in picked)


def test_prefilter_clamps_n() -> None:
    chunks = ["一块", "两块", "三块"]
    assert _prefilter("一块", chunks, 99) == [0, 1, 2]
    assert len(_prefilter("一块", chunks, 0)) == 1  # n≤0 也至少给一个候选
    assert _prefilter("一块", [], 4) == []


def test_prefilter_uniform_sampling_covers_head_and_tail() -> None:
    """词覆盖分全为 0（中文 query 配纯英文页）时改为均匀取样，且**必须覆盖到尾部**。

    按 `int(j * len/n)` 取样时最后 1/n 的正文永远进不了精排，
    而技术页的答案恰恰常在中后段——那等于在最需要兜底的情况下偏心得最厉害。
    """
    chunks = [f"pure english chunk number {i} about databases" for i in range(9)]
    assert lexical_scores("向量数据库", chunks) == [0.0] * 9  # 前提：确实全无信号
    picked = _prefilter("向量数据库", chunks, 3)
    assert picked == [0, 4, 8]


def test_prefilter_uniform_sampling_single_candidate() -> None:
    chunks = [f"english chunk {i}" for i in range(5)]
    assert _prefilter("向量数据库", chunks, 1) == [0]


# ---------------------------------------------------------------- 片段选择
def test_pick_passages_falls_back_to_snippet_without_text() -> None:
    """没有正文就用引擎摘要，不留空片段：留空等于让模型只看到标题。"""
    passages, scores, source = _run_pick("", "none", _cfg(reranker_url="http://127.0.0.1:9"))
    assert passages == ["引擎给的摘要：bge-reranker-v2-m3 模型卡"]
    assert scores == []
    assert source == "snippet"


def test_pick_passages_without_text_or_snippet() -> None:
    doc = _doc(snippet="")
    passages, _, source = _run_pick("", "none", _cfg(reranker_url="http://127.0.0.1:9"), doc=doc)
    assert passages == []
    assert source == "none"


def test_pick_passages_short_text_skips_reranking() -> None:
    """块数不超过要留的段数时直接全给：一次 cross-encoder 调用都不必发。"""
    cfg = _cfg(reranker_url="http://127.0.0.1:9")  # 不可达；若真发了请求就会走词覆盖兜底
    passages, scores, source = _run_pick("一句话讲完了。", "trafilatura", cfg)
    assert passages == ["一句话讲完了。"]
    assert scores == []  # 没有分数 = 没有精排过，下游不会把它当成 cross-encoder 的输出
    assert source == "trafilatura"


def test_pick_passages_uses_lexical_order_when_reranker_down() -> None:
    """reranker 不可用 → 在预筛候选里按词覆盖分定序，仍然交出最相关的段落。"""
    cfg = _cfg(reranker_url="http://127.0.0.1:9")
    passages, scores, source = _run_pick(_page(), "trafilatura", cfg)
    assert len(passages) == cfg.passages_per_doc
    assert any("8192" in p for p in passages), f"词覆盖兜底也该选中答案段：{passages}"
    assert scores and scores == sorted(scores, reverse=True)
    assert source == "trafilatura"  # 正文来源不变，只是排序退了一级


def test_pick_passages_two_level_funnel_with_reranker(fake_reranker: Any) -> None:
    """完整两级漏斗：预筛把几十块压到 4 块，cross-encoder 在 4 块里定胜负。"""

    def responder(req: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        docs = req["documents"]
        assert len(docs) <= 4, "预筛没起作用：一页几十块全送上来了"
        scored = [(i, 0.95 if "8192" in d else 0.42) for i, d in enumerate(docs)]
        scored.sort(key=lambda x: -x[1])
        return 200, {"results": [{"index": i, "score": s} for i, s in scored[:2]]}

    with fake_reranker(responder) as fake:
        cfg = _fake_cfg(fake)
        passages, scores, source = _run_pick(_page(), "crawl4ai", cfg)
    assert len(passages) == 2
    assert "8192" in passages[0]  # 分数最高的那段排在前面
    assert scores[0] == 0.95 and scores[1] == 0.42
    assert source == "crawl4ai"


def test_pick_passages_below_threshold_falls_back_to_snippet(fake_reranker: Any) -> None:
    """全部候选都没过阈值 → 这页大概率不回答问题，退回摘要比硬塞一段更诚实。"""
    with fake_reranker(lambda req: (200, {"results": [{"index": 0, "score": 0.05}]})) as fake:
        cfg = _fake_cfg(fake, passage_score_threshold=0.30)
        passages, scores, source = _run_pick(_page(), "trafilatura", cfg)
    assert passages == [_squash(_doc().snippet)[: cfg.passage_chars]]
    assert source == "snippet"
    assert scores == [0.05]  # 分数照样报出来：让人看得见「为什么退回了摘要」


def test_pick_passages_keeps_only_passages_above_threshold(fake_reranker: Any) -> None:
    with fake_reranker(
        lambda req: (
            200,
            {"results": [{"index": 0, "score": 0.88}, {"index": 1, "score": 0.12}]},
        )
    ) as fake:
        cfg = _fake_cfg(fake, passage_score_threshold=0.30)
        passages, scores, _ = _run_pick(_page(), "trafilatura", cfg)
    assert len(passages) == 1 and scores == [0.88]


def test_pick_passages_ignores_out_of_range_indices(fake_reranker: Any) -> None:
    """reranker 回了越界下标 → 不能 IndexError，退回摘要即可。"""
    with fake_reranker(lambda req: (200, {"results": [{"index": 99, "score": 0.9}]})) as fake:
        passages, _, source = _run_pick(_page(), "trafilatura", _fake_cfg(fake))
    assert passages and source == "snippet"


def test_pick_passages_clips_to_passage_chars(fake_reranker: Any) -> None:
    with fake_reranker(
        lambda req: (200, {"results": [{"index": 0, "score": 0.9}, {"index": 1, "score": 0.8}]})
    ) as fake:
        cfg = _fake_cfg(fake, passage_chars=30)
        passages, _, _ = _run_pick(_page(), "trafilatura", cfg)
    assert passages and all(len(p) <= 30 for p in passages)


def test_pick_passages_respects_passages_per_doc(fake_reranker: Any) -> None:
    """服务端多给了也不能多留：「每篇 N 段」是输出预算的约定，不能依赖对方的自觉。"""

    def generous(req: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        results = [
            {"index": i, "score": 0.9 - i * 0.1} for i in range(len(req["documents"]))
        ]
        return 200, {"results": results}

    with fake_reranker(generous) as fake:
        cfg = _fake_cfg(fake, passages_per_doc=1)
        passages, scores, _ = _run_pick(_page(), "trafilatura", cfg)
    assert len(passages) == 1 and len(scores) == 1


def test_passages_and_scores_stay_index_aligned(fake_reranker: Any) -> None:
    """两个数组是按位置配对的（写进 doc.passage_scores），长度必须相等。

    只筛片段不筛分数，会让第 N 段的分数挂到第 M 段上——不报错，只是让评测与排障
    看到的归因全是错的。
    """
    with fake_reranker(
        lambda req: (
            200,
            {"results": [{"index": 0, "score": 0.88}, {"index": 1, "score": 0.12}]},
        )
    ) as fake:
        cfg = _fake_cfg(fake, passage_score_threshold=0.30, passages_per_doc=2)
        passages, scores, _ = _run_pick(_page(), "trafilatura", cfg)
    assert len(passages) == len(scores)


# ---------------------------------------------------------------- 小工具
def test_squash_collapses_whitespace() -> None:
    """输出预算按字符算，片段里的多余空白是纯浪费。"""
    assert _squash("  a\n\n  b\tc ") == "a b c"
    assert _squash("") == ""


def test_extract_outcome_counts_sources() -> None:
    out = ExtractOutcome()
    out.note("trafilatura")
    out.note("trafilatura")
    out.note("snippet")
    assert out.sources == {"trafilatura": 2, "snippet": 1}
    assert out.attempted == 0 and out.extracted == 0  # 计数由调用方负责，note 只管来源


def _run_pick(
    text: str,
    source: str,
    cfg: Settings,
    *,
    query: str = _QUERY,
    doc: Doc | None = None,
) -> tuple[list[str], list[float], str]:
    return asyncio.run(pick_passages(query, doc or _doc(), text, source, cfg))
