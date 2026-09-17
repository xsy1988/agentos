"""切片层单测：清洗、递归切分、句子边界、overlap、块长上限。

切片粒度是**质量参数**而不是实现细节：片段级精排的输入就是这些块。
块太大 → 一个块里混三件事，cross-encoder 分数被稀释、输出预算被无关文字吃掉；
块太小 → 「上下文长度是 8192」这种关键句正好跨块，两头都不完整。
所以这里既测「不丢内容」，也测「块长不突破上限」——后者直接关系到 reranker 的 token 预算。
"""

from websearch.chunk import chunk_text, clean_text


def _sentences(n: int, body_chars: int = 140) -> list[str]:
    """造 n 句互不重叠、长度可控的中文句子（不含 markdown 噪声）。"""
    return [f"第{i}句" + "向量数据库的索引结构" * (body_chars // 10) + "。" for i in range(n)]


# ---------------------------------------------------------------- clean_text
def test_clean_text_strips_markdown_decoration() -> None:
    raw = "## 二级标题\n\n- 列表项一\n+ 列表项二\n**加粗**与`行内代码`以及*斜体*"
    out = clean_text(raw)
    assert "##" not in out and "**" not in out and "`" not in out
    assert "二级标题" in out and "列表项一" in out and "加粗" in out and "行内代码" in out


def test_clean_text_preserves_single_underscore_identifiers() -> None:
    """单下划线在技术正文里几乎都是标识符的一部分，剥掉等于把答案串改错字。

    这不是洁癖：评测的 answer_spans 与模型引用都按字面匹配，
    `depends_on` 被洗成 `dependson` 会让一条本来命中的片段被判成未命中。
    """
    raw = "在 compose 里配置 depends_on 与 max_length，以及 context_compact_threshold 阈值"
    out = clean_text(raw)
    assert "depends_on" in out
    assert "max_length" in out
    assert "context_compact_threshold" in out


def test_clean_text_strips_paired_underscore_emphasis() -> None:
    """成对下划线才是 markdown 强调，该剥。"""
    assert "重要" in clean_text("__重要__")
    assert "__" not in clean_text("__重要__")


def test_clean_text_collapses_whitespace() -> None:
    assert clean_text("a    b\t\tc") == "a b c"
    assert clean_text("一段\n\n\n\n二段") == "一段\n\n二段"
    assert clean_text("   首尾都有空白   ") == "首尾都有空白"


def test_clean_text_empty_input() -> None:
    assert clean_text("") == ""
    assert clean_text("   \n\n  ") == ""


# ---------------------------------------------------------------- 基本行为
def test_chunk_text_empty_input() -> None:
    assert chunk_text("") == []
    assert chunk_text("   ") == []


def test_short_text_is_a_single_chunk() -> None:
    assert chunk_text("一句话。", size=400) == ["一句话。"]


def test_short_sentences_are_merged_not_one_chunk_each() -> None:
    """碎句必须合并：一句一块会让片段级精排在几十块里挑，CPU 上是几十秒。"""
    text = "".join(f"句子{i}讲了向量数据库的第{i}个要点。" for i in range(30))
    chunks = chunk_text(text, size=200, overlap=0)
    assert 1 < len(chunks) < 30


# ---------------------------------------------------------------- 不变量
def test_no_chunk_exceeds_size_even_with_overlap() -> None:
    """块长上限是硬不变量：chunk_size 与 passage_max_length 是成对调的，
    超出的字符会被 reranker 的 token 上限无声截掉——花了算力却让模型看不到块尾。"""
    for overlap in (0, 30, 60):
        text = "".join(_sentences(12))
        chunks = chunk_text(text, size=200, overlap=overlap)
        assert chunks
        for c in chunks:
            assert len(c) <= 200, f"overlap={overlap} 时出现 {len(c)} 字符的块"


def test_no_content_is_lost() -> None:
    """每一句都必须能在某个块里找到：切片丢内容比切得难看严重得多。"""
    sentences = [f"句子{i}讲了向量数据库的第{i}个要点。" for i in range(30)]
    joined = "".join(chunk_text("".join(sentences), size=200, overlap=0))
    for s in sentences:
        assert s.rstrip("。") in joined


def test_splits_at_sentence_boundaries() -> None:
    """句子比 size 稍短时，一块一句、块尾落在句号上，不在句中硬断。"""
    text = "".join(_sentences(4, body_chars=140))
    chunks = chunk_text(text, size=200, overlap=0)
    assert len(chunks) == 4
    assert all(c.endswith("。") for c in chunks)


# ---------------------------------------------------------------- overlap
def test_overlap_carries_previous_tail_into_next_chunk() -> None:
    """overlap 的作用就是兜住跨块句子：下一块必须以上一块的尾部开头。"""
    text = "".join(_sentences(4, body_chars=140))
    chunks = chunk_text(text, size=200, overlap=50)
    assert len(chunks) >= 2
    tail = chunks[0][-50:]
    assert chunks[1].startswith(tail)


def test_overlap_is_dropped_when_it_would_burst_size() -> None:
    """放不下时宁可丢掉 overlap，也不能让块长突破 size（见 test_no_chunk_exceeds_size）。"""
    text = "".join(_sentences(4, body_chars=190))  # 每句约 194 字符，逼近 size
    chunks = chunk_text(text, size=200, overlap=60)
    assert all(len(c) <= 200 for c in chunks)
    assert all(c.startswith("第") for c in chunks)  # 尾部没有被前置进来


# ---------------------------------------------------------------- 兜底与钳制
def test_hard_cut_when_no_separator_exists() -> None:
    """超长无分隔符串（压缩过的 JSON、base64）必须硬切：既不无限递归也不丢内容。"""
    raw = "啊" * 1000
    chunks = chunk_text(raw, size=100, overlap=0)
    assert len(chunks) == 10
    assert all(len(c) == 100 for c in chunks)
    assert "".join(chunks) == raw


def test_size_floor_and_overlap_clamp() -> None:
    """size 有下限、overlap 不超过 size 的一半：调用方传了离谱值也不能把切片搞成逐字一块。"""
    text = "".join(_sentences(6))
    tiny = chunk_text(text, size=1, overlap=0)
    assert all(len(c) <= 80 for c in tiny)  # size 被抬到下限 80

    huge = chunk_text(text, size=200, overlap=10_000)
    assert all(len(c) <= 200 for c in huge)  # overlap 被钳到 100，仍不突破 size


def test_chunk_text_is_deterministic() -> None:
    text = "".join(_sentences(8))
    assert chunk_text(text, size=200, overlap=40) == chunk_text(text, size=200, overlap=40)
