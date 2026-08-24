"""skills_forge 钩子：on_run_end 异步触发复盘（不阻塞结果返回）。

挂接条件（模块详细设计 §1.4）：
- run done 且 iterations ≥ 3 → 成功复盘（success）
- 纠错沉淀（correction）由确认卡片路径显式调用 review_run，
  钩子只负责 success 一类。
"""

import asyncio
import logging
from typing import Any

from app.modules.engine.hooks import RunContext

logger = logging.getLogger(__name__)


class SkillsForgeHook:
    """复盘触发钩子：fire-and-forget，异常只记日志。"""

    async def on_run_end(self, ctx: RunContext, status: str, result: Any) -> None:
        if status != "done":
            return
        iterations = int(ctx.budget.get("iterations") or 0)
        if iterations < 3:  # 太简单不值得沉淀
            return
        asyncio.create_task(self._review(ctx.run_id))

    async def _review(self, run_id: str) -> None:
        from app.modules.skills_forge.service import review_run

        try:
            outcome = await review_run(run_id, trigger="success")
            logger.info("skills_forge review run %s: %s", run_id, outcome)
        except Exception:  # noqa: BLE001 复盘永不影响主链路
            logger.exception("skills_forge review run %s failed", run_id)
