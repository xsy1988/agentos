"""自建网页搜索服务入驻 seed：平台侧只登记一个 type=mcp 能力，不并入任何搜索代码。

五级漏斗（查询理解 → 多路召回 → RRF 融合 → cross-encoder 精排 → 正文抽取 + 片段级精排）
全部在 `services/websearch/` 内完成，torch / trafilatura / jieba / chromium 一个都不进
核心进程（设计方案 §7.3）。平台这一层薄到只有三件事：

1. 幂等登记 `websearch` 能力（type=mcp / category=external / transport=http）；
2. 按 REST `/health` 探测结果决定 enabled 与 health_status——搜索栈是 compose profile，
   默认不启动，不该以「已启用但连不上」的样子占着检索池；探测失败**不阻断启动**
   （与采购 seed 同纪律：定义与运行解耦）；
3. 给默认 Agent 建 pinned 绑定。理由：`assemble_tools` 里 pinned 先入列表、必然占位，
   而现有十几个 builtin 能力在抢 `tool_budget=8` 的名额——通用联网搜索不该靠语义竞争上岗。

已知陷阱（本模块刻意规避）：幂等 seed 的「已存在」分支若只判断不刷新，旧库里的
description / payload.url / version 会永远停在第一次写入的值。下面每条可变字段都刷。
"""

import logging
from typing import Any
from uuid import UUID

import httpx
from sqlalchemy import select

from app.core.config import settings
from app.core.db import session_factory
from app.modules.agents.models import Agent
from app.modules.capabilities.models import Capability, CapabilityBinding
from app.modules.capabilities.service import broadcast_changed, index_capability

logger = logging.getLogger(__name__)

WEBSEARCH_CAPABILITY_NAME = "websearch"

# 与服务侧 src/websearch/__init__.py 的 __version__ 手工对齐：平台不 import 服务代码
# （隔离区依赖纪律），所以只能靠约定。服务升版本时记得同步这里。
WEBSEARCH_VERSION = "0.1.0"

# description 同时是语义发现的检索文本（index_capability 取 name+description+transport 摘要），
# 因此把用户真会说的表述都铺进去：联网搜索 / 网页搜索 / 查资料 / 最新信息 / 新闻 / 官方文档。
DESCRIPTION = (
    "自建网页搜索服务（自托管、零按次查询成本）：联网搜索网页、查资料、找最新信息与新闻进展、"
    "查官方文档与技术规范、核实事实。多路召回 + 融合去重 + cross-encoder 精排 + 正文抽取，"
    "返回已排序、已去重、带证据片段的结果，可直接引用作答；也可抓取单个 URL 的正文并精选片段。"
    "覆盖「上网查一下 / 搜一搜 / 最近有什么新进展 / 官方怎么说 / 帮我找找资料」等表述。"
)

# 冒烟用例（走 capabilities.smoke.run_smoke_test）。expected 一律留空 = 只要求执行不报错：
# 搜索结果天天在变，写死断言只会造出一个逢跑必红的脆弱用例。
# 第二条刻意 extract=False：冒烟单用例上限 30s，而冷启动抽正文（抓取 + 片段精排）会顶到线上。
TEST_INFO: list[dict[str, Any]] = [
    {"tool": "search_meta", "input": {}, "expected": ""},
    {
        "tool": "web_search",
        "input": {"query": "什么是向量数据库", "max_results": 3, "extract": False},
        "expected": "",
    },
]


def _payload() -> dict[str, Any]:
    """MCP 连接参数（mcp_client.open_mcp_session 按 transport=http 走 streamable http）。"""
    return {
        "transport": "http",
        "url": settings.search_mcp_url,
        # 来源标记：seed 只自动拨自己登记的那条的开关，人工接管后不再覆盖
        "managed_by": "websearch_seed",
    }


