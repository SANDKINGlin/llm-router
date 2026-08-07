"""S2.8a · health.db 探活结果存储(Phase 1 基础版,数据层拆片)。

5 个独立 SQLite WAL 之一(`data/health.db`,与 trace/token_ledger/task_state/circuit
各自锁、各自崩溃恢复,**不合并**,同 design.md ⑦)。

用途(S2.8 整体):每 5min ping fallback/paid key,结果写独立 health.db(**不污染 trace**);
熔断恢复优先参考探活结果(修 BUG-fallback-01/02)。本拆片只做**数据层**——
探活循环(scheduler/定时器)+ 熔断接线留 S2.8b/c。

模型:**每 provider 一行最新状态**(provider PK,UPSERT)。每次探活覆盖该 provider 的
last_probe_at/latency_ms/alive。取「最新 alive 状态」即一行读,O(1),供熔断/路由
切换前 fast 查询。历史明细(若 Phase2 要趋势分析)可另开表,本切片不做(YAGNI)。

连接用 autocommit(isolation_level=None),同 token_ledger.py/task_state.py 模式:
INSERT...ON CONFLICT(provider) DO UPDATE 原子 UPSERT,WAL + busy_timeout 让并发写者排队。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import aiosqlite

_SCHEMA = """\
CREATE TABLE IF NOT EXISTS health (
    provider      TEXT PRIMARY KEY,
    last_probe_at TEXT NOT NULL,
    latency_ms    REAL,
    alive         INTEGER NOT NULL,  -- 0/1(bool 存 INTEGER,SQLite 无原生 BOOL)
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_health_alive ON health(alive);
"""


@dataclass(frozen=True)
class HealthRow:
    provider: str
    last_probe_at: str
    latency_ms: Optional[float]
    alive: bool
    created_at: str
    updated_at: str


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class HealthStore:
    """health.db 探活结果(每 provider 一行最新状态,UPSERT)。连接用 autocommit
    (同 token_ledger.py/task_state.py 模式):UPSERT 原子提交,WAL + busy_timeout 排队并发写。
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
            raise RuntimeError("HealthStore 未 init()")
        return self._conn

    # ── schema 自省(ops/测试)──────────────────────────────────────

    async def columns(self) -> list[dict]:
        """PRAGMA table_info(health)。"""
        async with self._db.execute("PRAGMA table_info(health)") as cur:
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
    def _row(r: aiosqlite.Row) -> HealthRow:
        return HealthRow(
            provider=r["provider"],
            last_probe_at=r["last_probe_at"],
            latency_ms=r["latency_ms"],
            alive=bool(r["alive"]),
            created_at=r["created_at"],
            updated_at=r["updated_at"],
        )

    async def get(self, provider: str) -> Optional[HealthRow]:
        async with self._db.execute(
            "SELECT provider, last_probe_at, latency_ms, alive, created_at, updated_at "
            "FROM health WHERE provider = ?",
            (provider,),
        ) as cur:
            r = await cur.fetchone()
        return self._row(r) if r is not None else None

    async def latest_probe(
        self,
        providers: Optional[list[str]] = None,
        *,
        alive_only: bool = False,
    ) -> list[HealthRow]:
        """返回各 provider 的最新探活行(供熔断/路由切换前 fast 查询)。

        providers=None → 全部;否则只含指定 provider(不存在的跳过)。
        alive_only=True → 只返 alive=True 的行(供「哪些 provider 还能用」查询)。
            闭合名实 bug(CODEX 实证):旧名 latest_alive 暗示过滤 alive 但实际不过滤——
            改名 latest_probe(诚实:返最新探活行,死活都含),要过滤显式传 alive_only=True,
            让熔断/路由切换前的 caller 不会因名字误导而漏过滤死 provider。
        """
        if providers is None:
            sql = (
                "SELECT provider, last_probe_at, latency_ms, alive, created_at, updated_at "
                "FROM health"
                + (" WHERE alive = 1" if alive_only else "")
                + " ORDER BY provider ASC"
            )
            async with self._db.execute(sql) as cur:
                rows = await cur.fetchall()
            return [self._row(r) for r in rows]
        out: list[HealthRow] = []
        for p in providers:
            row = await self.get(p)
            if row is not None and (not alive_only or row.alive):
                out.append(row)
        return out

    # ── 写(UPSERT)─────────────────────────────────────────────────

    async def record_probe(
        self,
        provider: str,
        *,
        latency_ms: Optional[float],
        alive: bool,
        at: Optional[str] = None,
    ) -> HealthRow:
        """记一次探活结果(UPSERT:同 provider 覆盖为最新,但**旧戳不盖新戳**)。

        latency_ms 可 None(探活超时/连接失败,无延迟测量);alive 必填。
        at:可选,显式探活时刻(测试注入 / 排队探活回填;生产默认 now)。须可与 last_probe_at
            同序比较(标准 ISO 格式,_now_iso 满足)。
        返回该 provider 当前行——若新戳更老被单调守卫拒绝,返回的是现有(更新)行,不变更。
        created_at 仅首次插入写,后续 UPSERT 保留(守「首次入池时刻」)。

        单调性守卫(闭合 CODEX 实证 bug):UPSERT 仅当新戳 >= 现有戳才更新——多副本 / 启动期
        时钟漂移下旧探活后到不会倒退熔断恢复判断。
        # ponytail: SQL `<=` 文本比较依赖 last_probe_at 同格式(均来自 _now_iso / 一致 ISO)。
        #   若未来允许异构格式戳,改 Python 端 datetime 解析比较(同 task_state HIGH #1 范式)。
        """
        ts = at if at is not None else _now_iso()
        now = _now_iso()  # updated_at 始终真实写入时刻,与探活时刻 ts 解耦
        await self._db.execute(
            "INSERT INTO health "
            "(provider, last_probe_at, latency_ms, alive, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(provider) DO UPDATE SET "
            "  last_probe_at = excluded.last_probe_at, "
            "  latency_ms = excluded.latency_ms, "
            "  alive = excluded.alive, "
            "  updated_at = excluded.updated_at "
            "WHERE health.last_probe_at <= excluded.last_probe_at",
            (provider, ts, latency_ms, 1 if alive else 0, now, now),
        )
        row = await self.get(provider)
        assert row is not None
        return row
