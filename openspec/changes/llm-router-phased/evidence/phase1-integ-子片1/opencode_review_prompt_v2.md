# OpenCode 异构对抗审查 — Phase1 集成验证·子片 1（v2 自包含版）

> 你是异构对抗审查者。任务：用 HERMES 标签协议审查测试文件，对抗式找漏（"假绿"、覆盖盲区、逻辑矛盾），不是夸；最后必须给可执行结论。

## ⚠️ 自包含约束

**所有需要的源码已嵌入下方 §源码锚点**。**禁止再 Read/Glob/Grep 其他源文件**。仅基于本文件的内容审查并直接输出发现。如确实需要某个未嵌入的细节，**写在 [DEADLOCK]** 里说明缺哪段，不要去探查。

## HERMES 标签协议（必须用）

每条发现必须以下列标签之一开头：
- `[CHALLENGE] <严重度> <file:line> <问题> <可复现反例>`（"如果改成 X 输入，测试应败但会绿"）
- `[CONSENSUS]` 维度收敛认可，无 CHALLENGE
- `[DEADLOCK]` 多轮无法收敛 / 缺关键源码

严重度：CRITICAL（出厂门假绿）/ HIGH（关键场景遗漏）/ MED（强化建议）/ LOW（风格）。

**整文件结论**单独一行（最后）：
- `[CONSENSUS] 子片 1 测试可作 Phase 1 出厂回归门`（无 CRITICAL/HIGH）
- `[CHALLENGE] 子片 1 测试存在 N 项 CRITICAL/HIGH，需修复后重审`

## 项目背景

智能路由层 Phase 1（`~/projects/llm-router`），4 个 CRITICAL BUG 已修复（多切片完成）：

| BUG-id | 描述 | 修复切片 | 关键代码锚点（绝对项目路径） |
|---|---|---|---|
| BUG-FR-01 | Cascade 总重试无上界 | S2.1b（budget=6） | `src/llm_router/api/cascade.py:280-296` budget 门 |
| BUG-FR-02 | 嵌套 fallback hop 语义未定义 | S1.5a（hop_attribution + advance） | `src/llm_router/api/cascade.py:298-303, 339-368` + `src/llm_router/routing/hop.py` |
| BUG-policy-01 | 同 provider 多账号薅羊毛 | S2.7（policy_enforcer.check） | `src/llm_router/api/cascade.py:242-249` layer ① compliance |
| BUG-fallback-01 | 切换前未知存活状态 | S2.8（health_store + hard-skip） | `src/llm_router/api/cascade.py:251-255` `_surviving_candidates` |

## 子片 1 设计意图

| 现有测试 | 与子片 1 区别 |
|---|---|
| `tests/unit/test_cascade.py` | 组件层单测，只组装 store+breaker+strategy |
| `tests/unit/test_policy_enforcer.py` | 纯 PolicyEnforcer 逻辑 |
| `tests/unit/health/test_*.py` | 纯 HealthStore 测 |
| `tests/e2e/test_fallback_e2e.py` | orchestrator 测试 helper（非生产 Cascade） |
| **子片 1（本文件）** | **多组件全开生产 Cascade**：policy_enforcer + health_store + breaker + cost_gate + epsilon strategy 同时启用 |

## 审重点（必查 4 项 + 任意自由发掘）

1. **覆盖度**：4 测试是否真覆盖 4 BUG 的修复点？反查下方 §源码锚点 实际行号，断言是否落在修复行附近？
2. **假绿陷阱**：`calls` 计数 / `_store_ready` 私有属性 / 链长 / `from/to_provider` 等断言能否被"虚假实现"绕过？比如 BUG-FR-01 测试若 cascade 改成"调 7 次但最后 1 次返失败"也能通过吗？
3. **关键场景遗漏**：除已 defer 到子片 3 的（budget+health 同触发 / 合规 vs cost_gate 顺序 / exact quota 边界 / quota=0 / compliance×cost 顺序）之外，**还有什么本应在子片 1 出厂门里但未覆盖**？
4. **`quota=1_000_000` 旁路 cost_gate 意图**：`_entry()` helper 默认 `quota=1_000_000` 是否真能让 cost_gate 不干扰？还是隐式被 `is_free=True / cost_multiplier=0.0` 救场？这层旁路若被未来重构破坏，测试会假绿吗？

