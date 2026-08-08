"""S1.1 · trace.db 持久化(Phase 1 基础版)。

5 个独立 SQLite WAL 之一(各自锁、各自崩溃恢复,故障隔离,**不合并**)。
修两个 CRITICAL BUG:
  - BUG-幂等-01:幂等用 compare-and-swap(UNIQUE 闸 + 轮询返缓存)
  - BUG-correlation-03:correlation_id 一对多(每 hop 新 trace_id,parent 指上一 hop)

Phase 1 不做:热冷表分离(WAL-02)、bandit_state 预留 → 均 Phase 2 S1.1增强。
reward / reward_committed_at / hop_attribution 只建字段(schema 预留):
  - reward / reward_committed_at:Phase1 不填,S3+ bandit 用;
  - hop_attribution:hop 计数语义在 S1.5a 定(B-1 精度点),本切片**不写 hop 逻辑**。
"""
from __future__ import annotations

import asyncio
import logging
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Awaitable, Callable, Optional

import aiosqlite

_LOG = logging.getLogger(__name__)

_SCHEMA = """\
CREATE TABLE IF NOT EXISTS trace (
    trace_id              TEXT PRIMARY KEY,
    correlation_id        TEXT NOT NULL,
    parent_correlation_id TEXT,
    idempotency_key       TEXT NOT NULL UNIQUE,
    provider              TEXT NOT NULL,
    result                TEXT,
    latency               REAL,
    cost                  REAL,
    reward                REAL,
    reward_committed_at   TEXT,
    hop_attribution       TEXT,
    created_at            TEXT NOT NULL,
    arm                   TEXT
);
CREATE INDEX IF NOT EXISTS idx_trace_correlation ON trace(correlation_id);
CREATE INDEX IF NOT EXISTS idx_trace_parent ON trace(parent_correlation_id);

-- S1.1 增强子片 0.2(2026-06-19,A2):热冷表分离 schema 预留(WAL-02 优化路径)。
-- **本切片仅建表,不接读写路径**——`trace` 表保留为 Phase 1 现有写入入口
-- (向后兼容,329p 测试不动);后续子片接 commit() 双写 hot + 异步迁移 cold。
-- 字段与 `trace` 完全一致(共享 TRACE_COLUMNS),便于 Phase 2 commit() 双写 + 迁移
-- 时不做 schema 转换。idempotency_key UNIQUE 保留(hot 表也是写入入口候选)。
-- 设计意图:design.md §持久化"Phase2 热冷表分离(WAL-02)"+ task 24 (S1.1增强)。
-- A4(子片 0.4,2026-06-20):三表均加末列 `arm`(per-arm 追踪,见 TRACE_COLUMNS)。
CREATE TABLE IF NOT EXISTS trace_hot (
    trace_id              TEXT PRIMARY KEY,
    correlation_id        TEXT NOT NULL,
    parent_correlation_id TEXT,
    idempotency_key       TEXT NOT NULL UNIQUE,
    provider              TEXT NOT NULL,
    result                TEXT,
    latency               REAL,
    cost                  REAL,
    reward                REAL,
    reward_committed_at   TEXT,
    hop_attribution       TEXT,
    created_at            TEXT NOT NULL,
    arm                   TEXT
);
CREATE INDEX IF NOT EXISTS idx_trace_hot_correlation ON trace_hot(correlation_id);
CREATE INDEX IF NOT EXISTS idx_trace_hot_parent ON trace_hot(parent_correlation_id);
CREATE INDEX IF NOT EXISTS idx_trace_hot_created ON trace_hot(created_at);

CREATE TABLE IF NOT EXISTS trace_cold (
    trace_id              TEXT PRIMARY KEY,
    correlation_id        TEXT NOT NULL,
    parent_correlation_id TEXT,
    idempotency_key       TEXT NOT NULL UNIQUE,
    provider              TEXT NOT NULL,
    result                TEXT,
    latency               REAL,
    cost                  REAL,
    reward                REAL,
    reward_committed_at   TEXT,
    hop_attribution       TEXT,
    created_at            TEXT NOT NULL,
    arm                   TEXT
);
CREATE INDEX IF NOT EXISTS idx_trace_cold_correlation ON trace_cold(correlation_id);
CREATE INDEX IF NOT EXISTS idx_trace_cold_parent ON trace_cold(parent_correlation_id);
CREATE INDEX IF NOT EXISTS idx_trace_cold_created ON trace_cold(created_at);
"""

