"""chat-protocol-passthrough · 路由器透传 chat completions 协议(messages + tools)。

守 spec「路由器透传 chat completions 协议」:保留 messages 结构 + 透传 tools/tool_choice,
响应保留 tool_calls,支持 function calling agent(Cline)。

P1.1:ChatResult dataclass + Provider.complete 新签名。
P1.2:OpenAIProvider 透传 messages/tools/tool_choice,返 ChatResult(含 tool_calls)。
P1.3:MockProvider 迁移新签名。
"""
from __future__ import annotations

import asyncio

import httpx
import respx

from llm_router.providers.base import ChatResult, Provider
from llm_router.providers.mock import MockProvider
from llm_router.providers.openai import OpenAIProvider

_BASE = "https://test.openai.invalid/v1"
_URL = f"{_BASE}/chat/completions"


def _run(coro):
    return asyncio.run(coro)


def _msg(role, content):
    return {"role": role, "content": content}


# ── P1.1 · ChatResult + Provider.complete 新签名 ────────────────────────


class TestChatResultAndSignature:
    def test_chat_result_fields(self):
        """ChatResult 含 content/model/usage/tool_calls;tool_calls 默认 None。"""
        r = ChatResult(content="hi", model="m")
        assert r.content == "hi"
        assert r.model == "m"
        assert r.usage is None
        assert r.tool_calls is None  # 默认 None(无工具调用)

    def test_chat_result_with_tool_calls(self):
        """ChatResult 可带 tool_calls(模型返回 function call)。"""
        tc = [{"id": "call_1", "type": "function", "function": {"name": "run", "arguments": "{}"}}]
        r = ChatResult(content="", model="m", tool_calls=tc)
        assert r.tool_calls == tc

    def test_base_complete_new_signature_accepts_messages_tools(self):
        """Provider.complete 新签名:messages(list[dict]) + tools/tool_choice。"""

        class _P(Provider):
            name = "p"

            async def complete(self, messages, *, tools=None, tool_choice=None):
                return ChatResult(content="ok", model="m")

        p = _P()
        r = _run(p.complete([_msg("user", "hi")], tools=[{"type": "function"}], tool_choice="auto"))
        assert r.content == "ok"


# ── P1.2 · OpenAIProvider 透传 messages/tools/tool_choice ───────────────


class TestOpenAIProviderPassthrough:
    @respx.mock
    def test_plain_text_response(self):
        """无 tools → 纯文本 ChatResult(content+model,tool_calls=None)。"""
        respx.post(_URL).mock(return_value=httpx.Response(200, json={
            "id": "x", "model": "llama", "choices": [{"index": 0, "message": {"role": "assistant", "content": "hello"}, "finish_reason": "stop"}],
        }))
        p = OpenAIProvider("p", api_key="sk", base_url=_BASE, model="m")
        r = _run(p.complete([_msg("user", "hi")]))
        assert r.content == "hello"
        assert r.model == "llama"
        assert r.tool_calls is None

    @respx.mock
    def test_tool_calls_returned(self):
        """模型返 tool_calls → ChatResult.tool_calls 含工具调用。"""
        respx.post(_URL).mock(return_value=httpx.Response(200, json={
            "id": "x", "model": "m", "choices": [{"index": 0, "message": {
                "role": "assistant", "content": None,
                "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "exec", "arguments": "{\"cmd\":\"ls\"}"}}],
            }, "finish_reason": "tool_calls"}],
        }))
        p = OpenAIProvider("p", api_key="sk", base_url=_BASE, model="m")
        r = _run(p.complete([_msg("user", "ls")], tools=[{"type": "function", "function": {"name": "exec"}}]))
        assert r.tool_calls is not None
        assert r.tool_calls[0]["function"]["name"] == "exec"

    @respx.mock
    def test_system_role_preserved(self):
        """system message 独立透传(不拍平进 user)。验 SDK 收到的 messages 含 system role。"""
        route = respx.post(_URL).mock(return_value=httpx.Response(200, json={
            "model": "m", "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
        }))
        p = OpenAIProvider("p", api_key="sk", base_url=_BASE, model="m")
        _run(p.complete([_msg("system", "You are Cline."), _msg("user", "hi")]))
        sent = route.calls.last.request.read()
        import json
        body = json.loads(sent)
        roles = [m["role"] for m in body["messages"]]
        assert roles == ["system", "user"]  # system 独立,非拍平

    @respx.mock
    def test_tools_passed_to_sdk(self):
        """tools 参数透传给 SDK(create 请求 body 含 tools)。"""
        route = respx.post(_URL).mock(return_value=httpx.Response(200, json={
            "model": "m", "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
        }))
        p = OpenAIProvider("p", api_key="sk", base_url=_BASE, model="m")
        tools = [{"type": "function", "function": {"name": "exec", "parameters": {}}}]
        _run(p.complete([_msg("user", "hi")], tools=tools, tool_choice="auto"))
        import json
        body = json.loads(route.calls.last.request.read())
        assert body["tools"] == tools
        assert body["tool_choice"] == "auto"

    @respx.mock
    def test_provider_error_on_api_failure(self):
        """SDK APIError → ProviderError(可熔断),不吞。"""
        respx.post(_URL).mock(return_value=httpx.Response(429, json={"error": {"message": "rl"}}))
        from llm_router.providers.base import ProviderError
        p = OpenAIProvider("p", api_key="sk", base_url=_BASE, model="m")
        import pytest
        with pytest.raises(ProviderError):
            _run(p.complete([_msg("user", "hi")]))


# ── P1.3 · MockProvider 迁移新签名 ──────────────────────────────────────


class TestMockProviderNewSignature:
    def test_mock_complete_accepts_messages(self):
        """MockProvider.complete 新签名:接收 messages,返 ChatResult(canned)。"""
        r = _run(MockProvider().complete([_msg("user", "hi")]))
        assert isinstance(r, ChatResult)
        assert "[mock]" in r.content
        assert r.tool_calls is None

    def test_mock_ignores_tools(self):
        """MockProvider 忽略 tools(无真实工具调用),仍返 canned content。"""
        r = _run(MockProvider().complete([_msg("user", "hi")], tools=[{"type": "function"}]))
        assert r.tool_calls is None
        assert "[mock]" in r.content
