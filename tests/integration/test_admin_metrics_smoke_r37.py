"""R37: 5 个 metrics 端点端到端 smoke test (R36 实施后第一次端到端验证).

范围: 只验证 R36 改的 5 个 metrics 端点 (circuit-breakers, rate-limits, trends, errors, latency).
不含 health/status — 该端点 admin/app.py:2078 有 pre-existing 'Connection' object is not callable bug,
跟 R36 无关, 留给 R38+ 修.

URL 来源: monitoring.html 实际 grep 抓取:
  fetch():
    /api/admin/metrics/trends
    /api/admin/metrics/errors
    /api/admin/metrics/latency
  hx-get:
    /api/admin/metrics/circuit-breakers
    /api/admin/metrics/rate-limits
"""
import os, sqlite3, tempfile, hashlib, re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

REPO = Path("/home/lin/projects/llm-router-wt-r37-smoke")
SCHEMA = (REPO / "src/llm_router/admin/migrations/001_initial_schema.sql").read_text()

# monitoring.html 真实引用的 5 个 metrics URL
MONITORING_HTML_URLS = {
    "trends": "/api/admin/metrics/trends",
    "errors": "/api/admin/metrics/errors",
    "latency": "/api/admin/metrics/latency",
    "circuit_breakers": "/api/admin/metrics/circuit-breakers",
    "rate_limits": "/api/admin/metrics/rate-limits",
}

# R36/R35 端点真实 schema 字段
EXPECTED_FIELDS = {
    "trends": {"trends", "total_24h", "data_source"},
    "errors": {"errors", "data_source"},
    "latency": {"latency", "data_source"},
    "circuit_breakers": {"circuit_breakers", "data_source"},
    "rate_limits": {"total_errors_24h", "providers", "providers_by_result", "data_source"},
}


@pytest.fixture
def live_data_dir(monkeypatch):
    """跟 R36 一样的真 data 准备: 5 db + trace_hot + circuit_keys 真数据."""
    t = Path(tempfile.mkdtemp(prefix="r37-smoke-"))
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
    conn = sqlite3.connect(t / "trace.db")
    conn.execute("""CREATE TABLE trace_hot (
        trace_id TEXT PRIMARY KEY, correlation_id TEXT NOT NULL,
        idempotency_key TEXT NOT NULL UNIQUE, provider TEXT NOT NULL,
        result TEXT, latency REAL, created_at TEXT NOT NULL, arm TEXT)""")
    for i, (p, r, lat) in enumerate([
        ("openai", "success", 50), ("openai", "error", 200),
        ("anthropic", "success", 80), ("anthropic", "rate_limited", 0),
        ("mock", "error", 150),
    ]):
        conn.execute("INSERT INTO trace_hot (trace_id, correlation_id, idempotency_key, provider, result, latency, created_at) VALUES (?,?,?,?,?,?,datetime('now'))",
                     (f"t{i}", f"c{i}", f"k{i}", p, r, lat))
    conn.commit(); conn.close()
    conn = sqlite3.connect(t / "circuit.db")
    conn.execute("""CREATE TABLE circuit_keys (
        provider TEXT, key TEXT, state TEXT, hard_failures INTEGER, soft_failures INTEGER,
        half_open_failures INTEGER, opened_at REAL, next_probe_at REAL,
        probe_in_flight INTEGER, PRIMARY KEY (provider, key))""")
    conn.execute("INSERT INTO circuit_keys VALUES ('openai','k1','closed',0,0,0,NULL,NULL,0), ('anthropic','k2','half_open',2,1,1,12345.0,12355.0,1)")
    conn.commit(); conn.close()
    return t


@pytest.fixture
def client(live_data_dir):
    sys_path = str(REPO / "src")
    import sys
    sys.path.insert(0, sys_path)
    from llm_router.admin.app import admin_app
    c = TestClient(admin_app)
    r = c.post("/admin/auth/login", json={"username": "admin", "password": "admin"})
    token = r.json()["token"]
    return c, {"Authorization": f"Bearer {token}"}


