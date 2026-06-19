"""S3.4 · Wilson score 置信区间单测(stdlib,无 scipy)。

守 4 类契约:
  1. **数值正确**:对照 R/Python 经典 (5,10) → (0.237, 0.763) 在 1e-3 容差内
  2. **边界**:p=0 / p=1 / n=1 / 极小样本不崩,且区间限 [0,1]
  3. **单调**:n↑ → 区间窄;confidence↑ → 区间宽
  4. **fail-loud**:非法参数 ValueError(不静默返怪值)
"""
from __future__ import annotations

import math

import pytest

from llm_router.stats.wilson import wilson_lower_bound, wilson_score_interval


# ── 1. 数值正确性(对照公开经典数值) ──────────────────────────────────


def test_classic_5_of_10_at_95_confidence():
    """经典 (successes=5, total=10, conf=95%) → (0.237, 0.763) 在 1e-3 容差内。

    数值参考:R `binom.test(5,10)$conf.int`(Wilson 法)与 Python statsmodels
    `proportion_confint(5,10,method='wilson')` 同结果(对称双尾)。
    """
    lo, hi = wilson_score_interval(5, 10)
    assert math.isclose(lo, 0.2366, abs_tol=1e-3), f"lower={lo}"
    assert math.isclose(hi, 0.7634, abs_tol=1e-3), f"upper={hi}"


def test_classic_50_of_100_at_95_confidence():
    """大样本 (50/100, 95%) → (0.404, 0.596),区间窄于 (5/10)。"""
    lo, hi = wilson_score_interval(50, 100)
    assert math.isclose(lo, 0.404, abs_tol=2e-3), f"lower={lo}"
    assert math.isclose(hi, 0.596, abs_tol=2e-3), f"upper={hi}"


def test_99_confidence_wider_than_95():
    """99% 置信度区间宽于 95%(同 successes/total)。"""
    lo95, hi95 = wilson_score_interval(50, 100, confidence=0.95)
    lo99, hi99 = wilson_score_interval(50, 100, confidence=0.99)
    assert lo99 < lo95 < hi95 < hi99, (
        f"99%({lo99},{hi99}) 应严格包含 95%({lo95},{hi95})"
    )


# ── 2. 边界(p=0 / p=1 / n=1 / 区间限 [0,1]) ───────────────────────


def test_zero_successes_lower_bound_is_zero():
    """p=0(0 success)→ lower bound 数学上 = 0(浮点 1e-10 容差,且不为负)。"""
    lo, hi = wilson_score_interval(0, 10)
    assert math.isclose(lo, 0.0, abs_tol=1e-10), f"p=0 时 lower 应 ≈0;实际 {lo}"
    assert lo >= 0.0, f"p=0 时 lower 不应负;实际 {lo}"
    assert 0.0 < hi < 1.0, f"p=0 时 upper 应在 (0,1);实际 {hi}"


def test_all_successes_lower_bound_below_one_small_sample():
    """p=1 + 小样本 → lower bound 严格 <1(未达上限,Wilson 性质)。

    数学性质:p=1 时 upper bound 数学上恰好 = 1.0(center+margin 化简=1);
    "未饱和"体现在 lower bound:lower = 1/(1+z²/n),n 越小 lower 越远离 1。
    n=10, 95% conf:lower ≈ 1/(1+3.84/10) ≈ 0.722。
    """
    lo, hi = wilson_score_interval(10, 10)
    assert lo < 1.0, f"p=1 + 小样本时 lower 应 <1(Wilson 不饱和);实际 {lo}"
    assert math.isclose(lo, 0.722, abs_tol=5e-3), f"lower={lo}"
    # upper 数学上 = 1.0(p=1 公式化简)
    assert math.isclose(hi, 1.0, abs_tol=1e-10), f"p=1 时 upper 数学上 = 1;实际 {hi}"


def test_n1_does_not_crash_returns_valid_interval():
    """n=1 极小样本不崩,区间合法 [0,1]。"""
    lo, hi = wilson_score_interval(1, 1)
    assert 0.0 <= lo <= hi <= 1.0


def test_interval_always_within_unit_range():
    """所有合法输入区间都在 [0, 1] 内(防数值溢出回怪值)。"""
    for s, n in [(0, 1), (1, 1), (1, 2), (50, 100), (100, 100), (1, 1000)]:
        lo, hi = wilson_score_interval(s, n)
        assert 0.0 <= lo <= hi <= 1.0, (
            f"interval({s},{n})=({lo},{hi}) 应限 [0,1] 且 lo<=hi"
        )


# ── 3. 单调性 ──────────────────────────────────────────────────────


def test_more_samples_narrow_interval_at_same_p():
    """同 p=0.5,n 越大区间越窄(中心极限定理)。"""
    spans = []
    for n in [10, 100, 1000]:
        lo, hi = wilson_score_interval(n // 2, n)
        spans.append(hi - lo)
    assert spans[0] > spans[1] > spans[2], (
        f"n↑ 应使区间窄;实际 spans = {spans}"
    )


def test_symmetric_around_half():
    """p=0.5 时区间中心 ≈ 0.5(对称性,容差 1e-9)。"""
    lo, hi = wilson_score_interval(50, 100)
    center = (lo + hi) / 2.0
    assert math.isclose(center, 0.5, abs_tol=1e-9), f"center={center}"


# ── 4. fail-loud(非法参数 ValueError) ────────────────────────────


def test_total_zero_raises():
    with pytest.raises(ValueError, match="total"):
        wilson_score_interval(0, 0)


def test_negative_successes_raises():
    with pytest.raises(ValueError, match="successes"):
        wilson_score_interval(-1, 10)


def test_successes_exceeds_total_raises():
    with pytest.raises(ValueError, match="successes"):
        wilson_score_interval(11, 10)


def test_invalid_confidence_raises():
    for bad in (0.0, 1.0, -0.1, 1.5):
        with pytest.raises(ValueError, match="confidence"):
            wilson_score_interval(5, 10, confidence=bad)


# ── 便利包装 wilson_lower_bound ─────────────────────────────────────


def test_lower_bound_helper_matches_interval_lower():
    """wilson_lower_bound 应等于 wilson_score_interval 的 lower。"""
    lo, _hi = wilson_score_interval(5, 10)
    assert wilson_lower_bound(5, 10) == lo


def test_lower_bound_used_for_ranking_smaller_sample_lower():
    """同一 p,样本越小 lower bound 越低(用于"惩罚不确定性"排序)。"""
    # p=1.0 但 n 小 vs 同 p=1.0 但 n 大
    lb_small = wilson_lower_bound(2, 2)
    lb_large = wilson_lower_bound(20, 20)
    assert lb_small < lb_large, (
        f"小样本 p=1 lb({lb_small}) 应 < 大样本 p=1 lb({lb_large})"
    )
