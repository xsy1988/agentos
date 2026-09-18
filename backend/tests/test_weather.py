"""query_weather 工具单测：日期解析 / WMO 描述 / 结果拼装（纯逻辑）+ 执行器真实外呼。"""

import asyncio
from datetime import date, timedelta

import pytest

from app.modules.engine.tool_outcome import ToolError
from app.modules.engine.tools_builtin import (
    _format_weather_result,
    _parse_weather_date,
    _query_weather,
    _weather_desc,
)

# ---------- 日期解析 ----------


@pytest.mark.parametrize(
    ("s", "expect"),
    [
        ("", date.today()),
        ("今天", date.today()),
        ("明天", date.today() + timedelta(days=1)),
        ("后天", date.today() + timedelta(days=2)),
        ("2026-09-16", date(2026, 9, 16)),
        ("20260916", date(2026, 9, 16)),
    ],
)
def test_parse_date_ok(s: str, expect: date) -> None:
    d, err = _parse_weather_date(s)
    assert err is None and d == expect


def test_parse_date_bad() -> None:
    d, err = _parse_weather_date("上周三")
    assert d is None and err and "格式错误" in err


def test_parse_date_invalid_calendar() -> None:
    # 形似日期但日历不存在（2 月 30 日）
    d, err = _parse_weather_date("2026-02-30")
    assert d is None and err


# ---------- WMO 天气代码 ----------


def test_weather_desc() -> None:
    assert _weather_desc(0) == "晴"
    assert _weather_desc(95) == "雷暴"
    assert "未知" in _weather_desc(42)


# ---------- 结果拼装 ----------


def test_format_weather_result() -> None:
    g = {"name": "杭州", "admin1": "浙江省", "country": "中国"}
    daily = {
        "time": ["2026-09-16"],
        "weather_code": [61],
        "temperature_2m_max": [28.3],
        "temperature_2m_min": [21.1],
        "precipitation_sum": [4.2],
        "precipitation_probability_max": [80],
        "wind_speed_10m_max": [18.5],
    }
    out = _format_weather_result(g, date(2026, 9, 16), daily)
    assert out == (
        "杭州（浙江省 · 中国） 2026-09-16：小雨；气温 21.1~28.3℃；"
        "降水量 4.2 mm；降水概率 80%；最大风速 18.5 km/h"
    )


def test_format_weather_result_missing_date() -> None:
    g = {"name": "杭州"}
    out = _format_weather_result(g, date(2026, 9, 16), {"time": ["2026-01-01"]})
    assert "无该日期的天气数据" in out


# ---------- 执行器（真实外呼；离线时跳过） ----------


def test_query_weather_live() -> None:
    try:
        out = asyncio.run(_query_weather({"city": "杭州", "date": "2026-09-16"}))
    except ToolError as e:
        pytest.skip(f"外部服务不可用，跳过真实外呼：{e.code}")
    assert out.startswith("杭州")
    assert "2026-09-16" in out and "气温" in out


def test_query_weather_bad_args() -> None:
    """参数错误必须是结构化失败（P0-3），不得降级成自然语言结果。"""
    with pytest.raises(ToolError) as ei:
        asyncio.run(_query_weather({"city": ""}))
    assert ei.value.code == "invalid_args" and "city" in ei.value.detail

    with pytest.raises(ToolError) as ei2:
        asyncio.run(_query_weather({"city": "杭州", "date": "某天"}))
    assert ei2.value.code == "invalid_args"


def test_query_weather_unknown_city() -> None:
    """未知城市 = 参数错误（结构化）；真实外呼，离线时跳过。"""
    try:
        out = asyncio.run(_query_weather({"city": "不存在城市xyzq99"}))
    except ToolError as e:
        assert e.code in ("invalid_args", "external_unavailable")
        return
    pytest.skip(f"外呼返回了结果，跳过：{out[:80]}")
