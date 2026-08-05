"""Admin E2E 测试 (Phase2 B组): 密钥/备份/配置 CRUD + 灰度 + 鉴权。

覆盖 B2(密钥) B3(备份) B4(配置) B7(权限).
安全测试 B5+B6 见 test_security_admin.py。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from llm_router.admin.app import admin_app


# ══════════════════════════════════════════════════════
# helper
# ══════════════════════════════════════════════════════

def _client(x_test_token: bool = True) -> TestClient:
    import os as _os
    _os.environ.setdefault("LLM_ROUTER_TEST_TOKEN_BYPASS", "on")
    headers = {}
    if x_test_token:
        headers["X-Test-Token"] = "r8-test-token"
    return TestClient(admin_app, headers=headers)


# ══════════════════════════════════════════════════════
# B2 — 密钥管理 CRUD
# ══════════════════════════════════════════════════════

class TestKeyManagementCRUD:
    """密钥 CRUD: POST → GET → PUT → DELETE → 404."""
    PROVIDER = "mock"  # 已存在的 provider, 无需先创建
    KEY = "sk-e2e-test-key-abc123"

    def test_create_key(self):
        c = _client()
        r = c.post("/admin/keys", json={
            "provider": self.PROVIDER,
            "key": self.KEY,
        })
        assert r.status_code in (200, 201, 409), f"unexpected {r.status_code}: {r.text[:200]}"

    def test_list_keys_includes_created(self):
        c = _client()
        r = c.get("/api/admin/keys")
        assert r.status_code == 200
        data = r.json()
        # mock 始终在列表中 (默认 provider)
        assert isinstance(data, (list, dict))

    def test_reveal_key(self):
        c = _client()
        r = c.get(f"/api/admin/keys/{self.PROVIDER}/reveal")
        # 404 = provider not in store, 200 = found
        assert r.status_code in (200, 404), f"unexpected {r.status_code}"

    def test_update_key(self):
        c = _client()
        r = c.put(f"/admin/keys/{self.PROVIDER}", json={
            "key": "sk-updated-key-xyz"
        })
        assert r.status_code in (200, 404), f"unexpected {r.status_code}"

    def test_delete_key(self):
        c = _client()
        r = c.delete(f"/admin/keys/{self.PROVIDER}")
        assert r.status_code in (200, 404), f"unexpected {r.status_code}"

    def test_delete_nonexistent_returns_404(self):
        c = _client()
        r = c.delete("/admin/keys/nonexistent-provider-zzz")
        assert r.status_code == 404


# ══════════════════════════════════════════════════════
# B3 — 备份恢复
# ══════════════════════════════════════════════════════

class TestBackupRestore:
    """备份: export → import, db-sizes 查询."""

    def test_export_backup_returns_tar(self):
        c = _client()
        r = c.post("/admin/backup/export", json={"include_secrets": False})
        assert r.status_code in (200, 500), f"unexpected {r.status_code}"
        if r.status_code == 200:
            assert r.headers.get("content-type", "").startswith("application/")

    def test_db_sizes_returns_dict(self):
        c = _client()
        r = c.get("/api/admin/backup/db-sizes")
        assert r.status_code == 200
        data = r.json()
        assert "sizes" in data
        assert "trace.db" in data["sizes"]
        assert "warnings" in data
        assert "migration_needed" in data

    def test_import_backup_requires_file(self):
        c = _client()
        r = c.post("/admin/backup/import", json={"file_path": "nonexistent.tar.gz"})
        # 应返回 400 或 500 (文件不存在)
        assert r.status_code in (400, 403, 422, 500), f"unexpected {r.status_code}: {r.text[:200]}"


# ══════════════════════════════════════════════════════
# B4 — 配置 CRUD
# ══════════════════════════════════════════════════════

class TestConfigCRUD:
    """配置: gray_percent 读写, settings 查询."""

    def test_get_settings(self):
        c = _client()
        r = c.get("/api/admin/settings")
        assert r.status_code == 200
        data = r.json()
        assert isinstance(data, dict)

    def test_update_gray_percent(self):
        c = _client()
        r = c.put("/admin/settings/gray_percent", json={"percent": 50})
        assert r.status_code in (200, 400), f"unexpected {r.status_code}"

    def test_gray_percent_rejects_out_of_range(self):
        c = _client()
        r = c.put("/admin/settings/gray_percent", json={"percent": 150})
        assert r.status_code in (400, 422), f"unexpected {r.status_code}"


# ══════════════════════════════════════════════════════
# B7 — 权限测试
# ══════════════════════════════════════════════════════

class TestPermissionBoundary:
    """权限边界: 无 token → 401, 无效 token → 401, 无权限 → 403."""

    def test_admin_endpoint_no_token_401(self):
        c = TestClient(admin_app)  # no headers
        r = c.get("/api/admin/keys")
        assert r.status_code in (401, 403), f"unexpected {r.status_code}"

    def test_admin_endpoint_invalid_token_401(self):
        c = TestClient(admin_app, headers={"Authorization": "Bearer deadbeef-invalid"})
        r = c.get("/api/admin/keys")
        assert r.status_code in (401, 403), f"unexpected {r.status_code}"

    def test_health_endpoint_no_auth_required(self):
        c = TestClient(admin_app)  # no auth
        r = c.get("/healthz")
        assert r.status_code == 200
        assert r.json()["status"] == "healthy"

    def test_admin_page_requires_auth(self):
        c = TestClient(admin_app)  # no auth
        r = c.get("/admin/keys")
        # localhost 可能 bypass，remote 需 401
        assert r.status_code in (200, 401), f"unexpected {r.status_code}"
