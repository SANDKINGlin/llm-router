"""S4.1 接线测试:cascade session_id 透传 context + app._extract_session_id 派生。

Phase1 范围 A(机制就位):测**接线正确**(session_id 流入 strategy context / header 派生),
不测"灰度行为差异"(Phase1 无第二策略)。模式沿用 test_cascade:同步 def + asyncio.run。
"""
from __future__ import annotations

import asyncio

from starlette.requests import Request

from llm_router.api.cascade import Cascade
from llm_router.api.strategy import RoutingStrategy
from llm_router.app import _extract_session_id
from llm_router.providers.mock import MockProvider
from llm_router.resilience.circuit_breaker import CircuitBreaker
from llm_router.store.trace import TraceStore


class _RecordingStrategy(RoutingStrategy):
    """记录 plan 收到的 context(隔离 cascade,不耦合 ε);返首候选当 primary。"""

    def __init__(self):
        self.last_context = None

    def plan(self, candidates, context):
        self.last_context = dict(context)
        if not candidates:
            raise RuntimeError("empty candidates")
        return [candidates[0]]


def _run(coro):
    return asyncio.run(coro)


def _cascade_with_recorder(tmp_path):
    store = TraceStore(tmp_path / "trace.db")
    breaker = CircuitBreaker(tmp_path / "circuit.db")
    strat = _RecordingStrategy()
    cascade = Cascade(
        store,
        breaker,
        strat,
        [("mock", MockProvider(), "mock-key")],
    )
    return cascade, strat


def test_cascade_session_id_passed_to_context(tmp_path):
    """run(session_id=...) → strategy context 含 session_id + gray_released(机制就位)。"""
    cascade, strat = _cascade_with_recorder(tmp_path)

    async def body():
        await cascade.run("ping", correlation_id="CID", session_id="sess-xyz")

    _run(body())
    assert strat.last_context is not None
    assert strat.last_context["session_id"] == "sess-xyz"
    assert "gray_released" in strat.last_context  # 非 None → 判定进 context
    assert isinstance(strat.last_context["gray_released"], bool)


def test_cascade_none_session_id_skips_gray(tmp_path):
    """run(session_id=None) → context 含 session_id=None,无 gray_released 键(不判定)。"""
    cascade, strat = _cascade_with_recorder(tmp_path)

    async def body():
        await cascade.run("ping", correlation_id="CID", session_id=None)

    _run(body())
    assert strat.last_context is not None
    assert strat.last_context["session_id"] is None
    assert "gray_released" not in strat.last_context


def test_cascade_default_session_id_none_backward_compat(tmp_path):
    """run() 不传 session_id → 默认 None(向后兼容,现有 30+ 调用零行为变化)。"""
    cascade, strat = _cascade_with_recorder(tmp_path)

    async def body():
        await cascade.run("ping", correlation_id="CID")

    _run(body())
    assert strat.last_context is not None
    assert strat.last_context["session_id"] is None
    assert "gray_released" not in strat.last_context


def _make_request(headers: dict[str, str]) -> Request:
    """构造最小 Starlette Request(只读 headers,不需完整 ASGI scope)。"""
    raw = [
        (k.lower().encode("latin-1"), v.encode("latin-1"))
        for k, v in headers.items()
    ]
    return Request({"type": "http", "headers": raw})


class TestExtractSessionId:
    def test_explicit_header_used(self):
        assert (
            _extract_session_id(_make_request({"X-Session-Id": "explicit-sess"}))
            == "explicit-sess"
        )

    def test_bearer_key_derived_not_raw(self):
        sid = _extract_session_id(
            _make_request({"Authorization": "Bearer sk-secret-key"})
        )
        assert sid is not None
        assert sid != "sk-secret-key"  # 非 raw key
        assert "secret" not in sid  # hash,不含 key 明文

    def test_explicit_overrides_bearer(self):
        req = _make_request(
            {"X-Session-Id": "explicit", "Authorization": "Bearer sk-x"}
        )
        assert _extract_session_id(req) == "explicit"

    def test_no_headers_returns_none(self):
        assert _extract_session_id(_make_request({})) is None

    def test_same_bearer_same_session(self):
        """同 key → 同 session(按 agent 灰度的基础:同 agent 同桶)。"""
        r1 = _make_request({"Authorization": "Bearer sk-agent-key"})
        r2 = _make_request({"Authorization": "Bearer sk-agent-key"})
        assert _extract_session_id(r1) == _extract_session_id(r2)

    def test_case_insensitive_bearer_prefix(self):
        # Bearer scheme 大小写不敏感(RFC;覆盖 bearer/BEARER/Bearer;OpenCode LOW#5)
        for scheme in ("bearer", "BEARER", "Bearer", "bEaReR"):
            sid = _extract_session_id(
                _make_request({"Authorization": f"{scheme} sk-key"})
            )
            assert sid is not None, f"scheme={scheme!r} 未识别"

    def test_bearer_tab_separator(self):
        # tab 分隔也识别(split(None) 容忍 SP/HTAB;OpenCode LOW#2 修复验证)
        sid = _extract_session_id(_make_request({"Authorization": "Bearer\tsk-key"}))
        assert sid is not None

    def test_empty_bearer_falls_through_to_none(self):
        """Authorization: Bearer (空 token)→ api_key=None → None(防御)。"""
        assert (
            _extract_session_id(_make_request({"Authorization": "Bearer "})) is None
        )

    def test_explicit_newlines_stripped(self):
        """X-Session-Id 含换行 → 清洗(防 log 注入;OpenCode LOW#1 修复验证)。"""
        sid = _extract_session_id(_make_request({"X-Session-Id": "clean\ninjected"}))
        assert sid is not None
        assert "\n" not in sid
        assert "\r" not in sid
