"""S2.8b · 探活循环 HealthProber(每 interval 秒 ping 注入 providers,记 health.db)。

侧挂(design ⑨),不在主请求路径。接 S2.8a HealthStore.record_probe。
守 S2.8 spec 职责边界:探活是**新鲜度信号**,latest-wins,**不维护恢复计数**(恢复归 CB)。
本切片只做循环本身;路由 hard-skip 死亡 key + CB HALF_OPEN 喂探活留 S2.8c。

TDD:本套件先 RED——HealthProber 未实现时 import 即失败。
"""
from __future__ import annotations

import asyncio
import sqlite3

import pytest

from llm_router.health.probe import HealthProber
from llm_router.providers.base import Provider, ProviderError
from llm_router.store.health_store import HealthStore


# ── 测试用 fake providers(可控注入延迟/异常,hermetic,不依赖 MockProvider)──


class _OKProvider(Provider):
    name = "ok"

    async def complete(self, prompt: str) -> tuple[str, str]:
        return "pong", "ok-model"


class _SlowProvider(Provider):
    name = "slow"

    async def complete(self, prompt: str) -> tuple[str, str]:
        await asyncio.sleep(10)  # 远超 probe_timeout
        return "x", "y"


class _ProviderErrorProvider(Provider):
    name = "boom"

    async def complete(self, prompt: str) -> tuple[str, str]:
        raise ProviderError("simulated 5xx")


class _BugProvider(Provider):
    """非 ProviderError 异常(模拟编程 bug)。探活循环须健壮:记死,不崩循环。"""

    name = "bug"

    async def complete(self, prompt: str) -> tuple[str, str]:
        raise RuntimeError("unexpected")


def _run(coro):
    return asyncio.run(coro)


def test_probe_one_success_records_alive(tmp_path):
    """成功 provider → record_probe(alive=True, latency_ms>=0)。"""

    async def body():
        store = HealthStore(tmp_path / "h.db")
        await store.init()
        try:
            prober = HealthProber(store, [("ok", _OKProvider())], probe_timeout_seconds=2.0)
            row = await prober.probe_one("ok", _OKProvider())
            assert row.alive is True
            assert row.latency_ms is not None
            assert row.latency_ms >= 0.0
            got = await store.get("ok")  # 落盘可查
            assert got is not None and got.alive is True
        finally:
            await store.close()

    _run(body())


def test_probe_one_timeout_records_dead(tmp_path):
    """超 probe_timeout → alive=False, latency_ms=None。"""

    async def body():
        store = HealthStore(tmp_path / "h.db")
        await store.init()
        try:
            prober = HealthProber(store, [], probe_timeout_seconds=0.05)
            row = await prober.probe_one("slow", _SlowProvider())
            assert row.alive is False
            assert row.latency_ms is None
        finally:
            await store.close()

    _run(body())


def test_probe_one_provider_error_records_dead(tmp_path):
    """ProviderError(5xx/限流/连接)→ alive=False, latency_ms=None。"""

    async def body():
        store = HealthStore(tmp_path / "h.db")
        await store.init()
        try:
            prober = HealthProber(store, [], probe_timeout_seconds=2.0)
            row = await prober.probe_one("boom", _ProviderErrorProvider())
            assert row.alive is False
            assert row.latency_ms is None
        finally:
            await store.close()

    _run(body())


def test_probe_one_generic_exception_caught_not_raised(tmp_path):
    """非 ProviderError 异常:探活循环须健壮(一个 provider 的 bug 不崩后台循环)→ 记死,不上抛。

    与 Cascade 路径(其余异常上抛不吞,防掩盖 bug)不同——探活是后台信号源,
    单点异常不该中断整个循环;记 alive=False 让该 provider 本轮被视为不可用。
    """

    async def body():
        store = HealthStore(tmp_path / "h.db")
        await store.init()
        try:
            prober = HealthProber(store, [], probe_timeout_seconds=2.0)
            row = await prober.probe_one("bug", _BugProvider())  # 不抛
            assert row.alive is False
            assert row.latency_ms is None
        finally:
            await store.close()

    _run(body())


