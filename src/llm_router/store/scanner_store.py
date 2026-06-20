"""S2.10-0.3 · scanner.db 动态 Scanner 持久化(快照 + 入池贴标条目)。

5 个独立 SQLite WAL 体系新增第 6 个 `data/scanner.db`(同 design ⑦ 独立锁/独立崩溃恢复
纪律;与 health.db 同为侧挂低频写,但语义独立——scanner 记"发现了什么/入池了什么",
health 记"探活存活",不合并)。

两张表:
- `scan_snapshot`:每 source 一行**最新快照**(source PK,models JSON,taken_at)。
  供 0.5 编排下次轮询 diff 的 prev(latest-wins,同 health 单调;但 scanner 快照无单调
  守卫——轮询天然时序,后到的就是新,taken_at 仅记录用)。
- `dynamic_entry`:通过面试(0.4)入池贴标的动态模型(model_id PK,source,display_name,
  tier,status=active|expired,first_seen,last_seen,interview_passed)。
  - `active` = 当前在池可路由;`expired` = 过期清退(下架/面试失改/狠限流)。
  - first_seen 首次入池(UPSERT 保留);last_seen 最近一次轮询仍发现该模型(续命)。

连接用 autocommit(isolation_level=None),同 health_store.py/token_ledger.py 模式:
UPSERT 原子提交,WAL + busy_timeout 排队并发写。

红线(守 routing-priority-principle):dynamic_entry 只存贴标元数据(tier/source/name/status),
路由选择时由 0.5 把 active entries 喂候选池;排序键仍字典序,is_free=True/cost=0 的动态
entry 与静态免费 provider 同档竞争。tier 只进能力匹配首槽,不进排序键加权。
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import aiosqlite

from ..scanner.snapshot import DiscoveredModel, ScannerSource, Snapshot

_SCHEMA = """\
CREATE TABLE IF NOT EXISTS scan_snapshot (
    source     TEXT PRIMARY KEY,
    models     TEXT NOT NULL,       -- JSON: list[DiscoveredModel dict]
    taken_at   TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS dynamic_entry (
    model_id           TEXT PRIMARY KEY,
    source             TEXT NOT NULL,
    display_name       TEXT,
    tier               TEXT,
    status             TEXT NOT NULL,    -- active | expired
    interview_passed   INTEGER NOT NULL, -- 0/1
    first_seen         TEXT NOT NULL,
    last_seen          TEXT NOT NULL,
    updated_at         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_dynamic_entry_status ON dynamic_entry(status);
CREATE INDEX IF NOT EXISTS idx_dynamic_entry_source ON dynamic_entry(source);
"""

ENTRY_STATUS_ACTIVE = "active"
ENTRY_STATUS_EXPIRED = "expired"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class DynamicEntryRow:
    """dynamic_entry 一行(入池贴标的动态模型)。"""

    model_id: str
    source: str
    display_name: Optional[str]
    tier: Optional[str]
    status: str
    interview_passed: bool
    first_seen: str
    last_seen: str
    updated_at: str


def _model_to_dict(m: DiscoveredModel) -> dict:
    return {
        "source": m.source.value,
        "model_id": m.model_id,
        "display_name": m.display_name,
        "tier": m.tier,
        "is_free": m.is_free,
    }


def _model_from_dict(d: dict) -> DiscoveredModel:
    return DiscoveredModel(
        source=ScannerSource(d["source"]),
        model_id=d["model_id"],
        display_name=d.get("display_name"),
        tier=d.get("tier"),
        is_free=d.get("is_free", True),
    )


def _snapshot_to_json(snap: Snapshot) -> str:
    return json.dumps([_model_to_dict(m) for m in snap.models])


def _snapshot_from_json(source: ScannerSource, raw: str, taken_at: str) -> Snapshot:
    rows = json.loads(raw) if raw else []
    return Snapshot(
        source=source,
        models=frozenset(_model_from_dict(r) for r in rows),
        taken_at=taken_at,
    )


class ScannerStore:
    """scanner.db:最新快照(per source)+ 入池动态条目(per model_id)。

    连接 autocommit(isolation_level=None),同 health_store.py 模式。init() 幂等。
    """

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = str(db_path)
        self._conn: Optional[aiosqlite.Connection] = None

    async def init(self) -> None:
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
            raise RuntimeError("ScannerStore 未 init()")
        return self._conn

    # ── 快照(per source,latest-wins)──────────────────────────────

    async def save_snapshot(self, snap: Snapshot) -> None:
        """UPSERT 某 source 最新快照(供下次轮询 diff 的 prev)。"""
        now = _now_iso()
        await self._db.execute(
            "INSERT INTO scan_snapshot (source, models, taken_at, updated_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(source) DO UPDATE SET "
            "  models = excluded.models, taken_at = excluded.taken_at, "
            "  updated_at = excluded.updated_at",
            (snap.source.value, _snapshot_to_json(snap), snap.taken_at, now),
        )

    async def load_snapshot(self, source: ScannerSource) -> Optional[Snapshot]:
        """读某 source 最新快照;无记录 → None(首次轮询)。"""
        async with self._db.execute(
            "SELECT models, taken_at FROM scan_snapshot WHERE source = ?",
            (source.value,),
        ) as cur:
            r = await cur.fetchone()
        if r is None:
            return None
        return _snapshot_from_json(source, r["models"], r["taken_at"])

    # ── 动态条目(per model_id)────────────────────────────────────

    @staticmethod
    def _entry_row(r: aiosqlite.Row) -> DynamicEntryRow:
        return DynamicEntryRow(
            model_id=r["model_id"],
            source=r["source"],
            display_name=r["display_name"],
            tier=r["tier"],
            status=r["status"],
            interview_passed=bool(r["interview_passed"]),
            first_seen=r["first_seen"],
            last_seen=r["last_seen"],
            updated_at=r["updated_at"],
        )

    async def upsert_entry(
        self,
        model: DiscoveredModel,
        *,
        interview_passed: bool,
        status: str = ENTRY_STATUS_ACTIVE,
        at: Optional[str] = None,
    ) -> DynamicEntryRow:
        """UPSERT 动态条目(面试通过入池 / 续命 last_seen / 清退改 status)。

        - 首次插入(first_seen = now,status=active 默认)。
        - 已存在:first_seen 保留,last_seen 续命(更新为 now),status/interview_passed/tier
          按 caller 指定覆写(0.5 编排用此改 expired)。
        返回该行最新状态。
        """
        ts = at if at is not None else _now_iso()
        now = _now_iso()
        await self._db.execute(
            "INSERT INTO dynamic_entry "
            "(model_id, source, display_name, tier, status, interview_passed, "
            " first_seen, last_seen, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(model_id) DO UPDATE SET "
            "  source = excluded.source, display_name = excluded.display_name, "
            "  tier = excluded.tier, status = excluded.status, "
            "  interview_passed = excluded.interview_passed, "
            "  last_seen = excluded.last_seen, updated_at = excluded.updated_at",
            (
                model.model_id,
                model.source.value,
                model.display_name,
                model.tier,
                status,
                1 if interview_passed else 0,
                ts,  # first_seen(首次发现/面试时刻;仅首次插入生效,UPSERT 不覆写)
                ts,  # last_seen(最近一次轮询仍发现,续命)
                now,  # updated_at(真实写入时刻,与发现时刻 ts 解耦)
            ),
        )
        # NOTE:first_seen 在 ON CONFLICT 分支未列在 UPDATE SET,故保留首次值(守"首次入池时刻")。
        row = await self.get_entry(model.model_id)
        assert row is not None
        return row

    async def get_entry(self, model_id: str) -> Optional[DynamicEntryRow]:
        async with self._db.execute(
            "SELECT model_id, source, display_name, tier, status, interview_passed, "
            "first_seen, last_seen, updated_at FROM dynamic_entry WHERE model_id = ?",
            (model_id,),
        ) as cur:
            r = await cur.fetchone()
        return self._entry_row(r) if r is not None else None

    async def list_entries(
        self,
        *,
        status: Optional[str] = None,
        source: Optional[ScannerSource] = None,
    ) -> list[DynamicEntryRow]:
        """列动态条目;status/source 可选过滤。供 0.5 取 active 喂候选池 / 取 expired 审计。"""
        sql = (
            "SELECT model_id, source, display_name, tier, status, interview_passed, "
            "first_seen, last_seen, updated_at FROM dynamic_entry WHERE 1=1"
        )
        params: list = []
        if status is not None:
            sql += " AND status = ?"
            params.append(status)
        if source is not None:
            sql += " AND source = ?"
            params.append(source.value)
        sql += " ORDER BY model_id ASC"
        async with self._db.execute(sql, params) as cur:
            rows = await cur.fetchall()
        return [self._entry_row(r) for r in rows]

    async def expire_entry(self, model_id: str, at: Optional[str] = None) -> Optional[DynamicEntryRow]:
        """把某条目标记 expired(过期清退,0.5 在 diff.removed 时调)。不改 last_seen(留作
        最后一次发现时刻,便于审计何时下架)。无此条目 → None(幂等,不报错)。"""
        ts = at if at is not None else _now_iso()
        await self._db.execute(
            "UPDATE dynamic_entry SET status = ?, updated_at = ? WHERE model_id = ?",
            (ENTRY_STATUS_EXPIRED, ts, model_id),
        )
        return await self.get_entry(model_id)

    async def active_models(self) -> list[DiscoveredModel]:
        """所有 status=active 条目 → DiscoveredModel 列表(供 0.5 喂候选池)。

        排序按 model_id 稳定(不引入路由偏好——路由选择归 EpsilonGreedy 字典序)。
        """
        rows = await self.list_entries(status=ENTRY_STATUS_ACTIVE)
        return [
            DiscoveredModel(
                source=ScannerSource(r.source),
                model_id=r.model_id,
                display_name=r.display_name,
                tier=r.tier,
                is_free=True,  # 动态 scanner 只抓免费(0.2 poller 已过滤)
            )
            for r in rows
        ]


def load_active_models_sync(db_path: str | Path) -> list[DiscoveredModel]:
    """同步只读 scanner.db 的 active 动态条目(Phase B · B2.1)。

    供 ``app._build_cascade`` 在 **import 期**(同步上下文)读 scanner.db,把动态候选热入
    候选池。用 stdlib sqlite3 read-only 打开(零 aiosqlite 依赖,不创建文件、不写 WAL),
    与 ScannerStore.active_models() 同语义(active 条目 → DiscoveredModel)。

    fail-open(同 health_store fail-open 理念):
      - db 文件不存在 → [](向后兼容:无 scanner.db = 无动态,候选池退化两层)
      - 读失败(sqlite 异常/表不存在/损坏)→ [](不崩 import,守 routing-change-safety)

    排序按 model_id 稳定(同 active_models,不引入路由偏好)。

    红线:只读,不写;不 init schema(表不存在 → except 返 [])。
    """
    p = Path(db_path)
    if not p.exists():
        return []
    try:
        # URI mode=ro:只读,防意外写;db 损坏/表缺失 → sqlite3.OperationalError → except。
        conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT model_id, source, display_name, tier FROM dynamic_entry "
            "WHERE status = ? ORDER BY model_id ASC",
            (ENTRY_STATUS_ACTIVE,),
        ).fetchall()
        conn.close()
    except sqlite3.Error:
        return []
    return [
        DiscoveredModel(
            source=ScannerSource(r["source"]),
            model_id=r["model_id"],
            display_name=r["display_name"],
            tier=r["tier"],
            is_free=True,
        )
        for r in rows
    ]
