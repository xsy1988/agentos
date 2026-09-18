"""open_api 服务：能力批量注册 + Worker 文件包落盘编排。

编排顺序（与规范文档一致）：
1. 逐个注册能力（幂等：同名已存在跳过；冒烟失败保留 disabled 并如实上报）
2. 引用清单校验：worker.capabilities 必须全部命中（平台已有 ∪ 本次提交），否则 422
3. Worker 文件包落盘：不存在 → ensure_worker（v1）；已存在按 if_exists 决策
"""

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.capabilities import service as cap_service
from app.modules.capabilities.capacity import target_agent_warnings
from app.modules.capabilities.models import Capability
from app.modules.capabilities.schemas import CapabilityCreateIn
from app.modules.engine.tools_builtin import BUILTIN_TOOLS
from app.modules.open_api.schemas import (
    OpenCapabilityResultOut,
    OpenSubWorkerIn,
    OpenWorkerRegisterIn,
    OpenWorkerRegisterOut,
)
from app.modules.workers import registry
from app.modules.workers.registry import WorkerError


def _guard_third_party_tool(cap: CapabilityCreateIn) -> None:
    """第三方 tool 守卫：type=tool 的执行依赖平台进程内 BUILTIN_TOOLS 注册键。

    只允许声明 payload.builtin 且必须命中已注册的内置工具（引用平台能力）；
    纯 schema 声明的新 tool 在引擎侧没有执行通道（会落到「工具执行通道缺失」），
    可执行的新能力请走 mcp（或 plugin + transport）。
    """
    if cap.type != "tool":
        return
    builtin_key = (cap.payload or {}).get("builtin")
    if builtin_key in BUILTIN_TOOLS:
        return
    raise HTTPException(
        status.HTTP_422_UNPROCESSABLE_CONTENT,
        f"能力「{cap.name}」：第三方注册 type=tool 必须携带 payload.builtin 且为平台已注册的"
        f"内置工具键（现有：{', '.join(sorted(BUILTIN_TOOLS))}）。"
        "新的可执行能力请注册 mcp（外部工具服务）或 plugin（前端 + 可选 transport）。",
    )


async def _register_capabilities(
    db: AsyncSession, caps: list[CapabilityCreateIn]
) -> list[OpenCapabilityResultOut]:
    results: list[OpenCapabilityResultOut] = []
    for cap_in in caps:
        _guard_third_party_tool(cap_in)
        existing = await db.scalar(select(Capability).where(Capability.name == cap_in.name))
        if existing is not None:
            results.append(
                OpenCapabilityResultOut(
                    name=existing.name,
                    type=existing.type,
                    status="exists",
                    enabled=existing.enabled,
                    smoke_summary="同名能力已存在，本次跳过（不覆盖既有配置）",
                )
            )
            continue
        # 校验/冒烟失败会以 4xx 抛出（create_capability 内部处理），冒烟未过则落库 disabled
        cap, report = await cap_service.create_capability(db, cap_in)
        results.append(
            OpenCapabilityResultOut(
                name=cap.name,
                type=cap.type,
                status="created" if report.passed else "smoke_failed",
                enabled=cap.enabled,
                smoke_summary=report.summary,
            )
        )
    return results


def _normalize_sub_workers(subs: list[OpenSubWorkerIn]) -> list[dict]:
    return [
        {
            "name": s.name,
            "seq": s.seq,
            "kind": s.kind,
            "optional": s.optional if s.optional is not None else s.kind == "branch",
            "description": s.description,
            "capability_hint": s.capability_hint,
            "playbook": s.playbook,
            "inputs": [i.to_registry() for i in s.inputs],
        }
        for s in subs
    ]


