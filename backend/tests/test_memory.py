"""memory / scheduler 纯逻辑单测（M5）。"""

from datetime import UTC, datetime

from app.modules.memory.service import _est_tokens, _parse_products
from app.modules.scheduler.service import render_input


class TestParseProducts:
    """LLM 双产物输出解析（===PLATFORM=== / ===DAILY=== 分隔）。"""

    def test_both_sections(self) -> None:
        reply = (
            "前置废话\n===PLATFORM===\n- 工具A 故障已恢复\n===DAILY===\n- 用户偏好简洁回答"
        )
        plat, daily = _parse_products(reply)
        assert plat == "- 工具A 故障已恢复"
        assert daily == "- 用户偏好简洁回答"

    def test_platform_only(self) -> None:
        reply = "===PLATFORM===\n- 平台事实\n"
        plat, daily = _parse_products(reply)
        assert plat == "- 平台事实"
        assert daily == "无"

    def test_daily_only(self) -> None:
        plat, daily = _parse_products("===DAILY===\n- 用户事实")
        assert plat == "无"
        assert daily == "- 用户事实"

    def test_empty_sections_fallback_wu(self) -> None:
        reply = "===PLATFORM===\n\n===DAILY===\n  \n"
        plat, daily = _parse_products(reply)
        assert plat == "无"
        assert daily == "无"

    def test_no_markers(self) -> None:
        plat, daily = _parse_products("模型输出跑题了，没有分隔符")
        assert plat == "无"
        assert daily == "无"


class TestRenderInput:
    """input_template 变量实例化（{date}/{time}/{now}）。"""

    def test_variable_substitution(self) -> None:
        now = datetime(2026, 8, 24, 3, 5, tzinfo=UTC)
        out = render_input({"text": "现在 {date} {time}，完整 {now}"}, now)
        assert out["text"] == "现在 2026-08-24 03:05，完整 2026-08-24 03:05"

    def test_non_string_values_untouched(self) -> None:
        out = render_input({"k": 1, "b": True, "nested": {"x": "{date}"}}, datetime.now(UTC))
        assert out["k"] == 1 and out["b"] is True
        assert out["nested"]["x"] == "{date}"  # 只展开顶层字符串

    def test_empty_template(self) -> None:
        assert render_input(None) == {}
        assert render_input({}) == {}


def test_est_tokens_chinese_rough() -> None:
    # 中文粗估 chars/2，至少 1
    assert _est_tokens("四字四字") == 2
    assert _est_tokens("") == 1
