"""S2.1a · EpsilonGreedy 路由策略(③ 路由层,RoutingStrategy 子类)。

core spec line 26-29:ε=0.3 起步、每 1000 次请求衰减 10%(×0.9)、下限 0.05。
design.md 约束#3(字典序非加权和):排序键 `(capability_match DESC, is_free DESC, 倍率 ASC)`。

S2.1a 落地(详见 design.md §S2.1 切片决策):
  - 排序键用 `(is_free DESC, cost_multiplier ASC)`;`capability_match` 槽位**预留但置常量**
    (② matcher S2.2 才给真值,S2.1a 时所有 candidate 视为等能力 → 不影响 is_free/cost 排序)。
  - ε 计数器**内存**(实例属性,进程重启 reset 到 0.3——重启后多探索,Phase1 可接受;
    持久化 defer S3+ bandit_state.db)。
  - 探索:prob ε → 从有序候选随机挑一个;利用:否则挑最优(ordered[0])。
    标准 ε-greedy,探索只在"首跳"发生,fallback 脊柱交给 ④ Cascade(S2.1b)按优先级序走。

注入(测试确定性,套 breaker `_jitter_fn` 模式):
  - chooser: ()->float∈[0,1),默认 random.random;返回值 < 当前 ε 则探索。
  - explorer: (k)->int∈[0,k),默认 random.randrange;探索时选 index。
"""
from __future__ import annotations

import random
from typing import Callable

from ..config import ProviderEntry
from .matcher import TierMatcher
from .strategy import RoutingStrategy

# core spec 默认值。
_EPSILON_START = 0.3
_DECAY_EVERY = 1000  # 每 N 次请求衰减一次
_DECAY_FACTOR = 0.9  # 每次衰减 ×0.9(衰减 10%)
_EPSILON_MIN = 0.05


class NoCandidateError(RuntimeError):
    """候选 provider 列表为空,无法选择。由调用层(④ Cascade S2.1b)处理。"""


class EpsilonGreedy(RoutingStrategy):
    """ε-greedy 策略:在按优先级排序的候选中,ε 概率随机探索,否则利用最优。

    长生命周期实例(计数器跨请求累积);select_provider 每调一次 +1 请求计数。
    """

    def __init__(
        self,
        entries: dict[str, ProviderEntry],
        *,
        epsilon_start: float = _EPSILON_START,
        decay_every: int = _DECAY_EVERY,
        decay_factor: float = _DECAY_FACTOR,
        epsilon_min: float = _EPSILON_MIN,
        chooser: Callable[[], float] | None = None,
        explorer: Callable[[int], int] | None = None,
        matcher: "TierMatcher | None" = None,
    ) -> None:
        self._entries = entries
        self._eps_start = epsilon_start
        self._decay_every = decay_every
        self._decay_factor = decay_factor
        self._eps_min = epsilon_min
        self._chooser: Callable[[], float] = chooser or random.random
        self._explorer: Callable[[int], int] = explorer or (lambda k: random.randrange(k))
        # ② 匹配层注入(测试确定性;Phase2 换 BgeMatcher 同接口,排序键逻辑不动)。
        self._matcher: TierMatcher = matcher or TierMatcher()
        self._requests = 0  # 内存计数器(重启 reset)

    def refresh_entries(self, entries: dict) -> None:
        """S4.3:apply_policy 同步更新排序键字典(防 stale 排序)。

        ponytail:仅替换 _entries,不动 _eps_*/_requests(运行时状态由 D7 决定是否保留,
        本切片保守只换数据)。
        """
        self._entries = entries

    def _epsilon(self) -> float:
        """当前 ε = max(下限, 起步 × 衰减因子^(请求//周期))。"""
        eps = self._eps_start * (self._decay_factor ** (self._requests // self._decay_every))
        return max(self._eps_min, eps)

    def _rank(self, name: str, task_type: str | None) -> tuple[bool, bool, float]:
        """字典序排序键(约束#3 routing-priority-principle,**非加权和**):
        (capability_match DESC, is_free DESC, 倍率 ASC)。

        - capability_match:② 匹配层 TierMatcher 判 provider tier 是否对口 task_type。
          对口排前(not matched=False);非对口落尾作 fallback(S2.2 软尾,不硬滤)。
        - task_type=None/未知 → 全对口(向后兼容 S2.1a,S2.1a 时 capability 槽置常量 True)。
        """
        entry = self._entries[name]
        matched = self._matcher.matches(entry.tier, task_type)
        return (not matched, not entry.is_free, entry.cost_multiplier)

    def _sort_key(self, name: str) -> tuple[bool, bool, float]:
        """S2.1a 单测用的 unary 入口(task_type=None → 全对口,顺序零变化)。

        返回 (False, not is_free, cost_multiplier):首槽全 False 不扰序,
        退化为 S2.1a 的 (is_free DESC, cost ASC)——test_epsilon_greedy 8 单测契约不变。
        """
        return self._rank(name, None)

    def plan(  # type: ignore[override]
        self, candidates: list[str], context: dict
    ) -> list[str]:
        """返回完整尝试链:ε 探索选 primary(每请求计 1 次,非每跳),其余按优先级序。

        单一排序源(_rank,含 S2.2 capability 首槽)+ 单一计数点(_requests)+ 零漂移
        (HERMES [CONSENSUS] 2026-06-15,优于初版 select_provider+fallback_order 双方法)。
        select_provider 继承 ABC 包装 = plan(...)[0]。task_type 从 context 取(无则全对口,向后兼容)。
        """
        if not candidates:
            raise NoCandidateError("候选 provider 列表为空,无法选择")
        missing = [c for c in candidates if c not in self._entries]
        if missing:
            raise ValueError(f"候选 {missing!r} 不在 entry map 中(配置不一致)")

        self._requests += 1  # 计入本次选择,驱动 ε 衰减(每请求一次,非每跳)

        task_type = context.get("task_type")  # ② 匹配层信号;无 → 全对口(S2.1a 顺序)
        ordered = sorted(candidates, key=lambda n: self._rank(n, task_type))

        # ε 概率探索:从有序候选随机挑 primary;否则利用最优 ordered[0]。
        if self._chooser() < self._epsilon():
            primary = ordered[self._explorer(len(ordered))]
        else:
            primary = ordered[0]
        # 尝试链:primary 在前,fallback 按优先级序(去掉 primary)。
        return [primary] + [c for c in ordered if c != primary]
