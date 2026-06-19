"""S3.4 · Wilson score 置信区间(小样本统计)。

**用途**:Phase 2 Golden Set 校准 / bandit reward 评估的小样本可信下界,**不依赖 scipy**
(stdlib `statistics.NormalDist` 已自带 inv_cdf,Python 3.8+)。

经典公式(Wilson 1927):给定 successes / total,在置信度 c 下:

    z = NormalDist.inv_cdf((1+c)/2)        # 双侧 z 临界值(95% → 1.95996...)
    p = successes / total
    denom  = 1 + z**2 / total
    center = (p + z**2 / (2*total)) / denom
    margin = z * sqrt(p*(1-p)/total + z**2 / (4*total**2)) / denom
    return (center - margin, center + margin)

性质(单元测试守门):
  - 区间总在 [0, 1] 内
  - p=0 时 lower=0;p=1 时 upper<1(小样本未达上限)
  - n 越大区间越窄(单调收紧)
  - 置信度越高区间越宽(95% ⊂ 99%)
  - 对称(0.5 处中心 = 0.5)
"""
from __future__ import annotations

import math
from statistics import NormalDist


def wilson_score_interval(
    successes: int,
    total: int,
    *,
    confidence: float = 0.95,
) -> tuple[float, float]:
    """计算 Wilson score 双侧置信区间 (lower, upper)。

    Args:
        successes: 成功次数(整数,0 ≤ successes ≤ total)。
        total: 试验总数(整数,total ≥ 1)。
        confidence: 置信度,(0, 1) 开区间;默认 0.95(z ≈ 1.96)。

    Returns:
        (lower, upper):置信区间下/上界,均限 [0, 1]。

    Raises:
        ValueError: 参数非法(total<1 / successes<0 / successes>total / confidence∉(0,1))。

    Example:
        >>> lo, hi = wilson_score_interval(5, 10)
        >>> 0.20 < lo < 0.30 and 0.70 < hi < 0.80  # 经典 (0.237, 0.763)
        True
    """
    if total < 1:
        raise ValueError(f"total 必须 ≥1;实际 {total}")
    if successes < 0 or successes > total:
        raise ValueError(
            f"successes 必须 ∈ [0, total]({total});实际 {successes}"
        )
    if not (0.0 < confidence < 1.0):
        raise ValueError(f"confidence 必须 ∈ (0, 1);实际 {confidence}")

    # z = 标准正态分布双侧临界值(stdlib NormalDist.inv_cdf,无 scipy 依赖)
    z = NormalDist().inv_cdf((1.0 + confidence) / 2.0)
    n = float(total)
    p = successes / n
    z2 = z * z
    denom = 1.0 + z2 / n
    center = (p + z2 / (2.0 * n)) / denom
    margin = z * math.sqrt(p * (1.0 - p) / n + z2 / (4.0 * n * n)) / denom

    lower = max(0.0, center - margin)
    upper = min(1.0, center + margin)
    return lower, upper


def wilson_lower_bound(
    successes: int,
    total: int,
    *,
    confidence: float = 0.95,
) -> float:
    """便利包装:返 Wilson score 区间下界。

    用例:bandit / Golden Set 排序时按"小样本可信下界"打分,既惩罚样本量小又
    奖励高成功率(Reddit-style 排序);单一标量便于排序键。
    """
    lower, _upper = wilson_score_interval(successes, total, confidence=confidence)
    return lower
