"""S2.8c · app lifespan 接线(Face 1)+ Prober on_alive 回调(Face 3)。

守 health-probe/spec.md Req 1(每 5min 主动探活,侧挂后台循环):
  - lifespan startup:建共享 HealthStore(init)+ 起 prober.run_loop(stop) 后台 task,
    on_alive=cascade.feed_probe_success 喂 HALF_OPEN(Face 3 接线)。
  - lifespan shutdown:stop_event.set + cancel task + store.close。
  - 无探活目标(生产无真 key)→ 不起后台 task(无谓空转)。

TDD:先 RED——app._make_lifespan / HealthProber(on_alive=...) 不存在时失败。
不依赖 TestClient 是否跑 lifespan:直接 async with 驱动 _make_lifespan(注入 tmp 实例),
确定性验证 task 生命周期。
"""
from __future__ import annotations

import asyncio

from fastapi import FastAPI

from llm_router.api.cascade import Cascade
from llm_router.api.strategy import RoutingStrategy
from llm_router.app import _make_lifespan
from llm_router.health.probe import HealthProber
from llm_router.providers.base import Provider, ProviderError
from llm_router.resilience.circuit_breaker import CircuitBreaker
from llm_router.store.health_store import HealthStore
from llm_router.store.trace import TraceStore


def _run(coro):
    return asyncio.run(coro)


class _OKProvider(Provider):
    name = "ok"

    async def complete(self, prompt):
        return "pong", "ok-model", None


class _BoomProvider(Provider):
    name = "boom"

    async def complete(self, prompt):
        raise ProviderError("down")


class _FixedOrderStrategy(RoutingStrategy):
    def __init__(self, order):
        self._order = list(order)

    def plan(self, candidates, context):
        seen = set(candidates)
        return [c for c in self._order if c in seen]


def _cascade_with_health(tmp_path):
    """建带 health_store 的 Cascade(tmp 实例,lifespan 测试用)。"""
    breaker = CircuitBreaker(db_path=tmp_path / "circuit.db", key_hard_threshold=3)
    strat = _FixedOrderStrategy(["ok"])
    store = TraceStore(tmp_path / "trace.db")
    health = HealthStore(tmp_path / "health.db")
    cands = [("ok", _OKProvider(), "k1")]
    cascade = Cascade(store, breaker, strat, cands, health_store=health)
    return cascade, store, health


# ── Face 1:lifespan task 生命周期 ─────────────────────────────────────────────


def test_lifespan_starts_probe_task_and_stops_cleanly(tmp_path):
    """有探活目标 → lifespan startup 起 run_loop task;shutdown stop+cancel,task 干净结束。"""
    cascade, store, _health = _cascade_with_health(tmp_path)

    async def body():
        lf = _make_lifespan(cascade, [("ok", _OKProvider())], interval_seconds=0.01)
        app = FastAPI(lifespan=lf)
        try:
            async with lf(app):
                assert app.state.probe_task is not None
                assert not app.state.probe_task.done()
                task = app.state.probe_task
            # 退出 lifespan 后 task 应结束(shutdown cancel)
            assert task.done()
        finally:
            await store.close()

    _run(body())


def test_lifespan_no_targets_no_task(tmp_path):
    """无探活目标(生产无真 key)→ 不起后台 task(无谓空转),lifespan 仍正常进出。"""
    cascade, store, _health = _cascade_with_health(tmp_path)

    async def body():
        lf = _make_lifespan(cascade, [], interval_seconds=0.01)
        app = FastAPI(lifespan=lf)
        try:
            async with lf(app):
                assert app.state.probe_task is None
        finally:
            await store.close()

    _run(body())


def test_lifespan_inits_and_closes_health_store(tmp_path):
    """lifespan startup init health_store(可写),shutdown close(连接归零)。"""
    cascade, store, health = _cascade_with_health(tmp_path)

    async def body():
        lf = _make_lifespan(cascade, [], interval_seconds=0.01)
        app = FastAPI(lifespan=lf)
        try:
            async with lf(app):
                # startup 后 store 已 init(_conn 非空)
                assert health._conn is not None
            # shutdown 后 close(_conn 归 None)
            assert health._conn is None
        finally:
            await store.close()

    _run(body())


# ── Face 3:Prober on_alive 回调(成功才报,失败不报)─────────────────────────────


def test_prober_on_alive_fires_only_on_success(tmp_path):
    """probe 成功 → on_alive(name) 被调;失败(ProviderError/超时)→ 不调。

    Face 3 接线:Prober 不判断 CB(守 probe.py "Prober 不判断,只 ping"),只通过回调报成功;
    失败不报(无信号喂 CB)。回调抛错须被吞(best-effort,不崩后台循环)。
    """
    fired: list[str] = []

    def on_alive(name):
        fired.append(name)

    async def body():
        health = HealthStore(tmp_path / "health.db")
        await health.init()
        try:
            ok = HealthProber(health, [("ok", _OKProvider())], probe_timeout_seconds=2.0, on_alive=on_alive)
            await ok.probe_one("ok", _OKProvider())
            boom = HealthProber(health, [("boom", _BoomProvider())], probe_timeout_seconds=2.0, on_alive=on_alive)
            await boom.probe_one("boom", _BoomProvider())
        finally:
            await health.close()

    _run(body())
    assert fired == ["ok"], "仅成功探活触发 on_alive;失败不触发"


