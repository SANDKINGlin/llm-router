"""S2.4 · Cost Budget Gate 单测 + Cascade 用量记账/cost 门接线。

Cost Gate(design 候选过滤家族,与 compliance 门 ① / health 过滤同属「候选筛选」):
路由前查 token_ledger.total(name),已消费 token ≥ quotas[name] → 剔出候选(降级到剩余免费/mock)。

两块:
  1. CostGate 纯逻辑(过滤/fail-open/无 quota 放行/is_over_budget)。
  2. Cascade 接线:成功 complete() 后 best-effort 记 usage 到 ledger + 路由前 cost 过滤。

TDD:本套件先 RED——CostGate / Usage / complete() 3-tuple 未实现时即失败。
模式沿用 test_cascade/test_policy_enforcer:sync def + asyncio.run;tmp_path 临时 DB。
"""
from __future__ import annotations

import asyncio

from llm_router.api.cascade import Cascade
from llm_router.api.cost_gate import CostGate
from llm_router.api.strategy import RoutingStrategy
from llm_router.providers.base import ChatResult, Provider, ProviderError, Usage
from llm_router.resilience.circuit_breaker import CircuitBreaker
from llm_router.store.token_ledger import LedgerStore
from llm_router.store.trace import TraceStore


def _run(coro):
    return asyncio.run(coro)


def _new_breaker(tmp_path):
    return CircuitBreaker(db_path=tmp_path / "circuit.db", key_hard_threshold=3)


async def _seed(ledger: LedgerStore, name: str, prompt: int, completion: int) -> None:
    """直接往 ledger 写一条,模拟历史消费(供 cost 门判定)。"""
    await ledger.record(
        provider=name, model="m", prompt_tokens=prompt, completion_tokens=completion
    )


class _FixedOrderStrategy(RoutingStrategy):
    def __init__(self, order):
        self._order = list(order)

    def plan(self, candidates, context):
        seen = set(candidates)
        return [c for c in self._order if c in seen]

    def select_provider(self, candidates, context):
        return self.plan(candidates, context)[0]


class _UsageProvider(Provider):
    """可控 provider:返指定 (text, model, Usage);记录调用次数(证明真调/真不调)。"""

    def __init__(self, name, *, text="real", usage=None, counter=None):
        self.name = name
        self._text = text
        self._usage = usage  # Usage 实例或 None
        self._counter = counter

    async def complete(self, messages, *, tools=None, tool_choice=None):
        if self._counter is not None:
            self._counter[self.name] = self._counter.get(self.name, 0) + 1
        return ChatResult(content=self._text, model=f"m-{self.name}", usage=self._usage)


# ── L1:CostGate 纯逻辑 ──────────────────────────────────────────────────────


def test_under_budget_survives(tmp_path):
    """消费 < quota → 保留。"""
    ledger = LedgerStore(tmp_path / "ledger.db")

    async def body():
        await ledger.init()
        try:
            await _seed(ledger, "a", prompt=5, completion=5)  # total 10
            gate = CostGate(ledger, quotas={"a": 100})
            assert await gate.survivors(["a"]) == ["a"]
        finally:
            await ledger.close()

    _run(body())


def test_over_budget_filtered(tmp_path):
    """消费 ≥ quota → 剔出(降级)。"""
    ledger = LedgerStore(tmp_path / "ledger.db")

    async def body():
        await ledger.init()
        try:
            await _seed(ledger, "a", prompt=60, completion=50)  # total 110 ≥ quota 100
            gate = CostGate(ledger, quotas={"a": 100})
            assert await gate.survivors(["a"]) == []
        finally:
            await ledger.close()

    _run(body())


def test_mixed_over_and_under_budget(tmp_path):
    """混合:a 超预算剔出,b 未超保留;顺序保持。"""
    ledger = LedgerStore(tmp_path / "ledger.db")

    async def body():
        await ledger.init()
        try:
            await _seed(ledger, "a", prompt=200, completion=0)  # 200 ≥ 100
            await _seed(ledger, "b", prompt=5, completion=5)  # 10 < 100
            gate = CostGate(ledger, quotas={"a": 100, "b": 100})
            assert await gate.survivors(["a", "b"]) == ["b"]
        finally:
            await ledger.close()

    _run(body())


