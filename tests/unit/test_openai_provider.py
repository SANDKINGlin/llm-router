"""S2.1b · 真 OpenAI adapter 单测(respx 注入真 openai SDK 代码路径)。

测试策略(design §S2.1b 决策1,用户 2026-06-15 拍板):respx 在 httpx 传输层拦截,
**真 adapter 代码 + 真 openai SDK 状态码→异常映射跑全**,只 mock socket。
可控注入 429/5xx/超时 → 验 ProviderError;200 → 验 (text, model)。零成本零 key,
不触发 routing-change-safety。

分类契约(design 点2,HERMES [CONSENSUS]):RateLimit/5xx/Timeout/连接 → ProviderError(→ Cascade HARD);
真 adapter SDK 内部重试关闭(max_retries=0,重试归 Cascade+breaker,防双重重试)。
"""
from __future__ import annotations

import asyncio

import httpx
import pytest
import respx

from llm_router.providers.base import ProviderError
from llm_router.providers.openai import OpenAIProvider

_BASE = "https://test.openai.invalid/v1"
_URL = f"{_BASE}/chat/completions"

_OK_BODY = {
    "id": "chatcmpl-x",
    "object": "chat.completion",
    "created": 1,
    "model": "test-model",
    "choices": [
        {"index": 0, "message": {"role": "assistant", "content": "hello-back"}, "finish_reason": "stop"}
    ],
    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
}


def _run(coro):
    return asyncio.run(coro)


def _provider(model="test-model"):
    return OpenAIProvider("openai-test", base_url=_BASE, api_key="sk-test-fake", model=model)


@respx.mock
def test_success_returns_text_and_model():
    """200 + 合法 chat completion → adapter 返 (content, model)。"""
    respx.post(_URL).mock(return_value=httpx.Response(200, json=_OK_BODY))
    text, model = _run(_provider().complete("hello"))
    assert text == "hello-back"
    assert model == "test-model"


@respx.mock
def test_rate_limit_429_raises_provider_error():
    """429 → openai SDK RateLimitError → adapter 包成 ProviderError(→ Cascade HARD)。"""
    respx.post(_URL).mock(
        return_value=httpx.Response(
            429, json={"error": {"message": "rate limit", "type": "rate_limit_error"}}
        )
    )
    with pytest.raises(ProviderError):
        _run(_provider().complete("hello"))


@respx.mock
def test_server_error_500_raises_provider_error():
    """500 → openai SDK APIStatusError → adapter 包成 ProviderError(→ Cascade HARD)。"""
    respx.post(_URL).mock(
        return_value=httpx.Response(
            500, json={"error": {"message": "internal", "type": "internal_server_error"}}
        )
    )
    with pytest.raises(ProviderError):
        _run(_provider().complete("hello"))


@respx.mock
def test_timeout_raises_provider_error():
    """超时 → openai SDK APITimeoutError(或裸 httpx 超时)→ adapter 包成 ProviderError。

    验传输层失败也被正确分类为 ProviderError(防超时被当 bug 上抛)。
    """
    respx.post(_URL).mock(side_effect=httpx.TimeoutException("timed out"))
    with pytest.raises(ProviderError):
        _run(_provider().complete("hello"))


@respx.mock
def test_request_payload_carries_prompt_and_model():
    """真 adapter 把 prompt 放进 messages、model 放进请求体(respx 捕获,验接线对)。"""
    route = respx.post(_URL).mock(return_value=httpx.Response(200, json=_OK_BODY))
    _run(_provider(model="gpt-test").complete("translate this"))
    assert route.called, "adapter 必须真发了一次 HTTP"
    sent = route.calls.last.request.read()
    payload = sent.decode() if isinstance(sent, (bytes, bytearray)) else sent
    assert "translate this" in payload, "prompt 必须进 messages"
    assert "gpt-test" in payload, "model 必须进请求体"
