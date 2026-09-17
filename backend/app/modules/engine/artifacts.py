"""结果产物（artifact）契约与"先外置再进上下文"纪律（方案 §4 P0-5）。

动机：结果为单字段文本时，清单/报告类长结果只能整段塞进上下文，随后被 L1 压缩折成
`str(content)[:120]` —— 折叠即失真，且判据（上下文用量）与真相（DB 里的完整结果）脱钩。

契约：
- 超阈值的内容**先落 `run_artifacts` 再进上下文/终答**；上下文只留「引用行 + 预览」。
- 引用行格式固定，L1 折叠必须原样保留（`fold_brief`），因此"压缩"不再损坏可追溯性：

      [artifact id=<uuid> kind=text name="采购报价对比" size=12345]

本模块为纯函数：不碰 DB、不碰 I/O；落库由 `EngineBackend.save_artifact` 完成，
便于单测直接覆盖引用行解析与阈值判定。
"""

import json
import re
from collections.abc import Mapping
from typing import Any

# 产物种类：text/json 内联；file 指向既有 files 表；link 存 URL
KINDS: tuple[str, ...] = ("text", "json", "file", "link")

# 引用行：`[artifact id=... kind=... name="..." size=...]`（name 可省，宽容解析）
_REF_RE = re.compile(r"\[artifact\s+([^\]]*?)\]")
_FIELD_RE = re.compile(r'(\w+)=("[^"]*"|\S+)')


def detect_kind(text: str) -> str:
    """JSON 对象/数组 → `json`（前端按结构化渲染），其余 → `text`。"""
    stripped = text.strip()
    if stripped[:1] in ("{", "["):
        try:
            json.loads(stripped)
        except ValueError:
            return "text"
        return "json"
    return "text"


def mime_for_kind(kind: str) -> str:
    return "application/json" if kind == "json" else "text/plain"


def ref_line(artifact: Mapping[str, Any]) -> str:
    """产物引用行（进上下文的唯一形态）。"""
    name = str(artifact.get("name") or "").replace('"', "'")[:120]
    return (
        f"[artifact id={artifact.get('id')} kind={artifact.get('kind') or 'text'}"
        f' name="{name}" size={int(artifact.get("size") or 0)}]'
    )


def parse_artifact_refs(text: str) -> list[dict[str, Any]]:
    """从文本里取出全部产物引用（L1 折叠保真与前端定位都用它）。"""
    refs: list[dict[str, Any]] = []
    for match in _REF_RE.finditer(text or ""):
        fields: dict[str, Any] = {}
        for key, raw in _FIELD_RE.findall(match.group(1)):
            fields[key] = raw[1:-1] if raw.startswith('"') and raw.endswith('"') else raw
        if fields.get("id"):
            refs.append(fields)
    return refs


def should_externalize(text: str, *, limit: int) -> bool:
    return limit > 0 and len(text) > limit


def preview(text: str, *, chars: int) -> str:
    """预览体：截断处显式标注，读者不会把预览误当完整结果。"""
    if chars <= 0 or len(text) <= chars:
        return text
    return f"{text[:chars]}…（共 {len(text)} 字符，完整内容见上方产物引用）"


def externalize(
    text: str,
    *,
    name: str | None = None,
    limit: int,
    preview_chars: int,
    kind: str | None = None,
) -> tuple[str, dict[str, Any]] | None:
    """超阈值内容 → `(预览体, 待落库的产物 spec)`；未超阈值 → `None`（调用方原样使用）。

    预览体不含引用行：落库拿到 id 后由调用方 `with_ref(ref_line(row), body)` 拼装，
    避免"先写引用再改 id"的二次改写。
    """
    if not should_externalize(text, limit=limit):
        return None
    resolved = kind or detect_kind(text)
    payload: Any = json.loads(text) if resolved == "json" else {"text": text}
    spec: dict[str, Any] = {
        "kind": resolved,
        "name": name,
        "mime": mime_for_kind(resolved),
        "size": len(text),
        "storage": "inline",
        "payload": payload,
    }
    return preview(text, chars=preview_chars), spec


def with_ref(ref: str, body: str) -> str:
    """引用行 + 预览体（进上下文/终答的标准拼装）。"""
    return f"{ref}\n{body}" if ref else body


def fold_brief(content: str, *, keep_chars: int = 120) -> str:
    """L1 折叠文本：**引用行原样保留**，其余只留开头 `keep_chars` 字符。

    Q-05 核心回归点：折叠后仍能凭引用行找回完整产物。
    """
    text = str(content or "")
    refs = [m.group(0) for m in _REF_RE.finditer(text)]
    body = _REF_RE.sub("", text).strip()
    brief = body[:keep_chars] if keep_chars > 0 else ""
    if body and len(body) > len(brief):
        brief = f"{brief}…"
    return " ".join([*refs, brief]).strip()
