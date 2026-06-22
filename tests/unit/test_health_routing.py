"""S2.8c · 探活接线单测:路由 hard-skip 死亡 key(Face 2)+ CB HALF_OPEN 喂探活(Face 3)
+ Req3a 守卫(探活活但 CB OPEN 仍跳过)。

守 health-probe/spec.md:
  - Req 4:死亡 key(alive=False)路由时 hard-skip 剔除,**先于**字典序排序;从未探活的保留
    (无信号不过滤);health 查询失败 → fail-open 降级(health 非权威新鲜度信号,不崩请求)。
  - Req 3a:CB OPEN 退避期,即便探活 alive=True 仍跳过(不强制关未到期 OPEN)。
  - Req 3b:探活成功喂 HALF_OPEN → record_success 恢复;但 OPEN 未到期不动(feed_probe_success 仅 HALF_OPEN 才动)。

TDD:本套件先 RED——Cascade 无 health_store 参数/feed_probe_success 方法时即失败。
测试模式沿用 test_cascade:_FakeProvider + counter(证真调/真不调,防假绿);breaker 钩子
_jitter_fn=0.0 + _now_override;tmp_path 临时 DB;sync def + asyncio.run 包异步。
"""
from __future__ import annotations

import asyncio

from llm_router.api.cascade import Cascade
from llm_router.api.strategy import RoutingStrategy
from llm_router.providers.base import ChatResult, Provider
from llm_router.resilience.circuit_breaker import CircuitBreaker, CircuitState, TripReason
from llm_router.store.health_store import HealthStore
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
    """可控 provider + 调用计数(证明真调/真不调,防假绿)。"""

    def __init__(self, name, *, text="real", model="mX", raises=None, counter=None):
        self.name = name
        self._text = text
        self._model = model
        self._raises = raises  # ProviderError=硬失败
        self._counter = counter  # dict[name]->int

    async def complete(self, messages, *, tools=None, tool_choice=None):
        if self._counter is not None:
            self._counter[self.name] = self._counter.get(self.name, 0) + 1
        if self._raises is not None:
            raise self._raises
        return ChatResult(content=self._text, model=self._model, usage=None)


class _FixedOrderStrategy(RoutingStrategy):
    """确定性策略:plan 返固定序(隔离 cascade 逻辑,不耦合 ε)。"""

    def __init__(self, order):
        self._order = list(order)

    def plan(self, candidates, context):
        seen = set(candidates)
        return [c for c in self._order if c in seen]


def _cascade(tmp_path, breaker, strategy, providers, *, health_store=None, budget=6):
    """建 Cascade(providers 为 _FakeProvider 列表,统一 key='k1')。health_store 可选。"""
    store = TraceStore(tmp_path / "trace.db")
    cands = [(p.name, p, "k1") for p in providers]
    return Cascade(store, breaker, strategy, cands, health_store=health_store, budget=budget), store


# ── Face 2:路由 hard-skip 死亡 key(spec Req 4)───────────────────────────────


def test_run_hard_skips_dead_provider(tmp_path, monkeypatch):
    """pB 探活 alive=False → hard-skip 剔除;pA(alive=True)首跳成功,pB.complete 零调用。"""
    breaker = _new_breaker(tmp_path)
    monkeypatch.setattr(breaker, "_jitter_fn", lambda: 0.0)
    breaker._now_override = 1000.0
    strat = _FixedOrderStrategy(["pA", "pB"])
    calls: dict[str, int] = {}
    health = HealthStore(tmp_path / "health.db")

    async def body():
        await health.init()
        try:
            await health.record_probe("pA", latency_ms=10.0, alive=True)
            await health.record_probe("pB", latency_ms=None, alive=False)  # 死
            cascade, store = _cascade(
                tmp_path, breaker, strat,
                [_FakeProvider("pA", counter=calls), _FakeProvider("pB", counter=calls)],
                health_store=health,
            )
            try:
                return await cascade.run([{"role":"user","content":"ping"}], correlation_id="CID")
            finally:
                await store.close()
        finally:
            await health.close()

    res = _run(body())
    assert res.success is True and res.final_text == "real"
    assert calls.get("pA") == 1
    assert calls.get("pB", 0) == 0, "死亡 key 必须 hard-skip,pB.complete 不被调用(不占 hop)"


