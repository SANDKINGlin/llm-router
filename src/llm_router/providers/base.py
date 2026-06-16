"""Provider 抽象基类。S2.x 各 provider 子类实现;S0.0 只有 MockProvider。

S2.1a:complete 翻 async——真 adapter(httpx/OpenAI/Anthropic SDK)是异步的,
现在翻避免 S2.1b 接真 provider 时返工(ABC + 所有子类 + 调用方一起 async)。

S2.1b:新增 ProviderError——provider 调用失败的可熔断信号(超时/5xx/限流/连接)。
adapter 把 SDK 异常包成 ProviderError;Cascade 只 except ProviderError → record_failure(HARD),
**其余异常上抛不吞**(防把编程 bug 误当 provider 硬失败、错误 trip 熔断 + 掩盖 bug)。
"""
from __future__ import annotations

from abc import ABC, abstractmethod


class ProviderError(RuntimeError):
    """provider 调用失败(可熔断:超时/5xx/限流/连接错误)。

    Cascade 视为 HARD(record_failure(TripReason.HARD))。残缺内容由 is_complete 判 → SOFT_CONTENT
    (不走异常,adapter 仍返回 (text, model),空文本由 Cascade 的完整性检查兜底)。

    TODO(S2.x):细分 RateLimit(429,可立即重试)/ Transient(5xx·超时)/ Permanent(不可恢复)。
    Phase1 全归 HARD(用户 2026-06-15 AskUserQuestion 锁定映射;HERMES [CONSENSUS])。
    """


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
