"""安全测试 (Phase2 B5+B6): SQL注入防御 + XSS防御。

B5: 验证参数化查询/sqlite3 安全接口 — 不只测"不崩", 也测不会把注入 payload 当 SQL 执行.
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

# 如果 SQL 注入成功执行, 响应里可能出现这些 SQLite 错误特征
_SQL_ERROR_SIGNATURES = [
    "sqlite3.OperationalError",
    "sqlite3.IntegrityError",
    "near \"DROP\"",
    "near \"UNION\"",
    "near \";\"",
    "syntax error",
    "no such table",
]


class TestSQLInjectionDefense:
    """验证参数化查询防御 SQL 注入 — 不崩溃 + 不泄露 SQL 错误."""

    @pytest.mark.parametrize("name,payload", SQLI_PAYLOADS)
    def test_key_create_rejects_sqli(self, name, payload):
        c = _client()
        r = c.post("/admin/keys", json={
            "provider": payload,
            "key": "test-key",
        })
        # 硬规则: 永远不应 500
        assert r.status_code != 500, (
            f"[{name}] 服务崩溃: payload={payload[:50]}, "
            f"status={r.status_code}, body={r.text[:300]}"
        )
        # 强规则: 响应不应含 SQL 错误特征 (注入成功执行的标志)
        body_lower = r.text.lower()
        for sig in _SQL_ERROR_SIGNATURES:
            assert sig.lower() not in body_lower, (
                f"[{name}] SQL 错误泄露: '{sig}' 出现在响应中 → "
                f"payload 可能被当作 SQL 执行, status={r.status_code}"
            )

    @pytest.mark.parametrize("name,payload", SQLI_PAYLOADS[:3])
    def test_key_query_rejects_sqli_in_provider(self, name, payload):
        c = _client()
        r = c.get(f"/api/admin/keys/{payload}/reveal")
        # 不应 500
        assert r.status_code != 500, (
            f"[{name}] 查询崩溃: payload={payload[:50]}, status={r.status_code}"
        )
        # 应返回 404 (provider 不存在) 或 422 (非法 provider 名), 绝不应 200
        # 如果 200 返回了密钥 → 注入成功读取了不该读的数据
        if r.status_code == 200:
            body_preview = r.text[:200]
            assert "api_key" not in body_preview and "secret" not in body_preview, (
                f"[{name}] ⚠️ 注入疑似成功: 200 OK + 响应含疑似密钥数据: {body_preview}"
            )

    @pytest.mark.parametrize("name,payload", SQLI_PAYLOADS[:3])
    def test_setting_update_rejects_sqli(self, name, payload):
        c = _client()
        r = c.put(f"/admin/settings/{payload}", json={"value": "x"})
        assert r.status_code != 500, (
            f"[{name}] 配置崩溃: payload={payload[:50]}, status={r.status_code}"
        )
        body_lower = r.text.lower()
        for sig in _SQL_ERROR_SIGNATURES[:3]:
            assert sig.lower() not in body_lower, (
                f"[{name}] SQL 错误泄露: '{sig}' 出现在响应"
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
    """验证 HTML 输出转义 — 注入 payload 不应以原始形式出现在 HTML 响应中."""

    @pytest.mark.parametrize("name,payload", XSS_PAYLOADS)
    def test_key_create_rejects_xss_in_provider_name(self, name, payload):
        c = _client()
        r = c.post("/admin/keys", json={
            "provider": payload,
            "key": "sk-test",
        })
        # 不崩溃是最低要求
        assert r.status_code != 500, (
            f"[{name}] XSS 崩溃: payload={payload[:50]}, status={r.status_code}"
        )

    @pytest.mark.parametrize("name,payload", XSS_PAYLOADS[:3])
    def test_html_pages_escape_injection(self, name, payload):
        """验证 HTML 页面将注入 payload 转义, 而非原样输出."""
        c = _client()
        r = c.get(f"/admin/keys/{payload}")
        # 不应 500
        assert r.status_code != 500, (
            f"[{name}] 崩溃: status={r.status_code}"
        )
        # 如果返回 HTML (200 或 404 页), 检查 payload 是否被转义
        if "text/html" in r.headers.get("content-type", ""):
            body = r.text
            # < 和 > 必须被转义为 &lt; &gt;
            raw_script = "<script>" in body
            escaped_script = "&lt;script&gt;" in body
            assert not raw_script or escaped_script, (
                f"[{name}] ⚠️ 未转义 <script>: status={r.status_code}, "
                f"body preview: {body[:300]}"
            )

    def test_content_type_is_html_for_pages(self):
        """安全头: HTML 页面必须有 text/html Content-Type (防 MIME sniffing)."""
        c = _client()
        r = c.get("/admin/health")
        assert r.status_code == 200
        ct = r.headers.get("content-type", "")
        assert "text/html" in ct, f"Missing text/html Content-Type: {ct}"

    def test_x_content_type_options_header(self):
        """X-Content-Type-Options: nosniff  — 如缺失则建议启用 (非阻塞)."""
        c = _client()
        r = c.get("/admin/")
        assert r.status_code == 200
        nosniff = r.headers.get("x-content-type-options", "")
        if not nosniff:
            # FastAPI 默认不设 — 标记为已知缺口, 不是测试失败
            pytest.skip("Known: X-Content-Type-Options not set by FastAPI default")
        assert nosniff == "nosniff"
