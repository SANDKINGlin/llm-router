"""S4.3 · CircuitBreaker.rollback(active_keys) — 回滚状态同步核心原语。

S4.3 DoD #1:`CircuitBreaker.rollback(active_keys)` 单事务,删 db 中 active_keys 之外
的 key(无幽灵)+ 重置 active 里 OPEN/HALF_OPEN 的为 CLOSED(cooldown 清空)。

`active_keys` 是 rollback **之后**仍存在的 (provider, key) 集合——回滚后这些 key 应被
重置为全新起点(全部计数清零,state=CLOSED,probe_in_flight=False);不在此集合的 key
= 旧版本有但新版本无 = 幽灵,必须从 db 删除。

OpenCode 节点 1 [MED] 采纳:`probe_in_flight` 显式清零(语义完整),active_keys 入口
assert(防算错)。CRITICAL race 归 cascade.py 处理(rollback 本身是同步单事务,无 race)。
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
    """确定性 CB:jitter=0、_now=1000.0、threshold=3 走标准路径。"""
    cb = CircuitBreaker(
        db_path=tmp_path / "circuit.db",
        key_hard_threshold=3,
    )
    cb._jitter_fn = lambda: 0.0
    cb._now_override = 1000.0
    return cb


def _trip_key(cb, provider, key):
    for _ in range(3):
        cb.record_failure(provider=provider, key=key, reason=TripReason.HARD)


def _seed_soft_one(cb, provider, key):
    """一次软失败(不达硬阈值 3),state 仍 CLOSED。"""
    cb.record_failure(provider=provider, key=key, reason=TripReason.SOFT_CONTENT)


class TestRollbackDeletesGhosts:
    """DoD:rollback 删 db 中 active_keys 之外的 key(无幽灵)。"""

    def test_keys_outside_active_deleted_from_db(self, breaker, tmp_path):
        _trip_key(breaker, "groq", "GROQ_API_KEY_OLD")
        _trip_key(breaker, "openrouter", "OPENROUTER_KEY_OLD")
        _seed_soft_one(breaker, "mock", "MOCK_KEY")  # 1 软,仍 CLOSED
        # 回滚到只剩 mock/MOCK_KEY
        breaker.rollback(active_keys={("mock", "MOCK_KEY")})

        assert breaker.get_key_state("groq", "GROQ_API_KEY_OLD").state == CircuitState.CLOSED
        assert breaker.get_key_state("openrouter", "OPENROUTER_KEY_OLD").state == CircuitState.CLOSED
        # 持久化层(db)同样清空
        with __import__("sqlite3").connect(tmp_path / "circuit.db") as conn:
            rows = conn.execute(
                "SELECT provider, key FROM circuit_keys"
            ).fetchall()
        assert ("groq", "GROQ_API_KEY_OLD") not in rows
        assert ("openrouter", "OPENROUTER_KEY_OLD") not in rows
        assert ("mock", "MOCK_KEY") in rows

    def test_active_key_not_deleted_even_if_state_open(self, breaker):
        """active 里的 key 哪怕原本 OPEN,rollback 是 reset 而非 delete(场景:回滚后仍在用)。"""
        _trip_key(breaker, "mock", "MOCK_KEY")
        assert breaker.get_key_state("mock", "MOCK_KEY").state == CircuitState.OPEN
        breaker.rollback(active_keys={("mock", "MOCK_KEY")})
        # 仍在 db,但被 reset
        assert breaker.get_key_state("mock", "MOCK_KEY").state == CircuitState.CLOSED


class TestRollbackClearsCooldown:
    """DoD:active 里 OPEN/HALF_OPEN 的 key 重置为 CLOSED(cooldown 清空)。"""

    def test_open_key_reset_to_closed_with_counters_zeroed(self, breaker):
        _trip_key(breaker, "mock", "MOCK_KEY")
        ks = breaker.get_key_state("mock", "MOCK_KEY")
        assert ks.state == CircuitState.OPEN
        assert ks.hard_failures == 3
        assert ks.opened_at is not None
        assert ks.next_probe_at is not None

        breaker.rollback(active_keys={("mock", "MOCK_KEY")})

        ks = breaker.get_key_state("mock", "MOCK_KEY")
        assert ks.state == CircuitState.CLOSED
        assert ks.hard_failures == 0
        assert ks.soft_failures == 0
        assert ks.half_open_failures == 0
        assert ks.opened_at is None
        assert ks.next_probe_at is None
        assert ks.probe_in_flight is False  # OpenCode [MED] 采纳

    def test_half_open_key_reset_to_closed_probe_in_flight_cleared(self, breaker):
        """OpenCode [MED]:HALF_OPEN 也要清 probe_in_flight,语义完整。"""
        _trip_key(breaker, "mock", "MOCK_KEY")
        ks = breaker.get_key_state("mock", "MOCK_KEY")
        # 人工把状态推到 HALF_OPEN(probe_in_flight=True)
        ks.state = CircuitState.HALF_OPEN
        ks.probe_in_flight = True
        breaker._persist_key("mock", "MOCK_KEY", ks)

        breaker.rollback(active_keys={("mock", "MOCK_KEY")})

        ks = breaker.get_key_state("mock", "MOCK_KEY")
        assert ks.state == CircuitState.CLOSED
        assert ks.probe_in_flight is False

    def test_closed_key_unchanged(self, breaker):
        """CLOSED key 在 active 里 rollback 是 no-op(本来就是干净状态)。"""
        _seed_soft_one(breaker, "mock", "MOCK_KEY")  # 1 软,未达阈值
        ks_before = breaker.get_key_state("mock", "MOCK_KEY")
        assert ks_before.state == CircuitState.CLOSED
        ks_before_hard = ks_before.hard_failures

        breaker.rollback(active_keys={("mock", "MOCK_KEY")})

        ks_after = breaker.get_key_state("mock", "MOCK_KEY")
        assert ks_after.state == CircuitState.CLOSED
        # CLOSED 状态下 hard_failures 是连续失败计数,不在 reset 范围(只 reset OPEN/HALF_OPEN)
        # 守"rollback 只清 cooldown,不主动重置 success 计数"语义
        assert ks_after.hard_failures == ks_before_hard


class TestRollbackEdgeCases:
    """边界:空 active / 空 breaker / assertion guard。"""

    def test_empty_active_deletes_all_keys(self, breaker):
        """空 active = 全部回滚 = 删所有(极端场景:全版本切换)。"""
        _trip_key(breaker, "mock", "K1")
        _trip_key(breaker, "openrouter", "K2")
        breaker.rollback(active_keys=set())
        assert breaker.get_key_state("mock", "K1").state == CircuitState.CLOSED
        assert breaker.get_key_state("openrouter", "K2").state == CircuitState.CLOSED

    def test_empty_breaker_is_noop(self, breaker):
        """空 breaker + rollback = 无副作用,无 error。"""
        breaker.rollback(active_keys={("mock", "K1")})  # 不会抛

    def test_assertion_catches_malformed_active_keys(self, breaker):
        """well-formedness guard:非 (str, str) tuple → fail-fast(防 caller 传 None/空)。"""
        with pytest.raises(AssertionError):
            breaker.rollback(active_keys={("", "")})  # 空串 → 拒
        with pytest.raises(AssertionError):
            breaker.rollback(active_keys={("mock", None)})  # type: ignore[arg-type]
        with pytest.raises(AssertionError):
            breaker.rollback(active_keys={("mock",)})  # type: ignore[arg-type]

    def test_assertion_skipped_for_fresh_start(self, breaker):
        """CB 空 + active 非空 = fresh start(apply_policy 切版本,新 key 不在 known 集合属常态)。"""
        # 不抛
        breaker.rollback(active_keys={("mock", "K1")})  # CB 空,active 有 → OK

    def test_rollback_allows_reseed_after(self, breaker):
        """rollback 后 key 真的"全新起点":再 trip 一次,计数从 0 开始。"""
        _trip_key(breaker, "mock", "MOCK_KEY")
        breaker.rollback(active_keys={("mock", "MOCK_KEY")})
        # 再 trip:从 0 开始,需要 3 次硬失败
        breaker.record_failure("mock", "MOCK_KEY", TripReason.HARD)
        assert breaker.get_key_state("mock", "MOCK_KEY").state == CircuitState.CLOSED
        breaker.record_failure("mock", "MOCK_KEY", TripReason.HARD)
        assert breaker.get_key_state("mock", "MOCK_KEY").state == CircuitState.CLOSED
        breaker.record_failure("mock", "MOCK_KEY", TripReason.HARD)
        assert breaker.get_key_state("mock", "MOCK_KEY").state == CircuitState.OPEN
