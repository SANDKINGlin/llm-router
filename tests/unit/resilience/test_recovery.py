"""S1.6 补丁 — recovery 状态机:OPEN→HALF_OPEN→CLOSED 闭环 + 退避翻倍。

Codex 原实现是单向阀(能 trip 不能自愈)。本套件补齐 key 级 recovery:
  - allow_request 在退避窗口到期时转 HALF_OPEN 放 1 探测
  - record_success 闭合(HALF_OPEN→CLOSED,清计数)
  - half-open 失败→OPEN + 窗口翻倍(指数退避)
  - 同一 half-open 实体只放 1 探测;健康兄弟 key 不受影响

HERMES 设计审 [CONSENSUS]:probe_in_flight 在 success/failure 统一清零防半开死锁。

测试隔离:用 2-key provider 只 trip 一个 key → provider/global 保持 CLOSED,
故可纯测 key 级 recovery(单 key provider 会级联到 global,见 cascade 测试)。
"""
from __future__ import annotations

import pytest

from llm_router.resilience.circuit_breaker import (
    CircuitBreaker,
    CircuitState,
    TripReason,
)


@pytest.fixture
def breaker(tmp_path):
    return CircuitBreaker(db_path=tmp_path / "circuit.db", key_hard_threshold=3)


def _trip_key(breaker, provider, key):
    for _ in range(3):
        breaker.record_failure(provider=provider, key=key, reason=TripReason.HARD)


def _setup(monkeypatch, breaker):
    """注入零 jitter + 固定时钟;trip k1(保持 k2 为健康兄弟,防级联)。"""
    monkeypatch.setattr(breaker, "_jitter_fn", lambda: 0.0)
    breaker._now_override = 1000.0
    breaker.record_failure(provider="p", key="k2", reason=TripReason.HARD)  # k2 存在且 CLOSED
    _trip_key(breaker, "p", "k1")  # k1 OPEN,provider 仍 CLOSED(k2 没全 OPEN)


def test_open_key_blocks_during_backoff_window(breaker, monkeypatch):
    _setup(monkeypatch, breaker)
    decision = breaker.allow_request(provider="p", key="k1")
    assert decision.allowed is False
    assert decision.reason == "key_open"


def test_half_open_probe_after_window_elapsed(breaker, monkeypatch):
    """窗口到期 → allow 转 HALF_OPEN 放 1 探测;同 key 再 allow 拒(half_open_busy)。"""
    _setup(monkeypatch, breaker)
    breaker._now_override = 1031.0  # 过 30s 窗口
    assert breaker.allow_request(provider="p", key="k1").allowed is True  # 放探测
    # 同 key 已 probe_in_flight → 拒
    d2 = breaker.allow_request(provider="p", key="k1")
    assert d2.allowed is False
    assert d2.reason == "half_open_busy"
    # 健康兄弟 key k2 不受影响
    assert breaker.allow_request(provider="p", key="k2").allowed is True


def test_half_open_success_closes_and_clears(breaker, monkeypatch):
    """OPEN→到期→HALF_OPEN→record_success→CLOSED(计数清零)。"""
    _setup(monkeypatch, breaker)
    breaker._now_override = 1031.0
    breaker.allow_request(provider="p", key="k1")  # 进 HALF_OPEN
    breaker.record_success(provider="p", key="k1")
    ks = breaker.get_key_state("p", "k1")
    assert ks.state == CircuitState.CLOSED
    assert ks.hard_failures == 0
    assert ks.soft_failures == 0
    assert ks.next_probe_at is None


def test_half_open_failure_reopens_with_doubled_window(breaker, monkeypatch):
    """HALF_OPEN 探测失败 → OPEN,窗口翻倍(30→60)。"""
    _setup(monkeypatch, breaker)
    breaker._now_override = 1031.0
    breaker.allow_request(provider="p", key="k1")  # HALF_OPEN 放探测
    breaker._now_override = 1032.0
    breaker.record_failure(provider="p", key="k1", reason=TripReason.HARD)
    ks = breaker.get_key_state("p", "k1")
    assert ks.state == CircuitState.OPEN
    assert ks.half_open_failures == 1
    # 新窗口 = 60:1032+59 仍拒,1032+60 放行
    breaker._now_override = 1032.0 + 59.0
    assert breaker.allow_request(provider="p", key="k1").allowed is False
    breaker._now_override = 1032.0 + 60.0
    assert breaker.allow_request(provider="p", key="k1").allowed is True


def test_consecutive_success_resets_closed_counters(breaker, monkeypatch):
    """CLOSED 状态下 2 次失败后 record_success → hard_failures 清零(连续成功重置)。"""
    monkeypatch.setattr(breaker, "_jitter_fn", lambda: 0.0)
    breaker._now_override = 1000.0
    breaker.record_failure(provider="p", key="k1", reason=TripReason.HARD)
    breaker.record_failure(provider="p", key="k1", reason=TripReason.HARD)
    assert breaker.get_key_state("p", "k1").hard_failures == 2
    breaker.record_success(provider="p", key="k1")
    assert breaker.get_key_state("p", "k1").hard_failures == 0
    assert breaker.get_key_state("p", "k1").state == CircuitState.CLOSED


def test_provider_state_is_derived_from_keys(breaker, monkeypatch):
    """派生模型(选项 A):provider 状态完全由 key 派生——
    全 key OPEN → provider OPEN;只要任一 key 恢复 CLOSED → provider 立即 CLOSED。
    无独立 provider 状态机,无级联 close 代码(provider 自动随 key 恢复)。
    """
    monkeypatch.setattr(breaker, "_jitter_fn", lambda: 0.0)
    breaker._now_override = 1000.0
    _trip_key(breaker, "p", "k1")
    _trip_key(breaker, "p", "k2")  # 全 key OPEN → 派生 provider OPEN
    assert breaker.get_provider_state("p").state == CircuitState.OPEN
    # k1 半开探测成功 → CLOSED。只要 k1 不再 OPEN,provider 就不是"全 OPEN"→ 派生 CLOSED
    breaker._now_override = 1031.0
    breaker.allow_request(provider="p", key="k1")  # 进半开放探测
    breaker.record_success(provider="p", key="k1")  # k1 CLOSED
    assert breaker.get_key_state("p", "k1").state == CircuitState.CLOSED
    assert breaker.get_key_state("p", "k2").state == CircuitState.OPEN
    # 派生:k1 CLOSED → 不再"全 OPEN" → provider CLOSED(即使 k2 仍 OPEN)
    assert breaker.get_provider_state("p").state == CircuitState.CLOSED
