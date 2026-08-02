"""router-policy.yaml 加载。S1.3:ProviderEntry 真实 schema + pydantic 校验(CI 门禁)。

r9.6.2 后切片 #2 (e2e_workflows 6-fail fix) 新增:
- Policy.model_save() / .save() 写 override 路径 (CC 约束 1, 不碰 tracked 基线)
- load_policy 优先级: override > tracked > 默认 (CC 约束 1)
- threading.Lock 保并发写安全 (CC 约束 2)
"""
from __future__ import annotations

import os
import tempfile
import threading
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field

_POLICY_PATH = Path(__file__).resolve().parents[2] / "router-policy.yaml"
_POLICY_OVERRIDE_PATH = _POLICY_PATH.with_name(_POLICY_PATH.stem + ".runtime.yaml")

# 并发写锁 (CC 约束 2: 防多请求同时 PUT gray_percent 写写 race)
_policy_save_lock = threading.Lock()

# IP 安全等级映射（r9.3 排序键第1 维）
IP_SAFETY_RANK = {"safe": 0, "risky": 1, "forbidden": 2}


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
    ip_safety_rank: Literal["safe", "risky", "forbidden"] = "safe"
    model: str | None = None
    base_url: str | None = None
    api_key_env: str | None = None


class Policy(BaseModel):
    version: int = 1
    policy_version: str = "0.0.1"
    gray_percent: int = 100
    providers: list[ProviderEntry] = Field(default_factory=list)
    tiers: dict = Field(default_factory=dict)
    sort_keys: list[str] = Field(
        default_factory=lambda: [
            "ip_safety_rank",  # 越小越优先 (safe=0 < risky=1 < forbidden=2)
            "is_free",         # true(0) < false(1), 免费优先
            "quota_remaining", # 越大越优先 (desc)
            "capability_match", # match(0) < mismatch(1), 能力匹配优先
            "model_strength"   # 越大越优先 (desc), 同能力下选最强
        ]
    )

    def save(self, path: "Path | None" = None) -> None:
        """落盘 Policy 到 override 路径 (CC 约束 1: 不碰 tracked 基线).

        默认写 _POLICY_OVERRIDE_PATH (router-policy.runtime.yaml). 不传 path
        时, 走 model_save() 同实现.

        调用方约定: 由 admin/app.py handler 包 try/except, IO 失败时审计写
        GRANULAR_CHANGE_FAILED + raise HTTPException(500), 见 CC 约束 3.
        """
        self.model_save(path=path)

    def model_save(self, path: "Path | None" = None) -> None:
        """原子写 Policy 到 path 或 override 路径 (CC 约束 1 + 2).

        实施细节:
        - 写路径: 默认 _POLICY_OVERRIDE_PATH, 不传 path 时同 save()
        - 原子写: tempfile.mkstemp + os.replace (防写半截崩溃, CC 约束 2)
        - 并发锁: _policy_save_lock (CC 约束 2, 防多请求写写 race)
        - 序列化: yaml.safe_dump + model_dump(mode="python") (Pydantic v2 兼容)
        - 不动 _POLICY_PATH (tracked 基线, git working tree 不 dirty)

        Raises:
            OSError: 写失败 (由调用方 handler 捕获并审计降级)
        """
        target = Path(path) if path is not None else _POLICY_OVERRIDE_PATH
        data = self.model_dump(mode="python")
        with _policy_save_lock:
            fd, tmp = tempfile.mkstemp(
                dir=target.parent, prefix=target.stem + ".", suffix=".tmp"
            )
            try:
                with os.fdopen(fd, "w") as f:
                    yaml.safe_dump(
                        data, f, allow_unicode=True, sort_keys=False
                    )
                os.replace(tmp, target)
            except Exception:
                # 失败清理 tmp (不污染 dir)
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise


_policy: Policy | None = None


def load_policy(path: Path | None = None) -> Policy:
    """加载 Policy 优先级: override > tracked > 默认 (CC 约束 1).

    加载顺序:
    1. 如果传 path, 用 path 加载 (测试兼容)
    2. 否则: 先尝试 _POLICY_OVERRIDE_PATH (router-policy.runtime.yaml), 不存在则
       尝试 _POLICY_PATH (router-policy.yaml, tracked 基线), 都不存在回退默认
       (S0.0 容错)

    override 优先实现 admin UI 运行时持久化 (gray_percent 调整) 不污染 git
    tracked 基线. 启动时 override 加载 → 内存 Policy 是运行时最新值.
    """
    global _policy
    if path is not None:
        try:
            data = yaml.safe_load(Path(path).read_text()) or {}
            _policy = Policy(**data)
        except FileNotFoundError:
            _policy = Policy()
        return _policy

    # 默认加载顺序: override > tracked > 默认
    for candidate in (_POLICY_OVERRIDE_PATH, _POLICY_PATH):
        try:
            data = yaml.safe_load(candidate.read_text()) or {}
            _policy = Policy(**data)
            return _policy
        except FileNotFoundError:
            continue
        except (yaml.YAMLError, ValueError, TypeError) as e:
            # yaml 损坏或 schema 不匹配: 警告并继续下一个候选
            import warnings
            warnings.warn(
                f"load_policy: {candidate} 解析失败 ({type(e).__name__}: {e}),"
                f" 跳过",
                RuntimeWarning,
            )
            continue

    # 全失败: 回退默认 (S0.0 容错)
    _policy = Policy()
    return _policy


def policy() -> Policy:
    global _policy
    if _policy is None:
        load_policy()
    assert _policy is not None
    return _policy