def test_no_quota_unlimited(tmp_path):
    """quotas 未登记的 name → 无限放行(保留)。"""
    ledger = LedgerStore(tmp_path / "ledger.db")

    async def body():
        await ledger.init()
        try:
            await _seed(ledger, "ghost", prompt=999999, completion=0)
            gate = CostGate(ledger, quotas={"a": 100})  # ghost 不在 quotas
            assert await gate.survivors(["ghost"]) == ["ghost"]
        finally:
            await ledger.close()

    _run(body())


def test_fail_open_on_ledger_error(tmp_path):
    """ledger 查询抛异常 → fail-open 返全候选(cost 软约束,不崩)。"""

    class _BoomLedger:
        async def total(self, provider=None):
            raise RuntimeError("db read failed")

    gate = CostGate(_BoomLedger(), quotas={"a": 100})  # type: ignore[arg-type]
    assert _run(gate.survivors(["a"])) == ["a"]


def test_survivors_preserves_order(tmp_path):
    """幸存者保持原候选序(不重排)。"""
    ledger = LedgerStore(tmp_path / "ledger.db")

    async def body():
        await ledger.init()
        try:
            gate = CostGate(ledger, quotas={})
            assert await gate.survivors(["c", "a", "b"]) == ["c", "a", "b"]
        finally:
            await ledger.close()

    _run(body())


def test_is_over_budget_pure_function():
    """纯函数判定(不查库):有 quota 且 consumed ≥ quota → True;无 quota → False。"""
    gate = CostGate(ledger=None, quotas={"a": 100})  # type: ignore[arg-type]
    assert gate.is_over_budget("a", 100) is True
    assert gate.is_over_budget("a", 99) is False
    assert gate.is_over_budget("ghost", 999999) is False  # 无 quota


# ── L2:Cascade 接线 —— cost 门 + usage 记账 ──────────────────────────────────


def test_cascade_skips_over_budget_provider(tmp_path):
    """a 超预算 → cost 门剔出 → 跳 a、路由 b;a 零调用。"""
    ledger = LedgerStore(tmp_path / "ledger.db")
    store = TraceStore(tmp_path / "trace.db")
    calls: dict[str, int] = {}

    async def body():
        await ledger.init()
        try:
            await _seed(ledger, "a", prompt=200, completion=0)  # a 已超 quota=100
            gate = CostGate(ledger, quotas={"a": 100, "b": 1000})
            cascade = Cascade(
                store,
                _new_breaker(tmp_path),
                _FixedOrderStrategy(["a", "b"]),
                [
                    ("a", _UsageProvider("a", counter=calls), "k1"),
                    ("b", _UsageProvider("b", text="from-b", counter=calls), "k1"),
                ],
                ledger=ledger,
                cost_gate=gate,
            )
            res = await cascade.run([{"role":"user","content":"ping"}], correlation_id="CID")
            return res
        finally:
            await store.close()
            await ledger.close()

    res = _run(body())
    assert res.success is True and res.final_text == "from-b"
    assert calls.get("a", 0) == 0, "超预算的 a 必须零调用(cost 门剔出)"
    assert calls.get("b", 0) == 1


def test_cascade_records_usage_after_success(tmp_path):
    """成功 complete() 后,usage 落 ledger(下次 total 反映)。"""
    ledger = LedgerStore(tmp_path / "ledger.db")
    store = TraceStore(tmp_path / "trace.db")

    async def body():
        await ledger.init()
        try:
            cascade = Cascade(
                store,
                _new_breaker(tmp_path),
                _FixedOrderStrategy(["a"]),
                [("a", _UsageProvider("a", usage=Usage(7, 13)), "k1")],
                ledger=ledger,
                cost_gate=CostGate(ledger, quotas={"a": 1000}),
            )
            res = await cascade.run([{"role":"user","content":"ping"}], correlation_id="CID")
            total = await ledger.total("a")
            return res, total
        finally:
            await store.close()
            await ledger.close()

    res, total = _run(body())
    assert res.success is True
    assert total["prompt_tokens"] == 7 and total["completion_tokens"] == 13


