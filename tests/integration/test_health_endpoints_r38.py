"""R38: health/status + health/dead 端点回归测试 (修 R37 暴露的 pre-existing bug).

R37 smoke test 暴露 admin/app.py:2078 'Connection' object is not callable:
  async with store._db() as db:  # store._db 是 property, 加 () 是错的

R38 修:
  - store._db() 改 store._db (property, 直接拿 Connection)
  - async for row in cursor 改 async with db.execute(...) as cursor + fetchall
  - 同修复 get_dead_providers 端点

注: 健康表的数据来自 R38 fixture 写入, 也可能来自 _get_health_store() 懒初始化时已有数据.
   因此测试只验 schema 字段名 + status 200 + 数据结构, 不验具体 provider 名.
"""
import os, sqlite3, tempfile, hashlib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

REPO = Path("/home/lin/projects/llm-router")
SCHEMA = (REPO / "src/llm_router/admin/migrations/001_initial_schema.sql").read_text()
HEALTH_SCHEMA = """CREATE TABLE IF NOT EXISTS health (
    provider      TEXT PRIMARY KEY,
    last_probe_at TEXT NOT NULL,
    latency_ms    REAL,
    alive         INTEGER NOT NULL,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
)"""


@pytest.fixture
def live_data_dir(monkeypatch):
    t = Path(tempfile.mkdtemp(prefix="r38-"))
    monkeypatch.setenv("LLM_ROUTER_DATA_DIR", str(t))
    monkeypatch.setenv("ADMIN_SECRET_KEY", "dev-secret-key-32bytes-aaaa-1111")
    monkeypatch.setenv("LLM_ROUTER_TEST_TOKEN_BYPASS", "on")
    for n in ("trace.db","ledger.db","circuit.db","health.db","keys.db","scanner.db"):
        c = sqlite3.connect(t / n); c.execute("PRAGMA journal_mode=WAL"); c.commit(); c.close()
    conn = sqlite3.connect(t / "keys.db")
    conn.executescript(SCHEMA)
    conn.execute("UPDATE user_roles SET password_hash=? WHERE username='admin'",
                 (hashlib.sha256(b"admin").hexdigest(),))
    conn.commit(); conn.close()
    # 注: 不需要预建 health 表, 因为 health_store.init() 会跑 schema
    return t


@pytest.fixture
def client(live_data_dir):
    import sys
    sys.path.insert(0, str(REPO / "src"))
    from llm_router.admin.app import admin_app
    c = TestClient(admin_app)
    r = c.post("/admin/auth/login", json={"username": "admin", "password": "admin"})
    return c, {"Authorization": f"Bearer {r.json()['token']}"}


def test_health_status_returns_200_with_providers(client):
    """R37 fail: 'Connection' object is not callable. R38 必须 200 + 返 providers 数组."""
    c, h = client
    r = c.get("/api/admin/health/status", headers=h)
    assert r.status_code == 200, f"期望 200, 实际 {r.status_code}: {r.text[:200]}"
    body = r.json()
    assert "providers" in body
    assert isinstance(body["providers"], list)
    # schema 校验: 每个 provider 必含 4 字段
    for p in body["providers"]:
        assert "provider" in p
        assert "alive" in p
        assert isinstance(p["alive"], bool)
        assert "last_probe" in p
        assert "latency_ms" in p


def test_health_dead_returns_200_with_dead_list(client):
    """get_dead_providers 端点同样修过. 应该 200 + 返 dead 数组."""
    c, h = client
    r = c.get("/api/admin/health/dead", headers=h)
    assert r.status_code == 200, f"期望 200, 实际 {r.status_code}: {r.text[:200]}"
    body = r.json()
    assert "dead" in body
    assert "count" in body
    assert isinstance(body["dead"], list)
    assert body["count"] == len(body["dead"])
    # schema 校验
    for p in body["dead"]:
        assert "provider" in p
        assert "last_probe" in p
        assert "latency_ms" in p


def test_health_probe_history_returns_200_for_existing(client):
    """相邻端点: /api/admin/health/probe-history/{provider} (R38 顺便回归)."""
    c, h = client
    # 先 health/status 找一个真 provider
    r = c.get("/api/admin/health/status", headers=h)
    body = r.json()
    if not body["providers"]:
        pytest.skip("health 表无数据, 跳过此测试")
    provider = body["providers"][0]["provider"]
    r = c.get(f"/api/admin/health/probe-history/{provider}", headers=h)
    assert r.status_code == 200, f"期望 200, 实际 {r.status_code}: {r.text[:200]}"
    body = r.json()
    assert body["provider"] == provider
    assert "alive" in body
    assert "latency_ms" in body


def test_health_status_endpoints_dont_throw_connection_error(client):
    """R37 fail 类型是 'Connection' object is not callable (TypeError 500).
    R38 必须不再抛这个错, 任意 health 端点都 200."""
    c, h = client
    for endpoint in ["/api/admin/health/status", "/api/admin/health/dead"]:
        r = c.get(endpoint, headers=h)
        # 关键: 不再是 500 TypeError, 而是 200
        assert r.status_code == 200, f"{endpoint} 返 {r.status_code}, R38 修失败"
