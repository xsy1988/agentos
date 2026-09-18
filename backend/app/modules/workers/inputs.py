"""Worker 输入契约（P1-4）：必需输入的声明、校验与预检。

WORKER.md（主任务与 sub_workers/<子任务>）front-matter::

    inputs:
      - name: quote_file
        type: file          # text | number | date | file | url | json
        required: true
        description: 待比价的报价单（xlsx/pdf）
        example: 报价单-2026Q1.xlsx

纪律（这决定它能不能"不靠模型自觉"）：

- 只允许声明**平台能确定性判定**的输入：取值来自 `run.input.inputs` 快照、
  同会话历史 run 的快照、或会话附件（`file` 类型）——不做文本抽取、不让模型填。
- 缺必填 → 调用模型**之前**拦截（`missing_inputs` 结构化错误，含缺失字段名）；
  不缺失 → 契约与取值随固定区注入（`protected_context.system_prompt`）。
- front-matter 出现未知键直接报错（写入即 4xx）：拼错字段被静默忽略比报错更贵。
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

# 输入类型：与"可确定性提供"的取值方式一一对应（file 由会话附件顶替）
INPUT_TYPES = ("text", "number", "date", "file", "url", "json")

# 输入名是机器键（run.input.inputs 的 key），限 ASCII snake_case
INPUT_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")

MAX_INPUTS = 16
MAX_DESCRIPTION = 500
MAX_EXAMPLE = 200
MAX_VALUE_CHARS = 2000

# front-matter 单条输入允许的键（其余一律报错）
_INPUT_KEYS = ("name", "type", "required", "description", "example")


class InputContractError(ValueError):
    """输入契约声明/取值非法（registry 写入路径转 `WorkerError` → 4xx）。"""


@dataclass(frozen=True)
class InputSpec:
    """一条输入声明（Worker 或子任务 front-matter 的 `inputs` 项）。"""

    name: str
    type: str
    required: bool = False
    description: str = ""
    example: str = ""

    def to_dict(self) -> dict[str, Any]:
        """API/文档用字典（键序稳定，便于审计）。"""
        return {
            "name": self.name,
            "type": self.type,
            "required": self.required,
            "description": self.description,
            "example": self.example,
        }

    @property
    def label(self) -> str:
        """人话描述：`quote_file（file，必填）`。"""
        return f"{self.name}（{self.type}，{'必填' if self.required else '可选'}）"


def _require_text(value: Any, *, field: str, scope: str, limit: int) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise InputContractError(f"{scope} 的 {field} 必须是字符串")
    text = value.strip()
    if len(text) > limit:
        raise InputContractError(f"{scope} 的 {field} 超过 {limit} 字符")
    return text


def parse_input_specs(raw: Any, *, scope: str) -> list[InputSpec]:
    """解析并严格校验 `inputs` 声明；非法即抛 `InputContractError`。"""
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise InputContractError(f"{scope} 的 inputs 必须是列表（每项一个输入声明）")
    if len(raw) > MAX_INPUTS:
        raise InputContractError(f"{scope} 的 inputs 最多 {MAX_INPUTS} 条，得到 {len(raw)} 条")

    specs: list[InputSpec] = []
    seen: set[str] = set()
    for index, item in enumerate(raw, start=1):
        where = f"{scope} 的第 {index} 条输入"
        if not isinstance(item, dict):
            raise InputContractError(
                f"{where} 必须是键值结构（name/type/required/description/example）"
            )
        unknown = sorted(str(k) for k in item if k not in _INPUT_KEYS)
        if unknown:
            raise InputContractError(
                f"{where} 含未知字段 {'、'.join(unknown)}（允许：{'/'.join(_INPUT_KEYS)}）"
            )
        name = _require_text(item.get("name"), field="name", scope=where, limit=40)
        if not INPUT_NAME_RE.match(name):
            raise InputContractError(
                f"{where} 的 name「{name}」非法：限小写字母开头的 ASCII 标识（a-z0-9_，≤40）"
            )
        if name in seen:
            raise InputContractError(f"{where} 的 name「{name}」重复")
        seen.add(name)
        itype = _require_text(item.get("type"), field="type", scope=where, limit=16) or "text"
        if itype not in INPUT_TYPES:
            raise InputContractError(
                f"{where} 的 type「{itype}」不支持（允许：{'/'.join(INPUT_TYPES)}）"
            )
        required = item.get("required", False)
        if not isinstance(required, bool):
            raise InputContractError(f"{where} 的 required 必须是布尔值（true/false）")
        specs.append(
            InputSpec(
                name=name,
                type=itype,
                required=required,
                description=_require_text(
                    item.get("description"), field="description", scope=where, limit=MAX_DESCRIPTION
                ),
                example=_require_text(
                    item.get("example"), field="example", scope=where, limit=MAX_EXAMPLE
                ),
            )
        )
    return specs


def dump_input_specs(specs: Sequence[InputSpec] | Sequence[Mapping[str, Any]] | None) -> list[dict]:
    """把声明写成 front-matter 结构（写盘 round-trip；只落非空字段）。"""
    out: list[dict] = []
    for spec in specs or []:
        item = spec.to_dict() if isinstance(spec, InputSpec) else dict(spec)
        row: dict[str, Any] = {"name": item.get("name"), "type": item.get("type") or "text"}
        if item.get("required"):
            row["required"] = True
        if item.get("description"):
            row["description"] = item["description"]
        if item.get("example"):
            row["example"] = item["example"]
        out.append(row)
    return out


def sanitize_provided_inputs(raw: Any) -> dict[str, str]:
    """校验并归一化调用方提供的输入（API 层入口）；非法即抛 `InputContractError`。"""
    if raw is None or raw == {}:
        return {}
    if not isinstance(raw, Mapping):
        raise InputContractError('inputs 必须是对象（形如 {"quote_file": "..."}）')
    if len(raw) > MAX_INPUTS:
        raise InputContractError(f"inputs 最多 {MAX_INPUTS} 项，得到 {len(raw)} 项")
    out: dict[str, str] = {}
    for key, value in raw.items():
        name = str(key)
        if not INPUT_NAME_RE.match(name):
            raise InputContractError(
                f"输入名「{name}」非法：限小写字母开头的 ASCII 标识（a-z0-9_，≤40）"
            )
        if isinstance(value, bool | int | float):
            text = json.dumps(value, ensure_ascii=False)
        elif isinstance(value, str):
            text = value.strip()
        elif value is None:
            text = ""
        else:
            raise InputContractError(f"输入「{name}」只接受文本/数字/布尔值")
        if len(text) > MAX_VALUE_CHARS:
            raise InputContractError(f"输入「{name}」超过 {MAX_VALUE_CHARS} 字符")
        if text:
            out[name] = text
    return out


def resolve_inputs(
    specs: Sequence[InputSpec],
    provided: Mapping[str, str] | None,
    attachment_names: Sequence[str] = (),
) -> tuple[dict[str, str], list[InputSpec]]:
    """按声明判定取值与缺失：返回 (已提供取值, 缺失的必填项)。

    `file` 类型可由会话附件顶替（附件是平台的确定性事实，不依赖模型转述）；附件数量够时
    按声明顺序一对一顶替，不够时**剩余 file 输入共享第一个附件**（用户只附了一件材料时，
    按「一个附件只顶一个输入」会让其余必填项假缺失）。未声明的额外键忽略：契约外的输入
    不参与判定，也不注入上下文。
    """
    provided = provided or {}
    values: dict[str, str] = {}
    for spec in specs:
        raw = provided.get(spec.name)
        if raw is None:
            continue
        text = str(raw).strip()
        if text:
            values[spec.name] = text

    pending = [str(a) for a in attachment_names if str(a).strip()]
    shared = pending[0] if pending else None
    for spec in specs:
        if spec.type != "file" or spec.name in values:
            continue
        if pending:
            values[spec.name] = pending.pop(0)
        elif shared is not None:
            values[spec.name] = shared

    missing = [s for s in specs if s.required and s.name not in values]
    return values, missing


def _shared_file_notes(specs: Sequence[InputSpec], values: Mapping[str, str]) -> dict[str, str]:
    """同一附件顶替了多个 `file` 输入时，给出「与谁共用」的说明。"""
    owners: dict[str, list[str]] = {}
    for spec in specs:
        if spec.type == "file" and spec.name in values:
            owners.setdefault(values[spec.name], []).append(spec.name)
    notes: dict[str, str] = {}
    for names in owners.values():
        if len(names) < 2:
            continue
        for name in names:
            others = [n for n in names if n != name]
            notes[name] = f"（与 {'、'.join(others)} 共用同一附件）"
    return notes


def render_input_contract(specs: Sequence[InputSpec], values: Mapping[str, str] | None) -> str:
    """渲染注入固定区的输入契约文本；无声明返回空串。"""
    if not specs:
        return ""
    values = values or {}
    notes = _shared_file_notes(specs, values)
    lines = [
        "# 输入契约（平台已按 Worker 声明完成预检）",
        "带「必填」的输入项缺失时平台会在调用模型前拦截；未提供的可选输入不要臆造，"
        "确需取值请用 ask_user 向用户澄清。",
    ]
    for spec in specs:
        row = f"- {spec.label}"
        if spec.description:
            row += f"：{spec.description}"
        if spec.example:
            row += f"（示例：{spec.example}）"
        lines.append(row)
        if spec.name in values:
            lines.append(f"  已提供：{values[spec.name]}{notes.get(spec.name, '')}")
        else:
            lines.append("  未提供" if spec.required else "  未提供（可选）")
    return "\n".join(lines)


def missing_inputs_text(missing: Sequence[InputSpec]) -> str:
    """缺失必填项 → 给用户看的清单（含字段名与用途）。"""
    lines = ["本次执行缺少必需输入，已在调用模型前拦截（未消耗模型调用）："]
    for spec in missing:
        row = f"- {spec.label}"
        if spec.description:
            row += f"：{spec.description}"
        if spec.type == "file":
            row += "（请随消息附带该文件）"
        lines.append(row)
    lines.append("补充后请重新发起；如需由外部系统传值，用 run 输入参数 inputs 提供。")
    return "\n".join(lines)