def test_prober_on_alive_error_does_not_crash_loop(tmp_path):
    """on_alive 回调抛错 → probe_one 不崩(吞 + 记录),record_probe 仍正常落盘(best-effort 喂 CB)。"""
    async def body():
        health = HealthStore(tmp_path / "health.db")
        await health.init()
        try:
            def bad_callback(name):
                raise RuntimeError("cb boom")

            prober = HealthProber(
                health, [("ok", _OKProvider())], probe_timeout_seconds=2.0, on_alive=bad_callback
            )
            row = await prober.probe_one("ok", _OKProvider())  # 不抛
            assert row.alive is True  # record_probe 仍正常
        finally:
            await health.close()

    _run(body())  # 不抛即过


# ── Phase B · B3.2:DynamicScanner run_loop lifespan 接线 ───────────────────────


class _RecordingScanner:
    """Fake DynamicScanner:记录 run_loop 是否启动 + stop 优雅退出 + 注入 rebuild 回调。

    不做真 tick(零网络);只验 lifespan 起 task / stop 退出 / rebuild 回调被接进 on_tick_complete。
    """

    def __init__(self, store, *, on_tick_complete=None, **_kw):
        self.store = store
        self.on_tick_complete = on_tick_complete
        self.loop_started = False

    async def run_loop(self, stop_event, *, interval=3600.0):
        self.loop_started = True
        # 若注入了 on_tick_complete,触发一次(模拟 tick 有变更 → 重建回调被调)
        if self.on_tick_complete is not None:
            try:
                await self.on_tick_complete(None)
            except Exception:
                pass
        await stop_event.wait()  # 优雅退出:stop_event.set 唤醒


class _ApplyPolicySpy:
    """包一层 cascade,记录 apply_policy 调用(验重建回调真调 apply_policy)。"""

    def __init__(self, cascade):
        self._cascade = cascade
        self.applied = []

    def __getattr__(self, name):
        return getattr(self._cascade, name)

    def apply_policy(self, candidates, version):
        self.applied.append((tuple(n for n, _p, _k in candidates), version))
        return self._cascade.apply_policy(candidates, version)


def test_scanner_lifespan_starts_task_and_stops(tmp_path, monkeypatch):
    """scanner_factory 返非 None → lifespan 起 scanner run_loop task;shutdown stop 优雅退出。"""
    cascade, store, _health = _cascade_with_health(tmp_path)
    monkeypatch.setattr("llm_router.app._SCANNER_DB", tmp_path / "scanner.db")

    async def body():
        lf = _make_lifespan(
            cascade, [],
            scanner_factory_resolver=lambda: lambda c, s: _RecordingScanner(s),
            scanner_interval_seconds=0.01,
        )
        app = FastAPI(lifespan=lf)
        try:
            async with lf(app):
                assert app.state.scanner_task is not None
                assert not app.state.scanner_task.done()
                assert app.state.scanner_store is not None
            assert app.state.scanner_task.done()  # shutdown 后退出
        finally:
            await store.close()
    _run(body())


def test_scanner_lifespan_no_task_when_factory_returns_none(tmp_path, monkeypatch):
    """scanner_factory 返 None(无 key/禁用)→ 不起 scanner task(无谓空转)。"""
    cascade, store, _health = _cascade_with_health(tmp_path)
    monkeypatch.setattr("llm_router.app._SCANNER_DB", tmp_path / "scanner.db")

    async def body():
        lf = _make_lifespan(
            cascade, [],
            scanner_factory_resolver=lambda: lambda c, s: None,
        )
        app = FastAPI(lifespan=lf)
        try:
            async with lf(app):
                assert app.state.scanner_task is None
        finally:
            await store.close()
    _run(body())


def test_scanner_lifespan_no_task_when_resolver_none(tmp_path, monkeypatch):
    """scanner_factory_resolver=None(默认)→ 不起 scanner task(向后兼容旧 lifespan)。"""
    cascade, store, _health = _cascade_with_health(tmp_path)
    monkeypatch.setattr("llm_router.app._SCANNER_DB", tmp_path / "scanner.db")

    async def body():
        lf = _make_lifespan(cascade, [])  # 不传 scanner_factory_resolver
        app = FastAPI(lifespan=lf)
        try:
            async with lf(app):
                assert app.state.scanner_task is None
                assert app.state.scanner_store is None
        finally:
            await store.close()
    _run(body())


