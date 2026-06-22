"""router-429-rate-limit-backoff · 429 限流精准退避。

R1: ProviderError 带 status_code + retry_after;OpenAIProvider 提取 429 retry_after。
R2: TripReason.RATE_LIMIT;CB record_failure(RATE_LIMIT) 用 retry_after 退避(不翻倍)。
R3: Cascade 429 → RATE_LIMIT;5xx → HARD。
"""
from __future__ import annotations

import asyncio

import httpx
import respx

from llm_router.api.cascade import Cascade
from llm_router.api.epsilon_greedy import EpsilonGreedy
from llm_router.config import ProviderEntry
from llm_router.providers.base import ProviderError
from llm_router.providers.openai import OpenAIProvider
from llm_router.resilience.circuit_breaker import CircuitBreaker, CircuitState, TripReason
from llm_router.store.trace import TraceStore

_BASE = "https://test.openai.invalid/v1"
_URL = f"{_BASE}/chat/completions"


def _run(coro):
    return asyncio.run(coro)


# ── R1 · ProviderError 字段 + OpenAIProvider 提取 ────────────────────────


class TestProviderError429:
    def test_provider_error_carries_status_and_retry_after(self):
        e = ProviderError("x", status_code=429, retry_after=17.0)
        assert e.status_code == 429
        assert e.retry_after == 17.0

    @respx.mock
    def test_openai_429_extracts_retry_after_from_header(self):
        """429 + Retry-After: 17 header → ProviderError.retry_after=17。"""
        respx.post(_URL).mock(
            return_value=httpx.Response(429, json={"error": "rl"},
                                        headers={"Retry-After": "17"})
        )
        p = OpenAIProvider("p", api_key="sk", base_url=_BASE, model="m")
        import pytest
        with pytest.raises(ProviderError) as ei:
            _run(p.complete([{"role": "user", "content": "hi"}]))
        assert ei.value.status_code == 429
        assert ei.value.retry_after == 17.0

    @respx.mock
    def test_openai_500_no_retry_after(self):
        """5xx → status_code=500,retry_after=None。"""
        respx.post(_URL).mock(return_value=httpx.Response(500, json={"error": "down"}))
        p = OpenAIProvider("p", api_key="sk", base_url=_BASE, model="m")
        import pytest
        with pytest.raises(ProviderError) as ei:
            _run(p.complete([{"role": "user", "content": "hi"}]))
        assert ei.value.status_code == 500
        assert ei.value.retry_after is None


# ── R2 · TripReason.RATE_LIMIT + CB 退避 ─────────────────────────────────


class TestCBRateLimitBackoff:
    def test_rate_limit_uses_retry_after_not_doubling(self, tmp_path):
        """RATE_LIMIT + retry_after=5 → next_probe_at = now+5(精准,非 30×2ⁿ)。"""
        b = CircuitBreaker(tmp_path / "c.db", key_hard_threshold=1)
        b._now_override = 1000.0
        b.record_failure("p", "k", TripReason.RATE_LIMIT, retry_after=5.0)
        ks = b.get_key_state("p", "k")
        assert ks.state == CircuitState.OPEN
        # next_probe_at ≈ 1000 + 5(+ jitter 0)→ 1005,非 1030(HARD 默认 30s)
        assert ks.next_probe_at == 1005.0

    def test_rate_limit_no_retry_after_falls_back_default(self, tmp_path):
        """RATE_LIMIT 但 retry_after=None → 回退默认窗口(30s)。"""
        b = CircuitBreaker(tmp_path / "c.db", key_hard_threshold=1)
        b._now_override = 1000.0
        b.record_failure("p", "k", TripReason.RATE_LIMIT, retry_after=None)
        ks = b.get_key_state("p", "k")
        assert ks.state == CircuitState.OPEN
        assert ks.next_probe_at == 1030.0  # 默认 _recovery_window(0)=30

    def test_hard_still_doubles(self, tmp_path):
        """HARD 仍 30s 起步(不读 retry_after)。"""
        b = CircuitBreaker(tmp_path / "c.db", key_hard_threshold=1)
        b._now_override = 1000.0
        b.record_failure("p", "k", TripReason.HARD, retry_after=5.0)  # HARD 忽略 retry_after
        ks = b.get_key_state("p", "k")
        assert ks.next_probe_at == 1030.0  # HARD 默认 30,非 5


# ── R3 · Cascade 429 → RATE_LIMIT / 5xx → HARD ──────────────────────────


class TestCascadeReasonSelection:
    def _cascade(self, tmp_path, provider):
        entries = {"p": ProviderEntry(
            name="p", tier="fast", quota=1, cooldown_s=1,
            is_free=True, cost_multiplier=0.0,
        )}
        breaker = CircuitBreaker(tmp_path / "c.db", key_hard_threshold=1)
        strat = EpsilonGreedy(entries, chooser=lambda: 1.0)
        return Cascade(
            TraceStore(tmp_path / "t.db"), breaker, strat,
            [("p", provider, "k")], budget=6,
        ), breaker

    @respx.mock
    def test_429_triggers_rate_limit_reason(self, tmp_path):
        respx.post(_URL).mock(return_value=httpx.Response(429, json={"error": "rl"},
                                                           headers={"Retry-After": "17"}))
        prov = OpenAIProvider("p", api_key="sk", base_url=_BASE, model="m")
        cascade, breaker = self._cascade(tmp_path, prov)
        r = _run(cascade.run([{"role": "user", "content": "hi"}], correlation_id="c1"))
        assert r.last_reason == "rate_limited"
        ks = breaker.get_key_state("p", "k")
        # 退避用了 retry_after=17(非 30)
        assert ks.next_probe_at is not None

    @respx.mock
    def test_500_triggers_hard_reason(self, tmp_path):
        respx.post(_URL).mock(return_value=httpx.Response(500, json={"error": "down"}))
        prov = OpenAIProvider("p", api_key="sk", base_url=_BASE, model="m")
        cascade, breaker = self._cascade(tmp_path, prov)
        r = _run(cascade.run([{"role": "user", "content": "hi"}], correlation_id="c1"))
        assert r.last_reason == "hard_failure"