async def register_bundle(db: AsyncSession, body: OpenWorkerRegisterIn) -> OpenWorkerRegisterOut:
    """一键注册：能力清单 → 引用校验 → Worker 文件包。"""
    warnings: list[str] = []

    # 1. 能力注册（幂等）
    cap_results = await _register_capabilities(db, body.capabilities)
    for r in cap_results:
        if r.status == "smoke_failed":
            warnings.append(
                f"能力「{r.name}」冒烟未通过（{r.smoke_summary}）：已落库但未启用，"
                "修复服务后经平台管理端重试启用"
            )
        elif r.status == "exists":
            warnings.append(f"能力「{r.name}」已存在：沿用平台既有配置（含密钥与启停状态）")

    # 2. 引用清单校验（平台已有 ∪ 本次提交）
    ref_names = set(body.worker.capabilities)
    if ref_names:
        rows = list(
            (
                await db.scalars(
                    select(Capability).where(Capability.name.in_(ref_names))  # type: ignore[arg-type]
                )
            ).all()
        )
        registered = {c.name for c in rows}
    else:
        rows = []
        registered = set()
    submitted = {c.name for c in body.capabilities}
    missing = sorted(ref_names - registered - submitted)
    if missing:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"Worker「{body.worker.name}」引用的能力未注册且未包含在本次提交中："
            f"{', '.join(missing)}。请把它们加入 capabilities 一并提交，"
            "或改引平台已有能力（GET /open/capabilities 查询）。",
        )

    # 2.5 容量硬门：声明的能力展开后的工具总数不得越过硬上限（方案 §4 P0-2）
    from app.modules.discovery.assembler import MAX_TOOLS_HARD, count_capability_tools
    from app.modules.discovery.retriever import cap_dict

    tool_count = await count_capability_tools([cap_dict(c) for c in rows])
    if tool_count > MAX_TOOLS_HARD:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"Worker「{body.worker.name}」的 capabilities 展开后共 {tool_count} 个工具，"
            f"超过硬上限 {MAX_TOOLS_HARD}；请收敛 capabilities 名单，或按需拆分 Worker。",
        )
    # 2.6 更早预警（P0-2 增量）：绑定了目标 Agent 时，把"装不下"报出来（非阻断）
    warnings.extend(await target_agent_warnings(db, tool_count, body.target_agents))

    # 3. Worker 文件包落盘
    meta = registry.get_meta(body.worker.name)
    try:
        if meta is None:
            meta = registry.ensure_worker(
                body.worker.name,
                description=body.worker.description,
                icon=body.worker.icon,
                color=body.worker.color,
                capabilities=body.worker.capabilities,
                references=body.worker.references,
                playbook=body.worker.playbook,
                sub_workers=_normalize_sub_workers(body.worker.sub_workers),
                inputs=[i.to_registry() for i in body.worker.inputs],
            )
            action, version = "created", meta.effective_version
        elif body.if_exists == "fail":
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"Worker「{body.worker.name}」已存在（当前生效版本 {meta.effective_version}）。"
                "重试时设置 if_exists=skip 沿用现状，或 if_exists=new_version 发布新版本。",
            )
        elif body.if_exists == "skip":
            action, version = "skipped", meta.effective_version
        else:  # new_version
            version = registry.publish_version(
                body.worker.name,
                description=body.worker.description,
                icon=body.worker.icon,
                color=body.worker.color,
                capabilities=body.worker.capabilities,
                references=body.worker.references,
                playbook=body.worker.playbook,
                sub_workers=_normalize_sub_workers(body.worker.sub_workers),
                inputs=[i.to_registry() for i in body.worker.inputs],
            )
            action = "new_version"
    except WorkerError as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(e)) from e

    if not body.worker.playbook:
        warnings.append(
            "L2 playbook 为空，已落脚手架模板：请经平台「能力 → Worker」文件管理器补写"
            "（五件事 + 第零步依赖预检），否则 Agent 只有 L1 简介可用"
        )

    return OpenWorkerRegisterOut(
        worker_name=body.worker.name,
        action=action,  # type: ignore[arg-type]
        version=version,
        capability_results=cap_results,
        missing_capabilities=[],
        warnings=warnings,
    )