def _full_cascade_for_rebuild(tmp_path):
    """建带 EpsilonGreedy + CostGate + PolicyEnforcer 的 Cascade(供重建回调测试,
    这些组件的 refresh_entries/update_quotas/rebuild 是 _refresh_and_apply 的依赖)。"""
    from llm_router.api.cascade import Cascade
    from llm_router.api.cost_gate import CostGate
    from llm_router.api.epsilon_greedy import EpsilonGreedy
    from llm_router.api.policy_enforcer import PolicyEnforcer
    from llm_router.config import ProviderEntry
    from llm_router.providers.mock import MockProvider
    from llm_router.resilience.circuit_breaker import CircuitBreaker
    from llm_router.store.token_ledger import LedgerStore
    from llm_router.store.trace import TraceStore

    entries = {
        "mock": ProviderEntry(name="mock", tier="fast", quota=1000000, cooldown_s=1,
                              is_free=True, cost_multiplier=0.0),
    }
    ledger = LedgerStore(tmp_path / "ledger.db")
    cost_gate = CostGate(ledger, {"mock": 1000000})
    cascade = Cascade(
        store=TraceStore(tmp_path / "trace.db"),
        breaker=CircuitBreaker(tmp_path / "circuit.db"),
        strategy=EpsilonGreedy(entries, chooser=lambda: 1.0),
        candidates=[("mock", MockProvider(), "mock")],
        policy_enforcer=PolicyEnforcer(entries.values()),
        ledger=ledger,
        cost_gate=cost_gate,
    )
    return cascade


def test_rebuild_callback_calls_apply_policy(tmp_path, monkeypatch):
    """on_tick_complete 重建回调:重读 active → apply_policy 被调(version=content-hash)。"""
    from llm_router.app import _make_rebuild_callback
    from llm_router.config import Policy, ProviderEntry
    from llm_router.scanner.snapshot import DiscoveredModel, ScannerSource
    from llm_router.store.scanner_store import ScannerStore

    cascade = _full_cascade_for_rebuild(tmp_path)
    scanner_db = tmp_path / "scanner.db"
    monkeypatch.setattr("llm_router.app._SCANNER_DB", scanner_db)
    monkeypatch.setattr(
        "llm_router.app.policy",
        lambda: Policy(
            policy_version="t1", gray_percent=100,
            providers=[ProviderEntry(name="mock", tier="fast", quota=1, cooldown_s=1,
                                     is_free=True, cost_multiplier=0.0)],
        ),
    )

    async def body():
        sstore = ScannerStore(scanner_db)
        await sstore.init()
        try:
            await sstore.upsert_entry(
                DiscoveredModel(source=ScannerSource.NVIDIA, model_id="nvidia/a-70b", tier="strong"),
                interview_passed=True,
            )
            spy = _ApplyPolicySpy(cascade)
            rebuild = _make_rebuild_callback(spy, sstore)
            await rebuild(None)
            assert len(spy.applied) == 1
            _names, version = spy.applied[0]
            assert version.startswith("scan-")  # content-hash 版本
            # mock 候选在(动态缺 key 不产候选,但 mock 兜底在)
            assert "mock" in _names
        finally:
            await sstore.close()
    _run(body())


def test_rebuild_callback_idempotent_same_active_set(tmp_path, monkeypatch):
    """同 active 集 → 同 version → 第二次 apply_policy noop(返 False)。"""
    from llm_router.app import _make_rebuild_callback
    from llm_router.config import Policy, ProviderEntry
    from llm_router.scanner.snapshot import DiscoveredModel, ScannerSource
    from llm_router.store.scanner_store import ScannerStore

    cascade, store, _health = _cascade_with_health(tmp_path)
    cascade = _full_cascade_for_rebuild(tmp_path)
    scanner_db = tmp_path / "scanner.db"
    monkeypatch.setattr("llm_router.app._SCANNER_DB", scanner_db)
    monkeypatch.setattr(
        "llm_router.app.policy",
        lambda: Policy(
            policy_version="t1", gray_percent=0,  # gray=0 → 无动态,纯 mock(确定性)
            providers=[ProviderEntry(name="mock", tier="fast", quota=1, cooldown_s=1,
                                     is_free=True, cost_multiplier=0.0)],
        ),
    )

    async def body():
        sstore = ScannerStore(scanner_db)
        await sstore.init()
        try:
            await sstore.upsert_entry(
                DiscoveredModel(source=ScannerSource.NVIDIA, model_id="nvidia/a-70b", tier="strong"),
                interview_passed=True,
            )
            spy = _ApplyPolicySpy(cascade)
            rebuild = _make_rebuild_callback(spy, sstore)
            await rebuild(None)  # 首次:version "" → "scan-<active-hash>" → apply_policy True
            first = spy.applied[-1]
            assert first[1].startswith("scan-")
            await rebuild(None)  # 二次:同 active 集 → 同 version → noop(apply_policy 返 False)
            second = spy.applied[-1]
            assert second == first  # 同 candidates + 同 version(幂等)
        finally:
            await sstore.close()
    _run(body())
