# OpenCode 异构对抗审查 — Phase1 集成验证·子片 3(自包含 prompt v1)

> 你是异构对抗审查者。任务:用 HERMES 标签协议审查测试文件,对抗式找漏("假绿"、覆盖盲区、逻辑矛盾),不是夸;最后必须给可执行结论。

## ⚠️ 自包含约束

**所有需要的源码已嵌入下方 §源码锚点**。**禁止 Read/Glob/Grep 其他源文件**。仅基于本文件的内容审查并直接输出发现。如确实需要某个未嵌入的细节,**写在 [DEADLOCK]** 里说明缺哪段,不要去探查。直接 Read 其他源文件会浪费 timeout(子片 1 v1 prompt 用相对锚点导致模型反查 → 480s 超时被杀,v2 自包含才完成,本审继承 v2 教训)。

## HERMES 标签协议(每条发现必须用)

- `[CHALLENGE] <严重度> <file:line> <问题> <可复现反例>`("如果改成 X 输入,测试应败但会绿")
- `[CONSENSUS]` 维度收敛认可,无 CHALLENGE
- `[DEADLOCK]` 多轮无法收敛 / 缺关键源码

严重度:CRITICAL(出厂门假绿)/ HIGH(关键场景遗漏)/ MED(强化建议)/ LOW(风格)。

**整文件结论**单独一行(最后):
- `[CONSENSUS] 子片 3 测试可作 Phase 1 出厂回归门`(无 CRITICAL/HIGH)
- `[CHALLENGE] 子片 3 测试存在 N 项 CRITICAL/HIGH,需修复后重审`

## 项目背景

智能路由层 Phase 1(`~/projects/llm-router`)。**Phase 1 集成验证**拆 4 子片:
- 子片 1 done:4 CRITICAL BUG 端到端回归门(组件层全开 Cascade 直 .run())
- 子片 2 done:端到端 happy path 经 FastAPI(TestClient + 模块级 _cascade)
- **子片 3(本审)**:**BUG 跨场景交互 + 多 defer 收口**(直接 cascade.run() 模式,与子片 1 同)
- 子片 4 待开:压测(模拟 429 burst,验 fallback 链不雪崩)

## 子片 3 收口三批 defer

**A1** 子片 2 OpenCode #3 主体失败路径(端点静默 200 已 sanity-tested,本切片测 cascade 真路径):
- no_candidates 真触发(health.db 全死)
- budget_exhausted(7+ provider 全 HARD,DEFAULT_RETRY_BUDGET=6)
- 全 SOFT_CONTENT 链耗尽

**A2** 子片 1 OpenCode 4 项 defer 收口:
- #1 breaker 阻断零覆盖:跨请求 3 连 HARD → key_open
- #4 no_candidates(由 A1 共同覆盖)
- #5 cost_gate 静默旁路:预灌 ledger 至 consumed >= quota → 剔出

**A3** S2.4 defer 3 盲区:
- exact quota 边界(consumed == quota → 应剔出)
- quota=0 → 配置错也 fail-loud
- compliance × cost 顺序(layer ①)

## 审重点(必查 5 项 + 任意自由发掘)

1. **覆盖度**:10 测试是否真覆盖三批 defer?反查下方源码锚点,断言是否落在 cascade.run() 关键分支(no_candidates 早返 / budget 门 / SOFT 终态 / breaker hard-skip / cost_gate 过滤)?
2. **假绿陷阱**:counter 计数 / trace 直查 / spy CostGate / breaker 状态断言能否被"虚假实现"绕过?比如把 `_surviving_candidates` 改成"返空就直接 raise" 是否被抓?把 budget=6 改成 7 是否被抓?
3. **A2 breaker × policy 测试设计**:`test_cascade_skips_pre_tripped_open_key_and_routes_to_good` 用 `record_failure(good, SOFT)` 注册 good 到 _keys,这个是 production 真实场景吗?如果未来改 record_success 也 setdefault,本测试是否假阴(场景失效)?
4. **顺序契约证**:`test_compliance_check_runs_before_cost_gate` 用 spy CostGate.survivors,如果有人把 health 过滤层挪到合规层之前(layer 顺序错),本测能否抓?
5. **状态污染**:三批测试共享 tmp_path 文件吗?有跨测试 SQLite 状态泄漏吗?预 trip breaker 是否影响后续测试?

**额外要求**:
- 反例必须可执行,格式:"如果把 X.py 的 Y 改成 Z,本测试应败但会绿"
- 引用必须 `file:line`(不要"某个地方")
- 嵌入文件外的源码请写 [DEADLOCK] 说明缺哪段,**禁止 Read**

