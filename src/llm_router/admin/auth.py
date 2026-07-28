"""Admin WebUI认证鉴权和审计日志系统。"""
from __future__ import annotations

import os
import time
from datetime import datetime
from typing import Callable

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

# 简单JWT token生成（生产环境建议用python-jose）
import hashlib
import hmac


class AuthMiddleware(BaseHTTPMiddleware):
    """认证中间件：localhost默认暴露，远程需要Bearer Token。"""

    def __init__(self, app, secret_key: str | None = None):
        super().__init__(app)
        self.secret_key = secret_key or os.environ.get("ADMIN_SECRET_KEY", "dev-secret-key")

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        # 跳过健康检查和登录端点
        # X1 fix (2026-07-28 三方真调共识): strip mount prefix 让白名单同时支持
        #   - admin_subapp 独立跑 :8790 (mount_prefix="")
        #   - admin_subapp mount 进 :8789 /admin (mount_prefix="/admin", url.path=/admin/admin/auth/login)
        mount_prefix = request.scope.get("root_path", "") or ""
        raw_path = request.url.path
        sub_path = raw_path[len(mount_prefix):] if mount_prefix and raw_path.startswith(mount_prefix) else raw_path
        if sub_path in ["/healthz", "/admin/auth/login"] or raw_path.endswith("/admin/auth/login"):
            return await call_next(request)

        # localhost默认暴露
        client_host = request.client.host if request.client else ""
        if client_host in ("127.0.0.1", "::1", "localhost"):
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
        """验证Bearer Token（简化版，24h有效期）。"""
        try:
            # token格式：timestamp:signature
            parts = token.split(":")
            if len(parts) != 2:
                return False

            timestamp = int(parts[0])
            signature = parts[1]

            # 检查过期（24小时）
            if time.time() - timestamp > 86400:
                return False

            # 验证签名
            expected = hmac.new(
                self.secret_key.encode(),
                str(timestamp).encode(),
                hashlib.sha256
            ).hexdigest()

            return hmac.compare_digest(signature, expected)
        except (ValueError, IndexError):
            return False


def generate_token(secret_key: str | None = None) -> str:
    """生成Bearer Token（24h有效）。"""
    key = secret_key or os.environ.get("ADMIN_SECRET_KEY", "dev-secret-key")
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
