"""Provider 抽象基类。S2.x 各 provider 子类实现;S0.0 只有 MockProvider。

S2.1a:complete 翻 async——真 adapter(httpx/OpenAI/Anthropic SDK)是异步的,
现在翻避免 S2.1b 接真 provider 时返工(ABC + 所有子类 + 调用方一起 async)。
"""
from __future__ import annotations

from abc import ABC, abstractmethod


class Provider(ABC):
    """LLM provider 适配层接口。

    Phase1: async complete(prompt) → (text, model_name)
    Phase2: 流式 / tool_use / 真实 token 计量在子类扩展。
    """

    name: str = "base"

    @abstractmethod
    async def complete(self, prompt: str) -> tuple[str, str]:
        """返回 (响应文本, 模型名)。async:真 adapter 走异步 HTTP。"""
        raise NotImplementedError
