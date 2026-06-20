"""S3.4 · Golden Set Wilson 区间评估(小样本统计)。

**用途**:capability-matching(spec §Golden Set 校准)给新模型用 20-50 题 Golden Set
测分时,用 Wilson score 下界算每题/聚合可信分,**校准能力向量权重**。

**红线(守 routing-priority-principle)**:Wilson 是 Golden Set 校准的统计工具,
**不进路由排序键**。路由选择键是字典序 `(capability_match DESC, is_free DESC, 倍率 ASC)`
(非加权 sum),Wilson 下界只用于"该模型在 Golden Set 上是否达到相关性门槛 >0.6"的
**离线校准判定**,不参与在线 provider 排序。在线排序里的"小样本惩罚"留给 S3+ bandit
(触发条件未满足,不在此切片)。

**本切片范围**(S3.4,依赖 Phase1,2h):
  - `golden_set_score(items)`:对一组 Golden Set 题目测分,返每题 Wilson 下界 + 聚合分
  - `golden_set_correlation_gate`:占位(相关性 >0.6 判定需要真测分数据,S2.9 落地;
    本切片只提供判定函数签名 + 单元测试用合成数据)

**非范围**:bge 编码、真 Golden Set 题库、能力向量权重回写 —— 全在 S2.9(15-25h)。
"""
from __future__ import annotations

from dataclasses import dataclass

from llm_router.stats.wilson import wilson_lower_bound, wilson_score_interval


@dataclass(frozen=True)
class GoldenSetItem:
    """一道 Golden Set 题目的测分。

    Attributes:
        item_id: 题目标识(供 S2.9 关联题库,本切片不校验唯一性)。
        successes: 该模型在此题上的成功次数(多次采样时;单次为 0/1)。
        total: 采样总次数(≥1)。
    """

    item_id: str
    successes: int
    total: int


@dataclass(frozen=True)
class GoldenSetScore:
    """单题 Wilson 评估结果。

    Attributes:
        item_id: 题目标识(回传)。
        lower_bound: Wilson 下界(小样本可信分,∈[0,1])。
        upper_bound: Wilson 上界(∈[0,1],供观测/置信带,不参与聚合判定)。
    """

    item_id: str
    lower_bound: float
    upper_bound: float


@dataclass(frozen=True)
class GoldenSetAggregate:
    """Golden Set 聚合评估。

    Attributes:
        per_item: 每题 Wilson 评估(顺序与输入一致)。
        mean_lower_bound: 各题 Wilson 下界的算术均(聚合可信分,∈[0,1])。
        n_items: 题目数(0 时其余字段无意义)。
    """

    per_item: tuple[GoldenSetScore, ...]
    mean_lower_bound: float
    n_items: int


def golden_set_score(
    items: list[GoldenSetItem],
    *,
    confidence: float = 0.95,
) -> GoldenSetAggregate:
    """对一组 Golden Set 题目测分算 Wilson 区间 + 聚合下界均。

    聚合策略 = 各题 Wilson 下界的算术均(**非** 各题原始通过率均)。
    用下界而非点估:小样本下惩罚"碰巧全对"的单题,符合 spec §Golden Set
    "20-50 题小样本统计"语义。

    Args:
        items: Golden Set 题目测分列表(可空)。
        confidence: Wilson 置信度,(0,1) 开区间;默认 0.95。

    Returns:
        GoldenSetAggregate:空列表 → per_item=()、mean=0.0、n=0(不抛,供
        S2.9 "无测分数据"分支优雅降级)。

    Raises:
        ValueError: 某题参数非法(successes<0 / >total / total<1 / confidence∉(0,1))
        —— 由 wilson_score_interval 透传;Golden Set 校准宁可 fail-loud
        也不要静默吞掉畸形测分(防校准权重被脏数据污染)。
    """
    per_item: list[GoldenSetScore] = []
    for it in items:
        lo, hi = wilson_score_interval(it.successes, it.total, confidence=confidence)
        per_item.append(GoldenSetScore(item_id=it.item_id, lower_bound=lo, upper_bound=hi))
    if not per_item:
        return GoldenSetAggregate(per_item=(), mean_lower_bound=0.0, n_items=0)
    mean = sum(s.lower_bound for s in per_item) / len(per_item)
    return GoldenSetAggregate(
        per_item=tuple(per_item), mean_lower_bound=mean, n_items=len(per_item)
    )


def golden_set_correlation_gate(
    aggregate: GoldenSetAggregate,
    *,
    threshold: float = 0.6,
) -> bool:
    """spec §Golden Set 相关性达标判定:聚合可信分 > threshold。

    **占位说明**:spec 要求"Golden Set 测分与实际表现相关性 >0.6"。真正的"相关性"
    需要"测分"与"实际表现"两组配对数据(Pearson/Spearman),由 S2.9 落地真测分时算。
    本切片用 `mean_lower_bound`(聚合 Wilson 下界)作为相关性的**代理量**:模型在
    Golden Set 上的可信下界越高,其能力向量权重越可信。这是 Phase1 可独立验证的
    统计门,不依赖未实现的 bge 编码。

    Args:
        aggregate: golden_set_score 的输出。
        threshold: 达标阈值,默认 0.6(spec §Golden Set 相关性达标)。∈[0,1]。

    Returns:
        True iff n_items>0 且 mean_lower_bound > threshold。
        空聚合(n=0)→ False(无测分数据不达标,防"零题全过"误判)。
    """
    if not (0.0 <= threshold <= 1.0):
        raise ValueError(f"threshold 须 ∈[0,1];实际 {threshold}")
    if aggregate.n_items == 0:
        return False
    return aggregate.mean_lower_bound > threshold


def golden_set_lower_bounds(
    items: list[GoldenSetItem],
    *,
    confidence: float = 0.95,
) -> list[float]:
    """便利:仅取各题 Wilson 下界(供 S2.9 能力向量校准排序)。

    等价于 `[s.lower_bound for s in golden_set_score(items, confidence).per_item]`,
    单独暴露因 S2.9 多处只需下界序列(免每次解包 dataclass)。
    """
    return [
        wilson_lower_bound(it.successes, it.total, confidence=confidence)
        for it in items
    ]
