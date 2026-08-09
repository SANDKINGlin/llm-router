"""Admin WebUI认证鉴权和审计日志系统。"""
from __future__ import annotations

import os
import time
from datetime import datetime
from typing import Callable, Optional

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

# 简单JWT token生成（生产环境建议用python-jose）
import hashlib
import hmac


# S2 (2026-08-04): X-Test-Token 旁路统一三门禁 (单源真相)
# D7 fix (e4b974e) 在 auth.py + auth_enhanced.py 各自加 ENV flag + host 软门禁逻辑, 重复
# 抽到本 helper, 三方调用避免漂移
_TEST_TOKEN_HEADER = "x-test-token"
_TEST_TOKEN_VALUE = "r8-test-token"
_TEST_BYPASS_ENV = "LLM_ROUTER_TEST_TOKEN_BYPASS"
_TEST_BYPASS_ALLOWED_HOSTS = frozenset({"127.0.0.1", "::1", "localhost", "testclient"})


def is_test_token_bypass_allowed(request: Request) -> bool:
    """三门禁同时满足才 True (defense-in-depth):
    1. header x-test-token == "r8-test-token"
    2. ENV LLM_ROUTER_TEST_TOKEN_BYPASS=on (默认 off, 生产永远 off)
    3. client host ∈ {loopback set} (防御意外远程 abuse)
    """
    if request.headers.get(_TEST_TOKEN_HEADER) != _TEST_TOKEN_VALUE:
        return False
    if os.environ.get(_TEST_BYPASS_ENV, "").lower() != "on":
        return False
    client_host = (request.client.host if request.client else "") or ""
    return client_host in _TEST_BYPASS_ALLOWED_HOSTS


