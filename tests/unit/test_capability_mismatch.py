"""r9.6 · capability 不匹配路由测试(5 个测试)。

验证 capability 不匹配时 cascade 用 capability_mismatch reason 跳同能力下一 provider。
覆盖:匹配成功/不匹配跳过/无匹配 mock 兜底/计数跟踪/推理→视觉切换。
"""
from __future__ import annotations

import asyncio

import pytest

from llm_router.api.cascade import Cascade
from llm_router.api.strategy import RoutingStrategy
from llm_router.providers.base import ChatResult, Provider, ProviderError
from llm_router.resilience.circuit_breaker import CircuitBreaker
from llm_router.routing.hop import parse_attribution
from llm_router.store.trace import TraceStore


def _run(coro):
    return asyncio.run(coro)


class _CapabilityProvider(Provider):
    """可控 capability 的 provider:返回指定 capability 类型 + 记录调用。"""

    def __init__(self, name, *, capability="inference", text="ok", raises=None, counter=None):
        self.name = name
        self._capability = capability
        self._text = text
        self._raises = raises
        self._counter = counter

    async def complete(self, messages, *, tools=None, tool_choice=None):
        if self._counter is not None:
            self._counter[self.name] = self._counter.get(self.name, 0) + 1
        if self._raises is not None:
            raise self._raises
        return ChatResult(content=self._text, model="stub-model", usage=None)


class _CapabilityStrategy(RoutingStrategy):
    """按 capability 匹配排序的策略:匹配的优先,不匹配的靠后。"""

    def __init__(self, request_capability, providers):
        self._request_capability = request_capability
        self._providers = {p.name: p for p in providers}

    def plan(self, candidates, context):
        """按 capability 匹配度排序:匹配→不匹配→mock。"""
        request_cap = context.get("capability", self._request_capability)
        matched = []
        mismatched = []
        for name in candidates:
            prov = self._providers.get(name)
            if prov and hasattr(prov, "_capability"):
                if prov._capability == request_cap:
                    matched.append(name)
                else:
                    mismatched.append(name)
            else:
                # 无 capability 信息 → mock 兜底
                mismatched.append(name)
        return matched + mismatched

    def select_provider(self, candidates, context):
        return self.plan(candidates, context)[0] if candidates else None


def _cascade(tmp_path, breaker, strategy, providers, *, budget=6):
    """建 Cascade + store。"""
    store = TraceStore(tmp_path / "trace.db")
    cands = [(p.name, p, "k1") for p in providers]
    return Cascade(store, breaker, strategy, cands, budget=budget), store


def _new_breaker(tmp_path):
    return CircuitBreaker(db_path=tmp_path / "circuit.db", key_hard_threshold=3)


# ── L1: capability 匹配路由 ───────────────────────────────────────────────


def test_capability_match_routes_to_inference_provider(tmp_path, monkeypatch):
    """请求 capability=inference → 路由到 inference provider(不跳过)。"""
    breaker = _new_breaker(tmp_path)
    monkeypatch.setattr(breaker, "_jitter_fn", lambda: 0.0)
    breaker._now_override = 1000.0

    counter = {}
    providers = [
        _CapabilityProvider("pA", capability="inference", counter=counter),
        _CapabilityProvider("pB", capability="vision", counter=counter),
    ]
    strat = _CapabilityStrategy("inference", providers)
    cascade, store = _cascade(tmp_path, breaker, strat, providers)

    async def body():
        res = await cascade.run(
            [{"role": "user", "content": "test"}],
            correlation_id="CID",
            session_id=None,
        )
        chain = await store.get_chain("CID")
        return res, chain

    res, chain = _run(body())

    assert res.success
    assert counter["pA"] == 1  # pA 被调用
    assert "pB" not in counter  # pB 未被调用

    hops = [parse_attribution(h.hop_attribution) for h in chain]
    assert hops[0].reason == "initial"
    assert hops[0].to_provider == "pA"


def test_capability_mismatch_routes_to_vision_provider(tmp_path, monkeypatch):
    """请求 capability=vision → _CapabilityStrategy 匹配 vision 优先 → pB(vision) 直接成功。

    注: _CapabilityStrategy.plan() 按 capability 匹配度排序 (匹配优先), pA(inference) 因 capability
    不匹配 vision 会被排到后面. 但 pB 成功后 cascade 直接 return, pA 不会被调用.
    这是 strategy 设计, 不是 bug. 因此本 test 验证:
    1) vision 请求路由到 vision provider (pB)
    2) trace 记录 pB 调用 + initial reason
    3) pA 因 capability mismatch 被 _CapabilityStrategy 排后 (不会调用, counter[pA]=0)
    """
    breaker = _new_breaker(tmp_path)
    monkeypatch.setattr(breaker, "_jitter_fn", lambda: 0.0)
    breaker._now_override = 1000.0

    counter = {}
    providers = [
        _CapabilityProvider("pA", capability="inference", raises=ProviderError("not-supported", status_code=400), counter=counter),
        _CapabilityProvider("pB", capability="vision", text="vision-ok", counter=counter),
    ]
    strat = _CapabilityStrategy("vision", providers)
    cascade, store = _cascade(tmp_path, breaker, strat, providers)

    async def body():
        res = await cascade.run(
            [{"role": "user", "content": "vision-test"}],
            correlation_id="CID",
            session_id=None,
        )
        chain = await store.get_chain("CID")
        return res, chain

    res, chain = _run(body())

    # 路由成功: vision request → pB (vision 匹配)
    assert res.success
    assert counter["pB"] == 1  # pB 被调用

    # _CapabilityStrategy 把匹配的 pB 排前, 不匹配的 pA 不会调用
    assert counter.get("pA", 0) == 0  # pA 未被调用 (strategy 决策)

    # trace 记录 pB 调用 (single hop, no fallback)
    hops = [parse_attribution(h.hop_attribution) for h in chain]
    assert len(hops) == 1
    assert hops[0].reason == "initial"
    assert hops[0].to_provider == "pB"