---

## §源码锚点 ① — 测试文件全文 `tests/integration/test_phase1_bug_interactions.py`(本审目标)

```python
"""Phase 1 集成验证·子片 3:BUG 跨场景交互 + 多 defer 收口。

子片 1(test_phase1_critical_bugs.py)用全组件 Cascade 直 .run() 测 4 BUG 各自的回归门;
子片 2(test_phase1_happy_path.py)经 TestClient → FastAPI 测端到端 happy path 集成。

子片 3 收口三批 defer:

**A1 子片 2 OpenCode #3 主体失败路径(端点静默 200 已 sanity-tested,这里测 cascade 真路径)**:
  - no_candidates 真触发(health.db 全死,cascade._surviving_candidates 返空)
  - budget_exhausted(7+ provider 全 HARD,DEFAULT_RETRY_BUDGET=6 拦第 7 跳)
  - 全 SOFT_CONTENT 终态(链耗尽 last_reason="soft_content")

**A2 子片 1 OpenCode 4 项 defer 收口**:
  - #1 breaker 阻断零覆盖:跨请求 3 连 HARD → key_open;后续请求该 key 被 hard-skip
  - #4 no_candidates(由 A1 #1 共同覆盖)
  - #5 cost_gate 静默旁路:预灌 ledger 至 consumed >= quota → cost_gate 剔出

**A3 S2.4 defer 3 盲区**:
  - exact quota 边界(consumed == quota → 应剔出,is_over_budget 用 `>=`)
  - quota=0 → consumed=0 即 over_budget(配置错也要 fail-loud)
  - compliance × cost 顺序(合规违规早返,不查 ledger,顺序契约 layer ①)

防假绿:
- 用 counter 验 provider 是否真被调过(尤其熔断/cost_gate 跳过场景下不应被调)
- 用 health_store.record_probe / breaker.record_failure 直接预置状态(不依赖 cascade
  内部状态泄漏)
- 用 ledger.record / ledger.begin_stream 直接灌账(不依赖 _record_usage 间接路径)
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from llm_router.api.cascade import Cascade
from llm_router.api.cost_gate import CostGate
from llm_router.api.policy_enforcer import PolicyEnforcer
from llm_router.api.strategy import RoutingStrategy
from llm_router.config import ProviderEntry
from llm_router.providers.base import Provider, ProviderError, Usage
from llm_router.resilience.circuit_breaker import CircuitBreaker, TripReason
from llm_router.routing.hop import DEFAULT_RETRY_BUDGET, parse_attribution
from llm_router.store.health_store import HealthStore
from llm_router.store.token_ledger import LedgerStore
from llm_router.store.trace import TraceStore


def _run(coro):
    return asyncio.run(coro)


# ── stub providers ────────────────────────────────────────────────────────


class _StubOK(Provider):
    """成功 provider:配返 text/model/usage,记 counter。"""

    def __init__(
        self,
        name: str,
        *,
        text: str = "ok",
        model: str = "m",
        usage: Usage | None = None,
        counter: dict[str, int] | None = None,
    ) -> None:
        self.name = name
        self._text = text
        self._model = model
        self._usage = usage
        self._counter = counter

    async def complete(self, prompt: str):
        if self._counter is not None:
            self._counter[self.name] = self._counter.get(self.name, 0) + 1
        return self._text, self._model, self._usage


class _StubHardFail(Provider):
    """硬失败 provider:抛 ProviderError → cascade record_failure(HARD)。"""

    def __init__(self, name: str, *, counter: dict[str, int] | None = None) -> None:
        self.name = name
        self._counter = counter

    async def complete(self, prompt: str):
        if self._counter is not None:
            self._counter[self.name] = self._counter.get(self.name, 0) + 1
        raise ProviderError(f"{self.name} down")


class _StubSoft(Provider):
    """软残缺 provider:返 text="" → is_complete False → SOFT_CONTENT。"""

    def __init__(self, name: str, *, counter: dict[str, int] | None = None) -> None:
        self.name = name
        self._counter = counter

    async def complete(self, prompt: str):
        if self._counter is not None:
            self._counter[self.name] = self._counter.get(self.name, 0) + 1
        return "", "soft-m", None


class _FixedOrder(RoutingStrategy):
    def __init__(self, order: list[str]) -> None:
        self._order = list(order)

    def plan(self, candidates, context):
        seen = set(candidates)
        return [c for c in self._order if c in seen]


# ── 隔离 Cascade 装配 ─────────────────────────────────────────────────────


def _entry(
    name: str,
    *,
    quota: int = 1_000_000,
    is_free: bool = True,
    cost_multiplier: float = 0.0,
    api_key_env: str | None = None,
    entity: str | None = None,
) -> ProviderEntry:
    return ProviderEntry(
        name=name,
        entity=entity,
        tier="fast",
        quota=quota,
        cooldown_s=30,
        is_free=is_free,
        cost_multiplier=cost_multiplier,
        api_key_env=api_key_env,
    )


def _make_cascade(
    tmp_path: Path,
    candidates: list[tuple[str, Provider, str]],
    *,
    entries: dict[str, ProviderEntry] | None = None,
    quotas: dict[str, int] | None = None,
    order: list[str] | None = None,
    key_hard_threshold: int = 3,
) -> tuple[Cascade, HealthStore, CircuitBreaker, LedgerStore]:
    """返 (cascade, health_store, breaker, ledger) — 分量返便于直接预置状态。"""
    eff_entries = entries or {n: _entry(n) for n, _p, _k in candidates}
    eff_quotas = quotas if quotas is not None else {n: 1_000_000 for n in eff_entries}
    eff_order = order or [n for n, _p, _k in candidates]

    health = HealthStore(tmp_path / "health.db")
    breaker = CircuitBreaker(
        tmp_path / "circuit.db", key_hard_threshold=key_hard_threshold
    )
    ledger = LedgerStore(tmp_path / "ledger.db")
    cost_gate = CostGate(ledger, eff_quotas)

    cascade = Cascade(
        store=TraceStore(tmp_path / "trace.db"),
        breaker=breaker,
        strategy=_FixedOrder(eff_order),
        candidates=candidates,
        health_store=health,
        policy_enforcer=PolicyEnforcer(eff_entries.values()),
        ledger=ledger,
        cost_gate=cost_gate,
    )
    return cascade, health, breaker, ledger


# ── A1 子片 2 OpenCode #3 主体失败路径 ──────────────────────────────────


def test_no_candidates_when_all_providers_dead_via_health(tmp_path):
    """子片 1 OpenCode #4 + 子片 2 OpenCode #3:health.db 全死 → no_candidates 终态。

    防假绿:provider 必须**未**被调过(_surviving_candidates 在 plan 前 hard-skip 死 key
    → 空 survivors → 早返 no_candidates,不进 plan/不调 provider)。trace 应空。
    """
    counter: dict[str, int] = {}
    candidates = [
        ("p1", _StubOK("p1", counter=counter), "k1"),
        ("p2", _StubOK("p2", counter=counter), "k2"),
    ]
    cascade, health, _br, _ldg = _make_cascade(tmp_path, candidates)

    async def body():
        await health.init()
        try:
            # 全部 provider 探活记死
            for name in ("p1", "p2"):
                await health.record_probe(name, latency_ms=None, alive=False)
            res = await cascade.run("ping", correlation_id="cor-no-cand")
            return res
        finally:
            await health.close()

    res = _run(body())
    assert res.success is False
    assert res.last_reason == "no_candidates"
    assert res.hops_attempted == 0
    assert res.final_text is None and res.final_model is None
    assert counter == {}, "no_candidates 路径不应触达任何 provider"


def test_budget_exhausted_after_six_hard_failures_stops_at_seventh(tmp_path):
    """子片 2 OpenCode #3:7 个 provider 全 HARD,budget=6 在 depth=6 拦下第 7 跳。

    防假绿:① 实调用 6 个 HARD provider(counter 验)② 第 7 个 provider 永不被调
    (check_hop_budget 拦在 acquire 前)③ trace 7 行(6 失败 + 1 budget_exhausted 终态)
    ④ last_reason="budget_exhausted"。
    """
    counter: dict[str, int] = {}
    names = [f"hard-{i}" for i in range(7)]  # 7 providers,budget=6 拦最后 1 个
    candidates = [(n, _StubHardFail(n, counter=counter), f"key-{n}") for n in names]
    cascade, health, _br, _ldg = _make_cascade(tmp_path, candidates)

    async def body():
        await health.init()
        try:
            return await cascade.run("ping", correlation_id="cor-budget")
        finally:
            await health.close()

    res = _run(body())
    assert res.success is False
    assert res.last_reason == "budget_exhausted"
    # 实际尝试次数 == budget (6),第 7 个被 budget 门拦,不计 attempted
    assert res.hops_attempted == DEFAULT_RETRY_BUDGET
    # counter 验防假绿:前 6 个 provider 各被调 1 次,第 7 个未被调
    expected = {n: 1 for n in names[:DEFAULT_RETRY_BUDGET]}
    assert counter == expected, (
        f"前 6 应各调 1 次,第 7 个不调;实际 {counter}"
    )

    # trace 应有 7 行(6 真失败 + 1 budget_exhausted 终态)。
    rows = _trace_rows_db(tmp_path)
    assert len(rows) == 7
    last = rows[-1]
    last_attr = parse_attribution(last["hop_attribution"])
    assert last_attr is not None and last_attr.reason == "budget_exhausted"
    assert last_attr.depth == DEFAULT_RETRY_BUDGET  # depth=6 是被拦的第 7 跳


def test_all_soft_content_chain_terminates_with_soft_content_reason(tmp_path):
    """子片 1 OpenCode #3 主路径 + 子片 2 OpenCode #3:全部 provider SOFT_CONTENT,
    链耗尽,last_reason="soft_content"(终态而非 budget,因 chain 长度 < budget)。

    防假绿:每个 provider 真被调一次(counter)+ 链终态非 budget_exhausted。
    """
    counter: dict[str, int] = {}
    names = ["soft-a", "soft-b", "soft-c"]  # 3 < budget=6 → 链耗尽,非 budget
    candidates = [(n, _StubSoft(n, counter=counter), f"k-{n}") for n in names]
    cascade, health, _br, _ldg = _make_cascade(tmp_path, candidates)

    async def body():
        await health.init()
        try:
            return await cascade.run("ping", correlation_id="cor-all-soft")
        finally:
            await health.close()

    res = _run(body())
    assert res.success is False
    assert res.last_reason == "soft_content"  # 链耗尽,非 budget_exhausted
    assert res.hops_attempted == 3
    assert counter == {n: 1 for n in names}, "每个 SOFT provider 各调 1 次"


# ── A2 子片 1 OpenCode #1 breaker × policy / #5 cost_gate 静默旁路 ────────


def test_breaker_opens_key_after_three_consecutive_hard_failures(tmp_path):
    """子片 1 OpenCode #1 状态机闭合(纯断言):3 连 HARD → 该 key OPEN(Gap1 阈值=3)。

    分离 breaker 状态机本身(本测)与 cascade 路由集成(下一测)——单元层 spec 已覆盖
    breaker 状态机但**生产 cascade 内**累积 3 HARD 这条路径在子片 1 零覆盖,本测补。
    """
    counter: dict[str, int] = {}
    candidates = [
        ("bad", _StubHardFail("bad", counter=counter), "k-bad"),
    ]
    cascade, health, breaker, _ldg = _make_cascade(tmp_path, candidates)

    async def body():
        await health.init()
        try:
            for i in range(3):
                await cascade.run("ping", correlation_id=f"cor-trip-{i}")
            return breaker.get_key_state("bad", "k-bad")
        finally:
            await health.close()

    ks = _run(body())
    assert ks.state.value == "open", f"3 连 HARD 后 bad 应 OPEN,实际 {ks.state}"
    assert ks.hard_failures == 3
    assert counter == {"bad": 3}, "前 3 跳每跳 bad 真被调"


def test_cascade_skips_pre_tripped_open_key_and_routes_to_good(tmp_path):
    """子片 1 OpenCode #1 cascade 集成:bad 已 OPEN + good 已注册 _keys CLOSED →
    cascade 真路径 hard-skip bad,routed 到 good。**production 真实场景**(provider
    经初次 SOFT 或某次 HARD 注册过即在 _keys;首次未注册仅出现在派生聚合早期边界)。

    防假绿:counter[bad]==0(被 allow_request 拒,不调 complete);counter[good]==1。
    trace 2 行:bad 跳 reason=key_open,good 跳成功。

    场景设计:
      - 直接 breaker.record_failure(bad, HARD)×3 预 trip bad 到 OPEN(不经 cascade,
        干净状态)
      - 用 record_failure(good, SOFT) 让 good 注册到 _keys(SOFT 默认 ratio=3,1 SOFT
        → hf=0 sf=1 仍 CLOSED;不影响判定但保证 good ∈ _keys → 派生 global=CLOSED)
    """
    counter: dict[str, int] = {}
    candidates = [
        ("bad", _StubHardFail("bad", counter=counter), "k-bad"),
        ("good", _StubOK("good", text="ok", model="m-g", counter=counter), "k-good"),
    ]
    cascade, health, breaker, _ldg = _make_cascade(tmp_path, candidates)

    async def body():
        await health.init()
        try:
            # 预 trip bad 到 OPEN(直接调 breaker,不经 cascade,counter 干净)
            for _ in range(3):
                breaker.record_failure("bad", "k-bad", TripReason.HARD)
            assert breaker.get_key_state("bad", "k-bad").state.value == "open"
            # 注册 good 到 _keys(用 1 次 SOFT_CONTENT,ratio=3 → hf=0,仍 CLOSED;
            # 关键是让 good ∈ _keys → 派生 global=CLOSED 而非 all-open-bad)
            breaker.record_failure("good", "k-good", TripReason.SOFT_CONTENT)
            assert breaker.get_key_state("good", "k-good").state.value == "closed"
            return await cascade.run("ping", correlation_id="cor-skip")
        finally:
            await health.close()

    res = _run(body())
    assert res.success is True
    assert res.final_text == "ok" and res.final_model == "m-g"
    assert counter == {"good": 1}, (
        f"bad OPEN 不应被调,实际 counter={counter}"
    )
    # trace 2 行:bad(key_open)+ good(initial / advance:key_open)
    rows = _trace_rows_db(tmp_path)
    assert len(rows) == 2
    bad_row, good_row = rows[0], rows[1]
    assert bad_row["provider"] == "bad" and bad_row["result"] == ""
    assert good_row["provider"] == "good" and good_row["result"] == "ok"
    good_attr = parse_attribution(good_row["hop_attribution"])
    assert good_attr is not None
    assert good_attr.depth == 1 and good_attr.reason == "key_open"
    assert good_attr.from_provider == "bad" and good_attr.to_provider == "good"


def test_cost_gate_blocks_provider_when_consumed_reaches_quota(tmp_path):
    """子片 1 OpenCode #5 闭合:预灌 ledger 到 consumed == quota → cost_gate.survivors
    剔出该 provider;cascade 跳到下一候选(good),不调超预算 provider。

    防假绿:① counter[over] == 0(被剔出,不调)② counter[good] == 1。
    """
    counter: dict[str, int] = {}
    candidates = [
        ("over", _StubOK("over", text="should-not", model="m-o", counter=counter), "k-o"),
        ("good", _StubOK("good", text="ok-good", model="m-g", counter=counter), "k-g"),
    ]
    quotas = {"over": 100, "good": 1_000_000}
    cascade, health, _br, ledger = _make_cascade(
        tmp_path, candidates, quotas=quotas
    )

    async def body():
        await health.init()
        await ledger.init()
        try:
            # 预灌 over 已消费 100 token(== quota,is_over_budget 用 >=)
            await ledger.record(
                provider="over",
                model="m-o",
                prompt_tokens=60,
                completion_tokens=40,
                cost=None,
            )
            return await cascade.run("ping", correlation_id="cor-cost-block")
        finally:
            await health.close()

    res = _run(body())
    assert res.success is True
    assert res.final_text == "ok-good"
    assert counter == {"good": 1}, (
        f"超预算 over 应被 cost_gate 剔出未被调;实际 {counter}"
    )


# ── A3 S2.4 defer 3 盲区 ─────────────────────────────────────────────────


def test_cost_gate_exact_quota_boundary_blocks_at_equality(tmp_path):
    """S2.4 defer 盲区 #1:consumed == quota 时是 over_budget(`is_over_budget` 用 `>=`)。

    pure-function 验证(不经 cascade,直接 CostGate.survivors):consumed=100 quota=100
    应被剔出(>= 是边界严格);consumed=99 quota=100 仍允许。
    """
    candidates = [("p", _StubOK("p"), "k")]
    cascade, _h, _br, ledger = _make_cascade(
        tmp_path, candidates, quotas={"p": 100}
    )

    async def body():
        await ledger.init()
        # consumed=99 < 100:仍允许
        await ledger.record(
            provider="p", model="m", prompt_tokens=99, completion_tokens=0, cost=None
        )
        # 通过 cost_gate.survivors 真路径验证(不只查 is_over_budget 纯函数)
        survivors_99 = await cascade._cost_gate.survivors(["p"])
        assert survivors_99 == ["p"], "consumed=99 < quota=100 应放行"

        # 再加 1 token,consumed=100 == quota:是 over,应剔出
        await ledger.record(
            provider="p", model="m", prompt_tokens=1, completion_tokens=0, cost=None
        )
        survivors_100 = await cascade._cost_gate.survivors(["p"])
        assert survivors_100 == [], "consumed=100 == quota=100 应剔出(>= 严格边界)"

    _run(body())


def test_cost_gate_quota_zero_blocks_provider_with_zero_consumption(tmp_path):
    """S2.4 defer 盲区 #2:quota=0 → consumed=0 即 0 >= 0,fail-loud 剔出。

    保护配置错误(quota 误设 0)的 provider 不被静默放行薅羊毛。
    """
    candidates = [("p", _StubOK("p"), "k")]
    cascade, _h, _br, ledger = _make_cascade(
        tmp_path, candidates, quotas={"p": 0}
    )

    async def body():
        await ledger.init()
        # 完全没记账(consumed=0)
        survivors = await cascade._cost_gate.survivors(["p"])
        assert survivors == [], "quota=0 应立即剔出(0 >= 0,配置错也不薅羊毛)"
        # is_over_budget 纯函数同语义
        assert cascade._cost_gate.is_over_budget("p", 0) is True

    _run(body())


def test_compliance_check_runs_before_cost_gate(tmp_path):
    """S2.4 defer 盲区 #3:合规层 ① 早返,**不调 _surviving_candidates**(不查 ledger,
    不查 health),layer ① compliance → ② health/cost → ③ plan 顺序契约证。

    构造:同 entity 多账号违规 + 假装 over_budget 状态(均不应被查到)。
    防假绿:① cascade 早返 compliance_blocked ② counter == {}(不调任何 provider)
    ③ 标志:用 spy CostGate.survivors 验它**未被调**(若顺序错,合规先放行,会查 ledger)。
    """
    spy_calls: list[list[str]] = []

    class _SpyCostGate(CostGate):
        async def survivors(self, names):
            spy_calls.append(list(names))
            return await super().survivors(names)

    counter: dict[str, int] = {}
    entries = {
        "acct-a": _entry(
            "acct-a",
            entity="paid-x",
            api_key_env="KEY_A",
            is_free=False,
            cost_multiplier=1.0,
        ),
        "acct-b": _entry(
            "acct-b",
            entity="paid-x",
            api_key_env="KEY_B",
            is_free=False,
            cost_multiplier=1.0,
        ),
    }
    candidates = [
        ("acct-a", _StubOK("acct-a", counter=counter), "k-a"),
        ("acct-b", _StubOK("acct-b", counter=counter), "k-b"),
    ]
    health = HealthStore(tmp_path / "health.db")
    breaker = CircuitBreaker(tmp_path / "circuit.db")
    ledger = LedgerStore(tmp_path / "ledger.db")
    spy_cost_gate = _SpyCostGate(ledger, {"acct-a": 999, "acct-b": 999})
    cascade = Cascade(
        store=TraceStore(tmp_path / "trace.db"),
        breaker=breaker,
        strategy=_FixedOrder(["acct-a", "acct-b"]),
        candidates=candidates,
        health_store=health,
        policy_enforcer=PolicyEnforcer(entries.values()),  # 多账号 → ComplianceError
        ledger=ledger,
        cost_gate=spy_cost_gate,
    )

    async def body():
        await health.init()
        try:
            return await cascade.run("ping", correlation_id="cor-order")
        finally:
            await health.close()

    res = _run(body())
    assert res.success is False
    assert res.last_reason == "compliance_blocked"
    assert counter == {}, "合规拒不应触达 provider"
    assert spy_calls == [], (
        "compliance 层 ① 必须先于 cost_gate(layer ②)——本测试合规违规时,"
        f"cost_gate.survivors 不应被调,实际 spy_calls={spy_calls}"
    )


# ── 自由覆盖:全局派生 OPEN(子片 1 OpenCode #1 全局熔断阻断) ─────────────


def test_breaker_global_open_blocks_all_via_derived_aggregate(tmp_path):
    """子片 1 OpenCode #1 加强:派生 global OPEN(全部 key OPEN)→ allow_request 全
    返 global_open,所有 provider 被 hard-skip;链 attempt 计数全 acquire+immediate-skip。

    防假绿:counter == {}(全部 provider 被 allow_request 拒,不调 complete);
    cascade 返回 success=False,last_reason 链终态。
    """
    counter: dict[str, int] = {}
    names = ["p1", "p2"]
    candidates = [
        (n, _StubOK(n, counter=counter), f"k-{n}") for n in names
    ]
    cascade, health, breaker, _ldg = _make_cascade(
        tmp_path, candidates, key_hard_threshold=1  # 1 次 HARD 即 OPEN
    )

    async def body():
        await health.init()
        try:
            # 把所有 key 一次性 trip 到 OPEN(派生 global OPEN)
            for n in names:
                breaker.record_failure(n, f"k-{n}", TripReason.HARD)
            # 验证全部 OPEN
            for n in names:
                ks = breaker.get_key_state(n, f"k-{n}")
                assert ks.state.value == "open", (
                    f"key {n} 应已 OPEN,实际 {ks.state}"
                )
            return await cascade.run("ping", correlation_id="cor-global")
        finally:
            await health.close()

    res = _run(body())
    assert res.success is False
    # 全部跳被 allow_request 拒,无一 provider 被调
    assert counter == {}, f"全 OPEN 时不应调 provider;实际 {counter}"
    # last_reason 是最后一跳的拒因(global_open 或 key_open,看 allow_request 顺序)
    # cascade.allow 检查 global 派生 → key:此处全 key OPEN → 派生 global OPEN → reason="global_open"
    assert res.last_reason in ("global_open", "key_open"), (
        f"应 reason ∈ {{global_open, key_open}}; 实际 {res.last_reason}"
    )


# ── 直查 SQLite 辅助 ─────────────────────────────────────────────────────


def _trace_rows_db(tmp_path: Path) -> list[dict]:
    import sqlite3

    conn = sqlite3.connect(tmp_path / "trace.db")
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM trace ORDER BY idempotency_key"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()

```

