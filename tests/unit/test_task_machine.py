"""S2.5 · task_machine 状态机策略层(逻辑半片)。

坐在 TaskStateStore(纯持久化数据层)之上的**策略层**:retry 预算 + 退避(复用 S1.6
公式 min(base×2^n, cap)+jitter)+ 僵死回收(recover_stale 把死掉的 running 标 failed)。
**不接线**(caller/runner 留 Phase2)。守 routing-priority-principle:超预算=可用性过滤。
TDD:本套件先 RED——TaskMachine 未实现时 import 即失败。
"""
from __future__ import annotations

import asyncio

import pytest

from llm_router.store.task_machine import (
    RecoveryReport,
    RetryPlan,
    TaskMachine,
)
from llm_router.store.task_state import State, TaskNotFoundError, TaskStateStore


def _run(coro):
    return asyncio.run(coro)


def _deterministic_jitter(value: float = 0.0):
    """注入确定 jitter(验退避序列时剥离随机性)。"""

    return lambda: value


# ── retry 预算 ─────────────────────────────────────────────────────


def test_retry_budget_remaining(tmp_path):
    """新任务剩满额度;每重试一次 -1;耗尽为 0。"""

    async def body():
        store = TaskStateStore(tmp_path / "task_state.db")
        await store.init()
        try:
            m = TaskMachine(store, max_retries=3)
            await store.create("t1")
            assert await m.retry_budget_remaining("t1") == 3  # pending 也按额度算

            # 一次失败+重试 → retry_count=1
            await store.start("t1")
            await store.mark_failed("t1", error="x")
            await store.start("t1")
            assert await m.retry_budget_remaining("t1") == 2

            # 再两次 → retry_count=3,耗尽
            await store.mark_failed("t1", error="x")
            await store.start("t1")
            await store.mark_failed("t1", error="x")
            await store.start("t1")
            await store.mark_failed("t1", error="x")
            await store.start("t1")
            assert await m.retry_budget_remaining("t1") == 0
        finally:
            await store.close()

    _run(body())


# ── plan_retry ─────────────────────────────────────────────────────


def test_plan_retry_raises_not_found_for_missing_task(tmp_path):
    """不存在的 task → TaskNotFoundError(OpenCode #3:docstring 声称的契约须有测试)。"""

    async def body():
        store = TaskStateStore(tmp_path / "task_state.db")
        await store.init()
        try:
            m = TaskMachine(store, max_retries=3)
            with pytest.raises(TaskNotFoundError):
                await m.plan_retry("ghost")
        finally:
            await store.close()

    _run(body())


def test_retry_budget_remaining_zero_for_missing_task(tmp_path):
    """不存在的 task → 0(OpenCode #4:该分支须有覆盖)。"""

    async def body():
        store = TaskStateStore(tmp_path / "task_state.db")
        await store.init()
        try:
            m = TaskMachine(store, max_retries=3)
            assert await m.retry_budget_remaining("ghost") == 0
        finally:
            await store.close()

    _run(body())


def test_plan_retry_returns_delay_for_failed(tmp_path):
    """failed 且有额度 → RetryPlan,delay = base×2^retry_count + jitter。"""

    async def body():
        store = TaskStateStore(tmp_path / "task_state.db")
        await store.init()
        try:
            m = TaskMachine(
                store, max_retries=3, jitter_fn=_deterministic_jitter(5.0)
            )
            await store.create("t1")
            await store.start("t1")
            await store.mark_failed("t1", error="boom")  # retry_count=0, failed

            plan = await m.plan_retry("t1")
            assert plan is not None
            assert plan.delay_seconds == 30 * 1 + 5.0  # base×2^0 + jitter
            assert plan.next_retry_count == 1
            assert plan.next_attempt == 2
        finally:
            await store.close()

    _run(body())


@pytest.mark.parametrize("setup_state", ["pending", "running", "completed"])
def test_plan_retry_none_for_non_failed(tmp_path, setup_state):
    """非 failed 状态 → None(无待重试)。"""

    async def body():
        store = TaskStateStore(tmp_path / "task_state.db")
        await store.init()
        try:
            m = TaskMachine(store, max_retries=3)
            await store.create("t1")
            if setup_state == "running":
                await store.start("t1")
            elif setup_state == "completed":
                await store.start("t1")
                await store.mark_completed("t1")
            assert await m.plan_retry("t1") is None
        finally:
            await store.close()

    _run(body())


