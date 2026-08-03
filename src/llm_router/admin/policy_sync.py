"""Policy 状态同步 helper · D7 /admin/rollback 端点实施

提供 admin_subapp 复用 policy() / cascade.refresh 状态的最小封装,避免
admin/app.py 直接 import llm_router.app 触发循环依赖 (app.py:47 反向
import admin_subapp, admin_subapp 现在再 import app 会循环)。

helper 用 lazy import (函数体内 import),确保 module-load 时不触发
app.py 的 module-level 副作用 (e.g. _cascade = _build_cascade())。

设计原则:
- 单点入口 refresh_policy_state(cascade, policy_version) — DRY 单一刷新源
  (跟 app.py._refresh_and_apply 同形, 但跨文件)
- RollbackRequest Pydantic 跟原 app.py 同 schema, 防 schema 漂移
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

# TYPE_CHECKING 避免运行时 import (循环), 仅给 type hint
if TYPE_CHECKING:
    from ..api.cascade import Cascade


class RollbackRequest(BaseModel):
    """D7 · /admin/rollback body

    policy_version 必须 == policy().policy_version (灰度一致 guard,
    防"操作方以为回了但 yaml 还没生效"的隐式不一致)。
    """

    policy_version: str = Field(
        ...,
        description="回滚目标版本号(必须 == 当前 policy().policy_version)",
    )


def refresh_policy_state(cascade: "Cascade", policy_version: str) -> tuple[bool, list[str]]:
    """单点入口:读 policy + manifest + 重建 candidates + refresh + apply

    Returns:
        (applied, candidate_names): applied = apply_policy 返值, candidate_names =
        重建后 candidates 三元组第 0 项 (provider name) 列表, 给端点返 schema 同原
        main app admin_rollback 完全一致 ({"applied": bool, "policy_version": str,
        "candidates": list[str]})。

    内部 lazy import app.py helper 函数 (避免循环), DRY 跟 app.py:_refresh_and_apply
    行为完全等价 — 任何一边改了,另一边必须同步。
    """
    # lazy import 解决循环: admin/app.py 加载时 admin_subapp 实例化, policy_sync
    # import 时 app.py 已 import admin/app.py (admin_subapp 已构造), 此处再 import
    # 不会重新执行 admin/app.py 顶层代码 (Python module cache), 只解 helper 函数对象。
    from ..config import policy as _policy  # 同 ..config.policy, 不同名避免 shadow
    from ..scanner.mnfst import load_manifest as _load_manifest
    from ..app import _build_three_layer_candidates, _refresh_and_apply

    pol = _policy()
    manifest_entries = _load_manifest()
    entries, candidates = _build_three_layer_candidates(pol, manifest_entries)
    applied = _refresh_and_apply(cascade, entries, candidates, policy_version)
    candidate_names = [n for n, _p, _k in candidates]
    return applied, candidate_names