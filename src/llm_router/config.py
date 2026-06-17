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

    model(S2.3 新增,Optional):真 provider 调用时指定的模型名(喂 OpenAIProvider.model)。
    D4 解锁:S2.1b 时 defer(无真 provider 用到);S2.3 接真 provider 入候选池时需要。
    mock / 未设 → None(adapter 用各自默认)。Phase2 Scanner 填真模型。

    entity(S2.7 新增,Optional):provider 别名归一化的**canonical 实体**(compliance-gate spec Req 1)。
    None → entity 默认 = name 自身(向后兼容:mock / 现有 yaml 无 entity,每个 name 自成一实体)。
    空串("") 同 None,回退 name(显式写 `entity: ""` 等价未设)。
    设了 entity 的 entry 是别名:多别名映射同一 entity(如 openrouter-gptoss/openrouter-qwen → openrouter)
    = 同账号多模型/档位,**合规**;但同一 entity 下出现 ≥2 个不同 api_key_env = 同 provider 多账号薅羊毛,**违规**。
    归一化表由 PolicyEnforcer 从 entries 派生(scanner/manifest 生成)。
    """

    name: str
    entity: str | None = None
    tier: Literal["strong", "medium", "fast"]
    quota: int
    cooldown_s: int
    is_free: bool
    cost_multiplier: float
    model: str | None = None
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
