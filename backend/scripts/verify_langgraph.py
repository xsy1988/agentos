"""2a 接口冻结前的实机验证：LangGraph interrupt + AsyncPostgresSaver。

验证链路：建图 → 真库检查点 → interrupt 暂停 → Command(resume=...) 恢复 → 状态延续。
跑通即证明 M2 最小闭环的核心写法成立；通过后本脚本删除。
"""

import asyncio
import uuid
from typing import Annotated

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.types import Command, interrupt
from typing_extensions import TypedDict

DB = "postgresql://agent:agent_dev_password@localhost:5432/agent_platform"


class St(TypedDict):
    messages: Annotated[list, add_messages]
    confirmed: bool


def ask_node(state: St) -> dict:
    answer = interrupt({"question": "确认执行？", "n_messages": len(state["messages"])})
    return {"confirmed": answer == "yes"}


def final_node(state: St) -> dict:
    return {"messages": [{"role": "assistant", "content": f"confirmed={state['confirmed']}"}]}


def build(saver):
    g = StateGraph(St)
    g.add_node("ask", ask_node)
    g.add_node("final", final_node)
    g.add_edge(START, "ask")
    g.add_edge("ask", "final")
    g.add_edge("final", END)
    return g.compile(checkpointer=saver)


async def main() -> None:
    async with AsyncPostgresSaver.from_conn_string(DB) as saver:
        await saver.setup()  # 建框架表（checkpoints 三件套）
        graph = build(saver)
        tid = str(uuid.uuid4())

        # 第一轮：应停在 interrupt
        await graph.ainvoke(
            {"messages": [{"role": "user", "content": "hi"}]}, {"configurable": {"thread_id": tid}}
        )
        snap = await graph.aget_state({"configurable": {"thread_id": tid}})
        assert snap.next == ("ask",), f"应停在 ask 节点, 实际 {snap.next}"
        print("1) interrupt 停在 ask 节点 ✓；interrupt 数据:", snap.tasks[0].interrupts[0].value)

        # 第二轮：恢复
        r2 = await graph.ainvoke(Command(resume="yes"), {"configurable": {"thread_id": tid}})
        assert r2["confirmed"] is True
        assert r2["messages"][-1].content == "confirmed=True"
        print("2) Command(resume) 恢复并跑完 ✓")

        # 第三轮：同 thread 新消息，验证历史延续
        await graph.ainvoke(
            {"messages": [{"role": "user", "content": "again"}]},
            {"configurable": {"thread_id": tid}},
        )
        snap3 = await graph.aget_state({"configurable": {"thread_id": tid}})
        assert snap3.next == ("ask",), "第二次同样停在 ask"
        msgs = snap3.values["messages"]
        print("3) 同 thread 历史延续 ✓，累计消息数:", len(msgs))  # noqa: E501
        print("\n全部验证通过")


if __name__ == "__main__":
    asyncio.run(main())
