"""注册期容量预警（方案 §4 P0-2 的「更早预警」增量）。

注册请求不携带 Agent 绑定关系（同一个 Worker 可被不同预算的 Agent 引用），故绑定由
调用方按需传入 `target_agents`——缺省 = 零影响（与既有硬门行为完全一致）。

分工：
- **硬门**（展开工具数 > `MAX_TOOLS_HARD` → 422）留在调用方：它是平台可判定的界；
- 本模块只出**非阻断的 warnings**：把"某个 Agent 装不下"提前告诉配置者，但不替它改配置，
  也不因此拒绝一次合法的注册（`[推断]` 依赖关系的正确性由配置者掌握）。
"""

from collections.abc import Mapping, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.agents.models import Agent

# 与运行时兜底同源：`graph.py` 读 Agent 配置时用 `tool_budget or 8`，
# 历史数据里可能为 NULL，比对时按同一个默认值算，避免预警与实跑口径不一致。
DEFAULT_TOOL_BUDGET = 8

# 提示里给出的复核入口（与 capabilities 路由的实机接口一致）
_VISIBILITY_HINT = "GET /capabilities/visibility?agent_id=…"


def budget_warnings(tool_count: int, budgets: Mapping[str, int]) -> list[str]:
    """展开工具数 vs 目标 Agent 的 `tool_budget`，逐 Agent 出提示（纯函数，可纯单测）。"""
    out: list[str] = []
    for name in sorted(budgets):
        limit = int(budgets[name] or DEFAULT_TOOL_BUDGET)
        if tool_count > limit:
            out.append(
                f"目标 Agent「{name}」的 tool_budget={limit}，"
                f"而本 Worker 展开后 {tool_count} 个工具："
                f"该 Agent 引用时至少 {tool_count - limit} 个工具会因预算不足落进 dropped"
                f"（可用 {_VISIBILITY_HINT} 复核，或提高该 Agent 的 tool_budget）"
            )
    return out


async def target_agent_warnings(
    db: AsyncSession, tool_count: int, target_agents: Sequence[str]
) -> list[str]:
    """按名字取目标 Agent 的 `tool_budget` 后出提示；名字不存在也如实说明，不静默跳过。"""
    names = sorted({str(n).strip() for n in target_agents if str(n).strip()})
    if not names:
        return []
    rows = (await db.scalars(select(Agent).where(Agent.name.in_(names)))).all()
    budgets = {a.name: int(a.tool_budget or DEFAULT_TOOL_BUDGET) for a in rows}
    return budget_warnings(tool_count, budgets) + [
        f"目标 Agent「{n}」不存在：跳过预算比对" for n in names if n not in budgets
    ]
