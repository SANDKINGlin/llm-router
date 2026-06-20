"""S2.10-0.2 · 动态 Scanner 轮询器(NVIDIA /v1/models + OpenRouter /models,只读 GET)。

验收(specs/free-model-scanner Req「动态 diff 抓新免费模型」):每 h 轮询两端点,解析出
免费模型 → DiscoveredModel → Snapshot,供 0.1 diff。本拆片只做**抓取 + 解析**——
diff(0.1)/存储(0.3)/面试(0.4)/编排(0.5)留后续子片。

只读 GET,零副作用(守 routing-change-safety:不改路由配置,不出站写)。
HTTP 错误 → 返回空 Snapshot(降级,不崩后台循环,同 health-probe 侧挂健壮语义);
但解析出的模型**必**过 `is_free` 过滤(非免费不进快照,spec 钉死"抓免费顶级模型")。

设计:
- `Fetcher` 协议:async (url, headers, timeout) -> dict(JSON)。默认 httpx 实现;
  测试注入 fake fetcher 返回 canned JSON(零网络,守 respx 已用模式)。
- `poll_nvidia` / `poll_openrouter`:各解析自己的 /models 形状,过滤免费,贴 tier 标,
  返回 Snapshot。NVIDIA NIM 免费档全 is_free=True;OpenRouter 按 `:free` 后缀
  或 pricing==0 判定免费(只留免费)。
- 真 API 端点 + key 从 env 读;key 缺失 → 空 Snapshot(降级,不抛——后台循环健壮)。
"""
from __future__ import annotations

import logging
import os
from typing import Any, Protocol

import httpx

from .snapshot import DiscoveredModel, ScannerSource, Snapshot
from .tier_infer import label_tier

_LOG = logging.getLogger(__name__)

NVIDIA_MODELS_URL = "https://integrate.api.nvidia.com/v1/models"
OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
_DEFAULT_TIMEOUT = 15.0


class Fetcher(Protocol):
    """async JSON 抓取协议(测试注入 fake,生产 httpx)。"""

    async def __call__(self, url: str, headers: dict[str, str], timeout: float) -> dict[str, Any]:
        ...


async def _httpx_fetch(url: str, headers: dict[str, str], timeout: float) -> dict[str, Any]:
    """默认 httpx 抓取器(生产用)。网络/HTTP 错误上抛(由 poller 捕获降级)。"""
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.get(url, headers=headers)
        resp.raise_for_status()
        return resp.json()