**额外要求**：
- 反例必须可执行（不要只说"可能"），格式："如果把 cascade.py 的 X 改成 Y，本测试应败但会绿"
- 引用必须 `file:line`（不要"某个地方"）

---

## §源码锚点 ① — `src/llm_router/api/cascade.py:220-396` 的 `Cascade.run()` 关键段

```python
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
        """
        if self._policy_enforcer is not None:                          # line 242
            try:
                self._policy_enforcer.check()
            except ComplianceError:
                _LOG.warning(
                    "routing rejected by compliance gate (same-provider multi-account)"
                )
                return CascadeResult(None, None, False, 0, "compliance_blocked")  # line 249
        await self._ensure_store()                                     # line 250
        survivors = await self._surviving_candidates()                 # line 251
        if not survivors:
            return CascadeResult(None, None, False, 0, "no_candidates")
        # ... gray release 略 ...
        chain = self._strategy.plan(survivors, context)                # line 272

        parent_trace_id: Optional[str] = None
        prev_provider: Optional[str] = None
        last_reason = "initial"
        attempted = 0

        for idx, name in enumerate(chain):                             # line 279
            # ① budget 门(首跳 idx=0 不过;后续跳变前检查,被拦则写终态停止)。
            if idx > 0 and not check_hop_budget(idx, self._budget):    # line 281
                gate_provider = prev_provider or name
                out = await self._store.acquire(...)
                await self._store.commit(
                    trace_id=out.trace_id,
                    result="",
                    hop_attribution=budget_exhausted(idx, gate_provider).to_json(),
                )
                return CascadeResult(None, None, False, attempted, "budget_exhausted")

            # ② 本跳归因(首跳 initial;之后 reason=上一跳失败原因,from=上一 provider)。
            attr = (                                                   # line 299
                initial_attribution(name)
                if idx == 0
                else advance(idx - 1, last_reason, prev_provider, name)
            )

            # ③ acquire trace 行
            out = await self._store.acquire(...)                       # line 306

            # ④ 幂等 replay
            if out.status is AcquireStatus.REPLAYED: ...               # line 314

            attempted += 1                                              # line 319

            # S4.3 KeyError 兜底
            try:
                provider, key = self._providers[name]                  # line 324
            except KeyError:
                # 记 provider_removed_during_rollback 归因 + continue
                ...

            # ⑤ breaker
            dec = self._breaker.allow_request(name, key)               # line 341
            if not dec.allowed:
                await self._store.commit(..., hop_attribution=attr.to_json())
                last_reason = dec.reason  # key_open / global_open / half_open_busy
                prev_provider = name
                parent_trace_id = out.trace_id
                continue

            # ⑥ 调 provider
            try:
                text, model, usage = await provider.complete(prompt)   # line 356
            except ProviderError:                                       # line 357
                self._breaker.record_failure(name, key, TripReason.HARD)
                await self._store.commit(..., hop_attribution=attr.to_json())
                last_reason = "hard_failure"
                prev_provider = name
                parent_trace_id = out.trace_id
                continue

            # ⑥.5 token 记账(best-effort)
            await self._record_usage(name, model, usage)                # line 371

            # ⑦ 内容完整性:残缺 → 软失败(3 软 = 1 硬)
            if not is_complete(text, model):                            # line 374
                self._breaker.record_failure(name, key, TripReason.SOFT_CONTENT)
                ...
                last_reason = "soft_content"
                continue

            # ⑧ 成功
            self._breaker.record_success(name, key)                     # line 387
            ...
```

**`_surviving_candidates`（cascade.py 内）语义**：从 `health_store` 读 `alive=False` 的 (provider, key)，从 `self._providers` 候选集中剔除；返回剩余 provider 名列表。

**`__init__` 关键参数**（cascade.py:80-150 区间）：`Cascade(store, breaker, strategy, candidates, *, health_store=None, policy_enforcer=None, ledger=None, cost_gate=None, budget=DEFAULT_RETRY_BUDGET=6)`；`self._store_ready = False`；`_ensure_store()` 是惰性 init（首次跑设 `_store_ready=True`）。

## §源码锚点 ② — `src/llm_router/routing/hop.py` 全文

