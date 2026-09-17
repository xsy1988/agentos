"""正文切片：段落感知的递归切分。

为什么切片粒度是质量参数而不只是实现细节：片段级精排的输入就是这些块。块太大 → 一个块里
混进三件事，cross-encoder 分数被稀释，且输出预算被无关文字吃掉；块太小 → 关键句被切断，
「BGE-Reranker-v2-m3 的上下文长度是 8192」可能正好跨块，两头都不完整。

策略与 knowledge/chunker.py 同思路（服务内独立实现，不跨进程引用平台代码）：
按分隔符优先级递归下钻——空行 > 句号/换行 > 分号逗号 > 空格 > 硬切；
优先在句子边界收尾，块间保留 overlap 以兜住跨块信息。
"""

import re

# 分隔符优先级：先试段落，再试句子，再试子句
_SEPARATORS = ("\n\n", "\n", "。", "！", "？", ". ", "! ", "? ", "；", "; ", "，", ", ", " ")
# markdown/HTML 残留：标题井号、列表符号、加粗星号、行内代码。
# 下划线只当**成对**（`__x__`）时才剥：单下划线在技术正文里绝大多数是标识符的一部分
# （depends_on、max_length、context_compact_threshold），剥掉它会把答案串改错字——
# 评测的 answer_spans 与模型的引用都会因此对不上
_NOISE_RE = re.compile(r"(?m)^\s{0,3}#{1,6}\s*|^\s*[*+\-]\s+|[*`]{1,3}|_{2,3}")
_MULTI_SPACE_RE = re.compile(r"[ \t]{2,}")
_MULTI_NL_RE = re.compile(r"\n{3,}")


def clean_text(raw: str) -> str:
    """清洗抽取到的正文（去 markdown 装饰、压空白），不改语义文字。"""
    text = _NOISE_RE.sub("", raw or "")
    text = _MULTI_SPACE_RE.sub(" ", text)
    text = _MULTI_NL_RE.sub("\n\n", text)
    return text.strip()


def _split_by(text: str, sep: str) -> list[str]:
    """按分隔符切并保留分隔符（避免句号被吃掉导致片段不可读）。"""
    if sep == "\n\n":
        parts = [p for p in text.split(sep)]
    else:
        chunks = text.split(sep)
        parts = [c + sep for c in chunks[:-1]]
        if chunks[-1]:
            parts.append(chunks[-1])
    return [p for p in parts if p.strip()]


def _recursive_split(text: str, size: int, level: int = 0) -> list[str]:
    """递归下钻到 size 以内。level 越深用的分隔符越细。"""
    if len(text) <= size:
        return [text] if text.strip() else []
    if level >= len(_SEPARATORS):
        # 分隔符用尽（如超长无空格串）→ 硬切，保证不丢内容也不无限递归
        return [text[i : i + size] for i in range(0, len(text), size)]
    parts = _split_by(text, _SEPARATORS[level])
    if len(parts) <= 1:
        return _recursive_split(text, size, level + 1)
    out: list[str] = []
    for p in parts:
        out.extend(_recursive_split(p, size, level + 1) if len(p) > size else [p])
    return out


def chunk_text(text: str, size: int = 500, overlap: int = 60) -> list[str]:
    """段落感知递归切分 → 合并到接近 size 的块，块间保留 overlap。

    返回块列表（已 strip，空块剔除）。overlap 通过「把上一块尾部若干字符前置到下一块」实现，
    比滑窗重复切分更省算力，效果等价于兜住跨块句子。
    """
    cleaned = clean_text(text)
    if not cleaned:
        return []
    size = max(80, size)
    overlap = max(0, min(overlap, size // 2))

    pieces = _recursive_split(cleaned, size)
    chunks: list[str] = []
    buf = ""
    for piece in pieces:
        piece = piece.strip()
        if not piece:
            continue
        if not buf:
            buf = piece
            continue
        if len(buf) + len(piece) + 1 <= size:
            buf = f"{buf}\n{piece}" if buf.endswith(("\n", "：", ":")) else f"{buf} {piece}"
            continue
        chunks.append(buf)
        tail = buf[-overlap:].strip() if overlap else ""
        if tail:
            carried = f"{tail} {piece}"
            # 带上 overlap 后若超出 size，就丢掉 overlap：宁可不兜跨块句子，
            # 也不能让块长突破上限——超出的部分会被 reranker 的 token 上限无声截掉，
            # 等于花了算力却让模型看不到块尾（chunk_size 与 passage_max_length 是成对调的）
            buf = carried if len(carried) <= size else piece
        else:
            buf = piece
    if buf:
        chunks.append(buf)
    return [c.strip() for c in chunks if c.strip()]
