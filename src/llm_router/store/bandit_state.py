"""S1.1 增强 · `bandit_state.db` schema 预留(Phase 2 S3+ bandit 用)。

**本切片范围**:**仅** schema + init,**不写**任何读写 API。

**目的**:S1.1 增强子片 0.1 — 提前建表规避未来 S3+ bandit 落地时的迁移债
(`bandit_state.db` 现在就建,S3+ 启用时只填值,**不动 schema**)。
设计意图见 design.md:186 + epsilon_greedy.py:10(`持久化 defer S3+ bandit_state.db`)。

5 个独立 SQLite WAL 之一(同 trace/ledger/circuit/health/task_state 模式),各自锁,
故障隔离。Phase 2 S3+ bandit 接入时,通过本模块的 `init()` + 后续 record/get API
扩展(本切片不实施)读写。

字段:
  - arm:provider+model+key 复合标识(具体编码 S3+ 决,留 TEXT 灵活)
  - successes/failures:Beta/Bernoulli bandit 基础计数(整数)
  - total_reward:累计 reward(REAL,Thompson Sampling / UCB / linear bandit 通用)
  - decay_factor:遗忘因子(per-arm,memory `routing-impl-phased-2026-06-14`
    "ε-greedy 暂缓 bandit;触发后填" + design.md 蓝图 §5)
  - last_updated:最近一次写时间(TEXT ISO 8601 UTC)
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import aiosqlite

_SCHEMA = """\
CREATE TABLE IF NOT EXISTS bandit_state (
    arm           TEXT PRIMARY KEY,
    successes     INTEGER NOT NULL DEFAULT 0,
    failures      INTEGER NOT NULL DEFAULT 0,
    total_reward  REAL    NOT NULL DEFAULT 0.0,
    decay_factor  REAL    NOT NULL DEFAULT 1.0,
    last_updated  TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_bandit_last_updated ON bandit_state(last_updated);
"""

# 蓝图 §5 + design.md:186 + memory routing-impl-phased-2026-06-14 的字段权威清单。
BANDIT_STATE_COLUMNS: tuple[str, ...] = (
    "arm",
    "successes",
    "failures",
    "total_reward",
    "decay_factor",
    "last_updated",
)


class BanditStateStore:
    """`bandit_state.db` 的 schema-only stub(S1.1 增强子片 0.1,Phase 2)。

    **本切片范围**:仅 schema + init + columns 自省(测试用)。
    **不实施**:record/get/decay 等 read/write API(留 S3+ bandit 子片)。

    与 trace.py / token_ledger.py 同模式:aiosqlite WAL 自动提交,`init()` 幂等。
    单文件 SQLite,独立锁(规避 WAL-02 单写者瓶颈,见 design.md §持久化)。
    """

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        # WAL mode 同步设置(同 trace.py:S1.1 模式)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")

    async def init(self) -> None:
        """幂等初始化 schema。CREATE TABLE IF NOT EXISTS,可重复跑。"""
        async with aiosqlite.connect(self.db_path) as db:
            await db.executescript(_SCHEMA)
            await db.commit()

    async def columns(self) -> list[str]:
        """schema 自省(ops/测试用):返本表当前列名顺序。

        S1.1 增强子片 0.1 的契约:列与 BANDIT_STATE_COLUMNS 元组一致。
        """
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute("PRAGMA table_info(bandit_state)") as cur:
                rows = await cur.fetchall()
        return [r[1] for r in rows]
