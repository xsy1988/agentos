"""能力 payload 校验的纯逻辑单测（决策1：plugin 前端清单 §3.5）。

capabilities.service.validate_payload：按 type 校验 payload 结构。
本文件聚焦 plugin 的「前端清单 + 可选后端 transport」新规则，并回归 mcp/tool/skill。
"""

from app.modules.capabilities.service import validate_payload

# ---------- plugin：前端清单（展示类，可无后端） ----------


def test_plugin_iframe_frontend_only_ok() -> None:
    """纯前端 iframe plugin（无 transport）合法——展示类 plugin 是人在环 UI 载体。"""
    payload = {"frontend": {"mode": "iframe", "url": "https://purchase.local/decision"}}
    assert validate_payload("plugin", payload) is None


def test_plugin_server_driven_frontend_only_ok() -> None:
    """纯前端 server_driven plugin 合法（平台原生渲染 JSON UI schema）。"""
    payload = {"frontend": {"mode": "server_driven", "schema": {"type": "table"}}}
    assert validate_payload("plugin", payload) is None


def test_plugin_iframe_requires_http_url() -> None:
    """iframe 模式的 url 必须是 http(s)，否则拒绝（防注入本地文件/脚本）。"""
    payload = {"frontend": {"mode": "iframe", "url": "javascript:alert(1)"}}
    assert validate_payload("plugin", payload) is not None


def test_plugin_server_driven_requires_schema_dict() -> None:
    """server_driven 模式必须带 dict 形式的 schema。"""
    payload = {"frontend": {"mode": "server_driven"}}
    assert validate_payload("plugin", payload) is not None


def test_plugin_bad_mode_rejected() -> None:
    """未知 mode 拒绝（只认 iframe / server_driven）。"""
    payload = {"frontend": {"mode": "webview", "url": "https://x"}}
    assert validate_payload("plugin", payload) is not None


def test_plugin_needs_frontend_or_transport() -> None:
    """plugin 既无 frontend 又无 transport → 拒绝（空壳 plugin 无意义）。"""
    assert validate_payload("plugin", {}) is not None


def test_plugin_transport_only_ok() -> None:
    """纯后端 plugin（http transport，无前端）仍合法（复杂数据处理类）。"""
    payload = {"transport": "http", "url": "https://proc.local/mcp"}
    assert validate_payload("plugin", payload) is None


def test_plugin_frontend_plus_transport_ok() -> None:
    """前端 + 后端都有的 plugin 合法（既有处理管道又有决策页）。"""
    payload = {
        "frontend": {"mode": "iframe", "url": "https://x/decision"},
        "transport": "stdio",
        "command": "uvx",
        "args": ["some-plugin"],
    }
    assert validate_payload("plugin", payload) is None


# ---------- 回归：mcp / tool / skill 不受影响 ----------


def test_mcp_still_requires_transport() -> None:
    """mcp 不接受纯前端（它无前端页面），仍需 transport。"""
    assert validate_payload("mcp", {"frontend": {"mode": "iframe", "url": "https://x"}}) is not None
    assert validate_payload("mcp", {"transport": "http", "url": "https://x/mcp"}) is None


def test_tool_and_skill_unchanged() -> None:
    """tool/skill 校验规则保持原样。"""
    assert validate_payload("tool", {"schema": {"type": "object"}}) is None
    assert validate_payload("tool", {}) is not None
    skill_md = "---\nname: x\ndescription: y\n---\n\n正文"
    assert validate_payload("skill", {"skill_md": skill_md}) is None
    assert validate_payload("skill", {}) is not None
