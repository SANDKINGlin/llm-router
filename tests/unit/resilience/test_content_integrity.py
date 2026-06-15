"""S1.6.5 — 内容完整性 + 软失败换算(3 软 = 1 硬,Phase1)。

is_complete(text, model):Phase1 两者均非空即完整;残缺=软失败。
breaker.record_failure(SOFT_CONTENT):soft_failures 累计,每 SOFT_TO_HARD_RATIO(3)软换算 1 硬
(整数除法防浮点漂移 — HERMES [CONSENSUS]),累计到 key.hard_failures 达阈值 → key OPEN。
真实 OpenAI/Anthropic dict 字段校验 defer S2.x(接真 provider 时)。
"""
from __future__ import annotations

import pytest

from llm_router.resilience.circuit_breaker import (
    CircuitBreaker,
    CircuitState,
    TripReason,
)
from llm_router.resilience.content_integrity import is_complete


# ── is_complete Phase1 ──────────────────────────────────────────────

def test_is_complete_both_present():
    assert is_complete("hello world", "gpt-free-x") is True


def test_is_complete_empty_text():
    assert is_complete("", "gpt-free-x") is False
    assert is_complete(None, "gpt-free-x") is False


def test_is_complete_empty_model():
    assert is_complete("hello", "") is False
    assert is_complete("hello", None) is False


def test_is_complete_whitespace_only_text():
    assert is_complete("   ", "model") is False


# ── 软失败换算 + 计入 key ───────────────────────────────────────────

@pytest.fixture
def breaker(tmp_path):
    return CircuitBreaker(
        db_path=tmp_path / "circuit.db",
        key_hard_threshold=3,
        soft_to_hard_ratio=3,
    )


def test_three_soft_failures_open_key(breaker, monkeypatch):
    """3 软 = 1 硬;key_hard_threshold=3 → 9 软 = 3 硬 → key OPEN。"""
    monkeypatch.setattr(breaker, "_jitter_fn", lambda: 0.0)
    breaker._now_override = 1000.0
    for _ in range(9):
        breaker.record_failure(provider="p", key="k1", reason=TripReason.SOFT_CONTENT)
    assert breaker.get_key_state("p", "k1").state == CircuitState.OPEN


def test_soft_failures_below_threshold_stay_closed(breaker, monkeypatch):
    """6 软 = 2 硬 < 3 阈值 → key 仍 CLOSED(可访问)。"""
    monkeypatch.setattr(breaker, "_jitter_fn", lambda: 0.0)
    breaker._now_override = 1000.0
    for _ in range(6):
        breaker.record_failure(provider="p", key="k1", reason=TripReason.SOFT_CONTENT)
    ks = breaker.get_key_state("p", "k1")
    assert ks.state == CircuitState.CLOSED
    assert ks.hard_failures == 2  # 2 硬换算


def test_single_soft_failure_does_not_open(breaker, monkeypatch):
    monkeypatch.setattr(breaker, "_jitter_fn", lambda: 0.0)
    breaker._now_override = 1000.0
    breaker.record_failure(provider="p", key="k1", reason=TripReason.SOFT_CONTENT)
    assert breaker.allow_request(provider="p", key="k1").allowed is True


def test_hard_failure_distinguished_from_soft(breaker, monkeypatch):
    """1 硬 → hard_failures=1,key 仍 CLOSED(未达阈值 3)。"""
    monkeypatch.setattr(breaker, "_jitter_fn", lambda: 0.0)
    breaker._now_override = 1000.0
    breaker.record_failure(provider="p", key="k1", reason=TripReason.HARD)
    assert breaker.allow_request(provider="p", key="k1").allowed is True
