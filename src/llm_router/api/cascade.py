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
import logging
from dataclasses import dataclass
from typing import Optional

from ..config import policy
from ..providers.base import Provider, ProviderError, Usage
from ..resilience.circuit_breaker import CircuitBreaker, CircuitState, TripReason
from ..resilience.content_integrity import is_complete
from ..store.health_store import HealthStore
from ..store.token_ledger import LedgerStore
from ..routing.hop import (
    DEFAULT_RETRY_BUDGET,
    advance,
    budget_exhausted,
    check_hop_budget,
    initial_attribution,
)
from ..store.trace import AcquireStatus, TraceStore
from .cost_gate import CostGate
from .gray import gray_release
from .policy_enforcer import ComplianceError, PolicyEnforcer
from .strategy import RoutingStrategy

_LOG = logging.getLogger(__name__)


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
        health_store: Optional[HealthStore] = None,
        policy_enforcer: Optional[PolicyEnforcer] = None,
        ledger: Optional[LedgerStore] = None,
        cost_gate: Optional[CostGate] = None,
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
        self._health_store = health_store  # S2.8c:可选,路由前 hard-skip 死亡 key
        self._policy_enforcer = policy_enforcer  # S2.7:可选,合规门(layer ①,最先)
        self._ledger = ledger  # S2.4:可选,token 用量记账(writer);与 cost_gate 共享实例
        self._cost_gate = cost_gate  # S2.4:可选,路由前剔超 token 预算的候选(降级)
        self._store_ready = False
        self._init_lock = asyncio.Lock()

    @property
    def health_store(self) -> Optional[HealthStore]:
        """S2.8c:暴露 health_store(lifespan 用同一实例 init + 喂 prober,共享单例)。"""
        return self._health_store

    async def _ensure_store(self) -> None:
        """惰性幂等 init store(双重检查锁 + asyncio.Lock,并发安全)。

        init 本身幂等(CREATE TABLE IF NOT EXISTS);无论 FastAPI lifespan 是否跑都安全
        (TestClient 模块级不触发 lifespan,lazy 兜底;真服务器首请求前完成 init)。
        S2.4:同时 init ledger(若挂了)——writer(Cascade)+ reader(CostGate)共享同一实例。
        """
        if self._store_ready:
            return
        async with self._init_lock:
            if not self._store_ready:
                await self._store.init()
                if self._ledger is not None:
                    await self._ledger.init()
                self._store_ready = True

    async def _record_usage(
        self, name: str, model: str, usage: Usage | None
    ) -> None:
        """S2.4:把 complete() 的 usage 落 token_ledger(best-effort)。

        usage=None(mock/未报)→ 跳过。ledger 写失败 → log warning 不崩请求
        (cost 是软约束,记账失败不该阻断路由;同 health fail-open 理念)。
        无论内容是否完整,token 已消耗,故在 is_complete 判定**前**记。
        """
        if usage is None or self._ledger is None:
            return
        try:
            await self._ledger.record(
                provider=name,
                model=model,
                prompt_tokens=usage.prompt_tokens,
                completion_tokens=usage.completion_tokens,
                cost=usage.cost,
            )
        except Exception:
            _LOG.warning(
                "token_ledger 记账失败(provider=%s)→ 跳过(软约束,不崩请求)",
                name,
                exc_info=True,
            )

    async def _surviving_candidates(self) -> list[str]:
        """路由前候选过滤(全部 fail-open,非权威信号不崩请求):

        - S2.8c health:hard-skip health.db 中 alive=False 的 key(从未探活保留=无信号不过滤)。
        - S2.4 cost:剔超 token 预算的 provider(降级到剩余免费/mock 兜底)。

        两过滤均先于 strategy.plan() 字典序排序(只幸存者进排序池)。任一查询失败 → fail-open
        返全候选(health/cost 都是非权威软信号,读不到不该崩请求)。
        """
        if self._health_store is None:
            survivors = list(self._candidate_names)
        else:
            try:
                rows = await self._health_store.latest_probe(
                    providers=self._candidate_names
                )
            except Exception:
                _LOG.warning(
                    "health_store 查询失败 → fail-open 不过滤(非权威信号,不崩请求)",
                    exc_info=True,
                )
                survivors = list(self._candidate_names)
            else:
                dead = {r.provider for r in rows if not r.alive}
                survivors = [n for n in self._candidate_names if n not in dead]
        # S2.4 cost gate:超预算剔出(fail-open,见 CostGate.survivors)。
        if self._cost_gate is not None:
            survivors = await self._cost_gate.survivors(survivors)
        return survivors

    def feed_probe_success(self, name: str) -> None:
        """S2.8c Face 3 / spec Req 3b:探活成功喂 HALF_OPEN 加速恢复。

        **仅当 key 处于 HALF_OPEN** 才 record_success(→CLOSED);OPEN 未到期**不动**(Req 3a:
        探活不得强制关未到期 OPEN——CB 退避窗口权威,裁决 CB 先判→探活后过滤)。未知 name → noop。
        Cascade 拥有 breaker + name→key 映射,故 CB 喂养逻辑归此(prober 只报活,不判断 CB)。
        """
        entry = self._providers.get(name)
        if entry is None:
            return
        _provider, key = entry
        ks = self._breaker.get_key_state(name, key)
        if ks.state == CircuitState.HALF_OPEN:
            self._breaker.record_success(name, key)

    async def run(
        self,
        prompt: str,
        *,
        correlation_id: str,
        session_id: str | None = None,
    ) -> CascadeResult:
        """按 strategy.plan() 序跑 fallback 链。返回 CascadeResult。

        路由前:S2.8c hard-skip health.db 中 alive=False 的 key(_surviving_candidates),
        幸存者才进 plan() 字典序排序(spec Req 4)。每跳:acquire trace → 幂等 replay 返缓存
        → breaker 判(CB 先于探活,Req 3a)→ provider.complete → ProviderError(HARD)/
        is_complete False(SOFT_CONTENT)/ 成功。budget 门拦第 7 跳。

        最先:S2.7 合规门(layer ①)——配置非合规(同 provider 多账号)→ 拒绝路由,
        不 init store、不 plan、不调 provider(check() 内已记合规日志)。

        session_id(S4.1 灰度,可选):派生自 X-Session-Id / api_key(见 app.py)。None → 不判定。
        Phase1 范围 A:gray_release 结果只 log + 进 strategy context(**不改链逻辑**——路由层
        内只一策略,无新旧切换行为;真实行为差异在接入层 D9)。D7/Phase2 多版本时 strategy 读
        context["gray_released"] 驱动版本切换。不进排序键(routing-priority-principle 不动)。
        """
        if self._policy_enforcer is not None:
            try:
                self._policy_enforcer.check()
            except ComplianceError:
                _LOG.warning(
                    "routing rejected by compliance gate (same-provider multi-account)"
                )
                return CascadeResult(None, None, False, 0, "compliance_blocked")
        await self._ensure_store()
        survivors = await self._surviving_candidates()
        if not survivors:
            # 全候选死亡或候选为空 → fail-loud 明确失败(对抗审 MED),不依赖"mock 恒存活"隐式
            # 契约,也不让 plan([]) 抛 opaque NoCandidateError(否则请求 500 + 难诊断)。
            return CascadeResult(None, None, False, 0, "no_candidates")
        # S4.1 灰度判定(机制就位,可观测):session_id 派生 + gray_release 进 context。
        # Phase1 范围 A:released 只 log + 进 context,**不改链逻辑**(路由层内只一策略,
        # 无"新旧切换"行为;真实行为差异在接入层 cc-switch/D9)。D7/Phase2 多版本时 strategy
        # 读 context["gray_released"] 驱动版本切换。不进排序键(routing-priority-principle 不动)。
        context: dict = {"session_id": session_id}
        if session_id is not None:
            pol = policy()
            released = gray_release(session_id, pol.gray_percent)
            context["gray_released"] = released
            _LOG.info(
                "gray decision: session_id=%s gray_percent=%d released=%s policy_version=%s",
                session_id,
                pol.gray_percent,
                released,
                pol.policy_version,
            )
        chain = self._strategy.plan(survivors, context)

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
            #   S2.4:complete 返 3-tuple(text, model, usage)。
            try:
                text, model, usage = await provider.complete(prompt)
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

            # ⑥.5 S2.4:token 用量记账(best-effort)。token 已消耗,先于完整性判定记。
            await self._record_usage(name, model, usage)

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
