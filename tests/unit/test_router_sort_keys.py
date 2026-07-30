"""r9.3 排序键测试 - 5 维字典序验证。

5 维排序键 (按优先级高→低):
1. ip_safety_rank: safe(0) < risky(1) < forbidden(2) (越小越优先)
2. is_free: true(0) < false(1) (免费优先)
3. quota_remaining: 大优先 (desc)
4. capability_match: match(0) < mismatch(1) (能力匹配优先)
5. model_strength: 大优先 (desc, 同能力下选最强)

测试场景: 5 个 provider 按 5 维排序。
期望: nvidia(ip_safe=0) > openrouter(ip_safe=0) > agnes(ip_safe=0) > groq(ip_safe=2=skip)
同 ip_safe 内: is_free=true 优先
同 is_free 内: quota_remaining 大的优先
同 quota 内: capability_match 优先
同 capability 内: model_strength 大的优先
"""
from __future__ import annotations

from dataclasses import dataclass

import pytest

from src.llm_router.config import IP_SAFETY_RANK


@dataclass
class MockProvider:
    """Mock provider for sort key testing."""

    name: str
    ip_safety_rank: str  # "safe" | "risky" | "forbidden"
    is_free: bool
    quota_remaining: int
    capability_match: bool  # True=match(0), False=mismatch(1)
    model_strength: float  # 越大越强

    def __lt__(self, other: "MockProvider") -> bool:
        """5 维排序比较。

        优先级: ip_safety_rank < is_free < -quota_remaining < capability_match < -model_strength
        (注意: quota_remaining 和 model_strength 是越大越优先,所以取负)
        """
        # 维 1: ip_safety_rank (越小越优先)
        self_rank = IP_SAFETY_RANK[self.ip_safety_rank]
        other_rank = IP_SAFETY_RANK[other.ip_safety_rank]
        if self_rank != other_rank:
            return self_rank < other_rank

        # 维 2: is_free (true=0 优先于 false=1)
        self_free = 0 if self.is_free else 1
        other_free = 0 if other.is_free else 1
        if self_free != other_free:
            return self_free < other_free

        # 维 3: quota_remaining (越大越优先, 取负比较)
        if self.quota_remaining != other.quota_remaining:
            return self.quota_remaining > other.quota_remaining

        # 维 4: capability_match (match=0 优先于 mismatch=1)
        self_match = 0 if self.capability_match else 1
        other_match = 0 if other.capability_match else 1
        if self_match != other_match:
            return self_match < other_match

        # 维 5: model_strength (越大越优先, 取负比较)
        return self.model_strength > other.model_strength


def test_sort_keys_5_dimensions():
    """测试 5 维排序键正确性。

    场景: 5 个 provider 按 5 维排序
    期望顺序: nvidia > openrouter > agnes > groq(ip_safe=2=skip)
    """
    providers = [
        MockProvider("nvidia", "safe", True, 1000000, True, 10.0),
        MockProvider("openrouter", "safe", True, 500000, True, 8.0),
        MockProvider("agnes", "safe", True, 500000, True, 6.0),
        MockProvider("modelscope", "safe", False, 2000000, True, 9.0),
        MockProvider("groq", "forbidden", True, 999999, True, 7.0),
    ]

    sorted_providers = sorted(providers)
    names = [p.name for p in sorted_providers]

    # 期望: nvidia > openrouter > agnes > modelscope > groq
    # - nvidia vs openrouter: 同 ip_safe+同 is_free, nvidia quota 更大
    # - openrouter vs agnes: 同 ip_safe+同 is_free+同 quota, openrouter model_strength 更大
    # - agnes vs modelscope: 同 ip_safe, agnes is_free=True 优先于 modelscope is_free=False
    # - modelscope vs groq: modelscope ip_safe=0 优先于 groq ip_safe=2
    assert names == ["nvidia", "openrouter", "agnes", "modelscope", "groq"]


def test_sort_keys_ip_safety_priority():
    """测试 ip_safety_rank 优先级最高。

    safe(0) < risky(1) < forbidden(2)
    """
    providers = [
        MockProvider("safe_provider", "safe", False, 100, True, 5.0),
        MockProvider("risky_provider", "risky", True, 1000000, True, 10.0),
        MockProvider("forbidden_provider", "forbidden", True, 1000000, True, 10.0),
    ]

    sorted_providers = sorted(providers)
    names = [p.name for p in sorted_providers]

    # safe 优先, 即使 risky/forbidden 的其他维度更优
    assert names == ["safe_provider", "risky_provider", "forbidden_provider"]


def test_sort_keys_is_free_priority():
    """测试 is_free 第 2 优先级（同 ip_safe 内）。

    true(0) < false(1), 免费优先。
    """
    providers = [
        MockProvider("free_provider", "safe", True, 100, True, 5.0),
        MockProvider("paid_provider", "safe", False, 1000000, True, 10.0),
    ]

    sorted_providers = sorted(providers)
    names = [p.name for p in sorted_providers]

    # free 优先, 即使 paid 的 quota/model_strength 更大
    assert names == ["free_provider", "paid_provider"]


def test_sort_keys_quota_priority():
    """测试 quota_remaining 第 3 优先级（同 ip_safe+同 is_free 内）。

    越大越优先 (desc)。
    """
    providers = [
        MockProvider("high_quota", "safe", True, 1000000, True, 5.0),
        MockProvider("low_quota", "safe", True, 100, True, 10.0),
    ]

    sorted_providers = sorted(providers)
    names = [p.name for p in sorted_providers]

    # 高 quota 优先, 即使 model_strength 更小
    assert names == ["high_quota", "low_quota"]


def test_sort_keys_capability_priority():
    """测试 capability_match 第 4 优先级（同 ip_safe+同 is_free+同 quota 内）。

    match(0) < mismatch(1), 能力匹配优先。
    """
    providers = [
        MockProvider("matched", "safe", True, 1000, True, 5.0),
        MockProvider("mismatched", "safe", True, 1000, False, 10.0),
    ]

    sorted_providers = sorted(providers)
    names = [p.name for p in sorted_providers]

    # capability_match 优先, 即使 model_strength 更小
    assert names == ["matched", "mismatched"]


def test_sort_keys_model_strength_priority():
    """测试 model_strength 第 5 优先级（同前 4 维都相同内）。

    越大越优先 (desc)。
    """
    providers = [
        MockProvider("strong_model", "safe", True, 1000, True, 10.0),
        MockProvider("weak_model", "safe", True, 1000, True, 5.0),
    ]

    sorted_providers = sorted(providers)
    names = [p.name for p in sorted_providers]

    # 强模型优先
    assert names == ["strong_model", "weak_model"]


def test_sort_keys_complex_scenario():
    """测试复杂场景: 多维度同时不同。

    验证字典序: ip_safe > is_free > quota > capability > strength
    """
    providers = [
        # A: safe+free+低quota+match+强 = 优先级 1
        MockProvider("A", "safe", True, 100, True, 10.0),
        # B: safe+free+低quota+mismatch+强 = A 后 (mismatch 后于 match)
        MockProvider("B", "safe", True, 100, False, 10.0),
        # C: safe+paid+高quota+match+强 = A 后 (paid 后于 free)
        MockProvider("C", "safe", False, 1000000, True, 10.0),
        # D: risky+free+高quota+match+强 = C 后 (risky 后于 safe)
        MockProvider("D", "risky", True, 1000000, True, 10.0),
    ]

    sorted_providers = sorted(providers)
    names = [p.name for p in sorted_providers]

    # 字典序: A > B > C > D
    assert names == ["A", "B", "C", "D"]