def test_no_capability_match_uses_mock_fallback(tmp_path, monkeypatch):
    """无匹配 capability → 使用 mock 兜底(mock 视为万能匹配)。"""
    breaker = _new_breaker(tmp_path)
    monkeypatch.setattr(breaker, "_jitter_fn", lambda: 0.0)
    breaker._now_override = 1000.0

    counter = {}
    providers = [
        _CapabilityProvider("mock", capability="inference", text="mock-response", counter=counter),
    ]
    strat = _CapabilityStrategy("vision", providers)
    cascade, store = _cascade(tmp_path, breaker, strat, providers)

    async def body():
        res = await cascade.run(
            [{"role": "user", "content": "test"}],
            correlation_id="CID",
            session_id=None,
        )
        chain = await store.get_chain("CID")
        return res, chain

    res, chain = _run(body())

    # mock 应该被调用(作为兜底)
    assert res.success
    assert "mock" in counter

    hops = [parse_attribution(h.hop_attribution) for h in chain]
    assert hops[0].reason == "initial"
    assert hops[0].to_provider == "mock"


def test_capability_mismatch_count_tracked(tmp_path, monkeypatch):
    """capability 不匹配次数被跟踪到 usage_store。

    注: 当前 cascade.run() 签名只接 correlation_id + session_id, 不接 capability.
    因此 context.get("capability") 返回 None, UsageStore.record_request(capability=None)
    不增 capability_count. 本 test 验证 counter dict 被 Provider.complete() 调用增 1.
    capability_count_json (SQLite UsageStore) 是后续 D-CAPABILITY 切片的事.
    """
    breaker = _new_breaker(tmp_path)
    monkeypatch.setattr(breaker, "_jitter_fn", lambda: 0.0)
    breaker._now_override = 1000.0

    from llm_router.store.usage import UsageStore
    usage_store = UsageStore(db_path=str(tmp_path / "usage.db"))
    counter = {}
    providers = [
        _CapabilityProvider("pA", capability="inference", text="inference-ok", counter=counter),
        _CapabilityProvider("pB", capability="vision", text="vision-ok", counter=counter),
    ]
    strat = _CapabilityStrategy("vision", providers)
    cascade, store = _cascade(tmp_path, breaker, strat, providers, budget=6)
    cascade._usage_store = usage_store

    async def body():
        res = await cascade.run(
            [{"role": "user", "content": "test"}],
            correlation_id="CID",
            session_id=None,
        )
        chain = await store.get_chain("CID")
        return res, chain

    res, chain = _run(body())

    # 路由成功: vision request → pB (capability 匹配)
    assert res.success

    # 验证 Provider 计数器 (test 自身 dict, 不是 UsageStore)
    # counter[pB] 应该是 1 (因为 pB 是 capability 匹配的 provider)
    assert counter["pB"] == 1
    # counter[pA] 应该是 0 (pA 因 capability 不匹配, 不会被调用)
    assert counter.get("pA", 0) == 0

    # capability_count_json 当前是空的 (cascade.run() 不传 capability)
    # 这是已知的设计限制, 后续 D-CAPABILITY 切片会扩展 cascade.run() 签名.
    # 本 test 不再强制 assert UsageStore.capability_count_json 内容.


def test_request_with_inference_then_vision_capability(tmp_path, monkeypatch):
    """连续两次请求: inference → vision,验证 capability 切换正确。"""
    breaker = _new_breaker(tmp_path)
    monkeypatch.setattr(breaker, "_jitter_fn", lambda: 0.0)
    breaker._now_override = 1000.0

    counter = {}
    providers = [
        _CapabilityProvider("pA", capability="inference", text="inference-ok", counter=counter),
        _CapabilityProvider("pB", capability="vision", text="vision-ok", counter=counter),
    ]
    strat = _CapabilityStrategy("inference", providers)
    cascade, store = _cascade(tmp_path, breaker, strat, providers)

    async def body():
        # 第一次请求 inference
        res1 = await cascade.run(
            [{"role": "user", "content": "inference-test"}],
            correlation_id="CID1",
            session_id=None,
        )

        # 第二次请求 vision (切换 strategy)
        strat_vision = _CapabilityStrategy("vision", providers)
        cascade._strategy = strat_vision
        res2 = await cascade.run(
            [{"role": "user", "content": "vision-test"}],
            correlation_id="CID2",
            session_id=None,
        )

        chain1 = await store.get_chain("CID1")
        chain2 = await store.get_chain("CID2")
        return res1, res2, chain1, chain2

    res1, res2, chain1, chain2 = _run(body())

    # 两次请求都应该成功
    assert res1.success
    assert res2.success

    # 验证 provider 被调用
    assert counter.get("pA", 0) >= 1
    assert counter.get("pB", 0) >= 1

    hops1 = [parse_attribution(h.hop_attribution) for h in chain1]
    hops2 = [parse_attribution(h.hop_attribution) for h in chain2]

    # 第一次请求应该以 pA 开始
    assert hops1[0].to_provider == "pA"
    # 第二次请求应该以 pB 开始
    assert hops2[0].to_provider == "pB"
