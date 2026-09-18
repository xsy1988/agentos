"""open_api 路由：第三方开发者开放注册接口（/open，静态令牌鉴权）。

端点一览（完整契约见 docs/第三方Worker开发与注册规范.md）：
- GET  /open/capabilities            能力目录（查名冲突 / 引用平台已有能力）
- GET  /open/workers                 Worker 目录
- GET  /open/workers/{name}          Worker 概览 + 引用清单校验
- POST /open/workers/register        一键注册（Worker + 能力捆绑包）
- POST /open/awaits/{id}/resolve     外部流程回传等待结果（P0-4，回调契约）
- GET  /open/awaits/{id}/files       本次等待所属 run 的可取文件清单（P2-1，取件通道）
- GET  /open/files/{id}/content      取件（P2-1，只读，归属校验 + 体积上限）
"""

from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, status
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.db import get_db
from app.modules.awaits.models import AwaitBroker
from app.modules.awaits.schemas import AwaitResolveIn, AwaitResolveOut
from app.modules.awaits.service import enqueue_resume, resolve, verify_callback_token
from app.modules.capabilities.models import Capability
from app.modules.open_api import files as open_files
from app.modules.open_api.schemas import (
    OpenCapabilityBriefOut,
    OpenRunFileOut,
    OpenWorkerBriefOut,
    OpenWorkerRegisterIn,
    OpenWorkerRegisterOut,
)
from app.modules.open_api.service import register_bundle
from app.modules.workers import registry


async def verify_api_key(x_api_key: str | None = Header(default=None)) -> None:
    """开放接口鉴权：X-API-Key 头与 settings.open_api_token 比对。

    未配置令牌 = 开放接口整体停用（503）；令牌不匹配 = 401。
    """
    if not settings.open_api_token:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "开放注册接口未启用：平台侧需在环境变量配置 OPEN_API_TOKEN",
        )
    if x_api_key != settings.open_api_token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "X-API-Key 无效")


router = APIRouter(
    prefix="/open",
    tags=["open-api"],
    dependencies=[Depends(verify_api_key)],
)


@router.get("/capabilities", response_model=list[OpenCapabilityBriefOut])
async def list_open_capabilities(
    include_disabled: bool = False, db: AsyncSession = Depends(get_db)
) -> list[OpenCapabilityBriefOut]:
    """能力目录：第三方提交前查名冲突，或把平台已有能力写进引用清单。"""
    stmt = select(Capability)
    if not include_disabled:
        stmt = stmt.where(Capability.enabled.is_(True))  # noqa: E712
    stmt = stmt.order_by(Capability.type, Capability.name)
    caps = list((await db.scalars(stmt)).all())
    return [
        OpenCapabilityBriefOut(
            name=c.name,
            type=c.type,
            category=c.category,
            risk_level=c.risk_level,
            enabled=c.enabled,
            description=c.description,
        )
        for c in caps
    ]


@router.get("/workers", response_model=list[OpenWorkerBriefOut])
async def list_open_workers() -> list[OpenWorkerBriefOut]:
    """Worker 目录：注册前确认命名不冲突。"""
    out: list[OpenWorkerBriefOut] = []
    for m in registry.list_workers():
        out.append(
            OpenWorkerBriefOut(
                name=m.name,
                description=m.def_.description if m.def_ else "",
                enabled=m.enabled,
                active_version=m.effective_version,
                capabilities=m.def_.capabilities if m.def_ else [],
            )
        )
    return out


@router.get("/workers/{name}", response_model=OpenWorkerBriefOut)
async def get_open_worker(name: str, db: AsyncSession = Depends(get_db)) -> OpenWorkerBriefOut:
    """Worker 概览 + 引用清单校验（found/missing，引用了未注册能力会如实指出）。"""
    meta = registry.get_meta(name)
    if meta is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Worker「{name}」不存在")
    wdef = meta.def_
    names = wdef.capabilities if wdef else []
    registered: set[str] = set()
    if names:
        rows = await db.scalars(
            select(Capability).where(Capability.name.in_(names))  # type: ignore[arg-type]
        )
        registered = {c.name for c in rows}
    missing = [n for n in names if n not in registered]
    out = OpenWorkerBriefOut(
        name=meta.name,
        description=wdef.description if wdef else "",
        enabled=meta.enabled,
        active_version=meta.effective_version,
        capabilities=names,
    )
    if missing:
        out.description += f"（引用未注册能力：{', '.join(missing)}）"
    return out


