"""S2.2 · 粗档位能力匹配(② 匹配层,Phase1)。

Phase1 用 tier(strong/medium/fast)粗档位 + task_type 关键词做"能力下限"匹配:
provider 能力 ≥ 任务所需 → 对口。strong 能向下兼容(覆盖所有任务档);fast 只覆盖轻任务。
Phase2 由 bge-small 向量细粒度匹配替代(specs/capability-matching/spec.md,BgeMatcher 同接口)。

匹配语义(约束#3 routing-priority-principle 第一槽 capability_match):
  - tier_rank: fast=1 < medium=2 < strong=3(数字大 = 能力强,能向下兼容)
  - task_type → required_rank(能力下限):
      code/coding/编程/补全     → 1(任意 tier 能干)
      general/chat/对话/翻译    → 2(需 medium+)
      reasoning/math/推理       → 3(需 strong)
      None/""/未知              → 1(全 matches——向后兼容 S2.1a 空 ctx 顺序零变化)
  - matches iff tier_rank(provider) >= required_rank(task_type)

无状态纯函数:Phase1 用 TierMatcher;Phase2 换 BgeMatcher(同 matches(tier, task_type)->bool 接口,
epsilon_greedy 注入即可,不动排序键逻辑)。
"""
from __future__ import annotations

# tier → 能力 rank(数字大 = 能力强,能向下兼容低档任务)。
_TIER_RANK: dict[str, int] = {"fast": 1, "medium": 2, "strong": 3}

# task_type 关键词 → 能力下限(rank)。**从高到低**匹配(命中更强关键词者优先,
# 过配不过欠配:task_type 同时含多个语义时取更严要求)。子串匹配,归一小写。
_TASK_FLOOR: dict[int, tuple[str, ...]] = {
    3: ("reasoning", "reason", "complex", "math", "推理"),
    2: ("general", "chat", "conversation", "translate", "翻译", "summar", "对话"),
    1: ("code", "coding", "编程", "补全", "fast"),
}


class TierMatcher:
    """粗档位能力匹配器(Phase1,无状态)。

    matches(tier, task_type) 纯函数。Phase2 换 BgeMatcher 同接口。
    """

    def matches(self, tier: str, task_type: str | None) -> bool:
        """provider 的 tier 能力是否 ≥ task_type 所需下限 → 对口。"""
        prov_rank = _TIER_RANK.get(tier, 1)  # 未知 tier fail-open 当 fast(policy Literal 已校验)
        required = self._required_rank(task_type)
        return prov_rank >= required

    def _required_rank(self, task_type: str | None) -> int:
        """task_type → 能力下限 rank。无/未知 → 1(全 matches,向后兼容)。"""
        if not task_type:
            return 1
        norm = task_type.strip().lower()
        if not norm:
            return 1
        for rank, keywords in _TASK_FLOOR.items():  # 从高到低,首个命中即取
            if any(kw in norm for kw in keywords):
                return rank
        return 1  # 未知 task_type → 全 matches(向后兼容)
