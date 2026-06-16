"""S2.5 · task_state.db 长任务状态机(Phase 1 基础版,数据层半片)。

独立 SQLite WAL(data/task_state.db,同 design.md ⑦,与 trace/token_ledger 同模式)。
守两条验收(蓝图 §4 S2.5):
  - 验收①(BUG-state-04):重启后重试历史不丢(retry_count/attempts/last_error 持久)
  - 验收②(timeout-state-09):僵死任务可由 heartbeat 检测(stale_running 查询)

四态机:pending → running → {completed | failed};failed → running(重试,retry_count+1)。
TDD:本套件先 RED——TaskStateStore 未实现时 import 即失败。
"""
from __future__ import annotations

import asyncio

import pytest

from llm_router.store.task_state import (
    InvalidStateTransitionError,
    State,
    TaskNotFoundError,
    TaskStateStore,
)

EXPECTED_COLUMNS = {
    "task_id",
    "state",
    "attempts",
    "retry_count",
    "last_error",
    "heartbeat_at",
    "created_at",
    "updated_at",
}


def _run(coro):
    return asyncio.run(coro)


# ── schema ─────────────────────────────────────────────────────────


def test_task_state_table_schema(tmp_path):
    """8 字段在 + state NOT NULL(PRAGMA)。"""

    async def body():
        store = TaskStateStore(tmp_path / "task_state.db")
        await store.init()
        try:
            cols = await store.columns()
            names = {c["name"] for c in cols}
            missing = EXPECTED_COLUMNS - names
            assert not missing, f"缺字段: {missing}"

            by = {c["name"]: c for c in cols}
            assert by["state"]["notnull"] == 1, "state 必须 NOT NULL"
            assert by["attempts"]["notnull"] == 1, "attempts 必须 NOT NULL"
            assert by["retry_count"]["notnull"] == 1, "retry_count 必须 NOT NULL"
        finally:
            await store.close()

    _run(body())


# ── create ─────────────────────────────────────────────────────────


def test_create_pending(tmp_path):
    """create → pending,attempts/retry_count 归零。"""

    async def body():
        store = TaskStateStore(tmp_path / "task_state.db")
        await store.init()
        try:
            row = await store.create("job-1")
            assert row.task_id == "job-1"
            assert row.state is State.PENDING
            assert row.attempts == 0
            assert row.retry_count == 0
            assert row.last_error is None
            assert row.heartbeat_at is None
        finally:
            await store.close()

    _run(body())


def test_create_idempotent(tmp_path):
    """同一 task_id 重复 create 不重置既有状态(守「重启续跑」)。"""

    async def body():
        store = TaskStateStore(tmp_path / "task_state.db")
        await store.init()
        try:
            await store.create("job-1")
            await store.start("job-1")  # 已 running
            # 模拟重启后再次 create(幂等):不应把状态打回 pending
            row = await store.create("job-1")
            assert row.state is State.RUNNING, "幂等 create 不得重置既有状态"
            assert row.attempts == 1
        finally:
            await store.close()

    _run(body())


# ── 状态转换 ───────────────────────────────────────────────────────


def test_lifecycle_happy_path(tmp_path):
    """pending → running → completed。"""

    async def body():
        store = TaskStateStore(tmp_path / "task_state.db")
        await store.init()
        try:
            await store.create("job-1")
            running = await store.start("job-1")
            assert running.state is State.RUNNING
            assert running.attempts == 1
            assert running.heartbeat_at is not None  # start 即刷新心跳
            done = await store.mark_completed("job-1")
            assert done.state is State.COMPLETED
        finally:
            await store.close()

    _run(body())


def test_first_start_does_not_increment_retry_count(tmp_path):
    """首启 attempts=1 但 retry_count=0(首启不算重试)。"""

    async def body():
        store = TaskStateStore(tmp_path / "task_state.db")
        await store.init()
        try:
            await store.create("job-1")
            row = await store.start("job-1")
            assert row.attempts == 1
            assert row.retry_count == 0
        finally:
            await store.close()

    _run(body())


def test_failed_then_retry_increments_retry_count(tmp_path):
    """running → failed → running(重试):attempts=2 且 retry_count=1。"""

    async def body():
        store = TaskStateStore(tmp_path / "task_state.db")
        await store.init()
        try:
            await store.create("job-1")
            await store.start("job-1")
            failed = await store.mark_failed("job-1", error="boom")
            assert failed.state is State.FAILED
            assert failed.last_error == "boom"
            assert failed.retry_count == 0, "mark_failed 不加 retry_count(在 start 重试时加)"

            retried = await store.start("job-1")  # failed → running 重试
            assert retried.state is State.RUNNING
            assert retried.attempts == 2
            assert retried.retry_count == 1, "重试时 retry_count +1"
        finally:
            await store.close()

    _run(body())


def test_pending_can_fail_without_running(tmp_path):
    """pending → failed 守「未启动即判定不可行」最坏情形。"""

    async def body():
        store = TaskStateStore(tmp_path / "task_state.db")
        await store.init()
        try:
            await store.create("job-1")
            failed = await store.mark_failed("job-1", error="bad-config")
            assert failed.state is State.FAILED
        finally:
            await store.close()

    _run(body())


