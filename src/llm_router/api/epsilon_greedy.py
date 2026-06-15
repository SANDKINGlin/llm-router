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
    ) -> None:
        self._entries = entries
        self._eps_start = epsilon_start
        self._decay_every = decay_every
        self._decay_factor = decay_factor
        self._eps_min = epsilon_min
        self._chooser: Callable[[], float] = chooser or random.random
        self._explorer: Callable[[int], int] = explorer or (lambda k: random.randrange(k))
        self._requests = 0  # 内存计数器(重启 reset)

    def _epsilon(self) -> float:
        """当前 ε = max(下限, 起步 × 衰减因子^(请求//周期))。"""
        eps = self._eps_start * (self._decay_factor ** (self._requests // self._decay_every))
        return max(self._eps_min, eps)

    def _sort_key(self, name: str) -> tuple[bool, float]:
        """字典序排序键(约束#3 的 S2.1a 部分)。

        返回 (not is_free, cost_multiplier):free 排前(not is_free=False),
        同 free 组内 cost 升序。capability_match 槽位 defer S2.2(此处等能力)。
        """
        entry = self._entries[name]
        return (not entry.is_free, entry.cost_multiplier)

    def select_provider(  # type: ignore[override]
        self, candidates: list[str], context: dict
    ) -> str:
        if not candidates:
            raise NoCandidateError("候选 provider 列表为空,无法选择")
        missing = [c for c in candidates if c not in self._entries]
        if missing:
            raise ValueError(f"候选 {missing!r} 不在 entry map 中(配置不一致)")

        self._requests += 1  # 计入本次选择,驱动 ε 衰减

        ordered = sorted(candidates, key=self._sort_key)

        # ε 概率探索:从有序候选随机挑;否则利用最优 ordered[0]。
        if self._chooser() < self._epsilon():
            return ordered[self._explorer(len(ordered))]
        return ordered[0]
