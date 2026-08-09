"""Enhanced authentication and authorization module for admin system."""
from __future__ import annotations

import logging
import json
from datetime import datetime, timedelta
from typing import Optional, Dict, Any
from functools import wraps
from fastapi import HTTPException, Request
import sqlite3
import hashlib
import os
from pathlib import Path

logger = logging.getLogger(__name__)

# JWT配置
SECRET_KEY = "llm-router-admin-secret-key-2026"  # TODO: 移到环境变量
ALGORITHM = "HS256"
TOKEN_EXPIRATION_HOURS = 24


class EnhancedAuthManager:
    """增强认证管理器 - 处理JWT token、权限验证、会话管理。"""

    def __init__(self, db_path: str | Path | None = None):
        data_dir = Path(
            os.environ.get(
                "LLM_ROUTER_DATA_DIR",
                str(Path(__file__).resolve().parents[3] / "data"),
            )
        )
        self.db_path = str(Path(db_path) if db_path is not None else data_dir / "keys.db")

    def get_connection(self) -> sqlite3.Connection:
        """Get database connection with row factory."""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def create_token(self, user_data: Dict[str, Any]) -> str:
        """创建JWT token。"""
        try:
            import jwt
            payload = {
                "user_id": user_data.get("id"),
                "username": user_data.get("username"),
                "role": user_data.get("role"),
                "permissions": user_data.get("permissions", []),
                "exp": datetime.utcnow() + timedelta(hours=TOKEN_EXPIRATION_HOURS),
                "iat": datetime.utcnow()
            }
            token = jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)
            return token
        except ImportError:
            # 如果没有pyjwt，使用简化token
            return self._create_simple_token(user_data)

    def _create_simple_token(self, user_data: Dict[str, Any]) -> str:
        """创建简化token（当pyjwt不可用时）。"""
        import time
        import hmac
        timestamp = int(time.time())
        payload = {
            "user_id": user_data.get("id"),
            "username": user_data.get("username"),
            "role": user_data.get("role"),
            "exp": timestamp + (TOKEN_EXPIRATION_HOURS * 3600)
        }
        payload_json = json.dumps(payload)
        signature = hmac.new(
            SECRET_KEY.encode(),
            payload_json.encode(),
            hashlib.sha256
        ).hexdigest()
        return f"{timestamp}:{payload_json}:{signature}"

    def verify_token(self, token: str) -> Dict[str, Any]:
        """验证JWT token。"""
        try:
            import jwt
            payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
            return payload
        except ImportError:
            return self._verify_simple_token(token)
        except jwt.ExpiredSignatureError:
            raise HTTPException(status_code=401, detail="Token已过期")
        except jwt.InvalidTokenError:
            raise HTTPException(status_code=401, detail="无效的Token")

    def _verify_simple_token(self, token: str) -> Dict[str, Any]:
        """验证简化token。"""
        try:
            parts = token.split(":")
            if len(parts) != 3:
                raise HTTPException(status_code=401, detail="无效的Token格式")

            timestamp, payload_json, signature = parts

            # 验证签名
            expected_signature = hmac.new(
                SECRET_KEY.encode(),
                payload_json.encode(),
                hashlib.sha256
            ).hexdigest()

            if not hmac.compare_digest(signature, expected_signature):
                raise HTTPException(status_code=401, detail="Token签名无效")

            payload = json.loads(payload_json)

            # 检查过期
            if datetime.utcnow().timestamp() > payload.get("exp", 0):
                raise HTTPException(status_code=401, detail="Token已过期")

            return payload
        except Exception as e:
            raise HTTPException(status_code=401, detail=f"Token验证失败: {str(e)}")

    def authenticate_user(self, username: str, password: str) -> Optional[Dict[str, Any]]:
        """验证用户凭据 (R16 加 WHERE is_active = 1 — inactive 用户不能登录)."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM user_roles WHERE username = ? AND is_active = 1",
                (username,)
            )
            user = cursor.fetchone()

            if not user:
                return None

            user_dict = dict(user)
            password_hash = hashlib.sha256(password.encode()).hexdigest()

            if user_dict["password_hash"] == password_hash:  # 简化验证
                return {
                    "id": user_dict["id"],
                    "username": user_dict["username"],
                    "role": user_dict["role"],
                    "permissions": self._parse_permissions(user_dict["permissions"])
                }

            return None

    def _parse_permissions(self, permissions_json: Optional[str]) -> list[str]:
        """解析权限JSON。"""
        if not permissions_json:
            return []
        try:
            return json.loads(permissions_json)
        except:
            return []

    def check_permission(self, user_role: str, required_permission: str) -> bool:
        """检查用户权限。"""
        permission_hierarchy = {
            "view": 1,
            "operate": 2,
            "admin": 3
        }

        user_level = permission_hierarchy.get(user_role, 0)
        required_level = permission_hierarchy.get(required_permission, 999)

        return user_level >= required_level

    def log_auth_event(self, user_id: Optional[int], event_type: str, details: str = ""):
        """记录认证事件到审计日志。"""
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    INSERT INTO audit_logs (user_id, action_type, resource_type, details, timestamp)
                    VALUES (?, ?, ?, ?, ?)
                """, (user_id, event_type, "auth", details, datetime.now().isoformat()))
                conn.commit()
        except Exception as e:
            logger.error(f"Failed to log auth event: {e}")

    def create_user(self, username: str, password: str, role: str, permissions: list[str] = None) -> int:
        """创建新用户。"""
        with self.get_connection() as conn:
            cursor = conn.cursor()

            password_hash = hashlib.sha256(password.encode()).hexdigest()
            permissions_json = json.dumps(permissions or [])

            cursor.execute("""
                INSERT INTO user_roles (username, password_hash, role, permissions, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (username, password_hash, role, permissions_json, datetime.now().isoformat(), datetime.now().isoformat()))

            user_id = cursor.lastrowid

            # 记录到审计日志
            self.log_auth_event(user_id, "CREATE_USER", f"Created user: {username} with role: {role}")

            conn.commit()
            return user_id

    def get_user(self, username: str) -> Optional[Dict[str, Any]]:
        """获取用户信息。"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM user_roles WHERE username = ?", (username,))
            user = cursor.fetchone()
            if user:
                user_dict = dict(user)
                user_dict["permissions"] = self._parse_permissions(user_dict["permissions"])
                return user_dict
            return None

    def list_users(self) -> list[Dict[str, Any]]:
        """列出所有用户。"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT id, username, role, permissions, created_at FROM user_roles")
            users = []
            for row in cursor.fetchall():
                user_dict = dict(row)
                user_dict["permissions"] = self._parse_permissions(user_dict["permissions"])
                users.append(user_dict)
            return users

    def update_user_role(self, username: str, new_role: str, permissions: list[str] = None) -> bool:
        """更新用户角色和权限。"""
        with self.get_connection() as conn:
            cursor = conn.cursor()

            permissions_json = json.dumps(permissions or []) if permissions else None

            cursor.execute("""
                UPDATE user_roles SET role = ?, permissions = ?, updated_at = ?
                WHERE username = ?
            """, (new_role, permissions_json, datetime.now().isoformat(), username))

            success = cursor.rowcount > 0

            if success:
                cursor.execute("SELECT id FROM user_roles WHERE username = ?", (username,))
                user = cursor.fetchone()
                if user:
                    self.log_auth_event(user["id"], "UPDATE_ROLE", f"Updated user {username} to role {new_role}")

            conn.commit()
            return success

    def delete_user(self, username: str) -> bool:
        """软删除用户 (R16 实施 — UPDATE is_active = 0, 保留记录).

        跟 providers 表 soft_delete 模式对齐 (is_active = 0).
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT id, is_active FROM user_roles WHERE username = ?", (username,))
            user = cursor.fetchone()
            if not user:
                return False
            if not user["is_active"]:
                # 已 inactive, 幂等返 True (但 log 不重复)
                conn.commit()
                return True

            cursor.execute(
                "UPDATE user_roles SET is_active = 0, updated_at = CURRENT_TIMESTAMP WHERE username = ?",
                (username,)
            )
            self.log_auth_event(user["id"], "DELETE_USER", f"Soft-deleted user: {username}")
            conn.commit()
            return True
            return True


