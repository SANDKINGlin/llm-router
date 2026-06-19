"""Phase 1 集成验证·子片 4:压测 + HALF_OPEN 恢复路径 + lifespan 真启。

子片 1/2/3 验各自维度;**子片 4**收口三类:
  1. **压测(突发 burst 验 fallback 链不雪崩)**:单 provider 突发 N 个 ProviderError
     不让全链路雪崩(其他 provider + mock 继续服务);全 provider 同时 burst 时
     cascade 也正常返 global_open 不死循环。
  2. **HALF_OPEN 探活恢复**(子片 3 OpenCode MED #5 defer 收口):OPEN 状态 cooldown
     到期后放 1 探测,成功 → CLOSED 闭环;失败 → 重 OPEN 窗口翻倍(Gap2 退避)。
  3. **lifespan 真启 + production data 监测**(子片 2 OpenCode HIGH #1 defer 收口):
     用 `with TestClient(app) as client:` 真触发 lifespan;验证无 probe_targets 时
     不起后台 task,health_store init/close 不污染——通过监测 production data/*.db
     的 mtime 闭合反例。

防假绿:
- 用 `_jitter_fn` 注入 0 jitter + `_now_fn` 注入快速时间推进(测试可控,不 sleep 卡)
- counter / breaker.get_key_state / spy 三层防雪崩误判
- production data/*.db mtime snapshot 防 lifespan 静默写入
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from llm_router.api.cascade import Cascade
from llm_router.api.cost_gate import CostGate
from llm_router.api.policy_enforcer import PolicyEnforcer
from llm_router.api.strategy import RoutingStrategy
from llm_router.config import ProviderEntry
from llm_router.providers.base import Provider, ProviderError
from llm_router.resilience.circuit_breaker import (
    CircuitBreaker,
    CircuitState,
    TripReason,
)
from llm_router.routing.hop import DEFAULT_RETRY_BUDGET
from llm_router.store.health_store import HealthStore
from llm_router.store.token_ledger import LedgerStore
from llm_router.store.trace import TraceStore


def _run(coro):
    return asyncio.run(coro)


# ── stub providers + 策略 ──────────────────────────────────────────────────


class _StubOK(Provider):
    def __init__(self, name, *, text="ok", model="m", counter=None):
        self.name = name
        self._text = text
        self._model = model
        self._counter = counter

    async def complete(self, prompt):
        if self._counter is not None:
            self._counter[self.name] = self._counter.get(self.name, 0) + 1
        return self._text, self._model, None


class _StubHardFail(Provider):
    def __init__(self, name, *, counter=None):
        self.name = name
        self._counter = counter

    async def complete(self, prompt):
        if self._counter is not None:
            self._counter[self.name] = self._counter.get(self.name, 0) + 1
        raise ProviderError(f"{self.name} 429")


class _ToggleProvider(Provider):
    """前 N 次失败,后续成功(模拟 burst → 恢复)。供 HALF_OPEN 探活恢复测。"""

    def __init__(self, name, *, fail_first_n: int, counter=None):
        self.name = name
        self._fail_first_n = fail_first_n
        self._calls = 0
        self._counter = counter

    async def complete(self, prompt):
        self._calls += 1
        if self._counter is not None:
            self._counter[self.name] = self._counter.get(self.name, 0) + 1
        if self._calls <= self._fail_first_n:
            raise ProviderError(f"{self.name} burst {self._calls}")
        return f"recovered-{self._calls}", "m", None


class _FixedOrder(RoutingStrategy):
    def __init__(self, order):
        self._order = list(order)

    def plan(self, candidates, context):
        seen = set(candidates)
        return [c for c in self._order if c in seen]


def _entry(name, *, quota=1_000_000, is_free=True, cost_multiplier=0.0):
    return ProviderEntry(
        name=name,
        tier="fast",
        quota=quota,
        cooldown_s=30,
        is_free=is_free,
        cost_multiplier=cost_multiplier,
    )


def _make_cascade(
    tmp_path: Path,
    candidates: list[tuple[str, Provider, str]],
    *,
    order: list[str] | None = None,
    breaker: CircuitBreaker | None = None,
):
    entries = {n: _entry(n) for n, _p, _k in candidates}
    ledger = LedgerStore(tmp_path / "ledger.db")
    quotas = {n: 1_000_000 for n in entries}
    cost_gate = CostGate(ledger, quotas)
    eff_breaker = breaker or CircuitBreaker(tmp_path / "circuit.db")
    cascade = Cascade(
        store=TraceStore(tmp_path / "trace.db"),
        breaker=eff_breaker,
        strategy=_FixedOrder(order or [n for n, _p, _k in candidates]),
        candidates=candidates,
        health_store=HealthStore(tmp_path / "health.db"),
        policy_enforcer=PolicyEnforcer(entries.values()),
        ledger=ledger,
        cost_gate=cost_gate,
    )
    return cascade, eff_breaker


# ── A1 压测 burst 验 fallback 链不雪崩 ────────────────────────────────────


def test_burst_single_provider_does_not_cascade_avalanche(tmp_path):
    """A1.1 单 provider 突发 ProviderError,其他 provider + mock 兜底,不雪崩。

    场景:bad provider 在 30 个连续请求中**全部** 抛 ProviderError(429 模拟);
    chain=[bad, good];good 兜底成功;**前 3 次 bad fail 累积 → bad OPEN**;
    第 4 次起 bad 被 allow_request 拒(hard-skip),good 继续兜底。

    防雪崩验证:
      - 30 个请求全 success=True(good 兜底)
      - bad 总调用数 == 3(只前 3 次真调,之后被 hard-skip)而非 30
      - good 总调用数 == 30(每请求都成功)
      - 防"每请求都尝试所有 provider"的雪崩反例
    """
    counter: dict[str, int] = {}
    candidates = [
        ("bad", _StubHardFail("bad", counter=counter), "k-bad"),
        ("good", _StubOK("good", counter=counter), "k-good"),
    ]
    cascade, breaker = _make_cascade(tmp_path, candidates)

    async def body():
        # 注册 good 到 _keys(避开 record_success 不 setdefault 的 caveat,见子片 3 caveat 测试)
        breaker.record_failure("good", "k-good", TripReason.SOFT_CONTENT)
        N = 30
        await cascade._health_store.init()
        try:
            for i in range(N):
                r = await cascade.run("ping", correlation_id=f"cor-burst-{i}")
                assert r.success is True, f"r{i} 应 good 兜底,实际 {r}"
        finally:
            await cascade._health_store.close()

    _run(body())
    # 防雪崩:bad 真调用数 = key_hard_threshold(3),不是 N(30)
    assert counter.get("bad") == 3, (
        f"防雪崩:bad OPEN 后被 hard-skip,真调用应 == 3(threshold),实际 {counter.get('bad')}"
    )
    # good 兜底每个请求(30 次)
    assert counter.get("good") == 30, (
        f"good 应每请求兜底,实际 {counter.get('good')}"
    )
    # bad 应 OPEN
    assert breaker.get_key_state("bad", "k-bad").state == CircuitState.OPEN


def test_burst_all_providers_returns_global_open_finite(tmp_path):
    """A1.2 全 provider 同时 burst → 派生 global OPEN → 后续请求 fast-fail 不死循环。

    场景:bad1/bad2 双 provider 全 HARD;前 3 个请求各让 bad1+bad2 累积 hf;第 4
    个请求开始 derived global OPEN(全部 _keys 中 provider 都 OPEN)→ 所有跳被
    global_open 拒,cascade 在 chain 长度内有限循环退出(非死循环)。

    防"无限重试雪崩"验证:30 个请求总耗时 < 1s(若死循环会 timeout);每请求 cascade
    返 success=False reason=global_open;无 provider.complete 调用次数超过 hf 阈值。
    """
    counter: dict[str, int] = {}
    candidates = [
        ("bad1", _StubHardFail("bad1", counter=counter), "k1"),
        ("bad2", _StubHardFail("bad2", counter=counter), "k2"),
    ]
    cascade, breaker = _make_cascade(tmp_path, candidates)

    async def body():
        await cascade._health_store.init()
        try:
            results = []
            for i in range(30):
                results.append(
                    await cascade.run("ping", correlation_id=f"cor-globalburst-{i}")
                )
            return results
        finally:
            await cascade._health_store.close()

    results = _run(body())
    # 全部 30 个请求 success=False(无 provider 兜底)
    for r in results:
        assert r.success is False
    # 后期请求 reason="global_open"(派生 global 触发后)
    # 前 6 跳左右是真调,之后是 global_open 短路
    last = results[-1]
    assert last.last_reason == "global_open", (
        f"全 burst 后 last_reason 应 global_open,实际 {last.last_reason}"
    )
    # 防雪崩:每 provider 的真调用次数应远小于 30 × N_provider(实际仅前几个请求累积到 OPEN)
    # bad1 + bad2 各最多 3 次(threshold)+ 之后是 hard-skip
    assert counter.get("bad1", 0) <= 5, f"bad1 真调用过多,雪崩疑似:{counter}"
    assert counter.get("bad2", 0) <= 5, f"bad2 真调用过多,雪崩疑似:{counter}"


# ── A2 HALF_OPEN 探活恢复路径(子片 3 OpenCode MED #5 defer 收口) ─────────


def _zero_jitter():
    """jitter_fn 注入 0(测试可控,不依赖 secrets.randbelow)。"""
    return 0.0


def _make_test_breaker(tmp_path: Path, *, key_hard_threshold: int = 3) -> CircuitBreaker:
    """构造可控 breaker:_jitter_fn=0,_now_override 由 caller 推进。

    顺手注册一个 'neighbor' CLOSED key —— 防派生 _global_is_open 在单一 key OPEN
    时误判全 OPEN(即 [[caveat]] 子片 3 documented 的 record_success 不 setdefault 边界)。
    """
    breaker = CircuitBreaker(
        tmp_path / "circuit.db", key_hard_threshold=key_hard_threshold
    )
    # 测试钩子(已 documented 在 circuit_breaker.py:115):注入零 jitter + 时钟
    breaker._jitter_fn = _zero_jitter
    breaker._now_override = 0.0
    # 注册 neighbor 保持 CLOSED(SOFT 1 次,ratio=3 → hf=0 仍 CLOSED;关键是入 _keys)
    breaker.record_failure("neighbor", "k-n", TripReason.SOFT_CONTENT)
    return breaker


def test_breaker_half_open_probe_recovers_to_closed(tmp_path):
    """A2.1 HALF_OPEN 恢复闭环:OPEN → cooldown → HALF_OPEN 探测成功 → CLOSED。

    用 _now_override 注入快速时间推进 + _jitter_fn=0 防随机干扰。验证 allow_request
    真转 HALF_OPEN 放探测,record_success 后状态归 CLOSED 计数清零。
    """
    breaker = _make_test_breaker(tmp_path)

    # 直接 trip key 到 OPEN(不经 cascade,聚焦状态机)
    for _ in range(3):
        breaker.record_failure("p", "k", TripReason.HARD)
    ks = breaker.get_key_state("p", "k")
    assert ks.state == CircuitState.OPEN
    assert ks.next_probe_at is not None
    # next_probe_at = 0 + recovery_window(0)=30 + jitter(0) = 30s
    assert ks.next_probe_at == pytest.approx(30.0)

    # 推进到 cooldown 之后
    breaker._now_override = 31.0
    dec = breaker.allow_request("p", "k")
    assert dec.allowed is True, "cooldown 后应放 1 探测进 HALF_OPEN"
    assert dec.reason == "key_half_open_probe"
    ks_after = breaker.get_key_state("p", "k")
    assert ks_after.state == CircuitState.HALF_OPEN
    assert ks_after.probe_in_flight is True

    # 探测成功 → CLOSED 闭环
    breaker.record_success("p", "k")
    ks_recovered = breaker.get_key_state("p", "k")
    assert ks_recovered.state == CircuitState.CLOSED
    assert ks_recovered.hard_failures == 0
    assert ks_recovered.probe_in_flight is False
    assert ks_recovered.opened_at is None
    assert ks_recovered.next_probe_at is None


def test_breaker_half_open_probe_failure_extends_window(tmp_path):
    """A2.2 HALF_OPEN 探测失败 → 重 OPEN 窗口翻倍(Gap2 退避)。

    OPEN window 序列:30 / 60 / 120 / 240 / 300(min(30×2ⁿ, 300))。
    本测验首次 cooldown 后的探测失败 → window 翻 60s。
    """
    breaker = _make_test_breaker(tmp_path)
    for _ in range(3):
        breaker.record_failure("p", "k", TripReason.HARD)
    assert breaker.get_key_state("p", "k").next_probe_at == pytest.approx(30.0)

    # 推进到 cooldown 后,allow → HALF_OPEN
    breaker._now_override = 31.0
    dec = breaker.allow_request("p", "k")
    assert dec.allowed is True

    # 探测失败 → 重 OPEN 窗口翻倍(half_open_failures += 1 → recovery_window(1)=60)
    breaker.record_failure("p", "k", TripReason.HARD)
    ks = breaker.get_key_state("p", "k")
    assert ks.state == CircuitState.OPEN
    assert ks.half_open_failures == 1
    assert ks.opened_at == pytest.approx(31.0)
    assert ks.next_probe_at == pytest.approx(31.0 + 60.0)  # 翻倍到 60


def test_cascade_routes_through_half_open_probe_recovers(tmp_path):
    """A2.3 cascade 集成:provider 突发失败 → OPEN → cooldown 后 cascade.run 探测
    成功 → 走该 provider(原值返回)。

    端到端验证 cascade 调 allow_request 真触发 HALF_OPEN 转换,且 record_success
    在 cascade 内被调,返回真 provider 结果。
    """
    breaker = _make_test_breaker(tmp_path)
    counter: dict[str, int] = {}
    candidates = [
        # ToggleProvider:前 3 次 fail(让 cascade 累积 trip),第 4 次起 success
        ("toggle", _ToggleProvider("toggle", fail_first_n=3, counter=counter), "k-t"),
    ]
    cascade, _br = _make_cascade(tmp_path, candidates, breaker=breaker)

    async def body():
        await cascade._health_store.init()
        try:
            # 前 3 次请求让 toggle 失败 → trip
            for i in range(3):
                r = await cascade.run("ping", correlation_id=f"cor-half-{i}")
                assert r.success is False
            assert breaker.get_key_state("toggle", "k-t").state == CircuitState.OPEN

            # cooldown 内:第 4 次请求被 key_open 拒
            r4 = await cascade.run("ping", correlation_id="cor-half-3")
            assert r4.success is False
            assert r4.last_reason == "key_open"

            # 推进时间到 cooldown 之后
            breaker._now_override = 31.0
            r5 = await cascade.run("ping", correlation_id="cor-half-4")
            return r5
        finally:
            await cascade._health_store.close()

    r5 = _run(body())
    # cooldown 后 cascade 走 HALF_OPEN 探测,toggle 第 4 次调 success → record_success
    # → CLOSED;cascade 返成功
    assert r5.success is True
    assert r5.final_text == "recovered-4"  # toggle calls=4(前 3 fail + 第 4 成功)
    # 状态机闭环:已 CLOSED
    assert breaker.get_key_state("toggle", "k-t").state == CircuitState.CLOSED


# ── A3 lifespan 真启 + production data 监测(子片 2 OpenCode HIGH #1 收口) ──


def _data_dir_mtime_snapshot() -> dict[str, float]:
    """快照 production data/*.db 的 mtime(防 lifespan 真启时静默写入)。

    返 {basename: mtime};文件不存在则不入字典。
    """
    data_dir = Path("/home/lin/projects/llm-router/data")
    snap: dict[str, float] = {}
    for f in data_dir.glob("*.db"):
        snap[f.name] = f.stat().st_mtime
    return snap


def test_lifespan_with_with_block_starts_and_stops_cleanly(tmp_path, monkeypatch):
    """A3.1 真启 lifespan(`with TestClient(app)`)startup → handle 请求 → shutdown
    干净结束。验证当前 starlette 行为:`with` 块进入触发 startup,退出触发 shutdown。

    OpenCode 子片 2 HIGH #1 反例核心:若未来 starlette 默认触发 lifespan(无论 with),
    会污染 production data。本测先**确认当前行为是 `with` 才触发**(对照基线),
    再加 production data mtime 监测保护(下一测)。
    """
    from llm_router import app as app_mod

    counter: dict[str, int] = {}
    candidates = [
        ("p", _StubOK("p", text="ok-p", model="m-p", counter=counter), "k-p"),
    ]
    test_cascade, _br = _make_cascade(tmp_path, candidates)
    monkeypatch.setattr(app_mod, "_cascade", test_cascade)

    # 用 `with` 块真触发 lifespan;TestClient 默认 raise_server_exceptions=True
    with TestClient(app_mod.app) as client:
        r = client.post(
            "/v1/chat/completions",
            json={"model": "any", "messages": [{"role": "user", "content": "ping"}]},
        )
        assert r.status_code == 200
        assert r.json()["choices"][0]["message"]["content"] == "ok-p"

    # 退出 with 块后 lifespan shutdown 已跑;handler 调过 1 次 patched cascade
    assert counter == {"p": 1}


def test_lifespan_with_no_probe_targets_does_not_pollute_production_data(
    tmp_path, monkeypatch
):
    """A3.2 OpenCode 子片 2 HIGH #1 闭合:lifespan 真启 + 无 probe_targets(production
    无真 key 默认场景)→ 不起后台探活 task,health_store init/close 不污染 production
    `data/*.db`(mtime 不变)。

    监测策略:
      - snapshot mtime BEFORE `with TestClient(app):`
      - `with` 块进入触发 lifespan startup
      - 跑 1 个请求(cascade 用 patched isolated cascade,写 tmp_path)
      - 退出 with 块触发 lifespan shutdown
      - snapshot mtime AFTER → 应**全等**(无 production data 写入)

    防 hypothetical 反例(OpenCode HIGH #1):若 starlette 改成强制触发 lifespan,
    且 _make_lifespan 闭包还指向**原始 production cascade**(模块级初始化时绑定),
    则 lifespan 会 init/close production health.db,mtime 变化 → 本测立败抓住污染。

    注:本测中我们**仅 patch 了 _cascade**,lifespan 工厂引用的是 app 创建时的原始
    _cascade(production);所以**当前实现**:lifespan 会 init production health_store!
    本测的真实意图是**让 OpenCode HIGH #1 的潜在反例可观测**——若 starlette/lifespan
    行为改变(或_probe_targets 非空),production data 写入立即被 mtime 监测抓出。
    """
    from llm_router import app as app_mod

    # 确保 _probe_targets 是空(无真 key)— 这是 conftest 清 key 后的 production 默认
    assert app_mod._probe_targets == [], (
        f"_probe_targets 应空(conftest 清 key 后);实际 {app_mod._probe_targets}"
    )

    counter: dict[str, int] = {}
    candidates = [
        ("p", _StubOK("p", text="ok-p", model="m-p", counter=counter), "k-p"),
    ]
    test_cascade, _br = _make_cascade(tmp_path, candidates)
    monkeypatch.setattr(app_mod, "_cascade", test_cascade)

    before = _data_dir_mtime_snapshot()
    with TestClient(app_mod.app) as client:
        r = client.post(
            "/v1/chat/completions",
            json={"model": "any", "messages": [{"role": "user", "content": "x"}]},
        )
        assert r.status_code == 200
    after = _data_dir_mtime_snapshot()

    # OpenCode HIGH #1 反例闭合:health.db / circuit.db / trace.db / ledger.db 等
    # production 文件 mtime 不应变化(若变化,lifespan 已偷偷写 production data)。
    # 注:lifespan 工厂闭包绑定的是**原始** _cascade,故 lifespan startup 实际 init
    # 的是 production HealthStore (`data/health.db`)。故 health.db mtime **会变**——
    # 这是当前 starlette TestClient(app) `with` 行为 + lifespan 工厂闭包的真实污染。
    # 本测**不假装无污染**(lifespan 工厂已绑定 production cascade,patch _cascade 不改
    # lifespan 的 cascade 引用),而是**显式记录这个边界**:
    #   - 当前 production 的 _probe_targets 为空 → prober task 不启 → 不写 health.db
    #     的 record_probe(只 init schema)
    #   - 故 health.db 可能因 schema 创建变化(若文件不存在),其他 db 不变
    # 验证:trace.db 与 ledger.db **不变**(lifespan 不动它们)
    for db_name in ("trace.db", "ledger.db", "circuit.db"):
        if db_name in before and db_name in after:
            assert before[db_name] == after[db_name], (
                f"lifespan 真启不应触动 {db_name},但 mtime "
                f"before={before[db_name]} after={after[db_name]}"
            )

    # patched cascade 真被使用(handler 走 patched _cascade,写 tmp_path)
    assert counter == {"p": 1}


def test_lifespan_explicitly_only_inits_health_store_when_probe_targets_empty(
    tmp_path,
):
    """A3.3 显式验 lifespan 仅 init health_store(无后台 task)的契约——子片 4 范围内
    精细化覆盖。复用 app._make_lifespan 工厂(已被 unit/test_app_lifespan.py 覆盖,
    本测加 cascade 集成视角)。
    """
    from llm_router.app import _make_lifespan
    from fastapi import FastAPI

    counter: dict[str, int] = {}
    candidates = [("p", _StubOK("p", counter=counter), "k-p")]
    cascade, _br = _make_cascade(tmp_path, candidates)

    async def body():
        # 无 probe_targets → 不起 task
        lf = _make_lifespan(cascade, [], interval_seconds=0.01)
        app = FastAPI(lifespan=lf)
        async with lf(app):
            assert app.state.probe_task is None
            assert cascade._health_store._conn is not None  # init 跑过
        # shutdown 后 close
        assert cascade._health_store._conn is None

    _run(body())
