"""M3 discovery/assembler 纯逻辑单测（不依赖 DB / LLM）。

- graph._normalize_tool_responses：悬空 tool_calls 补齐 + 响应位置规范化
  （abort 于 tools interrupt 后 thread 遗留悬空调用的 400 防御）
- assembler.local_view：L1/L2 压缩操作的内存归约视图
"""

from langchain_core.messages import AIMessage, HumanMessage, RemoveMessage, ToolMessage

from app.modules.discovery.assembler import (
    REQUEST_DECISION_SCHEMA,
    _meta_tools,
    local_view,
)
from app.modules.engine.graph import _normalize_tool_responses


def _ai(tool_calls: list[dict], **kw: object) -> AIMessage:
    return AIMessage(content=str(kw.get("content", "")), tool_calls=tool_calls)


def test_normalize_patches_dangling_tool_calls() -> None:
    """悬空 tool_calls（abort 遗留）：补 [aborted] 空响应，紧贴 AIMessage。"""
    dangling = _ai([{"name": "t", "args": {}, "id": "call-1"}], content="")
    msgs = [HumanMessage(content="任务"), dangling]
    out, patches = _normalize_tool_responses(msgs)
    assert len(patches) == 1
    assert patches[0].tool_call_id == "call-1"
    assert [type(m) for m in out] == [HumanMessage, AIMessage, ToolMessage]
    assert out[2] is patches[0]  # 响应紧贴 AIMessage


def test_normalize_moves_detached_response() -> None:
    """补丁响应被 add_messages append 到末尾：发送视图重新吸附到正确位置。"""
    ai = _ai([{"name": "t", "args": {}, "id": "call-1"}], content="")
    late_patch = ToolMessage(content="[aborted] ...", tool_call_id="call-1", id="p1")
    human = HumanMessage(content="新消息")
    # checkpoint 实际顺序：AI(悬空) → human → 补丁（末尾）
    out, patches = _normalize_tool_responses([ai, human, late_patch])
    assert patches == []  # 已有响应，不再新增补丁
    assert [type(m) for m in out] == [AIMessage, ToolMessage, HumanMessage]
    assert out[1] is late_patch  # 吸附到 AIMessage 之后


def test_normalize_keeps_normal_history() -> None:
    """正常历史（AI→Tool 紧邻）零改动、零补丁。"""
    ai = _ai([{"name": "t", "args": {}, "id": "call-1"}], content="")
    tm = ToolMessage(content="ok", tool_call_id="call-1", id="r1")
    human = HumanMessage(content="hi")
    out, patches = _normalize_tool_responses([human, ai, tm])
    assert patches == []
    assert out == [human, ai, tm]


def test_local_view_applies_remove_and_append() -> None:
    """L1/L2 操作的内存归约：RemoveMessage 删除 + 新消息追加 + 同 id 替换。"""
    m1 = HumanMessage(content="a", id="1")
    m2 = HumanMessage(content="b", id="2")
    m2p = HumanMessage(content="b2", id="2")  # 同 id 替换
    m3 = HumanMessage(content="c", id="3")
    ops = [RemoveMessage(id="3"), m2p, HumanMessage(content="new")]
    view = local_view([m1, m2, m3], ops)
    assert [(m.id, m.content) for m in view] == [("1", "a"), ("2", "b2"), (None, "new")]


def test_request_decision_is_resident_meta_tool() -> None:
    """request_decision（决策2/§3.6）作为常驻元工具注册，不占 tool_budget。"""
    names = {t["name"] for t in _meta_tools()}
    assert {"search_more_tools", "ask_user", "declare_subtask", "request_decision"} <= names
    rd = next(t for t in _meta_tools() if t["name"] == "request_decision")
    assert rd["kind"] == "meta"
    assert rd["risk_level"] == "read"
    assert rd["schema"] is REQUEST_DECISION_SCHEMA


def test_request_decision_schema_shape() -> None:
    """schema 契约：title 必填，携带 summary/severity/body/plugin/init_data。"""
    fn = REQUEST_DECISION_SCHEMA["function"]
    assert fn["name"] == "request_decision"
    props = fn["parameters"]["properties"]
    assert fn["parameters"]["required"] == ["title"]
    assert props["severity"]["enum"] == ["info", "warn", "danger"]
    assert {"title", "summary", "severity", "body", "plugin", "init_data"} <= set(props)
