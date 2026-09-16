"""采购报价对比 Agent 入驻 seed（决策4）：平台侧只做登记，不并入外部代码。

采购 Agent 保持独立外部服务；平台侧极薄——本模块幂等登记：
1. 一个 plugin 能力「采购决策面板」（iframe 前端清单，供侧边栏加载其决策页，DB 能力表）；
2. 一个业务 Worker「供应商报价对比」文件包（data/workers/供应商报价对比/v1/，
   WORKER.md + sub_workers/ 5 主线 + 4 支线；文件为唯一权威，已存在不覆盖用户编辑）；
3. 桥接 builtin tool（5 个，见 capabilities.BUILTIN_SEED）与 plugin 写入
   WORKER.md 的 capabilities 引用清单。

外部服务不可用时桥接工具友好降级，Worker 文件包仍可登记（定义与运行解耦）。
"""

import uuid
from urllib.parse import urlparse

from sqlalchemy import select

from app.core.config import settings
from app.core.db import session_factory
from app.modules.capabilities.models import Capability
from app.modules.capabilities.service import index_capability
from app.modules.workers.registry import ensure_worker

PROCUREMENT_WORKER_NAME = "供应商报价对比"
PROCUREMENT_PLUGIN_NAME = "采购决策面板"

# 桥接能力名（与 capabilities.BUILTIN_SEED / tools_builtin.BUILTIN_TOOLS 一致）
BRIDGE_TOOL_NAMES = [
    "procurement_trigger",
    "procurement_status",
    "procurement_decisions",
    "procurement_submit_decision",
    "procurement_report",
]

# L2 正文 playbook：干什么 / 怎么干 / 会遇到什么问题 / 如何处理 / 能力路由（五件事）
WORKER_PLAYBOOK = """# 供应商报价对比 Worker

## 干什么
接收多家供应商的报价单，完成语义映射、机械价差比对与 AI 综合分析，产出结构化对比报告。
执行留在外部采购 Agent（九段流水线），平台负责编排、人在环决策与产出回流。

## 第零步：依赖预检（必做）
正式开始任务前，先调 probe_url 探测外部采购 Agent 服务基址（如 {base}/health）：
- 可达 → 继续主线；
- 不可达 → 直接告知用户「依赖的外部采购 Agent 服务未启动/不可达」并停止，
  不要触发任何主线工具，不要臆造数据。

## 怎么干（主线 5 步）
1. 报价单接入与解析：调 procurement_trigger 异步触发外部解析（立即返回 task_id），
   随后用 procurement_status 轮询直到解析完成。
2. 语义映射与校验：解析完成后调 procurement_decisions 拉取待决策面。
3. 机械对比：外部流水线完成价差比对，用 procurement_status 跟踪进度。
4. AI 综合分析：比对完成后调 procurement_report 获取 AI 分析。
5. 生成对比报告：整理报告；产出可经知识库管道入库，供 search_knowledge 检索历史结论。

## 会遇到什么问题 + 如何处理（支线 4 类决策点）
外部流水线在以下情形产出待决策面（procurement_decisions 返回），需人工介入：
- 识别到未管理的供应商 → 启用子 Worker：sub_workers/确认新增供应商
- 出现无法识别的工艺 → 启用子 Worker：sub_workers/确认工艺处理方案
- 报价未关联项目 → 启用子 Worker：sub_workers/绑定项目
- 单据内部数据矛盾/勾稽异常 → 启用子 Worker：sub_workers/数据纠错
处理纪律：对每个待决策面，调 request_decision 抛交互决策卡（plugin=采购决策面板，
init_data 带决策面数据），用户在侧边栏选/删/改后结构化回传；拿到回传 data 后调
procurement_submit_decision 写回外部 Agent 落库，再继续主线。仅需一句文本澄清时改用 ask_user。

## 工具引用清单与能力路由表
- probe_url（read）：依赖预检（第零步，探测外部服务可达性）
- procurement_trigger（write）：触发解析
- procurement_status（read）：轮询进度
- procurement_decisions（read）：拉待决策面
- procurement_submit_decision（write）：回写决策结果
- procurement_report（read）：取对比报告 / AI 分析
- request_decision（元工具）：抛交互决策卡，开侧边栏（plugin=采购决策面板）
- ask_user（元工具）：纯文本澄清
- search_knowledge（read）：检索历史对比结论

## 注意
外部采购 Agent 不可用时，桥接工具返回友好提示而非报错；此时应告知用户服务未就绪，
不要臆造数据。
"""

# L3 引用资源：外部集成契约文档 + 承载侧边栏的 plugin（按需拉取，不预载入）
WORKER_REFERENCES: list[dict] = [
    {
        "kind": "rest_contract",
        "title": "采购 Agent 外部集成契约",
        "path": "docs/任务架构注册规范.md",
        "section": "采购 Agent 改造契约",
    },
    {"kind": "plugin", "title": PROCUREMENT_PLUGIN_NAME, "capability": PROCUREMENT_PLUGIN_NAME},
]

