"""workers 路由：Worker 文件包管理（/workers，替代旧 /task-types）。

管理本质 = 管理 WORKER.md 文件包：
- 列表/详情/启停/删除（Worker 级）
- 版本管理：手动构建新版本（编辑文件不产生新版本）、删除历史版本、切换 active
- 在线文件管理器：版本内文件树 + 读写（只有 active 版本可写，历史版本只读）
- 子任务脚手架 + 工具引用清单校验（与 capabilities 表比对）
"""

from datetime import datetime
from uuid import UUID  # noqa: F401 —— 保持与其它 router 一致的导入习惯

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.modules.auth.deps import get_current_user
from app.modules.capabilities.models import Capability
from app.modules.discovery.assembler import MAX_TOOLS_HARD
from app.modules.workers import registry
from app.modules.workers.registry import WorkerError
from app.modules.workers.schemas import (
    CapabilityRefOut,
    CapabilityRefsOut,
    FileNodeOut,
    SubWorkerCreateIn,
    SubWorkerOut,
    VersionBuildOut,
    WorkerCreateIn,
    WorkerFileCreateIn,
    WorkerFileIn,
    WorkerFileOut,
    WorkerOut,
    WorkerPatchIn,
    WorkerVersionOut,
)

router = APIRouter(
    prefix="/workers",
    tags=["workers"],
    dependencies=[Depends(get_current_user)],
)


def _err(e: WorkerError) -> HTTPException:
    return HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(e))


def _get_meta(name: str):
    meta = registry.get_meta(name)
    if meta is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Worker「{name}」不存在")
    return meta


def _resolve_version(name: str, version: str | None) -> str:
    """version 为空 → 生效版本；版本不存在 → 404。"""
    meta = _get_meta(name)
    target = version or meta.effective_version
    if target is None or target not in meta.versions:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"版本「{version}」不存在")
    return target


async def _expanded_tool_count(db: AsyncSession, names: list[str]) -> int:
    """Worker 声明的能力名单 → 展开后的工具总数（发布前硬门校验）。"""
    from app.modules.discovery.assembler import count_capability_tools
    from app.modules.discovery.retriever import cap_dict

    if not names:
        return 0
    rows = list(
        (await db.scalars(select(Capability).where(Capability.name.in_(names)))).all()
    )
    return await count_capability_tools([cap_dict(c) for c in rows])


def _to_out(meta) -> WorkerOut:
    wdef = meta.def_
    versions = [
        WorkerVersionOut(
            version=v,
            active=v == meta.effective_version,
            latest=v == meta.latest_version,
            created_at=datetime.fromtimestamp(registry.version_dir(meta.name, v).stat().st_mtime),
        )
        for v in meta.versions
    ]
    return WorkerOut(
        name=meta.name,
        description=wdef.description if wdef else "",
        icon=wdef.icon if wdef else None,
        color=wdef.color if wdef else None,
        enabled=meta.enabled,
        active_version=meta.effective_version,
        pinned_version=meta.active_version,
        latest_version=meta.latest_version,
        versions=versions,
        capabilities=wdef.capabilities if wdef else [],
        references=wdef.references if wdef else None,
        playbook=wdef.playbook if wdef else "",
        sub_workers=[
            SubWorkerOut(
                ref=s.ref,
                name=s.name,
                seq=s.seq,
                kind=s.kind,  # type: ignore[arg-type]
                optional=s.optional,
                description=s.description,
                capability_hint=s.capability_hint,
            )
            for s in (wdef.ordered_sub_workers() if wdef else [])
        ],
        has_files=wdef is not None,
    )


# ---------- Worker 级 ----------


@router.get("", response_model=list[WorkerOut])
async def list_workers() -> list[WorkerOut]:
    return [_to_out(m) for m in registry.list_workers()]


@router.post("", response_model=WorkerOut, status_code=status.HTTP_201_CREATED)
async def create_worker(body: WorkerCreateIn) -> WorkerOut:
    try:
        meta = registry.create_worker(
            body.name, description=body.description, icon=body.icon, color=body.color
        )
    except WorkerError as e:
        raise _err(e) from e
    return _to_out(meta)


@router.get("/{name}", response_model=WorkerOut)
async def get_worker(name: str) -> WorkerOut:
    return _to_out(_get_meta(name))


@router.patch("/{name}", response_model=WorkerOut)
async def patch_worker(name: str, body: WorkerPatchIn) -> WorkerOut:
    _get_meta(name)
    try:
        if body.enabled is not None:
            registry.set_enabled(name, body.enabled)
        if body.active_version is not None:
            registry.set_active_version(name, body.active_version)
    except WorkerError as e:
        raise _err(e) from e
    return _to_out(_get_meta(name))


