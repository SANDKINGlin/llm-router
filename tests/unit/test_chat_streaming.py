"""chat-stream-true-streaming · 真流式 SSE(ponytail 版)。

最小验证:OpenAIProvider.complete_stream 逐 chunk yield SDK chunk;
MockProvider.complete_stream fake 流;端点 stream=true 真流式 pipe。
非 stream 分支向后兼容(已有测试覆盖,不重复)。
"""
from __future__ import annotations

import asyncio
import json

import httpx
import respx

from llm_router.providers.base import ProviderError
from llm_router.providers.mock import MockProvider
from llm_router.providers.openai import OpenAIProvider

_BASE = "https://test.openai.invalid/v1"
_URL = f"{_BASE}/chat/completions"


def _run(coro):
    return asyncio.run(coro)


def _msg(role, content):
    return {"role": role, "content": content}


# ── OpenAIProvider.complete_stream ──────────────────────────────────────


class TestOpenAIProviderStream:
    @respx.mock
    def test_yields_chunks_from_sdk_stream(self):
        """SDK stream=True 的多 chunk → complete_stream 逐个 yield OpenAI SSE dict。"""
        # 模拟 SDK 流式响应(respx 拦截,SDK 收到 SSE 解析为 chunks)
        sse = (
            'data: {"id":"x","object":"chat.completion.chunk","created":1,"model":"m","choices":[{"index":0,"delta":{"role":"assistant","content":"hel"},"finish_reason":null}]}\n\n'
            'data: {"id":"x","object":"chat.completion.chunk","created":1,"model":"m","choices":[{"index":0,"delta":{"content":"lo"},"finish_reason":null}]}\n\n'
            'data: {"id":"x","object":"chat.completion.chunk","created":1,"model":"m","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n'
            "data: [DONE]\n\n"
        )
        respx.post(_URL).mock(return_value=httpx.Response(200, content=sse.encode(), headers={"content-type": "text/event-stream"}))
        p = OpenAIProvider("p", api_key="sk", base_url=_BASE, model="m")
        chunks = [c for c in _run(_collect(p.complete_stream([_msg("user", "hi")])))]
        assert len(chunks) == 3
        # chunk 内容正确拼回 "hello"
        content = "".join(
            c["choices"][0].get("delta", {}).get("content", "")
            for c in chunks if c.get("choices")
        )
        assert content == "hello"
        # 末 chunk finish_reason
        assert chunks[-1]["choices"][0]["finish_reason"] == "stop"

    @respx.mock
    def test_api_error_before_stream_raises_provider_error(self):
        """首 chunk 前 SDK APIError → ProviderError(让端点回退非流式)。"""
        respx.post(_URL).mock(return_value=httpx.Response(429, json={"error": "rl"}))
        p = OpenAIProvider("p", api_key="sk", base_url=_BASE, model="m")
        import pytest
        with pytest.raises(ProviderError):
            _run(_collect(p.complete_stream([_msg("user", "hi")])))


async def _collect(async_gen):
    """collect async generator into list(支持 complete_stream 的 async gen)。"""
    out = []
    async for c in async_gen:
        out.append(c)
    return out


# ── MockProvider.complete_stream(fake 流)────────────────────────────────


class TestMockProviderStream:
    def test_mock_stream_yields_chunks_then_finish(self):
        chunks = [c for c in _run(_collect(MockProvider().complete_stream([_msg("user", "hi")])))]
        assert len(chunks) == 2
        assert "[mock]" in chunks[0]["choices"][0]["delta"]["content"]
        assert chunks[1]["choices"][0]["finish_reason"] == "stop"
