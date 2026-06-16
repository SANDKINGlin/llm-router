"""S2.3 · Scanner 静态清单(⑧ Scanner,Phase1 mnfst 版)。

读 mnfst/providers.yaml → list[ProviderEntry](免费/低成本 provider 的完整配置)。
Phase2 由 scanner/dynamic.py(每 h 轮询 NVIDIA/OpenRouter diff)自动更新本清单。

接入候选池的链路(S2.3 真集成):
  load_manifest(path) → list[ProviderEntry]
  → build_adapters(entries, env) → list[(name, OpenAIProvider, account_key)]
      (只对 api_key_env 在 env 里有值的 entry 建真 adapter;缺 key 跳过,不崩)
  → app.py 把它们 + MockProvider 一起喂给 Cascade 候选池

key 安全:env 只读;breaker account_key 用稳定的 api_key_env 名(非 secret 本身,防日志泄密)。
"""
from __future__ import annotations

import os
from pathlib import Path

import yaml

from ..config import ProviderEntry
from ..providers.openai import OpenAIProvider

# 默认清单路径:仓库根 mnfst/providers.yaml。scanner/mnfst.py 在 src/llm_router/scanner/,
# parents[3] = 仓库根(同 config.py parents[2] 的算法,但本文件深一层)。
_DEFAULT_MANIFEST = Path(__file__).resolve().parents[3] / "mnfst" / "providers.yaml"


def load_manifest(path: str | Path | None = None) -> list[ProviderEntry]:
    """读 mnfst 清单 → list[ProviderEntry]。

    缺失文件 → [](Phase1 无真 provider 时降级 mock-only,不崩)。
    解析失败/字段非法 → ValueError(fail-fast,不静默吞坏配置;pydantic 校验 tier/is_free/cost)。
    """
    p = Path(path) if path is not None else _DEFAULT_MANIFEST
    try:
        raw = yaml.safe_load(p.read_text()) or {}
    except FileNotFoundError:
        return []
    rows = raw.get("providers") or []
    entries: list[ProviderEntry] = []
    try:
        for row in rows:
            entries.append(ProviderEntry(**row))
    except Exception as exc:  # pydantic.ValidationError 等
        raise ValueError(f"manifest {p} 解析失败(fail-fast): {exc}") from exc
    return entries


def build_adapters(
    entries: list[ProviderEntry],
    *,
    env: dict[str, str] | None = None,
) -> list[tuple[str, OpenAIProvider, str]]:
    """清单 + env → 真 OpenAIProvider 候选三元组 (name, provider, account_key)。

    只对 entry.api_key_env 且该变量在 env 中有非空值的 entry 建 adapter;其余跳过(不崩)。
    account_key = api_key_env 名(stable,日志/熔断记账用,非 secret 本身)。
    env 默认 os.environ(生产);测试注入确定性 dict。
    """
    environ = env if env is not None else os.environ
    candidates: list[tuple[str, OpenAIProvider, str]] = []
    for entry in entries:
        if not entry.api_key_env or not entry.base_url:
            continue  # 无 key 引用或无 endpoint(mock/不完整)→ 跳过
        secret = environ.get(entry.api_key_env)
        if not secret:
            continue  # 该 key 未配置 → 跳过(渐进接入)
        provider = OpenAIProvider(
            entry.name,
            api_key=secret,
            base_url=entry.base_url,
            model=entry.model or "gpt-4o-mini",
        )
        candidates.append((entry.name, provider, entry.api_key_env))
    return candidates