@router.delete("/{name}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_worker(name: str) -> None:
    _get_meta(name)
    try:
        registry.delete_worker(name)
    except WorkerError as e:
        raise _err(e) from e


# ---------- 版本管理 ----------


@router.post(
    "/{name}/versions", response_model=VersionBuildOut, status_code=status.HTTP_201_CREATED
)
async def build_version(name: str, db: AsyncSession = Depends(get_db)) -> VersionBuildOut:
    """手动构建新版本：复制当前生效版本 → vN+1，并把 active 指向新版本。

    发布前校验展开后的工具数（方案 §4 P0-2）：Worker 没有 Agent 上下文，拿不到
    它的 `tool_budget`，故以 `MAX_TOOLS_HARD` 为硬门——超过 24 个工具的能力组合
    任何 Agent 都装不下，与其上线后在 run 里失败，不如在发布这一步就拒绝。
    """
    meta = _get_meta(name)
    copied_from = meta.effective_version or ""
    wdef = registry.get_def(name, copied_from) if copied_from else None
    names = list(wdef.capabilities) if wdef else []
    tool_count = await _expanded_tool_count(db, names)
    if tool_count > MAX_TOOLS_HARD:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"能力展开后共 {tool_count} 个工具，超过硬上限 {MAX_TOOLS_HARD}；"
            "请先收敛 WORKER.md 的 capabilities 名单，或按需拆分 Worker",
        )
    try:
        version = registry.build_version(name)
    except WorkerError as e:
        raise _err(e) from e
    return VersionBuildOut(version=version, copied_from=copied_from, tool_count=tool_count)


@router.delete("/{name}/versions/{version}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_version(name: str, version: str) -> None:
    _get_meta(name)
    try:
        registry.delete_version(name, version)
    except WorkerError as e:
        raise _err(e) from e


# ---------- 在线文件管理器 ----------


@router.get("/{name}/tree", response_model=FileNodeOut)
async def get_tree(name: str, version: str | None = None) -> FileNodeOut:
    v = _resolve_version(name, version)
    try:
        return FileNodeOut.model_validate(registry.version_tree(name, v))
    except WorkerError as e:
        raise _err(e) from e


@router.get("/{name}/file", response_model=WorkerFileOut)
async def get_file(name: str, path: str = Query(alias="path"), version: str | None = None):
    v = _resolve_version(name, version)
    meta = _get_meta(name)
    try:
        content = registry.read_version_file(name, v, path)
    except WorkerError as e:
        raise _err(e) from e
    return WorkerFileOut(
        path=path, version=v, writable=v == meta.effective_version, content=content
    )


@router.put("/{name}/file", response_model=WorkerFileOut)
async def put_file(
    name: str,
    body: WorkerFileIn,
    path: str = Query(alias="path"),
    version: str | None = None,
) -> WorkerFileOut:
    """编辑保存文件（不产生新版本；只有 active 版本可写；WORKER.md 头校验）。"""
    v = _resolve_version(name, version)
    try:
        registry.write_version_file(name, v, path, body.content)
    except WorkerError as e:
        raise _err(e) from e
    return WorkerFileOut(path=path, version=v, writable=True, content=body.content)


@router.post("/{name}/file", response_model=WorkerFileOut, status_code=status.HTTP_201_CREATED)
async def create_file(
    name: str,
    body: WorkerFileCreateIn,
    version: str | None = None,
) -> WorkerFileOut:
    """新建文件（references/*.md 等文本文件）。"""
    v = _resolve_version(name, version)
    try:
        registry.create_version_file(name, v, body.path, body.content)
    except WorkerError as e:
        raise _err(e) from e
    return WorkerFileOut(path=body.path, version=v, writable=True, content=body.content)


@router.delete("/{name}/file", status_code=status.HTTP_204_NO_CONTENT)
async def delete_file(
    name: str, path: str = Query(alias="path"), version: str | None = None
) -> None:
    v = _resolve_version(name, version)
    try:
        registry.delete_version_file(name, v, path)
    except WorkerError as e:
        raise _err(e) from e


@router.post(
    "/{name}/sub-workers", response_model=SubWorkerOut, status_code=status.HTTP_201_CREATED
)
async def create_sub_worker(name: str, body: SubWorkerCreateIn, version: str | None = None):
    """新建子任务文件夹脚手架（sub_workers/<名>/WORKER.md）。"""
    v = _resolve_version(name, version)
    try:
        registry.create_sub_worker(name, v, body.name, kind=body.kind)
    except WorkerError as e:
        raise _err(e) from e
    wdef = registry.get_def(name, v)
    sub = wdef.find_sub(body.name) if wdef else None
    if sub is None:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "子任务创建后解析失败")
    return SubWorkerOut(
        ref=sub.ref,
        name=sub.name,
        seq=sub.seq,
        kind=sub.kind,  # type: ignore[arg-type]
        optional=sub.optional,
        description=sub.description,
        capability_hint=sub.capability_hint,
    )


# ---------- 工具引用清单校验 ----------


@router.get("/{name}/capabilities", response_model=CapabilityRefsOut)
async def check_capability_refs(
    name: str, version: str | None = None, db: AsyncSession = Depends(get_db)
) -> CapabilityRefsOut:
    """WORKER.md 的 capabilities 名单 ↔ 平台已注册能力比对（命中/缺失）。"""
    v = _resolve_version(name, version)
    wdef = registry.get_def(name, v)
    names = wdef.capabilities if wdef else []
    rows = list(
        (
            await db.scalars(
                select(Capability).where(Capability.name.in_(names))
                if names
                else select(Capability).limit(0)
            )
        ).all()
    )
    by_name = {c.name: c for c in rows}
    refs: list[CapabilityRefOut] = []
    missing: list[str] = []
    for n in names:
        cap = by_name.get(n)
        if cap is None:
            missing.append(n)
            refs.append(CapabilityRefOut(name=n, found=False))
        else:
            refs.append(
                CapabilityRefOut(
                    name=n,
                    found=True,
                    capability_id=str(cap.id),
                    type=cap.type,
                    risk_level=cap.risk_level,
                    enabled=cap.enabled,
                )
            )
    return CapabilityRefsOut(version=v, references=refs, missing=missing)
