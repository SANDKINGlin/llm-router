"""S2.9 子片 0.2 · Golden Set 校准接入(闭合 spec Req 3)。

spec: capability-matching/spec.md Req 3「新模型 SHALL 用 20-50 题 Golden Set 自动测分,
校准能力向量权重」+ Scenario「Golden Set 测分与实际表现相关性 >0.6」。

S2.9-0.1 defer 项闭合:
  - BgeMatcher.threshold 硬编码 0.5 → 0.2 由 calibrate_threshold 校准注入
  - S3.4 golden_set.py 真 Pearson 相关性 defer → 0.2 落地(stats/correlation.py)

**校准机制**(方案 A,用户拍板):
  - 配对数据 GoldenSetPair(tier, task_type, actual_success∈[0,1])
  - correlation = pearson([cosine 分], [actual])  —— 预测力(衡量 cosine 对实际表现的线性相关)
  - threshold = 遍历候选,选使 phi(matches 二值, actual) 最高的 t
  - passed = correlation > gate(默认 0.6,spec Req 3)

**红线(守 routing-priority-principle)**:校准只调 BgeMatcher.threshold(进 capability 首槽
bool 的判定阈值),**不进排序键加权**;路由键仍是字典序 `(capability_match DESC,
is_free DESC, 倍率 ASC)`。Pearson/校准不参与在线 provider 排序。

TDD:先 RED——calibration 未实现时 import 即失败。
"""
from __future__ import annotations

import pytest

from llm_router.embedding.bge_matcher import BgeMatcher
from llm_router.embedding.calibration import (
    CalibrationResult,
    GoldenSetPair,
    calibrate_threshold,
)
from llm_router.embedding.encoder import HashEncoder


@pytest.fixture
def matcher() -> BgeMatcher:
    """默认 BgeMatcher(HashEncoder,threshold 占位 0.5;校准后由结果注入)。"""
    return BgeMatcher(HashEncoder(dim=128), threshold=0.5)


def _aligned_pairs() -> list[GoldenSetPair]:
    """合成配对数据:cosine 分与 actual 单调正相关 → Pearson 预测力 >0.6(达标)。

    **数据构造基于 HashEncoder 实测 cosine**(非语义预期):0.1 诚实边界声明 HashEncoder 是
    n-gram 代理非真语义,strong/coding·fast/chat 等"语义对口"对的 cosine 与不对口对重叠
    (均 ~0.35);故本子集只用 cosine 干净分离的对(高分 >0.7 标 actual=1,低分 <0.36 标
    actual=0),验证**校准机制本身**(Pearson + phi 选 threshold)。真 Golden Set 数据由
    S2.10 scanner 抓 model card 测分,真 bge(0.3)接入后语义分离更干净。

    实测分:strong/reasoning 0.86、strong/math 0.72、medium/chat 0.80、fast/code 0.87(高);
    fast/reasoning 0.35、fast/math 0.30、medium/reasoning 0.29、strong/chat 0.27(低)。
    """
    return [
        # 高 cosine(>0.7)→ 实际成功
        GoldenSetPair("strong", "reasoning", 1.0),
        GoldenSetPair("strong", "math", 1.0),
        GoldenSetPair("medium", "chat", 1.0),
        GoldenSetPair("fast", "code", 1.0),
        # 低 cosine(<0.36)→ 实际失败
        GoldenSetPair("fast", "reasoning", 0.0),
        GoldenSetPair("fast", "math", 0.0),
        GoldenSetPair("medium", "reasoning", 0.0),
        GoldenSetPair("strong", "chat", 0.0),
    ]


# ── L1:calibrate_threshold 核心契约 ─────────────────────────────────────────


class TestCalibrateCore:
    def test_returns_calibration_result(self, matcher):
        result = calibrate_threshold(matcher, _aligned_pairs())
        assert isinstance(result, CalibrationResult)

    def test_aligned_data_passes_gate(self, matcher):
        """★ 合成对口数据 → cosine 与 actual 正相关 → correlation >0.6 → passed=True。"""
        result = calibrate_threshold(matcher, _aligned_pairs())
        assert result.n_pairs >= 2
        assert result.correlation > 0.6, f"预测力应>0.6,实际 {result.correlation}"
        assert result.passed is True

    def test_threshold_in_valid_range(self, matcher):
        """校准出的 threshold ∈ (0, 1)。"""
        result = calibrate_threshold(matcher, _aligned_pairs())
        assert result.threshold is not None
        assert 0.0 < result.threshold < 1.0

    def test_threshold_separates_matched_from_unmatched(self, matcher):
        """★ 校准 threshold 落在「对口 cosine」与「不对口 cosine」之间 → 分离两组。

        验证 threshold 使 matches(对口)=True、matches(不对口)=False(对已知 task_type)。
        """
        m = BgeMatcher(HashEncoder(dim=128), threshold=0.5)
        result = calibrate_threshold(m, _aligned_pairs())
        calibrated = BgeMatcher(HashEncoder(dim=128), threshold=result.threshold)
        # 对口组 → matches True;不对口组 → matches False
        assert calibrated.matches("strong", "reasoning") is True
        assert calibrated.matches("fast", "reasoning") is False

    def test_n_pairs_counts_valid_scores(self, matcher):
        """n_pairs = score 非 None 的配对数(全对口/未知 task_type 排除)。"""
        pairs = _aligned_pairs() + [
            GoldenSetPair("strong", None, 1.0),  # None task_type → score None → 排除
            GoldenSetPair("strong", "totally-unknown-xyz", 1.0),  # 未知 → 排除
        ]
        result = calibrate_threshold(matcher, pairs)
        assert result.n_pairs == len(_aligned_pairs())


