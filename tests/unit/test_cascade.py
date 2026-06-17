"""S2.1b · 生产 Cascade 单测(从 tests/e2e/_fallback_orchestrator.py 提炼)。

Cascade 串起真实生产组件:breaker + routing.hop + content_integrity + store + strategy.plan()。
覆盖:首跳成功 / HARD(ProviderError)fallback / SOFT(残缺)fallback / 跳过熔断 provider /
多跳 depth 单调 / 幂等 replay / budget=6 硬停止 / global 冻结 / strategy.plan 决定链序 /
**非 ProviderError(编程 bug)上抛不吞**(design 点2 DEFEND,防错误 trip 熔断)。

测试模式沿用 tests/e2e/test_fallback_e2e:同步 def + asyncio.run 包异步(无 pytest-asyncio,
不动 hash 锁);breaker 钩子 monkeypatch _jitter_fn=0.0 + _now_override=1000.0;tmp_path 临时 DB。
"""
from __future__ import annotations

import asyncio

import pytest

from llm_router.api.cascade import Cascade
from llm_router.api.strategy import RoutingStrategy
from llm_router.providers.base import Provider, ProviderError
from llm_router.resilience.circuit_breaker import CircuitBreaker, CircuitState, TripReason
from llm_router.routing.hop import parse_attribution
from llm_router.store.trace import TraceStore


def _run(coro):
    return asyncio.run(coro)


def _trip_key(breaker, provider, key):
    for _ in range(3):
        breaker.record_failure(provider=provider, key=key, reason=TripReason.HARD)


def _seed_closed_key(breaker, provider, key):
    breaker.record_failure(provider=provider, key=key, reason=TripReason.HARD)


def _new_breaker(tmp_path):
    return CircuitBreaker(db_path=tmp_path / "circuit.db", key_hard_threshold=3)


class _FakeProvider(Provider):
    """可控 provider:可配返成功/残缺/抛异常,记录调用计数(证明真调/真不调,防假绿)。"""

    def __init__(self, name, *, text="real", model="mX", raises=None, incomplete=False, counter=None):
        self.name = name
        self._text = text
        self._model = model
        self._raises = raises  # 异常实例(complete 时抛);ProviderError=硬失败,其他=bug
        self._incomplete = incomplete  # True → 返空文本(残缺 → SOFT)
        self._counter = counter  # dict[name]->int,记录调用次数

    async def complete(self, prompt):
        if self._counter is not None:
            self._counter[self.name] = self._counter.get(self.name, 0) + 1
        if self._raises is not None:
            raise self._raises
        if self._incomplete:
            return "", self._model, None
        return self._text, self._model, None


class _FixedOrderStrategy(RoutingStrategy):
    """确定性策略:plan 返固定序(隔离 cascade 逻辑,不耦合 ε;epsilon 自身由 test_epsilon_greedy 覆盖)。"""

    def __init__(self, order):
        self._order = list(order)

    def plan(self, candidates, context):
        seen = set(candidates)
        return [c for c in self._order if c in seen]

    def select_provider(self, candidates, context):
        return self.plan(candidates, context)[0]


def _cascade(tmp_path, breaker, strategy, providers, *, budget=6):
    """建 Cascade + store(providers 为 _FakeProvider 列表,统一 key='k1')。"""
    store = TraceStore(tmp_path / "trace.db")
    cands = [(p.name, p, "k1") for p in providers]
    return Cascade(store, breaker, strategy, cands, budget=budget), store


# ── L1:首跳成功 + hop 归因 ─────────────────────────────────────────────────


def test_success_on_first_hop(tmp_path, monkeypatch):
    """primary 首跳即成功 → 链长 1,hop0=initial,result 来自 primary。"""
    breaker = _new_breaker(tmp_path)
    monkeypatch.setattr(breaker, "_jitter_fn", lambda: 0.0)
    breaker._now_override = 1000.0
    strat = _FixedOrderStrategy(["pA"])
    cascade, store = _cascade(tmp_path, breaker, strat, [_FakeProvider("pA", model="mA")])

    async def body():
        try:
            res = await cascade.run("ping", correlation_id="CID")
            chain = await store.get_chain("CID")
            return res, chain
        finally:
            await store.close()

    res, chain = _run(body())
    assert res.success is True and res.final_text == "real"
    assert res.final_model == "mA"
    assert len(chain) == 1
    h0 = parse_attribution(chain[0].hop_attribution)
    assert h0.depth == 0 and h0.reason == "initial" and h0.to_provider == "pA"
    assert chain[0].result == "real"


