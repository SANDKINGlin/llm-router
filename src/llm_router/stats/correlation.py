"""S2.9 子片 0.2 · Pearson 相关系数(纯 Python,无 scipy)。

spec: capability-matching/spec.md Req 3「Golden Set 测分与实际表现相关性 >0.6」。
S3.4 golden_set.py docstring 明确 defer:真 Pearson/Spearman 相关性需配对数据,
defer 到 S2.9;本切片落地 Pearson(配对数据相关性,闭合 spec Req 3)。

**用途**:Golden Set 校准时,算「BgeMatcher cosine 分」与「模型实际表现」的线性相关性,
衡量能力匹配的预测力;达标 >0.6(spec Req 3 Scenario)。

**红线(守 routing-priority-principle)**:Pearson 是离线校准统计工具,**不进路由排序键**。
路由键仍是字典序 `(capability_match DESC, is_free DESC, 倍率 ASC)`(非加权 sum);
Pearson 只用于离线判定「cosine 分对实际表现的预测力」+ 选 BgeMatcher.threshold,
不参与在线 provider 排序。静态断言见 test_calibration.py TestRedLine。

纯 Python(stdlib `math`,无 scipy/numpy,同 S3.4 Wilson 纪律)。
fail-loud:长度不一致 / 样本<2 / 零方差 → ValueError(不静默返 NaN,守 gotchas「失败要响亮」)。
"""
from __future__ import annotations

import math
from collections.abc import Sequence


def pearson(xs: Sequence[float], ys: Sequence[float]) -> float:
    """Pearson 积矩相关系数 r ∈ [-1, 1]。

    r = Σ(dxi·dyi) / sqrt(Σdxi² · Σdyi²),其中 dxi = xi - mean(x)。
    纯 Python(stdlib,无 scipy/numpy)。

    Args:
        xs: 第一组观测值。
        ys: 第二组观测值(与 xs 配对,长度须一致)。

    Returns:
        r ∈ [-1.0, 1.0]:1.0 完全正相关,-1.0 完全负相关,0 无线性相关。

    Raises:
        ValueError: 长度不一致 / 样本数 <2 / 某组零方差(除零无定义)——
            不静默返 NaN(fail-loud,防校准被脏数据污染)。
    """
    n = len(xs)
    if n != len(ys):
        raise ValueError(f"pearson 长度不一致: len(xs)={n} len(ys)={len(ys)}")
    if n < 2:
        raise ValueError(f"pearson 样本数须 ≥2;实际 {n}(无法算相关性)")
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    sxy = 0.0
    sxx = 0.0
    syy = 0.0
    for x, y in zip(xs, ys):
        dx = x - mean_x
        dy = y - mean_y
        sxy += dx * dy
        sxx += dx * dx
        syy += dy * dy
    if sxx == 0.0 or syy == 0.0:
        raise ValueError(
            "pearson 零方差无定义(某组为常量,除零);不返 NaN 静默"
        )
    return sxy / math.sqrt(sxx * syy)
