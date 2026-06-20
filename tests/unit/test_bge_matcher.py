"""S2.9 子片 0.1 · 能力匹配核心架构(② 匹配层,Phase2)。

spec: capability-matching/spec.md。design 约束#3:字典序非加权和;spec §S2.9 旧
"w4 加权"→ 实施改字典序(免费对口必胜)。design R1:bge-small 实测,必要时改更小 embedding。

本切片交付**匹配架构**(可插拔 Encoder + 纯 Python cosine + BgeMatcher 守 matches 接口),
真 BgeEncoder(sentence-transformers + torch + 130MB 模型)懒加载接线 defer 到模型就绪子片
(同 A5 诚实边界:venv 无 numpy/torch,1Gi free,装 2GB torch 风险高;design R1 授权替代)。
HashEncoder(hashlib 确定性)用于测试 + Phase1 占位,真 bge 通过 Encoder 协议槽位接入。

TDD:先 RED——Encoder/cosine/BgeMatcher 未实现时 import 即失败。
"""
from __future__ import annotations

import pytest

from llm_router.api.epsilon_greedy import EpsilonGreedy
from llm_router.api.matcher import TierMatcher
from llm_router.config import ProviderEntry
from llm_router.embedding.bge_matcher import BgeMatcher
from llm_router.embedding.encoder import HashEncoder, cosine


def _entry(name: str, tier: str = "fast", *, is_free: bool = True, cost: float = 0.0) -> ProviderEntry:
    return ProviderEntry(
        name=name, tier=tier, quota=1000, cooldown_s=30, is_free=is_free, cost_multiplier=cost
    )


# ── L1:纯 Python cosine + HashEncoder 确定性 ──────────────────────────────


