"""配额前置化与 limits 生效（方案 §5 P1-2）。

缺陷（修前）：token 上限在 `on_turn_end` 才判——**钱已经花出去了**；且
`max_tokens_per_run` 为 NULL 时按 0 处理 = 不限，"没配"与"不限"语义混同；
`model_providers.limits` 只是留位字段，写进去也无人执行。

修后口径（本文件冻住）：
1. 解析链 快照 > Agent > 平台缺省；NULL = 继承缺省，显式 0 = 不限；
2. `on_turn_start` 用本轮 prompt 估算做**前置闸**，超限拒绝且不发起模型调用；
3. `provider.limits` 的 `rate_limit_rps` / `rate_limit_tpm` 经令牌桶真正生效。
"""

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from app.modules.engine import timing
from app.modules.engine.graph import _llm_invoke
from app.modules.engine.hooks import BudgetExceededError, RunContext
from app.modules.engine.hooks_impl import BudgetHook
from app.modules.engine.runtime import EngineRuntime
from app.modules.engine.tokens import estimate_messages_tokens, estimate_text_tokens
from app.modules.models_module.provider import get_chat_model
from app.modules.models_module.ratelimit import ProviderRateLimiter, rate_limiter


def _ctx(**limits: int) -> RunContext:
    ctx = RunContext(run_id="r1", conversation_id=None, agent_id="a1")
    ctx.limits = dict(limits)
    return ctx


# ---------------------------------------------------------------- 估算与解析链


def test_estimate_text_tokens_cjk_and_ascii() -> None:
    assert estimate_text_tokens("") == 0
    # 中文按 1 token/字，英文按 1 token/4 字符
    assert estimate_text_tokens("你好世界") == 4
    assert estimate_text_tokens("abcdefgh") == 2


def test_estimate_messages_tokens_includes_overhead_and_tool_calls() -> None:
    plain = [SimpleNamespace(content="hi")]
    with_calls = [SimpleNamespace(content="hi", tool_calls=[{"name": "t", "args": {}}])]
    assert estimate_messages_tokens(plain) == 4 + estimate_text_tokens("hi")
    assert estimate_messages_tokens(with_calls) > estimate_messages_tokens(plain)


def test_estimate_messages_tokens_handles_multimodal_blocks() -> None:
    msg = SimpleNamespace(content=[{"type": "text", "text": "abc"}, {"type": "image_url"}])
    # 图片块无法按字符估，按固定开销计入（宁多勿少）
    assert estimate_messages_tokens([msg]) >= 250


def test_resolve_max_tokens_chain() -> None:
    default = 1_000_000
    # 快照缺省 + Agent 未配 → 平台缺省（不再是"不限"）
    assert timing.resolve_max_tokens_per_run({}, None, default) == default
    assert timing.resolve_max_tokens_per_run(None, None, default) == default
    # 快照显式值优先于 Agent 配置
    assert timing.resolve_max_tokens_per_run({"max_tokens_per_run": 500}, 900, default) == 500
    assert timing.resolve_max_tokens_per_run({}, 900, default) == 900
    # 显式 0 = 不限（逃生舱）；负数按 0 处理
    assert timing.resolve_max_tokens_per_run({"max_tokens_per_run": 0}, 900, default) == 0
    assert timing.resolve_max_tokens_per_run({"max_tokens_per_run": -5}, None, default) == 0


def test_build_ctx_applies_default_limit() -> None:
    """`_build_ctx` 是唯一的 run 预算装配点：NULL 快照必须落到平台缺省。"""
    rt = EngineRuntime()
    run = SimpleNamespace(
        id="00000000-0000-0000-0000-000000000001",
        conversation_id=None,
        agent_id="00000000-0000-0000-0000-000000000002",
        budget={"max_tokens_per_run": None},
        budget_used={},
    )
    ctx = rt._build_ctx(run)  # type: ignore[arg-type]
    assert ctx.limits["max_tokens_per_run"] == 1_000_000

    run.budget = {"max_tokens_per_run": 0}
    assert rt._build_ctx(run).limits["max_tokens_per_run"] == 0  # type: ignore[arg-type]


