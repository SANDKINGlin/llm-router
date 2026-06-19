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
        # S1.0 修复后(2026-06-19):record_success setdefault → good 首次成功即入 _keys CLOSED
        # 派生 _global_is_open 不再因单一 trip 误判全 OPEN。本行 SOFT 标 good 仍保留以保持
        # 测试场景的明确性(确保 good 在 _keys 是显式可见,与防雪崩主断言无关)。
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

    防"无限重试雪崩"验证:
      - 30 个请求总耗时 < 5s(OpenCode 子片4 MED #3 闭合:加耗时断言而非仅 docstring)
      - 每请求 cascade 返 success=False reason=global_open
      - 每 provider 真调用 == 3(OpenCode 子片4 MED #2 闭合:精确值非魔数 5,
        前 3 次 cascade.run 各让 bad1/bad2 fail 1 次累积到 threshold;之后 derived
        global_open 短路,真 complete 不再调)
    """
    import time as _time

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

    elapsed_start = _time.monotonic()
    results = _run(body())
    elapsed = _time.monotonic() - elapsed_start

    # OpenCode 子片4 MED #3 闭合:30 请求 5s 内完成(若死循环 / asyncio.sleep 注入
    # 雪崩,本断言立败)
    assert elapsed < 5.0, (
        f"30 个 burst 请求耗时 {elapsed:.2f}s 超 5s,疑似死循环 / 同步阻塞雪崩"
    )

    # 全部 30 个请求 success=False(无 provider 兜底)
    for r in results:
        assert r.success is False
    # 后期请求 reason="global_open"(派生 global 触发后)
    last = results[-1]
    assert last.last_reason == "global_open", (
        f"全 burst 后 last_reason 应 global_open,实际 {last.last_reason}"
    )
    # OpenCode 子片4 MED #2 闭合:精确断言各 provider 真调用 == 3(threshold)
    # 前 3 次 cascade.run:bad1/bad2 各被尝试 1 次累积 → r3 内 bad1 hf=3 OPEN,
    # bad2 hf=3 OPEN(record_failure 在 complete 后,r3 内两 provider 都被尝试)。
    # r4 起:bad1 allow → _global_is_open=True(只两 provider 全 OPEN)→ 短路,无
    # complete 调用。
    assert counter.get("bad1") == 3, (
        f"防雪崩:bad1 真调用应 == 3(threshold),实际 {counter.get('bad1')}"
    )
    assert counter.get("bad2") == 3, (
        f"防雪崩:bad2 真调用应 == 3(threshold),实际 {counter.get('bad2')}"
    )


# ── A2 HALF_OPEN 探活恢复路径(子片 3 OpenCode MED #5 defer 收口) ─────────


def _zero_jitter():
    """jitter_fn 注入 0(测试可控,不依赖 secrets.randbelow)。"""
    return 0.0


def _make_test_breaker(tmp_path: Path, *, key_hard_threshold: int = 3) -> CircuitBreaker:
    """构造可控 breaker:_jitter_fn=0,_now_override 由 caller 推进。

    历史 work-around(已不必要,2026-06-19 S1.0 修复):原本注册一个 'neighbor' CLOSED key
    避开 record_success 不 setdefault 的 caveat;S1.0 修复后 good 首次成功即入 _keys,
    派生不再误判,但保留 neighbor 注册以保持测试场景多样性(状态机测试隔离 cleaner)。
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


def test_breaker_half_open_busy_blocks_concurrent_probe(tmp_path):
    """A2.4 OpenCode 子片4 MED #4 闭合:HALF_OPEN 状态下并发探测互斥。

    场景:OPEN → cooldown 后第 1 次 allow → HALF_OPEN + probe_in_flight=True;
    第 2 次 allow(同 key)→ Decision(False, "half_open_busy")。验证 probe_in_flight
    防并发惊群机制有效(若删此分支,多个并发请求同时进 HALF_OPEN,状态机一致性破坏)。

    反例闭合:删 `if ks.probe_in_flight: return Decision(False, "half_open_busy")`,
    第 2 次 allow 也会进 HALF_OPEN 并设 probe_in_flight=True(已是 True),仍返
    `Decision(True, "key_half_open_probe")` → 本测立败抓住。
    """
    breaker = _make_test_breaker(tmp_path)
    for _ in range(3):
        breaker.record_failure("p", "k", TripReason.HARD)
    assert breaker.get_key_state("p", "k").state == CircuitState.OPEN

    # 推进 cooldown,第 1 次 allow → 进 HALF_OPEN,放探测
    breaker._now_override = 31.0
    dec1 = breaker.allow_request("p", "k")
    assert dec1.allowed is True
    assert dec1.reason == "key_half_open_probe"
    assert breaker.get_key_state("p", "k").probe_in_flight is True

    # 第 2 次 allow(同 key,模拟并发探测)→ probe_in_flight=True 被互斥,拒
    dec2 = breaker.allow_request("p", "k")
    assert dec2.allowed is False, (
        "HALF_OPEN + probe_in_flight=True 应互斥拒新探测,防并发惊群"
    )
    assert dec2.reason == "half_open_busy", (
        f"应 reason='half_open_busy';实际 {dec2.reason!r}"
    )


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

    返 {basename: mtime};文件不存在则不入字典。OpenCode 子片4 CRITICAL #1 闭合:
    路径用 `Path(__file__).resolve().parents[2] / "data"`(与 app.py:_DATA_DIR 一致),
    不硬编码用户路径,避免 CI / 跨开发机假绿。
    """
    data_dir = Path(__file__).resolve().parents[2] / "data"
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


def test_lifespan_with_no_probe_targets_zero_pollution_after_callable_fix(
    tmp_path, monkeypatch
):
    """A3.2 OpenCode 子片 2 HIGH #1 + 子片 4 HIGH #1 + S1.0 caveat 2 修复(2026-06-19)
    闭合验证:lifespan 真启 + 无 probe_targets(production 默认)→ **production data 全 4 个
    db(trace/ledger/circuit/health)mtime 严格不变**——零污染。

    **测试翻面**(2026-06-19,S1.0 caveat 2 修复后):
    历史名 "only_pollutes_health_db_via_init" 是当时诚实化(_make_lifespan 闭包硬绑
    production _cascade,monkeypatch _cascade 不影响已构造闭包,health.db 必污)。
    S1.0 修复改 _make_lifespan 接 callable resolver,production 传 ``lambda: _cascade``,
    每次 startup 重新解析 module attr → monkeypatch 后 lifespan 用 test_cascade
    (HealthStore 指 tmp_path/health.db),production health.db 不再被触动。

    现在改名 "zero_pollution_after_callable_fix" + **严格断言 4 个 db 全不变**(包括
    health.db)。任一变化 = caveat 2 修复回退或 starlette 行为漂移,反例立败。

    防假绿:
      - sanity:before/after 至少含 trace.db / ledger.db / circuit.db / health.db
      - 4 个 db mtime **必须**严格相等(任一变化 → 反例触发)
      - patched cascade 真被使用(counter 验)
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
    # OpenCode 子片4 CRITICAL #1 闭合(防 CI 假绿):sanity 至少 4 个 db 已存在,
    # 否则 mtime 比较为空字典 vs 空字典 = 跳过断言(假绿)。
    required_dbs = ("trace.db", "ledger.db", "circuit.db", "health.db")
    missing = [n for n in required_dbs if n not in before]
    assert not missing, (
        f"production data dir 缺 {missing} —— 跑此测试前需先跑过 cascade.run() "
        f"或本测无法验 lifespan 是否污染(空快照对空快照永远相等假绿)。"
        f"修法:在 conftest session-scoped fixture 创建占位 db,或先跑 happy path 测试。"
    )

    with TestClient(app_mod.app) as client:
        r = client.post(
            "/v1/chat/completions",
            json={"model": "any", "messages": [{"role": "user", "content": "x"}]},
        )
        assert r.status_code == 200
    after = _data_dir_mtime_snapshot()

    # S1.0 caveat 2 修复闭合:严格监测 4 个 db 不变(health.db 也算)
    # 任一变化 → caveat 2 修复回退(callable resolver 失效)或 starlette 行为漂移,反例立败
    for db_name in required_dbs:
        assert before[db_name] == after[db_name], (
            f"S1.0 caveat 2 修复:lifespan 真启 + monkeypatch _cascade → "
            f"production {db_name} mtime 应不变,但 before={before[db_name]} "
            f"after={after[db_name]} —— callable resolver 失效或 starlette 漂移。"
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
