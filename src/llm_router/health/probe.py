"""S2.8b · 探活循环 HealthProber(每 interval 秒 ping 注入 providers,记 health.db)。

侧挂(design ⑨),不在主请求路径。接 S2.8a HealthStore.record_probe。

职责边界(health-probe/spec.md 钉死):探活是**新鲜度信号**——latest-wins,不维护恢复计数
(恢复归三层熔断 CB);探活结果**不能绕过** CB 退避窗口;死亡 key 路由 hard-skip 留 S2.8c。
本切片只做循环本身。

设计:
- scheduler = asyncio while+sleep 循环(stdlib,零依赖,不引 APScheduler)。
- ping = asyncio.wait_for(provider.complete(prompt), timeout) + perf_counter 测延迟;
  成功 → alive=True+latency;超时/异常 → alive=False+latency=None。
- probe_one 区分异常:expected(TimeoutError/ProviderError)静默记死;unexpected(可能编程
  bug)记死 + log warning(后台循环健壮不崩,但可观测——#1 对抗审)。
- tick 不吞 record_probe 异常:store 基础设施失败应暴露(#2 对抗审);probe_one 已 catch
  complete 调用异常,只有 record_probe 可能抛。
- providers 由调用方注入(app 决定 ping 谁,可排除 Mock);Prober 不判断,只 ping 给它的。
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Callable, Optional

from llm_router.providers.base import Provider, ProviderError
from llm_router.store.health_store import HealthRow, HealthStore

_LOG = logging.getLogger(__name__)

_DEFAULT_INTERVAL = 300.0  # spec:每 5min
_DEFAULT_PROBE_TIMEOUT = 10.0
_DEFAULT_PROBE_PROMPT = "1"  # 最小:只测能否完成,内容质量归 is_complete


class HealthProber:
    """每 interval 秒并发 ping 注入 providers,结果 UPSERT 进 health.db。

    侧挂后台循环,不在请求路径。调用方(app lifespan)起 run_loop task,shutdown 取消。

    S2.8c:on_alive 回调——probe 成功时通知调用方(name),供其喂 CB HALF_OPEN 加速恢复
    (spec Req 3b)。Prober 仍**不判断 CB**(守"只 ping 给它的"),只报成功;失败不报(无信号)。
    回调抛错被吞(best-effort 喂养,不崩后台循环;record_probe 已先落盘)。
    """

    def __init__(
        self,
        store: HealthStore,
        providers: list[tuple[str, Provider]],
        *,
        interval_seconds: float = _DEFAULT_INTERVAL,
        probe_timeout_seconds: float = _DEFAULT_PROBE_TIMEOUT,
        probe_prompt: str = _DEFAULT_PROBE_PROMPT,
        on_alive: Optional[Callable[[str], None]] = None,
    ) -> None:
        self._store = store
        self._providers = list(providers)
        self._interval = interval_seconds
        self._probe_timeout = probe_timeout_seconds
        self._prompt = probe_prompt
        self._on_alive = on_alive

    async def probe_one(self, name: str, provider: Provider) -> HealthRow:
        """ping 单个 provider,记 record_probe。complete 异常 → alive=False,不上抛。

        返回该 provider 当前行(成功 alive=True+latency_ms;超时/异常 alive=False+latency_ms=None)。
        record_probe(store 写)不在 try 内——store 基础设施失败应上抛,由 tick 暴露(#2)。
        成功后(S2.8c)调 on_alive(name) 喂 CB HALF_OPEN;回调抛错被吞(best-effort,record_probe 已先落盘)。
        """
        start = time.perf_counter()
        alive = False
        latency_ms: Optional[float] = None
        try:
            await asyncio.wait_for(
                provider.complete(self._prompt), timeout=self._probe_timeout
            )
            alive = True
            latency_ms = (time.perf_counter() - start) * 1000.0
        except (asyncio.TimeoutError, ProviderError):
            # expected:超时 / provider 硬失败(5xx/限流/连接)→ 静默记死(常态新鲜度信号)
            alive = False
            latency_ms = None
        except Exception:
            # unexpected:可能是编程 bug → 记死(后台循环健壮不崩)+ log 让运维可见(#1 对抗审)
            _LOG.warning("probe_one(%s) unexpected error; recorded dead", name, exc_info=True)
            alive = False
            latency_ms = None
        row = await self._store.record_probe(name, latency_ms=latency_ms, alive=alive)
        if alive and self._on_alive is not None:
            try:
                self._on_alive(name)
            except Exception:
                _LOG.warning(
                    "on_alive(%s) callback error; ignored (best-effort CB feed)", name, exc_info=True
                )
        return row

    async def tick(self) -> None:
        """并发 ping 全部注入 providers。空列表 no-op。

        不吞 record_probe 异常(#2 对抗审):store 基础设施失败(库挂)应上抛暴露——run_loop
        是侧挂后台 task,其异常不波及请求路径;store 挂了探活本就无意义。
        """
        await asyncio.gather(*(self.probe_one(n, p) for n, p in self._providers))

    async def run_loop(self, stop_event: Optional[asyncio.Event] = None) -> None:
        """循环:tick → 等 interval(可被 stop_event 中断)→ 重复,直到 stop。

        stop_event=None → 无限循环(生产);注入 Event → 测试可控停止。
        生产优雅退出用 stop_event.set();勿 task.cancel()——取消注入可能在 record_probe
        的 DB 写中途,虽 store 原子性由 S2.8a 保证,仍避免中途取消(#3 对抗审)。
        """
        while stop_event is None or not stop_event.is_set():
            await self.tick()
            if stop_event is None:
                await asyncio.sleep(self._interval)
            else:
                # 可被 stop_event 提前唤醒,避免测试空等整个 interval
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=self._interval)
                except asyncio.TimeoutError:
                    pass
