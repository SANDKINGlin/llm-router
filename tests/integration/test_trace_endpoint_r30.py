"""R30: trace/{correlation_id} 端点回归测试 (治本 admin/app.py:2147 stub).

R7 标完 admin-dashboard spec trace链路追踪 requirement (SHALL 按 correlation_id 查链路),
但端点 8-11 12:50 仍 stub 永远 not_found. R30 治本合规化.

R30 改:
- admin/app.py: 加 _get_trace_store() 工厂 + get_trace() 端点从 stub 改 TraceStore.get_chain 真查
- 暴露 7 字段给前端 (trace_id, provider, result, latency, cost, created_at, parent_correlation_id)
- 返 {correlation_id, hops, count, not_found}

注: trace 表数据来自 R30 fixture 写入 (3 个 cascade fallback hop + 1 个独立 hop).
   测试验 schema 字段名 + 顺序 + count + not_found 标志.
"""
import os, sqlite3, tempfile, hashlib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

REPO = Path("/home/lin/projects/llm-router-wt-r30-trace-endpoint")
SCHEMA = (REPO / "src/llm_router/admin/migrations/001_initial_schema.sql").read_text()
TRACE_SCHEMA = """
CREATE TABLE IF NOT EXISTS trace (
    trace_id              TEXT PRIMARY KEY,
    correlation_id        TEXT NOT NULL,
    parent_correlation_id TEXT,
    idempotency_key       TEXT NOT NULL UNIQUE,
    provider              TEXT NOT NULL,
    result                TEXT,
    latency               REAL,
    cost                  REAL,
    reward                REAL,
    reward_committed_at   TEXT,
    hop_attribution       TEXT,
    created_at            TEXT NOT NULL,
    arm                   TEXT
);
CREATE INDEX IF NOT EXISTS idx_trace_correlation ON trace(correlation_id);
"""


@pytest.fixture
def live_data_dir(monkeypatch):
    t = Path(tempfile.mkdtemp(prefix="r30-"))
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
    # trace.db schema (TraceStore.init() 也会跑, 但 fixture 显式建保证)
    tconn = sqlite3.connect(t / "trace.db")
    tconn.executescript(TRACE_SCHEMA)
    tconn.commit(); tconn.close()
    return t


@pytest.fixture
def client(live_data_dir):
    import sys
    sys.path.insert(0, str(REPO / "src"))
    from llm_router.admin.app import admin_app
    c = TestClient(admin_app)
    r = c.post("/admin/auth/login", json={"username": "admin", "password": "admin"})
    return c, {"Authorization": f"Bearer {r.json()['token']}"}


def _insert_trace(db_path: Path, trace_id: str, correlation_id: str, parent: str | None,
                  provider: str, result: str, latency: float, cost: float, ts: str) -> None:
    """helper: 写 1 条 trace 记录 (跟 schema 一致, 走幂等键防 UNIQUE 冲突)."""
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO trace (trace_id, correlation_id, parent_correlation_id, "
        "idempotency_key, provider, result, latency, cost, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (trace_id, correlation_id, parent, f"idem-{trace_id}", provider, result, latency, cost, ts),
    )
    conn.commit(); conn.close()


def test_trace_returns_not_found_for_empty_correlation_id(client, live_data_dir):
    """R30: 没写入任何 trace 时, 端点返 not_found=True + hops=[] + count=0."""
    c, h = client
    r = c.get("/api/admin/traces/nonexistent-corr-id-xyz", headers=h)
    assert r.status_code == 200, f"期望 200, 实际 {r.status_code}: {r.text[:200]}"
    body = r.json()
    assert body["correlation_id"] == "nonexistent-corr-id-xyz"
    assert body["hops"] == []
    assert body["count"] == 0
    assert body["not_found"] is True


def test_trace_returns_single_hop(client, live_data_dir):
    """R30: 写 1 hop, 端点返 1 hop + not_found=False + count=1."""
    db = live_data_dir / "trace.db"
    _insert_trace(db, "t1", "corr-1", None, "openai", "ok", 0.42, 0.001, "2026-08-11T10:00:00Z")

    c, h = client
    r = c.get("/api/admin/traces/corr-1", headers=h)
    assert r.status_code == 200
    body = r.json()
    assert body["correlation_id"] == "corr-1"
    assert body["count"] == 1
    assert body["not_found"] is False
    assert len(body["hops"]) == 1

    hop = body["hops"][0]
    assert hop["trace_id"] == "t1"
    assert hop["provider"] == "openai"
    assert hop["result"] == "ok"
    assert hop["latency"] == 0.42
    assert hop["cost"] == 0.001
    assert hop["parent_correlation_id"] is None
    assert hop["created_at"] == "2026-08-11T10:00:00Z"


def test_trace_returns_cascade_fallback_chain_in_order(client, live_data_dir):
    """R30: 3 hop (cascade fallback), 端点按 created_at 升序返."""
    db = live_data_dir / "trace.db"
    # 故意乱序写入: t2 在 t1 之前, t3 在中间 — 端点应按 created_at 升序 (t1 -> t2 -> t3)
    _insert_trace(db, "t2", "corr-cascade", "t1", "anthropic", "rate_limited", 0.5, 0.002, "2026-08-11T10:00:01Z")
    _insert_trace(db, "t3", "corr-cascade", "t2", "google", "ok", 0.3, 0.003, "2026-08-11T10:00:02Z")
    _insert_trace(db, "t1", "corr-cascade", None, "openai", "error", 1.2, 0.001, "2026-08-11T10:00:00Z")

    c, h = client
    r = c.get("/api/admin/traces/corr-cascade", headers=h)
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 3
    assert body["not_found"] is False

    # 验顺序: t1 -> t2 -> t3 (按 created_at ASC)
    hops = body["hops"]
    assert [h["trace_id"] for h in hops] == ["t1", "t2", "t3"]
    assert hops[0]["provider"] == "openai"
    assert hops[1]["provider"] == "anthropic"
    assert hops[2]["provider"] == "google"
    # parent_correlation_id 链: t1=None, t2=t1, t3=t2
    assert hops[0]["parent_correlation_id"] is None
    assert hops[1]["parent_correlation_id"] == "t1"
    assert hops[2]["parent_correlation_id"] == "t2"


def test_trace_requires_auth(client, live_data_dir, monkeypatch):
    """R30: 无 auth token 时, 端点 401 (跟其他 admin 端点一致).

    注: 通用 fixture 设 LLM_ROUTER_TEST_TOKEN_BYPASS=on 让 happy path 测试通过.
    本 test 显式关掉 bypass 验真鉴权门生效.
    """
    monkeypatch.setenv("LLM_ROUTER_TEST_TOKEN_BYPASS", "off")
    c, _ = client
    r = c.get("/api/admin/traces/corr-any")
    assert r.status_code == 401, f"期望 401 (无 auth + bypass off), 实际 {r.status_code}: {r.text[:200]}"