def test_plan_retry_none_when_budget_exhausted(tmp_path):
    """retry_count >= max_retries → None(预算耗尽,不再重试)。"""

    async def body():
        store = TaskStateStore(tmp_path / "task_state.db")
        await store.init()
        try:
            m = TaskMachine(store, max_retries=3)
            await store.create("t1")
            # 烧满 3 次重试:首启(pending→running,retry_count 不加)+ 3 次重试
            # (failed→running 各 +1)= retry_count 3。循环 start;mark_failed 第 1 次首启、
            # 第 2~4 次为重试 → 共 4 轮得 retry_count=3。
            for _ in range(4):
                await store.start("t1")
                await store.mark_failed("t1", error="x")
            row = await store.get("t1")
            assert row.retry_count == 3, f"应烧满 3 次重试,实际 {row.retry_count}"
            assert row.state is State.FAILED
            assert await m.plan_retry("t1") is None, "预算耗尽不应再计划重试"
        finally:
            await store.close()

    _run(body())


def test_backoff_sequence(tmp_path):
    """退避序列 min(30×2^n, 300):0→30,1→60,2→120,3→240,4→300(cap)。jitter 注入 0。"""

    async def body():
        store = TaskStateStore(tmp_path / "task_state.db")
        await store.init()
        try:
            m = TaskMachine(
                store,
                max_retries=10,  # 抬高让 retry_count=4 仍可重试,验 cap
                jitter_fn=_deterministic_jitter(0.0),
            )
            await store.create("t1")
            expected = [30.0, 60.0, 120.0, 240.0, 300.0]
            for n, want in enumerate(expected):
                # 让任务停在 failed、retry_count=n
                await store.start("t1")
                await store.mark_failed("t1", error="x")
                assert (await store.get("t1")).retry_count == n
                plan = await m.plan_retry("t1")
                assert plan is not None, f"retry_count={n} 应可重试"
                assert plan.delay_seconds == want, (
                    f"retry_count={n} delay 应为 {want},实际 {plan.delay_seconds}"
                )
        finally:
            await store.close()

    _run(body())


# ── recover_stale(僵死回收)────────────────────────────────────────


def test_recover_stale_marks_dead_tasks_failed(tmp_path):
    """验收②的动作:stale running 被标 failed;新鲜的不动;report 计数对。"""

    async def body():
        store = TaskStateStore(tmp_path / "task_state.db")
        await store.init()
        try:
            m = TaskMachine(store, max_retries=3)
            # 2 个僵死(旧心跳)+ 1 个新鲜
            for tid in ("dead1", "dead2", "alive"):
                await store.create(tid)
                await store.start(tid)
            await store.heartbeat("dead1", at="2020-01-01T00:00:00+00:00")
            await store.heartbeat("dead2", at="2020-01-02T00:00:00+00:00")
            # alive 心跳是 start 时的 now(新)

            report = await m.recover_stale("2025-01-01T00:00:00+00:00")
            assert isinstance(report, RecoveryReport)
            assert report.marked_failed == 2
            assert set(report.task_ids) == {"dead1", "dead2"}
            assert report.skipped == 0

            assert (await store.get("dead1")).state is State.FAILED
            assert (await store.get("dead2")).state is State.FAILED
            assert (await store.get("alive")).state is State.RUNNING, "新鲜任务不应被动"
        finally:
            await store.close()

    _run(body())


def test_recover_stale_handles_concurrent_change(tmp_path):
    """并发改态致 mark_failed 抛 InvalidStateTransitionError → 计 skipped,不崩。"""

    async def body():
        store = TaskStateStore(tmp_path / "task_state.db")
        await store.init()
        try:
            m = TaskMachine(store, max_retries=3)
            await store.create("t1")
            await store.start("t1")
            await store.heartbeat("t1", at="2020-01-01T00:00:00+00:00")

            class _FlakyStore:
                """仅 stub recover_stale 用到的两个方法,mark_failed 抛并发错。"""

                async def stale_running(self, cutoff_iso):
                    return [await store.get("t1")]  # 返真实行(含 task_id)

                async def mark_failed(self, task_id, *, error):
                    # 模拟并发:任务已被别处改态,乐观锁未命中
                    from llm_router.store.task_state import (
                        InvalidStateTransitionError,
                    )

                    raise InvalidStateTransitionError(State.RUNNING, State.FAILED)

            flaky = _FlakyStore()
            m2 = TaskMachine(flaky, max_retries=3)
            report = await m2.recover_stale("2025-01-01T00:00:00+00:00")
            assert report.marked_failed == 0
            assert report.skipped == 1
        finally:
            await store.close()

    _run(body())


def test_recover_stale_no_stale_returns_empty(tmp_path):
    """无僵死任务 → 空 report(marked_failed=0),不报错。"""

    async def body():
        store = TaskStateStore(tmp_path / "task_state.db")
        await store.init()
        try:
            m = TaskMachine(store, max_retries=3)
            await store.create("t1")
            await store.start("t1")  # 新鲜
            report = await m.recover_stale("2025-01-01T00:00:00+00:00")
            assert report.marked_failed == 0
            assert report.task_ids == []
        finally:
            await store.close()

    _run(body())
