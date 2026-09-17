"""工具返回契约与失败分类（方案 §4 P0-3）。

背景（假完成根因）：工具失败过去被翻译成一句自然语言塞回模型（如"服务不可用，
请稍后重试"），模型无法区分"外部服务失败"与"业务结果就是这个字符串"，
于是把失败当结果写进终答。

契约（唯一真源，图节点按此判定 `ok`，禁止硬编码）：

    {"ok": true,  "content": <any>}
    {"ok": false, "error": {"code": str, "detail": str, "retryable": bool, "source": str}}

纪律：
- 工具实现可以直接返回裸字符串/字典（兼容层 `normalize` 视为成功）；失败必须走
  `ToolError` 抛出或返回失败契约，**不得**在 `except` 里产出面向模型的自然语言"结果"。
- `code` 取自固定枚举（`TOOL_FAILURE_CODES`），自由文本只允许出现在 `detail`。
- 本模块为纯函数：不碰 DB、不碰 I/O，便于单测直接覆盖。
"""

from typing import Any

# 失败分类码表（禁止自由文本；前端/统计按此收敛文案）
TOOL_FAILURE_CODES: tuple[str, ...] = (
    "external_unavailable",  # 连接失败 / 超时
    "external_rejected",  # 对端返回业务拒绝（4xx / 业务码）
    "invalid_args",  # 本地入参校验失败
    "parse_error",  # 响应不可解析（JSON / schema）
    "internal_error",  # 平台内部异常
    "denied_by_user",  # 用户拒绝执行（高危确认点拒绝）
    # P0-4 外部等待：等待落定失败对模型同样是"这不是业务结果"
    "await_expired",  # 等待超时，外部流程未回传
    "await_cancelled",  # 等待被撤销（run 收尾 / 人工撤销）
    "await_unresolved",  # 等待行状态异常（理论上不可达的兜底）
)

# 默认是否可重试（调用方可显式覆盖）
RETRYABLE_BY_CODE: dict[str, bool] = {
    "external_unavailable": True,
    "external_rejected": False,
    "invalid_args": False,
    "parse_error": True,
    "internal_error": False,
    "denied_by_user": False,
    # 超时/撤销都不自动重试：重试意味着重新派发外部流程，须由模型显式决定
    "await_expired": False,
    "await_cancelled": False,
    "await_unresolved": False,
}

# error.source 取值：失败发生的通道/归属（前端与统计按此归类）
# builtin=本进程工具自身；mcp=外部 MCP 服务；external=工具直连的第三方 HTTP；engine=平台引擎
TOOL_SOURCES: tuple[str, ...] = ("builtin", "mcp", "external", "engine")

# 计入"连续失败熔断"的失败码（对端/解析类抖动才熔断；入参错由模型自纠）
STREAK_CODES: frozenset[str] = frozenset({"external_unavailable", "parse_error"})


class ToolError(Exception):
    """结构化工具失败：图节点据此生成失败契约，不再降级为自然语言结果。"""

    def __init__(
        self,
        code: str,
        detail: str,
        *,
        retryable: bool | None = None,
        source: str = "builtin",
        tool: str | None = None,
    ) -> None:
        if code not in TOOL_FAILURE_CODES:
            raise ValueError(f"未登记的失败码：{code}")
        if source not in TOOL_SOURCES:
            raise ValueError(f"未登记的 source：{source}")
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail
        self.retryable = RETRYABLE_BY_CODE[code] if retryable is None else retryable
        self.source = source
        self.tool = tool

    def as_error(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "detail": self.detail[:500],
            "retryable": self.retryable,
            "source": self.source,
        }


def failure_error(
    code: str, detail: str, *, retryable: bool | None = None, source: str = "builtin"
) -> dict[str, Any]:
    """构造 error 段（不经异常时用，如未知工具）。"""
    if code not in TOOL_FAILURE_CODES:
        raise ValueError(f"未登记的失败码：{code}")
    if source not in TOOL_SOURCES:
        raise ValueError(f"未登记的 source：{source}")
    return {
        "code": code,
        "detail": detail[:500],
        "retryable": RETRYABLE_BY_CODE[code] if retryable is None else retryable,
        "source": source,
    }


def success(content: Any) -> dict[str, Any]:
    return {"ok": True, "content": content}


def failure(
    code: str,
    detail: str,
    *,
    retryable: bool | None = None,
    source: str = "builtin",
) -> dict[str, Any]:
    return {"ok": False, "error": failure_error(code, detail, retryable=retryable, source=source)}


def normalize(raw: Any) -> tuple[Any, bool, dict[str, Any] | None]:
    """适配层：契约返回值 / 遗留裸返回值 → `(content, ok, error)`。

    遗留 builtin 返回裸字符串或字典（无 `ok` 键）→ 视为成功，先跑起来再逐函数迁移
    （方案 §4 P0-3 风险处置）；适配层不做任何异常吞并。
    """
    if isinstance(raw, dict) and "ok" in raw:
        ok = bool(raw.get("ok"))
        if ok:
            return raw.get("content"), True, None
        error = raw.get("error")
        if not isinstance(error, dict) or "code" not in error:
            # 契约不完整：按内部错误处理，绝不当作成功
            return None, False, failure_error("internal_error", "工具返回的失败契约缺少 error.code")
        error = {
            "detail": "",
            "retryable": RETRYABLE_BY_CODE.get(str(error["code"]), False),
            "source": "builtin",
            **error,
        }
        return None, False, error
    return raw, True, None


def classify_exception(e: BaseException, *, source: str = "builtin") -> ToolError:
    """执行期异常 → 结构化失败码（禁止把异常文本当结果返回）。"""
    import json

    detail = f"{type(e).__name__}: {e}"

    if isinstance(e, json.JSONDecodeError | KeyError | ValueError):
        return ToolError("parse_error", detail, source=source)
    if isinstance(e, TimeoutError | ConnectionError | OSError):
        return ToolError("external_unavailable", detail, source=source)

    import httpx

    if isinstance(e, httpx.HTTPStatusError):
        return http_failure(e.response.status_code, detail, source=source)
    if isinstance(e, httpx.HTTPError):
        return ToolError("external_unavailable", detail, source=source)
    return ToolError("internal_error", detail, source=source)


def http_failure(
    status_code: int, detail: str, *, source: str = "external", tool: str | None = None
) -> ToolError:
    """HTTP 状态码 → 失败码：5xx/429 视为对端暂时不可用，其余 4xx 视为业务拒绝。"""
    code = (
        "external_unavailable"
        if status_code >= 500 or status_code == 429
        else "external_rejected"
    )
    return ToolError(code, detail, source=source, tool=tool)


def next_failure_streak(streak: int, code: str | None) -> int:
    """连续失败计数：只累计 `STREAK_CODES`；其它失败或成功都打断连续性。"""
    if code and code in STREAK_CODES:
        return int(streak) + 1
    return 0


def failure_content(tool: str, error: dict[str, Any]) -> str:
    """注入模型的 ToolMessage 载荷：结构化失败 + 固定提示（禁止裸自然语言）。"""
    import json

    return json.dumps(
        {
            "ok": False,
            "tool": tool,
            "error": error,
            "hint": "工具调用失败，这不是业务结果：请勿据此声称任务完成。"
            "可修正参数重试、换用其它工具，或向用户说明失败原因。",
        },
        ensure_ascii=False,
    )