def test_run_keeps_never_probed_provider(tmp_path, monkeypatch):
    """pC 从未探活(不在 health.db)→ 无信号不过滤 → 保留可路由(spec:只剔 alive=False)。"""
    breaker = _new_breaker(tmp_path)
    monkeypatch.setattr(breaker, "_jitter_fn", lambda: 0.0)
    breaker._now_override = 1000.0
    strat = _FixedOrderStrategy(["pA", "pC"])
    calls: dict[str, int] = {}
    health = HealthStore(tmp_path / "health.db")

    async def body():
        await health.init()
        try:
            await health.record_probe("pA", latency_ms=10.0, alive=False)  # pA 死
            # pC 从不 record_probe → 不在 db
            cascade, store = _cascade(
                tmp_path, breaker, strat,
                [_FakeProvider("pA", text="unused", counter=calls), _FakeProvider("pC", counter=calls)],
                health_store=health,
            )
            try:
                return await cascade.run([{"role":"user","content":"ping"}], correlation_id="CID")
            finally:
                await store.close()
        finally:
            await health.close()

    res = _run(body())
    assert res.success is True and res.final_text == "real"
    assert calls.get("pA", 0) == 0, "pA 死 → 跳过"
    assert calls.get("pC") == 1, "pC 从未探活 → 保留(无信号不过滤)→ 被路由"


def test_filter_failopen_when_store_not_init(tmp_path, monkeypatch):
    """health_store 未 init → 查询抛 → fail-open 不过滤(health 非权威,降级不崩请求)。"""
    breaker = _new_breaker(tmp_path)
    monkeypatch.setattr(breaker, "_jitter_fn", lambda: 0.0)
    breaker._now_override = 1000.0
    strat = _FixedOrderStrategy(["pA"])
    calls: dict[str, int] = {}
    health = HealthStore(tmp_path / "health.db")  # 故意不 init

    async def body():
        cascade, store = _cascade(
            tmp_path, breaker, strat,
            [_FakeProvider("pA", counter=calls)],
            health_store=health,
        )
        try:
            return await cascade.run([{"role":"user","content":"ping"}], correlation_id="CID")
        finally:
            await store.close()

    res = _run(body())
    assert res.success is True, "health 未 init → fail-open → pA 仍被尝试且成功"
    assert calls.get("pA") == 1


def test_no_health_store_no_filtering(tmp_path, monkeypatch):
    """health_store=None(旧构造/test helper/无 lifespan 场景)→ 不过滤,行为同 S2.8c 前。"""
    breaker = _new_breaker(tmp_path)
    monkeypatch.setattr(breaker, "_jitter_fn", lambda: 0.0)
    breaker._now_override = 1000.0
    strat = _FixedOrderStrategy(["pA"])
    cascade, store = _cascade(tmp_path, breaker, strat, [_FakeProvider("pA")])  # 无 health_store

    async def body():
        try:
            return await cascade.run([{"role":"user","content":"ping"}], correlation_id="CID")
        finally:
            await store.close()

    res = _run(body())
    assert res.success is True and res.final_text == "real"


def test_run_empty_candidates_returns_failure_not_crash(tmp_path, monkeypatch):
    """候选为空(全死或构造空)→ run 返明确失败(no_candidates),不抛 opaque NoCandidateError。

    守 S2.8c 对抗审 MED:不依赖"mock 恒存活"隐式契约——survivors 空时 fail-loud
    返 CascadeResult(success=False, last_reason="no_candidates"),而非让 plan([]) 抛异常。
    """
    breaker = _new_breaker(tmp_path)
    monkeypatch.setattr(breaker, "_jitter_fn", lambda: 0.0)
    breaker._now_override = 1000.0
    strat = _FixedOrderStrategy([])
    cascade, store = _cascade(tmp_path, breaker, strat, [])  # 空 candidates

    async def body():
        try:
            return await cascade.run([{"role":"user","content":"ping"}], correlation_id="CID")
        finally:
            await store.close()

    res = _run(body())
    assert res.success is False
    assert res.last_reason == "no_candidates"


# ── Req 3a 守卫:探活活但 CB OPEN → 仍跳过(不强制关未到期 OPEN)────────────────


