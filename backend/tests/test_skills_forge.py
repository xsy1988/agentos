"""skills_forge 纯逻辑单测（M6）。"""

from app.modules.skills_forge.service import _parse_reply, _parse_skill_header


class TestParseReply:
    """三问输出解析（===REUSABLE===/===COVERED===/===DRAFT===）。"""

    def test_full_output(self) -> None:
        reply = (
            "===REUSABLE===\n是，此流程可复用。\n"
            "===COVERED===\n否，现有工具不能直接覆盖。\n"
            "===DRAFT===\n---\nname: my-skill\ndescription: 用途\n---\n# 步骤\n1. ..."
        )
        reusable, not_covered, draft = _parse_reply(reply)
        assert reusable is True
        assert not_covered is True
        assert "my-skill" in draft

    def test_not_reusable(self) -> None:
        reply = "===REUSABLE===\n否，单次任务\n===COVERED===\n是\n===DRAFT===\n无"
        reusable, not_covered, draft = _parse_reply(reply)
        assert reusable is False
        assert not_covered is False
        assert draft == "无"

    def test_missing_markers(self) -> None:
        reusable, not_covered, draft = _parse_reply("模型跑题")
        assert reusable is False
        assert not_covered is False
        assert draft == ""


class TestParseSkillHeader:
    """SKILL.md yaml 头解析（approve 转能力时用）。"""

    def test_valid_header(self) -> None:
        md = "---\nname: my-skill\ndescription: 测试用途\n---\n# 正文"
        h = _parse_skill_header(md)
        assert h is not None
        assert h["name"] == "my-skill"
        assert h["description"] == "测试用途"

    def test_missing_name(self) -> None:
        md = "---\ndescription: 缺名字\n---\n# 正文"
        assert _parse_skill_header(md) is None

    def test_no_front_matter(self) -> None:
        assert _parse_skill_header("# 没有 yaml 头") is None

    def test_broken_yaml(self) -> None:
        md = "---\n: : :\n---\n# 正文"
        assert _parse_skill_header(md) is None
