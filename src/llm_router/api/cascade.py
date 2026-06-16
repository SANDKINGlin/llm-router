"""S2.1b · 生产 Cascade(④ 回退层,design.md line 52/82)。

从 tests/e2e/_fallback_orchestrator.py(被 10 条 E2E 证明可用的最小子集)提炼成生产编排器。
串起真实组件:breaker + routing.hop + content_integrity + store + strategy.plan()。

与 orchestrator 的差异(生产化):
  - 吃 (name, Provider, key) 三元组,调 await provider.complete(prompt)(orchestrator 吃 complete_fn 闭包)。
  - 链序由 strategy.plan() 决定(orchestrator 吃调用方给的序)。
  - except **ProviderError** → record_failure(HARD)(orchestrator 裸 except Exception;生产缩窄防吞 bug)。
  - 惰性幂等 init store(asyncio.Lock 双重检查,robust 无论 FastAPI lifespan 是否跑)。

接线契约(锁,见 S1.5a orchestrator + S1.6 REPAIR):每跳
  allow_request → await provider.complete() → is_complete → record_success/failure(TripReason)
  → store.acquire/commit(hop_attribution) → check_hop_budget。
REPLAYED(幂等)直接返缓存不计新 hop;budget=6,第 7 跳(depth 6)被拦写 budget_exhausted 终态。
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Optional

from ..providers.base import Provider, ProviderError
from ..resilience.circuit_breaker import CircuitBreaker, TripReason
from ..resilience.content_integrity import is_complete
from ..routing.hop import (
    DEFAULT_RETRY_BUDGET,
    advance,
    budget_exhausted,
    check_hop_budget,
    initial_attribution,
)
from ..store.trace import AcquireStatus, TraceStore
from .strategy import RoutingStrategy


@dataclass
class CascadeResult:
    """单次 Cascade 编排结果。"""

    final_text: Optional[str]
    final_model: Optional[str]
    success: bool
    hops_attempted: int  # 实际调用过 provider.complete 的跳数(不含被 budget 拦的)
    last_reason: str


class Cascade:
    """生产 fallback 编排器:按 strategy.plan() 序逐跳尝试,失败按契约回退。

    长生命周期实例(hold store/breaker/strategy/candidates);run() 每请求一次。
    """

    def __init__(
        self,
        store: TraceStore,
        breaker: CircuitBreaker,
        strategy: RoutingStrategy,
        candidates: list[tuple[str, Provider, str]],
        *,
        budget: int = DEFAULT_RETRY_BUDGET,
    ) -> None:
        self._store = store
        self._breaker = breaker
        self._strategy = strategy
        # name -> (Provider, key);breaker 按 (provider, key) 记账。
        self._providers: dict[str, tuple[Provider, str]] = {
            name: (prov, key) for name, prov, key in candidates
        }
        self._candidate_names: list[str] = [name for name, _p, _k in candidates]
        self._budget = budget
        self._store_ready = False
        self._init_lock = asyncio.Lock()

    async def _ensure_store(self) -> None:
        """惰性幂等 init store(双重检查锁 + asyncio.Lock,并发安全)。

        init 本身幂等(CREATE TABLE IF NOT EXISTS);无论 FastAPI lifespan 是否跑都安全
        (TestClient 模块级不触发 lifespan,lazy 兜底;真服务器首请求前完成 init)。
        """
        if self._store_ready:
            return
        async with self._init_lock:
            if not self._store_ready:
                await self._store.init()
                self._store_ready = True

    async def run(
        self,
        prompt: str,
        *,
        correlation_id: str,
    ) -> CascadeResult:
        """按 strategy.plan() 序跑 fallback 链。返回 CascadeResult。

        每跳:acquire trace → 幂等 replay 返缓存 → breaker 判 → provider.complete
        → ProviderError(HARD)/ is_complete False(SOFT_CONTENT)/ 成功。budget 门拦第 7 跳。
        """
        await self._ensure_store()
        chain = self._strategy.plan(self._candidate_names, {})

        parent_trace_id: Optional[str] = None
        prev_provider: Optional[str] = None
        last_reason = "initial"
        attempted = 0

        for idx, name in enumerate(chain):
            # ① budget 门(首跳 idx=0 不过;后续跳变前检查,被拦则写终态停止)。
            if idx > 0 and not check_hop_budget(idx, self._budget):
                gate_provider = prev_provider or name
                out = await self._store.acquire(
                    correlation_id=correlation_id,
                    idempotency_key=f"{correlation_id}#{idx}",
                    provider=gate_provider,
                    parent_correlation_id=parent_trace_id,
                )
                await self._store.commit(
                    trace_id=out.trace_id,
                    result="",
                    hop_attribution=budget_exhausted(idx, gate_provider).to_json(),
                )
                return CascadeResult(
                    None, None, False, attempted, "budget_exhausted"
                )

            # ② 本跳归因(首跳 initial;之后 reason=上一跳失败原因,from=上一 provider)。
            attr = (
                initial_attribution(name)
                if idx == 0
                else advance(idx - 1, last_reason, prev_provider, name)
            )

            # ③ acquire trace 行(parent 指上一跳,串 fallback 链)。
            out = await self._store.acquire(
                correlation_id=correlation_id,
                idempotency_key=f"{correlation_id}#{idx}",
                provider=name,
                parent_correlation_id=parent_trace_id,
            )

            # ④ 幂等 replay:同 idempotency_key 已 commit → 返缓存,不计新 hop。
            if out.status is AcquireStatus.REPLAYED:
                return CascadeResult(
                    out.cached_result, None, True, attempted, "replayed"
                )

            attempted += 1

            provider, key = self._providers[name]

            # ⑤ breaker 判定:拒 → 记归因(本跳),记 reason 给下一跳,continue。
            dec = self._breaker.allow_request(name, key)
            if not dec.allowed:
                await self._store.commit(
                    trace_id=out.trace_id,
                    result="",
                    hop_attribution=attr.to_json(),
                )
                last_reason = dec.reason  # key_open / global_open / half_open_busy
                prev_provider = name
                parent_trace_id = out.trace_id
                continue

            # ⑥ 放行 → 调 provider(真 HTTP 经 adapter)。ProviderError → HARD。
            try:
                text, model = await provider.complete(prompt)
            except ProviderError:
                self._breaker.record_failure(name, key, TripReason.HARD)
                await self._store.commit(
                    trace_id=out.trace_id,
                    result="",
                    hop_attribution=attr.to_json(),
                )
                last_reason = "hard_failure"
                prev_provider = name
                parent_trace_id = out.trace_id
                continue
            # 非 ProviderError(编程 bug)上抛不吞——不 trip 熔断、不掩盖(design 点2 DEFEND)。

            # ⑦ 内容完整性:残缺 → 软失败(3 软 = 1 硬)。
            if not is_complete(text, model):
                self._breaker.record_failure(name, key, TripReason.SOFT_CONTENT)
                await self._store.commit(
                    trace_id=out.trace_id,
                    result="",
                    hop_attribution=attr.to_json(),
                )
                last_reason = "soft_content"
                prev_provider = name
                parent_trace_id = out.trace_id
                continue

            # ⑧ 成功 → record_success + 落 result + 归因,返回。
            self._breaker.record_success(name, key)
            await self._store.commit(
                trace_id=out.trace_id,
                result=text,
                hop_attribution=attr.to_json(),
            )
            return CascadeResult(text, model, True, attempted, attr.reason)

        # 链耗尽,无一成功(未触发 budget 门,因列表先结束)。
        return CascadeResult(None, None, False, attempted, last_reason)
