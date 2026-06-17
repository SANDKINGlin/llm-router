"""FastAPI 应用:双协议入口 + /healthz。

Phase1 (S2.1b):/v1/chat/completions(OpenAI)+ /v1/messages(Anthropic)
经 Cascade(④ 回退编排)打到候选 provider 链。Phase1 候选只有 mock(router-policy.yaml),
S2.x 接真 provider 时由 Scanner(S2.3)按 entry.base_url/api_key_env 建真 adapter 填候选。
门卫/匹配/路由/熔断在 Cascade 内串起(store+breaker+hop+完整性+strategy.plan)。
"""
from __future__ import annotations

import asyncio
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .api.cascade import Cascade
from .api.cost_gate import CostGate
from .api.epsilon_greedy import EpsilonGreedy
from .api.gray import derive_session_id
from .api.policy_enforcer import PolicyEnforcer
from .config import policy
from .health.probe import HealthProber
from .providers.base import Provider
from .providers.mock import MockProvider
from .resilience.circuit_breaker import CircuitBreaker
from .scanner.mnfst import build_adapters, load_manifest
from .store.health_store import HealthStore
from .store.token_ledger import LedgerStore
from .store.trace import TraceStore

_DATA_DIR = Path(__file__).resolve().parents[2] / "data"

def _build_cascade() -> Cascade:
    """构造生产 Cascade(模块级单例):mock 兜底 + 真 provider(配了 key 的)入候选池。

    候选池:
      - 真 OpenAIProvider(mnfst 清单里 api_key_env 在环境有值的 entry,**在前**,S2.3 真集成)
      - MockProvider(router-policy.yaml 的 mock 条目,**最后兜底**——真 provider 全失败才用)
    顺序关键(修 B1 mock 支配):mock 与真 provider 排序键全平局(is_free/cost 相同)时,
    plan() 稳定排序保持插入序 → 真 provider 必须在前才会被优先调用;否则 mock 链首即成功,
    真 provider 形同虚设。无 key 时 candidates=[mock] → mock 正常(test_health 守绿)。

    **每次调用读最新 manifest + env**(load_manifest/build_adapters 在内),供
    test_app_build_cascade_orders_real_before_mock 注入临时 manifest 后验证顺序。
    S2.8c:注入共享 HealthStore(data/health.db)——Cascade 路由前 hard-skip 死亡 key(Face 2),
    lifespan 起探活循环写它 + 喂 CB(Face 1/3)。Cascade 不 init(fail-open 读),lifespan init。
    """
    pol = policy()
    manifest_entries = load_manifest()
    # entries map:policy(mock)+ manifest(真),供 EpsilonGreedy 排序键 + TierMatcher 用。
    entries = {e.name: e for e in (*pol.providers, *manifest_entries)}

    real_adapters = build_adapters(manifest_entries)  # 配了 key 的真 adapter
    mock_candidates = [(e.name, MockProvider(), e.name) for e in pol.providers]
    # 真 provider 在前,mock 最后兜底(修 B1)。
    candidates: list = [*real_adapters, *mock_candidates]

    # S2.7 合规门卫:候选 entries(含 mock)→ 别名归一化 + 同 provider 多账号检测。
    # Phase1 mock-only(entity=mock,无 api_key_env)→ 合规 → 门卫放行;配了同实体多 key 才拦。
    enforcer = PolicyEnforcer(entries.values())

    # S2.4 Cost Budget Gate:共享 ledger(Cascade writer + CostGate reader 同一实例)+
    # quotas 从 entries 取(ProviderEntry.quota,token 上限)。mock quota=1000000 → 永不超预算兜底。
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


_cascade = _build_cascade()

# S2.8c 探活目标:真 provider(排 Mock——mock 探活恒活无信号)。模块级算一次(import 期,
# 与 _cascade 同读一次 manifest/env,一致)。spec Req 1 ping 全部 fallback/paid key;
# Phase1 provider 少,全 ping(不取"前 2",YAGNI;key 多时再限)。
_probe_targets: list[tuple[str, Provider]] = [
    (name, provider) for name, provider, _key in build_adapters(load_manifest())
]