---

## §源码锚点 ② — `Cascade.run()` 关键分支(`src/llm_router/api/cascade.py:220-396`)

```python
async def run(self, prompt, *, correlation_id, session_id=None):
    # ① 合规门(layer ①,最先;ComplianceError → compliance_blocked 早返)
    if self._policy_enforcer is not None:
        try:
            self._policy_enforcer.check()
        except ComplianceError:
            return CascadeResult(None, None, False, 0, "compliance_blocked")
    await self._ensure_store()
    # ② 路由前 health/cost 过滤(_surviving_candidates)
    survivors = await self._surviving_candidates()
    if not survivors:
        return CascadeResult(None, None, False, 0, "no_candidates")
    # ③ session_id 进 context
    context = {"session_id": session_id}
    if session_id is not None:
        ...
    # ④ strategy.plan 排链
    chain = self._strategy.plan(survivors, context)
    parent_trace_id = None
    prev_provider = None
    last_reason = "initial"
    attempted = 0
    for idx, name in enumerate(chain):
        # budget 门(check_hop_budget(idx, self._budget))
        if idx > 0 and not check_hop_budget(idx, self._budget):
            ...  # 写 budget_exhausted 终态返
        attr = (initial_attribution(name) if idx == 0 else advance(idx - 1, last_reason, prev_provider, name))
        out = await self._store.acquire(...)
        if out.status is AcquireStatus.REPLAYED:
            return CascadeResult(out.cached_result, None, True, attempted, "replayed")
        attempted += 1
        try:
            provider, key = self._providers[name]
        except KeyError:
            ...  # provider_removed_during_rollback
        # breaker 判定(allow_request: 先 global,再 key)
        dec = self._breaker.allow_request(name, key)
        if not dec.allowed:
            await self._store.commit(trace_id=out.trace_id, result="", hop_attribution=attr.to_json())
            last_reason = dec.reason  # global_open / key_open / half_open_busy
            prev_provider = name
            parent_trace_id = out.trace_id
            continue
        # provider.complete
        try:
            text, model, usage = await provider.complete(prompt)
        except ProviderError:
            self._breaker.record_failure(name, key, TripReason.HARD)
            await self._store.commit(trace_id=out.trace_id, result="", hop_attribution=attr.to_json())
            last_reason = "hard_failure"
            prev_provider = name
            parent_trace_id = out.trace_id
            continue
        # token 用量记账(best-effort,先于完整性判定)
        await self._record_usage(name, model, usage)
        # 内容完整性
        if not is_complete(text, model):
            self._breaker.record_failure(name, key, TripReason.SOFT_CONTENT)
            await self._store.commit(trace_id=out.trace_id, result="", hop_attribution=attr.to_json())
            last_reason = "soft_content"
            prev_provider = name
            parent_trace_id = out.trace_id
            continue
        # 成功
        self._breaker.record_success(name, key)
        await self._store.commit(trace_id=out.trace_id, result=text, hop_attribution=attr.to_json())
        return CascadeResult(text, model, True, attempted, attr.reason)
    return CascadeResult(None, None, False, attempted, last_reason)
```

