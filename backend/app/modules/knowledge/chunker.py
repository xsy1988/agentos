"""markdown 结构化切分（langchain-text-splitters）：标题分节 + 节内按尺寸二切。

官方推荐组合：MarkdownHeaderTextSplitter 按标题层级分节（标题路径进 metadata），
RecursiveCharacterTextSplitter 节内递归切分（separators 支持中文句读，带 overlap）。
首切与重切（文档详情页 rechunk）走同一条代码路径，target_tokens/overlap_tokens
 可调；字符→token 粗估沿用 chars/2（中文为主）。
"""

from langchain_text_splitters import (
    MarkdownHeaderTextSplitter,
    RecursiveCharacterTextSplitter,
)

TARGET_TOKENS = 512  # 默认目标块大小（token）
OVERLAP_TOKENS = 64  # 默认相邻块重叠（token）

_HEADERS = [("#", "h1"), ("##", "h2"), ("###", "h3"), ("####", "h4")]


def est_tokens(text: str) -> int:
    """粗估 token 数（中文 ≈ 0.5 token/char 的保守估计）。"""
    return max(1, len(text) // 2)


def chunk_markdown(
    md: str, target_tokens: int = TARGET_TOKENS, overlap_tokens: int = OVERLAP_TOKENS
) -> list[dict]:
    """markdown → 块列表。

    返回 [{"content", "heading_path", "token_count"}]，heading_path 形如
    "第一章 > 1.1 概述"（文档结构元数据，检索结果上下文化用）。标题行保留在
    块内（strip_headers=False，检索命中自带章节上下文）；仅标题无正文的空节
    过滤（与旧手写切分器行为一致）。
    """
    if target_tokens < 64:
        target_tokens = 64
    if not 0 <= overlap_tokens < target_tokens // 2:
        overlap_tokens = target_tokens // 8

    header_splitter = MarkdownHeaderTextSplitter(_HEADERS, strip_headers=False)
    size_splitter = RecursiveCharacterTextSplitter(
        chunk_size=target_tokens * 2,  # token 粗估 chars/2
        chunk_overlap=overlap_tokens * 2,
        separators=["\n\n", "\n", "。", "，", " ", ""],
    )
    sections = header_splitter.split_text(md)
    pieces = size_splitter.split_documents(sections)

    chunks: list[dict] = []
    for p in pieces:
        heading_path = " > ".join(str(v) for v in p.metadata.values() if v)
        content = p.page_content.strip()
        if not _has_body(content, heading_path):
            continue
        chunks.append(
            {
                "content": content,
                "heading_path": heading_path or None,
                "token_count": est_tokens(content),
            }
        )
    return chunks


def _has_body(content: str, heading_path: str) -> bool:
    """仅标题无正文的空节过滤：内容去掉标题行后无剩余即空节。"""
    headings = {h.strip() for h in heading_path.split(">") if h.strip()}
    lines = [ln.strip() for ln in content.splitlines()]
    body = [
        ln
        for ln in lines
        if ln and not (ln.startswith("#") and ln.lstrip("#").strip() in headings)
    ]
    return bool(body)
