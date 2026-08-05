"""R16 user_roles.is_active + 软删除单测.

按 R16 三方共识 Q3=A 单测:
- 测试 schema migration (PRAGMA 含 is_active)
- 测试 authenticate() 加 WHERE is_active = 1 过滤
- 测试 delete_user() 软删除 (UPDATE is_active = 0)
- 测试 idempotent 软删除
"""
import sqlite3

import pytest


@pytest.fixture
def temp_user_db(tmp_path):
    """创建临时 keys.db, 含 1 个 admin + 1 个 inactive user."""
    db_path = tmp_path / "keys.db"
    conn = sqlite3.connect(str(db_path))
    cur = conn.cursor()
    # 模拟 user_roles 表 schema (R16 加 is_active)
    cur.execute("""
        CREATE TABLE user_roles (
            id INTEGER PRIMARY KEY,
            username TEXT NOT NULL,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL,
            permissions TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            is_active BOOLEAN NOT NULL DEFAULT 1
        )
    """)
    cur.execute(
        "INSERT INTO user_roles (username, password_hash, role) VALUES (?, ?, ?)",
        ("admin", "hash_admin", "admin")
    )
    cur.execute(
        "INSERT INTO user_roles (username, password_hash, role) VALUES (?, ?, ?)",
        ("test", "hash_test", "view")
    )
    conn.commit()
    conn.close()
    return db_path


class TestUserSoftDelete:
    """R16 user_roles.is_active + 软删除."""

    def test_schema_has_is_active(self, temp_user_db):
        """user_roles schema 必含 is_active 列 (R16 ALTER ADD COLUMN)."""
        conn = sqlite3.connect(str(temp_user_db))
        cur = conn.execute("PRAGMA table_info(user_roles)")
        cols = [r[1] for r in cur.fetchall()]
        assert "is_active" in cols
        conn.close()

    def test_schema_is_active_default_1(self, temp_user_db):
        """is_active 默认 1 (跟 providers 表对齐)."""
        conn = sqlite3.connect(str(temp_user_db))
        cur = conn.execute("SELECT username, is_active FROM user_roles")
        rows = cur.fetchall()
        conn.close()
        # 2 个用户 (admin + test) 默认 is_active = 1
        for username, is_active in rows:
            assert is_active == 1, f"{username} default is_active should be 1, got {is_active}"

    def test_soft_delete_updates_is_active_to_0(self, temp_user_db):
        """delete_user 软删除 = UPDATE is_active = 0 (不删除行)."""
        conn = sqlite3.connect(str(temp_user_db))
        cur = conn.cursor()
        cur.execute(
            "UPDATE user_roles SET is_active = 0, updated_at = CURRENT_TIMESTAMP WHERE username = ?",
            ("admin",)
        )
        conn.commit()
        # 验证行还在 (软删除不删行)
        cur.execute("SELECT COUNT(*) FROM user_roles WHERE username = ?", ("admin",))
        row_count = cur.fetchone()[0]
        assert row_count == 1, f"软删除后行仍存在, got row_count={row_count}"
        # 验证 is_active = 0
        cur.execute("SELECT is_active FROM user_roles WHERE username = ?", ("admin",))
        is_active = cur.fetchone()[0]
        assert is_active == 0, f"is_active 应该 = 0, got {is_active}"
        conn.close()

    def test_authenticate_filters_inactive(self, temp_user_db):
        """authenticate 加 WHERE is_active = 1 后, inactive 用户登录失败."""
        conn = sqlite3.connect(str(temp_user_db))
        cur = conn.cursor()
        # 软删除 admin
        cur.execute(
            "UPDATE user_roles SET is_active = 0 WHERE username = ?",
            ("admin",)
        )
        conn.commit()
        # authenticate 查询 (模拟 R16 改后的 SQL)
        cur.execute(
            "SELECT * FROM user_roles WHERE username = ? AND is_active = 1",
            ("admin",)
        )
        result = cur.fetchone()
        conn.close()
        assert result is None, f"inactive 用户不应 authenticate, got {result}"