# ── 非法转换 / not found ───────────────────────────────────────────


@pytest.mark.parametrize(
    "setup,op",
    [
        # completed 是终态,不能再 start
        ("completed", lambda s: s.start("job-1")),
        # running 不能再 start(防双启)
        ("running", lambda s: s.start("job-1")),
        # pending 未运行不能直接 completed
        ("pending", lambda s: s.mark_completed("job-1")),
        # completed 不能再 failed
        ("completed", lambda s: s.mark_failed("job-1", error="x")),
    ],
)
def test_invalid_transitions_rejected(tmp_path, setup, op):
    """非法状态转换抛 InvalidStateTransitionError。"""

    async def body():
        store = TaskStateStore(tmp_path / "task_state.db")
        await store.init()
        try:
            await store.create("job-1")
            if setup == "running":
                await store.start("job-1")
            elif setup == "completed":
                await store.start("job-1")
                await store.mark_completed("job-1")
            with pytest.raises(InvalidStateTransitionError):
                await op(store)
        finally:
            await store.close()

    _run(body())


@pytest.mark.parametrize(
    "op",
    [
        lambda s: s.start("ghost"),
        lambda s: s.mark_completed("ghost"),
        lambda s: s.mark_failed("ghost", error="x"),
        lambda s: s.heartbeat("ghost"),
    ],
)
def test_not_found_raises(tmp_path, op):
    """未知 task_id 抛 TaskNotFoundError(不是静默 no-op)。"""

    async def body():
        store = TaskStateStore(tmp_path / "task_state.db")
        await store.init()
        try:
            with pytest.raises(TaskNotFoundError):
                await op(store)
        finally:
            await store.close()

    _run(body())


# ── heartbeat / 僵死检测 ─────────────────────────────────────────


def test_heartbeat_running_only_and_updates(tmp_path):
    """heartbeat 仅 running 合法;pending 抛;running 刷新 heartbeat_at。"""

    async def body():
        store = TaskStateStore(tmp_path / "task_state.db")
        await store.init()
        try:
            await store.create("job-1")
            # pending 上心跳 = 非法
            with pytest.raises(InvalidStateTransitionError):
                await store.heartbeat("job-1")

            await store.start("job-1")
            old = (await store.get("job-1")).heartbeat_at
            await store.heartbeat("job-1", at="2026-01-01T00:00:00+00:00")
            new = (await store.get("job-1")).heartbeat_at
            assert new == "2026-01-01T00:00:00+00:00"
            assert new != old
        finally:
            await store.close()

    _run(body())


def test_stale_running_detects_dead_tasks(tmp_path):
    """验收②:僵死任务(running 且心跳早于 cutoff)被 stale_running 捞出。"""

    async def body():
        store = TaskStateStore(tmp_path / "task_state.db")
        await store.init()
        try:
            # A:新心跳(活);B:旧心跳(僵死)
            await store.create("A")
            await store.start("A")  # heartbeat=now(新)
            await store.create("B")
            await store.start("B")
            await store.heartbeat("B", at="2020-01-01T00:00:00+00:00")  # 老到掉牙

            stale = await store.stale_running("2025-01-01T00:00:00+00:00")
            ids = {r.task_id for r in stale}
            assert ids == {"B"}, f"只应捞出僵死的 B,实际 {ids}"
            assert stale[0].state is State.RUNNING
        finally:
            await store.close()

    _run(body())


# ── 重启持久化(验收①,BUG-state-04)──────────────────────────────


def test_retry_history_survives_restart(tmp_path):
    """验收①:close + reopen 新 store,重试历史(retry_count/attempts/last_error)不丢。"""

    db = tmp_path / "task_state.db"

    async def phase1():
        store = TaskStateStore(db)
        await store.init()
        try:
            await store.create("job-1")
            await store.start("job-1")
            await store.mark_failed("job-1", error="first-failure")
            await store.start("job-1")  # 重试一次
            await store.mark_failed("job-1", error="second-failure")
            # 此时 attempts=2, retry_count=1, last_error=second-failure, state=failed
        finally:
            await store.close()

    async def phase2():
        # 模拟崩溃后重启:全新 store 实例,同一 db 文件
        store = TaskStateStore(db)
        await store.init()
        try:
            row = await store.get("job-1")
            assert row is not None, "重启后任务行必须仍在"
            assert row.state is State.FAILED, "重启后状态不丢"
            assert row.attempts == 2, f"重启后 attempts 不丢,实际 {row.attempts}"
            assert row.retry_count == 1, f"重启后 retry_count 不丢,实际 {row.retry_count}"
            assert row.last_error == "second-failure", "重启后 last_error 不丢"
            # 重启后可从 failed 续跑(重试历史在,start 再加一次)
            retried = await store.start("job-1")
            assert retried.attempts == 3
            assert retried.retry_count == 2
        finally:
            await store.close()

    _run(phase1())
    _run(phase2())