def _bearer(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


async def poll_nvidia(
    *,
    api_key: str | None = None,
    fetcher: Fetcher | None = None,
    timeout: float = _DEFAULT_TIMEOUT,
) -> Snapshot:
    """轮询 NVIDIA NIM `/v1/models` → Snapshot(免费模型)。

    NVIDIA NIM 免费档:integrate.api.nvidia.com 上的模型均走免费 credit 额度,
    全部 is_free=True(无 `:free` 后缀约定)。返回的模型贴 tier 标(关键词推断)。
    key 缺失 / 网络错 / 解析异常 → 空 Snapshot(降级,不崩)。
    """
    key = api_key if api_key is not None else os.environ.get("NVIDIA_API_KEY", "")
    if not key:
        _LOG.debug("poll_nvidia: 无 NVIDIA_API_KEY,降级空快照")
        return Snapshot.empty(ScannerSource.NVIDIA)
    fetch = fetcher or _httpx_fetch
    try:
        data = await fetch(NVIDIA_MODELS_URL, _bearer(key), timeout)
    except Exception as exc:  # 网络/HTTP/超时——后台循环健壮,降级空
        _LOG.warning("poll_nvidia 抓取失败,降级空快照: %s", exc)
        return Snapshot.empty(ScannerSource.NVIDIA)
    models = _parse_nvidia(data)
    return Snapshot(source=ScannerSource.NVIDIA, models=frozenset(models))


def _parse_nvidia(data: dict[str, Any]) -> list[DiscoveredModel]:
    """解析 NVIDIA /v1/models 响应(OpenAI 形状 `{data: [{id, ...}]}`)。

    畸形(无 data / 非 list / 无 id)→ 跳过该条不崩;全畸形 → [](降级空)。
    """
    out: list[DiscoveredModel] = []
    rows = data.get("data") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        return out
    for row in rows:
        if not isinstance(row, dict):
            continue
        mid = row.get("id")
        if not isinstance(mid, str) or not mid:
            continue
        out.append(
            label_tier(
                DiscoveredModel(
                    source=ScannerSource.NVIDIA,
                    model_id=mid,
                    display_name=row.get("name") or mid,
                    is_free=True,  # NIM 免费档
                )
            )
        )
    return out


async def poll_openrouter(
    *,
    api_key: str | None = None,
    fetcher: Fetcher | None = None,
    timeout: float = _DEFAULT_TIMEOUT,
) -> Snapshot:
    """轮询 OpenRouter `/models` → Snapshot(**仅**免费模型)。

    OpenRouter 免费判定:model id 以 `:free` 结尾,或 pricing.prompt=="0" 且
    pricing.completion=="0"。非免费过滤掉(spec 钉死"抓免费顶级模型")。
    key 缺失 → 仍可调(/models 公开端点,但 spec 要求 key 路径一致)→ 用空 key 调;
    实际 OpenRouter /models 无 key 也可,但为对齐生产鉴权,缺 key 时降级空(守一致)。
    """
    key = api_key if api_key is not None else os.environ.get("OPENROUTER_API_KEY", "")
    if not key:
        _LOG.debug("poll_openrouter: 无 OPENROUTER_API_KEY,降级空快照")
        return Snapshot.empty(ScannerSource.OPENROUTER)
    fetch = fetcher or _httpx_fetch
    try:
        data = await fetch(OPENROUTER_MODELS_URL, _bearer(key), timeout)
    except Exception as exc:
        _LOG.warning("poll_openrouter 抓取失败,降级空快照: %s", exc)
        return Snapshot.empty(ScannerSource.OPENROUTER)
    models = _parse_openrouter(data)
    return Snapshot(source=ScannerSource.OPENROUTER, models=frozenset(models))


def _parse_openrouter(data: dict[str, Any]) -> list[DiscoveredModel]:
    """解析 OpenRouter /models 响应(`{data: [{id, name, pricing: {...}}, ...]}`)。

    仅留免费模型(`:free` 后缀 或 pricing 全 0)。畸形条目跳过不崩。
    """
    out: list[DiscoveredModel] = []
    rows = data.get("data") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        return out
    for row in rows:
        if not isinstance(row, dict):
            continue
        mid = row.get("id")
        if not isinstance(mid, str) or not mid:
            continue
        if not _is_openrouter_free(row, mid):
            continue
        out.append(
            label_tier(
                DiscoveredModel(
                    source=ScannerSource.OPENROUTER,
                    model_id=mid,
                    display_name=row.get("name") or mid,
                    is_free=True,
                )
            )
        )
    return out


def _is_openrouter_free(row: dict[str, Any], model_id: str) -> bool:
    """OpenRouter 免费判定:`:free` 后缀 或 pricing.prompt==completion==\"0\"。"""
    if model_id.endswith(":free"):
        return True
    pricing = row.get("pricing")
    if isinstance(pricing, dict):
        prompt = pricing.get("prompt")
        completion = pricing.get("completion")
        if prompt == "0" and completion == "0":
            return True
    return False


async def poll_all(
    *,
    fetcher: Fetcher | None = None,
    nvidia_key: str | None = None,
    openrouter_key: str | None = None,
    timeout: float = _DEFAULT_TIMEOUT,
) -> dict[ScannerSource, Snapshot]:
    """并发轮询所有 source → {source: Snapshot}(供 0.5 编排同批次 diff)。

    任一 source 失败已在其 poll_* 内降级空,不影响其他 source。latest-wins
    同 merge_snapshots 语义(本处每 source 只一个快照)。
    """
    import asyncio

    nv_task = poll_nvidia(api_key=nvidia_key, fetcher=fetcher, timeout=timeout)
    or_task = poll_openrouter(api_key=openrouter_key, fetcher=fetcher, timeout=timeout)
    nv, orr = await asyncio.gather(nv_task, or_task)
    return {ScannerSource.NVIDIA: nv, ScannerSource.OPENROUTER: orr}