# ── L2:fallback 决策 ───────────────────────────────────────────────────────


def test_fallback_on_hard_provider_error(tmp_path, monkeypatch):
    """pA 抛 ProviderError → record_failure(HARD) → 跳 pB 成功。

    断言:hop1 reason=hard_failure;pA/k1 hard_failures=1(真硬失败计数)。
    """
    breaker = _new_breaker(tmp_path)
    monkeypatch.setattr(breaker, "_jitter_fn", lambda: 0.0)
    breaker._now_override = 1000.0
    strat = _FixedOrderStrategy(["pA", "pB"])
    cascade, store = _cascade(
        tmp_path,
        breaker,
        strat,
        [_FakeProvider("pA", raises=ProviderError("pA down")), _FakeProvider("pB", model="mB")],
    )

    async def body():
        try:
            res = await cascade.run("ping", correlation_id="CID")
            chain = await store.get_chain("CID")
            return res, chain
        finally:
            await store.close()

    res, chain = _run(body())
    assert res.success is True and res.final_text == "real"
    assert breaker.get_key_state("pA", "k1").hard_failures == 1
    assert len(chain) == 2
    h1 = parse_attribution(chain[1].hop_attribution)
    assert h1.reason == "hard_failure" and h1.from_provider == "pA" and h1.to_provider == "pB"


def test_fallback_on_soft_incomplete_response(tmp_path, monkeypatch):
    """pA 返残缺(空文本)→ is_complete False → record_failure(SOFT_CONTENT) → 跳 pB 成功。

    断言:hop1 reason=soft_content;pA/k1 soft_failures=1。
    """
    breaker = _new_breaker(tmp_path)
    monkeypatch.setattr(breaker, "_jitter_fn", lambda: 0.0)
    breaker._now_override = 1000.0
    strat = _FixedOrderStrategy(["pA", "pB"])
    cascade, store = _cascade(
        tmp_path,
        breaker,
        strat,
        [_FakeProvider("pA", incomplete=True), _FakeProvider("pB", model="mB")],
    )

    async def body():
        try:
            res = await cascade.run("ping", correlation_id="CID")
            chain = await store.get_chain("CID")
            return res, chain
        finally:
            await store.close()

    res, chain = _run(body())
    assert res.success is True and res.final_text == "real"
    assert breaker.get_key_state("pA", "k1").soft_failures == 1
    h1 = parse_attribution(chain[1].hop_attribution)
    assert h1.reason == "soft_content" and h1.from_provider == "pA"


def test_fallback_skips_open_provider(tmp_path, monkeypatch):
    """pA 单 key 熔断(OPEN)→ allow 拒 → 跳 pB 成功。

    断言:hop1 reason=key_open/from pA;parent 链 pB→pA。
    """
    breaker = _new_breaker(tmp_path)
    monkeypatch.setattr(breaker, "_jitter_fn", lambda: 0.0)
    breaker._now_override = 1000.0
    _seed_closed_key(breaker, "pB", "k1")  # pB 存在 CLOSED → global 不冻
    _trip_key(breaker, "pA", "k1")  # pA 唯一 key OPEN
    assert breaker.allow_request("pA", "k1").allowed is False
    strat = _FixedOrderStrategy(["pA", "pB"])
    cascade, store = _cascade(
        tmp_path,
        breaker,
        strat,
        [_FakeProvider("pA", text="unused"), _FakeProvider("pB", model="mB")],
    )

    async def body():
        try:
            res = await cascade.run("ping", correlation_id="CID")
            chain = await store.get_chain("CID")
            return res, chain
        finally:
            await store.close()

    res, chain = _run(body())
    assert res.success is True and res.final_text == "real"
    assert len(chain) == 2
    h1 = parse_attribution(chain[1].hop_attribution)
    assert h1.reason == "key_open" and h1.from_provider == "pA" and h1.to_provider == "pB"
    assert chain[1].parent_correlation_id == chain[0].trace_id


