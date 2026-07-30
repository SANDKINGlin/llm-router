"""router-hop-rate-limited-fix · cascade↔hop 跳变原因契约单测。

回归 bug:上游 provider 返 429 → cascade 设 last_reason="rate_limited" →
hop.advance() 的 `assert reason in HOP_REASONS` 崩 → ASGI 500 → 调用方超时。
同病根:回滚移除 provider 时 cascade 设 "provider_removed_during_rollback" 也不在闭集。

修复后契约:rate_limited / provider_removed_during_rollback 都归 hard_failure 类
(闭集内),advance 不崩,链路降级到下一 provider;未知 reason 抛 HopReasonError
而非裸 AssertionError。

TDD:本文件先写为失败(复现 500/AssertionError),改 hop.py + cascade.py 后转绿。
"""
from __future__ import annotations

import asyncio

import pytest

from llm_router.api.cascade import Cascade
from llm_router.api.strategy import RoutingStrategy
from llm_router.providers.base import ChatResult, Provider, ProviderError
from llm_router.resilience.circuit_breaker import CircuitBreaker, TripReason
from llm_router.routing.hop import HOP_REASONS, advance, parse_attribution
from llm_router.store.trace import TraceStore


def _run(coro):
    return asyncio.run(coro)


def _new_breaker(tmp_path):
    return CircuitBreaker(db_path=tmp_path / "circuit.db", key_hard_threshold=3)


class _FakeProvider(Provider):
    """可控 provider:可配返成功/抛 ProviderError(含 429 限流)。"""

    def __init__(self, name, *, text="real", model="mX", raises=None, counter=None):
        self.name = name
        self._text = text
        self._model = model
        self._raises = raises
        self._counter = counter

    async def complete(self, messages, *, tools=None, tool_choice=None):
        if self._counter is not None:
            self._counter[self.name] = self._counter.get(self.name, 0) + 1
        if self._raises is not None:
            raise self._raises
        return ChatResult(content=self._text, model=self._model, usage=None)


class _FixedOrderStrategy(RoutingStrategy):
    def __init__(self, order):
        self._order = list(order)

    def plan(self, candidates, context):
        seen = set(candidates)
        return [c for c in self._order if c in seen]

    def select_provider(self, candidates, context):
        return self.plan(candidates, context)[0]


def _cascade(tmp_path, breaker, strategy, providers, *, budget=6):
    store = TraceStore(tmp_path / "trace.db")
    cands = [(p.name, p, "k1") for p in providers]
    return Cascade(store, breaker, strategy, cands, budget=budget), store


# ── 契约:advance 对未知 reason 抛 HopReasonError(非裸 AssertionError) ──────


def test_advance_unknown_reason_raises_hop_reason_error():
    """未知 reason 抛 HopReasonError(ValueError 子类),不再是裸 AssertionError。

    防御性契约:未来再有人传未登记 reason,上层可捕获返明确错误码,而非 ASGI 500 崩。
    """
    from llm_router.routing.hop import HopReasonError

    with pytest.raises(HopReasonError):
        advance(0, "totally_made_up", "pA", "pB")
    # HopReasonError 应是 ValueError 子类(语义=非法入参,便于上层 except ValueError 兜底)
    assert issubclass(HopReasonError, ValueError)


def test_advance_known_reasons_do_not_raise():
    """闭集内每个非特殊态 reason 都能 advance 不抛(契约不回归)。"""
    for reason in HOP_REASONS:
        if reason in ("initial", "budget_exhausted"):
            continue  # 特殊态由 initial_attribution / budget_exhausted 产出
        attr = advance(0, reason, "pA", "pB")
        assert attr.reason == reason
        assert attr.depth == 1


# ── 回归:429 限流不再 500,降级到下一 provider ──────────────────────────────


