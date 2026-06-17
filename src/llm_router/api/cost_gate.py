"""S2.4 · Cost Budget Gate(候选过滤层:超 token 配额的 provider 剔出 → 降级免费兜底)。

design 候选过滤家族(与 compliance 门 ① / health 过滤同属「候选筛选」,不破坏字典序排序——
routing-priority-principle:超预算本质是「可用性过滤」):路由前查 token_ledger.total(provider),
已消费 token ≥ provider.quota → 剔出候选(超预算=不可用);剩余(含 mock 兜底)正常路由 → 即
「超预算→降级 L0」(降级到免费/mock 兜底)。

fail-open:ledger 查询失败 → 返全候选(cost 是软约束,不崩请求,同 health fail-open 理念);
无 quota 配置的 provider(quotas 未登记)→ 视为无限放行。

与 Cascade 共享同一 LedgerStore 实例(Cascade 是 writer,CostGate 是 reader)。
"""
from __future__ import annotations

import logging
from typing import Optional

from ..store.token_ledger import LedgerStore

_LOG = logging.getLogger(__name__)


class CostGate:
    """超 token 预算过滤:ledger.total(name) 的 token ≥ quotas[name] → 剔出候选。

    quotas: name -> quota(ProviderEntry.quota,token 上限)。未登记的 name → 无限放行。
    ledger: 与 Cascade writer 共享的 LedgerStore(读已记账消费)。
    """

    def __init__(self, ledger: LedgerStore, quotas: dict[str, int]) -> None:
        self._ledger = ledger
        self._quotas = dict(quotas)

    @property
    def quotas(self) -> dict[str, int]:
        """name -> quota 配置(只读副本,供诊断)。"""
        return dict(self._quotas)

    def is_over_budget(self, name: str, consumed_tokens: int) -> bool:
        """纯函数判定(供测试/诊断,不查库):name 有 quota 且 consumed ≥ quota → True。

        无 quota 配置 → False(无限放行)。
        """
        quota = self._quotas.get(name)
        return quota is not None and consumed_tokens >= quota

    async def survivors(self, names: list[str]) -> list[str]:
        """返未超预算的 name 子集,保持原序。

        fail-open:ledger 查询失败 → 立即返全 names(cost 软约束,不崩请求——宁可放行多试一个,
        也别因记账库 hiccup 阻断路由)。无 quota 的 name → 无限放行。
        """
        result: list[str] = []
        for name in names:
            quota = self._quotas.get(name)
            if quota is None:
                result.append(name)  # 无 quota 配置 → 无限放行
                continue
            try:
                total = await self._ledger.total(name)
            except Exception:
                _LOG.warning(
                    "cost gate: ledger 查询 %s 失败 → fail-open 返全候选(软约束,不崩请求)",
                    name,
                    exc_info=True,
                )
                return list(names)
            consumed = total["prompt_tokens"] + total["completion_tokens"]
            if consumed < quota:
                result.append(name)
            # else: 超预算 → 剔出(降级到剩余免费/mock 兜底)
        return result

    async def consumed_tokens(self, name: str) -> Optional[int]:
        """查 name 已消费 token(prompt+completion)。无记录/查询失败 → None。供诊断/测试。"""
        try:
            total = await self._ledger.total(name)
        except Exception:
            return None
        return total["prompt_tokens"] + total["completion_tokens"]
