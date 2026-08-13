"""API Key management module for secure key operations."""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
from fastapi import HTTPException, Request
import sqlite3

logger = logging.getLogger(__name__)


class KeyManager:
    """密钥管理器 - 处理密钥的存储、掩码、显示、审计等功能。"""

    def __init__(self, db_path: Optional[str] = None):
        # R34 治本: db_path 走 LLM_ROUTER_DATA_DIR ENV (跟 R30 _get_trace_store ENV 化 precedent 一致,
        # 治本 fixture 隔离 + 测试可走 tmp_path). 默认 fallback 保留向后兼容.
        if db_path is None:
            data_dir = os.environ.get(
                "LLM_ROUTER_DATA_DIR",
                str(Path(__file__).resolve().parents[3] / "data"),
            )
            db_path = str(Path(data_dir) / "keys.db")
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
        """检查密钥是否即将过期（30 天内）。

        R34 实施: 读 providers 表 created_at, 算 (now - created_at).days,
        跟阈值 (默认 30 天, 可 ENV KEY_EXPIRY_WARNING_DAYS 覆盖) 比,
        返 warning 字符串或 None. 0 ALTER TABLE (created_at 已有).
        跟端点 admin/app.py:329 get_key_expiration 配套 (status: warning/ok).
        """
        try:
            warning_days = int(os.environ.get("KEY_EXPIRY_WARNING_DAYS", "30"))
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT created_at FROM providers WHERE name = ?",
                    (provider,),
                )
                row = cursor.fetchone()
            if not row:
                return None  # provider 不在 providers 表, 不警告

            created_at_str = row["created_at"]
            # SQLite CURRENT_TIMESTAMP 格式: "YYYY-MM-DD HH:MM:SS" (UTC)
            created_at = datetime.strptime(created_at_str, "%Y-%m-%d %H:%M:%S")
            now = datetime.utcnow()
            age_days = (now - created_at).days

            if age_days >= warning_days:
                return (
                    f"密钥已 {age_days} 天前创建 (provider: {provider}), "
                    f"超过 {warning_days} 天阈值, 建议轮换"
                )
            return None
        except Exception as e:
            # 兜底: DB 错不打破端点 (端点 expect None 时 status=ok)
            logger.warning(f"get_key_expiration_warning failed for {provider}: {e}")
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
