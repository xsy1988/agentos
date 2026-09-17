"""open_api 路由：第三方开发者开放注册接口（/open，静态令牌鉴权）。

端点一览（完整契约见 docs/第三方Worker开发与注册规范.md）：
- GET  /open/capabilities            能力目录（查名冲突 / 引用平台已有能力）
- GET  /open/workers                 Worker 目录
- GET  /open/workers/{name}          Worker 概览 + 引用清单校验
- POST /open/workers/register        一键注册（Worker + 能力捆绑包）
- POST /open/awaits/{id}/resolve     外部流程回传等待结果（P0-4，回调契约）
"""

from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.db import get_db
from app.modules.awaits.models import AwaitBroker
from app.modules.awaits.schemas import AwaitResolveIn, AwaitResolveOut
from app.modules.awaits.service import enqueue_resume, resolve, verify_callback_token
from app.modules.capabilities.models import Capability
from app.modules.open_api.schemas import (
    OpenCapabilityBriefOut,
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