def test_probe_alive_but_cb_open_still_skipped(tmp_path, monkeypatch):
    """pA CB OPEN(退避未到期)+ 探活 alive=True → Cascade 仍跳 pA(CB 先判,Req 3a)。

    hard-skip 过滤只剔 alive=False;alive=True 不复活 CB-OPEN key。CB allow(⑤)独立拒,
    裁决优先级 **CB 状态先判 → 探活新鲜度后过滤**(spec Req 3)。
    """
    breaker = _new_breaker(tmp_path)
    monkeypatch.setattr(breaker, "_jitter_fn", lambda: 0.0)
    breaker._now_override = 1000.0
    _seed_closed_key(breaker, "pB", "k1")
    _trip_key(breaker, "pA", "k1")  # pA OPEN
    assert breaker.allow_request("pA", "k1").allowed is False
    strat = _FixedOrderStrategy(["pA", "pB"])
    calls: dict[str, int] = {}
    health = HealthStore(tmp_path / "health.db")

    async def body():
        await health.init()
        try:
            await health.record_probe("pA", latency_ms=10.0, alive=True)  # 探活说活
            cascade, store = _cascade(
                tmp_path, breaker, strat,
                [_FakeProvider("pA", text="unused", counter=calls), _FakeProvider("pB", counter=calls)],
                health_store=health,
            )
            try:
                return await cascade.run([{"role":"user","content":"ping"}], correlation_id="CID")
            finally:
                await store.close()
        finally:
            await health.close()

    res = _run(body())
    assert res.success is True and res.final_text == "real"
    assert calls.get("pA", 0) == 0, "CB OPEN 即便探活 alive=True 也必须跳过(Req 3a)"
    assert calls.get("pB") == 1


# ── Face 3:CB HALF_OPEN 喂探活(spec Req 3b)──────────────────────────────────


def test_feed_probe_success_closes_half_open(tmp_path, monkeypatch):
    """breaker k1 在 HALF_OPEN → feed_probe_success(name) → record_success → CLOSED(加速恢复)。"""
    breaker = _new_breaker(tmp_path)
    monkeypatch.setattr(breaker, "_jitter_fn", lambda: 0.0)
    breaker._now_override = 1000.0
    breaker.record_failure(provider="pA", key="k2", reason=TripReason.HARD)  # 健康兄弟防级联
    _trip_key(breaker, "pA", "k1")  # k1 OPEN
    breaker._now_override = 1031.0  # 过 30s 窗口
    breaker.allow_request(provider="pA", key="k1")  # → HALF_OPEN 放探测
    assert breaker.get_key_state("pA", "k1").state == CircuitState.HALF_OPEN

    strat = _FixedOrderStrategy(["pA"])
    cascade, store = _cascade(tmp_path, breaker, strat, [_FakeProvider("pA")])

    async def body():
        try:
            cascade.feed_probe_success("pA")
        finally:
            await store.close()

    _run(body())
    assert breaker.get_key_state("pA", "k1").state == CircuitState.CLOSED


def test_feed_probe_success_keeps_open_open(tmp_path, monkeypatch):
    """breaker k1 在 OPEN(退避未到期)→ feed_probe_success → 仍 OPEN(不强制关,Req 3a)。"""
    breaker = _new_breaker(tmp_path)
    monkeypatch.setattr(breaker, "_jitter_fn", lambda: 0.0)
    breaker._now_override = 1000.0
    breaker.record_failure(provider="pA", key="k2", reason=TripReason.HARD)  # 健康兄弟防级联
    _trip_key(breaker, "pA", "k1")  # k1 OPEN,now=1000 未到 next_probe(1030)
    assert breaker.get_key_state("pA", "k1").state == CircuitState.OPEN

    strat = _FixedOrderStrategy(["pA"])
    cascade, store = _cascade(tmp_path, breaker, strat, [_FakeProvider("pA")])

    async def body():
        try:
            cascade.feed_probe_success("pA")
        finally:
            await store.close()

    _run(body())
    assert breaker.get_key_state("pA", "k1").state == CircuitState.OPEN, "OPEN 未到期,探活不得强制关"


def test_feed_probe_success_unknown_name_noop(tmp_path):
    """feed_probe_success 未知 name → 不崩(noop,defensive)。"""
    breaker = _new_breaker(tmp_path)
    strat = _FixedOrderStrategy(["pA"])
    cascade, store = _cascade(tmp_path, breaker, strat, [_FakeProvider("pA")])

    async def body():
        try:
            cascade.feed_probe_success("nonexistent")  # 不抛
        finally:
            await store.close()

    _run(body())  # 不抛即过
