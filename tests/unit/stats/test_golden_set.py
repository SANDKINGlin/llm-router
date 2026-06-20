"""S3.4 · Golden Set Wilson 区间评估单测。

守 5 类契约:
  1. **聚合正确**:mean_lower_bound = 各题 Wilson 下界均(非原始通过率均)
  2. **空输入优雅降级**:空列表不抛,返 n=0 / mean=0.0 / per_item=()
  3. **fail-loud**:畸形测分透传 Wilson 的 ValueError(不静默吞脏数据)
  4. **相关性门**:spec §Golden Set 阈值 0.6,空聚合不达标
  5. **红线**:本模块不被 epsilon_greedy import(Wilson 不进路由排序键)
"""
from __future__ import annotations

import math

import pytest

from llm_router.stats.golden_set import (
    GoldenSetAggregate,
    GoldenSetItem,
    golden_set_correlation_gate,
    golden_set_lower_bounds,
    golden_set_score,
)
from llm_router.stats.wilson import wilson_lower_bound


# ── 1. 聚合正确性 ────────────────────────────────────────────────────


def test_aggregate_mean_is_wilson_lower_bounds_mean_not_pass_rate():
    """mean_lower_bound = 各题 Wilson 下界均,**非** 原始 successes/total 均。

    反例守门:两题都 (1,1)(通过率均=1.0),但 Wilson 下界均 <1(小样本惩罚)。
    """
    items = [GoldenSetItem("q1", 1, 1), GoldenSetItem("q2", 1, 1)]
    agg = golden_set_score(items)
    expected = (wilson_lower_bound(1, 1) + wilson_lower_bound(1, 1)) / 2
    assert math.isclose(agg.mean_lower_bound, expected, abs_tol=1e-12)
    # 关键:不是原始通过率均 1.0
    assert agg.mean_lower_bound < 1.0, (
        "小样本 (1,1) Wilson 下界应 <1;若 ==1.0 说明用了原始通过率(WRONG)"
    )


def test_per_item_preserves_input_order_and_fields():
    """per_item 顺序与输入一致,字段回传 item_id + Wilson 区间。"""
    items = [
        GoldenSetItem("alpha", 5, 10),
        GoldenSetItem("beta", 0, 5),
        GoldenSetItem("gamma", 10, 10),
    ]
    agg = golden_set_score(items)
    assert agg.n_items == 3
    assert [s.item_id for s in agg.per_item] == ["alpha", "beta", "gamma"]
    # alpha: (5,10) 下界 ≈0.237
    assert math.isclose(agg.per_item[0].lower_bound, 0.2366, abs_tol=1e-3)
    # beta: (0,5) 下界 = 0
    assert math.isclose(agg.per_item[1].lower_bound, 0.0, abs_tol=1e-10)
    # gamma: (10,10) 下界 ≈0.722
    assert math.isclose(agg.per_item[2].lower_bound, 0.722, abs_tol=5e-3)


def test_confidence_parameter_propagates_to_wilson():
    """confidence 透传:99% 下界均 < 95% 下界均(同测分,区间更保守)。"""
    items = [GoldenSetItem("q", 5, 10)]
    agg95 = golden_set_score(items, confidence=0.95)
    agg99 = golden_set_score(items, confidence=0.99)
    assert agg99.per_item[0].lower_bound < agg95.per_item[0].lower_bound


# ── 2. 空输入优雅降级 ────────────────────────────────────────────────


def test_empty_items_returns_zero_aggregate_not_raises():
    """空列表不抛(供 S2.9 无测分数据分支优雅降级)。"""
    agg = golden_set_score([])
    assert isinstance(agg, GoldenSetAggregate)
    assert agg.per_item == ()
    assert agg.n_items == 0
    assert agg.mean_lower_bound == 0.0


# ── 3. fail-loud(透传 Wilson ValueError)────────────────────────────


