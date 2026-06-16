"""S1.3 · RoutingStrategy(ABC)可插拔路由接口(③ 路由层,design.md line 81)。

守两条验收:
  - 验收③:RoutingStrategy 是 ABC,直接实例化 → TypeError(不可实例化)
  - 验收④:子类可注入实现 select_provider,不同子类产生不同选择(接口可插拔)

⚠ 本切片只验 ABC 契约,不实现 ε-greedy(那是 S2.1,core spec line 26-29);
桩子类定义在测试内(不在生产代码留 stub,守 YAGNI)。
TDD:本套件先 RED——api/strategy.py 未建时 import 即失败。
"""
from __future__ import annotations

import pytest

from llm_router.api.strategy import RoutingStrategy


def test_routing_strategy_is_abstract():
    """验收③:RoutingStrategy 是 ABC,直接实例化 → TypeError。"""
    with pytest.raises(TypeError):
        RoutingStrategy()  # type: ignore[abstract]


def test_strategy_subclass_injectable():
    """验收④:子类可注入,不同实现产生不同选择(接口可插拔)。"""

    class FirstCandidate(RoutingStrategy):
        """桩子类:总选第一个候选(S1.3 只验可注入,非真实策略)。"""

        def plan(self, candidates, context):  # type: ignore[override]
            return list(candidates)  # 原序:select_provider()=plan()[0] 取首

    class LastCandidate(RoutingStrategy):
        """桩子类:总选最后一个候选。"""

        def plan(self, candidates, context):  # type: ignore[override]
            return list(reversed(candidates))  # 倒序:plan()[0] 取末

    candidates = ["alpha", "beta", "gamma"]
    ctx = {"session_id": "s1", "task_type": "chat"}

    first = FirstCandidate().select_provider(candidates, ctx)
    last = LastCandidate().select_provider(candidates, ctx)

    assert first == "alpha"
    assert last == "gamma"
    # 同接口,不同子类 → 不同选择:证明可插拔注入
    assert first != last