def _make_lifespan(
    cascade: Cascade,
    probe_targets: list[tuple[str, Provider]],
    *,
    interval_seconds: float = 300.0,
    probe_timeout_seconds: float = 10.0,
):
    """S2.8c Face 1:构造 FastAPI lifespan——startup 起探活循环,shutdown 停。

    抽成工厂(非模块级闭包)以便单测注入 tmp cascade/targets 确定性验证 task 生命周期
    (不依赖 TestClient 是否跑 lifespan)。startup:init 共享 health_store + create_task
    prober.run_loop(stop)(on_alive=cascade.feed_probe_success 喂 HALF_OPEN,Face 3);
    **仅当有探活目标才起 task**(无真 key → 空转无意义)。shutdown:stop_event.set + cancel
    task + store.close。
    """

    @asynccontextmanager
    async def _lifespan(app: FastAPI):
        store = cascade.health_store
        if store is not None:
            await store.init()
        stop_event = asyncio.Event()
        task = None
        if probe_targets:
            prober = HealthProber(
                store,
                probe_targets,
                interval_seconds=interval_seconds,
                probe_timeout_seconds=probe_timeout_seconds,
                on_alive=cascade.feed_probe_success,
            )
            task = asyncio.create_task(prober.run_loop(stop_event))
        app.state.probe_stop = stop_event
        app.state.probe_task = task
        try:
            yield
        finally:
            stop_event.set()
            if task is not None:
                # 优雅退出:stop_event 让 run_loop 在下个循环检查点退出(probe.py #3 设计)。
                # **不 task.cancel()**——避免在 record_probe 的 DB 写中途注入 CancelledError
                # (probe.py #3 对抗审结论)。await task 等其退出:sleep 期被 wait_for(stop_event.wait())
                # 即时唤醒,最坏等完一个 in-flight tick(≤ probe_timeout × providers,Phase1 秒级)。
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            if store is not None:
                await store.close()

    return _lifespan


app = FastAPI(
    title="llm-router", version="0.0.1", lifespan=_make_lifespan(_cascade, _probe_targets)
)


class _Message(BaseModel):
    role: str = "user"
    content: str | list | None = None


class _OpenAIRequest(BaseModel):
    model: str = "mock"
    messages: list[_Message] = Field(default_factory=list)
    # stream/tools/temperature 等:S2.x 接真 provider 时处理


class _AnthropicRequest(BaseModel):
    model: str = "mock"
    messages: list[_Message] = Field(default_factory=list)
    max_tokens: int | None = None


def _extract_prompt(messages: list[_Message]) -> str:
    """拍平 messages 成一个 prompt 串给 Cascade。"""
    parts = []
    for m in messages:
        c = m.content if isinstance(m.content, str) else str(m.content)
        parts.append(f"{m.role}: {c}")
    return "\n".join(parts) or "ping"


def _extract_session_id(request: Request) -> str | None:
    """S4.1:从请求派生 session_id(D9 灰度切 agent,design line 25/128)。

    优先级:X-Session-Id header(显式)> Authorization Bearer key 派生 > None。
    api_key 派生 = blake2b(key) → 同 key 同桶 = 天然按 agent 灰度(三 agent 各自 key 不同桶)。
    空串等同缺失。两者皆无 → None(Cascade 视为不参与灰度判定,不 log)。
    """
    explicit = request.headers.get("x-session-id") or None
    auth = request.headers.get("authorization", "")
    api_key: str | None = None
    # Bearer 解析:split(None) 容忍 SP/HTAB 分隔(RFC 用 SP,实践有 tab;OpenCode LOW#2)。
    parts = auth.split(None, 1)
    if len(parts) == 2 and parts[0].lower() == "bearer":
        api_key = parts[1].strip() or None
    return derive_session_id(api_key, explicit)


@app.get("/healthz")
def healthz() -> JSONResponse:
    """就绪探针。S0.0:返 200。readiness 切片补三库可写+policy加载+CB恢复。"""
    from .readiness import check_ready

    ok, detail = check_ready()
    return JSONResponse(
        {"status": "ok" if ok else "not_ready", "detail": detail},
        status_code=200 if ok else 503,
    )


@app.post("/v1/chat/completions")
async def openai_chat(req: _OpenAIRequest, request: Request) -> dict:
    """OpenAI 协议入口。经 Cascade(④)回退编排打到候选 provider 链。Roo/Codex 走这个。

    S2.1b:接 Cascade(prompt → strategy.plan 链 → 逐跳 complete + 熔断/完整性/幂等/hop)。
    S4.1:从 X-Session-Id / Authorization 派生 session_id 传 Cascade(灰度判定,可观测)。
    """
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
        "choices": [
            {"index": 0, "message": {"role": "assistant", "content": result.final_text or ""}, "finish_reason": "stop"}
        ],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


@app.post("/v1/messages")
async def anthropic_messages(req: _AnthropicRequest, request: Request) -> dict:
    """Anthropic 协议入口。经 Cascade(④)回退编排。CC 走这个。

    S2.1b:接 Cascade。S4.1:从 X-Session-Id / Authorization 派生 session_id 传 Cascade。
    """
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
