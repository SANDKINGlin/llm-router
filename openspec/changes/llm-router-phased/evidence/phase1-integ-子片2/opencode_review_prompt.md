# OpenCode 异构对抗审查 — Phase1 集成验证·子片 2(自包含 prompt v1)

> 你是异构对抗审查者。任务:用 HERMES 标签协议审查测试文件,对抗式找漏("假绿"、覆盖盲区、逻辑矛盾),不是夸;最后必须给可执行结论。

## ⚠️ 自包含约束(必须遵守)

**所有需要的源码已嵌入下方 §源码锚点**。**禁止 Read/Glob/Grep 其他源文件**。仅基于本文件的内容审查并直接输出发现。如确实需要某个未嵌入的细节,**写在 [DEADLOCK]** 里说明缺哪段,不要去探查。直接 Read 其他源文件会浪费 timeout(子片 1 v1 prompt 用相对锚点导致模型反查 → 480s 超时被杀,v2 自包含才完成,本审继承 v2 教训)。

## HERMES 标签协议(每条发现必须用)

- `[CHALLENGE] <严重度> <file:line> <问题> <可复现反例>`("如果改成 X 输入,测试应败但会绿")
- `[CONSENSUS]` 维度收敛认可,无 CHALLENGE
- `[DEADLOCK]` 多轮无法收敛 / 缺关键源码

严重度:CRITICAL(出厂门假绿)/ HIGH(关键场景遗漏)/ MED(强化建议)/ LOW(风格)。

**整文件结论**单独一行(最后):
- `[CONSENSUS] 子片 2 测试可作 Phase 1 出厂回归门`(无 CRITICAL/HIGH)
- `[CHALLENGE] 子片 2 测试存在 N 项 CRITICAL/HIGH,需修复后重审`

## 项目背景

智能路由层 Phase 1(`~/projects/llm-router`)。**Phase 1 集成验证**拆 4 子片:
- **子片 1(已 done + OpenCode 审过)**:4 CRITICAL BUG 端到端回归门(组件层全开 Cascade 直 .run())
- **子片 2(本审)**:**端到端 happy path 经 FastAPI 协议入口**(TestClient + 模块级 _cascade)
- **子片 3(待开)**:BUG 跨场景交互 + S2.4 defer 3 盲区 + 子片 1 OpenCode 4 项 defer 收口
- **子片 4(待开)**:压测(模拟 429 burst,验 fallback 链不雪崩)

子片 2 与子片 1 互补:子片 1 直接组装 Cascade .run(),子片 2 走 FastAPI 真协议(/v1/chat/completions、/v1/messages)→ 验 endpoint 层(请求解析、session_id 派生、响应塑形)与 cascade 集成层(trace+ledger 真写、SOFT_CONTENT 切换真路径)。

## 子片 2 测试设计意图

| 现有测试 | 与子片 2 区别 |
|---|---|
| `tests/test_health.py` | /healthz 平凡 smoke,不验 cascade 集成、不验持久化 |
| `tests/integration/test_phase1_critical_bugs.py`(子片 1)| 直接 cascade.run(),不经 FastAPI |
| `tests/e2e/test_fallback_e2e.py` | orchestrator 测试 helper,**非生产 Cascade** |
| `tests/unit/test_app_lifespan.py` | 只测 lifespan,不打请求 |
| **子片 2(本文件)** | **TestClient → FastAPI endpoint → 模块级 _cascade(monkeypatch 注入)→ 真 SQLite 持久化** |

## 审重点(必查 5 项 + 任意自由发掘)

