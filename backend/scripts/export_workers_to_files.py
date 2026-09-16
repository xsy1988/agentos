"""一次性迁移：把 DB 里的 task_types 模板导出为 data/workers/ 文件包。

必须在 Alembic 迁移 g8d9e0f1a2b3（模板三表退役）**之前**运行：
- 通用任务（kind='common'）不导出——运行时由内建常量 COMMON_WORKER 兜底；
- sub_workers 由 task_type_steps 生成（文件夹名 = 步骤名）；
- capabilities 名单由 task_type_capabilities 反查 capabilities.name 写入 yaml 头；
- 导出统一为 v1；幂等（文件包已存在则跳过，保护用户编辑）。

用法（backend/ 下）：python -m scripts.export_workers_to_files
"""

import asyncio
import logging
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import settings
from app.modules.workers import registry
from app.modules.workers.registry import WorkerError

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("export_workers")

# 通用任务的两种历史形态：kind='common' 或名为「通用任务」
COMMON_NAMES = {"通用任务"}


async def fetch_templates() -> list[dict[str, Any]]:
    """读三张定义表 + capabilities，聚合成 Worker 文件包描述。"""
    engine = create_async_engine(settings.database_url)
    try:
        async with engine.begin() as conn:
            types = (
                (
                    await conn.execute(
                        text(
                            "SELECT id, name, description, playbook, references, kind, icon, color, enabled "
                            "FROM task_types ORDER BY sort_order, name"
                        )
                    )
                )
                .mappings()
                .all()
            )
            steps = (
                (
                    await conn.execute(
                        text(
                            "SELECT task_type_id, seq, name, description, playbook, references, kind, optional, "
                            "capability_hint FROM task_type_steps ORDER BY task_type_id, seq"
                        )
                    )
                )
                .mappings()
                .all()
            )
            caps = (
                (
                    await conn.execute(
                        text(
                            "SELECT tc.task_type_id, c.name FROM task_type_capabilities tc "
                            "JOIN capabilities c ON c.id = tc.capability_id "
                            "ORDER BY tc.task_type_id, c.name"
                        )
                    )
                )
                .mappings()
                .all()
            )
    finally:
        await engine.dispose()

    steps_by_type: dict[str, list[dict[str, Any]]] = {}
    for s in steps:
        steps_by_type.setdefault(str(s["task_type_id"]), []).append(dict(s))
    caps_by_type: dict[str, list[str]] = {}
    for c in caps:
        caps_by_type.setdefault(str(c["task_type_id"]), []).append(c["name"])

    out: list[dict[str, Any]] = []
    for t in types:
        if t["kind"] == "common" or t["name"] in COMMON_NAMES:
            logger.info("跳过通用任务模板（由 COMMON_WORKER 内建兜底）")
            continue
        out.append(
            {
                "name": t["name"],
                "description": t["description"] or "",
                "playbook": t["playbook"] or "",
                "icon": t["icon"],
                "color": t["color"],
                "enabled": bool(t["enabled"]),
                "capabilities": caps_by_type.get(str(t["id"]), []),
                "references": list(t["references"]) if t["references"] else None,
                "sub_workers": steps_by_type.get(str(t["id"]), []),
            }
        )
    return out


def sanitize_step_name(name: str) -> str:
    """步骤名 → 子任务文件夹名（去掉路径分隔符，避免破坏文件布局）。"""
    cleaned = name.replace("/", "／").replace("\\", "＼").strip()
    return cleaned or "未命名步骤"


def export_one(t: dict[str, Any]) -> None:
    sub_workers = [
        {
            "name": sanitize_step_name(s["name"]),
            "seq": s["seq"] or 0,
            "kind": s["kind"] if s["kind"] in ("main", "branch") else "main",
            "optional": bool(s["optional"]),
            "description": s["description"] or "",
            "playbook": s["playbook"] or "",
            "capability_hint": list(s["capability_hint"]) if s["capability_hint"] else [],
        }
        for s in t["sub_workers"]
    ]
    meta = registry.ensure_worker(
        t["name"],
        description=t["description"],
        icon=t["icon"],
        color=t["color"],
        capabilities=t["capabilities"],
        references=t["references"],
        playbook=t["playbook"],
        sub_workers=sub_workers,
    )
    # ensure_worker 幂等：已存在不覆盖。manifest 的 enabled/引用文件可能需要补齐
    if not t["enabled"] and meta.enabled:
        registry.set_enabled(t["name"], False)
        logger.info("Worker「%s」按模板状态停用", t["name"])

    # 主任务 references（DB JSON 数组 → v1/references/*.md），文件包已存在时跳过
    if meta.versions == ["v1"] and t["references"]:
        for ref in t["references"]:
            if not isinstance(ref, dict):
                continue
            path = ref.get("path") or ref.get("name")
            content = ref.get("content")
            if not path or not isinstance(content, str):
                continue
            rel = f"references/{path}" if not path.startswith("references/") else path
            if not rel.endswith(".md"):
                rel += ".md"
            target = registry.version_dir(t["name"], "v1") / rel
            if target.exists():
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            logger.info("写入引用文件：%s/%s", t["name"], rel)
        registry.invalidate(t["name"])


async def main() -> None:
    templates = await fetch_templates()
    if not templates:
        logger.info("DB 中没有可导出的业务 Worker 模板（三表已退役或为空）")
        return
    for t in templates:
        try:
            export_one(t)
            logger.info(
                "导出 ✓ %s（sub_workers=%d, capabilities=%d）",
                t["name"],
                len(t["sub_workers"]),
                len(t["capabilities"]),
            )
        except WorkerError as e:
            logger.error("导出失败 %s：%s", t["name"], e)
    # 导出后自检：全部模板可被 registry 解析
    names = {m.name for m in registry.list_workers()}
    missing = [t["name"] for t in templates if t["name"] not in names]
    if missing:
        raise SystemExit(f"以下 Worker 导出后无法解析：{missing}")
    logger.info("导出完成，共 %d 个 Worker 文件包位于 %s", len(templates), registry.workers_root())


if __name__ == "__main__":
    asyncio.run(main())