def test_multi_hop_depth_attribution_monotonic(tmp_path, monkeypatch):
    """3 级:pA(key_open)→pB(soft_content)→pC(success)。depth 0/1/2 严格递增;parent 链。"""
    breaker = _new_breaker(tmp_path)
    monkeypatch.setattr(breaker, "_jitter_fn", lambda: 0.0)
    breaker._now_override = 1000.0
    _seed_closed_key(breaker, "pB", "k1")
    _trip_key(breaker, "pA", "k1")
    strat = _FixedOrderStrategy(["pA", "pB", "pC"])
    cascade, store = _cascade(
        tmp_path,
        breaker,
        strat,
        [
            _FakeProvider("pA", text="unused"),
            _FakeProvider("pB", incomplete=True),
            _FakeProvider("pC", model="mC"),
        ],
    )

    async def body():
        try:
            res = await cascade.run("ping", correlation_id="CID")
            chain = await store.get_chain("CID")
            return res, chain
        finally:
            await store.close()

    res, chain = _run(body())
    assert res.success is True and res.final_text == "real"
    assert len(chain) == 3
    depths = [parse_attribution(r.hop_attribution).depth for r in chain]
    assert depths == [0, 1, 2]
    h1 = parse_attribution(chain[1].hop_attribution)
    h2 = parse_attribution(chain[2].hop_attribution)
    assert h1.reason == "key_open" and h1.from_provider == "pA"
    assert h2.reason == "soft_content" and h2.from_provider == "pB"
    assert chain[2].parent_correlation_id == chain[1].trace_id


# ── 幂等 replay ────────────────────────────────────────────────────────────


def test_idempotent_replay_not_counted_as_new_hop(tmp_path, monkeypatch):
    """同 correlation_id 二次 run → acquire REPLAYED → 不计新 hop。守幂等(BUG-幂等-01)。"""
    breaker = _new_breaker(tmp_path)
    monkeypatch.setattr(breaker, "_jitter_fn", lambda: 0.0)
    breaker._now_override = 1000.0
    strat = _FixedOrderStrategy(["pA"])
    cascade, store = _cascade(tmp_path, breaker, strat, [_FakeProvider("pA")])

    async def body():
        try:
            r1 = await cascade.run("ping", correlation_id="CID")
            r2 = await cascade.run("ping", correlation_id="CID")  # 同 CID → replay
            chain = await store.get_chain("CID")
            return r1, r2, chain
        finally:
            await store.close()

    r1, r2, chain = _run(body())
    assert r1.success and r1.final_text == "real"
    assert r2.success and r2.last_reason == "replayed"
    assert len(chain) == 1, "幂等 replay 不应产生新 hop"


# ── L3:budget=6 硬停止 + global 冻结 ────────────────────────────────────────


def test_global_open_freezes_all_providers(tmp_path, monkeypatch):
    """全部 provider 唯一 key 都 OPEN → global 冻结 → 每个都被拒,provider_fn 零调用。"""
    breaker = _new_breaker(tmp_path)
    monkeypatch.setattr(breaker, "_jitter_fn", lambda: 0.0)
    breaker._now_override = 1000.0
    _trip_key(breaker, "pA", "k1")
    _trip_key(breaker, "pB", "k1")
    assert breaker.get_global_state().state == CircuitState.OPEN
    calls: dict[str, int] = {}
    strat = _FixedOrderStrategy(["pA", "pB"])
    cascade, store = _cascade(
        tmp_path,
        breaker,
        strat,
        [_FakeProvider("pA", counter=calls), _FakeProvider("pB", counter=calls)],
    )

    async def body():
        try:
            res = await cascade.run("ping", correlation_id="CID")
            chain = await store.get_chain("CID")
            return res, chain
        finally:
            await store.close()

    res, chain = _run(body())
    assert res.success is False
    assert res.last_reason == "global_open"
    assert calls == {}, "global_open 冻结时 provider.complete 必须不被调用"