# ---------------------------------------------------------------- 前置闸


def _hook() -> tuple[BudgetHook, list[tuple[str, dict[str, Any]]]]:
    events: list[tuple[str, dict[str, Any]]] = []

    async def _emit(run_id: str, event_type: str, payload: dict[str, Any]) -> int:
        events.append((event_type, payload))
        return 1

    return BudgetHook(_emit), events


def test_preflight_rejects_before_model_call() -> None:
    hook, events = _hook()
    ctx = _ctx(max_tokens_per_run=100)
    ctx.budget["input_tokens"] = 60
    ctx.budget["output_tokens"] = 10
    ctx.prompt_tokens_est = 40  # 60+10+40 > 100
    with pytest.raises(BudgetExceededError) as e:
        asyncio.run(hook.on_turn_start(ctx, 3))
    assert e.value.gate == "token_preflight"
    assert events and events[0][0] == "budget_warning"
    assert events[0][1]["gate"] == "token_preflight"
    assert events[0][1]["prompt_tokens_est"] == 40


def test_preflight_allows_when_within_quota() -> None:
    hook, events = _hook()
    ctx = _ctx(max_tokens_per_run=1000)
    ctx.budget["input_tokens"] = 100
    ctx.prompt_tokens_est = 200
    asyncio.run(hook.on_turn_start(ctx, 1))
    assert events == []


def test_preflight_skipped_when_unlimited_or_unestimated() -> None:
    hook, _ = _hook()
    unlimited = _ctx(max_tokens_per_run=0)
    unlimited.prompt_tokens_est = 10**9
    asyncio.run(hook.on_turn_start(unlimited, 1))  # 显式不限 → 不拒

    no_est = _ctx(max_tokens_per_run=10)
    no_est.budget["input_tokens"] = 10**6
    asyncio.run(hook.on_turn_start(no_est, 1))  # 未估算（内部短调用）→ 交给事后闸


def test_post_turn_gate_remains_as_backstop() -> None:
    """估算偏差的兜底：真实 usage 到手后仍要结算。"""
    hook, events = _hook()
    ctx = _ctx(max_tokens_per_run=100)
    ctx.budget["input_tokens"] = 200
    with pytest.raises(BudgetExceededError) as e:
        asyncio.run(hook.on_turn_end(ctx, 1, {"input_tokens": 0, "output_tokens": 0}))
    assert e.value.gate == "token_limit"
    assert events[0][1]["gate"] == "token_limit"


# ---------------------------------------------------------------- 限流


class _FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def _limiter() -> tuple[ProviderRateLimiter, _FakeClock]:
    clock = _FakeClock()
    return ProviderRateLimiter(clock=clock, sleep=clock.sleep), clock


def test_limiter_noop_without_limits() -> None:
    limiter, clock = _limiter()
    limiter.remember({"id": "p1", "limits": {}})
    for _ in range(5):
        assert asyncio.run(limiter.acquire("p1")) == 0.0
    assert clock.slept == []


def test_limiter_rps_waits_for_next_slot() -> None:
    limiter, clock = _limiter()
    limiter.remember({"id": "p1", "limits": {"rate_limit_rps": 1}})
    assert asyncio.run(limiter.acquire("p1")) == 0.0  # 突发额度 1
    waited = asyncio.run(limiter.acquire("p1"))
    assert waited == pytest.approx(1.0, abs=0.2)  # 1 rps → 等约 1 秒
    assert clock.slept


def test_limiter_tpm_counts_prompt_tokens() -> None:
    limiter, _ = _limiter()
    limiter.remember({"id": "p1", "limits": {"rate_limit_tpm": 600}})
    assert asyncio.run(limiter.acquire("p1", tokens=600)) == 0.0  # 用满一分钟配额
    waited = asyncio.run(limiter.acquire("p1", tokens=600))
    assert waited == pytest.approx(60.0, abs=1.0)


