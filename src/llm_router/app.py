"""FastAPI 应用:双协议入口 + /healthz。

Phase1 (S0.0 骨架):/v1/chat/completions(OpenAI)+ /v1/messages(Anthropic)
都打到 MockProvider(不调真 API)。真实 provider 接入在 S2.x+;
门卫/匹配/路由/回退/熔断在 S1.x / S2.x 切片补。
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .providers.mock import MockProvider

app = FastAPI(title="llm-router", version="0.0.1")
_provider = MockProvider()


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
    """拍平 messages 成一个 prompt 串给 MockProvider。"""
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
    """OpenAI 协议入口(S0.0 → MockProvider)。Roo/Codex 走这个。

    S2.1a:async def + await(Provider.complete 翻 async,为 S2.1b 真 adapter 铺路)。
    """
    text, model = await _provider.complete(_extract_prompt(req.messages))
    return {
        "id": "chatcmpl-mock",
        "object": "chat.completion",
        "created": int(datetime.now(timezone.utc).timestamp()),
        "model": model,
        "choices": [
            {"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}
        ],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


@app.post("/v1/messages")
async def anthropic_messages(req: _AnthropicRequest) -> dict:
    """Anthropic 协议入口(S0.0 → MockProvider)。CC 走这个。

    S2.1a:async def + await(Provider.complete 翻 async)。
    """
    text, model = await _provider.complete(_extract_prompt(req.messages))
    return {
        "id": "msg_mock",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": [{"type": "text", "text": text}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 0, "output_tokens": 0},
    }
