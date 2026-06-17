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
        return "pong", "ok-model"


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
