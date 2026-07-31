"""S1.5a · routing.hop 纯函数单测(hop 语义 + total_retry_budget)。

验证 conditional 边界跳变的归因计算 + JSON round-trip + budget 边界。
不计入 tasks.md 的"10 条 fallback E2E"(那是 tests/e2e/);本文件是纯逻辑单测。
"""
from __future__ import annotations

import json

import pytest

from llm_router.routing.hop import (
    DEFAULT_RETRY_BUDGET,
    HopAttribution,
    HopReasonError,
    advance,
    budget_exhausted,
    check_hop_budget,
    initial_attribution,
    parse_attribution,
)


def test_initial_attribution_depth_zero():
    """首跳归因:depth=0 / reason=initial / from=None / to=provider。"""
    attr = initial_attribution("pA")
    assert attr.depth == 0
    assert attr.reason == "initial"
    assert attr.from_provider is None
    assert attr.to_provider == "pA"
    # to_json 产出合法紧凑 JSON,字段齐全。
    d = json.loads(attr.to_json())
    assert d == {"depth": 0, "reason": "initial", "from": None, "to": "pA"}


def test_advance_increments_depth_and_carries_reason_chain():
    """连续跳变 depth 单调 +1,reason/from/to 链对齐。"""
    h0 = initial_attribution("pA")  # depth 0
    h1 = advance(h0.depth, "key_open", "pA", "pB")  # depth 1
    h2 = advance(h1.depth, "soft_content", "pB", "pC")  # depth 2
    h3 = advance(h2.depth, "hard_failure", "pC", "pD")  # depth 3
    assert [h.depth for h in (h0, h1, h2, h3)] == [0, 1, 2, 3]
    assert [h.reason for h in (h1, h2, h3)] == ["key_open", "soft_content", "hard_failure"]
    # from 链 = 上一跳的 to
    assert h3.from_provider == "pC" == h2.to_provider
    assert h2.from_provider == "pB" == h1.to_provider


def test_advance_rejects_unknown_and_special_reasons():
    """未知 reason 或特殊态(initial/budget_exhausted)不能由 advance 产出。

    注: r9.4 (commit 1699d52) 把 fail-closed 从裸 AssertionError 改 raise HopReasonError
    (ValueError 子类). pytest.raises(AssertionError) 不会捕获 ValueError, 因此本 test
    跟进用 HopReasonError, 跟 r9.4 代码契约一致. 完整 rationale 见 hop.py:105 docstring
    + commit message 'r9.4 hop.py 扩 HOP_REASONS …'.
    """
    with pytest.raises(HopReasonError):
        advance(0, "totally_made_up", "pA", "pB")
    with pytest.raises(HopReasonError):
        advance(0, "initial", "pA", "pB")
    with pytest.raises(HopReasonError):
        advance(0, "budget_exhausted", "pA", "pB")


def test_check_hop_budget_boundary():
    """budget=6:depth 0..5 True,depth 6 False(第 7 个 provider 被拦)。"""
    assert all(check_hop_budget(d) for d in range(0, 6))
    assert check_hop_budget(6) is False
    assert DEFAULT_RETRY_BUDGET == 6
    # 自定义 budget 也对
    assert check_hop_budget(2, budget=3) is True
    assert check_hop_budget(3, budget=3) is False


def test_to_json_parse_attribution_roundtrip():
    """to_json → parse_attribution 往返等价(防字段名漂移)。"""
    for attr in (
        initial_attribution("pA"),
        advance(0, "hard_failure", "pA", "pB"),
        budget_exhausted(6, "pF"),
    ):
        raw = attr.to_json()
        back = parse_attribution(raw)
        assert back == attr, f"round-trip 失败: {attr!r} → {raw} → {back!r}"
    # None → None
    assert parse_attribution(None) is None


def test_budget_exhausted_terminal_attribution():
    """终态归因:to=None(不再尝试下一 provider)。"""
    attr = budget_exhausted(6, "pF")
    assert attr.depth == 6
    assert attr.reason == "budget_exhausted"
    assert attr.from_provider == "pF"
    assert attr.to_provider is None
