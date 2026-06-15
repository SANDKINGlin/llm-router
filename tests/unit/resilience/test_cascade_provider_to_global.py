"""S1.6.2 — Gap3 级联(provider→global):global 仅当全部 provider 都 OPEN 才 trip。

用户确认 Gap3(number-free):"全部 provider 都 OPEN → global OPEN → freeze 下层"。
global 无自动恢复(灾难态,靠 S4.3 reset/重启)— HERMES 设计审 [CONSENSUS]。

Gap3 推论:单 provider 系统,该 provider OPEN → 全部 provider OPEN → global OPEN。
故测"global 不 OPEN"时,必须先 seed 一个保持 CLOSED 的兄弟 provider。
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


def _trip_provider(breaker, provider):
    """让 provider 的全部(2 个)key 都 OPEN → provider OPEN(Gap3)。"""
    _trip_key(breaker, provider, "k1")
    _trip_key(breaker, provider, "k2")


def _seed_closed_provider(breaker, provider):
    """建一个保持 CLOSED 的兄弟 provider(1 个 key 各 1 次硬失败,key/provider 都 CLOSED)。"""
    breaker.record_failure(provider=provider, key="k1", reason=TripReason.HARD)


def test_one_provider_open_sibling_closed_global_not_open(breaker, monkeypatch):
    """1 provider OPEN,另一 provider 仍 CLOSED → global 不 OPEN。"""
    monkeypatch.setattr(breaker, "_jitter_fn", lambda: 0.0)
    breaker._now_override = 1000.0
    _seed_closed_provider(breaker, "mock_b")
    _trip_provider(breaker, "mock_a")
    assert breaker.get_global_state().state == CircuitState.CLOSED


def test_all_providers_open_trips_global(breaker, monkeypatch):
    """mock_a + mock_b 全部 OPEN → global OPEN → freeze(任意 provider/key 都拒)。"""
    monkeypatch.setattr(breaker, "_jitter_fn", lambda: 0.0)
    breaker._now_override = 1000.0
    _trip_provider(breaker, "mock_a")
    _trip_provider(breaker, "mock_b")
    assert breaker.get_global_state().state == CircuitState.OPEN
    # 全新 provider 也被冻结
    assert breaker.allow_request(provider="mock_c", key="k1").reason == "global_open"
    # 已 OPEN provider 也被拒(global 优先)
    assert breaker.allow_request(provider="mock_a", key="k1").reason == "global_open"


def test_global_open_has_no_auto_recovery(breaker, monkeypatch):
    """global OPEN 后即使推进极久时间,allow 仍拒(灾难态无自动恢复,靠 S4.3)。"""
    monkeypatch.setattr(breaker, "_jitter_fn", lambda: 0.0)
    breaker._now_override = 1000.0
    _trip_provider(breaker, "mock_a")
    _trip_provider(breaker, "mock_b")
    breaker._now_override = 1_000_000.0
    assert breaker.allow_request(provider="mock_a", key="k1").allowed is False
