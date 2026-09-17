"""自建网页搜索服务 seed 的纯逻辑单测（不碰 DB、不发网络请求）。

落库与探测需要真库/真服务，留给 DoD 手工验证；这里锁住**契约面**——
seed 写出去的 payload 必须过平台自己的校验器、冒烟用例必须与服务侧工具名对得上、
description 必须覆盖语义发现要命中的那些真实表述。这些是最容易在改动中被无声破坏的部分。
"""

from app.core.config import settings
from app.modules.capabilities.service import validate_payload
from app.modules.capabilities.websearch_seed import (
    DESCRIPTION,
    TEST_INFO,
    WEBSEARCH_CAPABILITY_NAME,
    _payload,
)

# 服务侧 mcp_server.py 暴露的工具面（刻意收敛到 3 个：tool_budget 默认 8，每个工具都占位）
SERVER_TOOLS = {"web_search", "web_fetch", "search_meta"}


def test_payload_passes_platform_validator() -> None:
    """seed 写的 payload 必须能过 validate_payload("mcp", …)，否则人工编辑一次就 422。"""
    assert validate_payload("mcp", _payload()) is None


def test_payload_is_streamable_http_and_follows_settings() -> None:
    """transport=http + url 取自配置（SEARCH_MCP_URL），路径必须落在 /mcp。"""
    payload = _payload()
    assert payload["transport"] == "http"
    assert payload["url"] == settings.search_mcp_url
    assert payload["url"].endswith("/mcp")
    # 来源标记：seed 靠它判断「这条还归我管吗」，人工接管后不再自动拨开关
    assert payload["managed_by"] == "websearch_seed"


def test_capability_name_matches_mcp_tool_prefix() -> None:
    """能力名决定平台暴露名 mcp__<name>__<tool>；改名会让已存盘的 run 历史对不上。"""
    assert WEBSEARCH_CAPABILITY_NAME == "websearch"


def test_test_info_only_uses_real_tools() -> None:
    """冒烟用例引用的工具必须存在于服务侧工具面，否则冒烟必红（smoke 判「工具不存在」）。"""
    for case in TEST_INFO:
        assert case["tool"] in SERVER_TOOLS


def test_test_info_has_no_brittle_expectations() -> None:
    """expected 一律留空 = 只要求执行不报错。搜索结果天天变，写死断言逢跑必红。"""
    assert all(case["expected"] == "" for case in TEST_INFO)


def test_test_info_smoke_case_is_fast_enough() -> None:
    """web_search 用例必须关掉抽取：冒烟单用例上限 30s，冷启动抓正文 + 片段精排会顶到线上。"""
    search_cases = [c for c in TEST_INFO if c["tool"] == "web_search"]
    assert len(search_cases) == 1
    assert search_cases[0]["input"]["extract"] is False
    assert search_cases[0]["input"]["max_results"] <= 3


def test_description_covers_discovery_phrases() -> None:
    """description 是语义发现的检索文本，用户真会说的表述必须在里面。"""
    for phrase in ("联网搜索", "查资料", "最新信息", "官方文档", "去重"):
        assert phrase in DESCRIPTION, f"description 缺少语义命中关键词：{phrase}"


def test_description_mentions_zero_cost_and_self_hosted() -> None:
    """自托管 + 零按次成本是这个能力的定位，也是排障时判断「该不该用它」的依据。"""
    assert "自托管" in DESCRIPTION
    assert "零按次查询成本" in DESCRIPTION
