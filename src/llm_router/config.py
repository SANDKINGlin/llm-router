"""router-policy.yaml 加载。S1.3:ProviderEntry 真实 schema + pydantic 校验(CI 门禁)。"""
from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field

_POLICY_PATH = Path(__file__).resolve().parents[2] / "router-policy.yaml"


class ProviderEntry(BaseModel):
    """单个 provider 的策略配置(蓝图 §4 S1.3)。

    tier 由 pydantic Literal 校验(strong/medium/fast)——畸形值触发 ValidationError(CI 门禁)。
    base_url / api_key_env 留空:Phase1 mock 无 key,S2.x 接真 provider 时填;
    api_key_env 存的是**环境变量名**,非 key 本身(守 security.md,不硬编码 secret)。

    is_free / cost_multiplier(S2.1a 新增,**REQUIRED**):路由选择排序键的数据源
    (design.md 约束#3 字典序 `(capability_match DESC, is_free DESC, 倍率 ASC)`)。
    REQUIRED 而非 Optional——fail-fast:provider 不声明 cost 状态 → 配置加载 ValidationError,
    防真 provider 被静默当 free(破坏"免费对口严格优先")。
    """

    name: str
    tier: Literal["strong", "medium", "fast"]
    quota: int
    cooldown_s: int
    is_free: bool
    cost_multiplier: float
    base_url: str | None = None
    api_key_env: str | None = None


class Policy(BaseModel):
    version: int = 1
    policy_version: str = "0.0.1"
    gray_percent: int = 100
    providers: list[ProviderEntry] = Field(default_factory=list)
    tiers: dict = Field(default_factory=dict)


_policy: Policy | None = None


def load_policy(path: Path | None = None) -> Policy:
    """加载 router-policy.yaml。文件缺失回退默认(S0.0 容错)。"""
    global _policy
    p = path or _POLICY_PATH
    try:
        data = yaml.safe_load(p.read_text()) or {}
        _policy = Policy(**data)
    except FileNotFoundError:
        _policy = Policy()
    return _policy


def policy() -> Policy:
    global _policy
    if _policy is None:
        load_policy()
    assert _policy is not None
    return _policy
