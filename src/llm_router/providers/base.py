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


@dataclass(frozen=True)
class ChatResult:
    """complete() 调用结果(chat-protocol-passthrough)。

    content:模型回复文本(纯工具调用时可能空)。
    model:实际响应的模型名(供 trace/ledger)。
    usage:token 用量(可 None,部分 provider 不返)。
    tool_calls:模型返回的 function call 列表(OpenAI 格式),无工具调用时 None。
      agent(Cline 等)读此字段触发工具执行;None = 普通文本回复。
    """
    content: str
    model: str
    usage: Optional[Usage] = None
    tool_calls: Optional[list] = None


class ProviderError(RuntimeError):
    """provider 调用失败(可熔断:超时/5xx/限流/连接错误)。

    router-429-rate-limit-backoff:加 status_code + retry_after。
    Cascade 按 status_code 选 TripReason:429→RATE_LIMIT(精准退避 retry_after);
    其余→HARD(30×2ⁿ 翻倍)。残缺内容 → SOFT_CONTENT(不走异常)。
    """

    def __init__(
        self,
        msg: str,
        *,
        status_code: Optional[int] = None,
        retry_after: Optional[float] = None,
    ) -> None:
        super().__init__(msg)
        self.status_code = status_code
        self.retry_after = retry_after  # 秒;429 时从 Retry-After header 提取


class Provider(ABC):
    """LLM provider 适配层接口。

    complete(prompt) → (text, model_name, usage):async(真 adapter 走异步 HTTP)+ token 用量。
    usage 可 None(mock / 未报用量)→ Cascade 跳过 token_ledger 记账。
    """

    name: str = "base"

    @abstractmethod
    async def complete(
        self,
        messages: list[dict],
        *,
        tools: Optional[list] = None,
        tool_choice: Optional[str] = None,
    ) -> ChatResult:
        """chat completions 透传(chat-protocol-passthrough)。

        messages:OpenAI 格式 [{"role":"system|user|assistant","content":"..."}],结构保留
            (system role 独立,非拍平)。tools/tool_choice:function calling 定义,透传给 provider。
        返回 ChatResult(content + model + usage + tool_calls)。模型返 tool_calls 时 tool_calls 非空
        (agent 据此触发工具);普通文本回复 tool_calls=None。

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

    async def complete_stream(self, messages, *, tools=None, tool_choice=None):
        """真流式:yield OpenAI SSE chunk dict(ponytail:不自造 StreamChunk 抽象,SDK 格式直接透传)。

        默认 raise NotImplementedError。OpenAIProvider override:SDK stream=True + async for yield。
        app.py stream 分支挑首候选直接 pipe 到 SSE 响应;首 chunk 前失败回退非流式 _cascade.run。
        """
        raise NotImplementedError
        # ponytail: 这 yield 是为了让类型检查知道这是 generator;实际永不执行
        yield {}  # pragma: no cover

