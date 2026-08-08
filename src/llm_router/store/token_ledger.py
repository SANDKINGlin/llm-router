"""S1.2 · token_ledger.db 计量(Phase 1 基础版)。

5 个独立 SQLite WAL 之一(`data/ledger.db`,design.md:与 trace 同频但语义独立,
合并收益小,**保持独立**)。

流式中断不丢已计 token:begin_stream 建行(completion_tokens=0)→ 边收边
add_completion_tokens(UPDATE +=delta,token 边到边落盘)→ 即便连接中断,
已计 token 已在盘上,不丢。

供 S2.4 Cost Budget Gate 消费 total() 聚合。cost 在 Phase1 可 NULL
(免费 provider;真实 cost 在 S2.4 按 token×倍率算)。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import aiosqlite

_SCHEMA = """\
CREATE TABLE IF NOT EXISTS token_ledger (
    ledger_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    provider           TEXT NOT NULL,
    model              TEXT NOT NULL,
    prompt_tokens      INTEGER NOT NULL,
    completion_tokens  INTEGER NOT NULL,
    cost               REAL,
    timestamp          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ledger_provider ON token_ledger(provider);
"""

LEDGER_COLUMNS: tuple[str, ...] = (
    "ledger_id",
    "provider",
    "model",
    "prompt_tokens",
    "completion_tokens",
    "cost",
    "timestamp",
)


@dataclass(frozen=True)
class LedgerRow:
    ledger_id: int
    provider: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    cost: Optional[float]
    timestamp: str


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class LedgerStore:
    """token 计量(独立 WAL ledger.db)。连接用 autocommit(同 trace.py 模式):
    INSERT/UPDATE 原子提交,流式增量边收边存,中断不丢。
    """

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = str(db_path)
        self._conn: Optional[aiosqlite.Connection] = None

    async def init(self) -> None:
        if Path(self._db_path).parent.is_symlink():
            Path(self._db_path).parent.unlink()
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
            raise RuntimeError("LedgerStore 未 init()")
        return self._conn

    async def columns(self) -> list[dict]:
        """PRAGMA table_info(token_ledger)。"""
        async with self._db.execute("PRAGMA table_info(token_ledger)") as cur:
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

    async def record(
        self,
        *,
        provider: str,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
        cost: Optional[float] = None,
    ) -> int:
        """一次性记录完整请求(验收①),返回 ledger_id。"""
        cur = await self._db.execute(
            "INSERT INTO token_ledger "
            "(provider, model, prompt_tokens, completion_tokens, cost, timestamp) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (provider, model, prompt_tokens, completion_tokens, cost, _now_iso()),
        )
        assert cur.lastrowid is not None
        return cur.lastrowid

    async def begin_stream(
        self,
        *,
        provider: str,
        model: str,
        prompt_tokens: int,
        cost: Optional[float] = None,
    ) -> int:
        """流式计量起点:建行(completion_tokens=0,prompt_tokens 已计入)。

        返回 ledger_id;后续边收边 add_completion_tokens。
        """
        cur = await self._db.execute(
            "INSERT INTO token_ledger "
            "(provider, model, prompt_tokens, completion_tokens, cost, timestamp) "
            "VALUES (?, ?, ?, 0, ?, ?)",
            (provider, model, prompt_tokens, cost, _now_iso()),
        )
        assert cur.lastrowid is not None
        return cur.lastrowid

    async def add_completion_tokens(self, ledger_id: int, delta: int) -> None:
        """流式增量:completion_tokens += delta(原子 UPDATE)。

        token 边收边落盘是「中断不丢」的关键:即便连接断开,已 add 的量已在盘上。
        """
        await self._db.execute(
            "UPDATE token_ledger SET completion_tokens = completion_tokens + ? "
            "WHERE ledger_id = ?",
            (delta, ledger_id),
        )

    async def get(self, ledger_id: int) -> Optional[LedgerRow]:
        async with self._db.execute(
            "SELECT ledger_id, provider, model, prompt_tokens, completion_tokens, "
            "       cost, timestamp FROM token_ledger WHERE ledger_id = ?",
            (ledger_id,),
        ) as cur:
            r = await cur.fetchone()
        if r is None:
            return None
        return LedgerRow(
            ledger_id=r["ledger_id"],
            provider=r["provider"],
            model=r["model"],
            prompt_tokens=r["prompt_tokens"],
            completion_tokens=r["completion_tokens"],
            cost=r["cost"],
            timestamp=r["timestamp"],
        )

    async def total(self, provider: Optional[str] = None) -> dict:
        """聚合求和(供 S2.4 Cost Budget Gate)。

        返回 {rows, prompt_tokens, completion_tokens, cost}。cost 为 NULL 的行
        按 0 计入(COALESCE)。
        """
        sql = (
            "SELECT COUNT(*), COALESCE(SUM(prompt_tokens), 0), "
            "       COALESCE(SUM(completion_tokens), 0), COALESCE(SUM(cost), 0) "
            "FROM token_ledger"
        )
        params: tuple = ()
        if provider is not None:
            sql += " WHERE provider = ?"
            params = (provider,)
        async with self._db.execute(sql, params) as cur:
            row = await cur.fetchone()
        assert row is not None
        return {
            "rows": row[0],
            "prompt_tokens": row[1],
            "completion_tokens": row[2],
            "cost": row[3],
        }