```python
"""S1.5a · hop 语义(conditional 边界跳变)+ total_retry_budget 约束。"""
from __future__ import annotations
import json
from dataclasses import dataclass
from typing import Optional

DEFAULT_RETRY_BUDGET = 6

HOP_REASONS: frozenset[str] = frozenset({
    "initial", "key_open", "global_open", "half_open_busy",
    "hard_failure", "soft_content", "budget_exhausted",
})


@dataclass(frozen=True)
class HopAttribution:
    depth: int
    reason: str
    from_provider: Optional[str]
    to_provider: Optional[str]

    def to_json(self) -> str:
        return json.dumps(
            {"depth": self.depth, "reason": self.reason,
             "from": self.from_provider, "to": self.to_provider},
            separators=(",", ":"), sort_keys=False,
        )


def initial_attribution(to_provider: str) -> HopAttribution:
    return HopAttribution(depth=0, reason="initial", from_provider=None, to_provider=to_provider)


def advance(current_depth, reason, from_provider, to_provider) -> HopAttribution:
    assert reason in HOP_REASONS
    assert reason not in ("initial", "budget_exhausted")
    return HopAttribution(
        depth=current_depth + 1, reason=reason,
        from_provider=from_provider, to_provider=to_provider,
    )


def budget_exhausted(depth, from_provider) -> HopAttribution:
    return HopAttribution(
        depth=depth, reason="budget_exhausted",
        from_provider=from_provider, to_provider=None,
    )


def check_hop_budget(current_depth, budget=DEFAULT_RETRY_BUDGET) -> bool:
    """budget=6 允许 depth 0..5(6 个 provider 被尝试);depth 6 返 False → 拦下、写终态。"""
    return current_depth < budget


def parse_attribution(raw: Optional[str]) -> Optional[HopAttribution]:
    if raw is None:
        return None
    d = json.loads(raw)
    return HopAttribution(
        depth=d["depth"], reason=d["reason"],
        from_provider=d.get("from"), to_provider=d.get("to"),
    )
```

## §源码锚点 ③ — `PolicyEnforcer.check()` 行为契约

`src/llm_router/api/policy_enforcer.py`：构造 `PolicyEnforcer(entries: list[ProviderEntry])` 时按 `entity` 分组；`check()` 同步方法：若任一 entity 出现 ≥2 不同 `api_key_env` → 抛 `ComplianceError`，否则返回 None。`entity=None` 的条目不参与合规判定（视为独立 vendor）。

## §源码锚点 ④ — `HealthStore.record_probe` & `_surviving_candidates`

- `HealthStore.record_probe(provider, latency_ms, alive)` 写 `health.db`；以最新一条为准。
- `_surviving_candidates`（cascade.py）：调 `health_store.latest_probe(provider, key)` → `alive=False` 即剔除；`alive=True` 或没记录则保留。

---

## 测试文件全文（332 行，project-relative path：`tests/integration/test_phase1_critical_bugs.py`）

