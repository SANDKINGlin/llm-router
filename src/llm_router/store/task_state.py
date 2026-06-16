"""S2.5 · task_state.db 长任务状态机(Phase 1 基础版,**数据层半片**)。

5 个独立 SQLite WAL 之一(`data/task_state.db`,与 trace/token_ledger/circuit/health
各自锁、各自崩溃恢复,**不合并**,同 design.md ⑦)。

修两条 BUG(蓝图 §4 S2.5):
  - BUG-state-04:重试历史在重启后丢失 → retry_count/attempts/last_error 持久化,
    close+reopen 后可恢复续跑(验收①:重启后重试历史不丢)。
  - timeout-state-09:僵死任务无法检测 → heartbeat_at 字段 + stale_running() 查询,
    按「最后心跳早于截止点」捞出僵死任务(验收②:僵死任务可由 heartbeat 检测)。

四态机:pending → running → {completed | failed};failed → running(重试,retry_count+1)。
completed 为终态,无出边。

**本半片范围(数据层)**:schema + CRUD + 状态转换原语(乐观锁防并发双写)+ 心跳查询。
  **不做(留给「状态机逻辑」下半片)**:谁触发 retry、重试预算/退避、僵死任务回收动作、
  与 Cascade/Scanner 的接线。本模块是纯持久化原语,不碰 Provider 契约(S2.4 才需要)。

连接用 autocommit(isolation_level=None),同 trace.py/token_ledger.py 模式:
每条语句独立事务原子提交;WAL + busy_timeout 让并发写者排队。状态转换用
`UPDATE ... WHERE task_id=? AND state=<读到的旧态>` 乐观锁——并发改了状态则 rowcount=0,
抛 InvalidStateTransitionError,由调用方决策(不在数据层自旋)。

对抗审查记录:HERMES 本会话不可用(历史性 timeout),改由 **OpenCode(deepseek-v4-pro
异构模型,--pure 隔离 mem,项目目录运行)对抗审 [CHALLENGE 10 项]**,全已修:
  - HIGH #1:stale_running 原用 SQL 字符串比较,但 isoformat() 在 microsecond≠0 时
    产 '.xxx+00:00'、==0 时产 '+00:00',因 '+'(0x2B) < '.'(0x2E) 字典序会错乱 →
    改 **Python 端 datetime.fromisoformat() 比较 + 排序**(格式无关)。
  - MEDIUM #2:heartbeat 的 updated_at 改用 wall-clock(_now_iso),仅 heartbeat_at 用 at。
  - MEDIUM #3-#6:补测试(混格式 stale / 并发双 start 乐观锁 / stale 空+排序+非 running 排除 /
    heartbeat 在 completed|failed 上拒绝)。
  - MEDIUM #7-#8:删自我辩护性注释分句。
  - LOW #9:删未引用的死码 TASK_STATE_COLUMNS。
  - LOW #10:columns() 保留(贴 trace/ledger 的 ops/自省模式)。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Optional

import aiosqlite

_SCHEMA = """\
CREATE TABLE IF NOT EXISTS task_state (
    task_id      TEXT PRIMARY KEY,
    state        TEXT NOT NULL,
    attempts     INTEGER NOT NULL DEFAULT 0,
    retry_count  INTEGER NOT NULL DEFAULT 0,
    last_error   TEXT,
    heartbeat_at TEXT,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_task_state_state ON task_state(state);
"""

class State(str, Enum):
    """任务四态。str 枚举:可直接与 DB 文本比较/绑定。"""

    PENDING = "pending"
    RUNNING = "running"
    FAILED = "failed"
    COMPLETED = "completed"


@dataclass(frozen=True)
class TaskStateRow:
    task_id: str
    state: State
    attempts: int
    retry_count: int
    last_error: Optional[str]
    heartbeat_at: Optional[str]
    created_at: str
    updated_at: str


class TaskNotFoundError(RuntimeError):
    """操作的目标 task_id 不存在。"""


class InvalidStateTransitionError(RuntimeError):
    """状态转换非法(或并发改写导致乐观锁未命中)。

    from_state/to_state 保留供调用方判断:若是并发(rowcount=0)调用方可重试。
    """

    def __init__(self, from_state: object, to_state: object) -> None:
        self.from_state = from_state
        self.to_state = to_state
        super().__init__(f"非法状态转换:{from_state!r} → {to_state!r}")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class TaskStateStore:
    """task_state.db 长任务状态机持久化(数据层)。

    状态转换表(由各方法的源态检查编码):
        pending   → running, failed
        running   → completed, failed
        failed    → running (重试)
        completed → (终态,无出边)
    """

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = str(db_path)
        self._conn: Optional[aiosqlite.Connection] = None

    async def init(self) -> None:
        """建库 + 表 + 索引 + WAL pragma。幂等(可重复调)。"""
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self._db_path, isolation_level=None)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA synchronous=NORMAL")
        await self._conn.execute("PRAGMA busy_timeout=5000")
        await self._conn.executescript(_SCHEMA)

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def _db(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("TaskStateStore 未 init()")
        return self._conn

    # ── schema 自省(ops/测试)──────────────────────────────────────

    async def columns(self) -> list[dict]:
        """PRAGMA table_info(task_state)。"""
        async with self._db.execute("PRAGMA table_info(task_state)") as cur:
            rows = await cur.fetchall()
        return [
            {
                "cid": r["cid"],
                "name": r["name"],
                "type": r["type"],
                "notnull": r["notnull"],
                "dflt": r["dflt_value"],
                "pk": r["pk"],
            }
            for r in rows
        ]

    # ── 读 ─────────────────────────────────────────────────────────

    @staticmethod
    def _row(r: aiosqlite.Row) -> TaskStateRow:
        return TaskStateRow(
            task_id=r["task_id"],
            state=State(r["state"]),
            attempts=r["attempts"],
            retry_count=r["retry_count"],
            last_error=r["last_error"],
            heartbeat_at=r["heartbeat_at"],
            created_at=r["created_at"],
            updated_at=r["updated_at"],
        )

    async def get(self, task_id: str) -> Optional[TaskStateRow]:
        async with self._db.execute(
            "SELECT task_id, state, attempts, retry_count, last_error, "
            "       heartbeat_at, created_at, updated_at "
            "FROM task_state WHERE task_id = ?",
            (task_id,),
        ) as cur:
            r = await cur.fetchone()
        return self._row(r) if r is not None else None

    # ── 写(状态转换,乐观锁)──────────────────────────────────────

    async def create(self, task_id: str) -> TaskStateRow:
        """建 pending 任务。幂等:task_id 已存在则返回现有行(不重置状态)。

        守「重启续跑」:同一个 task_id 跨重启重复 create 不丢既有进度。
        """
        now = _now_iso()
        await self._db.execute(
            "INSERT INTO task_state "
            "(task_id, state, attempts, retry_count, last_error, heartbeat_at, "
            " created_at, updated_at) "
            "VALUES (?, ?, 0, 0, NULL, NULL, ?, ?) "
            "ON CONFLICT(task_id) DO NOTHING",
            (task_id, State.PENDING.value, now, now),
        )
        row = await self.get(task_id)
        assert row is not None  # 刚 insert 或已存在,必非空
        return row

    async def start(self, task_id: str) -> TaskStateRow:
        """pending|failed → running(开始或重试)。

        attempts 恒 +1(每次启动执行计一次);**仅当来自 failed 才** retry_count +1
        (首启不算重试)。同时刷新 heartbeat_at=now(运行期心跳起点)。
        """
        row = await self.get(task_id)
        if row is None:
            raise TaskNotFoundError(task_id)
        if row.state not in (State.PENDING, State.FAILED):
            raise InvalidStateTransitionError(row.state, State.RUNNING)
        bump_retry = 1 if row.state is State.FAILED else 0
        now = _now_iso()
        cur = await self._db.execute(
            "UPDATE task_state SET state = ?, attempts = attempts + 1, "
            "retry_count = retry_count + ?, heartbeat_at = ?, updated_at = ? "
            "WHERE task_id = ? AND state = ?",
            (
                State.RUNNING.value,
                bump_retry,
                now,
                now,
                task_id,
                row.state.value,
            ),
        )
        if cur.rowcount == 0:
            # 并发已改态:按非法转换抛,调用方可重读决策。
            raise InvalidStateTransitionError(row.state, State.RUNNING)
        return await self.get(task_id)  # type: ignore[return-value]

    async def mark_completed(self, task_id: str) -> TaskStateRow:
        """running → completed(成功终态)。"""
        row = await self.get(task_id)
        if row is None:
            raise TaskNotFoundError(task_id)
        if row.state is not State.RUNNING:
            raise InvalidStateTransitionError(row.state, State.COMPLETED)
        now = _now_iso()
        cur = await self._db.execute(
            "UPDATE task_state SET state = ?, updated_at = ? "
            "WHERE task_id = ? AND state = ?",
            (State.COMPLETED.value, now, task_id, row.state.value),
        )
        if cur.rowcount == 0:
            raise InvalidStateTransitionError(row.state, State.COMPLETED)
        return await self.get(task_id)  # type: ignore[return-value]

    async def mark_failed(self, task_id: str, *, error: str) -> TaskStateRow:
        """pending|running → failed(记 last_error;retry_count 不在此加,在 start 重试时加)。

        允许 pending→failed:守「未启动即判定不可行」的最坏情形。
        """
        row = await self.get(task_id)
        if row is None:
            raise TaskNotFoundError(task_id)
        if row.state not in (State.PENDING, State.RUNNING):
            raise InvalidStateTransitionError(row.state, State.FAILED)
        now = _now_iso()
        cur = await self._db.execute(
            "UPDATE task_state SET state = ?, last_error = ?, updated_at = ? "
            "WHERE task_id = ? AND state = ?",
            (State.FAILED.value, error, now, task_id, row.state.value),
        )
        if cur.rowcount == 0:
            raise InvalidStateTransitionError(row.state, State.FAILED)
        return await self.get(task_id)  # type: ignore[return-value]

    async def heartbeat(self, task_id: str, *, at: Optional[str] = None) -> None:
        """刷新 heartbeat_at(running 专属;非 running 抛非法转换)。

        at:可选,显式事件时刻(测试注入旧时刻验僵死检测;生产默认 now)。
        """
        row = await self.get(task_id)
        if row is None:
            raise TaskNotFoundError(task_id)
        if row.state is not State.RUNNING:
            raise InvalidStateTransitionError(row.state, "(heartbeat)")
        ts = at or _now_iso()
        now = _now_iso()  # updated_at 始终反映真实修改时刻(OpenCode #2),与心跳时刻解耦
        cur = await self._db.execute(
            "UPDATE task_state SET heartbeat_at = ?, updated_at = ? "
            "WHERE task_id = ? AND state = ?",
            (ts, now, task_id, row.state.value),
        )
        if cur.rowcount == 0:
            raise InvalidStateTransitionError(row.state, "(heartbeat)")

    # ── 僵死任务检测(验收②,timeout-state-09)─────────────────────

    async def stale_running(self, cutoff_iso: str) -> list[TaskStateRow]:
        """返回 running 且 heartbeat_at 早于 cutoff_iso 的任务(僵死候选)。

        **比较在 Python 端用 datetime.fromisoformat() 解析**(非 SQL 字符串比较):
        isoformat() 在 microsecond==0 时省略小数 → `+00:00`,非零时带 `.xxx+00:00`;
        因 ASCII `'+'`(0x2B) < `'.'`(0x2E),字典序比较在混合格式下会错乱 → 僵死漏检
        (OpenCode 对抗审 #1)。Python datetime 比较无此问题。候选集受 `state=running`
        限制(长任务在跑的通常很少),全拉再过滤成本低。cutoff_iso 与 heartbeat_at
        均须可被 fromisoformat 解析(标准 ISO 格式,_now_iso 满足)。
        heartbeat_at 为 NULL(从未 start 刷新过)的不计入。按时刻升序(最老先回收)。
        """
        cutoff = datetime.fromisoformat(cutoff_iso)
        async with self._db.execute(
            "SELECT task_id, state, attempts, retry_count, last_error, "
            "       heartbeat_at, created_at, updated_at "
            "FROM task_state WHERE state = ? AND heartbeat_at IS NOT NULL",
            (State.RUNNING.value,),
        ) as cur:
            rows = await cur.fetchall()
        parsed = [
            (self._row(r), datetime.fromisoformat(r["heartbeat_at"])) for r in rows
        ]
        parsed = [(row, ts) for row, ts in parsed if ts < cutoff]
        parsed.sort(key=lambda pair: pair[1])
        return [row for row, _ in parsed]
