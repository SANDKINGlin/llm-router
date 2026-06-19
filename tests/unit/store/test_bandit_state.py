"""S1.1 增强子片 0.1:`bandit_state.db` schema 预留单测。

**本切片契约**:
- schema init 幂等(可重复跑)
- columns() 与 BANDIT_STATE_COLUMNS 元组完全一致(顺序 + 名字)
- 默认值合规(successes/failures=0, total_reward=0.0, decay_factor=1.0)
- WAL mode 启用(同 trace.py 模式)

**本切片非范围**:read/write API(record/get)留 S3+ bandit。

测试模式同 tests/unit/test_trace.py:`asyncio.run(coro)` 包 async body
(免 pytest-asyncio 依赖,与项目其余 store 测试一致)。
"""
from __future__ import annotations

import asyncio
import sqlite3

from llm_router.store.bandit_state import (
    BANDIT_STATE_COLUMNS,
    BanditStateStore,
)


def _run(coro):
    return asyncio.run(coro)


def test_init_creates_schema(tmp_path):
    """init() 跑后 bandit_state 表存在,列与 BANDIT_STATE_COLUMNS 一致。"""

    async def body():
        store = BanditStateStore(tmp_path / "bandit.db")
        await store.init()
        return await store.columns()

    cols = _run(body())
    assert tuple(cols) == BANDIT_STATE_COLUMNS, (
        f"列序应与 BANDIT_STATE_COLUMNS 元组一致;实际 {cols}"
    )


def test_init_idempotent(tmp_path):
    """init() 可重复跑,IF NOT EXISTS 不破坏已存在数据。

    模拟 S3+ bandit 写入(本切片不实施读写 API,故用同步 sqlite3 直插一行),
    再 init,验证数据保留 + 默认值正确。
    """
    db_path = tmp_path / "bandit.db"

    async def init():
        store = BanditStateStore(db_path)
        await store.init()

    _run(init())
    # 直接插入一行(模拟 S3+ bandit 写入,不走 store API)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO bandit_state(arm, last_updated) VALUES (?, ?)",
            ("provider/model/key", "2026-06-19T15:00:00Z"),
        )
        conn.commit()
    # 再 init,数据应保留
    _run(init())
    with sqlite3.connect(db_path) as conn:
        rows = list(
            conn.execute(
                "SELECT arm, successes, failures, total_reward, decay_factor "
                "FROM bandit_state"
            )
        )
    assert rows == [("provider/model/key", 0, 0, 0.0, 1.0)], (
        f"重复 init 应保留已存数据 + 默认值正确;实际 {rows}"
    )


def test_wal_mode_enabled(tmp_path):
    """同 trace.py / token_ledger.py:journal_mode=WAL(各自独立 WAL,故障隔离)。"""

    async def body():
        store = BanditStateStore(tmp_path / "bandit.db")
        await store.init()

    _run(body())
    with sqlite3.connect(tmp_path / "bandit.db") as conn:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal", f"journal_mode 应 WAL;实际 {mode}"


def test_columns_constant_matches_schema():
    """**纯静态契约**:BANDIT_STATE_COLUMNS 元组就是 6 字段权威清单
    (arm/successes/failures/total_reward/decay_factor/last_updated)。
    任何字段增删 = schema 演化 = 必须同步改本元组(S3+ bandit 落地前的契约 guard)。
    """
    assert BANDIT_STATE_COLUMNS == (
        "arm",
        "successes",
        "failures",
        "total_reward",
        "decay_factor",
        "last_updated",
    )
