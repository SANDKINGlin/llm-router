"""S1.5a 测试专用 fallback 编排参考实现(**非生产代码**)。

仅用于在 trace store + circuit breaker 组件层验证 hop 语义与 total_retry_budget 契约,
**不接入 app.py**(那是 S2.1 Cascade 的职责)。它串起已存在的生产组件:

⚠️ **S2.4 契约不同步**:生产 `Provider.complete()` 已改 3-tuple `(text, model, Usage|None)`,
但本 helper 的 `complete_fn` 闭包仍返 2-tuple `(text, model)`(自洽——orchestrator 只解包 2 元,
不调真 Provider.complete())。**勿将本 helper 外接真 Provider 子类**(签名不匹配);它是 sealed
测试工件,S2.1 后已标注待删。

    allow_request → provider_fn → is_complete → record_success/failure
        → acquire/commit(带 hop 归因)→ check_hop_budget → 下一 hop

S2.1 实现接入 app.py 的完整 Cascade 编排器(含协议适配/真实异常分类/streaming/
router-policy 解析)时,可逐行对照本 helper 的 hop 归因与 budget 控制流,之后删除本文件。

控制流要点(实现时已遵守,防 off-by-one):
  - depth = idx(即将尝试的 provider 在链中的序号);首跳 idx=0 = depth 0。
  - budget 门仅在 idx>0 时检查:check_hop_budget(idx, budget) False → 写 budget_exhausted
    终态归因后停止,**不调该 provider 的 complete_fn**。
  - 归因 reason 归属"下一跳":上一跳判定失败那一刻确定 reason,赋给即将尝试的 provider。
  - acquire 返回 REPLAYED(同 idempotency_key 已 commit)→ 直接返缓存,不 commit、不 advance
    (守幂等,防污染链)。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

from llm_router.resilience.circuit_breaker import CircuitBreaker, TripReason
from llm_router.resilience.content_integrity import is_complete
from llm_router.routing.hop import (
    DEFAULT_RETRY_BUDGET,
    HopAttribution,
    advance,
    budget_exhausted,
    check_hop_budget,
    initial_attribution,
)
from llm_router.store.trace import AcquireStatus, TraceStore

# provider 描述:(provider 名, key 名, complete_fn)。
# complete_fn: ()->(text, model),可抛异常(模拟硬失败)或返回残缺(模拟软失败)。
ProviderSpec = tuple[str, str, Callable[[], tuple[str, str]]]


@dataclass
class OrchestrationResult:
    final_text: Optional[str]
    final_model: Optional[str]
    success: bool
    hops_attempted: int  # 实际尝试过的 provider 数(不含被 budget 拦的)
    last_reason: str


async def run_fallback(
    store: TraceStore,
    breaker: CircuitBreaker,
    correlation_id: str,
    providers: list[ProviderSpec],
    budget: int = DEFAULT_RETRY_BUDGET,
) -> OrchestrationResult:
    """测试参考实现:在组件层跑通 fallback 闭环。

    返回 OrchestrationResult。成功→final_text/model + success=True;
    全失败或预算耗尽→success=False + last_reason。
    """
    parent_trace_id: Optional[str] = None
    prev_provider: Optional[str] = None
    last_reason = "initial"
    attempted = 0

    for idx, (provider, key, complete_fn) in enumerate(providers):
        # ① budget 门(首跳 idx=0 不过;后续跳变前检查,被拦则写终态停止)。
        if idx > 0 and not check_hop_budget(idx, budget):
            out = await store.acquire(
                correlation_id=correlation_id,
                idempotency_key=f"{correlation_id}#{idx}",
                provider=prev_provider or provider,
                parent_correlation_id=parent_trace_id,
            )
            await store.commit(
                trace_id=out.trace_id,
                result="",
                hop_attribution=budget_exhausted(idx, prev_provider or provider).to_json(),
            )
            return OrchestrationResult(
                final_text=None,
                final_model=None,
                success=False,
                hops_attempted=attempted,
                last_reason="budget_exhausted",
            )

        # ② 本跳归因(首跳 initial;之后 reason=上一跳失败原因,from=上一 provider)。
        attr: HopAttribution = (
            initial_attribution(provider)
            if idx == 0
            else advance(idx - 1, last_reason, prev_provider, provider)
        )

        # ③ acquire trace 行(parent 指上一跳,串 fallback 链)。
        out = await store.acquire(
            correlation_id=correlation_id,
            idempotency_key=f"{correlation_id}#{idx}",
            provider=provider,
            parent_correlation_id=parent_trace_id,
        )

        # ④ 幂等 replay:同 idempotency_key 已 commit → 返缓存,不计新 hop。
        if out.status is AcquireStatus.REPLAYED:
            return OrchestrationResult(
                final_text=out.cached_result,
                final_model=None,
                success=True,
                hops_attempted=attempted,
                last_reason="replayed",
            )

        attempted += 1

        # ⑤ breaker 判定:拒 → 记归因(本跳),记 reason 给下一跳,continue。
        dec = breaker.allow_request(provider, key)
        if not dec.allowed:
            await store.commit(
                trace_id=out.trace_id,
                result="",
                hop_attribution=attr.to_json(),
            )
            last_reason = dec.reason  # key_open / global_open / half_open_busy
            prev_provider = provider
            parent_trace_id = out.trace_id
            continue

        # ⑥ 放行 → 调 provider_fn。
        try:
            text, model = complete_fn()
        except Exception:
            breaker.record_failure(provider, key, TripReason.HARD)
            await store.commit(
                trace_id=out.trace_id,
                result="",
                hop_attribution=attr.to_json(),
            )
            last_reason = "hard_failure"
            prev_provider = provider
            parent_trace_id = out.trace_id
            continue

        # ⑦ 内容完整性:残缺 → 软失败(3 软 = 1 硬)。
        if not is_complete(text, model):
            breaker.record_failure(provider, key, TripReason.SOFT_CONTENT)
            await store.commit(
                trace_id=out.trace_id,
                result="",
                hop_attribution=attr.to_json(),
            )
            last_reason = "soft_content"
            prev_provider = provider
            parent_trace_id = out.trace_id
            continue

        # ⑧ 成功 → record_success + 落 result + 归因,返回。
        breaker.record_success(provider, key)
        await store.commit(
            trace_id=out.trace_id,
            result=text,
            hop_attribution=attr.to_json(),
        )
        return OrchestrationResult(
            final_text=text,
            final_model=model,
            success=True,
            hops_attempted=attempted,
            last_reason=attr.reason,
        )

    # provider 列表耗尽,无一成功(未触发 budget 门,因列表先结束)。
    return OrchestrationResult(
        final_text=None,
        final_model=None,
        success=False,
        hops_attempted=attempted,
        last_reason=last_reason,
    )