def test_limiter_buckets_are_per_provider() -> None:
    limiter, _ = _limiter()
    limiter.remember({"id": "p1", "limits": {"rate_limit_rps": 1}})
    limiter.remember({"id": "p2", "limits": {"rate_limit_rps": 1}})
    assert asyncio.run(limiter.acquire("p1")) == 0.0
    assert asyncio.run(limiter.acquire("p2")) == 0.0  # 互不影响


def test_limiter_rebuilds_on_config_change() -> None:
    limiter, _ = _limiter()
    limiter.remember({"id": "p1", "limits": {"rate_limit_rps": 1}})
    assert asyncio.run(limiter.acquire("p1")) == 0.0
    limiter.remember({"id": "p1", "limits": {"rate_limit_rps": 100}})  # 改配置
    assert asyncio.run(limiter.acquire("p1")) == 0.0  # 新桶满额，立即可用


def test_limiter_gives_up_on_excessive_wait() -> None:
    """等待超过上限 → 放行（限流不该把 run 挂死）。"""
    limiter, clock = _limiter()
    limiter.remember({"id": "p1", "limits": {"rate_limit_rps": 0.01}})  # 极端低配
    assert asyncio.run(limiter.acquire("p1")) == 0.0
    assert asyncio.run(limiter.acquire("p1")) == 0.0  # 需等 100s > 上限
    assert clock.slept == []


def test_get_chat_model_registers_limits() -> None:
    """模型构造即登记 limits（调用侧只传 provider_id，不再查库）。"""
    rate_limiter.reset()
    try:
        get_chat_model(
            {
                "id": "p-chat",
                "impl": "openai_compatible",
                "base_url": "http://localhost:1/v1",
                "model_name": "m",
                "params": {},
                "limits": {"rate_limit_rps": 1},
            },
            api_key="k",
        )
        assert asyncio.run(rate_limiter.acquire("p-chat")) == 0.0
        assert asyncio.run(rate_limiter.acquire("p-chat")) > 0  # 即刻第二次要等
    finally:
        rate_limiter.reset()


def test_llm_invoke_applies_limiter(monkeypatch: pytest.MonkeyPatch) -> None:
    """内部短调用入口 `_llm_invoke` 是带上限流的（分类/规划/验收唯一入口）。"""
    limiter, clock = _limiter()
    monkeypatch.setattr("app.modules.engine.graph.rate_limiter", limiter)
    limiter.remember({"id": "p1", "limits": {"rate_limit_rps": 1}})

    calls: list[list[Any]] = []

    class _FakeLLM:
        async def ainvoke(self, messages: list[Any]) -> Any:
            calls.append(messages)
            return SimpleNamespace(content="ok")

    asyncio.run(_llm_invoke(_FakeLLM(), "p1", [SimpleNamespace(content="hi")]))
    asyncio.run(_llm_invoke(_FakeLLM(), "p1", [SimpleNamespace(content="hi")]))
    assert len(calls) == 2
    assert clock.slept  # 第二次被限流挡了一下


# ---------- limits 入参校验（schema 层）----------


def test_limits_schema_accepts_known_keys_and_unknown() -> None:
    from app.modules.models_module.schemas import ModelProviderCreateIn

    body = ModelProviderCreateIn(
        kind="llm",
        name="kimi",
        impl="openai_compatible",
        base_url="https://x/v1",
        model_name="kimi-latest",
        limits={"rate_limit_rps": 2, "rate_limit_tpm": 0, "vendor_note": "keep"},
    )
    assert body.limits["rate_limit_rps"] == 2
    assert body.limits["vendor_note"] == "keep"  # 未知键向前兼容


@pytest.mark.parametrize(
    "limits",
    [{"rate_limit_rps": "abc"}, {"rate_limit_tpm": -1}],
)
def test_limits_schema_rejects_bad_values(limits: dict[str, Any]) -> None:
    from pydantic import ValidationError

    from app.modules.models_module.schemas import ModelProviderUpdateIn

    with pytest.raises(ValidationError):
        ModelProviderUpdateIn(limits=limits)


def test_limits_schema_update_allows_none() -> None:
    from app.modules.models_module.schemas import ModelProviderUpdateIn

    assert ModelProviderUpdateIn(limits=None).limits is None
