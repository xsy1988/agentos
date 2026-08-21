"""agents 路由：CRUD（软删除）。"""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.modules.agents import service
from app.modules.agents.models import Agent
from app.modules.agents.schemas import AgentCreateIn, AgentOut, AgentUpdateIn
from app.modules.auth.deps import get_current_user

router = APIRouter(
    prefix="/agents",
    tags=["agents"],
    dependencies=[Depends(get_current_user)],
)


@router.get("", response_model=list[AgentOut])
async def list_agents(
    include_disabled: bool = Query(default=False),
    db: AsyncSession = Depends(get_db),
) -> list[Agent]:
    return await service.list_agents(db, include_disabled=include_disabled)


@router.post("", response_model=AgentOut, status_code=status.HTTP_201_CREATED)
async def create_agent(
    body: AgentCreateIn,
    db: AsyncSession = Depends(get_db),
) -> Agent:
    return await service.create_agent(db, body)


@router.get("/{agent_id}", response_model=AgentOut)
async def get_agent(agent_id: UUID, db: AsyncSession = Depends(get_db)) -> Agent:
    return await service.get_agent_or_404(db, agent_id)


@router.patch("/{agent_id}", response_model=AgentOut)
async def update_agent(
    agent_id: UUID,
    body: AgentUpdateIn,
    db: AsyncSession = Depends(get_db),
) -> Agent:
    agent = await service.get_agent_or_404(db, agent_id)
    return await service.update_agent(db, agent, body)


@router.delete("/{agent_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_agent(agent_id: UUID, db: AsyncSession = Depends(get_db)) -> None:
    agent = await service.get_agent_or_404(db, agent_id)
    if agent.is_default:
        raise HTTPException(status.HTTP_409_CONFLICT, "默认 Agent 不可删除")
    await service.soft_delete_agent(db, agent)
