"""S2.1a · EpsilonGreedy 策略单测(ε 衰减 + 字典序排序键 + 探索/利用)。

core spec line 26-29 + design.md 约束#3(字典序非加权和)。
注入 chooser/explorer 取得确定性(套 breaker _jitter_fn 模式)。
"""
from __future__ import annotations

import pytest

from llm_router.api.epsilon_greedy import EpsilonGreedy, NoCandidateError
from llm_router.config import ProviderEntry


def _entry(name: str, *, is_free: bool, cost: float) -> ProviderEntry:
    return ProviderEntry(
        name=name,
        tier="fast",
        quota=1000,
        cooldown_s=30,
        is_free=is_free,
        cost_multiplier=cost,
    )


def _strategy(entries: dict, *, chooser=None, explorer=None) -> EpsilonGreedy:
    """chooser=常返 1.0 = 永不探索(纯利用);常返 0.0 = 永远探索。"""
    return EpsilonGreedy(
        entries,
        chooser=chooser or (lambda: 1.0),
        explorer=explorer or (lambda k: 0),
    )


def test_sort_free_before_paid_then_cheapest_first():
    """约束#3 字典序:免费严格优先 → 组内 cost 升序。

    利用模式(chooser=1.0 不探索)应返回 ordered[0] = 最优(free + 最便宜)。
    """
    entries = {
        "paid_cheap": _entry("paid_cheap", is_free=False, cost=0.5),
        "free_expensive": _entry("free_expensive", is_free=True, cost=2.0),
        "free_cheap": _entry("free_cheap", is_free=True, cost=0.1),
        "paid_expensive": _entry("paid_expensive", is_free=False, cost=5.0),
    }
    strat = _strategy(entries)  # chooser=1.0 → 纯利用

    # ordered 应为 [free_cheap, free_expensive, paid_cheap, paid_expensive]
    picked = strat.select_provider(list(entries), {})
    assert picked == "free_cheap", "利用模式必须选 free + 最便宜"

    # 直接验排序键顺序
    ordered = sorted(entries, key=strat._sort_key)
    assert ordered == ["free_cheap", "free_expensive", "paid_cheap", "paid_expensive"]


def test_exploit_always_returns_best_when_never_explore():
    """chooser 常返 ≥ ε → 永不探索,每次都返回最优(free_cheap)。"""
    entries = {
        "a": _entry("a", is_free=True, cost=1.0),
        "b": _entry("b", is_free=True, cost=0.1),
    }
    strat = _strategy(entries, chooser=lambda: 1.0)
    for _ in range(20):
        assert strat.select_provider(["a", "b"], {}) == "b"


def test_explore_picks_explorer_index_when_under_epsilon():
    """chooser < ε → 探索,返回 explorer 指定的 index(非最优)。"""
    entries = {
        "best": _entry("best", is_free=True, cost=0.0),
        "second": _entry("second", is_free=True, cost=1.0),
        "worst": _entry("worst", is_free=False, cost=9.0),
    }
    # chooser=0.0 < ε(0.3)→ 探索;explorer 选 index 2(ordered[2]=worst)
    strat = _strategy(entries, chooser=lambda: 0.0, explorer=lambda k: k - 1)
    picked = strat.select_provider(list(entries), {})
    ordered = sorted(entries, key=strat._sort_key)
    assert picked == ordered[-1], "探索模式应返回 explorer 选的 index,非最优"


def test_epsilon_decay_over_requests():
    """ε=0.3 起步,每 1000 次衰减 ×0.9,下限 0.05。直接验 _epsilon()。"""
    strat = _strategy({"a": _entry("a", is_free=True, cost=0.0)})

    assert strat._epsilon() == pytest.approx(0.3)  # 0 次请求

    # 推进计数器到 1000、2000
    strat._requests = 999
    assert strat._epsilon() == pytest.approx(0.3)  # 仍第 0 周期
    strat._requests = 1000
    assert strat._epsilon() == pytest.approx(0.27)  # 第 1 周期(×0.9)
    strat._requests = 2000
    assert strat._epsilon() == pytest.approx(0.243)  # 第 2 周期

    # 衰减到下限(0.3×0.9^17 ≈ 0.0499 < 0.05 → floor)
    strat._requests = 100_000
    assert strat._epsilon() == pytest.approx(0.05)


def test_epsilon_decay_flips_explore_to_exploit_behaviorally():
    """行为级:ε 衰减到 < chooser 阈值后,同样的 chooser 从探索翻为利用。

    chooser=0.1:请求少时 ε=0.3>0.1 → 探索;请求极多 ε=0.05<0.1 → 利用。
    """
    entries = {"best": _entry("best", is_free=True, cost=0.0),
               "other": _entry("other", is_free=False, cost=9.0)}
    strat = EpsilonGreedy(entries, chooser=lambda: 0.1, explorer=lambda k: k - 1)

    strat._requests = 0  # ε=0.3 > 0.1 → 探索 → 非 best
    assert strat.select_provider(list(entries), {}) == "other"

    strat._requests = 100_000  # ε=0.05 < 0.1 → 利用 → best
    assert strat.select_provider(list(entries), {}) == "best"


def test_empty_candidates_raises():
    strat = _strategy({"a": _entry("a", is_free=True, cost=0.0)})
    with pytest.raises(NoCandidateError):
        strat.select_provider([], {})


def test_unknown_candidate_raises():
    """候选不在 entry map → 配置不一致 → ValueError(fail-fast)。"""
    strat = _strategy({"a": _entry("a", is_free=True, cost=0.0)})
    with pytest.raises(ValueError, match="不在 entry map"):
        strat.select_provider(["a", "ghost"], {})


def test_request_counter_increments_per_select():
    """select_provider 每调一次 +1(驱动衰减)。"""
    strat = _strategy({"a": _entry("a", is_free=True, cost=0.0)})
    assert strat._requests == 0
    strat.select_provider(["a"], {})
    strat.select_provider(["a"], {})
    assert strat._requests == 2
