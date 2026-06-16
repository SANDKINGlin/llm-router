"""FastAPI 应用:双协议入口 + /healthz。

Phase1 (S2.1b):/v1/chat/completions(OpenAI)+ /v1/messages(Anthropic)
经 Cascade(④ 回退编排)打到候选 provider 链。Phase1 候选只有 mock(router-policy.yaml),
S2.x 接真 provider 时由 Scanner(S2.3)按 entry.base_url/api_key_env 建真 adapter 填候选。
门卫/匹配/路由/熔断在 Cascade 内串起(store+breaker+hop+完整性+strategy.plan)。
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .api.cascade import Cascade
from .api.epsilon_greedy import EpsilonGreedy
from .config import policy
from .providers.mock import MockProvider
from .resilience.circuit_breaker import CircuitBreaker
from .scanner.mnfst import build_adapters, load_manifest
from .store.trace import TraceStore

app = FastAPI(title="llm-router", version="0.0.1")

_DATA_DIR = Path(__file__).resolve().parents[2] / "data"


def _build_cascade() -> Cascade:
    """构造生产 Cascade(模块级单例):mock 兜底 + 真 provider(配了 key 的)入候选池。

    候选池:
      - MockProvider(router-policy.yaml 的 mock 条目,始终在池,test_health 守绿 + 兜底)
      - 真 OpenAIProvider(mnfst 清单里 api_key_env 在环境有值的 entry,S2.3 真集成)
    缺 key 的真 provider 自动跳过(不崩,可渐进接入);key 安全:breaker account_key 用 env 名。
    策略 entries 合并 policy + manifest(后者补 tier/is_free/cost 给排序键 + matcher)。
    """
    pol = policy()
    manifest_entries = load_manifest()
    # entries map:policy(mock)+ manifest(真),供 EpsilonGreedy 排序键 + TierMatcher 用。
    entries = {e.name: e for e in (*pol.providers, *manifest_entries)}

    candidates: list = [(e.name, MockProvider(), e.name) for e in pol.providers]
    candidates.extend(build_adapters(manifest_entries))  # 真 adapter(配了 key 的)

    return Cascade(
        store=TraceStore(_DATA_DIR / "trace.db"),
        breaker=CircuitBreaker(_DATA_DIR / "circuit.db"),
        strategy=EpsilonGreedy(entries),
        candidates=candidates,
    )


_cascade = _build_cascade()


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
async def openai_chat(req: _OpenAIRequest) -> dict:
    """OpenAI 协议入口。经 Cascade(④)回退编排打到候选 provider 链。Roo/Codex 走这个。

    S2.1b:接 Cascade(prompt → strategy.plan 链 → 逐跳 complete + 熔断/完整性/幂等/hop)。
    """
    result = await _cascade.run(
        _extract_prompt(req.messages), correlation_id=uuid.uuid4().hex
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
async def anthropic_messages(req: _AnthropicRequest) -> dict:
    """Anthropic 协议入口。经 Cascade(④)回退编排。CC 走这个。

    S2.1b:接 Cascade。
    """
    result = await _cascade.run(
        _extract_prompt(req.messages), correlation_id=uuid.uuid4().hex
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
