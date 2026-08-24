"""knowledge 纯逻辑单测：chunker 切分与检索结果拼装。"""

from app.modules.knowledge.chunker import chunk_markdown, est_tokens
from app.modules.knowledge.search import format_hits


def test_chunk_small_doc_single_block():
    """短文档（< 640 token）整块输出，heading_path 保留标题路径。"""
    md = "# 规格\n\n星链助手 X1 功耗 15W。\n\n## 充电\n\nUSB-C 65W 快充。"
    chunks = chunk_markdown(md)
    assert len(chunks) == 2  # 「规格」与「充电」各一节
    assert chunks[0]["heading_path"] == "规格"
    assert chunks[1]["heading_path"] == "规格 > 充电"
    assert "65W" in chunks[1]["content"]


def test_chunk_long_section_accumulates_to_target():
    """超长节按段落累积切块，块大小落在 512±128 邻域（粗估 token）。"""
    para = "这是一段用于测试切分边界的中文文本，每段大约五十字左右。" * 3  # ~150 chars
    md = "# 长文\n\n" + "\n\n".join(para for _ in range(20))  # ~3000 chars
    chunks = chunk_markdown(md)
    assert len(chunks) > 1
    assert all(c["heading_path"] == "长文" for c in chunks)
    # 每块都在 [384, 640] 邻域（末块允许偏小）
    sizes = [c["token_count"] for c in chunks]
    assert all(384 <= s <= 640 for s in sizes[:-1])
    assert sizes[-1] > 0


def test_chunk_single_huge_paragraph_hard_split():
    """无空行的超长单段：按句子边界硬切。"""
    sentence = "这是同一句话会不断重复出现。" * 5  # ~70 chars/句
    md = "# 硬切\n\n" + sentence * 30  # ~2100 chars 无空行
    chunks = chunk_markdown(md)
    assert len(chunks) >= 2
    assert all(c["heading_path"] == "硬切" for c in chunks)


def test_est_tokens():
    assert est_tokens("") == 1
    assert est_tokens("a" * 100) == 50


def test_format_hits_empty_and_hit():
    assert "无结果" in format_hits([])
    hits = [
        {
            "doc_title": "规格书",
            "folder_path": "/产品知识",
            "heading_path": "保修政策",
            "content": "年费 399 元",
        }
    ]
    text = format_hits(hits)
    assert "规格书" in text and "保修政策" in text and "年费 399 元" in text
