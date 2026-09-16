"""Worker 文件化存储：WORKER.md 是任务模板的权威编辑载体（摒弃纯数据库管理）。

文件布局（`data/workers/<task_type_id>/`，目录名用 uuid 保证改名不换路径）：
- ``WORKER.md``            主任务 Worker：yaml frontmatter（L1 元信息 + L3 references）
                           + 正文（L2 playbook，讲清干什么/怎么干/遇何问题/能力路由）
- ``steps/<NN>-<slug>.md`` 子任务 Worker：同构 frontmatter（seq/kind/optional/
                           capability_hint）+ 正文 playbook

权威与同步（双向）：
- 模板 CRUD（表单/API）后自动投影 DB → 文件（serialize_worker）；
- PUT worker-file 解析文件 → 回写 DB（import_worker_content / import_step_content）。
运行时引擎仍读 task_types 表（装配链路零改动），文件侧编辑保存即同步。

解析纪律：frontmatter 之后的全部正文都是 playbook；references 列表用 yaml
原生结构（对齐设计文档 §2 的 references 条目建议结构）。
"""

from __future__ import annotations

import hashlib
import logging
import re
import shutil
from pathlib import Path
from typing import Any
from uuid import UUID

import yaml
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.modules.tasks.models import TaskType, TaskTypeStep

logger = logging.getLogger(__name__)

FRONT_DELIM = "---"
# frontmatter 顶部 yaml 字段顺序（稳定输出，便于 diff 与 review）
_MAIN_KEYS = (
    "name",
    "description",
    "icon",
    "color",
    "kind",
    "enabled",
    "sort_order",
    "default_agent_id",
    "references",
)
_STEP_KEYS = (
    "name",
    "seq",
    "kind",
    "optional",
    "description",
    "capability_hint",
    "references",
)


def workers_root() -> Path:
    root = settings.data_dir / "workers"
    root.mkdir(parents=True, exist_ok=True)
    return root


def worker_dir(task_type_id: UUID) -> Path:
    return workers_root() / str(task_type_id)


def _slug(name: str) -> str:
    """文件名友好的 slug：ASCII 走 kebab-case；中文名退化为短 hash 前缀（稳定）。"""
    ascii_part = re.sub(r"[^0-9a-zA-Z]+", "-", name).strip("-").lower()
    if ascii_part:
        return ascii_part[:48]
    return f"step-{hashlib.md5(name.encode()).hexdigest()[:8]}"


def _dump(front: dict[str, Any]) -> str:
    body = yaml.safe_dump(front, allow_unicode=True, sort_keys=False, default_flow_style=False)
    return f"{FRONT_DELIM}\n{body}{FRONT_DELIM}\n"


def _split_md(text: str) -> tuple[dict[str, Any], str]:
    """解析 WORKER.md：返回 (frontmatter, playbook 正文)。无 frontmatter 视为纯 playbook。"""
    if not text.startswith(FRONT_DELIM):
        return {}, text
    parts = text.split(FRONT_DELIM, 2)
    if len(parts) < 3:
        return {}, text
    front = yaml.safe_load(parts[1]) or {}
    if not isinstance(front, dict):
        raise ValueError("frontmatter 必须是 yaml 键值结构")
    return front, parts[2].strip("\n") + "\n"


