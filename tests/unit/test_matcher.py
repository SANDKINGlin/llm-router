"""S2.2 · 粗档位能力匹配单测(② 匹配层,Phase1)。

tier 能力下限模型:strong(3) > medium(2) > fast(1);task_type → 所需下限;
provider 能力 ≥ 所需 → matches。无/未知 task_type → 全 matches(向后兼容 S2.1a 空 ctx)。

约束#3(routing-priority-principle)字典序排序键 (capability_match DESC, is_free DESC, 倍率 ASC)
的**第一槽** capability_match 由本切片提供(S2.1a 时置常量 True 等能力)。
"""
from __future__ import annotations

import pytest

from llm_router.api.epsilon_greedy import EpsilonGreedy
from llm_router.api.matcher import TierMatcher
from llm_router.config import ProviderEntry


def _entry(name: str, tier: str = "fast", *, is_free: bool = True, cost: float = 0.0) -> ProviderEntry:
    return ProviderEntry(
        name=name, tier=tier, quota=1000, cooldown_s=30, is_free=is_free, cost_multiplier=cost
    )


@pytest.fixture
def m():
    return TierMatcher()


# ── L1:tier 能力下限语义 ──────────────────────────────────────────────────


class TestTierFloor:
    def test_strong_matches_all_task_types(self, m):
        """strong 能向下兼容,覆盖所有任务档。"""
        for tt in ("code", "general", "reasoning", "math"):
            assert m.matches("strong", tt) is True, f"strong 应覆盖 {tt}"

    def test_fast_only_matches_easy_tasks(self, m):
        """fast 只覆盖轻任务(code),够不到 general/reasoning。"""
        assert m.matches("fast", "code") is True
        assert m.matches("fast", "general") is False
        assert m.matches("fast", "reasoning") is False

    def test_medium_matches_general_not_reasoning(self, m):
        assert m.matches("medium", "code") is True
        assert m.matches("medium", "general") is True
        assert m.matches("medium", "reasoning") is False

    def test_none_or_empty_task_type_matches_all_tiers(self, m):
        """★ 向后兼容:无 task_type → 全 matches(S2.1a 空 ctx 顺序零变化)。"""
        for tier in ("fast", "medium", "strong"):
            assert m.matches(tier, None) is True
            assert m.matches(tier, "") is True

    def test_unknown_task_type_matches_all(self, m):
        assert m.matches("fast", "totally-unknown-task") is True

    @pytest.mark.parametrize(
        "tt,expected",
        [
            ("coding", 1), ("编程", 1), ("补全", 1),
            ("chat", 2), ("translate this", 2), ("summarize", 2),
            ("reasoning", 3), ("math problem", 3),
            (None, 1), ("", 1), ("unknown", 1),
        ],
    )
    def test_required_rank_normalization(self, m, tt, expected):
        assert m._required_rank(tt) == expected


# ── L2:接入 EpsilonGreedy 字典序首槽(约束#3) ─────────────────────────────


def _exploit_strategy(entries, **kw):
    """chooser=1.0 永不探索(纯利用),返 ordered[0]。隔离 ε,纯验排序键。"""
    return EpsilonGreedy(entries, chooser=lambda: 1.0, explorer=lambda k: 0, **kw)


class TestSortKeyIntegration:
    def test_capability_preserves_s2_1a_order_with_empty_ctx(self):
        """★ 向后兼容锚点:空 ctx(无 task_type)→ 全 matches → S2.1a 顺序零变化。

        必须与 test_epsilon_greedy.test_sort_free_before_paid_then_cheapest_first 同序。
        """
        entries = {
            "paid_cheap": _entry("paid_cheap", is_free=False, cost=0.5),
            "free_expensive": _entry("free_expensive", is_free=True, cost=2.0),
            "free_cheap": _entry("free_cheap", is_free=True, cost=0.1),
            "paid_expensive": _entry("paid_expensive", is_free=False, cost=5.0),
        }
        strat = _exploit_strategy(entries)
        assert sorted(entries, key=strat._sort_key) == [
            "free_cheap", "free_expensive", "paid_cheap", "paid_expensive"
        ]

    def test_capability_strictly_dominates_free_and_cost(self):
        """约束#3 首槽:对口(capability)严格压过免费/成本。

        reasoning 任务:strong 付费且贵但对口 → 排在 fast 免费但不对口 之前。
        """
        entries = {
            "fast_free": _entry("fast_free", tier="fast", is_free=True, cost=0.0),       # 不对口
            "strong_paid": _entry("strong_paid", tier="strong", is_free=False, cost=5.0),  # 对口
        }
        strat = _exploit_strategy(entries)
        plan = strat.plan(list(entries), {"task_type": "reasoning"})
        assert plan[0] == "strong_paid"

    def test_full_lexicographic_chain_matched_then_unmatched(self):
        """完整字典序:对口-免费(便宜→贵)→ 对口-付费 → 不对口-免费 → 不对口-付费。"""
        entries = {
            "matched_free_costly": _entry("matched_free_costly", tier="strong", is_free=True, cost=9.0),
            "matched_paid": _entry("matched_paid", tier="strong", is_free=False, cost=0.1),
            "unmatched_free": _entry("unmatched_free", tier="fast", is_free=True, cost=0.0),  # 不对口 reasoning
            "matched_free_cheap": _entry("matched_free_cheap", tier="strong", is_free=True, cost=0.1),
        }
        strat = _exploit_strategy(entries)
        plan = strat.plan(list(entries), {"task_type": "reasoning"})
        assert plan == [
            "matched_free_cheap", "matched_free_costly", "matched_paid", "unmatched_free"
        ]

    def test_custom_matcher_injection(self):
        """matcher 可注入(测试确定性;Phase2 换 BgeMatcher 同 matches 接口)。"""
        class AllMatch:
            def matches(self, tier, tt):
                return True

        entries = {"a": _entry("a", tier="fast")}
        strat = EpsilonGreedy(entries, chooser=lambda: 1.0, matcher=AllMatch())
        # 注入的 AllMatch 对 reasoning 也判对口 → plan 正常返回,验注入生效不崩
        assert strat.plan(["a"], {"task_type": "reasoning"}) == ["a"]