def test_tick_probes_all_providers_concurrently(tmp_path):
    """tick 并发 ping 全部注入 provider,各自 record_probe(一个失败不影响其他)。"""

    async def body():
        store = HealthStore(tmp_path / "h.db")
        await store.init()
        try:
            providers = [
                ("ok", _OKProvider()),
                ("boom", _ProviderErrorProvider()),
                ("ok2", _OKProvider()),
            ]
            prober = HealthProber(store, providers, probe_timeout_seconds=2.0)
            await prober.tick()
            rows = {r.provider: r for r in await store.latest_probe()}
            assert rows["ok"].alive is True
            assert rows["ok2"].alive is True
            assert rows["boom"].alive is False  # 失败的不影响其他
        finally:
            await store.close()

    _run(body())


def test_run_loop_runs_until_stop_event(tmp_path):
    """run_loop 每 interval 秒 tick,stop_event.set() 后干净退出。"""

    async def body():
        store = HealthStore(tmp_path / "h.db")
        await store.init()
        try:
            calls = []

            class _Count(Provider):
                name = "c"

                async def complete(self, prompt: str) -> tuple[str, str]:
                    calls.append(1)
                    return "x", "m"

            prober = HealthProber(
                store, [("c", _Count())], interval_seconds=0.01, probe_timeout_seconds=1.0
            )
            stop = asyncio.Event()
            task = asyncio.create_task(prober.run_loop(stop))
            await asyncio.sleep(0.05)  # 跑多轮
            stop.set()
            await asyncio.wait_for(task, timeout=2.0)  # 应在 stop 后退出
            assert len(calls) >= 2, f"应跑多轮,实际 {len(calls)}"
        finally:
            await store.close()

    _run(body())


def test_run_loop_empty_providers_noop(tmp_path):
    """空 provider 列表:tick 不崩,循环正常跑停。"""

    async def body():
        store = HealthStore(tmp_path / "h.db")
        await store.init()
        try:
            prober = HealthProber(store, [], interval_seconds=0.01)
            stop = asyncio.Event()
            task = asyncio.create_task(prober.run_loop(stop))
            await asyncio.sleep(0.03)
            stop.set()
            await asyncio.wait_for(task, timeout=2.0)  # 不崩即过
        finally:
            await store.close()

    _run(body())


def test_run_loop_no_stop_event_runs_at_least_one_tick(tmp_path):
    """run_loop() 无 stop_event(生产路径):至少完成一轮 tick 落盘(#4 对抗审盲区)。

    首轮 tick 在 sleep(300) 前,故 cancel 前已 record_probe;验证生产无限循环不空转。
    """

    async def body():
        store = HealthStore(tmp_path / "h.db")
        await store.init()
        try:
            prober = HealthProber(
                store, [("ok", _OKProvider())], interval_seconds=300.0, probe_timeout_seconds=2.0
            )
            task = asyncio.create_task(prober.run_loop())  # 无 stop_event → 无限循环
            await asyncio.sleep(0.05)  # 让首轮 tick 跑完(之后卡 sleep 300)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            got = await store.get("ok")  # 首轮应已落盘
            assert got is not None and got.alive is True
        finally:
            await store.close()

    _run(body())


def test_probe_one_propagates_store_error():
    """record_probe(store 写)失败应上抛,不被 complete 的 try 吞(#2/#5 对抗审)。

    注入会抛的 fake store(duck-typed),验证 probe_one 不吞 store 故障。
    """

    async def body():
        class _BoomStore:
            async def record_probe(self, name, *, latency_ms, alive):
                raise sqlite3.Error("db write failed")

        prober = HealthProber(_BoomStore(), [], probe_timeout_seconds=2.0)
        with pytest.raises(sqlite3.Error):
            await prober.probe_one("ok", _OKProvider())

    _run(body())