# 蓝图 §4 S1.1 的 12 字段 + A4(子片 0.4)per-arm `arm` 列 = 13 字段(权威字段名)。
# arm = provider+model+account_key 复合标识(同 bandit_state.db arm PK 语义),
# 供 S3+ bandit 按臂统计 reward;具体编码 defer S3+(bandit_state.py:14)。
# account_key = api_key_env 名(非 secret,scanner/mnfst.py:58),存储安全。
TRACE_COLUMNS: tuple[str, ...] = (
    "trace_id",
    "correlation_id",
    "parent_correlation_id",
    "idempotency_key",
    "provider",
    "result",
    "latency",
    "cost",
    "reward",
    "reward_committed_at",
    "hop_attribution",
    "created_at",
    "arm",
)

# S1.1 增强子片 0.2:hot/cold 表 schema 与 trace 表一致(便于双写 + 异步迁移)。
# 字段同 TRACE_COLUMNS——任何字段演化必须三表同步(本切片只建空表,不写读写)。
TRACE_HOT_COLUMNS: tuple[str, ...] = TRACE_COLUMNS
TRACE_COLD_COLUMNS: tuple[str, ...] = TRACE_COLUMNS


class AcquireStatus(str, Enum):
    """acquire() 的 CAS 结果。"""

    OWNER = "owner"  # 抢到,调用方须调 provider 后 commit()
    REPLAYED = "replayed"  # 已 commit,cached_result 填好


@dataclass(frozen=True)
class AcquireOutcome:
    status: AcquireStatus
    trace_id: str
    cached_result: Optional[str] = None  # 仅 REPLAYED 时非空


@dataclass(frozen=True)
class TraceRow:
    trace_id: str
    correlation_id: str
    parent_correlation_id: Optional[str]
    idempotency_key: str
    provider: str
    result: Optional[str]
    latency: Optional[float]
    cost: Optional[float]
    reward: Optional[float]
    reward_committed_at: Optional[str]
    hop_attribution: Optional[str]
    created_at: str
    arm: Optional[str] = None  # A4:per-arm 标识(S3+ bandit 用;Phase1 多为 None)


class IdempotencyConflictError(RuntimeError):
    """idempotency_key 被占但 owner 在 poll 窗口内未 commit。

    Phase1 直接抛(不自动重调 provider)——守「执行一次」最坏情形,由调用方决策。
    """


