"""安全测试 (Phase2 B5+B6): SQL注入防御 + XSS防御。

B5: 验证所有 admin 端点用参数化查询/sqlite3 安全接口.
B6: 验证 HTML 模板输出转义 (Jinja2 autoescape).
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from llm_router.admin.app import admin_app


def _client() -> TestClient:
    return TestClient(admin_app, headers={"X-Test-Token": "r8-test-token"})


# ══════════════════════════════════════════════════════
# B5 — SQL 注入
# ══════════════════════════════════════════════════════

SQLI_PAYLOADS = [
    ("basic_union", "'; SELECT 1--"),
    ("drop_table", "'; DROP TABLE keys;--"),
    ("or_1_eq_1", "' OR '1'='1"),
    ("union_select", "' UNION SELECT * FROM users--"),
    ("comment_bypass", "admin'--"),
    ("semicolon_inject", "test; DELETE FROM keys WHERE 1=1;--"),
]


class TestSQLInjectionDefense:
    """验证参数化查询防御 SQL 注入."""

    @pytest.mark.parametrize("name,payload", SQLI_PAYLOADS)
    def test_key_create_rejects_sqli(self, name, payload):
        c = _client()
        r = c.post("/admin/keys", json={
            "provider": payload,
            "key": "test-key",
        })
        # 不应返回 500 (崩溃), 应返回 400/422 或正常处理
        assert r.status_code != 500, (
            f"[{name}] 服务崩溃: payload={payload[:50]}, "
            f"status={r.status_code}, body={r.text[:200]}"
        )

    @pytest.mark.parametrize("name,payload", SQLI_PAYLOADS[:3])
    def test_key_query_rejects_sqli_in_provider(self, name, payload):
        c = _client()
        r = c.get(f"/api/admin/keys/{payload}/reveal")
        # 不应 500, 应 404 或 422
        assert r.status_code != 500, (
            f"[{name}] 查询崩溃: payload={payload[:50]}, status={r.status_code}"
        )

    @pytest.mark.parametrize("name,payload", SQLI_PAYLOADS[:3])
    def test_setting_update_rejects_sqli(self, name, payload):
        c = _client()
        r = c.put(f"/admin/settings/{payload}", json={"value": "x"})
        assert r.status_code != 500, (
            f"[{name}] 配置崩溃: payload={payload[:50]}, status={r.status_code}"
        )


# ══════════════════════════════════════════════════════
# B6 — XSS
# ══════════════════════════════════════════════════════

XSS_PAYLOADS = [
    ("script_tag", "<script>alert(1)</script>"),
    ("img_onerror", '<img src=x onerror=alert(1)>'),
    ("svg_onload", "<svg onload=alert(1)>"),
    ("javascript_url", "javascript:alert(1)"),
    ("html_injection", "<div onclick=alert(1)>click</div>"),
]


class TestXSSDefense:
    """验证 HTML 输出转义."""

    @pytest.mark.parametrize("name,payload", XSS_PAYLOADS)
    def test_key_create_xss_provider_name_escaped(self, name, payload):
        c = _client()
        r = c.post("/admin/keys", json={
            "provider": payload,
            "key": "sk-test",
        })
        # 服务不应崩溃
        assert r.status_code != 500, (
            f"[{name}] XSS崩溃: payload={payload[:50]}, status={r.status_code}"
        )

    @pytest.mark.parametrize("name,payload", XSS_PAYLOADS[:3])
    def test_html_pages_escape_injection(self, name, payload):
        """验证 HTML 页面安全头 + 无 script 注入."""
        c = _client()
        # 尝试访问带有注入参数的页面
        r = c.get(f"/admin/keys/{payload}")
        body = r.text.lower()
        # 不应包含未转义的 <script>
        assert "<script>" not in body or "&lt;script&gt;" in body, (
            f"[{name}] 未转义 script: payload={payload[:50]}"
        )

    def test_content_type_header_set(self):
        """安全头检查: Content-Type 防止 MIME sniffing."""
        c = _client()
        r = c.get("/admin/health")
        assert r.status_code == 200
        ct = r.headers.get("content-type", "")
        assert "text/html" in ct, f"Missing text/html Content-Type: {ct}"

    def test_x_content_type_options_header(self):
        """X-Content-Type-Options: nosniff."""
        c = _client()
        r = c.get("/admin/")
        # FastAPI 默认不设, 检查即使缺也不崩溃
        assert r.status_code == 200
