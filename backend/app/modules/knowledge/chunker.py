"""markdown 结构化切分：块 512±128 token（粗估 token = chars/2，中文为主），带标题路径。"""

import re

TARGET_TOKENS = 512  # 目标块大小
MIN_TOKENS = 384  # 512 - 128
MAX_TOKENS = 640  # 512 + 128
_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")


def est_tokens(text: str) -> int:
    """粗估 token 数（中文 ≈ 0.5 token/char 的保守估计）。"""
    return max(1, len(text) // 2)


def _is_heading(line: str) -> tuple[int, str] | None:
    m = _HEADING.match(line.strip())
    if m is None:
        return None
    return len(m.group(1)), m.group(2).strip()


def chunk_markdown(md: str) -> list[dict]:
    """按标题分节 → 节内段落累积切块。

    返回 [{"content", "heading_path", "token_count"}]，heading_path 形如
    "第一章 > 1.1 概述"（文档结构元数据，检索结果上下文化用）。
    """
    headings: list[tuple[int, str]] = []  # (level, title) 栈
    sections: list[tuple[str, str]] = []  # (heading_path, body lines)
    cur_path = ""
    cur_lines: list[str] = []

    def flush() -> None:
        if cur_lines:
            sections.append((cur_path, "\n".join(cur_lines).strip()))

    for line in md.splitlines():
        h = _is_heading(line)
        if h is not None:
            flush()
            level, title = h
            while headings and headings[-1][0] >= level:
                headings.pop()
            headings.append((level, title))
            cur_path = " > ".join(t for _, t in headings)
            cur_lines = []
        else:
            cur_lines.append(line)
    flush()

    chunks: list[dict] = []
    for heading_path, body in sections:
        if not body:
            continue
        if est_tokens(body) <= MAX_TOKENS:
            chunks.append(_mk(body, heading_path))
            continue
        # 超长节：段落（空行分隔）累积，攒到目标附近切；单段超限强切
        para_buf: list[str] = []
        buf_tokens = 0
        for para in re.split(r"\n\s*\n", body):
            pt = est_tokens(para)
            if pt > MAX_TOKENS:  # 单段超限：先冲刷缓冲，再硬切该段
                if para_buf:
                    chunks.append(_mk("\n\n".join(para_buf), heading_path))
                    para_buf, buf_tokens = [], 0
                chunks.extend(_hard_split(para, heading_path))
                continue
            if buf_tokens + pt > TARGET_TOKENS and para_buf:
                chunks.append(_mk("\n\n".join(para_buf), heading_path))
                para_buf, buf_tokens = [], 0
            para_buf.append(para)
            buf_tokens += pt
        if para_buf:
            chunks.append(_mk("\n\n".join(para_buf), heading_path))
    return chunks


def _hard_split(para: str, heading_path: str) -> list[dict]:
    """单段超长：按句号/换行边界硬切到目标大小。"""
    out: list[dict] = []
    sentences = re.split(r"(?<=[。！？；.!?;])\s*", para)
    buf: list[str] = []
    buf_tokens = 0
    for s in sentences:
        st = est_tokens(s)
        if buf and buf_tokens + st > TARGET_TOKENS:
            out.append(_mk("".join(buf), heading_path))
            buf, buf_tokens = [], 0
        buf.append(s)
        buf_tokens += st
    if buf:
        out.append(_mk("".join(buf), heading_path))
    return out


def _mk(content: str, heading_path: str) -> dict:
    content = content.strip()
    return {
        "content": content,
        "heading_path": heading_path or None,
        "token_count": est_tokens(content),
    }