def test_cascade_usage_none_skips_recording(tmp_path):
    """provider 返 usage=None(mock/未报)→ 不写 ledger。"""
    ledger = LedgerStore(tmp_path / "ledger.db")
    store = TraceStore(tmp_path / "trace.db")

    async def body():
        await ledger.init()
        try:
            cascade = Cascade(
                store,
                _new_breaker(tmp_path),
                _FixedOrderStrategy(["a"]),
                [("a", _UsageProvider("a", usage=None), "k1")],
                ledger=ledger,
            )
            await cascade.run([{"role":"user","content":"ping"}], correlation_id="CID")
            return await ledger.total("a")
        finally:
            await store.close()
            await ledger.close()

    total = _run(body())
    assert total["rows"] == 0, "usage=None 时 ledger 不应有记录"


def test_cascade_ledger_write_failure_doesnt_break_request(tmp_path):
    """ledger.record 抛异常 → best-effort 吞掉(log warning),请求仍成功。"""

    class _BoomLedger:
        async def init(self):
            pass

        async def close(self):
            pass

        async def record(self, **kwargs):
            raise RuntimeError("db write failed")

        async def total(self, provider=None):
            return {"rows": 0, "prompt_tokens": 0, "completion_tokens": 0, "cost": 0}

    boom = _BoomLedger()
    store = TraceStore(tmp_path / "trace.db")

    async def body():
        try:
            cascade = Cascade(
                store,
                _new_breaker(tmp_path),
                _FixedOrderStrategy(["a"]),
                [("a", _UsageProvider("a", usage=Usage(1, 1)), "k1")],
                ledger=boom,  # type: ignore[arg-type]
            )
            return await cascade.run([{"role":"user","content":"ping"}], correlation_id="CID")
        finally:
            await store.close()

    res = _run(body())
    assert res.success is True, "ledger 写失败不该崩请求(best-effort)"
    assert res.final_text == "real"


def test_cascade_without_cost_gate_unaffected(tmp_path):
    """Cascade 未挂 cost_gate/ledger(None)→ 行为零变化(向后兼容)。"""
    store = TraceStore(tmp_path / "trace.db")
    pa = _UsageProvider("a")

    async def body():
        try:
            cascade = Cascade(
                store,
                _new_breaker(tmp_path),
                _FixedOrderStrategy(["a"]),
                [("a", pa, "k1")],
            )  # 不传 ledger / cost_gate
            return await cascade.run([{"role":"user","content":"ping"}], correlation_id="CID")
        finally:
            await store.close()

    res = _run(body())
    assert res.success is True and res.final_text == "real"


def test_cost_gate_runs_within_surviving_candidates(tmp_path):
    """cost 门在 _surviving_candidates 内(health 之后):a 超预算 + health 无记录 → 仅 a 被剔。"""
    ledger = LedgerStore(tmp_path / "ledger.db")
    store = TraceStore(tmp_path / "trace.db")

    async def body():
        await ledger.init()
        try:
            await _seed(ledger, "a", prompt=200, completion=0)  # a 超 quota
            gate = CostGate(ledger, quotas={"a": 100, "b": 1000})

            class _SpyHealth:
                """无死亡记录的 spy(验 cost 门在 health 之后仍正确剔 a)。"""

                async def latest_probe(self, providers=None, *, alive_only=False):
                    return []

            cascade = Cascade(
                store,
                _new_breaker(tmp_path),
                _FixedOrderStrategy(["a", "b"]),
                [
                    ("a", _UsageProvider("a"), "k1"),
                    ("b", _UsageProvider("b", text="from-b"), "k1"),
                ],
                health_store=_SpyHealth(),
                ledger=ledger,
                cost_gate=gate,
            )
            res = await cascade.run([{"role":"user","content":"ping"}], correlation_id="CID")
            return res
        finally:
            await store.close()
            await ledger.close()

    res = _run(body())
    assert res.success is True and res.final_text == "from-b"