def test_rate_limit_429_fallback_no_crash(tmp_path, monkeypatch):
    """pA 返 429 限流 → cascade 归 hard_failure 类 → advance 不崩 → 降级 pB 成功。

    修复前:advance 收到 "rate_limited" → AssertionError → 链路 500(无降级)。
    修复后:reason="hard_failure"(闭集内),pB 被尝试并成功。
    breaker 层仍用 TripReason.RATE_LIMIT 记精准退避(retry_after 不变)。
    """
    breaker = _new_breaker(tmp_path)
    monkeypatch.setattr(breaker, "_jitter_fn", lambda: 0.0)
    breaker._now_override = 1000.0
    calls: dict[str, int] = {}
    strat = _FixedOrderStrategy(["pA", "pB"])
    cascade, store = _cascade(
        tmp_path,
        breaker,
        strat,
        [
            _FakeProvider(
                "pA",
                raises=ProviderError("pA rate-limited", status_code=429, retry_after=30.0),
                counter=calls,
            ),
            _FakeProvider("pB", model="mB", counter=calls),
        ],
    )

    async def body():
        try:
            res = await cascade.run([{"role": "user", "content": "ping"}], correlation_id="CID")
            chain = await store.get_chain("CID")
            return res, chain
        finally:
            await store.close()

    res, chain = _run(body())
    # 关键断言:不崩、降级到 pB 成功(修复前这里会抛 AssertionError)
    assert res.success is True and res.final_text == "real"
    assert calls == {"pA": 1, "pB": 1}, f"应先调 pA(429)再降级 pB,实际 {calls}"
    assert len(chain) == 2
    h1 = parse_attribution(chain[1].hop_attribution)
    # 限流归 hard_failure 类(闭集内),不当成功 hop
    assert h1.reason == "hard_failure"
    assert h1.from_provider == "pA" and h1.to_provider == "pB"
    # breaker 层 RATE_LIMIT 精准退避保留(retry_after 生效)
    ks = breaker.get_key_state("pA", "k1")
    assert ks.hard_failures >= 1, "429 限流应计入 failed_providers(归 hard_failure 类)"


# ── 回归:429 限流兜底到 budget 耗尽也不崩(全链 429) ────────────────────────


def test_rate_limit_429_all_providers_budget_exhausted_no_crash(tmp_path, monkeypatch):
    """所有 provider 都 429 → 全归 hard_failure → budget=2 耗尽 → budget_exhausted 终态。

    修复前:第二跳 advance("rate_limited") 就崩,根本到不了 budget 终态。
    修复后:每个 429 都 advance 成功,budget 拦下写终态,链路优雅失败(非 500)。
    """
    breaker = _new_breaker(tmp_path)
    monkeypatch.setattr(breaker, "_jitter_fn", lambda: 0.0)
    breaker._now_override = 1000.0
    calls: dict[str, int] = {}
    names = ["pA", "pB", "pC"]
    strat = _FixedOrderStrategy(names)
    cascade, store = _cascade(
        tmp_path,
        breaker,
        strat,
        [
            _FakeProvider(n, raises=ProviderError(f"{n} 429", status_code=429, retry_after=10.0), counter=calls)
            for n in names
        ],
        budget=2,
    )

    async def body():
        try:
            res = await cascade.run([{"role": "user", "content": "ping"}], correlation_id="CID")
            chain = await store.get_chain("CID")
            return res, chain
        finally:
            await store.close()

    res, chain = _run(body())
    assert res.success is False
    assert res.last_reason == "budget_exhausted"
    # budget=2 → 只试 pA/pB,pC 被拦
    assert calls == {"pA": 1, "pB": 1}
    last = parse_attribution(chain[-1].hop_attribution)
    assert last.reason == "budget_exhausted" and last.to_provider is None
    # 每个被试的 429 都归 hard_failure(非 rate_limited 崩溃)
    for row in chain[:-1]:
        h = parse_attribution(row.hop_attribution)
        assert h.reason in HOP_REASONS, f"reason {h.reason!r} 必须在闭集内(不崩)"


# ── 回归:回滚移除 provider 不再崩(apply_policy 在 await 间隙换 _providers) ──


def test_provider_removed_during_rollback_fallback_no_crash(tmp_path, monkeypatch):
    """pA 失败后,apply_policy 在 await 间隙把 pB 移出 _providers →
    idx=1 查 _providers["pB"] KeyError → cascade 设
    last_reason="provider_removed_during_rollback" → idx=2 advance 该 reason。

    修复前:advance 收到 "provider_removed_during_rollback" → AssertionError → 500。
    修复后:归 hard_failure 类(闭集内),pC 被尝试并成功。

    触发方式:pA 的 complete() 期间 pop pB(模拟 apply_policy 原子换 _providers 的竞态)。
    """
    breaker = _new_breaker(tmp_path)
    monkeypatch.setattr(breaker, "_jitter_fn", lambda: 0.0)
    breaker._now_override = 1000.0
    calls: dict[str, int] = {}
    cascade_holder: list = []

    class _RemovingProvider(Provider):
        name = "pA"

        async def complete(self, messages, *, tools=None, tool_choice=None):
            calls["pA"] = calls.get("pA", 0) + 1
            # 模拟 apply_policy 在 await 间隙换 _providers:pB 被移除
            cascade_holder[0]._providers.pop("pB", None)
            raise ProviderError("pA hard down")

    strat = _FixedOrderStrategy(["pA", "pB", "pC"])
    cascade, store = _cascade(
        tmp_path,
        breaker,
        strat,
        [
            _RemovingProvider(),
            _FakeProvider("pB", model="mB", counter=calls),  # 不会被调(KeyError 先于 complete)
            _FakeProvider("pC", model="mC", counter=calls),
        ],
    )
    cascade_holder.append(cascade)

    async def body():
        try:
            res = await cascade.run([{"role": "user", "content": "ping"}], correlation_id="CID")
            chain = await store.get_chain("CID")
            return res, chain
        finally:
            await store.close()

    res, chain = _run(body())
    # 关键:不崩、降级到 pC 成功(修复前这里会抛 AssertionError: 未知跳变原因)
    assert res.success is True and res.final_text == "real"
    assert calls == {"pA": 1, "pC": 1}, f"pB 应被 KeyError 跳过(不调),实际 {calls}"
    assert len(chain) == 3
    # idx=2 的归因 reason 必须在闭集内(回滚移除归 hard_failure 类)
    h2 = parse_attribution(chain[2].hop_attribution)
    assert h2.reason in HOP_REASONS, f"回滚移除 reason {h2.reason!r} 必须在闭集内(不崩)"
    assert h2.reason == "hard_failure"
    assert h2.from_provider == "pB" and h2.to_provider == "pC"


