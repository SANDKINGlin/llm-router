# OpenCode 异构对抗审查 — Phase1 集成验证·子片 1

> 你是异构对抗审查者（非 PR 评论助手）。任务：用 HERMES 标签协议审查下方测试文件，对抗式找漏（"假绿"、覆盖盲区、逻辑矛盾），不是夸；最后必须给可执行结论。

## HERMES 标签协议（必须用）

每条发现必须以下列标签之一开头：
- `[CHALLENGE]` 严重度=CRITICAL/HIGH/MED/LOW + file:line + 问题 + **可复现反例**（"如果改成 X 输入，测试应败但会绿"）
- `[CONSENSUS]` 维度收敛认可，无 CHALLENGE
- `[DEADLOCK]` 多轮无法收敛，需人工裁决

**整文件结论**单独一行：
- `[CONSENSUS] 子片 1 测试可作 Phase 1 出厂回归门` （无 CRITICAL/HIGH）
- `[CHALLENGE] 子片 1 测试存在 N 项 CRITICAL/HIGH，需修复后重审`

## 项目背景

智能路由层 Phase 1（`~/projects/llm-router`），4 个 CRITICAL BUG 已修复（多切片完成）：

| BUG-id | 描述 | 修复切片 | 关键代码锚点 |
|---|---|---|---|
| BUG-FR-01 | Cascade 总重试无上界 | S2.1b（budget=6） | `cascade.py:280-296` budget 门 |
| BUG-FR-02 | 嵌套 fallback hop 语义未定义 | S1.5a（hop_attribution + advance） | `cascade.py:332-380` + `routing/hop.py` |
| BUG-policy-01 | 同 provider 多账号薅羊毛 | S2.7（policy_enforcer.check） | `cascade.py:242-249` layer ① compliance |
| BUG-fallback-01 | 切换前未知存活状态 | S2.8（health_store + hard-skip） | `cascade.py:251` `_surviving_candidates` |

`cascade.run` 实际 layer 顺序（生产代码）：
1. **Layer ①** compliance 门：不通过 → 直接 `compliance_blocked`，**不 init store、不 plan、不调 provider**
2. **Layer ②** `_ensure_store` + `_surviving_candidates`（health 剔死候选）
3. **Layer ③** `strategy.plan(survivors, context)` 排候选序
4. **循环**：budget 门 → `store.acquire` → breaker → `provider.complete` → 写 `hop_attribution`

## 子片 1 设计意图（防与现有测试重复）

| 现有测试 | 与子片 1 区别 |
|---|---|
| `tests/unit/test_cascade.py` | 组件层单测，只组装 store+breaker+strategy |
| `tests/unit/test_policy_enforcer.py` | 纯 PolicyEnforcer 逻辑 |
| `tests/unit/health/test_*.py` | 纯 HealthStore 测 |
| `tests/e2e/test_fallback_e2e.py` | orchestrator 测试 helper（非生产 Cascade） |
| **子片 1（本文件）** | **多组件全开生产 Cascade**：policy_enforcer + health_store + breaker + cost_gate + epsilon strategy 同时启用，证明多过滤层不冲突 |

## 审重点（必查 4 项 + 任意自由发掘）

1. **覆盖度**：4 个测试是否真覆盖 4 BUG 的修复点？反查 `cascade.py` 实际代码（行号已给），断言是否落在修复行附近？
2. **假绿陷阱**：`calls` 计数 / `_store_ready` 私有属性 / 链长 / `from/to_provider` 等断言能否被"虚假实现"绕过？比如 BUG-FR-01 测试若 cascade 改成"调 7 次但最后 1 次返失败"也能通过吗？
3. **关键场景遗漏**：除已标 defer 到子片 3 的 (budget+health 同触发 / 合规 vs cost_gate 顺序 / exact quota 边界 / quota=0 / compliance×cost 顺序) 之外，**还有什么本应在子片 1 出厂门里但未覆盖**？
4. **`quota=1_000_000` 旁路 cost_gate 意图**：`_entry()` helper 默认 `quota=1_000_000` 是否真能让 cost_gate 不干扰？还是隐式被 `is_free=True / cost_multiplier=0.0` 救场？这层旁路若被未来重构破坏，测试会假绿吗？