def _verify_endpoint(c, h, label, path, expected):
    """真请求端点, 校验 200 + schema 字段."""
    r = c.get(path, headers=h)
    assert r.status_code == 200, f"{label} ({path}) 期望 200, 实际 {r.status_code}: {r.text[:200]}"
    body = r.json()
    actual_fields = set(body.keys())
    missing = expected - actual_fields
    assert not missing, f"{label} ({path}) 缺字段 {missing}, 实际 {actual_fields}"
    return body


def test_circuit_breakers_smoke(client):
    c, h = client
    body = _verify_endpoint(c, h, "circuit-breakers",
                            MONITORING_HTML_URLS["circuit_breakers"],
                            EXPECTED_FIELDS["circuit_breakers"])
    by_p = {b["provider"]: b for b in body["circuit_breakers"]}
    assert by_p["openai"]["state"] == "closed"
    assert by_p["openai"]["hard_failures"] == 0
    assert by_p["anthropic"]["state"] == "half_open"
    assert by_p["anthropic"]["hard_failures"] == 2
    assert by_p["anthropic"]["probe_in_flight"] == 1
    for p in by_p.values():
        assert "soft_failures" in p
        assert "half_open_failures" in p
        assert "opened_at" in p
        assert "next_probe_at" in p


def test_rate_limits_smoke(client):
    c, h = client
    body = _verify_endpoint(c, h, "rate-limits",
                            MONITORING_HTML_URLS["rate_limits"],
                            EXPECTED_FIELDS["rate_limits"])
    assert "total_429" not in body, "R36 后不应该再有 total_429 字段"
    assert body["total_errors_24h"] == 3
    assert body["providers"]["openai"] == 1
    assert body["providers"]["anthropic"] == 1
    assert body["providers"]["mock"] == 1
    assert body["providers_by_result"]["openai"]["error"] == 1
    assert body["providers_by_result"]["anthropic"]["rate_limited"] == 1


def test_trends_smoke(client):
    c, h = client
    body = _verify_endpoint(c, h, "trends",
                            MONITORING_HTML_URLS["trends"],
                            EXPECTED_FIELDS["trends"])
    assert body["total_24h"] == 5
    assert body["data_source"] == "trace.db"
    assert len(body["trends"]) == 24
    assert any(b["count"] > 0 for b in body["trends"])


def test_errors_smoke(client):
    c, h = client
    body = _verify_endpoint(c, h, "errors",
                            MONITORING_HTML_URLS["errors"],
                            EXPECTED_FIELDS["errors"])
    by_p = {e["provider"]: e for e in body["errors"]}
    assert by_p["openai"]["total"] == 2
    assert by_p["openai"]["errors"] == 1
    assert abs(by_p["openai"]["error_rate"] - 0.5) < 0.01


def test_latency_smoke(client):
    c, h = client
    body = _verify_endpoint(c, h, "latency",
                            MONITORING_HTML_URLS["latency"],
                            EXPECTED_FIELDS["latency"])
    assert len(body["latency"]) == 192
    assert any(c["p95_ms"] > 0 for c in body["latency"])


def test_monitoring_html_urls_actually_exist():
    """防 monitoring.html 加新端点后 smoke test 漏测 — 实际 grep 抓 URL 跟测试定义对得上."""
    html = (REPO / "src/llm_router/ui/templates/monitoring.html").read_text()
    # 注意字符类里 - 必须 escape 或放最后, 这里用 \- 放中间
    found = set(re.findall(r"/api/admin/[a-zA-Z0-9\-_/]+", html))
    expected_in_monitoring = {
        "/api/admin/metrics/trends",
        "/api/admin/metrics/errors",
        "/api/admin/metrics/latency",
        "/api/admin/metrics/circuit-breakers",
        "/api/admin/metrics/rate-limits",
    }
    missing_in_monitoring = expected_in_monitoring - found
    assert not missing_in_monitoring,         f"smoke test 期望端点 {missing_in_monitoring} 不在 monitoring.html 中 (监控页可能已改名)"
    extra_in_test = set(MONITORING_HTML_URLS.values()) - expected_in_monitoring
    assert not extra_in_test, f"smoke test 写了未在监控页引用的端点: {extra_in_test}"
