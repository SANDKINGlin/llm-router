"""S1.6.4 — jitter + 半开指数退避(min(30×2ⁿ, 300))。

trip 后窗口 = 30 + jitter(0-15);半开探测每失败一次,下次窗口翻倍:
30 → 60 → 120 → 240 → 300(封顶)。

测试用 2-key provider 只 trip 一个 key,保持 provider/global CLOSED(防级联干扰),
纯测 key 级退避窗口序列。
"""
from __future__ import annotations

import pytest

from llm_router.resilience.circuit_breaker import (
    CircuitBreaker,
    CircuitState,
    TripReason,
)


def _trip_key(breaker, provider, key):
    for _ in range(3):
        breaker.record_failure(provider=provider, key=key, reason=TripReason.HARD)


def _setup(monkeypatch, breaker):
    monkeypatch.setattr(breaker, "_jitter_fn", lambda: 0.0)
    breaker._now_override = 1000.0
    breaker.record_failure(provider="p", key="k2", reason=TripReason.HARD)  # 健康兄弟
    _trip_key(breaker, "p", "k1")


@pytest.fixture
def breaker(tmp_path):
    return CircuitBreaker(
        db_path=tmp_path / "circuit.db",
        key_hard_threshold=3,
        base_backoff_seconds=30,
        backoff_cap_seconds=300,
        jitter_seconds=15,
    )


def test_backoff_30s_window_then_allow(breaker, monkeypatch):
    """trip 后窗口 30s:29s 拒,30s 放行(零 jitter)。"""
    _setup(monkeypatch, breaker)
    breaker._now_override = 1000.0 + 29.0
    assert breaker.allow_request(provider="p", key="k1").allowed is False
    breaker._now_override = 1000.0 + 30.0
    assert breaker.allow_request(provider="p", key="k1").allowed is True


def test_exponential_backoff_sequence(breaker, monkeypatch):
    """半开探测连续失败,窗口 = next_probe_at - opened_at:
    60 → 120 → 240 → 300(封顶)→ 300。recovery_window(n)=min(30×2ⁿ,300)。"""
    _setup(monkeypatch, breaker)
    # _setup 后:k1 OPEN,窗口 30(recovery_window(0)),next_probe_at=1030
    expected = [60, 120, 240, 300, 300]  # recovery_window(1..5)
    t = 1030.0  # = 首个 next_probe_at
    for w in expected:
        breaker._now_override = t  # now >= next_probe_at → 可进半开
        assert breaker.allow_request(provider="p", key="k1").allowed is True, (
            f"t={t} 应放行(进半开)"
        )
        breaker.record_failure(provider="p", key="k1", reason=TripReason.HARD)  # 探测失败
        ks = breaker.get_key_state("p", "k1")
        assert ks.state == CircuitState.OPEN
        # 新窗口 = recovery_window(half_open_failures)
        assert ks.next_probe_at - ks.opened_at == w, (
            f"第 {expected.index(w) + 1} 次半开失败后窗口应为 {w},"
            f"实际 {ks.next_probe_at - ks.opened_at}"
        )
        t = ks.next_probe_at  # 推进到新探测点


def test_backoff_capped_at_300(breaker, monkeypatch):
    """连续多次半开失败,窗口不超过 300。"""
    _setup(monkeypatch, breaker)
    for _ in range(10):
        # 推进到能进半开的时刻
        breaker._now_override = breaker._next_probe_or_far(breaker._now_override, "p", "k1")
        breaker.allow_request(provider="p", key="k1")
        breaker.record_failure(provider="p", key="k1", reason=TripReason.HARD)
    ks = breaker.get_key_state("p", "k1")
    assert ks.state == CircuitState.OPEN
    # 最终 next_probe_at 距 opened_at 不超过 cap(300)+jitter(0)=300
    if ks.opened_at and ks.next_probe_at:
        assert ks.next_probe_at - ks.opened_at <= 300
