"""MockProvider:S0.0 唯一 provider。返回 canned 假响应,绝不调真 API。

用途:① 骨架验收 ② 单元测试 ③ C 三步验证证明链路通(不证明水质)。
S2.x 接真 provider 后,路由层优先选真 provider,mock 仅测试用。
"""
from __future__ import annotations

from .base import Provider

_CANNED = "[mock] llm-router skeleton OK — canned response, not a real model."


class MockProvider(Provider):
    name = "mock"

    async def complete(self, prompt: str) -> tuple[str, str]:
        return _CANNED, "mock-skeleton"
