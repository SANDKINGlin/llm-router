"""S2.1b · 真 OpenAI adapter(async,respx 测,零 key 零成本)。

openai SDK 异常(RateLimit/APIStatus/APITimeout/APIConnection 等,均为 openai.APIError 子类)
统一包成 ProviderError —— Cascade 视为 HARD 触发熔断。max_retries=0:关闭 SDK 内部重试,
重试归 Cascade+breaker(防双重重试:SDK 偷偷重试会让 breaker 看到非单次失败)。

非 Provider 异常(编程 bug:TypeError/AttributeError 等)上抛不吞 —— 见 base.py ProviderError。
残缺内容(空文本)不抛:仍返 (text, model),完整性判定走 Cascade 的 is_complete(SOFT_CONTENT)。

验证(2026-06-16 沙盒实测 openai 2.41.1):429→RateLimitError / 500→InternalServerError /
httpx timeout→APITimeoutError,三者均为 openai.APIError 子类,catch 基类全覆盖。
"""
from __future__ import annotations

import openai

from .base import Provider, ProviderError, Usage

_HEALTH_CHECK_TIMEOUT = 8.0  # GET /models 轻量探活超时(秒,大模型 thinking/限流不触发)


class OpenAIProvider(Provider):
    """OpenAI Chat Completions adapter(async)。

    Phase1:complete(prompt)->(text, model, usage);SDK 异常全归 ProviderError(HARD)。
    S2.4:提取 resp.usage → Usage(token_ledger 记账 + CostGate 超预算过滤)。部分 provider
    (如某些 OpenRouter 模型)不返 usage → Usage=None(Cascade 跳过记账,fail-open)。
    TODO(S2.x):细分 RateLimit(立即可重试)/Transient(5xx·超时)/Permanent(不可恢复);流式。
    """

    def __init__(
        self,
        name: str,
        *,
        api_key: str,
        base_url: str | None = None,
        model: str = "gpt-4o-mini",
    ) -> None:
        self.name = name
        self.model = model
        self._api_key = api_key
        self._base_url = base_url
        self._client = openai.AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            max_retries=0,  # 关 SDK 内部重试,重试归 Cascade+breaker(防双重重试)
        )

    async def health_check(self) -> bool:
        """轻量端点可达性检查(health-probe-lightweight D1/D4)。

        GET {base_url}/models 带 Authorization: Bearer {api_key},timeout 8s:
          - 2xx → True(端点可达)
          - 非 2xx/超时/异常 → False(不抛,探活循环健壮)

        比 complete() 探活轻量(/models 不消耗 token、不触发大模型 thinking/限流排队),
        消除 550B 大模型 false-positive 误杀。用 httpx 直接 GET(D4:不被 SDK 解析逻辑干扰,
        直接判 HTTP 状态码)。base_url 为 None → False(无法探活,守安全不猜端点)。
        """
        if not self._base_url:
            return False
        import httpx

        url = f"{self._base_url.rstrip('/')}/models"
        try:
            async with httpx.AsyncClient(timeout=_HEALTH_CHECK_TIMEOUT) as client:
                resp = await client.get(url, headers={"Authorization": f"Bearer {self._api_key}"})
            return 200 <= resp.status_code < 300
        except Exception:
            # 超时/连接错误/任何异常 → 不可达(不抛,HealthProber 探活循环健壮不崩)
            return False

    async def complete(self, prompt: str) -> tuple[str, str, Usage | None]:
        try:
            resp = await self._client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": prompt}],
            )
        except openai.APIError as exc:
            # D2(HERMES 共识):SDK 异常 → ProviderError → Cascade HARD。其余异常上抛不吞。
            raise ProviderError(
                f"{self.name}: openai 调用失败 ({type(exc).__name__}): {exc}"
            ) from exc
        content = resp.choices[0].message.content or ""
        usage = self._extract_usage(resp)
        return content, resp.model, usage

    @staticmethod
    def _extract_usage(resp) -> Usage | None:
        """从 SDK 响应提取 Usage;无 usage 字段(部分 provider 不返)→ None。"""
        u = getattr(resp, "usage", None)
        if u is None:
            return None
        prompt = getattr(u, "prompt_tokens", 0) or 0
        completion = getattr(u, "completion_tokens", 0) or 0
        # total 缺失时回退 prompt+completion(个别 provider 只给部分字段)。
        return Usage(prompt_tokens=prompt, completion_tokens=completion)