def _pick(front: dict[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
    return {k: front[k] for k in keys if front.get(k) is not None}


# ---------- DB → 文件（投影） ----------


async def serialize_worker(db: AsyncSession, tpl: TaskType) -> Path:
    """把主任务 Worker（含子任务）投影成 WORKER.md 文件树。幂等：整目录重写。"""
    steps = list(
        (
            await db.scalars(
                select(TaskTypeStep)
                .where(TaskTypeStep.task_type_id == tpl.id)
                .order_by(TaskTypeStep.seq)
            )
        ).all()
    )

    directory = worker_dir(tpl.id)
    steps_dir = directory / "steps"
    if steps_dir.exists():
        shutil.rmtree(steps_dir)  # 整表替换：删除已移除的步骤文件
    steps_dir.mkdir(parents=True, exist_ok=True)

    front = _pick(
        {
            "name": tpl.name,
            "description": tpl.description,
            "icon": tpl.icon,
            "color": tpl.color,
            "kind": tpl.kind,
            "enabled": tpl.enabled,
            "sort_order": tpl.sort_order,
            "default_agent_id": str(tpl.default_agent_id) if tpl.default_agent_id else None,
            "references": tpl.references,
        },
        _MAIN_KEYS,
    )
    (directory / "WORKER.md").write_text(
        _dump(front) + "\n" + (tpl.playbook or ""), encoding="utf-8"
    )

    for s in steps:
        s_front = _pick(
            {
                "name": s.name,
                "seq": s.seq,
                "kind": s.kind,
                "optional": s.optional,
                "description": s.description,
                "capability_hint": s.capability_hint,
                "references": s.references,
            },
            _STEP_KEYS,
        )
        path = steps_dir / f"{s.seq:02d}-{_slug(s.name)}.md"
        path.write_text(_dump(s_front) + "\n" + (s.playbook or ""), encoding="utf-8")

    logger.info("Worker 文件已投影：%s（%d 个子任务）", directory, len(steps))
    return directory


def remove_worker_files(task_type_id: UUID) -> None:
    directory = worker_dir(task_type_id)
    if directory.exists():
        shutil.rmtree(directory)


async def serialize_worker_safely(db: AsyncSession, tpl: TaskType) -> None:
    """CRUD 后的自动投影：文件写失败不阻断主流程（DB 已提交），记 warning。
    必须在请求内 await（db session 随请求关闭，不能挂到后台任务）。"""
    try:
        await serialize_worker(db, tpl)
    except Exception:  # noqa: BLE001 —— 投影失败不影响主流程
        logger.warning("Worker 文件投影失败 task_type=%s", tpl.id, exc_info=True)


# ---------- 文件 → DB（导入） ----------


def read_worker_file(tpl: TaskType) -> str:
    path = worker_dir(tpl.id) / "WORKER.md"
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


def read_step_file(step: TaskTypeStep) -> str:
    path = step_file_path(step)
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


def step_file_path(step: TaskTypeStep) -> Path:
    return worker_dir(step.task_type_id) / "steps" / f"{step.seq:02d}-{_slug(step.name)}.md"


def import_worker_content(tpl: TaskType, content: str) -> None:
    """解析 WORKER.md 内容并回写主任务模板（name/kind 由 DB 权威，文件里仅作记录）。"""
    front, playbook = _split_md(content)
    if front.get("name") and str(front["name"]).strip() != tpl.name:
        raise ValueError(
            f"文件 name「{front['name']}」与模板「{tpl.name}」不一致（重命名请走编辑表单）"
        )
    tpl.description = str(front.get("description") or "")
    tpl.playbook = playbook
    tpl.icon = str(front["icon"]) if front.get("icon") is not None else None
    tpl.color = str(front["color"]) if front.get("color") is not None else None
    if "enabled" in front and tpl.kind != "common":
        tpl.enabled = bool(front["enabled"])
    if front.get("sort_order") is not None:
        tpl.sort_order = int(front["sort_order"])
    if "references" in front:
        refs = front["references"]
        tpl.references = refs if isinstance(refs, list) else None


def import_step_content(step: TaskTypeStep, content: str) -> None:
    """解析子任务 WORKER.md 内容并回写步骤模板（seq 不允许文件改，防乱序）。"""
    front, playbook = _split_md(content)
    if front.get("name") and str(front["name"]).strip() != step.name:
        raise ValueError(f"文件 name「{front['name']}」与子任务「{step.name}」不一致")
    step.description = str(front.get("description") or "")
    step.playbook = playbook
    if front.get("kind") in ("main", "branch"):
        step.kind = str(front["kind"])
    if "optional" in front:
        step.optional = bool(front["optional"])
    if "capability_hint" in front:
        hint = front["capability_hint"]
        step.capability_hint = [str(x) for x in hint] if isinstance(hint, list) else None
    if "references" in front:
        refs = front["references"]
        step.references = refs if isinstance(refs, list) else None
