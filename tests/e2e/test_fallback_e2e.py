"""S1.5a · Fallback E2E 10 条(hop 语义 + trace 链 + breaker 决策 + budget 硬停止)。

在 trace store + circuit breaker 组件层闭环(**不接 app.py**,那是 S2.1)。
每条都驱动真实生产组件(breaker/store/content_integrity/hop),不假绿。

测试模式沿用 tests/unit/test_trace.py + tests/unit/resilience/test_cascade_key_to_provider.py:
  - 同步 def test_xxx(tmp_path, monkeypatch) + _run(body())=asyncio.run 包异步
  - breaker 钩子:monkeypatch _jitter_fn=0.0 + breaker._now_override=1000.0
  - tmp_path 建临时 DB

Gap3 派生模型注意(来自 test_cascade):trip 单 key provider(其唯一 key OPEN → 1/1 →
provider OPEN)时,若该 provider 是 breaker 唯一已知 provider → global 冻结(全部 OPEN)。
故"trip pA 但要 fallback 到 pB"的场景,必须先 seed 一个保持 CLOSED 的兄弟 provider pB,
否则 global_open 会把 pB 也拒掉。
"""
from __future__ import annotations

import asyncio

from llm_router.resilience.circuit_breaker import CircuitBreaker, CircuitState, TripReason
from llm_router.routing.hop import parse_attribution
from llm_router.store.trace import TraceStore

from ._fallback_orchestrator import OrchestrationResult, ProviderSpec, run_fallback


def _run(coro):
    """同步测试函数包 asyncio(免 pytest-asyncio,不动 hash 锁)。"""
    return asyncio.run(coro)


def _trip_key(breaker, provider, key):
    """单 key 连续 3 硬失败 → 该 key OPEN。"""
    for _ in range(3):
        breaker.record_failure(provider=provider, key=key, reason=TripReason.HARD)


def _seed_closed_key(breaker, provider, key):
    """建一个保持 CLOSED 的 provider(1 次硬失败,未达阈值)——防 global 冻结。"""
    breaker.record_failure(provider=provider, key=key, reason=TripReason.HARD)


def _new_breaker(tmp_path):
    return CircuitBreaker(db_path=tmp_path / "circuit.db", key_hard_threshold=3)


# ── L1:hop 语义 + trace 回填 ──────────────────────────────────────────────


def test_hop_attribution_written_and_read_back(tmp_path):
    """commit 传 hop_attribution → get_chain 读回 == 写入,parse 还原正确。

    验 commit() 新参数真生效(真实 SQLite WAL 写读,非 mock)。
    """

    async def body():
        store = TraceStore(tmp_path / "trace.db")
        await store.init()
        try:
            out = await store.acquire(
                correlation_id="CID", idempotency_key="k0", provider="pA"
            )
            raw = '{"depth":2,"reason":"hard_failure","from":"pA","to":"pB"}'
            await store.commit(trace_id=out.trace_id, result="r", hop_attribution=raw)
            chain = await store.get_chain("CID")
            assert len(chain) == 1
            assert chain[0].hop_attribution == raw, "写入的 JSON 串必须原样读回"
            assert chain[0].result == "r"
            attr = parse_attribution(chain[0].hop_attribution)
            assert attr.depth == 2 and attr.reason == "hard_failure"
            assert attr.from_provider == "pA" and attr.to_provider == "pB"
        finally:
            await store.close()

    _run(body())


def test_success_on_first_hop_writes_no_budget_record(tmp_path, monkeypatch):
    """首跳即成功(无 fallback)→ 链长 1,hop0=initial,无 budget_exhausted 记录。

    覆盖最常见路径(无 fallback),验证不误写终态归因。
    """
    breaker = _new_breaker(tmp_path)
    monkeypatch.setattr(breaker, "_jitter_fn", lambda: 0.0)
    breaker._now_override = 1000.0

    providers: list[ProviderSpec] = [("pA", "k1", lambda: ("real", "mA"))]

    async def body():
        store = TraceStore(tmp_path / "trace.db")
        await store.init()
        try:
            res = await run_fallback(store, breaker, "CID", providers)
            chain = await store.get_chain("CID")
            return res, chain
        finally:
            await store.close()

    res, chain = _run(body())
    assert res.success is True and res.final_text == "real"
    assert len(chain) == 1
    h0 = parse_attribution(chain[0].hop_attribution)
    assert h0.depth == 0 and h0.reason == "initial"
    assert chain[0].result == "real"  # 成功 result 已落