# ── 契约:rate_limited 不当成功 hop 推进(计入失败) ──────────────────────────


def test_rate_limit_records_failure_not_success(tmp_path, monkeypatch):
    """429 限流归 hard_failure 类 → 计入 failed_providers,不当成功 hop。

    守 spec:限流触发降级而非成功 hop;HopAttribution 不把它当成功推进。
    """
    breaker = _new_breaker(tmp_path)
    monkeypatch.setattr(breaker, "_jitter_fn", lambda: 0.0)
    breaker._now_override = 1000.0
    strat = _FixedOrderStrategy(["pA", "pB"])
    cascade, store = _cascade(
        tmp_path,
        breaker,
        strat,
        [
            _FakeProvider("pA", raises=ProviderError("429", status_code=429, retry_after=5.0)),
            _FakeProvider("pB", model="mB"),
        ],
    )

    async def body():
        try:
            res = await cascade.run([{"role": "user", "content": "ping"}], correlation_id="CID")
            chain = await store.get_chain("CID")
            return res, chain
        finally:
            await store.close()

    res, chain = _run(body())
    assert res.success is True  # pB 兜底成功
    h1 = parse_attribution(chain[1].hop_attribution)
    # 归因是失败类跳变(hard_failure),不是 initial(成功首跳)
    assert h1.reason == "hard_failure"
    assert h1.from_provider == "pA"  # pA 被放弃(失败),不是被跳过的成功


# ── 防御:契约破裂时 HopReasonError 不冒到 ASGI(返失败 result 非 raise) ──────


def test_hop_reason_error_does_not_propagate(tmp_path, monkeypatch):
    """契约破裂(advance 收到未登记 reason)时,cascade.run SHALL 捕获返失败 CascadeResult,
    不让 HopReasonError 冒到 ASGI 裸 500(spec:明确错误码+记 trace 非 assert 崩)。

    构造:pA 失败后,monkeypatch advance 抛 HopReasonError(模拟未来某 last_reason 漏登记),
    断言 run 返 success=False/last_reason="hop_reason_error",而非抛异常。
    """
    from llm_router.routing import hop as hop_mod
    from llm_router.routing.hop import HopReasonError

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

    real_advance = hop_mod.advance

    def _leak(*args, **kwargs):
        # 模拟未来契约破裂:cascade 传了未登记 reason → advance 抛 HopReasonError
        raise HopReasonError("simulated contract leak")

    # 仅在第二跳(idx=1,advance 被调)时抛;首跳 initial_attribution 不走 advance。
    call_count = {"n": 0}

    def _advance_once(*a, **kw):
        call_count["n"] += 1
        if call_count["n"] >= 1:
            raise HopReasonError("simulated contract leak")
        return real_advance(*a, **kw)

    monkeypatch.setattr(hop_mod, "advance", _advance_once)
    # cascade.py 内 advance 是直接 import 的名字,patch 模块属性不够,需 patch cascade 命名空间
    import llm_router.api.cascade as cascade_mod

    monkeypatch.setattr(cascade_mod, "advance", _advance_once)

    async def body():
        try:
            res = await cascade.run([{"role": "user", "content": "ping"}], correlation_id="CID")
            return res
        finally:
            await store.close()

    res = _run(body())
    # 关键:不抛(非 ASGI 500),返失败 result
    assert res.success is False
    assert res.last_reason == "hop_reason_error"
