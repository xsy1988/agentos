"""capabilities 路由：CRUD + 冒烟注册 + 工具开关 + Agent 绑定。"""

from uuid import UUID

from fastapi import APIRouter, Depends, Query, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.modules.auth.deps import get_current_user
from app.modules.capabilities import service
from app.modules.capabilities.models import (
    Capability,
    CapabilityBinding,
    CapabilityTool,
)
from app.modules.capabilities.schemas import (
    BindingIn,
    BindingOut,
    CapabilityCreateIn,
    CapabilityOut,
    CapabilitySmokeReport,
    CapabilityToolOut,
    CapabilityUpdateIn,
)

router = APIRouter(
    prefix="/capabilities",
    tags=["capabilities"],
    dependencies=[Depends(get_current_user)],
)


def _to_out(cap: Capability) -> CapabilityOut:
    """ORM → 出参（剥离加密内容）。"""
    payload = service.strip_payload(cap.payload or {})
    has_secret = "secret_env_encrypted" in (cap.payload or {})
    return CapabilityOut(
        id=cap.id,
        type=cap.type,
        category=cap.category,
        name=cap.name,
        description=cap.description,
        version=cap.version,
        risk_level=cap.risk_level,
        payload=payload,
        has_secret_env=has_secret,
        test_info=cap.test_info,
        enabled=cap.enabled,
        health_status=cap.health_status,
        last_health_check_at=cap.last_health_check_at,
        created_at=cap.created_at,
        updated_at=cap.updated_at,
    )


class CapabilityCreatedOut(BaseModel):
    """注册响应：能力 + 冒烟报告（失败时 enabled=false 供排查后修复重试）。"""

    capability: CapabilityOut
    smoke: CapabilitySmokeReport


@router.get("", response_model=list[CapabilityOut])
async def list_capabilities(
    include_disabled: bool = Query(default=False),
    db: AsyncSession = Depends(get_db),
) -> list[CapabilityOut]:
    caps = await service.list_capabilities(db, include_disabled=include_disabled)
    return [_to_out(c) for c in caps]


@router.post("", response_model=CapabilityCreatedOut, status_code=status.HTTP_201_CREATED)
async def create_capability(
    body: CapabilityCreateIn, db: AsyncSession = Depends(get_db)
) -> CapabilityCreatedOut:
    cap, report = await service.create_capability(db, body)
    return CapabilityCreatedOut(capability=_to_out(cap), smoke=report)


@router.get("/{cap_id}", response_model=CapabilityOut)
async def get_capability(cap_id: UUID, db: AsyncSession = Depends(get_db)) -> CapabilityOut:
    return _to_out(await service.get_capability_or_404(db, cap_id))


@router.patch("/{cap_id}", response_model=CapabilityOut)
async def update_capability(
    cap_id: UUID, body: CapabilityUpdateIn, db: AsyncSession = Depends(get_db)
) -> CapabilityOut:
    cap = await service.get_capability_or_404(db, cap_id)
    return _to_out(await service.update_capability(db, cap, body))


@router.delete("/{cap_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_capability(cap_id: UUID, db: AsyncSession = Depends(get_db)) -> None:
    cap = await service.get_capability_or_404(db, cap_id)
    await service.delete_capability(db, cap)


# ---------- 工具级开关 ----------


@router.get("/{cap_id}/tools", response_model=list[CapabilityToolOut])
async def list_capability_tools(
    cap_id: UUID, db: AsyncSession = Depends(get_db)
) -> list[CapabilityTool]:
    cap = await service.get_capability_or_404(db, cap_id)
    return await service.list_tools(db, cap)


@router.patch("/{cap_id}/tools/{tool_name}", response_model=CapabilityToolOut)
async def set_tool_enabled(
    cap_id: UUID, tool_name: str, enabled: bool, db: AsyncSession = Depends(get_db)
) -> CapabilityTool:
    """工具级开关——上下文膨胀的最后闸门（广播热更新）。"""
    cap = await service.get_capability_or_404(db, cap_id)
    return await service.set_tool_enabled(db, cap, tool_name, enabled)


# ---------- Agent 绑定（挂在 agents 命名空间下更合理，但 M3 先并置此处） ----------


bindings_router = APIRouter(
    prefix="/agents/{agent_id}/bindings",
    tags=["capabilities"],
    dependencies=[Depends(get_current_user)],
)


@bindings_router.get("", response_model=list[BindingOut])
async def list_bindings(
    agent_id: UUID, db: AsyncSession = Depends(get_db)
) -> list[CapabilityBinding]:
    return await service.list_bindings(db, agent_id)


@bindings_router.post("", response_model=BindingOut, status_code=status.HTTP_201_CREATED)
async def upsert_binding(
    agent_id: UUID, body: BindingIn, db: AsyncSession = Depends(get_db)
) -> CapabilityBinding:
    return await service.upsert_binding(db, agent_id, body.capability_id, body.mode)


@bindings_router.delete("/{capability_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_binding(
    agent_id: UUID, capability_id: UUID, db: AsyncSession = Depends(get_db)
) -> None:
    await service.delete_binding(db, agent_id, capability_id)