# 全局增强认证管理器实例
enhanced_auth_manager = EnhancedAuthManager()


def get_current_user_enhanced(request: Request) -> Optional[Dict[str, Any]]:
    """从请求中获取当前用户信息。

    D7 fix: 加 X-Test-Token 旁路 (跟 admin/auth.py:41 AuthMiddleware 对齐),
    让集成测试构造 happy path 时不必依赖 login 拿 token (login 走 EnhancedAuthManager
    独立 secret, 不跟 AuthMiddleware secret 一致 — D2-C 已知 bug 留作后续切片).
    集成测试场景: 用 X-Test-Token bypass AuthMiddleware + get_current_user_enhanced
    同时 bypass 返 admin user, 然后 dependency_overrides 切 RBAC 角色.

    ⚠️ D7 CC 复核关切 (FAIL): 之前实现无条件返 admin role, 是新增高风险点 —
    任何能访问服务的人都可复制固定头通过回滚 RBAC. 修正:
      1. 硬门禁 = ENV flag LLM_ROUTER_TEST_TOKEN_BYPASS=on (默认 off, 生产永远 off)
      2. 软门禁 = client host ∈ {127.0.0.1, ::1, localhost, testclient} (防御意外)
      3. 三个条件必须同时满足才放行 (defense-in-depth)
    """
    # X-Test-Token 旁路 (D7 · 跟 AuthMiddleware 对齐 + 安全门禁)
    if request.headers.get("x-test-token") == "r8-test-token":
        # 硬门禁 ENV: 默认 off, 必须显式开启
        import os as _os
        if _os.environ.get("LLM_ROUTER_TEST_TOKEN_BYPASS", "").lower() != "on":
            return None  # 生产路径: 跟无 Authorization 一样返 None → RBAC 401
        # 软门禁 localhost: 即使开了 env flag, 也只允许 loopback 调用
        client_host = (request.client.host if request.client else "") or ""
        if client_host not in ("127.0.0.1", "::1", "localhost", "testclient"):
            return None
        return {
            "id": 0,
            "username": "r8-test",
            "role": "admin",
            "permissions": ["admin"],
        }

    auth_header = request.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Bearer "):
        return None

    token = auth_header.split(" ")[1]
    try:
        payload = enhanced_auth_manager.verify_token(token)
        return payload
    except:
        return None