1. **覆盖度**:10 测试是否覆盖所有交班要求?(双协议 happy path 各 1 / SOFT_CONTENT 切换 / session_id 派生 3 路 / ledger 写入 2 / 多请求隔离 + 双协议共享)
2. **模块级单例 monkeypatch 是否真生效**:`monkeypatch.setattr(app_mod, "_cascade", cascade)` 后,endpoint handlers 通过模块 globals 解析 _cascade 该值。如果 patch 失败(继续用 production _cascade)而测试断言仍能过,这是假绿。如何反驳?
3. **TestClient 不 with → lifespan 不触发**:这个假设是否站得住?如果某天 starlette 改成 TestClient 默认触发 lifespan(即便不 with),测试会污染 production data/*.db。能否提供反例:本批测试若 lifespan 真触发,会发生什么?是否仍能假绿通过?
4. **SOFT_CONTENT 反例**:`_SoftProvider.complete` 返 `("", "soft-model", None)` → `is_complete("", "soft-model")` 返 False 触发 SOFT_CONTENT。如果 cascade.py 把 SOFT_CONTENT 处理改错(比如改成"soft 也算成功直接返"),本测试是否真能抓到?**反例必须可执行**:写出"把 cascade.py 的 X 改成 Y,测试应败但会绿"。
5. **session_id 派生**:`_RecordingStrategy.captured` 是否被 cascade.run() 真填?如果 endpoint 解析 session_id 抽错(比如永远返 None),`test_session_id_explicit_x_session_id_header_takes_priority` 会假绿吗?

**额外要求**:
- 反例必须可执行(不要只说"可能"),格式:"如果把 X.py 的 Y 改成 Z,本测试应败但会绿"
- 引用必须 `file:line`(不要"某个地方")
- 嵌入文件外的源码请写 [DEADLOCK] 说明缺哪段,**禁止 Read**

---

## §源码锚点 ① — 测试文件全文 `tests/integration/test_phase1_happy_path.py`(本审目标)

```python
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
from llm_router.providers.base import Provider, Usage
from llm_router.resilience.circuit_breaker import CircuitBreaker
from llm_router.routing.hop import parse_attribution
from llm_router.store.health_store import HealthStore
from llm_router.store.token_ledger import LedgerStore
from llm_router.store.trace import TraceStore


class _StubProvider(Provider):
    def __init__(self, name, *, text="ok-response", model="stub-model", usage=None, counter=None):
        self.name = name
        self._text = text
        self._model = model
        self._usage = usage
        self._counter = counter

    async def complete(self, prompt):
        if self._counter is not None:
            self._counter[self.name] = self._counter.get(self.name, 0) + 1
        return self._text, self._model, self._usage


class _SoftProvider(Provider):
    def __init__(self, name="soft", *, counter=None):
        self.name = name
        self._counter = counter

    async def complete(self, prompt):
        if self._counter is not None:
            self._counter[self.name] = self._counter.get(self.name, 0) + 1
        return "", "soft-model", None  # text="" → is_complete False


class _FixedOrderStrategy(RoutingStrategy):
    def __init__(self, order):
        self._order = list(order)

    def plan(self, candidates, context):
        seen = set(candidates)
        return [c for c in self._order if c in seen]


class _RecordingStrategy(RoutingStrategy):
    def __init__(self, order):
        self._order = list(order)
        self.captured = []

    def plan(self, candidates, context):
        self.captured.append(context.get("session_id"))
        seen = set(candidates)
        return [c for c in self._order if c in seen]


def _entry(name):
    return ProviderEntry(
        name=name, tier="fast", quota=1_000_000, cooldown_s=30,
        is_free=True, cost_multiplier=0.0,
    )


def _make_isolated_cascade(tmp_path, candidates, *, strategy=None):
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
def patched_app(monkeypatch):
    from llm_router import app as app_mod

    def install(cascade):
        monkeypatch.setattr(app_mod, "_cascade", cascade)

    return app_mod.app, install


def _trace_rows(tmp_path):
    conn = sqlite3.connect(tmp_path / "trace.db")
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM trace ORDER BY idempotency_key").fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _ledger_rows(tmp_path):
    conn = sqlite3.connect(tmp_path / "ledger.db")
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM token_ledger ORDER BY ledger_id").fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def test_openai_endpoint_e2e_happy_path_writes_trace(patched_app, tmp_path):
    app, install = patched_app
    counter = {}
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
    app, install = patched_app
    counter = {}
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


def test_soft_content_falls_back_to_next_provider_with_correct_hop_chain(patched_app, tmp_path):
    app, install = patched_app
    counter = {}
    candidates = [
        ("soft", _SoftProvider("soft", counter=counter), "k1"),
        ("stubB", _StubProvider("stubB", text="recovered-text", model="m-B", counter=counter), "k2"),
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
    assert counter == {"soft": 1, "stubB": 1}, f"call counter mismatch: {counter}"

    rows = _trace_rows(tmp_path)
    assert len(rows) == 2
    soft_row, stubB_row = rows[0], rows[1]
    assert soft_row["provider"] == "soft"
    assert stubB_row["provider"] == "stubB"
    assert soft_row["result"] == "" and stubB_row["result"] == "recovered-text"

    soft_attr = parse_attribution(soft_row["hop_attribution"])
    stubB_attr = parse_attribution(stubB_row["hop_attribution"])
    assert soft_attr is not None and stubB_attr is not None
    assert soft_attr.depth == 0 and soft_attr.reason == "initial"
    assert soft_attr.from_provider is None and soft_attr.to_provider == "soft"
    assert stubB_attr.depth == 1 and stubB_attr.reason == "soft_content"
    assert stubB_attr.from_provider == "soft" and stubB_attr.to_provider == "stubB"


def _make_session_capturing_cascade(tmp_path):
    strat = _RecordingStrategy(["cap"])
    cascade = _make_isolated_cascade(
        tmp_path,
        [("cap", _StubProvider("cap", text="ok", model="cap-m"), "k1")],
        strategy=strat,
    )
    return cascade, strat


def test_session_id_explicit_x_session_id_header_takes_priority(patched_app, tmp_path):
    app, install = patched_app
    cascade, strat = _make_session_capturing_cascade(tmp_path)
    install(cascade)

    client = TestClient(app)
    r = client.post(
        "/v1/chat/completions",
        json={"model": "any", "messages": [{"role": "user", "content": "x"}]},
        headers={"X-Session-Id": "explicit-session-42", "Authorization": "Bearer should-be-ignored"},
    )
    assert r.status_code == 200
    assert strat.captured == ["explicit-session-42"]


def test_session_id_derived_from_bearer_token_when_no_explicit(patched_app, tmp_path):
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
    assert expected is not None
    assert "my-api-key" not in expected


def test_session_id_none_when_no_header(patched_app, tmp_path):
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


def test_ledger_records_when_provider_returns_usage(patched_app, tmp_path):
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


def test_multiple_requests_each_get_distinct_correlation_id(patched_app, tmp_path):
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
    assert len(cids) == 3
    for r in rows:
        attr = parse_attribution(r["hop_attribution"])
        assert attr is not None and attr.reason == "initial"


def test_openai_and_anthropic_share_cascade_each_writes_own_trace(patched_app, tmp_path):
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
        json={"model": "any", "messages": [{"role": "user", "content": "b"}], "max_tokens": 10},
    )
    assert r1.status_code == 200 and r2.status_code == 200
    assert r1.json()["choices"][0]["message"]["content"] == "shared"
    assert r2.json()["content"][0]["text"] == "shared"

    rows = _trace_rows(tmp_path)
    assert len(rows) == 2
    cids = {r["correlation_id"] for r in rows}
    assert len(cids) == 2
    providers = {r["provider"] for r in rows}
    assert providers == {"stubS"}
```

---

## §源码锚点 ② — `src/llm_router/app.py:35-250`(FastAPI app + 双协议端点 + session_id 抽取)

```python
_DATA_DIR = Path(__file__).resolve().parents[2] / "data"

def _build_cascade() -> Cascade:
    pol = policy()
    manifest_entries = load_manifest()
    entries = {e.name: e for e in (*pol.providers, *manifest_entries)}
    real_adapters = build_adapters(manifest_entries)
    mock_candidates = [(e.name, MockProvider(), e.name) for e in pol.providers]
    candidates: list = [*real_adapters, *mock_candidates]
    enforcer = PolicyEnforcer(entries.values())
    ledger = LedgerStore(_DATA_DIR / "ledger.db")
    quotas = {e.name: e.quota for e in entries.values()}
    cost_gate = CostGate(ledger, quotas)
    return Cascade(
        store=TraceStore(_DATA_DIR / "trace.db"),
        breaker=CircuitBreaker(_DATA_DIR / "circuit.db"),
        strategy=EpsilonGreedy(entries),
        candidates=candidates,
        health_store=HealthStore(_DATA_DIR / "health.db"),
        policy_enforcer=enforcer,
        ledger=ledger,
        cost_gate=cost_gate,
    )


_cascade = _build_cascade()  # 模块级单例

_probe_targets = [(name, provider) for name, provider, _key in build_adapters(load_manifest())]


def _make_lifespan(cascade, probe_targets, *, interval_seconds=300.0, probe_timeout_seconds=10.0):
    @asynccontextmanager
    async def _lifespan(app):
        store = cascade.health_store
        if store is not None:
            await store.init()
        stop_event = asyncio.Event()
        task = None
        if probe_targets:
            prober = HealthProber(store, probe_targets, ...)
            task = asyncio.create_task(prober.run_loop(stop_event))
        app.state.probe_stop = stop_event
        app.state.probe_task = task
        try:
            yield
        finally:
            stop_event.set()
            if task is not None:
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            if store is not None:
                await store.close()
    return _lifespan


app = FastAPI(title="llm-router", version="0.0.1", lifespan=_make_lifespan(_cascade, _probe_targets))


def _extract_prompt(messages):
    parts = []
    for m in messages:
        c = m.content if isinstance(m.content, str) else str(m.content)
        parts.append(f"{m.role}: {c}")
    return "\n".join(parts) or "ping"


def _extract_session_id(request):
    """优先级:X-Session-Id header > Authorization Bearer > None。
    清洗换行防 log 注入(OpenCode 子片 1 LOW#1);Bearer split(None) 容忍 SP/HTAB。
    Bearer 派生 = blake2b(api_key) 同 key 同桶,不含原 key(防泄漏)。
    """
    explicit = request.headers.get("x-session-id") or None
    auth = request.headers.get("authorization", "")
    api_key = None
    parts = auth.split(None, 1)
    if len(parts) == 2 and parts[0].lower() == "bearer":
        api_key = parts[1].strip() or None
    return derive_session_id(api_key, explicit)


@app.post("/v1/chat/completions")
async def openai_chat(req, request):
    result = await _cascade.run(
        _extract_prompt(req.messages),
        correlation_id=uuid.uuid4().hex,
        session_id=_extract_session_id(request),
    )
    return {
        "id": "chatcmpl-mock",
        "object": "chat.completion",
        "created": int(datetime.now(timezone.utc).timestamp()),
        "model": result.final_model or "mock",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": result.final_text or ""}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


@app.post("/v1/messages")
async def anthropic_messages(req, request):
    result = await _cascade.run(
        _extract_prompt(req.messages),
        correlation_id=uuid.uuid4().hex,
        session_id=_extract_session_id(request),
    )
    return {
        "id": "msg_mock",
        "type": "message",
        "role": "assistant",
        "model": result.final_model or "mock",
        "content": [{"type": "text", "text": result.final_text or ""}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 0, "output_tokens": 0},
    }
```

---

## §源码锚点 ③ — `src/llm_router/api/cascade.py:220-396`(Cascade.run 逐跳骨架)

```python
async def run(self, prompt, *, correlation_id, session_id=None):
    # ① 合规门(最先,layer ①)
    if self._policy_enforcer is not None:
        try:
            self._policy_enforcer.check()
        except ComplianceError:
            return CascadeResult(None, None, False, 0, "compliance_blocked")
    await self._ensure_store()
    # ② 路由前 health/cost 过滤
    survivors = await self._surviving_candidates()
    if not survivors:
        return CascadeResult(None, None, False, 0, "no_candidates")
    # ③ session_id 进 context(灰度判定 log)
    context = {"session_id": session_id}
    if session_id is not None:
        pol = policy()
        released = gray_release(session_id, pol.gray_percent)
        context["gray_released"] = released
    # ④ strategy.plan 排链
    chain = self._strategy.plan(survivors, context)
    parent_trace_id = None
    prev_provider = None
    last_reason = "initial"
    attempted = 0
    for idx, name in enumerate(chain):
        # budget 门
        if idx > 0 and not check_hop_budget(idx, self._budget):
            ...  # 写 budget_exhausted 终态返
        # 本跳归因
        attr = (initial_attribution(name) if idx == 0 else advance(idx - 1, last_reason, prev_provider, name))
        # acquire trace
        out = await self._store.acquire(correlation_id=correlation_id, idempotency_key=f"{correlation_id}#{idx}", provider=name, parent_correlation_id=parent_trace_id)
        if out.status is AcquireStatus.REPLAYED:
            return CascadeResult(out.cached_result, None, True, attempted, "replayed")
        attempted += 1
        try:
            provider, key = self._providers[name]
        except KeyError:
            ...  # provider_removed_during_rollback
        # breaker 判定
        dec = self._breaker.allow_request(name, key)
        if not dec.allowed:
            await self._store.commit(trace_id=out.trace_id, result="", hop_attribution=attr.to_json())
            last_reason = dec.reason
            prev_provider = name
            parent_trace_id = out.trace_id
            continue
        # provider.complete
        try:
            text, model, usage = await provider.complete(prompt)
        except ProviderError:
            self._breaker.record_failure(name, key, TripReason.HARD)
            await self._store.commit(trace_id=out.trace_id, result="", hop_attribution=attr.to_json())
            last_reason = "hard_failure"
            prev_provider = name
            parent_trace_id = out.trace_id
            continue
        # token 用量记账(best-effort,先于完整性判定)
        await self._record_usage(name, model, usage)
        # 内容完整性
        if not is_complete(text, model):
            self._breaker.record_failure(name, key, TripReason.SOFT_CONTENT)
            await self._store.commit(trace_id=out.trace_id, result="", hop_attribution=attr.to_json())
            last_reason = "soft_content"
            prev_provider = name
            parent_trace_id = out.trace_id
            continue
        # 成功 → record_success + commit + return
        self._breaker.record_success(name, key)
        await self._store.commit(trace_id=out.trace_id, result=text, hop_attribution=attr.to_json())
        return CascadeResult(text, model, True, attempted, attr.reason)
    return CascadeResult(None, None, False, attempted, last_reason)
```

---

## §源码锚点 ④ — 关键辅助签名

```python
# src/llm_router/api/gray.py
def derive_session_id(api_key: str | None, explicit: str | None = None) -> str | None:
    """X-Session-Id explicit 优先,Bearer 派生其次,均无返 None。
    explicit 清洗 \\r\\n 防 log 注入;api_key 派生 = blake2b(key, digest_size=16).hexdigest()。
    """
    if explicit:
        return explicit.replace("\\r", "").replace("\\n", "")
    if api_key:
        return hashlib.blake2b(api_key.encode("utf-8"), digest_size=16).hexdigest()
    return None


# src/llm_router/resilience/content_integrity.py
def is_complete(text: Optional[str], model: Optional[str]) -> bool:
    """text/model 任一 None/非 str/strip 后空 → 残缺(SOFT_CONTENT)。"""
    if text is None or model is None:
        return False
    if not isinstance(text, str) or not isinstance(model, str):
        return False
    return text.strip() != "" and model.strip() != ""


# src/llm_router/routing/hop.py
DEFAULT_RETRY_BUDGET = 6
@dataclass(frozen=True)
class HopAttribution:
    depth: int
    reason: str
    from_provider: Optional[str]
    to_provider: Optional[str]


# src/llm_router/providers/base.py
@dataclass(frozen=True)
class Usage:
    prompt_tokens: int
    completion_tokens: int
    cost: Optional[float] = None


# src/llm_router/store/trace.py(schema 关键字段)
CREATE TABLE trace (
    trace_id TEXT PRIMARY KEY,
    correlation_id TEXT NOT NULL,
    parent_correlation_id TEXT,
    idempotency_key TEXT NOT NULL UNIQUE,
    provider TEXT NOT NULL,
    result TEXT,
    hop_attribution TEXT,
    created_at TEXT NOT NULL,
    ...
)


# src/llm_router/store/token_ledger.py(schema)
CREATE TABLE token_ledger (
    ledger_id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    prompt_tokens INTEGER NOT NULL,
    completion_tokens INTEGER NOT NULL,
    cost REAL,
    timestamp TEXT NOT NULL
)
```

---

## 输出格式要求

```
# OpenCode 异构对抗审 — 子片 2

## 维度 1:覆盖度
[标签] 严重度 file:line 内容
...

## 维度 2:模块级 _cascade monkeypatch 是否真生效
...

## 维度 3:TestClient/lifespan 假设
...

## 维度 4:SOFT_CONTENT 反例
...

## 维度 5:session_id 派生反例
...

## 自由发掘(其他维度)
...

---

[CONSENSUS]/[CHALLENGE] 整文件结论(单独一行,最后)
```
