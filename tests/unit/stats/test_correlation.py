"""S2.9 子片 0.2 · Pearson 相关系数(纯 Python,无 scipy)。

spec: capability-matching/spec.md Req 3「Golden Set 测分与实际表现相关性 >0.6」。
S3.4 golden_set.py docstring 明确 defer:真 Pearson/Spearman 相关性需配对数据,
defer 到 S2.9;本切片落地。

**红线(守 routing-priority-principle)**:Pearson 是 Golden Set 校准的统计工具,
**不进路由排序键**。路由键仍是字典序 `(capability_match DESC, is_free DESC, 倍率 ASC)`;
Pearson 只用于离线校准「BgeMatcher cosine 分对实际表现的预测力」+ 选 threshold,
不参与在线 provider 排序。静态断言见 test_calibration.py。

TDD:先 RED——pearson 未实现时 import 即失败。
"""
from __future__ import annotations

import math

import pytest

from llm_router.stats.correlation import pearson


class TestPearsonKnownValues:
    def test_perfect_positive(self):
        """完全正相关(线性 y=2x)→ 1.0。"""
        assert pearson([1.0, 2.0, 3.0], [2.0, 4.0, 6.0]) == pytest.approx(1.0)

    def test_perfect_negative(self):
        """完全负相关(线性 y=8-2x)→ -1.0。"""
        assert pearson([1.0, 2.0, 3.0], [6.0, 4.0, 2.0]) == pytest.approx(-1.0)

    def test_classical_value(self):
        """经典例子 [1,2,3,4] vs [2,4,5,4] → r≈0.7184。

        r = sum(dxdy)/sqrt(sum(dx²)·sum(dy²)) = 3.5/sqrt(5.0·4.75) ≈ 0.7184。
        """
        r = pearson([1.0, 2.0, 3.0, 4.0], [2.0, 4.0, 5.0, 4.0])
        assert r == pytest.approx(3.5 / math.sqrt(5.0 * 4.75))

    def test_anticorrelated_partial(self):
        """负相关但非完全:[1,2,3] vs [3,2,1] → r=-1.0(完全负相关,对称翻转)。

        mean_x=mean_y=2; dx=[-1,0,1], dy=[1,0,-1]; sum(dxdy)=-2; sum(dx²)=sum(dy²)=2;
        r = -2/sqrt(2·2) = -1.0。
        """
        assert pearson([1.0, 2.0, 3.0], [3.0, 2.0, 1.0]) == pytest.approx(-1.0)


class TestPearsonFailLoud:
    """★ fail-loud(守 gotchas「失败要响亮」):不静默返 NaN。"""

    def test_length_mismatch_raises(self):
        with pytest.raises(ValueError):
            pearson([1.0, 2.0, 3.0], [1.0, 2.0])

    def test_empty_raises(self):
        with pytest.raises(ValueError):
            pearson([], [])

    def test_single_pair_raises(self):
        """n=1 无方差 → raise(n=2 才有意义,但 n=1 必拒)。"""
        with pytest.raises(ValueError):
            pearson([1.0], [2.0])

    def test_zero_variance_x_raises(self):
        """x 常量(零方差)→ 除零无定义 → raise(不返 NaN)。"""
        with pytest.raises(ValueError):
            pearson([5.0, 5.0, 5.0], [1.0, 2.0, 3.0])

    def test_zero_variance_y_raises(self):
        """y 常量(零方差)→ raise。"""
        with pytest.raises(ValueError):
            pearson([1.0, 2.0, 3.0], [7.0, 7.0, 7.0])


class TestPearsonProperties:
    def test_symmetric(self):
        """pearson(x,y) == pearson(y,x)。"""
        xs = [1.0, 2.5, 3.0, 4.5, 6.0]
        ys = [2.0, 3.0, 2.5, 5.0, 6.5]
        assert pearson(xs, ys) == pytest.approx(pearson(ys, xs))

    def test_range_minus_one_to_one(self):
        """|r| ≤ 1。"""
        xs = [1.0, 2.0, 3.0, 4.0, 5.0]
        ys = [9.0, 7.0, 8.0, 5.0, 6.0]
        r = pearson(xs, ys)
        assert -1.0 <= r <= 1.0
