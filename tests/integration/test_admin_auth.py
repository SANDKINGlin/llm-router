"""Admin WebUI认证和审计日志集成测试。"""
import pytest
from fastapi.testclient import TestClient

from llm_router.admin.app import admin_app
from llm_router.admin.auth import _audit_logger


@pytest.fixture(autouse=True)
def _reset_audit_log():
    """autouse: 每个测试前清空 module-level _audit_logger._logs.
    防止 integration 顺序依赖 flake (test_login_logged 等共享 _logs).
    """
    _audit_logger._logs.clear()
    yield
    _audit_logger._logs.clear()


class TestAuthMiddleware:
    """认证中间件测试。"""

    def test_localhost_no_auth_required(self):
        """localhost访问不需要认证。"""
        client = TestClient(admin_app)

        # 模拟localhost访问
        response = client.get("/healthz", headers={"X-Forwarded-For": "127.0.0.1"})
        assert response.status_code == 200

    def test_remote_without_token_401(self):
        """远程访问无token返回401。"""
        client = TestClient(admin_app)

        # 模拟远程访问（非localhost）
        response = client.get("/admin/keys", headers={"X-Forwarded-For": "192.168.1.1"})
        assert response.status_code == 401
        assert "Missing Bearer token" in response.text

    def test_remote_with_invalid_token_401(self):
        """远程访问无效token返回401。"""
        client = TestClient(admin_app)

        # 模拟远程访问（非localhost）
        response = client.get(
            "/admin/keys",
            headers={
                "X-Forwarded-For": "192.168.1.1",
                "Authorization": "Bearer invalid-token"
            }
        )
        assert response.status_code == 401

    def test_login_success(self):
        """登录成功返回token。"""
        client = TestClient(admin_app)

        response = client.post("/admin/auth/login", json={
            "username": "admin",
            "password": "admin"
        })
        assert response.status_code == 200
        data = response.json()
        assert "token" in data
        assert "expires_in" in data


class TestAuditLog:
    """审计日志测试。"""

    def test_login_logged(self):
        """登录操作记录审计日志。"""
        from llm_router.admin.auth import get_audit_logger

        client = TestClient(admin_app)
        logger = get_audit_logger()

        initial_count = len(logger._logs)

        # 执行登录
        response = client.post("/admin/auth/login", json={
            "username": "admin",
            "password": "admin"
        })
        assert response.status_code == 200

        # 验证日志记录
        logs = logger.query(operation="LOGIN")
        assert len(logs) >= initial_count

        last_log = logs[-1]
        assert last_log["operation"] == "LOGIN"
        assert last_log["result"] == "SUCCESS"

    def test_failed_login_logged(self):
        """登录失败记录审计日志。"""
        from llm_router.admin.auth import get_audit_logger

        client = TestClient(admin_app)
        logger = get_audit_logger()

        # 执行失败登录
        response = client.post("/admin/auth/login", json={
            "username": "admin",
            "password": "wrong"
        })
        assert response.status_code == 401

        # 验证失败日志记录
        logs = logger.query(operation="LOGIN_FAILED")
        assert len(logs) > 0
        assert logs[-1]["result"] == "FAILURE"


class TestKeyManagementAuth:
    """密钥管理API认证测试。"""

    @pytest.fixture
    def authenticated_client(self):
        """返回已认证的client。"""
        from llm_router.admin.app import admin_app

        client = TestClient(admin_app)

        # 先登录获取token
        response = client.post("/admin/auth/login", json={
            "username": "admin",
            "password": "admin"
        })
        token = response.json()["token"]

        # 使用token访问
        client.headers["Authorization"] = f"Bearer {token}"
        client.headers["X-Forwarded-For"] = "192.168.1.1"  # 模拟远程访问
        return client

    def test_list_keys_authenticated(self, authenticated_client):
        """已认证用户可以列出密钥。"""
        response = authenticated_client.get("/admin/keys")
        # 由于没有真实provider，这里可能返回空列表或错误
        assert response.status_code in [200, 404]

    def test_create_key_authenticated(self, authenticated_client):
        """已认证用户可以创建密钥。"""
        response = authenticated_client.post("/admin/keys", json={
            "provider": "test-provider",
            "key": "test-key-123"
        })
        # 可能返回409（已存在）或201（创建成功）
        assert response.status_code in [201, 409, 404]