# provider_fn:OWNER 时调用,返回结果文本(供 S2.x router 注入真 provider 调用)。
ProviderFn = Callable[[], Awaitable[str]]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class TraceStore:
    """trace.db 持久化 + 幂等 CAS + correlation 一对多。

    连接用 autocommit(isolation_level=None):每条语句独立事务 →
      - INSERT 原子提交(CAS 闸由 UNIQUE 约束保证);
      - poll 的 SELECT 总看到最新已提交快照(跨连接可见 owner 的 commit)。
    WAL + busy_timeout 让并发写者排队(不 SQLITE_BUSY),第二个 INSERT 才命中 UNIQUE。
    """

    def __init__(
        self,
        db_path: str | Path,
        *,
        dedup_timeout: float = 5.0,
        poll_interval: float = 0.01,
    ) -> None:
        self._db_path = str(db_path)
        self._conn: Optional[aiosqlite.Connection] = None
        self._dedup_timeout = dedup_timeout
        self._poll_interval = poll_interval

    async def init(self) -> None:
        """建库 + 表 + 索引 + WAL pragma。幂等(可重复调)。"""
        if Path(self._db_path).parent.is_symlink():
            Path(self._db_path).parent.unlink()
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self._db_path, isolation_level=None)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA synchronous=NORMAL")
        await self._conn.execute("PRAGMA busy_timeout=5000")
        await self._conn.executescript(_SCHEMA)
        # A4(子片 0.4):per-arm 列迁移。CREATE TABLE IF NOT EXISTS 不会给已存在的
        # 旧库加列,须显式 ALTER ADD COLUMN 补 arm(三表)。幂等:已含 arm 则跳过。
        await self._migrate_add_arm()

    async def _table_columns(self, table: str) -> list[str]:
        """PRAGMA table_info(<table>) → 列名顺序(迁移/测试自省用)。"""
        async with self._db.execute(f"PRAGMA table_info({table})") as cur:
            rows = await cur.fetchall()
        return [r[1] for r in rows]

    async def _migrate_add_arm(self) -> None:
        """A4:给已存在的旧库三表补 `arm TEXT` 列(末列,与 CREATE 一致)。

        CREATE TABLE IF NOT EXISTS 对已存在的表是 noop,不会加列;Phase 1 升级到
        Phase 2 时旧 trace.db / trace_hot / trace_cold 须 ALTER 补 arm。幂等:
        列已存在则跳过(不报 duplicate column name)。
        """
        for table in ("trace", "trace_hot", "trace_cold"):
            cols = await self._table_columns(table)
            if "arm" not in cols:
                await self._db.execute(f"ALTER TABLE {table} ADD COLUMN arm TEXT")

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    async def reconnect(self) -> None:
        """R20 F1: 关闭后重建连接 (用于 import_backup 后的 store 重连).

        os.replace 替换 .db 文件后, 旧 inode 上的连接持有 stale 句柄.
        close() + init() 让下次 query 自动重建到新 inode.
        """
        await self.close()
        await self.init()

    @property
    def _db(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("TraceStore 未 init()")
        return self._conn

    # ── schema 自省(ops/测试)──────────────────────────────────────

    async def columns(self) -> list[dict]:
        """PRAGMA table_info(trace)。"""
        async with self._db.execute("PRAGMA table_info(trace)") as cur:
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

    async def unique_indexes(self) -> list[dict]:
        """覆盖 trace 表的 unique 索引 + 其列(PRAGMA index_list/info)。"""
        out: list[dict] = []
        async with self._db.execute("PRAGMA index_list(trace)") as cur:
            idxs = await cur.fetchall()
        for ix in idxs:
            if ix["unique"]:
                async with self._db.execute(
                    f'PRAGMA index_info("{ix["name"]}")'
                ) as c2:
                    cols = await c2.fetchall()
                out.append(
                    {"name": ix["name"], "columns": [c["name"] for c in cols]}
                )
        return out

    # ── 幂等 CAS(BUG-幂等-01)──────────────────────────────────────

    async def acquire(
        self,
        *,
        correlation_id: str,
        idempotency_key: str,
        provider: str,
        parent_correlation_id: Optional[str] = None,
        trace_id: Optional[str] = None,
        arm: Optional[str] = None,
    ) -> AcquireOutcome:
        """幂等 compare-and-swap 入口。

        - INSERT pending(result=NULL);UNIQUE(idempotency_key) 拦并发重复:
          成功 → OWNER。
        - IntegrityError(key 已存在)→ 轮询等 owner commit → REPLAYED(缓存)。
        - poll 窗口内未 commit → IdempotencyConflictError。

        arm(A4,子片 0.4):可选 per-arm 标识,acquire 时刻若已知(S2.9 匹配层选定
        model 后可构造)则写入;None(默认)= arm 留空,待 commit 回填或保持 NULL,
        守现有调用点零回归。
        """
        trace_id = trace_id or str(uuid.uuid4())
        try:
            await self._db.execute(
                "INSERT INTO trace "
                "(trace_id, correlation_id, parent_correlation_id, idempotency_key, "
                " provider, result, latency, cost, reward, reward_committed_at, "
                " hop_attribution, created_at, arm) "
                "VALUES (?, ?, ?, ?, ?, NULL, NULL, NULL, NULL, NULL, NULL, ?, ?)",
                (
                    trace_id,
                    correlation_id,
                    parent_correlation_id,
                    idempotency_key,
                    provider,
                    _now_iso(),
                    arm,
                ),
            )
        except sqlite3.IntegrityError:
            return await self._await_committed(idempotency_key)
        return AcquireOutcome(AcquireStatus.OWNER, trace_id, None)

    async def _await_committed(self, idempotency_key: str) -> AcquireOutcome:
        """轮询等他连接 commit 的 result(autocommit 下每条 SELECT 见最新快照)。"""
        loop = asyncio.get_event_loop()
        deadline = loop.time() + self._dedup_timeout
        while loop.time() < deadline:
            async with self._db.execute(
                "SELECT trace_id, result FROM trace WHERE idempotency_key = ?",
                (idempotency_key,),
            ) as cur:
                row = await cur.fetchone()
            if row is not None and row["result"] is not None:
                return AcquireOutcome(
                    AcquireStatus.REPLAYED, row["trace_id"], row["result"]
                )
            await asyncio.sleep(self._poll_interval)
        raise IdempotencyConflictError(
            f"idempotency_key={idempotency_key!r} 被占但 owner 未在 "
            f"{self._dedup_timeout}s 内 commit"
        )

    async def commit(
        self,
        *,
        trace_id: str,
        result: str,
        latency: Optional[float] = None,
        cost: Optional[float] = None,
        hop_attribution: Optional[str] = None,
        arm: Optional[str] = None,
    ) -> None:
        """OWNER 调完 provider 后回填 result/latency/cost(CAS 的后半段)。

        reward/reward_committed_at 仍 Phase1 预留(不填)。
        hop_attribution:S1.5a 起可传(由 routing.hop 的 HopAttribution.to_json() 产出
        的 JSON 串);None(默认)= 不动该列,守现有调用点零回归。
        arm(A4,子片 0.4):可选 per-arm 标识,commit 时刻 model 已知
        (provider.complete() 返回)可构造;None(默认)= 不动该列,守零回归。
        若 acquire 已写 arm 而 commit 不传,arm 保持 acquire 值(不覆盖)。

        **B1(子片 0.3,2026-06-20):同事务双写 trace_hot**(WAL-02 热冷表分离激活)。
        trace 表 UPDATE(行由 acquire 创建)+ trace_hot 表
        `INSERT ... ON CONFLICT(trace_id) DO UPDATE`(首次 commit 插入,重复 commit
        幂等更新,PK=trace_id 防膨胀)。两条语句包在显式 BEGIN/COMMIT 内——任一失败
        ROLLBACK,不留残行(原子性)。acquire() 不写 hot(只有 commit 时刻
        result/latency/cost 齐全才双写)。trace_cold 迁移 defer A5(本切片不碰)。

        **SET 动态构造**:result/latency/cost 恒写;hop_attribution / arm 仅在非 None
        时追加(默认路径 SQL 文本与原 12 字段版完全一致,守 4 个现有调用点零行为变化)。
        """
        set_parts = ["result = ?", "latency = ?", "cost = ?"]
        params: list = [result, latency, cost]
        if hop_attribution is not None:
            set_parts.append("hop_attribution = ?")
            params.append(hop_attribution)
        if arm is not None:
            set_parts.append("arm = ?")
            params.append(arm)
        params.append(trace_id)
        await self._db.execute(
            f"UPDATE trace SET {', '.join(set_parts)} WHERE trace_id = ?",
            tuple(params),
        )
        await self._dual_write_hot(
            trace_id=trace_id,
            result=result,
            latency=latency,
            cost=cost,
            hop_attribution=hop_attribution,
            arm=arm,
        )

    async def _dual_write_hot(
        self,
        *,
        trace_id: str,
        result: str,
        latency: Optional[float],
        cost: Optional[float],
        hop_attribution: Optional[str],
        arm: Optional[str] = None,
    ) -> None:
        """B1:commit() 后把完整行镜像到 trace_hot(高频热表,WAL-02)。

        从 trace 表读出 acquire 时写入的不可变字段(correlation_id /
        parent_correlation_id / idempotency_key / provider / created_at / arm)+ commit
        回填的可变字段(result/latency/cost/hop_attribution),整行 upsert 进
        trace_hot。reward/reward_committed_at 仍 Phase1 预留(填 NULL)。

        arm(A4):从 trace 行读出(acquire 或 commit 写入的值),透传到 hot。
        commit 传 arm 时 trace 已先 UPDATE,本 SELECT 见最新值;acquire 写了 arm 而
        commit 未传,SELECT 仍见 acquire 值(不覆盖)。统一从 trace 读,避免双源不一致。

        **原子性**:与 trace 的 UPDATE 同处 autocommit 连接;trace 已先成功 UPDATE,
        本方法再 upsert hot。若 hot upsert 失败(如 UNIQUE(idempotency_key) 撞重——
        极少,仅当不同 trace_id 复用同 idempotency_key,而 trace 表 UNIQUE 已先拦),
        抛 IntegrityError 给调用方(不静默吞,守 WAL-02 双写一致性 fail-loud)。
        """
        async with self._db.execute(
            "SELECT correlation_id, parent_correlation_id, idempotency_key, "
            "       provider, created_at, arm FROM trace WHERE trace_id = ?",
            (trace_id,),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            # trace 行不存在(不应发生——commit 须在 acquire 之后);fail-loud 不静默。
            raise RuntimeError(
                f"commit() 双写 hot 时 trace 行 {trace_id!r} 不存在"
                "(acquire 必须先于 commit)"
            )
        await self._db.execute(
            "INSERT INTO trace_hot "
            "(trace_id, correlation_id, parent_correlation_id, idempotency_key, "
            " provider, result, latency, cost, reward, reward_committed_at, "
            " hop_attribution, created_at, arm) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?, ?) "
            "ON CONFLICT(trace_id) DO UPDATE SET "
            "  result = excluded.result, latency = excluded.latency, "
            "  cost = excluded.cost, hop_attribution = excluded.hop_attribution, "
            "  arm = excluded.arm",
            (
                trace_id,
                row["correlation_id"],
                row["parent_correlation_id"],
                row["idempotency_key"],
                row["provider"],
                result,
                latency,
                cost,
                hop_attribution,
                row["created_at"],
                row["arm"],
            ),
        )

    async def execute_idempotent(
        self,
        *,
        idempotency_key: str,
        correlation_id: str,
        provider: str,
        provider_fn: ProviderFn,
        parent_correlation_id: Optional[str] = None,
        trace_id: Optional[str] = None,
    ) -> str:
        """幂等执行:acquire → REPLAYED 返缓存;OWNER 调 provider_fn 再 commit。

        供 S2.x router 直接调:同 idempotency_key 的并发/重复请求,
        provider_fn 恰好执行一次(BUG-幂等-01 的运行时保证)。
        """
        outcome = await self.acquire(
            correlation_id=correlation_id,
            idempotency_key=idempotency_key,
            provider=provider,
            parent_correlation_id=parent_correlation_id,
            trace_id=trace_id,
        )
        if outcome.status is AcquireStatus.REPLAYED:
            assert outcome.cached_result is not None
            return outcome.cached_result
        result = await provider_fn()
        await self.commit(trace_id=outcome.trace_id, result=result)
        return result

    # ── correlation 一对多(BUG-correlation-03)─────────────────────

    async def get_chain(self, correlation_id: str) -> list[TraceRow]:
        """按 correlation_id 重建 fallback 链。

        返回该 correlation_id 下所有 hop,按 created_at(,rowid 兜底)升序
        = 提交顺序 = fallback 顺序。parent_correlation_id 字段保留树结构,
        供 S1.5a 树遍历(hop 语义届时定义,见 B-1)。
        """
        async with self._db.execute(
            "SELECT trace_id, correlation_id, parent_correlation_id, "
            "       idempotency_key, provider, result, latency, cost, "
            "       reward, reward_committed_at, hop_attribution, created_at, arm "
            "FROM trace WHERE correlation_id = ? "
            "ORDER BY created_at ASC, rowid ASC",
            (correlation_id,),
        ) as cur:
            rows = await cur.fetchall()
        return [
            TraceRow(
                trace_id=r["trace_id"],
                correlation_id=r["correlation_id"],
                parent_correlation_id=r["parent_correlation_id"],
                idempotency_key=r["idempotency_key"],
                provider=r["provider"],
                result=r["result"],
                latency=r["latency"],
                cost=r["cost"],
                reward=r["reward"],
                reward_committed_at=r["reward_committed_at"],
                hop_attribution=r["hop_attribution"],
                created_at=r["created_at"],
                arm=r["arm"],
            )
            for r in rows
        ]

    # ── A5(子片 0.5):trace 异步迁移 cold(WAL-02 热表有界)──────────────

    async def migrate_cold_once(
        self,
        *,
        max_age_seconds: float,
        batch_size: int = 500,
        now: Optional[datetime] = None,
    ) -> int:
        """把老热行从 trace_hot 迁 trace_cold,守 WAL-02 热表不膨胀。

        单事务原子:① INSERT 最老的 batch_size 行(created_at < cutoff)从 hot 进 cold
        (ON CONFLICT(trace_id) DO NOTHING,幂等防重复迁移);② DELETE 已确认落 cold
        的老 hot 行(本批 + 任何 re-added 旧行)。返回从 hot 删除的行数(WAL-02 有界指标)。

        cutoff = now - max_age_seconds;created_at < cutoff 严格(== 不迁)。
        created_at 存 ISO 8601 UTC 串,字典序 = 时间序,串比较正确。

        batch_size:限单次 INSERT 行数(渐进有界);剩余老行下次 tick 迁。DELETE 只删
        已落 cold 的行,故 hot 单次最多减 batch_size(+ re-added 旧行,见下)。

        **re-added 边界**(罕见):某行已迁 cold 后又被 commit 双写回 hot(同 trace_id
        upsert)。本方法 INSERT ON CONFLICT 不重复入 cold,但 DELETE 会把它从 hot 删
        (它老 + 已在 cold)→ deleted 可能 > 新增 cold 数。这对"hot 有界"是对的(老行
        不该留 hot),返回值反映 hot 实际减量。

        now:可注入固定时间(测试确定性);默认 datetime.now(timezone.utc)。
        """
        cutoff = (
            (now if now is not None else datetime.now(timezone.utc))
            - timedelta(seconds=max_age_seconds)
        ).isoformat()
        await self._db.execute("BEGIN")
        try:
            await self._db.execute(
                "INSERT INTO trace_cold "
                "(trace_id, correlation_id, parent_correlation_id, idempotency_key, "
                " provider, result, latency, cost, reward, reward_committed_at, "
                " hop_attribution, created_at, arm) "
                "SELECT trace_id, correlation_id, parent_correlation_id, idempotency_key, "
                "       provider, result, latency, cost, reward, reward_committed_at, "
                "       hop_attribution, created_at, arm "
                "FROM trace_hot WHERE created_at < ? "
                "ORDER BY created_at ASC LIMIT ? "
                "ON CONFLICT(trace_id) DO NOTHING",
                (cutoff, batch_size),
            )
            async with self._db.execute(
                "DELETE FROM trace_hot WHERE created_at < ? "
                "AND trace_id IN (SELECT trace_id FROM trace_cold)",
                (cutoff,),
            ) as cur:
                deleted = cur.rowcount
            await self._db.execute("COMMIT")
            return deleted if deleted is not None and deleted >= 0 else 0
        except Exception:
            await self._db.execute("ROLLBACK")
            raise


async def run_cold_migrator_loop(
    store: "TraceStore",
    stop_event: asyncio.Event,
    *,
    interval_seconds: float,
    max_age_seconds: float,
    batch_size: int = 500,
) -> None:
    """A5 后台循环:周期调 migrate_cold_once 把老热行迁 cold,stop_event 优雅停。

    **非自动接线**:本函数是后台任务原语,由调用方(lifespan / 独立 runner / 测试)
    `asyncio.create_task` 起、`stop_event.set()` 停。lifespan 自动接线 defer Phase B
    (TraceStore 由 Cascade 懒初始化,非 lifespan;S1.0 零污染 lifespan 红线;Phase1
    近零流量)。Phase B 启用:lifespan startup create_task 本循环、shutdown set+await。

    **未初始化容忍**:store 未 init(Cascade 首请求前 _conn=None)时 migrate_cold_once
    经 _db property 抛 RuntimeError → 本循环跳过该 tick(不崩),等 Cascade 懒初始化后
    再迁。其他异常 log warning 不崩(迁移是软优化,不该拖垮请求路径)。

    **cancellable sleep**:用 wait_for(stop_event.wait(), timeout=interval) 替代
    asyncio.sleep——stop_event.set() 即时唤醒退出,不等满一个 interval。
    """
    while not stop_event.is_set():
        try:
            await store.migrate_cold_once(
                max_age_seconds=max_age_seconds, batch_size=batch_size
            )
        except RuntimeError:
            # store 未 init(Cascade 懒初始化前);跳过,下个 tick 重试。
            pass
        except Exception:
            _LOG.warning("cold migration tick 失败(跳过,软优化不崩)", exc_info=True)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval_seconds)
        except asyncio.TimeoutError:
            pass