`_surviving_candidates`:health hard-skip(alive=False)→ cost_gate.survivors(超预算剔出);两过滤均 fail-open。

---

## §源码锚点 ③ — `CostGate`(`src/llm_router/api/cost_gate.py`)

```python
class CostGate:
    def __init__(self, ledger: LedgerStore, quotas: dict[str, int]) -> None:
        self._ledger = ledger
        self._quotas = dict(quotas)

    def is_over_budget(self, name: str, consumed_tokens: int) -> bool:
        # >= 严格边界:consumed == quota 即 over
        quota = self._quotas.get(name)
        return quota is not None and consumed_tokens >= quota

    async def survivors(self, names: list[str]) -> list[str]:
        result = []
        for name in names:
            quota = self._quotas.get(name)
            if quota is None:
                result.append(name)  # 无 quota 配置 → 无限放行
                continue
            try:
                total = await self._ledger.total(name)
            except Exception:
                return list(names)  # fail-open
            consumed = total["prompt_tokens"] + total["completion_tokens"]
            if consumed < quota:
                result.append(name)
            # else: 超预算剔出
        return result
```

---

## §源码锚点 ④ — `CircuitBreaker` 关键(`src/llm_router/resilience/circuit_breaker.py`)

```python
# Gap1: key 连续 3 次硬失败 → OPEN(默认 key_hard_threshold=3)
# Gap3 派生:provider OPEN ⟺ 该 provider 至少 1 个 key 且全部 OPEN
#           global OPEN ⟺ 至少 1 个 provider 且全部 provider OPEN
# 一个生产边界:record_success 内 self._keys.get(...) 不 setdefault → 首次成功的
# provider 不进 _keys;_global_is_open 仅遍历 _keys 已知 provider → 早期场景下
# 单一 provider trip 可能让"全 OPEN"误判为 global OPEN(本测试用 record_failure(SOFT)
# 注册绕开,见 test_cascade_skips_pre_tripped_open_key_and_routes_to_good)。

def allow_request(self, provider, key) -> Decision:
    if self._global_is_open():
        return Decision(False, "global_open")
    ks = self._keys.get((provider, key))
    if ks is None or ks.state == CircuitState.CLOSED:
        return Decision(True, "")
    if ks.state == CircuitState.OPEN:
        if ks.next_probe_at is not None and now >= ks.next_probe_at:
            ...  # HALF_OPEN 放 1 探测
        return Decision(False, "key_open", retry)
    # HALF_OPEN
    if ks.probe_in_flight:
        return Decision(False, "half_open_busy")
    ...
```