```python
"""Phase 1 集成验证·子片 1:4 CRITICAL BUG 端到端回归(workflow2)。"""
from __future__ import annotations
import asyncio

from llm_router.api.cascade import Cascade
from llm_router.api.cost_gate import CostGate
from llm_router.api.policy_enforcer import PolicyEnforcer
from llm_router.api.strategy import RoutingStrategy
from llm_router.config import ProviderEntry
from llm_router.providers.base import Provider, ProviderError
from llm_router.resilience.circuit_breaker import CircuitBreaker
from llm_router.routing.hop import parse_attribution
from llm_router.store.health_store import HealthStore
from llm_router.store.token_ledger import LedgerStore
from llm_router.store.trace import TraceStore


def _run(coro):
    return asyncio.run(coro)


class _FakeProvider(Provider):
    def __init__(self, name, *, text="ok", model="mX", raises=None, counter=None):
        self.name = name
        self._text = text
        self._model = model
        self._raises = raises
        self._counter = counter

    async def complete(self, prompt):
        if self._counter is not None:
            self._counter[self.name] = self._counter.get(self.name, 0) + 1
        if self._raises is not None:
            raise self._raises
        return self._text, self._model, None


class _FixedOrderStrategy(RoutingStrategy):
    def __init__(self, order):
        self._order = list(order)

    def plan(self, candidates, context):
        seen = set(candidates)
        return [c for c in self._order if c in seen]

    def select_provider(self, candidates, context):
        return self.plan(candidates, context)[0]


def _entry(name, *, entity=None, api_key_env=None, quota=1_000_000):
    return ProviderEntry(
        name=name, entity=entity, tier="fast",
        quota=quota, cooldown_s=30, is_free=True,
        cost_multiplier=0.0, api_key_env=api_key_env,
    )


def _build_full_cascade(tmp_path, *, entries, providers, strategy_order=None):
    """全组件生产 Cascade(policy_enforcer + health_store + breaker + cost_gate)。"""
    store = TraceStore(tmp_path / "trace.db")
    breaker = CircuitBreaker(db_path=tmp_path / "circuit.db", key_hard_threshold=3)
    health = HealthStore(tmp_path / "health.db")
    ledger = LedgerStore(tmp_path / "ledger.db")
    enforcer = PolicyEnforcer(entries)
    quotas = {e.name: e.quota for e in entries}
    cost_gate = CostGate(ledger, quotas)
    order = strategy_order or [e.name for e in entries]
    strategy = _FixedOrderStrategy(order)
    cands = [(p.name, p, "k1") for p in providers]
    cascade = Cascade(
        store, breaker, strategy, cands,
        health_store=health, policy_enforcer=enforcer,
        ledger=ledger, cost_gate=cost_gate, budget=6,
    )
    return cascade, store, breaker, health, ledger


def _zero_jitter(monkeypatch, breaker):
    monkeypatch.setattr(breaker, "_jitter_fn", lambda: 0.0)
    breaker._now_override = 1000.0


# ── BUG-FR-01:Cascade 总重试无上界 → budget=6 ──────────────────────────────


def test_bug_fr01_budget_exhausts_at_seventh_in_full_stack(tmp_path, monkeypatch):
    """BUG-FR-01 回归(S2.1b 修):全组件 Cascade(开 policy + health + cost_gate)下,
    7 个 hard-fail provider → budget=6 仍生效,第 7 个被拦,前 6 各调一次。"""
    names = ["pA", "pB", "pC", "pD", "pE", "pF", "pG"]
    entries = [_entry(n) for n in names]
    calls: dict[str, int] = {}
    providers = [
        _FakeProvider(n, raises=ProviderError(f"{n} down"), counter=calls) for n in names
    ]
    cascade, store, breaker, health, _ledger = _build_full_cascade(
        tmp_path, entries=entries, providers=providers
    )
    _zero_jitter(monkeypatch, breaker)

    async def body():
        try:
            await health.init()
            res = await cascade.run("ping", correlation_id="CID-fr01")
            chain = await store.get_chain("CID-fr01")
            return res, chain
        finally:
            await store.close()
            await health.close()

    res, chain = _run(body())
    assert res.success is False
    assert res.last_reason == "budget_exhausted"
    assert len(chain) == 7
    last = parse_attribution(chain[-1].hop_attribution)
    assert last.reason == "budget_exhausted" and last.to_provider is None
    assert calls.get("pG", 0) == 0  # 第 7 个 provider 必须未被调用
    assert all(calls.get(n) == 1 for n in names[:6])  # 前 6 各调一次


# ── BUG-FR-02:嵌套 fallback hop 语义未定义 ──────────────────────────────────


def test_bug_fr02_hop_attribution_intact_after_health_skip(tmp_path, monkeypatch):
    """BUG-FR-02 回归(S1.5a 修):health 剔死候选后,幸存链的 hop 归因仍单调正确。
    候选 [pA,pB,pC],health 标 pB alive=False → 幸存 [pA,pC]。pA 抛 hard,pC 成功。
    预期 hop:depth=0 reason=initial to=pA / depth=1 reason=hard_failure from=pA to=pC"""
    names = ["pA", "pB", "pC"]
    entries = [_entry(n) for n in names]
    calls: dict[str, int] = {}
    providers = [
        _FakeProvider("pA", raises=ProviderError("pA down"), counter=calls),
        _FakeProvider("pB", text="should-be-skipped", counter=calls),
        _FakeProvider("pC", text="hello", model="mC", counter=calls),
    ]
    cascade, store, breaker, health, _ledger = _build_full_cascade(
        tmp_path, entries=entries, providers=providers, strategy_order=names
    )
    _zero_jitter(monkeypatch, breaker)

    async def body():
        try:
            await health.init()
            await health.record_probe(provider="pB", latency_ms=None, alive=False)
            res = await cascade.run("ping", correlation_id="CID-fr02")
            chain = await store.get_chain("CID-fr02")
            return res, chain
        finally:
            await store.close()
            await health.close()

    res, chain = _run(body())
    assert res.success is True and res.final_text == "hello"
    assert calls.get("pB", 0) == 0
    assert len(chain) == 2
    h0 = parse_attribution(chain[0].hop_attribution)
    h1 = parse_attribution(chain[1].hop_attribution)
    assert h0.depth == 0 and h0.reason == "initial" and h0.to_provider == "pA"
    assert h1.depth == 1 and h1.reason == "hard_failure"
    assert h1.from_provider == "pA" and h1.to_provider == "pC"


# ── BUG-policy-01:同 provider 多账号薅羊毛 ──────────────────────────────────


def test_bug_policy01_compliance_blocks_before_breaker_and_provider(tmp_path, monkeypatch):
    """BUG-policy-01 回归(S2.7 修):同 entity 配 ≥2 不同 api_key_env → ComplianceError →
    compliance_blocked,**provider 不被调,breaker 不被读**(layer ①合规先于 health/breaker)。"""
    entries = [
        _entry("openrouter-a", entity="openrouter", api_key_env="OR_KEY1"),
        _entry("openrouter-b", entity="openrouter", api_key_env="OR_KEY2"),
    ]
    calls: dict[str, int] = {}
    providers = [
        _FakeProvider("openrouter-a", text="should-not-call", counter=calls),
        _FakeProvider("openrouter-b", text="should-not-call", counter=calls),
    ]
    cascade, store, breaker, health, _ledger = _build_full_cascade(
        tmp_path, entries=entries, providers=providers
    )
    _zero_jitter(monkeypatch, breaker)

    async def body():
        try:
            await health.init()
            res = await cascade.run("ping", correlation_id="CID-pol01")
            return res
        finally:
            await health.close()

    res = _run(body())
    assert res.success is False
    assert res.last_reason == "compliance_blocked"
    assert res.hops_attempted == 0
    assert calls == {}
    assert cascade._store_ready is False  # layer ① 优先于 trace init


# ── BUG-fallback-01:切换前未知存活状态 ──────────────────────────────────────


def test_bug_fallback01_dead_key_skipped_route_to_alive(tmp_path, monkeypatch):
    """BUG-fallback-01 回归(S2.8 修):health.db 标 provider alive=False →
    _surviving_candidates 剔除 → Cascade 不走它,直接到 alive 候选。"""
    entries = [_entry("pDead"), _entry("pAlive")]
    calls: dict[str, int] = {}
    providers = [
        _FakeProvider("pDead", text="should-not-call", counter=calls),
        _FakeProvider("pAlive", text="hello", model="mA", counter=calls),
    ]
    cascade, store, breaker, health, _ledger = _build_full_cascade(
        tmp_path, entries=entries, providers=providers, strategy_order=["pDead", "pAlive"]
    )
    _zero_jitter(monkeypatch, breaker)

    async def body():
        try:
            await health.init()
            await health.record_probe(provider="pDead", latency_ms=None, alive=False)
            await health.record_probe(provider="pAlive", latency_ms=10.0, alive=True)
            res = await cascade.run("ping", correlation_id="CID-fb01")
            chain = await store.get_chain("CID-fb01")
            return res, chain
        finally:
            await store.close()
            await health.close()

    res, chain = _run(body())
    assert res.success is True and res.final_text == "hello"
    assert calls.get("pDead", 0) == 0
    assert calls.get("pAlive", 0) == 1
    assert len(chain) == 1
    h0 = parse_attribution(chain[0].hop_attribution)
    assert h0.depth == 0 and h0.reason == "initial" and h0.to_provider == "pAlive"
```

## 输出要求

1. **每条发现独立段落**，标签 + 严重度 + file:line + 反例
2. **整文件结论单独一行**（最后）
3. 不要客套、不要总结开场白；直接进发现
4. **禁止 Read 其他文件**（一切已嵌入）

开始审。
