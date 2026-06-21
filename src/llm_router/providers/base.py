"""Provider 抽象基类。S2.x 各 provider 子类实现;S0.0 只有 MockProvider。

S2.1a:complete 翻 async——真 adapter(httpx/OpenAI/Anthropic SDK)是异步的,
现在翻避免 S2.1b 接真 provider 时返工(ABC + 所有子类 + 调用方一起 async)。

S2.1b:新增 ProviderError——provider 调用失败的可熔断信号(超时/5xx/限流/连接)。
adapter 把 SDK 异常包成 ProviderError;Cascade 只 except ProviderError → record_failure(HARD),
**其余异常上抛不吞**(防把编程 bug 误当 provider 硬失败、错误 trip 熔断 + 掩盖 bug)。

S2.4:complete 返 3-tuple 加 Usage(token 用量,供 token_ledger 记账 + CostGate 超预算过滤)。
Usage 可 None(mock / 未报用量的 provider / 部分 OpenRouter 模型无 usage)→ Cascade 跳过记账。
Usage 用 frozen dataclass,将来加字段(如 latency)不破坏 tuple 外层形状(免调用点返工)。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class Usage:
    """单次 complete() 调用的 token 用量(供 token_ledger 记账 + CostGate 超预算判)。

    frozen:不可变(防误改;同一用量可安全传递/缓存)。将来加字段(如 latency/model-specific)
    只扩本 dataclass,complete() 外层 tuple 保持 3 元,调用点不返工。
    cost 默认 None:Phase1 免费 provider 无成本;付费 provider 可在 adapter 按倍率算后填。
    """

    prompt_tokens: int
    completion_tokens: int
    cost: Optional[float] = None

    @property
    def total_tokens(self) -> int:
        """prompt + completion(供 CostGate 对比 provider.quota)。"""
        return self.prompt_tokens + self.completion_tokens


class ProviderError(RuntimeError):
    """provider 调用失败(可熔断:超时/5xx/限流/连接错误)。

    Cascade 视为 HARD(record_failure(TripReason.HARD))。残缺内容由 is_complete 刭 → SOFT_CONTENT
    (不走异常,adapter 仍返回 (text, model, usage),空文本由 Cascade 的完整性检查兜底)。

    TODO(S2.x):细分 RateLimit(429,可立即重试)/ Transient(5xx·超时)/ Permanent(不可恢复)。
    Phase1 全归 HARD(用户 2026-06-15 AskUserQuestion 锁定映射;HERMES [CONSENSUS])。
    """


class Provider(ABC):
    """LLM provider 适配层接口。

    complete(prompt) → (text, model_name, usage):async(真 adapter 走异步 HTTP)+ token 用量。
    usage 可 None(mock / 未报用量)→ Cascade 跳过 token_ledger 记账。
    """

    name: str = "base"

    @abstractmethod
    async def complete(self, prompt: str) -> tuple[str, str, Optional[Usage]]:
        """返回 (响应文本, 模型名, token 用量)。usage=None 表示未报用量(跳过记账)。

        async:真 adapter 走异步 HTTP。失败抛 ProviderError(可熔断);其余异常上抛不吞。
        """
        raise NotImplementedError

    async def health_check(self) -> bool:
        """轻量端点可达性检查(health-probe-lightweight)。返 True=端点可达,False=不可达。

        默认 raise NotImplementedError = 无轻量探活能力(HealthProber 据此回退 complete() 探活,
        向后兼容)。OpenAIProvider override 为 GET {base_url}/models(轻量,不消耗 token,不触发
        大模型 thinking/限流排队),消除 complete() 探活对大模型的 false-positive 误杀。

        探活目的是**端点可达性新鲜度信号**,不是内容生成能力——两者解耦(见 design D1)。
        """
        raise NotImplementedError