class AuthMiddleware(BaseHTTPMiddleware):
    """认证中间件：localhost默认暴露，远程需要Bearer Token。"""

    def __init__(self, app, secret_key: str | None = None):
        super().__init__(app)
        self.secret_key = secret_key or os.environ.get("ADMIN_SECRET_KEY", "dev-secret-key-32bytes-aaaa-1111")

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        # 跳过健康检查和登录端点
        # X1 fix (2026-07-28 三方真调共识): strip mount prefix 让白名单同时支持
        #   - admin_subapp 独立跑 :8790 (mount_prefix="")
        #   - admin_subapp mount 进 :8789 /admin (mount_prefix="/admin", url.path=/admin/admin/auth/login)
        mount_prefix = request.scope.get("root_path", "") or ""
        raw_path = request.url.path
        sub_path = raw_path[len(mount_prefix):] if mount_prefix and raw_path.startswith(mount_prefix) else raw_path
        # S1 (2026-08-04): admin 子应用内部路由全部依赖 Depends(get_current_user_enhanced) 自鉴权
        # (跟 D7 派 D 同步). AuthMiddleware 只挡 *远程 + 非白名单*, 子应用内路由白名单跳过.
        # 不白名单会让 AuthMiddleware 用自家 2 段 token 格式去验 EnhancedAuthManager 的 3 段 token (mounted 必然 401).
        # 白名单仅限 EnhancedAuthManager 子应用内路由 (跟 admin/app.py 注册路径一致),
        # 收窄到 /api/admin/users 等已知 endpoint 避免过宽白名单 (远程生产暴露风险)
        _enhanced_self_routes = [
            "/admin/auth/login",     # Step 4 login (X1 已加)
            "/api/admin/users",      # Step 5 mount 端到端 (S1 新加)
            "/api/admin/keys",       # 密钥管理 (sub-app 自管)
            "/api/admin/providers",  # provider 管理 (sub-app 自管)
            "/api/admin/backup",     # 备份 (sub-app 自管)
            "/api/admin/metrics",    # 监控 (sub-app 自管)
            "/metrics",              # Prometheus scrape (mount 后 /admin/metrics → 子应用 /metrics)
        ]
        if (
            sub_path == "/healthz"
            or any(sub_path == r or sub_path.startswith(r + "/") for r in _enhanced_self_routes)
            or any(raw_path.endswith(r) for r in _enhanced_self_routes)
        ):
            return await call_next(request)

        # localhost默认暴露
        client_host = request.client.host if request.client else ""
        if client_host in ("127.0.0.1", "::1", "localhost"):
            return await call_next(request)

        # Test token bypass for integration tests
        # S2 (2026-08-04): 三门禁集中到 is_test_token_bypass_allowed helper (auth_enhanced.py 也共用)
        if is_test_token_bypass_allowed(request):
            return await call_next(request)

        # 远程访问需要Bearer Token认证
        if request.url.path.startswith("/admin/"):
            auth_header = request.headers.get("Authorization", "")
            if not auth_header.startswith("Bearer "):
                return Response(status_code=401, content="Missing Bearer token")

            token = auth_header[7:]  # 去掉"Bearer "
            if not self._verify_token(token):
                return Response(status_code=401, content="Invalid or expired token")

        return await call_next(request)

    def _verify_token(self, token: str) -> bool:
        """验证Bearer Token（兼容 auth.py 简化 token + EnhancedAuthManager PyJWT token）。

        S1 (2026-08-04): D2-C WARN Step 5 根因解.
        - 简化 token (格式 ts:sig, 旧 generate_token): 保持原行为
        - EnhancedAuthManager PyJWT token (格式 base64.base64.base64, 拆分是 3 段):
          委托给 enhanced_auth_manager.verify_token 验 (PyJWT 单源)
        """
        try:
            parts = token.split(":")
            if len(parts) == 2:
                # 旧版简化 token (auth.py:generate_token 签发)
                timestamp = int(parts[0])
                signature = parts[1]

                if time.time() - timestamp > 86400:
                    return False

                expected = hmac.new(
                    self.secret_key.encode(),
                    str(timestamp).encode(),
                    hashlib.sha256
                ).hexdigest()

                return hmac.compare_digest(signature, expected)

            if len(parts) == 3 and "." not in token:
                # 可能是 EnhancedAuthManager 的 _create_simple_token 格式 (ts:payload:sig)
                # 让 EnhancedAuthManager 验 (单源真相, 避免 secret 漂移)
                try:
                    from llm_router.admin.auth_enhanced import enhanced_auth_manager
                    from fastapi import HTTPException
                    enhanced_auth_manager.verify_token(token)
                    return True
                except HTTPException:
                    return False

            # PyJWT token (3 段以 . 分隔, 形如 eyJ..)
            if token.count(".") == 2:
                try:
                    from llm_router.admin.auth_enhanced import enhanced_auth_manager
                    from fastapi import HTTPException
                    enhanced_auth_manager.verify_token(token)
                    return True
                except HTTPException:
                    return False

            return False
        except (ValueError, IndexError):
            return False


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Security headers middleware — adds standard defensive headers (R12).

    Headers added to every response:
      X-Content-Type-Options: nosniff  (prevent MIME sniffing)
      X-Frame-Options: DENY            (prevent clickjacking)
      Referrer-Policy: no-referrer     (don't leak Referer to third-party)
    """
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response


def generate_token(secret_key: str | None = None) -> str:
    """生成Bearer Token（24h有效）。"""
    key = secret_key or os.environ.get("ADMIN_SECRET_KEY", "dev-secret-key-32bytes-aaaa-1111")
    timestamp = int(time.time())

    signature = hmac.new(
        key.encode(),
        str(timestamp).encode(),
        hashlib.sha256
    ).hexdigest()

    return f"{timestamp}:{signature}"


class AuditLogger:
    """操作审计日志。"""

    def __init__(self):
        self._logs: list[dict] = []

    def log(
        self,
        user_id: str | None,
        operation: str,
        details: dict | None = None,
        result: str = "SUCCESS"
    ) -> None:
        """记录审计事件。"""
        self._logs.append({
            "timestamp": datetime.utcnow().isoformat(),
            "user_id": user_id or "anonymous",
            "operation": operation,
            "details": details or {},
            "result": result,
        })

    def query(
        self,
        limit: int = 100,
        operation: str | None = None,
        user_id: str | None = None
    ) -> list[dict]:
        """查询审计日志。"""
        filtered = self._logs

        if operation:
            filtered = [log for log in filtered if log["operation"] == operation]
        if user_id:
            filtered = [log for log in filtered if log["user_id"] == user_id]

        return filtered[-limit:]


# 全局审计日志实例
_audit_logger = AuditLogger()


def get_audit_logger() -> AuditLogger:
    """获取审计日志实例。"""
    return _audit_logger


def log_admin_operation(operation: str):
    """装饰器：自动记录管理操作。"""
    def decorator(func):
        async def wrapper(*args, **kwargs):
            user_id = kwargs.get("user_id")  # 假设从JWT token提取
            try:
                result = await func(*args, **kwargs)
                get_audit_logger().log(user_id, operation, result="SUCCESS")
                return result
            except Exception as e:
                get_audit_logger().log(user_id, operation, {"error": str(e)}, result="FAILURE")
                raise
        return wrapper
    return decorator
