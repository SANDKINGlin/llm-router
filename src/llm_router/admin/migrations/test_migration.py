#!/usr/bin/env python3
"""
测试数据库迁移和回滚功能
"""
import sqlite3
import tempfile
import os
from pathlib import Path

def test_migration_rollback():
    """测试迁移001的回滚功能"""

    # 创建临时测试数据库
    with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.db') as f:
        test_db_path = f.name

    try:
        conn = sqlite3.connect(test_db_path)
        cursor = conn.cursor()

        # 执行迁移脚本
        migration_script = Path(__file__).parent / "001_initial_schema.sql"
        with open(migration_script, 'r') as f:
            migration_sql = f.read()

        cursor.executescript(migration_sql)
        conn.commit()

        # 验证表创建
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables_after_migration = [row[0] for row in cursor.fetchall()]

        expected_tables = ['providers', 'user_roles', 'config_history', 'cost_metrics', 'audit_logs']
        assert all(table in tables_after_migration for table in expected_tables), \
            f"迁移后缺少表: {set(expected_tables) - set(tables_after_migration)}"

        print("✓ 迁移成功: 所有表已创建")

        # 插入测试数据
        cursor.execute("INSERT INTO providers (name, tier, quota) VALUES ('test-provider', 'medium', 1000000)")
        cursor.execute("INSERT INTO user_roles (username, password_hash, role) VALUES ('testuser', 'hash', 'operate')")
        conn.commit()

        # 验证数据存在
        cursor.execute("SELECT COUNT(*) FROM providers")
        provider_count = cursor.fetchone()[0]
        assert provider_count == 1, f"Provider数量应该是1，实际是{provider_count}"

        cursor.execute("SELECT COUNT(*) FROM user_roles")
        user_count = cursor.fetchone()[0]
        assert user_count == 2, f"User数量应该是2（默认admin + testuser），实际是{user_count}"

        print("✓ 测试数据插入成功")

        # 执行回滚脚本
        rollback_script = Path(__file__).parent / "001_rollback.sql"
        with open(rollback_script, 'r') as f:
            rollback_sql = f.read()

        cursor.executescript(rollback_sql)
        conn.commit()

        # 验证表删除
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables_after_rollback = [row[0] for row in cursor.fetchall()]

        for table in expected_tables:
            assert table not in tables_after_rollback, \
                f"回滚失败: 表 '{table}' 仍然存在"

        print("✓ 回滚成功: 所有表已删除")

        conn.close()

        print("\n🎉 迁移和回滚测试全部通过!")
        return True

    except AssertionError as e:
        print(f"\n❌ 测试失败: {e}")
        return False
    except Exception as e:
        print(f"\n❌ 测试错误: {e}")
        return False
    finally:
        # 清理临时文件
        if os.path.exists(test_db_path):
            os.remove(test_db_path)

if __name__ == "__main__":
    success = test_migration_rollback()
    exit(0 if success else 1)