---

## §源码锚点 ⑤ — `PolicyEnforcer.check()`(`src/llm_router/api/policy_enforcer.py`)

```python
class PolicyEnforcer:
    def __init__(self, entries):
        # 派生 alias_table + violations(同 entity 多账号)
        ...
    def check(self):
        # 违规 → ComplianceError(once-guard 防日志刷屏);合规静默返
        if not self._violations:
            return
        if not self._logged:
            ...
            self._logged = True
        raise ComplianceError(self._violations)
```

---

## §源码锚点 ⑥ — `routing.hop`(`src/llm_router/routing/hop.py`)

```python
DEFAULT_RETRY_BUDGET = 6
HOP_REASONS = {"initial", "key_open", "global_open", "half_open_busy",
                "hard_failure", "soft_content", "budget_exhausted"}

@dataclass(frozen=True)
class HopAttribution:
    depth: int
    reason: str
    from_provider: Optional[str]
    to_provider: Optional[str]

def initial_attribution(to_provider): return HopAttribution(0, "initial", None, to_provider)
def advance(current_depth, reason, from_provider, to_provider): return HopAttribution(current_depth+1, reason, from_provider, to_provider)
def budget_exhausted(depth, from_provider): return HopAttribution(depth, "budget_exhausted", from_provider, None)
def check_hop_budget(current_depth, budget=6): return current_depth < budget
```

---

## §源码锚点 ⑦ — `is_complete`(`src/llm_router/resilience/content_integrity.py`)

```python
def is_complete(text, model):
    if text is None or model is None: return False
    if not isinstance(text, str) or not isinstance(model, str): return False
    return text.strip() != "" and model.strip() != ""
```

---

## 输出格式要求

```
# OpenCode 异构对抗审 — 子片 3

## 维度 1:覆盖度
[标签] 严重度 file:line 内容
...

## 维度 2:假绿陷阱
...

## 维度 3:A2 breaker × policy 测试设计(record_failure SOFT 注册场景)
...

## 维度 4:顺序契约证(spy CostGate)
...

## 维度 5:状态污染 / tmp_path 隔离
...

## 自由发掘
...

---

[CONSENSUS]/[CHALLENGE] 整文件结论(单独一行,最后)
```
