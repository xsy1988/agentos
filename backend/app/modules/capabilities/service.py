"""capabilities 服务：builtin 占位工具 seed（M2-2c）。

幂等：按 name upsert。M3 接入 MCP/HTTP 注册测试协议后，本 seed 保留——
builtin 演示工具是零配置冒烟链路的一部分。
"""

from typing import Any

from sqlalchemy import select

from app.core.db import session_factory
from app.modules.capabilities.models import Capability
from app.modules.engine.tools_builtin import builtin_tool_schema

# builtin 占位工具清单（name → 描述 / 风险级 / 参数 schema）
BUILTIN_SEED: list[dict[str, Any]] = [
    {
        "name": "echo",
        "description": "占位工具：原样返回输入文本（验证工具调用链路）",
        "risk_level": "read",
        "params": {
            "type": "object",
            "properties": {"text": {"type": "string", "description": "要原样返回的文本"}},
            "required": ["text"],
        },
    },
    {
        "name": "dangerous_demo",
        "description": "占位高危工具：演示确认卡片流程（无真实副作用）",
        "risk_level": "dangerous",
        "params": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "description": "要执行的危险动作名"},
                "target": {"type": "string", "description": "目标对象"},
            },
            "required": ["action"],
        },
    },
]


async def seed_builtin_capabilities() -> None:
    async with session_factory() as db:
        for item in BUILTIN_SEED:
            cap = await db.scalar(select(Capability).where(Capability.name == item["name"]))
            payload = {
                "builtin": item["name"],
                "schema": builtin_tool_schema(
                    item["name"], item["description"], item["params"]
                ),
            }
            if cap is None:
                db.add(
                    Capability(
                        type="tool",
                        category="builtin",
                        name=item["name"],
                        description=item["description"],
                        risk_level=item["risk_level"],
                        payload=payload,
                        health_status="ok",
                    )
                )
            else:
                cap.description = item["description"]
                cap.risk_level = item["risk_level"]
                cap.payload = payload
                cap.health_status = "ok"
        await db.commit()
