"""LoopState —— 引擎的数据契约（M2-2a 冻结）。

对应模块详细设计 §1.1.2。签名冻结：节点/钩子/后端只能读写这里声明的键。
LangGraph 版本：langgraph==1.2.11（已实机验证 add_messages/interrupt/Command 写法）。

验证过的 API 写法备忘（与本文件配套使用）：
- aget_state 的 config 必须嵌套：{"configurable": {"thread_id": ...}}
- interrupt 载荷读取：snapshot.tasks[0].interrupts[0].value
- 恢复：await graph.ainvoke(Command(resume=value), {"configurable": {"thread_id": ...}})
"""

from typing import Annotated, Any

from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages
from typing_extensions import TypedDict


class BudgetState(TypedDict, total=False):
    """预算实耗计数（hooks 计量、budget 熔断共用的账本）。"""

    iterations: int  # 已完成迭代数
    input_tokens: int
    output_tokens: int
    tool_calls: int  # 工具调用总次数
    tool_failure_streak: int  # 连续工具失败计数（P0-3 熔断账本；成功即清零）
    loop_strikes: dict[str, int]  # 工具指纹 → 连续相同调用计数（死循环检测）


class Confirmation(TypedDict):
    """待确认的参数包（interrupt 暂停点与确认卡片的载荷契约）。"""

    reason: str  # plan_review | high_risk_tool
    payload: dict[str, Any]  # 展示给用户的参数/计划摘要
    resolved: bool
    answer: str | None  # 用户答复（approved / rejected / 修改后的参数）


class LoopState(TypedDict, total=False):
    """图状态。total=False：各节点增量返回，checkpoint 全量保存。"""

    # 可压缩区：对话与工具观察结果（add_messages 自动追加/合并）
    messages: Annotated[list[AnyMessage], add_messages]
    # 永不压缩区：人格/画像/活跃计划/平台记忆（assembler 装配产物）
    protected_context: dict[str, Any]
    # 本轮已装配的工具与技能，含来源（pinned/semantic）
    capability_cache: dict[str, Any]
    # 计划外置：plans 表 id（uuid 字符串）。计划在库里，不在上下文
    plan_ref: str | None
    # 预算账本
    budget_state: BudgetState
    # 待确认参数包；None = 无确认请求
    confirmation: Confirmation | None