class TestCosine:
    def test_identical_vectors_cosine_one(self):
        assert cosine([1.0, 0.0, 0.0], [1.0, 0.0, 0.0]) == pytest.approx(1.0)

    def test_orthogonal_vectors_cosine_zero(self):
        assert cosine([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)

    def test_opposite_vectors_cosine_neg_one(self):
        assert cosine([1.0, 0.0], [-1.0, 0.0]) == pytest.approx(-1.0)

    def test_dimension_mismatch_raises(self):
        """fail-loud:维度不一致不静默返回 0(防隐 bug)。"""
        with pytest.raises(ValueError):
            cosine([1.0, 0.0], [1.0, 0.0, 0.0])

    def test_zero_vector_raises(self):
        """fail-loud:零向量无定义(除零),不返 NaN 静默。"""
        with pytest.raises(ValueError):
            cosine([0.0, 0.0], [1.0, 0.0])

    def test_general_case_known_value(self):
        # (1,2,3)·(4,5,6)=32;|a|=sqrt(14)|b|=sqrt(77);cos=32/sqrt(1078)≈0.9746
        import math
        expected = 32.0 / math.sqrt(14 * 77)
        assert cosine([1.0, 2.0, 3.0], [4.0, 5.0, 6.0]) == pytest.approx(expected)


class TestHashEncoder:
    def test_deterministic_same_text_same_vector(self):
        """★ 确定性:同文本同向量(无 Math.random,hashlib 驱动,跨运行稳定)。"""
        enc = HashEncoder(dim=32)
        assert enc.encode("hello") == enc.encode("hello")

    def test_different_text_different_vector(self):
        enc = HashEncoder(dim=32)
        assert enc.encode("hello") != enc.encode("world")

    def test_fixed_dim(self):
        enc = HashEncoder(dim=64)
        assert len(enc.encode("any text")) == 64

    def test_similar_text_higher_cosine_than_unrelated(self):
        """★ 语义代理(hash 共享 n-gram):共享 token 的文本 cosine 高于无关文本。

        真 bge 在此槽位接入后语义更准;HashEncoder 用共享子串作代理,保证测试可验。
        "reasoning math" 与 "reasoning" 共享 "reasoning" → cosine 高于 "reasoning" vs "cooking"。
        """
        enc = HashEncoder(dim=128)
        v_base = enc.encode("reasoning")
        v_related = enc.encode("reasoning math")
        v_unrelated = enc.encode("cooking recipe")
        assert cosine(v_base, v_related) > cosine(v_base, v_unrelated)


# ── L2:BgeMatcher matches 接口(守 design docstring"同 matches(tier,task_type)")──


class TestBgeMatcherInterface:
    def test_none_task_type_matches_all(self, m_bge):
        """★ 向后兼容:无 task_type → 全 matches(与 TierMatcher 同,S2.1a 空 ctx 零变化)。"""
        for tier in ("fast", "medium", "strong"):
            assert m_bge.matches(tier, None) is True
            assert m_bge.matches(tier, "") is True

    def test_unknown_task_type_matches_all(self, m_bge):
        """未知 task_type → 全 matches(fail-open,与 TierMatcher 同向后兼容)。"""
        assert m_bge.matches("fast", "totally-unknown-xyz") is True

    def test_strong_reasoning_matched(self, m_bge):
        """strong 能力描述含 reasoning 语义 → reasoning 任务对口。"""
        assert m_bge.matches("strong", "reasoning") is True

    def test_fast_reasoning_not_matched(self, m_bge):
        """fast 能力描述无 reasoning 语义 → reasoning 任务不对口(落 fallback 软尾)。"""
        assert m_bge.matches("fast", "reasoning") is False

    def test_threshold_tunable(self):
        """threshold 可调:高阈值更严(更多任务判不对口)。"""
        loose = BgeMatcher(HashEncoder(dim=128), threshold=0.05)
        strict = BgeMatcher(HashEncoder(dim=128), threshold=0.99)
        # loose 几乎全对口;strict 几乎全不对口(除非完全相同文本)
        assert loose.matches("fast", "reasoning") is True
        assert strict.matches("strong", "reasoning") is False


class TestBgeMatcherScore:
    """S2.9-0.2:score(tier, task_type) -> float|None 暴露 cosine 分(供校准)。

    matches()(0.1)复用 score():None → 全对口 True;否则 cosine > threshold。
    全对口情况(None/空/未知 task_type)→ score 返 None,校准时排除。
    """

    def test_score_returns_float_for_known_task(self, m_bge):
        """已知 task_type → 返 cosine 分(浮点,∈[-1,1])。"""
        s = m_bge.score("strong", "reasoning")
        assert s is not None
        assert isinstance(s, float)
        assert -1.0 <= s <= 1.0

    def test_score_none_for_none_task_type(self, m_bge):
        """None task_type(全对口)→ score None(校准排除)。"""
        assert m_bge.score("strong", None) is None

    def test_score_none_for_empty_task_type(self, m_bge):
        assert m_bge.score("strong", "") is None
        assert m_bge.score("strong", "   ") is None

    def test_score_none_for_unknown_task_type(self, m_bge):
        """未知 task_type(全对口 fail-open)→ score None。"""
        assert m_bge.score("strong", "totally-unknown-xyz") is None

    def test_score_consistent_with_matches(self, m_bge):
        """★ score 与 matches 一致:对已知 task_type,score>threshold ⟺ matches True。"""
        for tier, task in [("strong", "reasoning"), ("fast", "reasoning"),
                           ("fast", "chat"), ("medium", "chat")]:
            s = m_bge.score(tier, task)
            assert s is not None
            assert m_bge.matches(tier, task) is (s > 0.5)

    def test_score_deterministic(self, m_bge):
        """同 (tier, task) 两次 score 相同(HashEncoder 确定性)。"""
        assert m_bge.score("strong", "reasoning") == m_bge.score("strong", "reasoning")


@pytest.fixture
def m_bge() -> BgeMatcher:
    """默认 BgeMatcher(tier→能力描述内置,HashEncoder,threshold 中等)。"""
    return BgeMatcher(HashEncoder(dim=128), threshold=0.5)


# ── L3:接入 EpsilonGreedy 字典序守门(守 routing-priority-principle)─────────
# 红线:capability_match 槽由 BgeMatcher 填后,字典序 (capability DESC, is_free DESC,
# 倍率 ASC) 仍成立——免费对口必胜付费对口。Wilson/bge 不进排序键加权(仅 capability bool)。


def _exploit(entries, matcher=None):
    """chooser=1.0 永不探索(纯利用),验排序键。"""
    return EpsilonGreedy(entries, chooser=lambda: 1.0, explorer=lambda k: 0, matcher=matcher)


class TestDictOrderGuardWithBgeMatcher:
    def test_bge_matcher_injectable_into_epsilon_greedy(self):
        """BgeMatcher 守 matches(tier,task_type) 接口 → 可注入 EpsilonGreedy 不崩。"""
        entries = {"a": _entry("a", tier="strong")}
        strat = _exploit(entries, matcher=BgeMatcher(HashEncoder(dim=64), threshold=0.5))
        assert strat.plan(["a"], {"task_type": "reasoning"}) == ["a"]

    def test_free_matched_beats_paid_matched(self):
        """★ 红线守门(routing-priority-principle):两个都对口时,免费严格压过付费。

        strong-免费 vs strong-付费,都对口 reasoning → 免费排前(字典序第二槽 is_free)。
        bge/cosine 只决定第一槽(对口与否),不干扰免费优先。
        """
        entries = {
            "paid_strong": _entry("paid_strong", tier="strong", is_free=False, cost=0.1),
            "free_strong": _entry("free_strong", tier="strong", is_free=True, cost=9.0),
        }
        strat = _exploit(entries, matcher=BgeMatcher(HashEncoder(dim=128), threshold=0.5))
        plan = strat.plan(list(entries), {"task_type": "reasoning"})
        assert plan[0] == "free_strong", "对口相当时免费必须严格压过付费(字典序非加权)"

    def test_matched_paid_beats_unmatched_free(self):
        """★ 红线守门:对口(付费)严格压过不对口(免费)——capability 首槽最高优先。

        reasoning 任务:strong-付费对口 > fast-免费不对口。验证 bge 的 capability 信号
        正确进首槽,不被 is_free 反转(即 spec 旧 w4 加权"近似免费加分"的坑被字典序堵死)。
        """
        entries = {
            "free_fast": _entry("free_fast", tier="fast", is_free=True, cost=0.0),     # 不对口
            "paid_strong": _entry("paid_strong", tier="strong", is_free=False, cost=5.0),  # 对口
        }
        strat = _exploit(entries, matcher=BgeMatcher(HashEncoder(dim=128), threshold=0.5))
        plan = strat.plan(list(entries), {"task_type": "reasoning"})
        assert plan[0] == "paid_strong", "对口(付费)必须压过不对口(免费),capability 首槽最高"

    def test_bge_and_tier_matcher_same_backward_compat_empty_ctx(self):
        """空 ctx(无 task_type)→ BgeMatcher 与 TierMatcher 都全对口 → S2.1a 顺序零变化。"""
        entries = {
            "paid_cheap": _entry("paid_cheap", is_free=False, cost=0.5),
            "free_expensive": _entry("free_expensive", is_free=True, cost=2.0),
            "free_cheap": _entry("free_cheap", is_free=True, cost=0.1),
        }
        tier_plan = _exploit(entries, matcher=TierMatcher()).plan(
            list(entries), {}
        )
        bge_plan = _exploit(
            entries, matcher=BgeMatcher(HashEncoder(dim=64), threshold=0.5)
        ).plan(list(entries), {})
        assert tier_plan == bge_plan, "空 ctx 两 matcher 顺序须一致(全对口→S2.1a 序)"
