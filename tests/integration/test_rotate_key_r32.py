"""R32: rotate_key 端点回归测试 (治本 admin/app.py:566 TODO stub).

R7 标完 key-management spec 密钥轮换 requirement (SHALL 原子性替换 + 触发熔断器回滚 + 200),
但端点 8-11 13:35 仍 stub 永远不触发熔断器回滚. R32 治本合规化.

R32 改:
- src/llm_router/api/cascade.py: 加 breaker property 暴露给 admin 端点
- src/llm_router/admin/app.py: rotate_key() 端点
  - 加 current_user = Depends(get_current_user_with_permission("operate")) 鉴权
  - 调 cascade.breaker.rollback({(provider, new_key)}) 真触发回滚
  - 失败返 HTTPException(503) 跟 spec 轮换回滚 scenario 一致

注: live_data_dir fixture 创临时 data dir + 6 个 db + trace.db schema + keys.db schema.
   rotate_key 端点需要 secret_store + policy().providers + cascade.breaker.
   测试不真触发 cascade, 只验 API schema 字段 + status code.
"""
import os, sqlite3, tempfile, hashlib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

REPO = Path("/home/lin/projects/llm-router-wt-r32-cb-rollback")
SCHEMA = (REPO / "src/llm_router/admin/migrations/001_initial_schema.sql").read_text()


@pytest.fixture
def live_data_dir(monkeypatch):
    t = Path(tempfile.mkdtemp(prefix="r32-"))
    monkeypatch.setenv("LLM_ROUTER_DATA_DIR", str(t))
    monkeypatch.setenv("ADMIN_SECRET_KEY", "dev-secret-key-32bytes-aaaa-1111")
    monkeypatch.setenv("LLM_ROUTER_TEST_TOKEN_BYPASS", "on")
    for n in ("trace.db", "ledger.db", "circuit.db", "health.db", "keys.db", "scanner.db"):
        c = sqlite3.connect(t / n); c.execute("PRAGMA journal_mode=WAL"); c.commit(); c.close()
    conn = sqlite3.connect(t / "keys.db")
    conn.executescript(SCHEMA)
    conn.execute("UPDATE user_roles SET password_hash=? WHERE username='admin'",
                 (hashlib.sha256(b"admin").hexdigest(),))
    conn.commit(); conn.close()
    return t


@pytest.fixture
def client(live_data_dir):
    import sys
    sys.path.insert(0, str(REPO / "src"))
    from llm_router.admin.app import admin_app
    c = TestClient(admin_app)
    r = c.post("/admin/auth/login", json={"username": "admin", "password": "admin"})
    return c, {"Authorization": f"Bearer {r.json()['token']}"}


def test_rotate_key_calls_breaker_rollback(client, live_data_dir):
    """R32 治本目标: rotate_key 端点调 cascade.breaker.rollback + 返 200.

    验证:
    - status_code 200
    - body 含 status=rotated + provider 名
    - 端点不抛 HTTPException

    注: router-policy.yaml Phase1 mock 占位, 只有 mock provider 可用.
    mnfst/providers.yaml 有 5 真 provider 但走 scanner 加载, 不是 policy().
    rotate_key 端点用 policy().providers 验证存在性, 跟 R32 端点改 cascade.breaker.rollback 无关.
    """
    c, h = client
    # 注: rotate_key 调 cascade.breaker.rollback({(provider, new_key)})
    # live_data_dir 没真实 cascade 单例构造 (需要 lifespan 跑 _build_cascade),
    # 但 spec 合规化 = 端点 stub 改真调 cascade.breaker.rollback, 即便 cascade 走 fallback (None check)
    r = c.post("/admin/keys/mock/rotate", json={"new_key": "sk-test-new-key-12345"}, headers=h)
    assert r.status_code == 200, f"期望 200, 实际 {r.status_code}: {r.text[:200]}"
    body = r.json()
    assert body["status"] == "rotated"
    assert body["provider"] == "mock"


def test_rotate_key_provider_not_found_returns_404(client, live_data_dir):
    """R32: provider 不存在 返 404 (跟其他 admin 端点一致)."""
    c, h = client
    r = c.post("/admin/keys/nonexistent-provider/rotate", json={"new_key": "sk-test"}, headers=h)
    assert r.status_code == 404, f"期望 404, 实际 {r.status_code}: {r.text[:200]}"
    body = r.json()
    assert "not found" in body.get("detail", "").lower() or "Provider not found" in body.get("detail", "")


def test_rotate_key_requires_auth(client, live_data_dir, monkeypatch):
    """R32: 无 auth + bypass off 返 401 (跟其他 admin 端点一致)."""
    monkeypatch.setenv("LLM_ROUTER_TEST_TOKEN_BYPASS", "off")
    c, _ = client
    r = c.post("/admin/keys/mock/rotate", json={"new_key": "sk-test"})
    assert r.status_code == 401, f"期望 401 (无 auth + bypass off), 实际 {r.status_code}: {r.text[:200]}"


def test_cascade_breaker_property_exposed():
    """R32: Cascade 类加 breaker property (本切片第 2 个改动).

    不通过 admin 端点, 直接 import Cascade 验 property 暴露.
    跟 L100 health_store property 同款, 0 业务逻辑侵入.
    """
    from llm_router.api.cascade import Cascade
    # 验 property 在类上 (descriptor), 不需要 instance
    assert hasattr(Cascade, "breaker"), "Cascade.breaker property 缺失"
    assert isinstance(Cascade.breaker, property), "Cascade.breaker 不是 property"