def test_commit_without_hop_attribution_backward_compat(tmp_path):
    """commit 不传 hop_attribution(默认 None)→ 该列仍 None,result 正常。

    回归门:守现有 4 个调用点(execute_idempotent / 现有 trace 测试)零行为变化。
    """

    async def body():
        store = TraceStore(tmp_path / "trace.db")
        await store.init()
        try:
            out = await store.acquire(
                correlation_id="CID", idempotency_key="k0", provider="pA"
            )
            await store.commit(trace_id=out.trace_id, result="r", latency=1.0, cost=0.1)
            chain = await store.get_chain("CID")
            assert chain[0].hop_attribution is None, "默认 None 必须不碰该列"
            assert chain[0].result == "r" and chain[0].latency == 1.0
        finally:
            await store.close()

    _run(body())


# ── L2:fallback 决策契约(编排 helper + breaker + store)──────────────────


def test_fallback_skips_open_provider(tmp_path, monkeypatch):
    """pA 单 key 熔断(OPEN)→ allow 拒 → helper 跳 pB 成功。

    断言:链长 2;hop0=pA/initial;hop1=pB/key_open/from pA/depth 1;最终 result 来自 pB。
    """
    breaker = _new_breaker(tmp_path)
    monkeypatch.setattr(breaker, "_jitter_fn", lambda: 0.0)
    breaker._now_override = 1000.0
    _seed_closed_key(breaker, "pB", "k1")  # pB 存在 CLOSED → global 不冻
    _trip_key(breaker, "pA", "k1")  # pA 唯一 key OPEN → pA provider OPEN
    assert breaker.allow_request("pA", "k1").allowed is False

    providers: list[ProviderSpec] = [
        ("pA", "k1", lambda: ("unused", "m")),
        ("pB", "k1", lambda: ("real", "mB")),
    ]

    async def body():
        store = TraceStore(tmp_path / "trace.db")
        await store.init()
        try:
            res = await run_fallback(store, breaker, "CID", providers)
            chain = await store.get_chain("CID")
            return res, chain
        finally:
            await store.close()

    res, chain = _run(body())
    assert res.success is True and res.final_text == "real"
    assert len(chain) == 2
    h0 = parse_attribution(chain[0].hop_attribution)
    h1 = parse_attribution(chain[1].hop_attribution)
    assert h0.depth == 0 and h0.reason == "initial" and h0.to_provider == "pA"
    assert h1.depth == 1 and h1.reason == "key_open"
    assert h1.from_provider == "pA" and h1.to_provider == "pB"
    # parent 链:pB → pA
    assert chain[1].parent_correlation_id == chain[0].trace_id


def test_fallback_on_soft_content_incomplete_response(tmp_path, monkeypatch):
    """pA 返回残缺 → is_complete False → record_failure(SOFT_CONTENT) → 跳 pB 成功。

    断言:hop1 reason=soft_content;pA/k1 soft_failures=1(真软失败计数)。
    """
    breaker = _new_breaker(tmp_path)
    monkeypatch.setattr(breaker, "_jitter_fn", lambda: 0.0)
    breaker._now_override = 1000.0

    providers: list[ProviderSpec] = [
        ("pA", "k1", lambda: ("", "mA")),  # 文本空 → is_complete False
        ("pB", "k1", lambda: ("real", "mB")),
    ]

    async def body():
        store = TraceStore(tmp_path / "trace.db")
        await store.init()
        try:
            res = await run_fallback(store, breaker, "CID", providers)
            chain = await store.get_chain("CID")
            return res, chain
        finally:
            await store.close()

    res, chain = _run(body())
    assert res.success is True and res.final_text == "real"
    assert breaker.get_key_state("pA", "k1").soft_failures == 1
    h1 = parse_attribution(chain[1].hop_attribution)
    assert h1.reason == "soft_content" and h1.from_provider == "pA"


