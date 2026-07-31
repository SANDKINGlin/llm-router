"""r9.4 hop.py 新 reason 测试:capability_mismatch / ip_safety_skip / quota_exhausted.

验证:
1. 新 reason 在 HOP_REASONS 中
2. advance 正确处理特殊态(budget_exhausted/quota_exhausted/initial)
3. advance 对未知 reason 抛 HopReasonError
"""
import pytest

from llm_router.routing.hop import (
    HOP_REASONS,
    HopAttribution,
    HopReasonError,
    advance,
)


class TestNewHopReasons:
    """r9.4 新增 3 个 hop reason."""

    def test_capability_mismatch_in_set(self):
        """capability_mismatch 在 HOP_REASONS 中."""
        assert "capability_mismatch" in HOP_REASONS

    def test_ip_safety_skip_in_set(self):
        """ip_safety_skip 在 HOP_REASONS 中."""
        assert "ip_safety_skip" in HOP_REASONS

    def test_quota_exhausted_in_set(self):
        """quota_exhausted 在 HOP_REASONS 中."""
        assert "quota_exhausted" in HOP_REASONS


class TestAdvanceSpecialStates:
    """advance 对特殊态的正确处理(应抛 HopReasonError)."""

    def test_advance_reject_initial(self):
        """initial 是特殊态,advance 不能产出."""
        with pytest.raises(HopReasonError, match="是特殊态,不能由 advance 产出"):
            advance(0, "initial", "p1", "p2")

    def test_advance_reject_budget_exhausted(self):
        """budget_exhausted 是特殊态,advance 不能产出."""
        with pytest.raises(HopReasonError, match="是特殊态,不能由 advance 产出"):
            advance(0, "budget_exhausted", "p1", "p2")

    def test_advance_reject_quota_exhausted(self):
        """quota_exhausted 是特殊态,advance 不能产出."""
        with pytest.raises(HopReasonError, match="是特殊态,不能由 advance 产出"):
            advance(0, "quota_exhausted", "p1", "p2")


class TestAdvanceUnknownReason:
    """advance 对未知 reason 的正确处理(应抛 HopReasonError)."""

    def test_advance_reject_unknown_reason(self):
        """未知 reason 抛 HopReasonError."""
        with pytest.raises(HopReasonError, match="unknown reason"):
            advance(0, "totally_made_up", "p1", "p2")

    def test_advance_reject_malformed_reason(self):
        """拼写错误的 reason 抛 HopReasonError."""
        with pytest.raises(HopReasonError, match="unknown reason"):
            advance(0, "capablity_mismatch", "p1", "p2")  # 故意拼错


class TestAdvanceValidReasons:
    """advance 对合法 reason 的正确处理(应返回 HopAttribution)."""

    def test_advance_key_open(self):
        """key_open 是合法 reason,返回 HopAttribution."""
        result = advance(0, "key_open", "p1", "p2")
        assert isinstance(result, HopAttribution)
        assert result.depth == 1
        assert result.reason == "key_open"
        assert result.from_provider == "p1"
        assert result.to_provider == "p2"

    def test_advance_capability_mismatch(self):
        """capability_mismatch 是合法 reason,返回 HopAttribution."""
        result = advance(0, "capability_mismatch", "p1", "p2")
        assert isinstance(result, HopAttribution)
        assert result.depth == 1
        assert result.reason == "capability_mismatch"
        assert result.from_provider == "p1"
        assert result.to_provider == "p2"

    def test_advance_ip_safety_skip(self):
        """ip_safety_skip 是合法 reason,返回 HopAttribution."""
        result = advance(0, "ip_safety_skip", "p1", "p2")
        assert isinstance(result, HopAttribution)
        assert result.depth == 1
        assert result.reason == "ip_safety_skip"
        assert result.from_provider == "p1"
        assert result.to_provider == "p2"

    def test_advance_depth_increments(self):
        """advance depth 正确递增."""
        result = advance(5, "hard_failure", "p1", "p2")
        assert result.depth == 6
