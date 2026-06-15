"""Provider 抽象基类。S2.x 各 provider 子类实现;S0.0 只有 MockProvider。"""
from __future__ import annotations

from abc import ABC, abstractmethod


class Provider(ABC):
    """LLM provider 适配层接口。

    Phase1: complete(prompt) → (text, model_name)
    Phase2: 流式 / tool_use / 真实 token 计量在子类扩展。
    """

    name: str = "base"

    @abstractmethod
    def complete(self, prompt: str) -> tuple[str, str]:
        """返回 (响应文本, 模型名)。"""
        raise NotImplementedError