@router.post(
    "/workers/register", response_model=OpenWorkerRegisterOut, status_code=status.HTTP_201_CREATED
)
async def register_open_worker(
    body: OpenWorkerRegisterIn, db: AsyncSession = Depends(get_db)
) -> OpenWorkerRegisterOut:
    """一键注册：Worker 文件包 + 其依赖的 mcp/tool/plugin/skill 能力捆绑提交。"""
    return await register_bundle(db, body)


@router.post("/awaits/{await_id}/resolve", response_model=AwaitResolveOut)
async def resolve_await(
    await_id: UUID, body: AwaitResolveIn, db: AsyncSession = Depends(get_db)
) -> AwaitResolveOut:
    """外部流程回传等待结果（P0-4）：唤醒 run 从 interrupt 检查点继续。

    双重鉴权：X-API-Key（路由依赖）+ callback_token（本次等待专属）；
    幂等：CAS `waiting → granted`，只有翻成功的那一路投递唤醒事件——
    重复回调/超时先到都只回既有状态，不产生第二次唤醒。
    """
    row = await db.get(AwaitBroker, await_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "等待记录不存在")
    if not verify_callback_token(str(row.id), body.callback_token):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "callback_token 无效")
    if body.idempotency_key and body.idempotency_key != row.idempotency_key:
        raise HTTPException(status.HTTP_409_CONFLICT, "idempotency_key 与登记时不一致")
    resolved, won = await resolve(
        db,
        row.id,
        status="granted",
        payload=body.payload,
        error=body.error,
    )
    assert resolved is not None
    if won:
        await enqueue_resume(db, resolved, status="granted")
    await db.commit()
    return AwaitResolveOut(
        await_id=str(resolved.id),
        run_id=str(resolved.run_id),
        status=resolved.status,
        resumed=won,
        waited_ms=int(resolved.waited_ms or 0),
    )


# ---------- 取件通道（P2-1）：只读、按等待凭据锁定归属、无列举 ----------


def _require_await_credential(await_id: UUID, token: str | None) -> None:
    """取件的第二重鉴权：等待专属 `callback_token`（与路由的 X-API-Key 叠加）。

    平台级 `X-API-Key` 只证明"是认可过的集成方"，不证明"这份文件归你"；
    真正把范围锁到"自己那个 run"的是这笔等待的凭据。
    """
    if not verify_callback_token(str(await_id), token):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "callback_token 无效")


async def _run_scope(db: AsyncSession, await_id: UUID) -> UUID:
    run_id = await open_files.await_run_id(db, await_id)
    if run_id is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "等待记录不存在")
    return run_id


@router.get("/awaits/{await_id}/files", response_model=list[OpenRunFileOut])
async def list_await_files(
    await_id: UUID,
    db: AsyncSession = Depends(get_db),
    x_callback_token: str | None = Header(default=None),
) -> list[OpenRunFileOut]:
    """本次等待所属 run 的可取文件（产物 + 输入附件）。

    范围由 `await_id` 反查 `await_broker.run_id` 得到，**没有**"按会话/按 owner 列举"
    的入口——第三方无法遍历出别人的文件 id，只能看自己这笔等待的 run。
    """
    _require_await_credential(await_id, x_callback_token)
    run_id = await _run_scope(db, await_id)
    return [OpenRunFileOut(**item) for item in await open_files.list_run_files(db, run_id)]


@router.get("/files/{file_id}/content")
async def download_open_file(
    file_id: UUID,
    await_id: UUID,
    db: AsyncSession = Depends(get_db),
    x_callback_token: str | None = Header(default=None),
) -> FileResponse:
    """取件下载：归属校验通过才落盘返回；超上限 413。

    `await_id` 是**必填**的——取件凭据与文件在同一个请求里，平台才能判"这份文件是否
    属于这笔等待的 run"。文件不存在与不归这个 run 都返回 404（不给存在性预言）。
    """
    _require_await_credential(await_id, x_callback_token)
    run_id = await _run_scope(db, await_id)
    file = await open_files.get_run_file(db, run_id, file_id)
    if file is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "文件不存在或不属于本次等待的 run")
    abs_path = settings.data_dir / file.path
    if not abs_path.is_file():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "文件内容缺失（可能被清理）")
    if abs_path.stat().st_size > settings.open_file_max_bytes:
        raise HTTPException(
            status.HTTP_413_CONTENT_TOO_LARGE,
            f"文件超出取件上限（{settings.open_file_max_bytes // 1024 // 1024}MB）",
        )
    return FileResponse(
        path=abs_path,
        media_type=file.mime or "application/octet-stream",
        filename=file.filename,
        headers={"X-Content-Type-Options": "nosniff"},
    )
