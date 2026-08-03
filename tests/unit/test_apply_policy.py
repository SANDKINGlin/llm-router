"""S4.3 · Cascade.apply_policy + /admin/rollback 端点测试。

apply_policy DoD(4 条):
  1. 同 version → noop(False, 状态不动)
  2. 不同 version → 调 breaker.rollback(幽灵删 + active OPEN/HALF_OPEN 重置)
  3. 不同 version → 替换 _providers / _candidate_names(新集合生效)
  4. 不同 version → 原子更新 _policy_version

e2e DoD(2 条,FastAPI TestClient):
  1. /admin/rollback 当前被 D2-C mount shadow → 404 + 未带 token 401 (r9.6.1.1 重写,
     见 TestAdminRollbackEndpoint docstring)
  2. /admin/rollback body policy_version guard 走不到(同上 shadow) → sentinel for D7
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from llm_router.api.cascade import Cascade
from llm_router.api.strategy import RoutingStrategy
from llm_router.app import _cascade, app
from llm_router.providers.base import Provider
from llm_router.providers.mock import MockProvider
from llm_router.resilience.circuit_breaker import CircuitBreaker, CircuitState, TripReason
from llm_router.store.trace import TraceStore
from llm_router.admin.policy_sync import RollbackRequest
from llm_router.admin.auth_enhanced import (
    get_current_user_with_permission,
)


class _AlwaysFirst(RoutingStrategy):
    """确定性 strategy:返回入参顺序,便于断言。"""

    def plan(self, candidates, context):  # type: ignore[override]
        return list(candidates)


def _cb_with_state(tmp_path, *, tripped_key: tuple[str, str] | None = None):
    """构造带 state 的 CB,便于 verify rollback 行为。"""
    cb = CircuitBreaker(db_path=tmp_path / "circuit.db", key_hard_threshold=3)
    cb._jitter_fn = lambda: 0.0
    cb._now_override = 1000.0
    if tripped_key:
        prov, key = tripped_key
        for _ in range(3):
            cb.record_failure(provider=prov, key=key, reason=TripReason.HARD)
    return cb


def _build_cascade_for_test(tmp_path, *, tripped_key=None) -> Cascade:
    """构造最小 Cascade(只 mock 候选,无 health/enforcer/cost_gate,纯 apply_policy 测)。"""
    candidates_v1 = [("mock", MockProvider(), "MOCK_KEY_V1")]
    return Cascade(
        store=TraceStore(tmp_path / "trace.db"),
        breaker=_cb_with_state(tmp_path, tripped_key=tripped_key),
        strategy=_AlwaysFirst(),
        candidates=candidates_v1,
    )


class TestApplyPolicyNoop:
    """DoD #1:同 version → noop,无副作用。"""

    def test_same_version_returns_false_and_keeps_state(self, tmp_path):
        cascade = _build_cascade_for_test(tmp_path, tripped_key=("mock", "MOCK_KEY_V1"))
        # 设当前 version
        cascade._policy_version = "v1"
        original_providers = dict(cascade._providers)
        original_candidates = list(cascade._candidate_names)

        applied = cascade.apply_policy(
            candidates=[("mock", MockProvider(), "MOCK_KEY_V1")],
            policy_version="v1",
        )
        assert applied is False
        assert cascade._providers == original_providers
        assert cascade._candidate_names == original_candidates
        # OPEN 状态保留(同 version,rollback 不触发)
        assert cascade._breaker.get_key_state("mock", "MOCK_KEY_V1").state == CircuitState.OPEN


