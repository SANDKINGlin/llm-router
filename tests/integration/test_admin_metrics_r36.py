"""R36: 5 个 metrics 端点真实数据路径 + ENV 注入.

验证:
  - circuit-breakers 端点读 circuit_keys 真数据
  - rate-limits 端点扩 query (含 result=error)
  - trends/errors/latency 端点走 LLM_ROUTER_DATA_DIR
  - 5 端点都不打 5xx, 都返结构化 JSON
"""
import os, sqlite3, tempfile, json, hashlib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

REPO = Path("/home/lin/projects/llm-router")  # R28c: was wt-r36 (WT deleted, same as R38 precedent)
SCHEMA = (REPO / "src/llm_router/admin/migrations/001_initial_schema.sql").read_text()


@pytest.fixture
def tdir(monkeypatch):
    """临时 data 目录 + 6 个 db + trace_hot 假数据 + circuit_keys 假数据."""
    t = Path(tempfile.mkdtemp(prefix="r36-test-"))
    monkeypatch.setenv("LLM_ROUTER_DATA_DIR", str(t))
    monkeypatch.setenv("ADMIN_SECRET_KEY", "dev-secret-key-32bytes-aaaa-1111")
    monkeypatch.setenv("LLM_ROUTER_TEST_TOKEN_BYPASS", "on")
    for n in ("trace.db","ledger.db","circuit.db","health.db","keys.db","scanner.db"):
        c = sqlite3.connect(t / n); c.execute("PRAGMA journal_mode=WAL"); c.commit(); c.close()
    # keys.db schema + admin
    conn = sqlite3.connect(t / "keys.db")
    conn.executescript(SCHEMA)
    conn.execute("UPDATE user_roles SET password_hash=? WHERE username='admin'",
                 (hashlib.sha256(b"admin").hexdigest(),))
    conn.commit(); conn.close()
    # trace.db trace_hot 表 + 数据 (混合 success/error/rate_limited)
    conn = sqlite3.connect(t / "trace.db")
    conn.execute("""CREATE TABLE trace_hot (
        trace_id TEXT PRIMARY KEY, correlation_id TEXT NOT NULL,
        idempotency_key TEXT NOT NULL UNIQUE, provider TEXT NOT NULL,
        result TEXT, latency REAL, created_at TEXT NOT NULL, arm TEXT)""")
    for i, (p, r, lat) in enumerate([
        ("mock_a", "success", 50), ("mock_a", "success", 60),
        ("mock_a", "error", 200), ("mock_a", "rate_limited", 0),
        ("mock_b", "success", 40), ("mock_b", "error", 180),
    ]):
        conn.execute("INSERT INTO trace_hot (trace_id, correlation_id, idempotency_key, provider, result, latency, created_at) VALUES (?,?,?,?,?,?,datetime('now'))",
                     (f"t{i}", f"c{i}", f"k{i}", p, r, lat))
    conn.commit(); conn.close()
    # circuit.db
    conn = sqlite3.connect(t / "circuit.db")
    conn.execute("""CREATE TABLE circuit_keys (
        provider TEXT, key TEXT, state TEXT, hard_failures INTEGER, soft_failures INTEGER,
        half_open_failures INTEGER, opened_at REAL, next_probe_at REAL,
        probe_in_flight INTEGER, PRIMARY KEY (provider, key))""")
    conn.execute("INSERT INTO circuit_keys VALUES ('mock_a','k1','closed',0,0,0,NULL,NULL,0), ('mock_b','k2','open',3,1,2,12345.6,12355.6,1)")
    conn.commit(); conn.close()
    return t


@pytest.fixture
def client(tdir):
    sys_path = str(REPO / "src")
    import sys
    sys.path.insert(0, sys_path)
    from llm_router.admin.app import admin_app
    c = TestClient(admin_app)
    r = c.post("/admin/auth/login", json={"username": "admin", "password": "admin"})
    token = r.json()["token"]
    return c, {"Authorization": f"Bearer {token}"}


def test_circuit_breakers_real_data(client):
    c, h = client
    r = c.get("/api/admin/metrics/circuit-breakers", headers=h)
    assert r.status_code == 200
    body = r.json()
    assert body["data_source"] == "circuit.db"
    cb = {b["provider"]: b for b in body["circuit_breakers"]}
    assert cb["mock_a"]["state"] == "closed"
    assert cb["mock_a"]["hard_failures"] == 0
    assert cb["mock_b"]["state"] == "open"
    assert cb["mock_b"]["hard_failures"] == 3
    assert cb["mock_b"]["probe_in_flight"] == 1


def test_rate_limits_includes_error(client):
    c, h = client
    r = c.get("/api/admin/metrics/rate-limits", headers=h)
    assert r.status_code == 200
    body = r.json()
    # mock_a 1 error + 1 rate_limited = 2; mock_b 1 error = 1; total = 3
    assert body["total_errors_24h"] == 3
    assert body["providers"]["mock_a"] == 2
    assert body["providers"]["mock_b"] == 1
    assert body["providers_by_result"]["mock_a"]["error"] == 1
    assert body["providers_by_result"]["mock_a"]["rate_limited"] == 1


def test_trends_real_data(client):
    c, h = client
    r = c.get("/api/admin/metrics/trends", headers=h)
    assert r.status_code == 200
    body = r.json()
    assert body["total_24h"] == 6
    assert body["data_source"] == "trace.db"
    # 6 个 trace 都 in 24h, 至少有一个 bucket > 0
    assert any(b["count"] > 0 for b in body["trends"])


def test_errors_real_data(client):
    c, h = client
    r = c.get("/api/admin/metrics/errors", headers=h)
    assert r.status_code == 200
    body = r.json()
    assert body["data_source"] == "trace.db"
    by_p = {e["provider"]: e for e in body["errors"]}
    # mock_a 2 success + 2 not-success = 50% error rate
    assert by_p["mock_a"]["total"] == 4
    assert by_p["mock_a"]["errors"] == 2
    assert abs(by_p["mock_a"]["error_rate"] - 0.5) < 0.01
    # mock_b 1 success + 1 not-success = 50%
    assert by_p["mock_b"]["total"] == 2
    assert by_p["mock_b"]["errors"] == 1


def test_latency_real_data(client):
    c, h = client
    r = c.get("/api/admin/metrics/latency", headers=h)
    assert r.status_code == 200
    body = r.json()
    assert body["data_source"] == "trace.db"
    # 6 trace, 5 个有 latency (rate_limited=0 skip), p95 至少有一个 cell > 0
    assert any(c["p95_ms"] > 0 for c in body["latency"])