def test_fallback_on_hard_failure_exception(tmp_path, monkeypatch):
    """pA 抛异常 → catch → record_failure(HARD) → 跳 pB 成功。

    断言:hop1 reason=hard_failure;pA/k1 hard_failures=1(真硬失败计数)。
    """
    breaker = _new_breaker(tmp_path)
    monkeypatch.setattr(breaker, "_jitter_fn", lambda: 0.0)
    breaker._now_override = 1000.0

    def _boom():
        raise RuntimeError("provider down")

    providers: list[ProviderSpec] = [
        ("pA", "k1", _boom),
        ("pB", "k1", lambda: ("real", "mB")),
    ]

    async def body():
        store = TraceStore(tmp_path / "trace.db")
        await store.init()
        try:
            res = await run_fallback(store, breaker, "CID", providers)
            chain = await store.get_chain("CID")
            return res, chain
        finally:
            await store.close()

    res, chain = _run(body())
    assert res.success is True and res.final_text == "real"
    assert breaker.get_key_state("pA", "k1").hard_failures == 1
    h1 = parse_attribution(chain[1].hop_attribution)
    assert h1.reason == "hard_failure" and h1.from_provider == "pA"


def test_multi_hop_depth_attribution_monotonic(tmp_path, monkeypatch):
    """3 级 fallback:pA(key_open)→pB(soft_content)→pC(success)。

    断言:链长 3;depth 0/1/2 严格递增;hop1 reason=key_open(from pA);
    hop2 reason=soft_content(from pB);parent 链 pA←pB←pC。
    """
    breaker = _new_breaker(tmp_path)
    monkeypatch.setattr(breaker, "_jitter_fn", lambda: 0.0)
    breaker._now_override = 1000.0
    _seed_closed_key(breaker, "pB", "k1")  # 防 global 冻结(pA 是首个被 trip 的)
    _trip_key(breaker, "pA", "k1")

    providers: list[ProviderSpec] = [
        ("pA", "k1", lambda: ("unused", "m")),
        ("pB", "k1", lambda: ("", "mB")),  # soft fail
        ("pC", "k1", lambda: ("real", "mC")),
    ]

    async def body():
        store = TraceStore(tmp_path / "trace.db")
        await store.init()
        try:
            res = await run_fallback(store, breaker, "CID", providers)
            chain = await store.get_chain("CID")
            return res, chain
        finally:
            await store.close()

    res, chain = _run(body())
    assert res.success is True and res.final_text == "real"
    assert len(chain) == 3
    depths = [parse_attribution(r.hop_attribution).depth for r in chain]
    assert depths == [0, 1, 2], f"depth 必须严格递增,实际 {depths}"
    h1 = parse_attribution(chain[1].hop_attribution)
    h2 = parse_attribution(chain[2].hop_attribution)
    assert h1.reason == "key_open" and h1.from_provider == "pA"
    assert h2.reason == "soft_content" and h2.from_provider == "pB"
    # parent 链:pB→pA, pC→pB
    assert chain[1].parent_correlation_id == chain[0].trace_id
    assert chain[2].parent_correlation_id == chain[1].trace_id


def test_replay_idempotent_hop_not_counted_as_new_hop(tmp_path, monkeypatch):
    """同 idempotency_key 二次请求 → acquire REPLAYED → 不计新 hop。

    回归门:守幂等语义(BUG-幂等-01)不被 hop 逻辑破坏。get_chain 仍只 1 条。
    """
    breaker = _new_breaker(tmp_path)
    monkeypatch.setattr(breaker, "_jitter_fn", lambda: 0.0)
    breaker._now_override = 1000.0

    providers: list[ProviderSpec] = [("pA", "k1", lambda: ("real", "mA"))]

    async def body():
        store = TraceStore(tmp_path / "trace.db")
        await store.init()
        try:
            r1 = await run_fallback(store, breaker, "CID", providers)
            r2 = await run_fallback(store, breaker, "CID", providers)  # 同 CID → replay
            chain = await store.get_chain("CID")
            return r1, r2, chain
        finally:
            await store.close()

    r1, r2, chain = _run(body())
    assert r1.success and r1.final_text == "real"
    assert r2.success and r2.last_reason == "replayed"
    assert len(chain) == 1, "幂等 replay 不应产生新 hop"