def test_total_retry_budget_six_hard_stop(tmp_path, monkeypatch):
    """7 个 provider 全 HARD 失败 → budget=6 → 最多试 6 个,第 7 个(pG)被拦(调用计数==0 证明)。"""
    breaker = _new_breaker(tmp_path)
    monkeypatch.setattr(breaker, "_jitter_fn", lambda: 0.0)
    breaker._now_override = 1000.0
    calls: dict[str, int] = {}
    names = ["pA", "pB", "pC", "pD", "pE", "pF", "pG"]
    strat = _FixedOrderStrategy(names)
    cascade, store = _cascade(
        tmp_path,
        breaker,
        strat,
        [_FakeProvider(n, raises=ProviderError(f"{n} down"), counter=calls) for n in names],
        budget=6,
    )

    async def body():
        try:
            res = await cascade.run("ping", correlation_id="CID")
            chain = await store.get_chain("CID")
            return res, chain
        finally:
            await store.close()

    res, chain = _run(body())
    assert res.success is False
    assert res.last_reason == "budget_exhausted"
    assert len(chain) == 7, f"链长应 7(6 hard + 1 budget_exhausted),实际 {len(chain)}"
    last = parse_attribution(chain[-1].hop_attribution)
    assert last.reason == "budget_exhausted" and last.to_provider is None
    assert calls.get("pG", 0) == 0, "第 7 个 provider 必须未被调用(budget 拦下)"
    assert all(calls.get(n) == 1 for n in names[:6]), f"前 6 个应各调一次,实际 {calls}"


# ── strategy.plan 决定链序 + 非 ProviderError 上抛 ──────────────────────────


def test_strategy_plan_determines_chain_order(tmp_path, monkeypatch):
    """strategy.plan 的序即 Cascade 尝试序;_FixedOrderStrategy 反序 → pB 先于 pA。

    证明 Cascade 用 plan() 返的链(而非 candidates 原序)。
    """
    breaker = _new_breaker(tmp_path)
    monkeypatch.setattr(breaker, "_jitter_fn", lambda: 0.0)
    breaker._now_override = 1000.0
    strat = _FixedOrderStrategy(["pB", "pA"])  # 故意反序:pB primary
    cascade, store = _cascade(
        tmp_path,
        breaker,
        strat,
        [_FakeProvider("pA", text="fromA"), _FakeProvider("pB", text="fromB")],
    )

    async def body():
        try:
            res = await cascade.run("ping", correlation_id="CID")
            chain = await store.get_chain("CID")
            return res, chain
        finally:
            await store.close()

    res, chain = _run(body())
    assert res.success and res.final_text == "fromB", "plan 序 pB 在前 → 首跳 pB 成功"
    assert chain[0].provider == "pB"


def test_non_provider_error_propagates_not_masked(tmp_path, monkeypatch):
    """provider 抛**非** ProviderError(模拟编程 bug,如 KeyError)→ Cascade 不吞、不 trip 熔断。

    design 点2 DEFEND 的核心保证:只 except ProviderError;其余上抛暴露 bug,且 breaker 不被错误计数。
    """
    breaker = _new_breaker(tmp_path)
    monkeypatch.setattr(breaker, "_jitter_fn", lambda: 0.0)
    breaker._now_override = 1000.0
    strat = _FixedOrderStrategy(["pA"])

    def _bug():
        raise KeyError("simulated adapter bug")  # 非 ProviderError

    class _BuggyProvider(Provider):
        name = "pA"

        async def complete(self, prompt):
            raise KeyError("simulated adapter bug")

    cascade, store = _cascade(tmp_path, breaker, strat, [_BuggyProvider()])

    async def body():
        try:
            await cascade.run("ping", correlation_id="CID")
        finally:
            await store.close()

    with pytest.raises(KeyError):
        _run(body())
    # breaker 不应被错误 trip(无 hard/soft 计数)
    ks = breaker.get_key_state("pA", "k1")
    assert ks.hard_failures == 0 and ks.soft_failures == 0, "非 ProviderError 不应计入熔断"