# ── L2:降级(fail-loud 不静默,显式标记数据不足)──────────────────────────────


class TestCalibrateDegrade:
    def test_empty_pairs_degrades(self, matcher):
        """空配对 → 降级(threshold=None, correlation=0.0, passed=False, n=0)。"""
        result = calibrate_threshold(matcher, [])
        assert result.threshold is None
        assert result.correlation == 0.0
        assert result.passed is False
        assert result.n_pairs == 0

    def test_single_pair_degrades(self, matcher):
        """n<2 无法算 Pearson → 降级。"""
        result = calibrate_threshold(matcher, [GoldenSetPair("strong", "reasoning", 1.0)])
        assert result.threshold is None
        assert result.passed is False
        assert result.n_pairs == 1

    def test_all_none_task_type_degrades(self, matcher):
        """全 None/未知 task_type → 全 score None → n=0 降级。"""
        pairs = [
            GoldenSetPair("strong", None, 1.0),
            GoldenSetPair("fast", "unknown-xyz", 0.0),
        ]
        result = calibrate_threshold(matcher, pairs)
        assert result.n_pairs == 0
        assert result.passed is False


# ── L3:gate 可调 + 注入回 BgeMatcher ─────────────────────────────────────────


class TestCalibrateGateAndInject:
    def test_high_gate_fails_pass(self, matcher):
        """gate=0.999 → 即便相关性不错也 passed=False(spec 达标线可调)。"""
        result = calibrate_threshold(matcher, _aligned_pairs(), gate=0.999)
        assert result.passed is False

    def test_calibrated_threshold_injectable_into_matcher(self, matcher):
        """★ 校准结果注入 BgeMatcher → matches 行为按校准 threshold 改变。"""
        result = calibrate_threshold(matcher, _aligned_pairs())
        calibrated = BgeMatcher(HashEncoder(dim=128), threshold=result.threshold)
        # 校准后 strong/reasoning 仍对口(对口组 cosine > threshold)
        assert calibrated.matches("strong", "reasoning") is True


# ── L4:红线(守 routing-priority-principle)─────────────────────────────────
# 校准/Pearson 只调 threshold(进 capability 首槽 bool 判定),不进排序键加权。
# 路由键仍是字典序 (capability_match DESC, is_free DESC, 倍率 ASC) 非加权和。


class TestRedLine:
    def test_epsilon_greedy_does_not_import_calibration(self):
        """★ 红线:epsilon_greedy(在线排序)不 import calibration/correlation。

        校准是离线工具,不参与在线 provider 排序;排序键字典序不动。
        """
        import llm_router.api.epsilon_greedy as eg

        src = open(eg.__file__, encoding="utf-8").read()
        assert "calibration" not in src, "epsilon_greedy 不得 import calibration(排序键不掺校准)"
        assert "correlation" not in src, "epsilon_greedy 不得 import correlation(排序键不掺 Pearson)"

    def test_rank_key_still_triple_bool_bool_float(self, matcher):
        """★ 红线:_rank 仍返 (capability_match DESC, is_free DESC, 倍率 ASC) 三元字典序。

        校准只改 BgeMatcher.threshold(影响首槽 bool 判定),不改排序键结构/加权。
        """
        from llm_router.api.epsilon_greedy import EpsilonGreedy
        from llm_router.config import ProviderEntry

        def _entry(name, tier="fast", *, is_free=True, cost=0.0):
            return ProviderEntry(
                name=name, tier=tier, quota=1000, cooldown_s=30,
                is_free=is_free, cost_multiplier=cost,
            )

        result = calibrate_threshold(matcher, _aligned_pairs())
        calibrated = BgeMatcher(HashEncoder(dim=128), threshold=result.threshold)
        strat = EpsilonGreedy(
            {"a": _entry("a", tier="strong")},
            chooser=lambda: 1.0, explorer=lambda k: 0, matcher=calibrated,
        )
        key = strat._rank("a", "reasoning")
        # 三元组 (bool, bool, float) —— 字典序非加权
        assert len(key) == 3
        assert isinstance(key[0], bool)
        assert isinstance(key[1], bool)
        assert isinstance(key[2], float)

    def test_free_matched_still_beats_paid_matched_after_calibration(self, matcher):
        """★ 红线守门(同 0.1):校准注入后,两个都对口时免费严格压过付费。

        校准只调 threshold,不破字典序第二槽 is_free。
        """
        from llm_router.api.epsilon_greedy import EpsilonGreedy
        from llm_router.config import ProviderEntry

        def _entry(name, tier, *, is_free, cost):
            return ProviderEntry(
                name=name, tier=tier, quota=1000, cooldown_s=30,
                is_free=is_free, cost_multiplier=cost,
            )

        result = calibrate_threshold(matcher, _aligned_pairs())
        calibrated = BgeMatcher(HashEncoder(dim=128), threshold=result.threshold)
        entries = {
            "paid_strong": _entry("paid_strong", "strong", is_free=False, cost=0.1),
            "free_strong": _entry("free_strong", "strong", is_free=True, cost=9.0),
        }
        strat = EpsilonGreedy(
            entries, chooser=lambda: 1.0, explorer=lambda k: 0, matcher=calibrated,
        )
        plan = strat.plan(list(entries), {"task_type": "reasoning"})
        assert plan[0] == "free_strong", "校准后免费对口仍必胜付费对口(字典序非加权)"
