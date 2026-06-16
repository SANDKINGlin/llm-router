"""S1.3 · 可插拔路由策略接口(③ 路由层,design.md line 81)。

RoutingStrategy(ABC)是 Phase1 的契约层;后续切片作为子类注入此接口:
  - S2.1 EpsilonGreedy(ε=0.3→0.05 衰减,core spec line 26-29)
  - S2.9 能力匹配(Phase2)/ S3+ bandit

⚠ 本切片只建 ABC,不实现具体策略(YAGNI)。路由选择原则(字典序排序键
`capability_match DESC, is_free DESC, 倍率 ASC`,design.md 约束#3 / memory
`routing-priority-principle`)是**子类**的算子,**勿写进 ABC**——本接口只定通用契约:
candidates = 匹配层(② S2.2/S2.9)过滤后的 top-K 候选 provider name;
context = 请求上下文(session_id / task_type / retry_count 等)。
context 的键是**约定/可选**(子类按需读,如 ε-greedy 读 epsilon、bandit 读 arm 状态)——
基类不强制键集,避免与字典序选择算子(S2.1/S2.9 的职责)耦合。

S2.1b:ABC 契约从 select_provider(选 1)升为 **plan(返完整尝试链)**——Cascade(S2.1b)
需 primary + fallback 序,单方法零漂移零计数膨胀(HERMES [CONSENSUS] 2026-06-15)。
select_provider 保留为**具体便利包装**(= plan(...)[0]),S2.1a 的 8 单测行为零变化。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class RoutingStrategy(ABC):
    """可插拔路由策略接口(③ 路由层)。

    子类实现 plan() 返回完整尝试链(primary 在前,fallback 按优先级序)。
    select_provider() 是返链首的便利包装(保留 S2.1a 单测契约)。
    """

    @abstractmethod
    def plan(
        self, candidates: list[str], context: dict[str, Any]
    ) -> list[str]:
        """返回候选的完整尝试链(primary 在前,fallback 在后)。

        Args:
            candidates: 经匹配层(② S2.2/S2.9)过滤后的候选 provider name 列表(已 top-K)。
            context: 请求上下文(session_id / task_type / 已重试次数 等)。

        Returns:
            有序 provider name 列表(list[0] = primary,其余 = fallback 脊)。
            Cascade(④)按此序逐跳尝试。

        约定:若无法选出(如 candidates 为空),子类**应抛异常**而非返回空表,
        由调用层(④ Cascade)处理;本基类不规定具体异常类型。
        """
        raise NotImplementedError

    def select_provider(
        self, candidates: list[str], context: dict[str, Any]
    ) -> str:
        """便利包装:返回尝试链首(primary)。S2.1a 契约保留(行为零变化)。"""
        return self.plan(candidates, context)[0]
