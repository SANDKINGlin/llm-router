"""S2.9 子片 0.2 · Golden Set 校准接入(闭合 spec Req 3)。

spec: capability-matching/spec.md Req 3「新模型 SHALL 用 20-50 题 Golden Set 自动测分,
校准能力向量权重」+ Scenario「Golden Set 测分与实际表现相关性 >0.6」。

闭合两个 0.1/S3.4 defer 项:
  - S2.9-0.1 BgeMatcher.threshold 硬编码 0.5 → 0.2 由 calibrate_threshold 校准注入
  - S3.4 golden_set.py 真 Pearson 相关性 defer → 0.2 落地(stats/correlation.py)

**校准机制**(方案 A,用户拍板):
  1. 配对数据 GoldenSetPair(tier, task_type, actual_success∈[0,1])——「测分」= BgeMatcher
     cosine 分(score()),「实际表现」= actual_success(Golden Set 测得的成功率/Wilson 下界)。
  2. correlation = pearson([cosine 分], [actual]) —— 预测力(衡量 cosine 对实际表现的线性相关)。
  3. threshold = 遍历候选,选使 phi(matches 二值, actual) 最高的 t(二值判定与实际表现对齐最佳)。
  4. passed = correlation > gate(默认 0.6,spec Req 3 相关性达标)。

**红线(守 routing-priority-principle)**:校准只调 BgeMatcher.threshold(进 capability 首槽
bool 的判定阈值),**不进排序键加权**;路由键仍是字典序 `(capability_match DESC,
is_free DESC, 倍率 ASC)`(非加权 sum)。Pearson/校准不参与在线 provider 排序 ——
epsilon_greedy 不 import 本模块(静态断言见 test_calibration.py TestRedLine)。

降级(fail-loud 不静默):配对 <2 / 全 None task_type / cosine 零方差 → 返回 threshold=None、
correlation=0.0、passed=False,显式标记数据不足(不抛,供 scanner「新模型零样本即可用,
Golden Set 后续校准」分支优雅降级;真 fail-loud 由 pearson 透传给非降级路径)。
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from llm_router.stats.correlation import pearson

from .bge_matcher import BgeMatcher

# 候选 threshold 默认网格(0.05..0.90 步长 0.05)。覆盖 0.1 实测的分离区间
# (strong-reasoning 0.86 vs fast-reasoning 0.352),步长 0.05 足够分离两组。
_DEFAULT_CANDIDATES: tuple[float, ...] = tuple(
    round(0.05 * i, 2) for i in range(1, 19)  # 0.05, 0.10, ..., 0.90
)


@dataclass(frozen=True)
class GoldenSetPair:
    """一道 Golden Set 配对(测分 vs 实际表现)。

    Attributes:
        tier: provider 档位(strong/medium/fast);BgeMatcher 据此取能力描述文本。
        task_type: 任务类型(reasoning/math/code/chat...);BgeMatcher 据此取任务描述。
            None/空/未知 → score 返 None(全对口情况,校准排除)。
        actual_success: 模型在此题的实际表现 ∈ [0,1](成功率或 Wilson 下界)。
    """

    tier: str
    task_type: str | None
    actual_success: float


@dataclass(frozen=True)
class CalibrationResult:
    """calibrate_threshold 输出。

    Attributes:
        threshold: 校准出的最优 threshold(使 phi 最高);数据不足 → None。
            注入 BgeMatcher(encoder, threshold=result.threshold) 即生效。
        correlation: Pearson(cosine 分, actual) 预测力 ∈ [-1,1];数据不足 → 0.0。
        passed: correlation > gate(spec Req 3 相关性达标判定)。
        n_pairs: 有效配对数(score 非 None);全对口/未知 task_type 不计入。
    """

    threshold: float | None
    correlation: float
    passed: bool
    n_pairs: int


def calibrate_threshold(
    matcher: BgeMatcher,
    pairs: Sequence[GoldenSetPair],
    *,
    candidates: Sequence[float] | None = None,
    gate: float = 0.6,
) -> CalibrationResult:
    """用 Golden Set 配对数据校准 BgeMatcher.threshold(闭合 spec Req 3)。

    流程:
      1. 收集 score 非 None 的配对 → (cosine 分, actual_success) 列表。
      2. n<2 → 降级(threshold=None, correlation=0.0, passed=False)。
      3. correlation = pearson(cosines, actuals)(预测力;零方差 → 降级)。
      4. 遍历候选 threshold,选使 phi=pearson(matches 二值, actual) 最高的 t。
      5. passed = correlation > gate(默认 0.6,spec Req 3)。

    Args:
        matcher: BgeMatcher(提供 score() 算 cosine 分;encoder/tier 文本已内建)。
        pairs: Golden Set 配对数据(可空)。
        candidates: 候选 threshold 网格(默认 0.05..0.90 步长 0.05);空序列用默认。
        gate: 相关性达标阈值,默认 0.6(spec Req 3)。∈[0,1]。

    Returns:
        CalibrationResult:threshold 可注入回 BgeMatcher;数据不足时 threshold=None。

    Raises:
        ValueError: gate ∉ [0,1](校准达标线非法,fail-loud)。
    """
    if not (0.0 <= gate <= 1.0):
        raise ValueError(f"gate 须 ∈[0,1];实际 {gate}")

    # 1. 收集有效配对(score 非 None)。
    scored: list[tuple[float, float]] = []
    for p in pairs:
        s = matcher.score(p.tier, p.task_type)
        if s is not None:
            scored.append((s, float(p.actual_success)))
    n = len(scored)

    # 2. 数据不足 → 降级(显式标记,不抛)。
    if n < 2:
        return CalibrationResult(
            threshold=None, correlation=0.0, passed=False, n_pairs=n
        )

    cosines = [s for s, _ in scored]
    actuals = [a for _, a in scored]

    # 3. 预测力相关性(零方差 → 降级,不抛)。
    try:
        correlation = pearson(cosines, actuals)
    except ValueError:
        return CalibrationResult(
            threshold=None, correlation=0.0, passed=False, n_pairs=n
        )

    # 4. 遍历候选 threshold,选使 phi(matches 二值, actual) 最高的 t。
    cands = tuple(candidates) if candidates else _DEFAULT_CANDIDATES
    best_t: float | None = None
    best_phi = -2.0  # phi ∈ [-1,1],初值低于下界
    for t in cands:
        matches_bin = [1.0 if s > t else 0.0 for s in cosines]
        try:
            phi = pearson(matches_bin, actuals)
        except ValueError:
            continue  # 二值零方差(全对口或全不对口)→ 该 t 无区分力,跳过
        if phi > best_phi:
            best_phi = phi
            best_t = t

    # 5. 达标判定(spec Req 3 相关性 >0.6)。
    return CalibrationResult(
        threshold=best_t,
        correlation=correlation,
        passed=correlation > gate,
        n_pairs=n,
    )