**额外要求**：
- 反例必须可执行（不要只说"可能"），格式："如果把 cascade.py 的 X 改成 Y，本测试应败但会绿"
- 引用必须 `file:line`（不要"某个地方"）
- 严重度判定要稳：CRITICAL = 出厂门失效（假绿）；HIGH = 重要场景遗漏；MED = 强化建议；LOW = 可选

## 测试文件全文（332 行，project-relative path：`tests/integration/test_phase1_critical_bugs.py`）

```python
"""Phase 1 集成验证·子片 1:4 CRITICAL BUG 端到端回归(workflow2)。

聚合验证 4 个 CRITICAL BUG 在**生产 Cascade**(policy_enforcer + health_store + breaker +
cost_gate + epsilon strategy 全组件同时启用)下的端到端行为,证明 Phase 1 出厂前所有
CRITICAL BUG 修复未回归。

不同于:
- tests/unit/test_cascade.py(组件层单测,只组装 store+breaker+strategy)
- tests/unit/test_policy_enforcer.py(纯 PolicyEnforcer 逻辑测)
- tests/unit/health/test_*.py(纯 HealthStore 测)
- tests/e2e/test_fallback_e2e.py(orchestrator 测试 helper,非生产 Cascade)

本文件用**多组件全开 Cascade 实例**复现 4 BUG 场景,每个测试 docstring 标 BUG-id 与修复切片。
若任一回归,即 Phase 1 出厂受阻。

| BUG-id | 描述 | 修复切片 | 本文件测试 |
|---|---|---|---|
| BUG-FR-01 | Cascade 总重试无上界(最坏 3N 次) | S2.1b(budget=6) | test_bug_fr01_* |
| BUG-FR-02 | 嵌套 fallback hop 语义未定义 | S1.5a(hop_attribution + advance) | test_bug_fr02_* |
| BUG-policy-01 | 同 provider 多账号薅羊毛 | S2.7(policy_enforcer.check) | test_bug_policy01_* |
| BUG-fallback-01 | 切换前未知存活状态 | S2.8(health_store + hard-skip) | test_bug_fallback01_* |
"""
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
    """同步包 asyncio(免 pytest-asyncio,贴 test_cascade/test_fallback_e2e 模式)。"""
    return asyncio.run(coro)


class _FakeProvider(Provider):
    """可控 provider:配返成功/残缺/抛异常,记录调用计数(防假绿)。"""

    def __init__(
        self,
        name: str,
        *,
        text: str = "ok",
        model: str = "mX",
        raises: Exception | None = None,
        counter: dict[str, int] | None = None,
    ) -> None:
        self.name = name
        self._text = text
        self._model = model
        self._raises = raises
        self._counter = counter

    async def complete(self, prompt: str):
        if self._counter is not None:
            self._counter[self.name] = self._counter.get(self.name, 0) + 1
        if self._raises is not None:
            raise self._raises
        return self._text, self._model, None


class _FixedOrderStrategy(RoutingStrategy):
    """确定性策略:plan 返固定序(隔离 cascade,不耦合 ε 探索)。"""

    def __init__(self, order: list[str]) -> None:
        self._order = list(order)

    def plan(self, candidates, context):
        seen = set(candidates)
        return [c for c in self._order if c in seen]

    def select_provider(self, candidates, context):
        return self.plan(candidates, context)[0]

    # S4.3 rebuild 兼容:这里不需要 refresh_entries(测试不切版本)。


def _entry(
    name: str,
    *,
    entity: str | None = None,
    api_key_env: str | None = None,
    quota: int = 1_000_000,
) -> ProviderEntry:
    """建一个最小 ProviderEntry(全免费,大配额——不让 cost_gate 干扰本文件验证)。"""
    return ProviderEntry(
        name=name,
        entity=entity,
        tier="fast",
        quota=quota,
        cooldown_s=30,
        is_free=True,
        cost_multiplier=0.0,
        api_key_env=api_key_env,
    )


def _build_full_cascade(
    tmp_path,
    *,
    entries: list[ProviderEntry],
    providers: list[_FakeProvider],
    strategy_order: list[str] | None = None,
) -> tuple[Cascade, TraceStore, CircuitBreaker, HealthStore, LedgerStore]:
    """造一个**全组件**生产 Cascade(policy_enforcer + health_store + breaker + cost_gate),
    与 app.py _build_cascade 同形状(去掉 mock 兜底,测试用 _FakeProvider 全控)。

    每 provider 默认 key='k1';strategy_order 不传 → 用 entries 顺序。
    """
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
        store,
        breaker,
        strategy,
        cands,
        health_store=health,
        policy_enforcer=enforcer,
        ledger=ledger,
        cost_gate=cost_gate,
        budget=6,
    )
    return cascade, store, breaker, health, ledger


def _zero_jitter(monkeypatch, breaker: CircuitBreaker) -> None:
    """钉死 breaker 的 jitter(防测试不稳定);贴 test_cascade.py 模式。"""
    monkeypatch.setattr(breaker, "_jitter_fn", lambda: 0.0)
    breaker._now_override = 1000.0


# ── BUG-FR-01:Cascade 总重试无上界 → budget=6 ──────────────────────────────


def test_bug_fr01_budget_exhausts_at_seventh_in_full_stack(tmp_path, monkeypatch):
    """BUG-FR-01 回归(S2.1b 修):全组件 Cascade(开 policy + health + cost_gate)下,
    7 个 hard-fail provider → budget=6 仍生效,第 7 个被拦,前 6 各调一次。

    与 unit/test_cascade.test_total_retry_budget_six_hard_stop 不同点:
    本测试 **同时启用 policy_enforcer + health_store + cost_gate**——证明 budget 门
    与其他过滤层不冲突,也不被 cost_gate 隐式吃掉(7 个全免费 + 大 quota → cost_gate 放行)。
    """
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
            await health.init()  # 全活,不 hard-skip
            res = await cascade.run("ping", correlation_id="CID-fr01")
            chain = await store.get_chain("CID-fr01")
            return res, chain
        finally:
            await store.close()
            await health.close()

    res, chain = _run(body())
    assert res.success is False
    assert res.last_reason == "budget_exhausted", (
        f"BUG-FR-01 回归:全组件下 budget 门未触发,实际 {res.last_reason}"
    )
    assert len(chain) == 7, f"链长应 7(6 hard + 1 budget_exhausted),实际 {len(chain)}"
    last = parse_attribution(chain[-1].hop_attribution)
    assert last.reason == "budget_exhausted" and last.to_provider is None
    assert calls.get("pG", 0) == 0, "BUG-FR-01:第 7 个 provider 必须未被调用"
    assert all(calls.get(n) == 1 for n in names[:6]), (
        f"BUG-FR-01:前 6 个应各调一次,实际 {calls}"
    )


# ── BUG-FR-02:嵌套 fallback hop 语义未定义 ──────────────────────────────────


def test_bug_fr02_hop_attribution_intact_after_health_skip(tmp_path, monkeypatch):
    """BUG-FR-02 回归(S1.5a 修):health 剔死候选后,幸存链的 hop 归因仍单调正确。

    场景:候选 [pA, pB, pC],health.db 标 pB alive=False → _surviving_candidates 剔除 pB,
    幸存 [pA, pC]。pA 抛 hard,pC 成功。预期 hop:
      depth=0 reason=initial to=pA
      depth=1 reason=hard_failure from=pA to=pC
    证明 hop 不会因 pB 被 health 剔走而错乱(BUG-FR-02 修复点的边界场景:幸存子集仍按相邻
    顺序写归因)。
    """
    names = ["pA", "pB", "pC"]
    entries = [_entry(n) for n in names]
    calls: dict[str, int] = {}
    providers = [
        _FakeProvider("pA", raises=ProviderError("pA down"), counter=calls),
        _FakeProvider("pB", text="should-be-skipped", counter=calls),  # 死 → 不应被调
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
    assert calls.get("pB", 0) == 0, "BUG-fallback-01 副控:pB 死,不应被调"
    assert len(chain) == 2, f"幸存链 [pA, pC] → 2 跳,实际 {len(chain)}"
    h0 = parse_attribution(chain[0].hop_attribution)
    h1 = parse_attribution(chain[1].hop_attribution)
    assert h0.depth == 0 and h0.reason == "initial" and h0.to_provider == "pA"
    assert h1.depth == 1 and h1.reason == "hard_failure"
    assert h1.from_provider == "pA" and h1.to_provider == "pC", (
        "BUG-FR-02 回归:hop 归因 from/to 错位(health 剔走中间候选后,from 应是上一**真调**节点)"
    )


# ── BUG-policy-01:同 provider 多账号薅羊毛 ──────────────────────────────────


def test_bug_policy01_compliance_blocks_before_breaker_and_provider(tmp_path, monkeypatch):
    """BUG-policy-01 回归(S2.7 修):同 entity 配 ≥2 不同 api_key_env → ComplianceError →
    compliance_blocked,**provider 不被调,breaker 不被读**(layer ①合规先于 health/breaker)。

    场景:两个 entry 同 entity="openrouter",分别用 OR_KEY1/OR_KEY2 → 检测违规 → 拒路由。
    """
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
            # 合规拦截先于 store 惰性 init,store 未 init → 跳过 close。
            await health.close()

    res = _run(body())
    assert res.success is False
    assert res.last_reason == "compliance_blocked", (
        f"BUG-policy-01 回归:同 entity 多账号未被合规门拦截,实际 {res.last_reason}"
    )
    assert res.hops_attempted == 0, "合规拦截时 hops_attempted=0(provider 未调)"
    assert calls == {}, f"合规拦截时 provider 不应被调用,实际 {calls}"
    # 合规拦截先于 store init(layer ① layering 契约,见 cascade.run line 242-249):
    # _ensure_store 未跑 → store 仍未就绪。直接断言 _store_ready 守 layering。
    assert cascade._store_ready is False, (
        "BUG-policy-01:合规拦截不应触发 store init(layer ① 优先于 trace 写入)"
    )


# ── BUG-fallback-01:切换前未知存活状态 ──────────────────────────────────────


def test_bug_fallback01_dead_key_skipped_route_to_alive(tmp_path, monkeypatch):
    """BUG-fallback-01 回归(S2.8 修):health.db 标某 provider alive=False →
    _surviving_candidates 剔除 → Cascade 不走它,直接到 alive 候选。

    场景:候选 [pDead, pAlive],health.db 标 pDead alive=False。
    预期:链长 1(只 pAlive),pDead.complete 调用计数 == 0(真未路由,非"调了再失败")。
    """
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
    assert calls.get("pDead", 0) == 0, (
        "BUG-fallback-01 回归:death 候选被路由(应在 _surviving_candidates 剔除)"
    )
    assert calls.get("pAlive", 0) == 1
    assert len(chain) == 1, f"幸存链应只 [pAlive] → 1 跳,实际 {len(chain)}"
    h0 = parse_attribution(chain[0].hop_attribution)
    assert h0.depth == 0 and h0.reason == "initial" and h0.to_provider == "pAlive"
```

## 输出要求（再次强调）

1. **每条发现独立段落**，标签开头：`[CHALLENGE] CRITICAL test_bug_fr01_*:189 ...`
2. **整文件结论单独一行**（最后）
3. 不要客套、不要总结开场白；直接进发现
4. 严重度判定参考：
   - CRITICAL：测试在 BUG 重现时会假绿（出厂门失效）
   - HIGH：明确遗漏的关键 BUG 边界场景（不是 defer 到子片 3 的）
   - MED：稳定性/可读性强化建议（命名/重复/缺辅助断言）
   - LOW：可选风格/注释建议

开始审。
