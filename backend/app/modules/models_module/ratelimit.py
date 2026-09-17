"""provider.limits 限流（方案 §5 P1-2）。

`model_providers.limits` 此前只是"留位字段"（能写能读，无执行语义）；本模块让它
生效：`rate_limit_rps`（请求/秒）与 `rate_limit_tpm`（token/分钟）。字段名与
settings 的全局兜底同名同义：provider 显式配了以自己的为准，没配走全局缺省
（缺省为 0 = 不限，保持升级前行为不变）。

实现是**双桶令牌桶**（请求桶 + token 桶），按 provider 维度独立：
- 桶容量 = 速率（rps 个请求 / tpm 个 token），即"突发额度 ≤ 1 秒/1 分钟的配额"；
- 额度不足则等待补足；单次等待超过 `settings.model_rate_limit_max_wait_seconds`
  直接放行并 warn——限流是保护措施，不该把 run 挂成无限等待；
- `limits` 变更即重建桶（管理端改配置立即生效，不用重启进程）。

时钟与 sleep 可注入：限流是时间敏感逻辑，必须能用假时钟做确定性单测。
"""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from app.core.config import settings

logger = logging.getLogger(__name__)

Clock = Callable[[], float]
Sleeper = Callable[[float], Awaitable[None]]


class _Bucket:
    """令牌桶：capacity 为突发上限，rate 为每秒补足量。"""

    __slots__ = ("capacity", "rate", "tokens", "updated")

    def __init__(self, capacity: float, rate: float, now: float) -> None:
        self.capacity = capacity
        self.rate = rate
        self.tokens = capacity
        self.updated = now

    def _refill(self, now: float) -> None:
        elapsed = now - self.updated
        if elapsed > 0:
            self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)
            self.updated = now

    def wait_for(self, needed: float, now: float) -> float:
        """预扣 needed 额度，返回补足所需等待秒数（0 = 立即可用）。"""
        self._refill(now)
        if needed > self.capacity:
            # 单次需求超过整桶容量：永远等不够，按满桶计（不死等，放行与否由调用方裁决）
            needed = self.capacity
        if self.tokens >= needed:
            self.tokens -= needed
            return 0.0
        wait = (needed - self.tokens) / self.rate if self.rate > 0 else 0.0
        # 预支：桶置空并把时间戳推到额度补满的那一刻，后续调用据此排队
        self.tokens = 0.0
        self.updated = now + wait
        return wait

    def signature(self) -> tuple[float, float]:
        return (self.capacity, self.rate)


class ProviderRateLimiter:
    """按 provider 维度的限流器（进程内单例：`rate_limiter`）。"""

    def __init__(self, clock: Clock = time.monotonic, sleep: Sleeper | None = None) -> None:
        self._clock = clock
        self._sleep = sleep or asyncio.sleep
        self._limits: dict[str, dict[str, Any]] = {}
        self._buckets: dict[str, tuple[_Bucket | None, _Bucket | None]] = {}

    def remember(self, provider: Mapping[str, Any] | None) -> None:
        """登记 provider 的 limits（构造模型时调用，acquire 时无须再查库）。"""
        if not provider or provider.get("id") is None:
            return
        limits = provider.get("limits")
        if not isinstance(limits, Mapping):
            return
        pid = str(provider["id"])
        new = {k: v for k, v in limits.items() if v is not None}
        if self._limits.get(pid, {}) != new:
            self._buckets.pop(pid, None)  # 配置变更即重建桶
        self._limits[pid] = new

    def reset(self) -> None:
        self._limits.clear()
        self._buckets.clear()

    def _config(self, provider_id: str) -> tuple[float, int]:
        limits = self._limits.get(provider_id) or {}
        rps = limits.get("rate_limit_rps", settings.model_default_rate_limit_rps)
        tpm = limits.get("rate_limit_tpm", settings.model_default_rate_limit_tpm)
        try:
            rps_v = float(rps)
        except (TypeError, ValueError):
            rps_v = 0.0
        try:
            tpm_v = int(tpm)
        except (TypeError, ValueError):
            tpm_v = 0
        return (max(rps_v, 0.0), max(tpm_v, 0))

    def _buckets_for(
        self, provider_id: str, rps: float, tpm: int, now: float
    ) -> tuple[_Bucket | None, _Bucket | None]:
        cached = self._buckets.get(provider_id)
        want_req = (max(rps, 1.0), rps) if rps > 0 else None
        want_tok = (float(tpm), tpm / 60.0) if tpm > 0 else None
        if cached is not None:
            req, tok = cached
            same_req = (req is None and want_req is None) or (
                req is not None and want_req is not None and req.signature() == want_req
            )
            same_tok = (tok is None and want_tok is None) or (
                tok is not None and want_tok is not None and tok.signature() == want_tok
            )
            if same_req and same_tok:
                return cached
        req_bucket = _Bucket(want_req[0], want_req[1], now) if want_req else None
        tok_bucket = _Bucket(want_tok[0], want_tok[1], now) if want_tok else None
        self._buckets[provider_id] = (req_bucket, tok_bucket)
        return (req_bucket, tok_bucket)

    async def acquire(self, provider_id: str | None, tokens: int = 0) -> float:
        """取一次模型调用额度，返回实际等待秒数（0 = 未等待）。

        provider_id 为空或未配限流 → 直接返回（未配置时零开销）。
        tokens 为本轮预估的 prompt token 数，计入 tpm 桶。
        """
        if not provider_id:
            return 0.0
        rps, tpm = self._config(provider_id)
        if rps <= 0 and tpm <= 0:
            return 0.0
        now = self._clock()
        req_bucket, tok_bucket = self._buckets_for(provider_id, rps, tpm, now)
        req_wait = req_bucket.wait_for(1.0, now) if req_bucket is not None else 0.0
        tok_wait = (
            tok_bucket.wait_for(float(max(0, int(tokens))), now)
            if tok_bucket is not None and tokens > 0
            else 0.0
        )
        wait = max(req_wait, tok_wait)
        if wait <= 0:
            return 0.0
        if wait > settings.model_rate_limit_max_wait_seconds:
            logger.warning(
                "provider %s 限流需等待 %.1fs，超过上限 %.1fs，放行本次调用",
                provider_id,
                wait,
                settings.model_rate_limit_max_wait_seconds,
            )
            return 0.0
        await self._sleep(wait)
        return wait


rate_limiter = ProviderRateLimiter()