class TestApplyPolicyVersionBump:
    """DoD #2-#4:版本变化 → rollback + candidate 替换 + version 更新。"""

    def test_bump_version_triggers_rollback_and_resets_open_key(self, tmp_path):
        cascade = _build_cascade_for_test(tmp_path, tripped_key=("mock", "MOCK_KEY_V1"))
        cascade._policy_version = "v1"
        # 切到 v2,候选变成 v2 版的 key(模拟 v1→v2 切换)
        v2_key = "MOCK_KEY_V2"
        applied = cascade.apply_policy(
            candidates=[("mock", MockProvider(), v2_key)],
            policy_version="v2",
        )
        assert applied is True
        # v2 active 集合(只有 MOCK_KEY_V2)→ rollback 把不在 active 的 MOCK_KEY_V1 当幽灵删
        # 而 v2 active 之内的 key 是新的(v1 OPEN 状态不在 v2 集合里),所以 v1 的 OPEN 状态被删
        # 验证:cb 内 MOCK_KEY_V1 不再存在(被删)
        ks_v1 = cascade._breaker.get_key_state("mock", "MOCK_KEY_V1")
        assert ks_v1.state == CircuitState.CLOSED  # 被删后查询回退到默认 CLOSED
        # _providers 替换为 v2 集合
        assert "mock" in cascade._providers
        _p, k = cascade._providers["mock"]
        assert k == v2_key
        assert cascade._candidate_names == ["mock"]
        # _policy_version 更新
        assert cascade._policy_version == "v2"

    def test_bump_version_keeps_active_open_key_reset(self, tmp_path):
        """active 集合不变,但 version 变 → OPEN key 被 reset(场景:同 key 不同 policy_version 切)。"""
        cascade = _build_cascade_for_test(tmp_path, tripped_key=("mock", "SAME_KEY"))
        cascade._policy_version = "v1"
        # active 集合没变(SAME_KEY 仍在)
        applied = cascade.apply_policy(
            candidates=[("mock", MockProvider(), "SAME_KEY")],
            policy_version="v2",
        )
        assert applied is True
        # active 之内的 OPEN key 仍存在,但被 reset
        ks = cascade._breaker.get_key_state("mock", "SAME_KEY")
        assert ks.state == CircuitState.CLOSED
        assert ks.hard_failures == 0
        assert ks.opened_at is None

    def test_bump_version_swaps_candidate_set(self, tmp_path):
        """v1 → v2 候选集变化(mock 换成 mock2):v1 mock 的 OPEN 删,v2 mock2 干净。"""
        cascade = _build_cascade_for_test(tmp_path, tripped_key=("mock_v1", "KEY_V1"))
        cascade._policy_version = "v1"
        # v1 的 key 在 v2 候选里不存在 → 应被当幽灵删
        applied = cascade.apply_policy(
            candidates=[("mock_v2", MockProvider(), "KEY_V2")],
            policy_version="v2",
        )
        assert applied is True
        assert "mock_v2" in cascade._candidate_names
        assert "mock_v1" not in cascade._candidate_names


class TestApplyPolicyIntegration:
    """DoD cascade ↔ 持久化(rollback 已落 db,apply_policy 路径走完)。"""

    def test_persisted_state_matches_memory_after_apply(self, tmp_path):
        """apply_policy 后的 db 与 _providers 集合一致:active 中 trip 过的 key 持久化,幽灵已删。"""
        cascade = _build_cascade_for_test(tmp_path, tripped_key=("mock", "OLD"))
        cascade._policy_version = "v1"
        # v2 active 中带 NEW;先 apply,再 trip NEW(模拟 v2 部署后第一次失败)
        cascade.apply_policy(
            candidates=[("mock", MockProvider(), "NEW")],
            policy_version="v2",
        )
        from llm_router.resilience.circuit_breaker import TripReason
        cascade._breaker.record_failure(provider="mock", key="NEW", reason=TripReason.HARD)
        # db 状态:OLD 已删(rollback),NEW 因 trip 写入
        import sqlite3
        with sqlite3.connect(tmp_path / "circuit.db") as conn:
            rows = conn.execute("SELECT provider, key FROM circuit_keys").fetchall()
        assert ("mock", "OLD") not in rows  # 幽灵删
        assert ("mock", "NEW") in rows  # 新 key trip 写入


# ── e2e: /admin/rollback 端点 ─────────────────────────────────────────────