async def _probe_reachable() -> bool:
    """探 REST `/health`（不走 MCP：握手 + list_tools 比一次 GET 贵得多，且启动期要快）。

    超时刻意短（默认 3s）：这一步在 lifespan 里同步执行，搜索栈没起时不能拖慢后端启动。
    """
    url = f"{settings.search_rest_base_url.rstrip('/')}/health"
    try:
        async with httpx.AsyncClient(timeout=settings.search_probe_timeout) as client:
            resp = await client.get(url)
        if resp.status_code == 200:
            return True
        logger.info("websearch 探测返回 %d（%s）→ 视为不可用", resp.status_code, url)
    except Exception as e:  # noqa: BLE001 —— 探测失败是常态（profile 没起），不该炸启动
        logger.info("websearch 服务不可达（%s: %s）→ 能力置 disabled", type(e).__name__, e)
    return False


async def _bind_default_agent(db: Any, cap_id: UUID) -> None:
    """给默认 Agent 建 pinned 绑定（幂等）。没有默认 Agent 就静默跳过。"""
    stmt = (
        select(Agent.id)
        .where(Agent.is_default.is_(True))  # noqa: E712
        .order_by(Agent.created_at)
        .limit(1)
    )
    agent_id = await db.scalar(stmt)
    if agent_id is None:
        return
    binding = await db.scalar(
        select(CapabilityBinding).where(
            CapabilityBinding.agent_id == agent_id,
            CapabilityBinding.capability_id == cap_id,
        )
    )
    if binding is None:
        db.add(CapabilityBinding(agent_id=agent_id, capability_id=cap_id, mode="pinned"))
    elif binding.mode != "pinned":
        binding.mode = "pinned"


async def seed_websearch_capability() -> None:
    """幂等登记（应用启动时调用，晚于 seed_default_agent / seed_builtin_capabilities）。"""
    reachable = await _probe_reachable()
    health = "healthy" if reachable else "unhealthy"

    async with session_factory() as db:
        cap = await db.scalar(
            select(Capability).where(Capability.name == WEBSEARCH_CAPABILITY_NAME)
        )
        created = cap is None
        if cap is None:
            cap = Capability(
                type="mcp",
                category="external",
                name=WEBSEARCH_CAPABILITY_NAME,
                description=DESCRIPTION,
                version=WEBSEARCH_VERSION,
                risk_level="read",  # 只读联网检索，不触发确认门
                payload=_payload(),
                test_info=TEST_INFO,
                enabled=reachable,
                health_status=health,
            )
            db.add(cap)
            prev_enabled: bool | None = None
        else:
            prev_enabled = cap.enabled
            # ---- 已存在分支：刷新全部可变字段（否则旧库永远停在第一次写入的版本）----
            cap.description = DESCRIPTION
            cap.version = WEBSEARCH_VERSION
            cap.test_info = TEST_INFO
            # merge 而非替换：保留人工加过的 headers / secret_env_encrypted
            payload = dict(cap.payload or {})
            payload.update(_payload())
            cap.payload = payload
            cap.health_status = health
            # 开关只由 seed 拨自己登记的那条；人工接管（managed_by 被改掉）后不再干预
            if payload.get("managed_by") == "websearch_seed":
                cap.enabled = reachable

        await db.flush()
        # 语义向量：无可用 embedding provider 时 index_capability 静默返回 False，
        # 此时该能力只靠 pinned 常驻出现（retriever 的 pinned-only 退化路径）
        await index_capability(db, cap)
        await _bind_default_agent(db, cap.id)
        cap_id = cap.id
        enabled_now = cap.enabled
        await db.commit()

    # 状态翻转才广播：新建、或 enabled 发生 True↔False 变化（mcp 池据此拉起/摘除连接）。
    # 启动期广播其实是空转（池还没 start），但 seed 也可能被运行时重复调用，留着才对称。
    if created or prev_enabled != enabled_now:
        await broadcast_changed(cap_id)

    logger.info(
        "websearch capability seeded: enabled=%s health=%s url=%s%s",
        enabled_now,
        health,
        settings.search_mcp_url,
        "" if reachable else "（搜索栈未启动：docker compose --profile search up -d）",
    )