# 子任务：主线定 seq（planner 按骨架推进），支线 optional（按需触发）
STEP_TEMPLATES: list[dict] = [
    {
        "seq": 1,
        "name": "报价单接入与解析",
        "kind": "main",
        "description": "触发外部采购 Agent 解析报价单并轮询至完成。",
        "playbook": "调 procurement_trigger 异步触发（得 task_id）；再用 procurement_status 轮询，"
        "直到状态为解析完成。触发失败或服务不可用时如实告知用户，不臆造数据。",
        "capability_hint": ["procurement_trigger", "procurement_status"],
    },
    {
        "seq": 2,
        "name": "语义映射与校验",
        "kind": "main",
        "description": "拉取解析产出的待决策面，识别需人工确认的供应商/工艺/项目/数据。",
        "playbook": "调 procurement_decisions 取待决策面清单；对每个决策面按支线处理"
        "（见各支线子任务）。无待决策面则直接进入机械对比。",
        "capability_hint": ["procurement_decisions"],
    },
    {
        "seq": 3,
        "name": "机械对比",
        "kind": "main",
        "description": "外部流水线完成多供应商价差机械比对。",
        "playbook": "用 procurement_status 跟踪比对进度至完成；"
        "比对由外部 Agent 执行，平台不重复计算。",
        "capability_hint": ["procurement_status"],
    },
    {
        "seq": 4,
        "name": "AI 综合分析",
        "kind": "main",
        "description": "获取外部 Agent 的 AI 综合分析结论。",
        "playbook": "调 procurement_report 获取 AI 综合分析；"
        "可先 search_knowledge 检索历史对比结论作参照。",
        "capability_hint": ["procurement_report", "search_knowledge"],
    },
    {
        "seq": 5,
        "name": "生成对比报告",
        "kind": "main",
        "description": "整理并产出结构化对比报告。",
        "playbook": "调 procurement_report 取最终报告并整理呈现；"
        "提示用户产出可经知识库管道入库供跨任务复用。",
        "capability_hint": ["procurement_report"],
    },
    {
        "seq": 6,
        "name": "确认新增供应商",
        "kind": "branch",
        "optional": True,
        "description": "识别到未管理供应商时，由用户在侧边栏确认建档或合并到已有供应商。",
        "playbook": "调 request_decision（title=确认供应商建档，plugin=采购决策面板，"
        "init_data 带候选供应商）抛卡；用户回传后调 procurement_submit_decision 写回外部 Agent。",
        "capability_hint": ["request_decision", "procurement_submit_decision"],
    },
    {
        "seq": 7,
        "name": "确认工艺处理方案",
        "kind": "branch",
        "optional": True,
        "description": "出现无法识别的工艺时，由用户选择/修正工艺处理方案。",
        "playbook": "调 request_decision（title=确认工艺处理方案，plugin=采购决策面板，"
        "init_data 带工艺候选）抛卡；回传后调 procurement_submit_decision 写回。",
        "capability_hint": ["request_decision", "procurement_submit_decision"],
    },
    {
        "seq": 8,
        "name": "绑定项目",
        "kind": "branch",
        "optional": True,
        "description": "报价未关联项目时，由用户绑定到正确的项目。",
        "playbook": "调 request_decision（title=绑定项目，plugin=采购决策面板，"
        "init_data 带项目候选）抛卡；回传后调 procurement_submit_decision 写回。",
        "capability_hint": ["request_decision", "procurement_submit_decision"],
    },
    {
        "seq": 9,
        "name": "数据纠错",
        "kind": "branch",
        "optional": True,
        "description": "单据内部数据矛盾/勾稽异常时，由用户核对并纠正数据。",
        "playbook": "调 request_decision（title=数据纠错，severity=warn，plugin=采购决策面板，"
        "init_data 带异常明细）抛卡；回传后调 procurement_submit_decision 写回。",
        "capability_hint": ["request_decision", "procurement_submit_decision"],
    },
]


def _plugin_payload() -> dict:
    """采购决策面板 plugin 的前端清单（iframe 模式，与外部服务同源）。"""
    base = settings.procurement_agent_base_url.rstrip("/")
    parsed = urlparse(base)
    origin = f"{parsed.scheme}://{parsed.netloc}" if parsed.netloc else base
    return {
        "frontend": {
            "mode": "iframe",
            "url": f"{base}/worker/ui",
            "allowlist_origin": origin,
            "sandbox": "allow-scripts allow-forms allow-same-origin allow-popups",
        }
    }


async def _seed_plugin(db) -> uuid.UUID:
    """幂等登记采购决策面板 plugin（前端 URL 跟随配置刷新）。"""
    desc = (
        "采购报价对比 Agent 的决策前端（供应商建档/工艺确认/项目绑定/数据纠错四类决策页）。"
        "作为 plugin 前端由平台侧边栏以 iframe 加载，用户选/删/改后按统一契约结构化回传。"
    )
    cap = await db.scalar(select(Capability).where(Capability.name == PROCUREMENT_PLUGIN_NAME))
    if cap is None:
        cap = Capability(
            type="plugin",
            category="external",
            name=PROCUREMENT_PLUGIN_NAME,
            description=desc,
            risk_level="read",
            payload=_plugin_payload(),
            health_status="unknown",
        )
        db.add(cap)
    else:
        cap.description = desc
        cap.payload = _plugin_payload()
    await db.flush()
    await index_capability(db, cap)
    return cap.id


async def seed_procurement_worker() -> None:
    """幂等登记采购报价对比 Worker（应用启动时调用，晚于 builtin 能力 seed）。

    Worker 定义落文件包（data/workers/<名>/v1/，文件为唯一权威）：
    已存在时不覆盖——保护用户在文件管理器里的编辑（演进走「构建新版本」）。
    """
    async with session_factory() as db:
        await _seed_plugin(db)
        await db.commit()

    ensure_worker(
        PROCUREMENT_WORKER_NAME,
        description=(
            "对比多家供应商的报价单：机械比对价差 + AI 综合分析，产出对比报告。"
            "含供应商建档、工艺确认、项目绑定、数据纠错四类人工决策点。适用于采购询比价场景。"
        ),
        icon="📊",
        color="#1677ff",
        capabilities=[*BRIDGE_TOOL_NAMES, PROCUREMENT_PLUGIN_NAME],
        references=WORKER_REFERENCES,
        playbook=WORKER_PLAYBOOK,
        sub_workers=STEP_TEMPLATES,
    )
