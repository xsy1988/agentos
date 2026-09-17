"""now_datetime 工具 + 农历转换算法单测（纯逻辑，不依赖 DB/LLM）。

- 农历表/算法锚点：春节、中秋、闰月（经 lunardate 全量 72654 天比对校验后固化）
- 执行器：BUILTIN_TOOLS 键与 BUILTIN_SEED 种子一一对应；输出格式契约
"""

import asyncio
import re
from datetime import date, datetime

import pytest

from app.modules.capabilities.service import BUILTIN_SEED
from app.modules.engine.lunar import _day_name, _solar_to_lunar, format_datetime
from app.modules.engine.tools_builtin import BUILTIN_TOOLS, _now_datetime

# ---------- 农历转换锚点（与 lunardate 逐日比对校验过的样本） ----------

ANCHORS: list[tuple[date, tuple[int, int, int, bool]]] = [
    (date(1900, 1, 31), (1900, 1, 1, False)),  # 基准日
    (date(2024, 2, 10), (2024, 1, 1, False)),  # 2024 春节
    (date(2025, 1, 29), (2025, 1, 1, False)),  # 2025 春节
    (date(2026, 2, 17), (2026, 1, 1, False)),  # 2026 春节
    (date(2023, 3, 22), (2023, 2, 1, True)),  # 2023 闰二月初一
    (date(2025, 10, 6), (2025, 8, 15, False)),  # 2025 中秋（闰六月年）
    (date(2026, 9, 25), (2026, 8, 15, False)),  # 2026 中秋
    (date(2098, 12, 31), (2098, 12, 10, False)),  # 表末尾（2098 腊月初十）
    (date(2099, 1, 1), (2098, 12, 11, False)),  # 2098 腊月延伸进 2099 公历年
]


@pytest.mark.parametrize(("d", "expect"), ANCHORS)
def test_solar_to_lunar_anchors(d: date, expect: tuple[int, int, int, bool]) -> None:
    assert _solar_to_lunar(d) == expect


def test_out_of_range_rejected() -> None:
    with pytest.raises(ValueError):
        _solar_to_lunar(date(1899, 12, 31))
    with pytest.raises(ValueError):
        _solar_to_lunar(date(2099, 2, 1))  # 2098 农历年结束之后


def test_day_name() -> None:
    assert _day_name(1) == "初一"
    assert _day_name(10) == "初十"
    assert _day_name(15) == "十五"
    assert _day_name(20) == "二十"
    assert _day_name(21) == "廿一"
    assert _day_name(30) == "三十"


# ---------- 输出格式契约 ----------


def test_format_datetime_contract() -> None:
    out = format_datetime(datetime(2026, 9, 16, 14, 23, 45))
    # 【农历】干支年（中文数字年）X月X日 时:分:秒；【公历】年-月-日 时:分:秒；星期X
    assert re.fullmatch(
        r"【农历】.+?年（.+?）.+?月.+? \d{2}:\d{2}:\d{2}；"
        r"【公历】\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}；"
        r"星期[一二三四五六日]",
        out,
    )
    assert "丙午年" in out  # 2026 = 丙午
    assert "2026-09-16 14:23:45" in out
    assert "星期三" in out


# ---------- 工具执行器 + 种子一致性 ----------


def test_now_datetime_executor() -> None:
    out = asyncio.run(_now_datetime({}))
    assert out.startswith("【农历】")
    assert "【公历】" in out
    assert "星期" in out


def test_builtin_seed_covers_executor() -> None:
    """BUILTIN_SEED 的每个 tool 项必须有对应执行器（now_datetime 不例外）。"""
    seed_tools = {i["name"] for i in BUILTIN_SEED}
    assert "now_datetime" in seed_tools
    assert "now_datetime" in BUILTIN_TOOLS
    for i in BUILTIN_SEED:
        assert i["name"] in BUILTIN_TOOLS, f"种子 {i['name']} 缺执行器"
