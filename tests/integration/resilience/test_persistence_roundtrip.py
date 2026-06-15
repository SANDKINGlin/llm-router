"""S1.6.3 — circuit.db 持久化:kill+restart 状态等价(Gap3 语义)。

进程 1 trip key(provider 全 key OPEN→provider OPEN)→ 新实例同 db → 状态恢复。
"""
from __future__ import annotations

from llm_router.resilience.circuit_breaker import (
    CircuitBreaker,
    CircuitState,
    TripReason,
)


def _trip_key(breaker, provider, key):
    for _ in range(3):
        breaker.record_failure(provider=provider, key=key, reason=TripReason.HARD)


def _trip_provider(breaker, provider):
    """provider 的全部(2)key 各自 OPEN → provider OPEN(Gap3)。"""
    _trip_key(breaker, provider, "k1")
    _trip_key(breaker, provider, "k2")


def test_kill_restart_state_equivalent(tmp_path, monkeypatch):
    db_path = tmp_path / "circuit.db"
    cb1 = CircuitBreaker(db_path=db_path, key_hard_threshold=3)
    monkeypatch.setattr(cb1, "_jitter_fn", lambda: 0.0)
    cb1._now_override = 1000.0
    _trip_provider(cb1, "mock_a")
    assert cb1.get_provider_state("mock_a").state == CircuitState.OPEN

    # 进程 2:新实例,相同 db_path,状态应恢复
    cb2 = CircuitBreaker(db_path=db_path, key_hard_threshold=3)
    monkeypatch.setattr(cb2, "_jitter_fn", lambda: 0.0)
    cb2._now_override = 1000.0
    assert cb2.get_provider_state("mock_a").state == CircuitState.OPEN
    assert cb2.allow_request(provider="mock_a", key="k9").allowed is False


def test_state_survives_multiple_restarts(tmp_path, monkeypatch):
    db_path = tmp_path / "circuit.db"
    for _ in range(3):
        cb = CircuitBreaker(db_path=db_path, key_hard_threshold=3)
        monkeypatch.setattr(cb, "_jitter_fn", lambda: 0.0)
        cb._now_override = 1000.0
        # 单 key provider:solo 唯一 key OPEN → provider OPEN
        _trip_key(cb, "solo", "k1")
        assert cb.get_provider_state("solo").state == CircuitState.OPEN


def test_key_state_roundtrip(tmp_path, monkeypatch):
    """key 级状态(含 next_probe_at)也持久化:重启后半开窗口仍正确。"""
    db_path = tmp_path / "circuit.db"
    cb1 = CircuitBreaker(db_path=db_path, key_hard_threshold=3)
    monkeypatch.setattr(cb1, "_jitter_fn", lambda: 0.0)
    cb1._now_override = 1000.0
    _trip_key(cb1, "solo", "k1")
    ks1 = cb1.get_key_state("solo", "k1")
    assert ks1.state == CircuitState.OPEN

    cb2 = CircuitBreaker(db_path=db_path, key_hard_threshold=3)
    monkeypatch.setattr(cb2, "_jitter_fn", lambda: 0.0)
    cb2._now_override = 1000.0
    ks2 = cb2.get_key_state("solo", "k1")
    assert ks2.state == CircuitState.OPEN
    # 窗口内仍拒
    assert cb2.allow_request(provider="solo", key="k1").allowed is False


def test_half_open_probe_in_flight_cleared_on_restart(tmp_path, monkeypatch):
    """崩溃恢复防死锁:allow 放探测后(probe_in_flight=True)进程崩溃,
    新进程加载 HALF_OPEN 时 probe_in_flight 必须清零,否则永远 half_open_busy。"""
    db_path = tmp_path / "circuit.db"
    cb1 = CircuitBreaker(db_path=db_path, key_hard_threshold=3)
    monkeypatch.setattr(cb1, "_jitter_fn", lambda: 0.0)
    cb1._now_override = 1000.0
    cb1.record_failure(provider="p", key="k2", reason=TripReason.HARD)  # 健康兄弟防级联
    for _ in range(3):
        cb1.record_failure(provider="p", key="k1", reason=TripReason.HARD)  # k1 OPEN
    cb1._now_override = 1031.0  # 过窗口
    cb1.allow_request(provider="p", key="k1")  # 进 HALF_OPEN, probe_in_flight=True(已持久化)
    assert cb1.get_key_state("p", "k1").state == CircuitState.HALF_OPEN
    assert cb1.get_key_state("p", "k1").probe_in_flight is True

    # 模拟崩溃:丢弃 cb1,新进程同 db 加载
    cb2 = CircuitBreaker(db_path=db_path, key_hard_threshold=3)
    monkeypatch.setattr(cb2, "_jitter_fn", lambda: 0.0)
    cb2._now_override = 1031.0
    ks = cb2.get_key_state("p", "k1")
    assert ks.state == CircuitState.HALF_OPEN
    assert ks.probe_in_flight is False, "崩溃恢复后 probe_in_flight 必须清零(防 half_open 死锁)"
    # 可重新放探测(不死锁)
    assert cb2.allow_request(provider="p", key="k1").allowed is True
