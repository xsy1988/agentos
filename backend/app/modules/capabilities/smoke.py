"""注册冒烟测试执行器（模块详细设计 §1.3.2）。

三分支：
- tool：builtin 直接调函数比对返回值（external tool 无进程外执行器，只做结构校验）
- mcp/plugin：临时拉起连接 → list_tools（同步 capability_tools）→ 各启用工具试调用
- skill：yaml 头完整性（validate_payload 已做）+ 引用脚本存在性（M3 简化跳过）

通过才 enabled（service.create_capability 决定），失败附错误报告。
"""

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.capabilities.schemas import CapabilitySmokeReport
from app.modules.engine.tools_builtin import BUILTIN_TOOLS

SMOKE_TIMEOUT = 30  # 单用例超时（秒）


def _exception_detail(e: BaseException) -> str:
    """展开 ExceptionGroup（anyio TaskGroup 包裹的真实异常）。"""
    parts = [f"{type(e).__name__}: {e}"]
    for sub in getattr(e, "exceptions", None) or []:
        parts.append(f"  └ {type(sub).__name__}: {sub}")
        for sub2 in getattr(sub, "exceptions", None) or []:
            parts.append(f"      └ {type(sub2).__name__}: {sub2}")
    return "\n".join(parts)


def _match(actual: str, expected: str | None) -> bool:
    """宽松比对：expected 为空只看是否执行成功；否则要求包含或相等。"""
    if not expected:
        return True
    return expected in actual or expected == actual


def _result_text(result: Any) -> str:
    parts = []
    for block in getattr(result, "content", None) or []:
        text_part = getattr(block, "text", None)
        if text_part:
            parts.append(str(text_part))
    return "\n".join(parts) or "(空结果)"


async def run_smoke_test(db: AsyncSession, cap: Any) -> CapabilitySmokeReport:
    """执行注册冒烟。cap 为 capabilities ORM 行（已在 session 中）。"""
    import asyncio

    checks: list[dict[str, Any]] = []
    payload = cap.payload or {}
    test_info = cap.test_info or []

    # ---------- tool ----------
    if cap.type == "tool":
        builtin_key = payload.get("builtin")
        if builtin_key not in BUILTIN_TOOLS:
            checks.append(
                {
                    "name": "tool 结构校验",
                    "ok": True,
                    "detail": "external tool：无进程内执行器，仅结构校验（已通过）",
                }
            )
            return _report(checks)
        fn = BUILTIN_TOOLS[builtin_key]
        if not test_info:
            checks.append({"name": "连通", "ok": True, "detail": "无 test_info 用例，跳过执行比对"})
            return _report(checks)
        for i, case in enumerate(test_info):
            name = f"用例{i + 1}: {json_brief(case.get('input'))}"
            try:
                actual = await asyncio.wait_for(fn(dict(case.get("input") or {})), SMOKE_TIMEOUT)
                ok = _match(actual, case.get("expected"))
                checks.append({"name": name, "ok": ok, "detail": f"返回: {actual[:200]}"})
            except Exception as e:  # noqa: BLE001 —— 冒烟报告记录一切失败
                checks.append({"name": name, "ok": False, "detail": _exception_detail(e)})
        return _report(checks)

    # ---------- mcp / plugin ----------
    if cap.type in ("mcp", "plugin"):
        from app.modules.capabilities.mcp_client import open_mcp_session
        from app.modules.capabilities.models import CapabilityTool

        try:
            async with asyncio.timeout(SMOKE_TIMEOUT + 30):
                async with open_mcp_session(payload) as session:
                    resp = await session.list_tools()
                    tools = list(resp.tools)
                    tool_names = [t.name for t in tools[:10]]
                    checks.append(
                        {
                            "name": "list_tools",
                            "ok": True,
                            "detail": f"{len(tools)} 个工具: {', '.join(tool_names)}",
                        }
                    )
                    # 工具清单同步进 capability_tools（新工具默认 enabled）
                    existing_names = {
                        t.tool_name for t in (await db.scalars(_select_tools(cap.id)))
                    }
                    for t in tools:
                        if t.name in existing_names:
                            continue
                        db.add(
                            CapabilityTool(
                                capability_id=cap.id,
                                tool_name=t.name,
                                description=t.description or "",
                                enabled=True,
                            )
                        )
                    await db.flush()
                    # 各启用工具试调用（test_info 指定 tool+input+expected）
                    for i, case in enumerate(test_info):
                        tool_name = str(case.get("tool") or "")
                        name = f"用例{i + 1}: {tool_name}"
                        if tool_name not in {t.name for t in tools}:
                            checks.append(
                                {"name": name, "ok": False, "detail": "工具不存在于 Server"}
                            )
                            continue
                        try:
                            result = await session.call_tool(
                                tool_name, dict(case.get("input") or {})
                            )
                            actual = _result_text(result)
                            ok = (not result.isError) and _match(actual, case.get("expected"))
                            checks.append(
                                {"name": name, "ok": ok, "detail": f"返回: {actual[:200]}"}
                            )
                        except Exception as e:  # noqa: BLE001
                            checks.append(
                                {"name": name, "ok": False, "detail": f"{type(e).__name__}: {e}"}
                            )
        except Exception as e:  # noqa: BLE001 —— 连接失败即冒烟失败
            checks.append({"name": "连接 Server", "ok": False, "detail": _exception_detail(e)})
        return _report(checks)

    # ---------- skill ----------
    if cap.type == "skill":
        checks.append(
            {
                "name": "yaml 头校验",
                "ok": True,
                "detail": "name/description 完整（validate_payload 已过）",
            }
        )
        return _report(checks)

    checks.append({"name": "类型", "ok": False, "detail": f"未知类型: {cap.type}"})
    return _report(checks)


def _select_tools(cap_id: Any):
    from sqlalchemy import select

    from app.modules.capabilities.models import CapabilityTool

    return select(CapabilityTool).where(CapabilityTool.capability_id == cap_id)


def _report(checks: list[dict[str, Any]]) -> CapabilitySmokeReport:
    passed = all(c["ok"] for c in checks)
    failed = [c for c in checks if not c["ok"]]
    summary = (
        f"全部 {len(checks)} 项通过"
        if passed
        else f"{len(failed)}/{len(checks)} 项失败: "
        + "; ".join(f"{c['name']}→{c['detail'][:300]}" for c in failed[:3])
    )
    return CapabilitySmokeReport(passed=passed, checks=checks, summary=summary)


def json_brief(obj: Any) -> str:
    import json

    return json.dumps(obj, ensure_ascii=False)[:60]
