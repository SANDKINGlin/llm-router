"""Phase 1 集成验证·子片 2:端到端 happy path 经 FastAPI 协议入口。

聚合验证 /v1/chat/completions 与 /v1/messages 全链路打通(请求→ session_id 派生 →
合规门 → health/cost 过滤 → strategy.plan → cascade.run → provider.complete →
trace + ledger 写),通过 TestClient(app) 而非直接组装 Cascade,补 子片 1(组件层
直接 .run())未覆盖的:
- FastAPI 双协议契约(OpenAI / Anthropic 响应塑形 + session_id 派生入口)
- 模块级 _cascade 单例的注入测试模式(monkeypatch.setattr 替换;endpoint 通过
  globals 解析,故 patch 生效;TestClient 不 with → lifespan 不触发,production
  data/*.db 不污染)
- SOFT_CONTENT 切换链路(吸收子片 1 OpenCode #3 defer:首跳 is_complete=False 即
  SOFT_CONTENT,下跳成功;hop_attribution 链验证 from/to/depth/reason)
- trace+ledger 真写入持久化(tmp_path 隔离 SQLite WAL)

不同于:
- tests/test_health.py(/healthz only,minimum smoke test)
- tests/integration/test_phase1_critical_bugs.py(组件层全开 Cascade,直接 .run())
- tests/e2e/test_fallback_e2e.py(orchestrator 测试 helper,非生产 Cascade)

防假绿:每跳 provider 用 counter 验证真被调用过(不依赖响应文本判断 cascade 路径);
trace 行 hop_attribution 用 parse_attribution 反解析验 reason/from/to/depth(不
仅看 row count)。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from llm_router.api.cascade import Cascade
from llm_router.api.cost_gate import CostGate
from llm_router.api.gray import derive_session_id
from llm_router.api.policy_enforcer import PolicyEnforcer
from llm_router.api.strategy import RoutingStrategy
from llm_router.config import ProviderEntry
from llm_router.providers.base import ChatResult, Provider, Usage
from llm_router.resilience.circuit_breaker import CircuitBreaker
from llm_router.routing.hop import parse_attribution
from llm_router.store.health_store import HealthStore
from llm_router.store.token_ledger import LedgerStore
from llm_router.store.trace import TraceStore


# ── 测试 stub providers(防真 HTTP)────────────────────────────────────────


class _StubProvider(Provider):
    """可控 provider:配返成功 + 可选 usage,记录调用计数 + 抓 prompt(防假绿)。"""

    def __init__(
        self,
        name: str,
        *,
        text: str = "ok-response",
        model: str = "stub-model",
        usage: Usage | None = None,
        counter: dict[str, int] | None = None,
        prompts: list[str] | None = None,
    ) -> None:
        self.name = name
        self._text = text
        self._model = model
        self._usage = usage
        self._counter = counter
        # OpenCode 子片2 MED #1:抓 prompt 验 _extract_prompt 真路径。
        self._prompts = prompts

    async def complete(self, messages, *, tools=None, tool_choice=None):
        if self._counter is not None:
            self._counter[self.name] = self._counter.get(self.name, 0) + 1
        if self._prompts is not None:
            self._prompts.append(messages)
        return ChatResult(content=self._text, model=self._model, usage=self._usage)


class _SoftProvider(Provider):
    """返残缺(text=""),触发 is_complete=False → SOFT_CONTENT(测 fallback 切换)。

    可选 usage:即便 SOFT 也消耗 token,cascade 设计在 is_complete 判定**前**记账
    (cascade.py:124)。给 SOFT 配真 usage 后,ledger 应有该跳的记账行——OpenCode
    子片2 MED #4 反例闭合(若有人把 _record_usage 挪到 is_complete 后,SOFT 不记账,
    本断言会败)。
    """

    def __init__(
        self,
        name: str = "soft",
        *,
        counter: dict[str, int] | None = None,
        usage: Usage | None = None,
    ) -> None:
        self.name = name
        self._counter = counter
        self._usage = usage

    async def complete(self, messages, *, tools=None, tool_choice=None):
        if self._counter is not None:
            self._counter[self.name] = self._counter.get(self.name, 0) + 1
        return ChatResult(content="", model="soft-model", usage=self._usage)  # text="" → is_complete False(SOFT_CONTENT)


class _FixedOrderStrategy(RoutingStrategy):
    """确定性策略:plan 返固定序(隔离 cascade,不耦合 ε 探索)。"""

    def __init__(self, order: list[str]) -> None:
        self._order = list(order)

    def plan(self, candidates, context):
        seen = set(candidates)
        return [c for c in self._order if c in seen]


class _RecordingStrategy(RoutingStrategy):
    """plan() 时把 context["session_id"] 记到列表(测 session_id 真派生进 cascade)。"""

    def __init__(self, order: list[str]) -> None:
        self._order = list(order)
        self.captured: list[str | None] = []

    def plan(self, candidates, context):
        self.captured.append(context.get("session_id"))
        seen = set(candidates)
        return [c for c in self._order if c in seen]


# ── 隔离 Cascade 装配(tmp_path,不碰 production data/)─────────────────


def _entry(name: str) -> ProviderEntry:
    return ProviderEntry(
        name=name,
        tier="fast",
        quota=1_000_000,
        cooldown_s=30,
        is_free=True,
        cost_multiplier=0.0,
    )


def _make_isolated_cascade(
    tmp_path: Path,
    candidates: list[tuple[str, Provider, str]],
    *,
    strategy: RoutingStrategy | None = None,
) -> Cascade:
    """构造 tmp 隔离 Cascade(独立 SQLite WAL,默认 _FixedOrderStrategy 按 candidates 序)。"""
    entries = {name: _entry(name) for name, _p, _k in candidates}
    ledger = LedgerStore(tmp_path / "ledger.db")
    quotas = {name: 1_000_000 for name, _p, _k in candidates}
    cost_gate = CostGate(ledger, quotas)
    strat = strategy or _FixedOrderStrategy([n for n, _p, _k in candidates])
    return Cascade(
        store=TraceStore(tmp_path / "trace.db"),
        breaker=CircuitBreaker(tmp_path / "circuit.db"),
        strategy=strat,
        candidates=candidates,
        health_store=HealthStore(tmp_path / "health.db"),
        policy_enforcer=PolicyEnforcer(entries.values()),
        ledger=ledger,
        cost_gate=cost_gate,
    )


@pytest.fixture
def patched_app(monkeypatch: pytest.MonkeyPatch):
    """通用 fixture:返 (app, install_fn)。

    install_fn(cascade) 把 cascade 注入 app._cascade(模块级单例,endpoint handlers
    通过模块 globals 解析 _cascade,故 monkeypatch.setattr 生效)。lifespan 不触发
    (TestClient 不 `with`),production data/*.db 不污染。
    """
    from llm_router import app as app_mod

    def install(cascade: Cascade) -> None:
        monkeypatch.setattr(app_mod, "_cascade", cascade)

    return app_mod.app, install


# ── 直查 SQLite(不依赖 cascade 内部 API,验真持久化)──────────────────


def _trace_rows(tmp_path: Path) -> list[dict]:
    """读 trace 表全部行(按 idempotency_key 序,= cascade 内 hop 序)。"""
    conn = sqlite3.connect(tmp_path / "trace.db")
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM trace ORDER BY idempotency_key"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _ledger_rows(tmp_path: Path) -> list[dict]:
    conn = sqlite3.connect(tmp_path / "ledger.db")
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM token_ledger ORDER BY ledger_id"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# ──────────────────────────────────────────────────────────────────────────
# 双协议 happy path:契约 + trace 真写入
# ──────────────────────────────────────────────────────────────────────────


def test_openai_endpoint_e2e_happy_path_writes_trace(patched_app, tmp_path):
    """OpenAI /v1/chat/completions:200 + 响应塑形 + 1 trace 行(reason=initial)。"""
    app, install = patched_app
    counter: dict[str, int] = {}
    cascade = _make_isolated_cascade(
        tmp_path,
        [("stubA", _StubProvider("stubA", text="hello-A", model="m-A", counter=counter), "kA")],
    )
    install(cascade)

    client = TestClient(app)
    r = client.post(
        "/v1/chat/completions",
        json={"model": "any", "messages": [{"role": "user", "content": "ping"}]},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    # OpenCode 子片2 MED #6 部分接受:补 id 字段断言(响应塑形契约)。
    # chat-stream-support:id 改为 chatcmpl-<uuid 前缀>(标准 OpenAI 兼容,Cline 期望唯一 id)
    assert body["id"].startswith("chatcmpl-")
    assert body["object"] == "chat.completion"
    assert body["choices"][0]["message"]["content"] == "hello-A"
    assert body["choices"][0]["finish_reason"] == "stop"
    assert body["model"] == "m-A"
    assert counter == {"stubA": 1}, "防假绿:provider 必须真被调过 1 次"

    rows = _trace_rows(tmp_path)
    assert len(rows) == 1
    attr = parse_attribution(rows[0]["hop_attribution"])
    assert attr is not None
    assert attr.depth == 0 and attr.reason == "initial"
    assert attr.from_provider is None and attr.to_provider == "stubA"
    assert rows[0]["provider"] == "stubA"
    assert rows[0]["result"] == "hello-A"


def test_anthropic_endpoint_e2e_happy_path_writes_trace(patched_app, tmp_path):
    """Anthropic /v1/messages:200 + 响应塑形 + 1 trace 行 + hop_attribution 验证。

    OpenCode 子片2 MED #3 闭合:与 OpenAI 测试对称,补 hop_attribution 断言。
    """
    app, install = patched_app
    counter: dict[str, int] = {}
    cascade = _make_isolated_cascade(
        tmp_path,
        [("stubB", _StubProvider("stubB", text="hello-B", model="m-B", counter=counter), "kB")],
    )
    install(cascade)

    client = TestClient(app)
    r = client.post(
        "/v1/messages",
        json={"model": "any", "messages": [{"role": "user", "content": "ping"}], "max_tokens": 10},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    # OpenCode 子片2 MED #6 闭合:补 id 字段断言。
    assert body["id"] == "msg_mock"
    assert body["type"] == "message"
    assert body["role"] == "assistant"
    assert body["content"][0]["type"] == "text"
    assert body["content"][0]["text"] == "hello-B"
    assert body["stop_reason"] == "end_turn"
    assert body["model"] == "m-B"
    assert counter == {"stubB": 1}

    rows = _trace_rows(tmp_path)
    assert len(rows) == 1
    assert rows[0]["provider"] == "stubB"
    assert rows[0]["result"] == "hello-B"
    # OpenCode 子片2 MED #3 闭合:hop_attribution 断言对称(原 OpenAI 测试已做)。
    attr = parse_attribution(rows[0]["hop_attribution"])
    assert attr is not None
    assert attr.depth == 0 and attr.reason == "initial"
    assert attr.from_provider is None and attr.to_provider == "stubB"


# ──────────────────────────────────────────────────────────────────────────
# SOFT_CONTENT 切换链路(吸收子片 1 OpenCode #3 defer)
# ──────────────────────────────────────────────────────────────────────────


def test_soft_content_falls_back_to_next_provider_with_correct_hop_chain(
    patched_app, tmp_path
):
    """soft 首跳返 "" → SOFT_CONTENT → 下跳 stubB 成功。

    吸收子片 1 OpenCode #3 defer——经 cascade.run() **真路径**(非组件直组装)走
    SOFT_CONTENT → 下一跳。验证:
      ① 响应 final_text == stubB 的输出(非 soft 的"")
      ② 防假绿:soft 与 stubB 各调用 1 次(不靠响应判断 cascade 真走了 fallback)
      ③ trace 2 行,hop chain:#0 soft initial / #1 stubB advance(soft_content,
         from=soft, to=stubB)
      ④ #0 result="" / #1 result="recovered-text"(成功 row 才填 result)
      ⑤ OpenCode 子片2 MED #4 闭合:soft 跳 token 已消耗 → ledger 写入(cascade 设计
         在 is_complete 判定**前**记账)。若有人把 _record_usage 挪到 is_complete 后,
         此断言会败(ledger 仅 stubB 1 行,缺 soft 那行)。
    """
    app, install = patched_app
    counter: dict[str, int] = {}
    soft_usage = Usage(prompt_tokens=20, completion_tokens=0, cost=None)
    candidates: list[tuple[str, Provider, str]] = [
        ("soft", _SoftProvider("soft", counter=counter, usage=soft_usage), "k1"),
        (
            "stubB",
            _StubProvider(
                "stubB",
                text="recovered-text",
                model="m-B",
                usage=Usage(prompt_tokens=20, completion_tokens=10, cost=None),
                counter=counter,
            ),
            "k2",
        ),
    ]
    cascade = _make_isolated_cascade(tmp_path, candidates)
    install(cascade)

    client = TestClient(app)
    r = client.post(
        "/v1/chat/completions",
        json={"model": "any", "messages": [{"role": "user", "content": "ping"}]},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["choices"][0]["message"]["content"] == "recovered-text"
    assert body["model"] == "m-B"

    # 防假绿:counter 真验证 soft + stubB 各调用 1 次(不依赖 mock 恒存活隐式)
    assert counter == {"soft": 1, "stubB": 1}, f"call counter mismatch: {counter}"

    rows = _trace_rows(tmp_path)
    assert len(rows) == 2, f"应 2 trace 行(soft#0 + stubB#1),实际 {len(rows)}"
    soft_row, stubB_row = rows[0], rows[1]
    assert soft_row["provider"] == "soft"
    assert stubB_row["provider"] == "stubB"
    # 失败跳 result 为空字符串(commit 时传 "")
    assert soft_row["result"] == "" and stubB_row["result"] == "recovered-text"

    # hop_attribution 链:#0 initial soft / #1 advance soft_content from=soft to=stubB
    soft_attr = parse_attribution(soft_row["hop_attribution"])
    stubB_attr = parse_attribution(stubB_row["hop_attribution"])
    assert soft_attr is not None and stubB_attr is not None
    assert soft_attr.depth == 0 and soft_attr.reason == "initial"
    assert soft_attr.from_provider is None and soft_attr.to_provider == "soft"
    assert stubB_attr.depth == 1 and stubB_attr.reason == "soft_content"
    assert stubB_attr.from_provider == "soft" and stubB_attr.to_provider == "stubB"

    # OpenCode 子片2 MED #4:验 _record_usage 在 is_complete 前(SOFT 跳也记账)。
    # ledger 应有 2 行(soft 与 stubB 各 1)——若 _record_usage 挪到 is_complete 后,
    # 仅 stubB 1 行,本断言败。
    ledger = _ledger_rows(tmp_path)
    ledger_providers = sorted(r["provider"] for r in ledger)
    assert ledger_providers == ["soft", "stubB"], (
        f"_record_usage 应在 is_complete 判定前(SOFT 跳也记账),"
        f"实际 ledger: {ledger_providers}"
    )


# ──────────────────────────────────────────────────────────────────────────
# session_id 派生(S4.1)真进 cascade context
# ──────────────────────────────────────────────────────────────────────────


def _make_session_capturing_cascade(
    tmp_path: Path,
) -> tuple[Cascade, _RecordingStrategy]:
    """建带 _RecordingStrategy 的隔离 cascade,可读回 plan 时收到的 session_id。"""
    strat = _RecordingStrategy(["cap"])
    cascade = _make_isolated_cascade(
        tmp_path,
        [("cap", _StubProvider("cap", text="ok", model="cap-m"), "k1")],
        strategy=strat,
    )
    return cascade, strat


def test_session_id_explicit_x_session_id_header_takes_priority(patched_app, tmp_path):
    """X-Session-Id 显式 header → cascade context["session_id"] 收到该值,
    且优先于 Authorization Bearer(显式 > 派生)。"""
    app, install = patched_app
    cascade, strat = _make_session_capturing_cascade(tmp_path)
    install(cascade)

    client = TestClient(app)
    r = client.post(
        "/v1/chat/completions",
        json={"model": "any", "messages": [{"role": "user", "content": "x"}]},
        headers={
            "X-Session-Id": "explicit-session-42",
            "Authorization": "Bearer should-be-ignored",
        },
    )
    assert r.status_code == 200
    assert strat.captured == ["explicit-session-42"]


def test_session_id_derived_from_bearer_token_when_no_explicit(patched_app, tmp_path):
    """无 X-Session-Id,有 Bearer → session_id = blake2b(api_key) 派生(同 key 同桶,
    且不含原 key——守 security 防 log 泄漏)。"""
    app, install = patched_app
    cascade, strat = _make_session_capturing_cascade(tmp_path)
    install(cascade)

    client = TestClient(app)
    r = client.post(
        "/v1/chat/completions",
        json={"model": "any", "messages": [{"role": "user", "content": "x"}]},
        headers={"Authorization": "Bearer my-api-key"},
    )
    assert r.status_code == 200
    expected = derive_session_id("my-api-key", None)
    assert strat.captured == [expected]
    # 安全:派生串非原 key(不含 my-api-key 子串,防泄漏)
    assert expected is not None
    assert "my-api-key" not in expected


def test_session_id_none_when_no_header(patched_app, tmp_path):
    """既无 X-Session-Id 也无 Authorization → session_id=None(cascade 不判定灰度,
    plan 仍正常执行)。"""
    app, install = patched_app
    cascade, strat = _make_session_capturing_cascade(tmp_path)
    install(cascade)

    client = TestClient(app)
    r = client.post(
        "/v1/chat/completions",
        json={"model": "any", "messages": [{"role": "user", "content": "x"}]},
    )
    assert r.status_code == 200
    assert strat.captured == [None]


# ──────────────────────────────────────────────────────────────────────────
# token_ledger 写入(S2.4)
# ──────────────────────────────────────────────────────────────────────────


def test_ledger_records_when_provider_returns_usage(patched_app, tmp_path):
    """provider 返 Usage(prompt_tokens/completion_tokens/cost)→ ledger 写 1 行。"""
    app, install = patched_app
    usage = Usage(prompt_tokens=100, completion_tokens=50, cost=0.0012)
    cascade = _make_isolated_cascade(
        tmp_path,
        [("stubU", _StubProvider("stubU", text="ok", model="m-U", usage=usage), "kU")],
    )
    install(cascade)

    client = TestClient(app)
    r = client.post(
        "/v1/chat/completions",
        json={"model": "any", "messages": [{"role": "user", "content": "ping"}]},
    )
    assert r.status_code == 200

    rows = _ledger_rows(tmp_path)
    assert len(rows) == 1
    assert rows[0]["provider"] == "stubU"
    assert rows[0]["model"] == "m-U"
    assert rows[0]["prompt_tokens"] == 100
    assert rows[0]["completion_tokens"] == 50
    assert rows[0]["cost"] == pytest.approx(0.0012)


def test_ledger_skips_when_provider_returns_no_usage(patched_app, tmp_path):
    """provider 返 usage=None(mock/未报)→ ledger 不写(空表),cascade 仍成功。"""
    app, install = patched_app
    cascade = _make_isolated_cascade(
        tmp_path,
        [("stubX", _StubProvider("stubX", text="ok", model="m-X", usage=None), "kX")],
    )
    install(cascade)

    client = TestClient(app)
    r = client.post(
        "/v1/chat/completions",
        json={"model": "any", "messages": [{"role": "user", "content": "ping"}]},
    )
    assert r.status_code == 200
    assert _ledger_rows(tmp_path) == []


# ──────────────────────────────────────────────────────────────────────────
# 多请求隔离 + 双协议共享 cascade
# ──────────────────────────────────────────────────────────────────────────


def test_multiple_requests_each_get_distinct_correlation_id(patched_app, tmp_path):
    """N 请求各得独立 correlation_id(uuid.uuid4),N trace 行,每行 reason=initial。"""
    app, install = patched_app
    cascade = _make_isolated_cascade(
        tmp_path,
        [("stubM", _StubProvider("stubM", text="multi", model="m-M"), "kM")],
    )
    install(cascade)

    client = TestClient(app)
    for _ in range(3):
        r = client.post(
            "/v1/chat/completions",
            json={"model": "any", "messages": [{"role": "user", "content": "x"}]},
        )
        assert r.status_code == 200

    rows = _trace_rows(tmp_path)
    assert len(rows) == 3
    cids = {r["correlation_id"] for r in rows}
    assert len(cids) == 3, f"correlation_id 应互不相同,实际 {cids}"
    # 每行均首跳 initial(无 fallback)
    for r in rows:
        attr = parse_attribution(r["hop_attribution"])
        assert attr is not None and attr.reason == "initial"


def test_openai_and_anthropic_share_cascade_each_writes_own_trace(
    patched_app, tmp_path
):
    """OpenAI + Anthropic 共用模块级 _cascade,各请求独立 trace 行 + correlation_id。"""
    app, install = patched_app
    cascade = _make_isolated_cascade(
        tmp_path,
        [("stubS", _StubProvider("stubS", text="shared", model="m-S"), "kS")],
    )
    install(cascade)

    client = TestClient(app)
    r1 = client.post(
        "/v1/chat/completions",
        json={"model": "any", "messages": [{"role": "user", "content": "a"}]},
    )
    r2 = client.post(
        "/v1/messages",
        json={
            "model": "any",
            "messages": [{"role": "user", "content": "b"}],
            "max_tokens": 10,
        },
    )
    assert r1.status_code == 200 and r2.status_code == 200
    assert r1.json()["choices"][0]["message"]["content"] == "shared"
    assert r2.json()["content"][0]["text"] == "shared"

    rows = _trace_rows(tmp_path)
    assert len(rows) == 2
    cids = {r["correlation_id"] for r in rows}
    assert len(cids) == 2  # 两请求 correlation 不同
    providers = {r["provider"] for r in rows}
    assert providers == {"stubS"}  # 同候选,两次都走 stubS


# ──────────────────────────────────────────────────────────────────────────
# OpenCode 子片2 MED #1 闭合:_extract_prompt 真路径透传(防"prompt 永远是空串"假绿)
# ──────────────────────────────────────────────────────────────────────────


def test_extract_prompt_pipes_messages_to_provider(patched_app, tmp_path):
    """messages 经 _extract_prompt 真拍平送 provider.complete()。

    OpenCode 子片2 MED #1 闭合:之前测试用的 _StubProvider 不消费 prompt,所以即便
    _extract_prompt 改成 `return ""` 也全绿。本测试用 prompts 列表抓 provider 收到的
    串,验真传递。
    """
    app, install = patched_app
    captured: list[str] = []
    cascade = _make_isolated_cascade(
        tmp_path,
        [
            (
                "stubP",
                _StubProvider("stubP", text="ok", model="m-P", prompts=captured),
                "kP",
            )
        ],
    )
    install(cascade)

    client = TestClient(app)
    r = client.post(
        "/v1/chat/completions",
        json={
            "model": "any",
            "messages": [
                {"role": "system", "content": "be concise"},
                {"role": "user", "content": "ping-A"},
            ],
        },
    )
    assert r.status_code == 200
    # chat-protocol-passthrough:messages 结构透传(不拍平),provider 收到 list[dict] 保留 role
    assert captured == [
        [{"role": "system", "content": "be concise"}, {"role": "user", "content": "ping-A"}]
    ], f"messages 透传断裂:provider 实收 {captured!r}"


def test_extract_prompt_anthropic_path_also_pipes_messages(patched_app, tmp_path):
    """Anthropic 端点路径同样把 messages 拍平送 provider.complete()(对称验证)。"""
    app, install = patched_app
    captured: list[str] = []
    cascade = _make_isolated_cascade(
        tmp_path,
        [
            (
                "stubQ",
                _StubProvider("stubQ", text="ok", model="m-Q", prompts=captured),
                "kQ",
            )
        ],
    )
    install(cascade)

    client = TestClient(app)
    r = client.post(
        "/v1/messages",
        json={
            "model": "any",
            "messages": [{"role": "user", "content": "ping-B"}],
            "max_tokens": 10,
        },
    )
    assert r.status_code == 200
    # chat-protocol-passthrough:Anthropic 端点也用 messages 结构透传(不拍平)
    assert captured == [[{"role": "user", "content": "ping-B"}]]


# ──────────────────────────────────────────────────────────────────────────
# OpenCode 子片2 MED #2 部分:happy path sanity——证 endpoint 真把 cascade 结果传出
# (主体失败路径 defer 子片 3:compliance_blocked 真路径变化、no_candidates、budget_exhausted)
# ──────────────────────────────────────────────────────────────────────────


def test_compliance_blocked_returns_200_with_empty_content_sanity(patched_app, tmp_path):
    """compliance 拒路由 → cascade 早返 (None, None, False, 0, "compliance_blocked"),
    endpoint `result.final_text or ""` 静默返 200 + 空 content + model="mock"。

    OpenCode 子片2 MED #2 部分接受:本测试是 happy path 的负面 sanity——证当 cascade
    返回失败结果(success=False,final_text=None)时,endpoint 的响应塑形真把 None 转成
    空 content + 默认 model,而非异常 5xx 或泄漏 None。Phase1 "static 200"是已知设计
    模式(同 budget_exhausted/no_candidates/global_open,见子片 1 OpenCode #5 defer 注),
    错误响应塑形(把 success=False 映射 4xx/5xx)归未来切片。
    主体的失败路径(no_candidates 真触发 / budget_exhausted / 全 SOFT)defer 子片 3。
    """
    # 本测试自建 cascade(不通过 _make_isolated_cascade),为了配两个同 entity 不同
    # api_key_env 的 entry 触发 PolicyEnforcer.check() 的合规违规(同 provider 多账号)。
    app, install = patched_app
    entries = {
        "acct-a": ProviderEntry(
            name="acct-a", entity="paid-x", tier="fast",
            quota=1_000_000, cooldown_s=30, is_free=False, cost_multiplier=1.0,
            api_key_env="KEY_A",
        ),
        "acct-b": ProviderEntry(
            name="acct-b", entity="paid-x", tier="fast",
            quota=1_000_000, cooldown_s=30, is_free=False, cost_multiplier=1.0,
            api_key_env="KEY_B",
        ),
    }
    counter: dict[str, int] = {}
    candidates: list[tuple[str, Provider, str]] = [
        ("acct-a", _StubProvider("acct-a", text="should-not-arrive", model="m", counter=counter), "k-a"),
        ("acct-b", _StubProvider("acct-b", text="should-not-arrive", model="m", counter=counter), "k-b"),
    ]
    ledger = LedgerStore(tmp_path / "ledger.db")
    quotas = {n: 1_000_000 for n in entries}
    cascade = Cascade(
        store=TraceStore(tmp_path / "trace.db"),
        breaker=CircuitBreaker(tmp_path / "circuit.db"),
        strategy=_FixedOrderStrategy(["acct-a", "acct-b"]),
        candidates=candidates,
        health_store=HealthStore(tmp_path / "health.db"),
        policy_enforcer=PolicyEnforcer(entries.values()),  # 多账号 → ComplianceError
        ledger=ledger,
        cost_gate=CostGate(ledger, quotas),
    )
    install(cascade)

    client = TestClient(app)
    r = client.post(
        "/v1/chat/completions",
        json={"model": "any", "messages": [{"role": "user", "content": "x"}]},
    )
    # 静默 200(Phase1 已知设计):final_text=None → `or ""` → ""
    assert r.status_code == 200
    body = r.json()
    assert body["choices"][0]["message"]["content"] == ""
    assert body["model"] == "mock"  # final_model=None → `or "mock"`
    # 防假绿:provider 必须**未**被调过(合规拒路由,不 init store/不 plan/不 complete)
    assert counter == {}, "compliance_blocked 路径不应触达任何 provider"
    # trace 应空(cascade.run 在 _ensure_store 前早返,未 acquire)。
    # 注:_ensure_store 在 compliance check **之后**才调,所以 trace.db 表可能未建。
    try:
        rows = _trace_rows(tmp_path)
        assert rows == [], f"compliance_blocked 不应写 trace,实际 {rows}"
    except sqlite3.OperationalError:
        # trace 表未建(_ensure_store 未跑过)→ 同样证明合规门早返,可接受。
        pass