def test_malformed_item_propagates_value_error_total_zero():
    """total=0 的题透传 ValueError(Golden Set 校准宁 fail-loud 不吞脏数据)。"""
    with pytest.raises(ValueError, match="total"):
        golden_set_score([GoldenSetItem("bad", 0, 0)])


def test_malformed_item_propagates_value_error_successes_exceeds_total():
    with pytest.raises(ValueError, match="successes"):
        golden_set_score([GoldenSetItem("bad", 11, 10)])


def test_invalid_confidence_propagates_value_error():
    with pytest.raises(ValueError, match="confidence"):
        golden_set_score([GoldenSetItem("q", 1, 1)], confidence=1.5)


# ── 4. 相关性门(spec §Golden Set 阈值 0.6)─────────────────────────


def test_correlation_gate_passes_when_mean_above_threshold():
    """聚合可信分 > 0.6 → 达标。构造 (8,10)×3,Wilson 下界 ≈0.493×... 需 >0.6?
    (8,10) 95% 下界 ≈0.493,<0.6 → 不达标。改用 (9,10) 下界 ≈0.595 仍<0.6。
    用 (10,10) 下界 ≈0.722 >0.6 → 达标(但样本小,spec 真用 20-50 题)。
    """
    items = [GoldenSetItem(f"q{i}", 10, 10) for i in range(3)]
    agg = golden_set_score(items)
    assert agg.mean_lower_bound > 0.6
    assert golden_set_correlation_gate(agg) is True


def test_correlation_gate_fails_when_mean_below_threshold():
    """(5,10) 下界 ≈0.237,<0.6 → 不达标。"""
    items = [GoldenSetItem("q", 5, 10)]
    agg = golden_set_score(items)
    assert agg.mean_lower_bound < 0.6
    assert golden_set_correlation_gate(agg) is False


def test_correlation_gate_empty_aggregate_fails():
    """空聚合(n=0)不达标(防"零题全过"误判)。"""
    agg = golden_set_score([])
    assert golden_set_correlation_gate(agg) is False


def test_correlation_gate_custom_threshold():
    """阈值可配,严格 > (非 >=)。"""
    items = [GoldenSetItem("q", 10, 10)]
    agg = golden_set_score(items)  # mean ≈0.722
    assert golden_set_correlation_gate(agg, threshold=0.7) is True
    assert golden_set_correlation_gate(agg, threshold=0.8) is False


def test_correlation_gate_invalid_threshold_raises():
    with pytest.raises(ValueError, match="threshold"):
        golden_set_correlation_gate(golden_set_score([]), threshold=1.5)


# ── 5. 红线:Wilson 不进路由排序键 ───────────────────────────────────


def test_golden_set_not_imported_by_epsilon_greedy():
    """**红线守门**:epsilon_greedy 排序键是字典序 (is_free DESC, cost_multiplier ASC),
    **不含** Wilson/Golden Set 加权项(routing-priority-principle:非加权 sum)。
    golden_set 模块不应被路由策略层 import。本测试静态断言 import 图不越界。
    """
    import llm_router.api.epsilon_greedy as eg

    # golden_set 真正的公共 API 符号(排除 __future__.annotations 注入的假符号)
    gs_public = {
        "GoldenSetItem",
        "GoldenSetScore",
        "GoldenSetAggregate",
        "golden_set_score",
        "golden_set_correlation_gate",
        "golden_set_lower_bounds",
    }
    eg_names = set(vars(eg).keys())
    leaked = eg_names & gs_public
    assert not leaked, (
        f"epsilon_greedy 不应 import golden_set 符号(红线:Wilson 不进排序键);"
        f"泄漏 {leaked}"
    )


def test_lower_bounds_helper_matches_score_per_item():
    """golden_set_lower_bounds 便利函数 == golden_set_score.per_item.lower_bound 序列。"""
    items = [GoldenSetItem("a", 5, 10), GoldenSetItem("b", 10, 10)]
    agg = golden_set_score(items)
    bounds = golden_set_lower_bounds(items)
    assert bounds == [s.lower_bound for s in agg.per_item]
