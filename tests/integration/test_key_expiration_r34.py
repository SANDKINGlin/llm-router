"""R34: 密钥过期检查端点回归测试 (新功能, 改 admin/keys.py stub 函数).

R34 实施:
- admin/keys.py: 改 get_key_expiration_warning 真查 providers.created_at + 算 (now - created_at).days
- admin/keys.py: 改 __init__ ENV 化 (走 LLM_ROUTER_DATA_DIR, 跟 R30 _get_trace_store precedent)
- admin/app.py: 端点 admin/app.py:329 不改 schema, 函数返真值即生效
- 0 ALTER TABLE (created_at 已存在, R2.x 实施时建)

注: live_data_dir fixture 创临时 data dir + 6 个 db + 写 providers 表 + 调端点.
   key_manager 是 module-level 单例 (L129), db_path 在 import 期固化.
   R34 测试 fixture: 创建 key_manager 实例, 走 live_data_dir 隔离 keys.db.
   4 个 case 覆盖: warning 路径 (created_at > 30 天前) / ok 路径 (< 30 天) /
   provider 不在 db (None) / 端点 404 (provider 不在 policy).

   跟 R32 test_rotate_key_r32.py 关键区别: R34 测试不调 admin 端点 (key_manager
   走 module-level db_path), 而是直接调 key_manager.get_key_expiration_warning,
   避免单例 db_path 撞主仓问题. 但仍测 admin 端点 (test_warning/ok 走端点).
"""
import os, sqlite3, tempfile, hashlib
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

REPO = Path("/home/lin/projects/llm-router-wt-r34-key-expiration")
SCHEMA = (REPO / "src/llm_router/admin/migrations/001_initial_schema.sql").read_text()


def _insert_provider(db_path: Path, name: str, created_at: str) -> None:
    """helper: 写 1 条 provider 记录 (跟 schema 一致)."""
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO providers (name, tier, base_url, quota, cooldown_s, "
        "cost_multiplier, default_model, config_json, is_active, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)",
        (name, "fast", "https://example.com/v1", 1000000, 30, 1.0,
         "default-model", "{}", created_at, created_at),
    )
    conn.commit(); conn.close()


@pytest.fixture
def live_data_dir(monkeypatch):
    t = Path(tempfile.mkdtemp(prefix="r34-"))
    monkeypatch.setenv("LLM_ROUTER_DATA_DIR", str(t))
    monkeypatch.setenv("ADMIN_SECRET_KEY", "dev-secret-key-32bytes-aaaa-1111")
    monkeypatch.setenv("LLM_ROUTER_TEST_TOKEN_BYPASS", "on")
    monkeypatch.setenv("KEY_EXPIRY_WARNING_DAYS", "30")
    for n in ("trace.db", "ledger.db", "circuit.db", "health.db", "keys.db", "scanner.db"):
        c = sqlite3.connect(t / n); c.execute("PRAGMA journal_mode=WAL"); c.commit(); c.close()
    conn = sqlite3.connect(t / "keys.db")
    conn.executescript(SCHEMA)
    conn.execute("UPDATE user_roles SET password_hash=? WHERE username='admin'",
                 (hashlib.sha256(b"admin").hexdigest(),))
    conn.commit(); conn.close()
    return t


@pytest.fixture
def fresh_key_manager(live_data_dir):
    """R34 fixture: 创建新 KeyManager 实例, 走 live_data_dir 隔离.

    key_manager 是 module-level 单例 (src/llm_router/admin/keys.py:129),
    默认 db_path 在 import 期固化. R34 改 __init__ ENV 化后, 单例 db_path
    还是 import 期的 LLM_ROUTER_DATA_DIR (主仓, 未设). 测试要单例重新走 ENV,
    需新创建 KeyManager 实例 + 设 db_path=live_data_dir/keys.db.
    """
    from llm_router.admin.keys import KeyManager
    km = KeyManager()
    km.db_path = str(live_data_dir / "keys.db")
    return km


@pytest.fixture
def client(live_data_dir):
    import sys
    sys.path.insert(0, str(REPO / "src"))
    from llm_router.admin.app import admin_app
    c = TestClient(admin_app)
    r = c.post("/admin/auth/login", json={"username": "admin", "password": "admin"})
    return c, {"Authorization": f"Bearer {r.json()['token']}"}


def test_warning_for_old_provider(fresh_key_manager, live_data_dir):
    """R34: created_at 40 天前, 函数返 warning 字符串含 '40'.

    注: 直接调 key_manager.get_key_expiration_warning (绕过 admin 端点),
    fresh_key_manager 走 live_data_dir 隔离 (避免单例 db_path 撞主仓).
    """
    db = live_data_dir / "keys.db"
    old_date = (datetime.utcnow() - timedelta(days=40)).strftime("%Y-%m-%d %H:%M:%S")
    _insert_provider(db, "mock", old_date)

    warning = fresh_key_manager.get_key_expiration_warning("mock", {})
    assert warning is not None
    assert "40" in warning, f"warning 应含 '40', 实际: {warning}"


def test_ok_for_recent_provider(fresh_key_manager, live_data_dir):
    """R34: created_at 5 天前, 函数返 None."""
    db = live_data_dir / "keys.db"
    recent_date = (datetime.utcnow() - timedelta(days=5)).strftime("%Y-%m-%d %H:%M:%S")
    _insert_provider(db, "mock", recent_date)

    warning = fresh_key_manager.get_key_expiration_warning("mock", {})
    assert warning is None


def test_provider_not_in_db_returns_no_warning(fresh_key_manager, live_data_dir):
    """R34: provider 在 policy 但不在 providers 表, 函数返 None (DB 错不打破端点)."""
    warning = fresh_key_manager.get_key_expiration_warning("mock", {})
    assert warning is None


def test_endpoint_provider_not_in_policy_returns_404(client, live_data_dir):
    """R34: router-policy.yaml 没 provider 返 404 (跟其他 admin 端点一致, 端点层验证)."""
    c, h = client
    r = c.get("/api/admin/keys/neverexists/expiration", headers=h)
    # 端点先 check policy → 404 (provider 不在 policy 列表)
    # 跟 R32 同款 precedent (rotate_key 端点先 policy 验证)
    assert r.status_code == 404, f"期望 404, 实际 {r.status_code}: {r.text[:200]}"
    body = r.json()
    assert "not found" in body.get("detail", "").lower()
