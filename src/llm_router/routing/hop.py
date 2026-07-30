"""S1.5a · hop 语义(conditional 边界跳变)+ total_retry_budget 约束。

权威契约(specs/llm-routing-core/spec.md):
  - "hop SHALL 定义为 conditional 边界跳变(非节点);hop_depth 入 trace;max_hop 按条件边界计数"
  - "路由层 SHALL 对单请求施加共享总重试预算 total_retry_budget=6(每 hop 共享)"

hop_depth 语义(本切片定,B-1 精度点):
  - 首个被尝试的 provider = hop_depth 0(reason="initial",from=None)。
  - 每次"放弃当前 provider、决定尝试下一个"的**条件边界跳变** → depth += 1。
    跳变触发源:(a) breaker 拒(allow_request 返 False);(b) provider 硬失败(超时/5xx/异常);
    (c) 内容残缺(is_complete 返 False)。**被熔断直接跳过的 provider 也算 1 跳**
    (它确实跨了一个条件边界:从"考虑它"到"放弃它")——这正是 spec"非节点"的本意。
  - reason 在"判定失败那一刻"确定,归属到**下一跳的归因**(解释"为何来到这里")。

budget 语义:
  - total_retry_budget=6 → 最多 6 个 provider 被实际尝试(depth 0..5);
    第 7 个(depth 6)被 check_hop_budget 拦下,写一条 budget_exhausted 终态归因后停止。
  - 最坏请求次数有界 = 6(spec Scenario: 嵌套 fallback 有界)。

hop_attribution 字段是 trace 表的 TEXT 列(已在 S1.1 建 schema),存紧凑 JSON 串
(非纯 depth 字符串),保留 reason/from/to 供 S3+ bandit 与运维观测。

归因逻辑放本模块(纯函数,零 IO、零依赖);trace store 的 commit() 只负责
"caller 给字符串就落库",保持 dumb(职责分离)。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Optional

# spec: total_retry_budget=6(每 hop 共享,非每节点 ×3)。
DEFAULT_RETRY_BUDGET = 6

# 跳变原因闭集(parse/advance 共用,防拼写漂移)。
HOP_REASONS: frozenset[str] = frozenset(
    {
        "initial",  # 首跳(无前置失败)
        "key_open",  # 上一 provider 的 key 熔断(allow 拒)
        "global_open",  # 全局冻结
        "half_open_busy",  # 半开窗口已放探测
        "hard_failure",  # 上一 provider 硬失败(超时/5xx/异常)
        "soft_content",  # 上一 provider 内容残缺(软失败)
        "budget_exhausted",  # 重试预算耗尽,链终止
        "rate_limited",  # 上一 provider 返 429(归 hard_failure 类)
        "provider_removed_during_rollback",  # provider 在 rollback 中被移除
    }
)


@dataclass(frozen=True)
class HopAttribution:
    """一次 hop 的归因快照。to_json() 落库,parse_attribution() 反解析。

    reason 语义见模块 docstring。from_provider 为 None 仅合法于首跳(initial);
    to_provider 为 None 仅合法于终态(budget_exhausted)。
    """

    depth: int
    reason: str
    from_provider: Optional[str]
    to_provider: Optional[str]

    def to_json(self) -> str:
        """紧凑 JSON 串(落库到 trace.hop_attribution TEXT 列)。"""
        return json.dumps(
            {
                "depth": self.depth,
                "reason": self.reason,
                "from": self.from_provider,
                "to": self.to_provider,
            },
            separators=(",", ":"),
            sort_keys=False,
        )


def initial_attribution(to_provider: str) -> HopAttribution:
    """首个 provider 的归因(depth=0, reason=initial, from=None)。"""
    return HopAttribution(
        depth=0, reason="initial", from_provider=None, to_provider=to_provider
    )


def advance(
    current_depth: int,
    reason: str,
    from_provider: str,
    to_provider: str,
) -> HopAttribution:
    """条件边界跳变:返回 depth+1 的新归因。

    Args:
        current_depth: 上一跳的 depth(新跳 = current_depth + 1)。
        reason: 为何跳变(必须 ∈ HOP_REASONS,且非 initial/budget_exhausted 这两个
            特殊态——initial 只能由 initial_attribution 产出,budget_exhausted 由
            budget_exhausted() 产出)。
        from_provider: 被放弃的 provider。
        to_provider: 即将尝试的 provider。
    """
    assert reason in HOP_REASONS, f"unknown reason: {reason}"
    if reason in ("initial", "budget_exhausted"):
        raise ValueError(f"{reason!r} 是特殊态,不能由 advance 产出")
    return HopAttribution(
        depth=current_depth + 1,
        reason=reason,
        from_provider=from_provider,
        to_provider=to_provider,
    )


def budget_exhausted(depth: int, from_provider: str) -> HopAttribution:
    """预算耗尽终态归因:不再尝试下一 provider(to=None)。"""
    return HopAttribution(
        depth=depth,
        reason="budget_exhausted",
        from_provider=from_provider,
        to_provider=None,
    )


def check_hop_budget(current_depth: int, budget: int = DEFAULT_RETRY_BUDGET) -> bool:
    """是否仍在重试预算内可继续跳变。

    语义:current_depth 是**即将尝试的 provider 的 depth**(= 已发生跳变数)。
    budget=6 允许 depth 0..5(6 个 provider 被尝试);depth 6 返 False → 拦下、写终态。
    对应 spec"总重试次数 ≤ 6":最坏 6 次 provider 调用(首跳 depth=0 不算 retry,
    其余 depth 1..5 = 5 次跳变;第 6 次跳变即 depth 6 被拦)。
    """
    return current_depth < budget


def parse_attribution(raw: Optional[str]) -> Optional[HopAttribution]:
    """从 trace 行的 hop_attribution TEXT 反解析(测试/运维用)。

    None → None;非法 JSON → 抛(不静默吞,便于测试发现格式漂移)。
    """
    if raw is None:
        return None
    d = json.loads(raw)
    return HopAttribution(
        depth=d["depth"],
        reason=d["reason"],
        from_provider=d.get("from"),
        to_provider=d.get("to"),
    )
