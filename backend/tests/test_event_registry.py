"""事件类型注册表一致性（P0-6）：发射点 ⊆ `EVENT_TYPES`。

背景：`context_compacted` 曾在产线发射却未登记进 `EVENT_TYPES`，而冻结测试硬编码
10 类字符串，谁也发现不了。本测试用 AST 扫描全部发射点，把"漏登记"变成红灯。

规则（要绕过，先想清楚是否真的需要）：
1. 事件类型必须是**字符串字面量**；用变量/拼接发射的事件名不受 `EVENT_TYPES` 约束，
   故这类发射点必须显式登记到 `_DYNAMIC_EMIT_SITES` 并写明理由。
2. `EVENT_TYPES` 登记即须有发射点——不得保留"声明了但没实现"的条目。
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from app.modules.engine.backend import EVENT_TYPES
from app.modules.engine.models import INBOX_EVENT_TYPES

APP_DIR = Path(__file__).resolve().parent.parent / "app"
FRONTEND_SRC = APP_DIR.parent.parent / "frontend" / "src"

# 事件发射入口：emit_event(run_id, type, payload) / _emit(run_id, type, payload) /
# assembler 的 emit(type, payload)（无 run_id 参数，故类型实参位置为 0）
_EMIT_CALLS = {"emit_event", "_emit", "emit"}

# 动态事件名发射点（AST 取不到字面量）。键 = "相对路径:限定函数名"，值 = 理由。
_DYNAMIC_EMIT_SITES = {
    "modules/engine/graph.py:build_graph.agent": (
        "把 `lambda et, p: runtime.emit_event(run_id, et, p)` 交给 assembler.compact_messages，"
        "实际事件名在 assembler.py 内以字面量发射（已被本测试扫描到）"
    ),
}


def _resolve_type(call: ast.Call) -> tuple[str, int]:
    for idx in (0, 1):
        if len(call.args) > idx:
            arg = call.args[idx]
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                return arg.value, arg.lineno
    for kw in call.keywords:
        if kw.arg != "event_type" or not isinstance(kw.value, ast.Constant):
            continue
        if isinstance(kw.value.value, str):
            return kw.value.value, kw.value.lineno
    return "<dynamic>", call.lineno


def _collect(node: ast.AST, rel: str, scope: str, hits: list[tuple[str, str, int, str]]) -> None:
    """收集本层（不含嵌套函数/类，它们由 _walk 以更精确的 scope 处理）的发射点。"""
    stack: list[ast.AST] = [node]
    while stack:
        cur = stack.pop()
        nested = isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        if nested and cur is not node:
            continue
        if isinstance(cur, ast.Call):
            func = cur.func
            name = (
                func.attr
                if isinstance(func, ast.Attribute)
                else func.id
                if isinstance(func, ast.Name)
                else ""
            )
            if name in _EMIT_CALLS:
                etype, lineno = _resolve_type(cur)
                hits.append((rel, etype, lineno, scope))
        stack.extend(ast.iter_child_nodes(cur))


def _walk(
    body: list[ast.stmt], rel: str, scope: str, hits: list[tuple[str, str, int, str]]
) -> None:
    for node in body:
        if isinstance(node, ast.ClassDef):
            _walk(node.body, rel, f"{scope}.{node.name}" if scope else node.name, hits)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            here = f"{scope}.{node.name}" if scope else node.name
            _collect(node, rel, here, hits)
            _walk(node.body, rel, here, hits)
        else:
            _collect(node, rel, scope, hits)


def _scan() -> list[tuple[str, str, int, str]]:
    hits: list[tuple[str, str, int, str]] = []
    for path in sorted(APP_DIR.rglob("*.py")):
        rel = path.relative_to(APP_DIR).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        _walk(tree.body, rel, "", hits)
    return hits


def test_emitted_event_types_are_registered() -> None:
    unregistered = [
        f"{rel}:{lineno} 发射 {etype!r}"
        for rel, etype, lineno, _ in _scan()
        if etype != "<dynamic>" and etype not in EVENT_TYPES
    ]
    assert unregistered == [], (
        "以下事件类型未登记进 backend.EVENT_TYPES，请先登记再发射：\n  " + "\n  ".join(unregistered)
    )


def test_declared_event_types_are_emitted() -> None:
    emitted = {etype for _, etype, _, _ in _scan() if etype != "<dynamic>"}
    never = sorted(t for t in EVENT_TYPES if t not in emitted)
    assert never == [], "EVENT_TYPES 中声明了却无发射点（不得保留'声明了但没实现'的条目）：" + repr(
        never
    )


def test_dynamic_emit_sites_are_known() -> None:
    """动态事件名发射点必须显式登记——否则注册表约束会被静默绕过。"""
    dynamic = {f"{rel}:{scope}" for rel, etype, _, scope in _scan() if etype == "<dynamic>"}
    assert dynamic, "未发现任何动态发射点，请清理 _DYNAMIC_EMIT_SITES 白名单"
    unknown = sorted(dynamic - set(_DYNAMIC_EMIT_SITES))
    assert unknown == [], f"出现未登记的动态事件名发射点：{unknown}"


def test_inbox_event_types_are_all_handled() -> None:
    """INBOX_EVENT_TYPES 每项都有 `_handle` 分支。"""
    tree = ast.parse((APP_DIR / "modules/engine/runtime.py").read_text(encoding="utf-8"))
    handled: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) or node.name != "_handle":
            continue
        for sub in ast.walk(node):
            if isinstance(sub, ast.Compare) and isinstance(sub.left, ast.Name):
                for cmp in sub.comparators:
                    if isinstance(cmp, ast.Constant) and isinstance(cmp.value, str):
                        handled.add(cmp.value)
    missing = sorted(set(INBOX_EVENT_TYPES) - handled)
    assert missing == [], f"INBOX_EVENT_TYPES 中声明但 _handle 未实现：{missing}"


# ---------------------------------------------------------------- 前端一致性

_FRONTEND_REGISTRY = FRONTEND_SRC / "api" / "eventTypes.ts"
# `event_type === "thought"` / `event_type !== "error"` 形式的消费分支
_BRANCH_RE = re.compile(r"""event_type\s*[!=]==?\s*["']([a-z_]+)["']""")
# EVENT_META 形状的条目：`thought: { icon: <BulbOutlined />, color: "gold" },`
_ICON_MAP_RE = re.compile(r"""^\s{2}([a-z_]+):\s*\{\s*icon:.*color:.*\},\s*$""", re.MULTILINE)
# eventTypes.ts 的 EVENT_TYPES 数组字面量
_REGISTRY_RE = re.compile(r"""export const EVENT_TYPES\s*=\s*\[(.*?)\]\s*as const;""", re.DOTALL)


def _frontend_files() -> list[Path]:
    return sorted(p for p in FRONTEND_SRC.rglob("*") if p.suffix in {".ts", ".tsx"})


def test_frontend_registry_matches_backend() -> None:
    """前端单点常量必须与后端 EVENT_TYPES 一致（否则前端会漏渲染/多渲染类型）。"""
    text = _FRONTEND_REGISTRY.read_text(encoding="utf-8")
    match = _REGISTRY_RE.search(text)
    assert match, "frontend/src/api/eventTypes.ts 里找不到 EVENT_TYPES 数组字面量"
    frontend_types = set(re.findall(r"""["']([a-z_]+)["']""", match.group(1)))
    assert frontend_types == set(EVENT_TYPES), (
        f"前端多出 {sorted(frontend_types - set(EVENT_TYPES))}，"
        f"缺少 {sorted(set(EVENT_TYPES) - frontend_types)}"
    )


def test_frontend_has_no_unregistered_event_branches() -> None:
    """前端不得保留未登记事件类型的消费分支与图标条目（死分支会误导维护者）。"""
    dead: list[str] = []
    for path in _frontend_files():
        text = path.read_text(encoding="utf-8")
        rel = path.relative_to(FRONTEND_SRC).as_posix()
        for lineno, line in enumerate(text.splitlines(), start=1):
            for name in _BRANCH_RE.findall(line):
                if name not in EVENT_TYPES:
                    dead.append(f"{rel}:{lineno} 分支 {name!r}")
            for key in _ICON_MAP_RE.findall(line):
                if key not in EVENT_TYPES:
                    dead.append(f"{rel}:{lineno} 图标条目 {key!r}")
    assert dead == [], "前端存在未登记的事件类型分支/条目：\n  " + "\n  ".join(dead)