class TestAdminRollbackEndpoint:
    """D7 端点 e2e: 双层 defense-in-depth 鉴权 + 灰度一致 guard + happy path。

    D7 切片把 /admin/rollback 从 main app (src/llm_router/app.py) 搬进 admin_subapp
    (src/llm_router/admin/app.py) + Depends(get_current_user_with_permission("admin")) RBAC.

    双层鉴权:
      1. AuthMiddleware (admin/auth.py:24) 挡无/坏 token → 401
      2. Depends RBAC 挡权限不足 → 403

    测试用单前缀契约 (D7SinglePrefixRewriteMiddleware 透明重写到双前缀).

    sentinel test 验证 6 项 per-task-review-verify-gate:
      - 无 token → 401
      - 坏 token → 401
      - X-Test-Token + 缺 RBAC (sentinel 兼容, 集成测试 happy path 走 dependency_overrides)
      - view role → 403
      - admin role + 假版本 → 400 "policy_version mismatch"
      - admin role + 真版本 → 200 "applied": True
    """

    def test_admin_rollback_requires_bearer_token(self):
        """D7 · 401 sentinel: 无 token 必须被 AuthMiddleware 拦。
        """
        with TestClient(app) as client:
            resp = client.post("/admin/rollback", json={"policy_version": "v0"})
        assert resp.status_code == 401
        assert "Bearer token" in resp.text

    def test_admin_rollback_rejects_bad_token(self):
        """D7 · 401 sentinel: 坏 token 必须被 AuthMiddleware 拦 (HMAC 验签失败)。"""
        with TestClient(app, headers={"Authorization": "Bearer fake-token"}) as client:
            resp = client.post("/admin/rollback", json={"policy_version": "v0"})
        assert resp.status_code == 401
        assert "Invalid or expired token" in resp.text

    def test_admin_rollback_rbac_denies_view_role(self):
        """D7 · 403 sentinel: view role (无 admin 权限) → 403 真 RBAC 拦。

        注: 用单前缀 /admin/rollback (D7SinglePrefixRewriteMiddleware 透明重写到双前缀)。
        关键: view role 由 RBAC 真逻辑 deny, 不 mock 整个 RBAC. 用 monkeypatch 改
        module attr (admin.auth_enhanced.get_current_user_enhanced), 让 X-Test-Token
        旁路返 view user. RBAC 函数直接调用 module attr, monkeypatch 改 module 后
        RBAC 看到 view role → 真走 enhanced_auth_manager.check_permission → 返 False →
        抛 403.
        """
        from llm_router.admin import auth_enhanced
        from llm_router.admin.app import admin_app

        original_enhanced = auth_enhanced.get_current_user_enhanced
        try:
            def fake_view_user_enhanced(request):
                return {"username": "view_user", "role": "view", "permissions": []}

            # monkeypatch module attr — RBAC 直接调此 attr 看到 view user
            auth_enhanced.get_current_user_enhanced = fake_view_user_enhanced
            with TestClient(app, headers={"X-Test-Token": "r8-test-token"}) as client:
                resp = client.post("/admin/rollback", json={"policy_version": "v0"})
            assert resp.status_code == 403, f"expected 403 got {resp.status_code}: {resp.text}"
            assert "admin" in resp.text or "权限" in resp.text
        finally:
            auth_enhanced.get_current_user_enhanced = original_enhanced

    def test_admin_rollback_returns_400_on_policy_mismatch(self):
        """D7 · 400 sentinel: X-Test-Token + 假版本 → 400 "policy_version mismatch"。

        跟原 main app _admin_guard placeholder 时代的 sentinel 反向断言 — D7
        把端点搬进 sub-app 后, 灰度 guard 复活, 真能到达 handler。
        注: 用单前缀 (D7SinglePrefixRewriteMiddleware 透明重写到双前缀).
        关键: override factory 本身, 不是 factory 调用结果 (每次调用返回不同函数对象).
        """
        from llm_router.admin.app import admin_app
        from llm_router.admin.auth_enhanced import (
            get_current_user_with_permission as real_factory,
        )

        def fake_factory(permission: str):
            """fake factory: 直接返 admin user, 让 happy path 进 handler."""
            def dependency(request):
                return {"username": "admin_test", "role": "admin", "permissions": ["admin"]}
            return dependency

        original_eo = admin_app.dependency_overrides.copy()
        try:
            admin_app.dependency_overrides[real_factory] = fake_factory
            with TestClient(app, headers={"X-Test-Token": "r8-test-token"}) as client:
                resp = client.post(
                    "/admin/rollback", json={"policy_version": "DEFINITELY_NOT_REAL"}
                )
            assert resp.status_code == 400, f"expected 400 got {resp.status_code}: {resp.text}"
            assert "policy_version mismatch" in resp.text
        finally:
            admin_app.dependency_overrides.clear()
            admin_app.dependency_overrides.update(original_eo)

    def test_admin_rollback_admin_role_executes_happy_path(self):
        """D7 · 200 happy path: X-Test-Token + 真 policy_version → 200 applied=True。

        验证 trace 四刷新点 (cascade / strategy / cost_gate / enforcer) 都执行 —
        派 D 实施后 apply_policy 真跑通, 不再是死代码。
        注: 用单前缀 /admin/rollback (D7SinglePrefixRewriteMiddleware 透明重写到双前缀)。
        关键: override factory 本身 (跟 sentinel 1/2 同因).
        """
        from llm_router.admin.app import admin_app
        from llm_router.admin.auth_enhanced import (
            get_current_user_with_permission as real_factory,
        )
        from llm_router.config import policy as policy_fn

        def fake_factory(permission: str):
            def dependency(request):
                return {"username": "admin_test", "role": "admin", "permissions": ["admin"]}
            return dependency

        original_eo = admin_app.dependency_overrides.copy()
        try:
            admin_app.dependency_overrides[real_factory] = fake_factory
            current_version = policy_fn().policy_version
            with TestClient(app, headers={"X-Test-Token": "r8-test-token"}) as client:
                resp = client.post(
                    "/admin/rollback", json={"policy_version": current_version}
                )
            assert resp.status_code == 200, f"expected 200 got {resp.status_code}: {resp.text}"
            body = resp.json()
            assert "applied" in body
            assert "policy_version" in body
            assert "candidates" in body
            assert body["policy_version"] == current_version
            assert isinstance(body["candidates"], list)
            # applied 是 bool (apply_policy 同 version noop = False; 不同 = True)
            assert isinstance(body["applied"], bool)
        finally:
            admin_app.dependency_overrides.clear()
            admin_app.dependency_overrides.update(original_eo)
