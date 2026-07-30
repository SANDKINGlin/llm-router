"""API Key management module for secure key operations."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Optional
from functools import wraps
from fastapi import HTTPException, Request
import sqlite3

logger = logging.getLogger(__name__)


def require_permission(permission: str):
    """权限检查装饰器 - 验证用户是否有指定权限。"""
    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            # TODO: 从JWT token中提取用户权限
            # 当前简化版本：允许所有操作
            # 等Section 4实施后需要验证真实权限
            return await func(*args, **kwargs)
        return wrapper
    return decorator


class KeyManager:
    """密钥管理器 - 处理密钥的存储、掩码、显示、审计等功能。"""

    def __init__(self, db_path: str = "/home/lin/projects/llm-router/data/keys.db"):
        self.db_path = db_path

    def get_connection(self) -> sqlite3.Connection:
        """Get database connection with row factory."""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def mask_key(self, key: str, show_chars: int = 4) -> str:
        """对密钥进行掩码处理，默认显示前4位。"""
        if not key or len(key) <= show_chars:
            return "****"
        return key[:show_chars] + "*" * (len(key) - show_chars)

    def log_key_access(self, user_id: Optional[int], provider: str, action: str, details: str = ""):
        """记录密钥访问/操作到审计日志。"""
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    INSERT INTO audit_logs (user_id, action_type, resource_type, resource_id, details, timestamp)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (user_id, action, "api_key", provider, details, datetime.now().isoformat()))
                conn.commit()
        except Exception as e:
            logger.error(f"Failed to log key access: {e}")

    def get_key_expiration_warning(self, provider: str, key_data: dict) -> Optional[str]:
        """检查密钥是否即将过期（30天内）。"""
        # TODO: 实现密钥过期检查逻辑
        # 需要在密钥存储中添加expires_at字段
        return None

    def get_key_history(self, provider: str, limit: int = 5) -> list[dict]:
        """获取密钥的历史变更记录。"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT action_type, details, timestamp
                FROM audit_logs
                WHERE resource_type = 'api_key' AND resource_id = ?
                ORDER BY timestamp DESC
                LIMIT ?
            """, (provider, limit))

            history = []
            for row in cursor.fetchall():
                history.append({
                    "action": row["action_type"],
                    "details": row["details"],
                    "timestamp": row["timestamp"]
                })

            return history


# Global key manager instance
key_manager = KeyManager()
