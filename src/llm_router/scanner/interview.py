"""S2.10-0.4 · 自动面试(新免费模型冒烟 + 贴标合格判定)。

验收(specs/free-model-scanner Req「动态 diff 抓新免费模型」):新增免费模型 → 自动面试
合格后纳入候选池。本拆片做**面试本身**——给一个 DiscoveredModel,调它的 provider
端点发最小冒烟 prompt,判定合格(返回非空内容)+ 贴 tier 标(关键词推断,复用 tier_infer)。

设计:
- `ProbeFn` 协议:async (model_id) -> str(content)。默认用 OpenAIProvider 打该模型所在
  source 的端点(0.5 编排注入 base_url/key);测试注入 fake probe(零网络)。
- `interview_model`:调 probe + 计时,`content.strip()` 非空 → passed(最小 is_complete);
  异常/超时/空 → failed(reason 记失败类型)。返回 InterviewResult(passed/model/reason/
  latency/snippet)。
- `interview_batch`:并发面试一批(gather,任一失败不影响其他)。

红线(守 ollama-qwen3-rules + routing-priority-principle):
- 面试**不加载本地 ollama 模型**——probe 打的是被面试的远端免费 provider 自身,零本地模型。
- 面试结果只决定**是否入池贴标**(bool)+ tier 标(进能力匹配首槽),不进路由排序键加权。
  排序键仍字典序 `(capability_match DESC, is_free DESC, 倍率 ASC)`;动态 entry is_free=True/
  cost=0 与静态免费 provider 同档竞争。
- bge 不在此加载(defer S2.9;动态面试用关键词 tier 粗匹配,真语义能力匹配走路由时 BgeMatcher)。
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Callable, Optional

from .snapshot import DiscoveredModel
from .tier_infer import label_tier

_LOG = logging.getLogger(__name__)

_DEFAULT_PROBE_TIMEOUT = 20.0
_DEFAULT_PROBE_PROMPT = "Reply with the single word: ok"

ProbeFn = Callable[[str], "asyncio.Future | object"]
"""async (model_id) -> str(content)。抛异常 = 面试失败。0.5 注入真 OpenAIProvider 冒烟。"""


@dataclass(frozen=True)
class InterviewResult:
    """单模型面试结果。

    - model:贴 tier 标后的 DiscoveredModel(面试合格 → 0.5 入库用此;不合格也返回贴标模型
      供审计/重试)。原 model.tier 非 None 时保留,否则 label_tier 推断。
    - passed:冒烟是否合格(content.strip() 非空)。
    - reason:合格 → "ok";失败 → 失败类型/简述(供日志/审计)。
    - latency_ms:probe 耗时(失败时也可能有,超时则 None)。
    - response_snippet:响应前 80 字符(供日志/调试,不存全量防日志膨胀)。
    """

    model: DiscoveredModel
    passed: bool
    reason: str
    latency_ms: Optional[float]
    response_snippet: Optional[str]


def _snippet(text: str, maxlen: int = 80) -> str:
    t = text.strip().replace("\n", " ")
    return t[:maxlen] + ("..." if len(t) > maxlen else "")


async def interview_model(
    model: DiscoveredModel,
    *,
    probe: ProbeFn,
    probe_timeout: float = _DEFAULT_PROBE_TIMEOUT,
) -> InterviewResult:
    """面试单个模型:调 probe 冒烟,非空内容 → passed。

    probe 抛异常 / 超时 / 返空 → failed(reason 记类型,不崩)。
    模型贴 tier 标(label_tier,tier 已有则保留)。
    """
    labeled = label_tier(model)
    try:
        content = await asyncio.wait_for(probe(model.model_id), timeout=probe_timeout)
    except asyncio.TimeoutError:
        return InterviewResult(
            model=labeled, passed=False, reason="timeout",
            latency_ms=None, response_snippet=None,
        )
    except Exception as exc:  # probe 内部已包 ProviderError 等;这里兜底防崩
        return InterviewResult(
            model=labeled, passed=False, reason=f"error:{type(exc).__name__}",
            latency_ms=None, response_snippet=None,
        )
    # probe 返回非 str(编程 bug)→ 不崩,记 fail-loud
    if not isinstance(content, str):
        return InterviewResult(
            model=labeled, passed=False, reason=f"bad_response_type:{type(content).__name__}",
            latency_ms=None, response_snippet=None,
        )
    text = content.strip()
    if not text:
        return InterviewResult(
            model=labeled, passed=False, reason="empty_content",
            latency_ms=None, response_snippet=None,
        )
    return InterviewResult(
        model=labeled, passed=True, reason="ok",
        latency_ms=None,  # 真延迟由 0.5 包 perf_counter 测(同 health-probe);本层只判合格
        response_snippet=_snippet(text),
    )


async def interview_batch(
    models: list[DiscoveredModel],
    *,
    probe: ProbeFn,
    probe_timeout: float = _DEFAULT_PROBE_TIMEOUT,
) -> list[InterviewResult]:
    """并发面试一批(gather,任一失败已在其 interview_model 内捕获,不影响其他)。

    返回顺序与入参 models 一一对应(供 0.5 按结果决定入池/丢弃)。
    """
    results = await asyncio.gather(
        *(interview_model(m, probe=probe, probe_timeout=probe_timeout) for m in models)
    )
    return list(results)


def passed_models(results: list[InterviewResult]) -> list[DiscoveredModel]:
    """从面试结果取合格的 DiscoveredModel 列表(供 0.5 入库贴标)。

    纯函数(便利 API);失败的不进。顺序保留。
    """
    return [r.model for r in results if r.passed]
