"""token 估算（方案 §5 P1-2 前置闸用）。

不引第三方分词器：前置闸只需要"够早、够稳、宁多勿少"的量级判断，
真实 token 数由模型返回的 usage_metadata 事后修正（MeteringHook）。
估算偏保守（高估）：代价是提前拒绝，而不是"钱花了才炸"。

经验口径（中英混排实测）：CJK/全角标点 ≈ 1 token/字，其余 ≈ 1 token/4 字符。
"""

from collections.abc import Iterable
from typing import Any

# CJK 统一表意文字 + 中日韩标点 + 全角形式
_CJK_RANGES = ((0x2E80, 0x9FFF), (0xF900, 0xFAFF), (0xFE30, 0xFE4F), (0xFF00, 0xFF60))

# 每条消息的角色/分隔开销（服务端模板与特殊 token），与内容无关
_PER_MESSAGE_OVERHEAD = 4


def _is_cjk(ch: str) -> bool:
    code = ord(ch)
    return any(lo <= code <= hi for lo, hi in _CJK_RANGES)


def estimate_text_tokens(text: str) -> int:
    """文本 token 估算（空串为 0）。"""
    if not text:
        return 0
    cjk = sum(1 for ch in text if _is_cjk(ch))
    rest = len(text) - cjk
    return cjk + (rest + 3) // 4


def _content_text(content: Any) -> str:
    """取一条消息的文本视图：多模态块列表拍平，非文本块按固定开销估。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                if block.get("type") == "text":
                    parts.append(str(block.get("text") or ""))
                else:
                    # 图片/音频等非文本块：无法按字符估，按一张图的通用开销估
                    parts.append("x" * 1000)
            else:
                parts.append(str(block))
        return "\n".join(parts)
    return str(content) if content is not None else ""


def estimate_messages_tokens(messages: Iterable[Any]) -> int:
    """消息列表 token 估算（含角色开销与工具 schema 之外的固定项）。"""
    total = 0
    for msg in messages:
        content = getattr(msg, "content", msg)
        total += estimate_text_tokens(_content_text(content)) + _PER_MESSAGE_OVERHEAD
        # 上一轮遗留的 tool_calls 也会回灌进 prompt
        for call in getattr(msg, "tool_calls", None) or []:
            total += estimate_text_tokens(str(call)) + _PER_MESSAGE_OVERHEAD
    return total
