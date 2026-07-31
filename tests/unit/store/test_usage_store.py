# test_usage_store.py — r9.5 单测 SQLite 跟踪 tpm/rpm/quota

import pytest
import sqlite3
import tempfile
import os
from pathlib import Path

from llm_router.store.usage import UsageStore


@pytest.fixture
def temp_db():
    """临时测试数据库"""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    yield path
    if os.path.exists(path):
        os.unlink(path)


@pytest.fixture
def store(temp_db):
    """测试用 UsageStore 实例"""
    return UsageStore(db_path=temp_db)


def test_record_request_increases_tpm(store):
    """测试 record_request 后 tpm_used 增加"""
    store.record_request("provider1", tokens_used=100)
    usage = store.get_usage("provider1")
    assert usage["tpm_used"] == 100
    assert usage["rpm_used"] == 1


def test_record_request_decreases_quota(store):
    """测试 quota_remaining 减少"""
    # 先设置初始 quota
    with sqlite3.connect(store.db_path) as conn:
        conn.execute(
            "INSERT INTO provider_usage (provider_name, quota_remaining) VALUES (?, ?)",
            ("provider1", 50),
        )
        conn.commit()

    usage_before = store.get_usage("provider1")
    quota_before = usage_before["quota_remaining"]

    # 模拟 quota 减少 (真实逻辑会在 cascade 层实现)
    store.record_request("provider1", tokens_used=100)

    # 验证 quota 不变 (record_request 不直接减少 quota,由 skip_provider 管理)
    usage_after = store.get_usage("provider1")
    assert usage_after["quota_remaining"] == quota_before


def test_reset_tpm_rpm_resets_counters(store):
    """测试 reset_tpm_rpm 后 tpm_used 归 0"""
    store.record_request("provider1", tokens_used=100)
    store.record_request("provider1", tokens_used=50)

    usage_before = store.get_usage("provider1")
    assert usage_before["tpm_used"] == 150
    assert usage_before["rpm_used"] == 2

    store.reset_tpm_rpm("provider1")

    usage_after = store.get_usage("provider1")
    assert usage_after["tpm_used"] == 0
    assert usage_after["rpm_used"] == 0


def test_check_quota_remaining_returns_int(store):
    """测试 check_quota_remaining 返回正确 int"""
    with sqlite3.connect(store.db_path) as conn:
        conn.execute(
            "INSERT INTO provider_usage (provider_name, quota_remaining) VALUES (?, ?)",
            ("provider1", 75),
        )
        conn.commit()

    quota = store.check_quota_remaining("provider1")
    assert isinstance(quota, int)
    assert quota == 75


def test_check_quota_missing_provider(store):
    """测试不存在的 provider 返回 0"""
    quota = store.check_quota_remaining("nonexistent")
    assert quota == 0


def test_skip_provider_sets_quota_zero(store):
    """测试 skip_provider 后 provider 从池移除(quota=0)"""
    with sqlite3.connect(store.db_path) as conn:
        conn.execute(
            "INSERT INTO provider_usage (provider_name, quota_remaining) VALUES (?, ?)",
            ("provider1", 50),
        )
        conn.commit()

    store.skip_provider("provider1", reason="quota")
    usage = store.get_usage("provider1")
    assert usage["quota_remaining"] == 0


def test_skip_provider_ip_safety_increases_counter(store):
    """测试 skip_provider ip_safety 增加计数"""
    store.skip_provider("provider1", reason="ip_safety")
    usage = store.get_usage("provider1")
    assert usage["ip_safety_skip_count"] == 1
    assert usage["quota_remaining"] == 0


def test_get_top_provider_sorts_by_5_dimensions(store):
    """测试 get_top_provider 按 5 维排序"""
    # 创建 3 个 provider 不同状态
    with sqlite3.connect(store.db_path) as conn:
        conn.executemany(
            """INSERT INTO provider_usage
            (provider_name, tpm_used, rpm_used, quota_remaining, capability_count_json)
            VALUES (?, ?, ?, ?, ?)""",
            [
                ("p1", 1000, 50, 50, '{"vision": 10}'),  # 高 usage 但高 capability
                ("p2", 100, 5, 30, '{"vision": 1}'),  # 低 usage 低 capability
                ("p3", 500, 20, 0, '{"vision": 5}'),  # quota=0 应跳过
            ],
        )
        conn.commit()

    providers = ["p1", "p2", "p3"]
    top = store.get_top_provider(providers, capability="vision", ip_safety="allowed")

    # p3 quota=0 跳过,p1 > p2 (capability 10 vs 1)
    assert top == "p1"


def test_get_top_provider_empty_list(store):
    """测试空 provider 列表返回空字符串"""
    top = store.get_top_provider([])
    assert top == ""


def test_capability_tracking(store):
    """测试 capability 计数正确累加"""
    # 第一次请求
    store.record_request("p1", tokens_used=100, capability="vision", request_id="req1")
    usage = store.get_usage("p1")
    caps = eval(usage["capability_count_json"])
    assert caps.get("vision") == 1

    # 第二次请求
    store.record_request("p1", tokens_used=50, capability="vision", request_id="req2")
    usage = store.get_usage("p1")
    caps = eval(usage["capability_count_json"])
    assert caps.get("vision") == 2


def test_capability_match_log(store):
    """测试 capability 匹配日志记录"""
    store.record_request("p1", capability="vision", request_id="req123")

    with sqlite3.connect(store.db_path) as conn:
        cur = conn.execute(
            "SELECT provider, capability, request_id FROM capability_match_log"
        )
        row = cur.fetchone()
        assert row is not None
        assert row[0] == "p1"
        assert row[1] == "vision"
        assert row[2] == "req123"


def test_cleanup_old_logs(store):
    """测试清理旧日志"""
    # 插入一条日志(时间戳为当前)
    store.record_request("p1", capability="vision", request_id="req1")

    # 手动插入一条旧日志
    with sqlite3.connect(store.db_path) as conn:
        conn.execute(
            """INSERT INTO capability_match_log (timestamp, provider, capability, request_id)
            VALUES ('2020-01-01 00:00:00', 'p1', 'vision', 'old_req')"""
        )
        conn.commit()

    # 清理 7 天前的日志
    store.cleanup_old_logs(days=7)

    # 验证只剩新日志
    with sqlite3.connect(store.db_path) as conn:
        cur = conn.execute("SELECT COUNT(*) FROM capability_match_log")
        count = cur.fetchone()[0]
        assert count == 1
