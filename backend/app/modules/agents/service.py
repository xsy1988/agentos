"""agents 业务逻辑：CRUD + 默认 Agent seed。"""

from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import session_factory
from app.modules.agents.models import Agent
from app.modules.agents.schemas import AgentCreateIn, AgentUpdateIn

# 首次启动 seed：保证零配置可用（M1 阶段未绑定模型，model_provider_id 留空）
DEFAULT_AGENT_SEED = AgentCreateIn(
    name="小助",
    description="平台默认 Agent——首次启动自动创建，零配置可用。",
    soul_md=(
        "# SOUL\n\n"
        "- 语气：平和、务实，不堆砌客套话\n"
        "- 行为准则：先理解任务再动手；拿不准就问，不自作主张\n"
        "- 汇报习惯：结论先行，过程可追溯\n"
    ),
    identity_md=(
        "# IDENTITY\n\n"
        "- 名字：小助\n"
        "- 定位：个人工作助手，处理与编程无关的明确工作任务\n"
        "- 能力边界：通过平台注册的 MCP/工具/插件/Skill 完成任务，不自己写代码\n"
    ),
    memory_md="# MEMORY\n\n（常驻记忆为空，动态记忆见 memory_files）\n",
    system_prompt="",
)


async def list_agents(db: AsyncSession, include_disabled: bool = False) -> list[Agent]:
    stmt = select(Agent).order_by(Agent.created_at)
    if not include_disabled:
        stmt = stmt.where(Agent.status == "enabled")
    return list((await db.scalars(stmt)).all())


async def get_agent_or_404(db: AsyncSession, agent_id: UUID) -> Agent:
    agent = await db.get(Agent, agent_id)
    if agent is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Agent 不存在")
    return agent


async def create_agent(db: AsyncSession, body: AgentCreateIn) -> Agent:
    agent = Agent(**body.model_dump())
    db.add(agent)
    await db.commit()
    await db.refresh(agent)
    return agent


async def update_agent(db: AsyncSession, agent: Agent, body: AgentUpdateIn) -> Agent:
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(agent, field, value)
    await db.commit()
    await db.refresh(agent)
    return agent


async def soft_delete_agent(db: AsyncSession, agent: Agent) -> None:
    """主数据不做物理删除（数据库设计 §4）：置 disabled。"""
    agent.status = "disabled"
    await db.commit()


async def seed_default_agent() -> None:
    """应用启动时幂等 seed（见开发计划 M1）。"""
    async with session_factory() as db:
        existing = await db.scalar(select(Agent).where(Agent.is_default.is_(True)))
        if existing is None:
            db.add(Agent(**DEFAULT_AGENT_SEED.model_dump(), is_default=True))
            await db.commit()