def get_current_user_with_permission(permission: str):
    """FastAPI Depends兼容的权限检查函数。"""
    def dependency(request: Request):
        current_user = get_current_user_enhanced(request)
        if not current_user:
            raise HTTPException(status_code=401, detail="需要认证")

        user_role = current_user.get("role", "view")
        if not enhanced_auth_manager.check_permission(user_role, permission):
            raise HTTPException(status_code=403, detail=f"需要 {permission} 权限")

        return current_user

    return dependency


def get_current_user_auth(request: Request):
    """FastAPI Depends兼容的基本认证函数。"""
    current_user = get_current_user_enhanced(request)
    if not current_user:
        raise HTTPException(status_code=401, detail="需要认证")

    return current_user


def require_enhanced_permission(permission: str):
    """增强权限检查装饰器。"""
    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            # 从args中提取request
            request = None
            for arg in args:
                if isinstance(arg, Request):
                    request = arg
                    break

            if not request:
                # 如果args中没有，尝试从kwargs获取
                request = kwargs.get('request')

            if not request:
                raise HTTPException(status_code=500, detail="无法获取Request对象")

            current_user = get_current_user_enhanced(request)
            if not current_user:
                raise HTTPException(status_code=401, detail="需要认证")

            user_role = current_user.get("role", "view")
            if not enhanced_auth_manager.check_permission(user_role, permission):
                raise HTTPException(status_code=403, detail=f"需要 {permission} 权限")

            kwargs["current_user"] = current_user
            return await func(*args, **kwargs)

        # 保留类型提示供FastAPI依赖注入使用
        wrapper.__annotations__ = func.__annotations__
        return wrapper
    return decorator


def require_enhanced_auth(func):
    """增强基本认证装饰器。"""
    @wraps(func)
    async def wrapper(*args, **kwargs):
        # 从args中提取request
        request = None
        for arg in args:
            if isinstance(arg, Request):
                request = arg
                break

        if not request:
            # 如果args中没有，尝试从kwargs获取
            request = kwargs.get('request')

        if not request:
            raise HTTPException(status_code=500, detail="无法获取Request对象")

        current_user = get_current_user_enhanced(request)
        if not current_user:
            raise HTTPException(status_code=401, detail="需要认证")

        kwargs["current_user"] = current_user
        return await func(*args, **kwargs)

    # 保留类型提示供FastAPI依赖注入使用
    wrapper.__annotations__ = func.__annotations__
    return wrapper
