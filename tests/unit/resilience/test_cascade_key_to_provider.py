"""S1.6.1 — Gap3 级联(key→provider):provider 仅当其全部 key 都 OPEN 才 trip。

用户确认 Gap3(number-free):不用计数器,而用"该 provider 全部 key 都处于 OPEN"。
修 Codex 原计数器模型。

Gap3 语义推论(HERMES [CONSENSUS] 确认):单 key provider 的唯一 key OPEN → 1/1 全 OPEN
→ provider 立即 OPEN。故测"不 trip"时,必须先 seed 一个保持 CLOSED 的兄弟 key。
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
    return CircuitBreaker(
        db_path=tmp_path / "circuit.db",
        key_hard_threshold=3,  # 单 key 连续 3 硬失败 → 该 key OPEN
    )


def _trip_key(breaker, provider, key):
    for _ in range(3):
        breaker.record_failure(provider=provider, key=key, reason=TripReason.HARD)


def _seed_closed_key(breaker, provider, key):
    """建一个保持 CLOSED 的兄弟 key(1 次硬失败,未达阈值)。"""
    breaker.record_failure(provider=provider, key=key, reason=TripReason.HARD)


def test_single_key_open_sibling_closed_provider_not_open(breaker, monkeypatch):
    """provider 有 k1,k2;k1 OPEN 但 k2 仍 CLOSED → provider 不 OPEN。"""
    monkeypatch.setattr(breaker, "_jitter_fn", lambda: 0.0)
    breaker._now_override = 1000.0
    _seed_closed_key(breaker, "mock_a", "k2")  # 先建 k2(CLOSED)
    _trip_key(breaker, "mock_a", "k1")  # k1 OPEN
    assert breaker.get_key_state("mock_a", "k1").state == CircuitState.OPEN
    assert breaker.get_provider_state("mock_a").state == CircuitState.CLOSED


def test_all_keys_open_trips_provider(breaker, monkeypatch):
    """provider 的 k1,k2 全部各自达硬阈值 OPEN → provider OPEN(Gap3)。"""
    monkeypatch.setattr(breaker, "_jitter_fn", lambda: 0.0)
    breaker._now_override = 1000.0
    _trip_key(breaker, "mock_a", "k1")
    _trip_key(breaker, "mock_a", "k2")
    pv = breaker.get_provider_state("mock_a")
    assert pv.state == CircuitState.OPEN
    assert pv.opened_at is not None


def test_single_key_provider_trips_when_only_key_opens(breaker, monkeypatch):
    """provider 只有 1 个 key,该 key OPEN → 1/1 全 OPEN → provider OPEN(单 key 边界)。"""
    monkeypatch.setattr(breaker, "_jitter_fn", lambda: 0.0)
    breaker._now_override = 1000.0
    _trip_key(breaker, "solo", "k1")
    assert breaker.get_provider_state("solo").state == CircuitState.OPEN


def test_provider_open_blocks_its_open_keys_not_fresh_keys(breaker, monkeypatch):
    """派生模型:provider "全 key OPEN" 时,其 OPEN 的 key 自身被拒(key_open);
    但 provider 不独立阻塞——一个全新 key(未失败过)仍可放行(给它机会)。

    sub-decision(选项 A):provider 是派生聚合,不独立熔断;key 各自熔断。
    """
    monkeypatch.setattr(breaker, "_jitter_fn", lambda: 0.0)
    breaker._now_override = 1000.0
    _seed_closed_key(breaker, "mock_b", "k1")  # mock_b 存在 CLOSED → global 不冻
    _trip_key(breaker, "mock_a", "k1")
    _trip_key(breaker, "mock_a", "k2")
    assert breaker.get_provider_state("mock_a").state == CircuitState.OPEN
    # 已 OPEN 的 key 自身被拒
    assert breaker.allow_request(provider="mock_a", key="k1").reason == "key_open"
    # 全新 key k9(CLOSED)→ 放行(派生模型不独立阻塞 provider)
    assert breaker.allow_request(provider="mock_a", key="k9").allowed is True


def test_other_providers_unaffected(breaker, monkeypatch):
    """mock_a 全 key OPEN → mock_a OPEN;mock_b 存在且 CLOSED → global 不冻,mock_b 可访问。"""
    monkeypatch.setattr(breaker, "_jitter_fn", lambda: 0.0)
    breaker._now_override = 1000.0
    _seed_closed_key(breaker, "mock_b", "k1")
    _trip_key(breaker, "mock_a", "k1")
    _trip_key(breaker, "mock_a", "k2")
    decision = breaker.allow_request(provider="mock_b", key="k1")
    assert decision.allowed is True
