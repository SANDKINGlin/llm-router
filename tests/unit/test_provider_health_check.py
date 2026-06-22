"""health-probe-lightweight · Provider.health_check 抽象 + OpenAIProvider 实现。

守 spec「探活用轻量端点可达性检查」:health_check() 用 GET /models 而非 complete(),
消除大模型/限流 false-positive 误杀。

H1.1:Provider 基类 health_check 默认 raise NotImplementedError(无轻量探活能力)。
H1.2:OpenAIProvider.health_check httpx GET {base_url}/models 带 auth,2xx→True,否则 False。
"""
from __future__ import annotations

import asyncio

import httpx
import pytest
import respx

from llm_router.providers.base import ChatResult, Provider
from llm_router.providers.mock import MockProvider
from llm_router.providers.openai import OpenAIProvider

_BASE = "https://test.openai.invalid/v1"


def _run(coro):
    return asyncio.run(coro)


# ── H1.1 · Provider 基类 health_check 抽象 ──────────────────────────────


class TestProviderBaseHealthCheck:
    def test_base_provider_health_check_raises_not_implemented(self):
        """基类 health_check 默认 raise NotImplementedError(标识无轻量探活能力)。

        HealthProber 据此回退 complete() 探活(向后兼容)。
        """

        class _Bare(Provider):
            name = "bare"

            async def complete(self, messages, *, tools=None, tool_choice=None):
                return ChatResult(content="x", model="m", usage=None)

        bare = _Bare()
        with pytest.raises(NotImplementedError):
            _run(bare.health_check())

    def test_mock_provider_no_health_check_override(self):
        """MockProvider 不 override health_check(探活本就排除 mock,不会调到;继承基类 raise)。"""
        with pytest.raises(NotImplementedError):
            _run(MockProvider().health_check())


# ── H1.2 · OpenAIProvider.health_check 实现 ─────────────────────────────


class TestOpenAIProviderHealthCheck:
    @respx.mock
    def test_models_2xx_returns_true(self):
        """GET /models 返 2xx → alive=True(端点可达)。"""
        respx.get(f"{_BASE}/models").mock(return_value=httpx.Response(200, json={"data": []}))
        p = OpenAIProvider("p", api_key="sk", base_url=_BASE, model="m")
        assert _run(p.health_check()) is True

    @respx.mock
    def test_models_4xx_returns_false(self):
        """GET /models 返 4xx(key 错/端点不存在)→ alive=False。"""
        respx.get(f"{_BASE}/models").mock(return_value=httpx.Response(401, json={"error": "bad key"}))
        p = OpenAIProvider("p", api_key="sk", base_url=_BASE, model="m")
        assert _run(p.health_check()) is False

    @respx.mock
    def test_models_timeout_returns_false(self):
        """GET /models 超时 → alive=False(不抛,探活循环健壮)。"""
        respx.get(f"{_BASE}/models").mock(side_effect=httpx.ReadTimeout("timeout"))
        p = OpenAIProvider("p", api_key="sk", base_url=_BASE, model="m")
        assert _run(p.health_check()) is False

    @respx.mock
    def test_models_5xx_returns_false(self):
        """GET /models 返 5xx(provider 故障)→ alive=False。"""
        respx.get(f"{_BASE}/models").mock(return_value=httpx.Response(503))
        p = OpenAIProvider("p", api_key="sk", base_url=_BASE, model="m")
        assert _run(p.health_check()) is False

    @respx.mock
    def test_health_check_sends_auth_header(self):
        """health_check 带 Authorization: Bearer {api_key}(OpenRouter/NVIDIA 需要 auth)。"""
        route = respx.get(f"{_BASE}/models").mock(return_value=httpx.Response(200, json={"data": []}))
        p = OpenAIProvider("p", api_key="sk-secret", base_url=_BASE, model="m")
        assert _run(p.health_check()) is True
        assert route.called
        auth = route.calls.last.request.headers.get("authorization")
        assert auth == "Bearer sk-secret"

    @respx.mock
    def test_health_check_does_not_call_complete_endpoint(self):
        """health_check 只打 /models,不碰 /chat/completions(轻量,不消耗 token)。"""
        chat = respx.post(f"{_BASE}/chat/completions").mock(return_value=httpx.Response(200))
        respx.get(f"{_BASE}/models").mock(return_value=httpx.Response(200, json={"data": []}))
        p = OpenAIProvider("p", api_key="sk", base_url=_BASE, model="m")
        _run(p.health_check())
        assert not chat.called  # 没打 chat/completions(不消耗 token)