# ── L3:total_retry_budget=6 硬停止 ────────────────────────────────────────


def test_global_open_freezes_all_providers(tmp_path, monkeypatch):
    """全部 provider 的唯一 key 都 OPEN → global 冻结 → allow 对每个 provider 都返 global_open。

    覆盖 global_open 决策原因(派生模型灾难态):helper 逐个尝试均被拒,
    provider_fn 一次都不调,链耗尽返失败,last_reason=global_open。
    """
    breaker = _new_breaker(tmp_path)
    monkeypatch.setattr(breaker, "_jitter_fn", lambda: 0.0)
    breaker._now_override = 1000.0
    # 两个单 key provider 都 trip → 2/2 OPEN → global OPEN
    _trip_key(breaker, "pA", "k1")
    _trip_key(breaker, "pB", "k1")
    assert breaker.get_global_state().state == CircuitState.OPEN

    calls: dict[str, int] = {}
    providers: list[ProviderSpec] = [
        ("pA", "k1", lambda: calls.__setitem__("pA", calls.get("pA", 0) + 1) or ("x", "m")),
        ("pB", "k1", lambda: calls.__setitem__("pB", calls.get("pB", 0) + 1) or ("x", "m")),
    ]

    async def body():
        store = TraceStore(tmp_path / "trace.db")
        await store.init()
        try:
            res = await run_fallback(store, breaker, "CID", providers)
            chain = await store.get_chain("CID")
            return res, chain
        finally:
            await store.close()

    res, chain = _run(body())
    assert res.success is False
    assert res.last_reason == "global_open"
    assert len(chain) == 2
    h1 = parse_attribution(chain[1].hop_attribution)
    assert h1.reason == "global_open" and h1.from_provider == "pA"
    # global 冻结下 provider_fn 一次都不该被调
    assert calls == {}, "global_open 冻结时 provider_fn 必须不被调用"


def test_total_retry_budget_six_hard_stop(tmp_path, monkeypatch):
    """7 个 provider 全 HARD 失败 → 第 6 次跳变(depth 6)被 check_hop_budget 拦 → 停。

    断言:链长 7(6 hard 记录 + 1 budget_exhausted 终态);末条 reason=budget_exhausted;
    **第 7 个 provider(pG)的 complete_fn 调用计数 == 0**(计数器闭包证明真没调,
    非仅断言链长)。对应 spec Scenario:最坏请求次数有界。
    """
    breaker = _new_breaker(tmp_path)
    monkeypatch.setattr(breaker, "_jitter_fn", lambda: 0.0)
    breaker._now_override = 1000.0

    calls: dict[str, int] = {}

    def _make(name):
        def _fn():
            calls[name] = calls.get(name, 0) + 1
            raise RuntimeError(f"{name} down")

        return _fn

    # 7 个 provider,全失败。budget=6 → 最多尝试 6 个(pA..pF),第 7 个(pG)被拦。
    names = ["pA", "pB", "pC", "pD", "pE", "pF", "pG"]
    providers: list[ProviderSpec] = [(n, "k1", _make(n)) for n in names]

    async def body():
        store = TraceStore(tmp_path / "trace.db")
        await store.init()
        try:
            res = await run_fallback(store, breaker, "CID", providers, budget=6)
            chain = await store.get_chain("CID")
            return res, chain
        finally:
            await store.close()

    res, chain = _run(body())
    assert res.success is False
    assert res.last_reason == "budget_exhausted"
    assert len(chain) == 7, f"链长应为 7(6 hard + 1 budget_exhausted),实际 {len(chain)}"
    # 前 6 条:depth 0..5,末条 budget_exhausted depth 6
    last = parse_attribution(chain[-1].hop_attribution)
    assert last.reason == "budget_exhausted" and last.to_provider is None
    # 关键不假绿断言:pG(第 7 个 provider)真没被调用
    assert calls.get("pG", 0) == 0, "第 7 个 provider 的 complete_fn 必须未被调用(budget 拦下)"
    # 前 6 个各调一次
    assert all(calls.get(n) == 1 for n in names[:6]), f"前 6 个应各调一次,实际 {calls}"
