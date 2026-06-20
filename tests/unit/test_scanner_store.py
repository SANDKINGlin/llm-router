"""S2.10-0.3 · scanner.db 持久化测试(快照 + 动态条目)。

守 spec「动态 diff 抓新免费模型」存储层契约:
- scan_snapshot:per source latest-wins UPSERT,load 无记录 → None
- dynamic_entry:首次入池 first_seen 保留,续命 last_seen 更新,expire 改 status
- active_models 返回 status=active 的 DiscoveredModel 列表(供 0.5 喂候选池)
- 重启不丢(init 幂等 + 持久化)
"""
from __future__ import annotations

import asyncio

from llm_router.scanner.snapshot import DiscoveredModel, ScannerSource, Snapshot
from llm_router.store.scanner_store import (
    ENTRY_STATUS_ACTIVE,
    ENTRY_STATUS_EXPIRED,
    DynamicEntryRow,
    ScannerStore,
)


def _run(coro):
    return asyncio.run(coro)


def _nv(mid: str, tier="strong", name="N") -> DiscoveredModel:
    return DiscoveredModel(
        source=ScannerSource.NVIDIA, model_id=mid, display_name=name, tier=tier
    )


def _snap(models, source=ScannerSource.NVIDIA, taken_at="2026-06-20T00:00:00+00:00"):
    return Snapshot(source=source, models=frozenset(models), taken_at=taken_at)


# ── 快照 ──────────────────────────────────────────────────────────

def test_snapshot_roundtrip(tmp_path):
    async def body():
        store = ScannerStore(tmp_path / "scanner.db")
        await store.init()
        try:
            assert await store.load_snapshot(ScannerSource.NVIDIA) is None  # 首次 None
            snap = _snap([_nv("a"), _nv("b")])
            await store.save_snapshot(snap)
            loaded = await store.load_snapshot(ScannerSource.NVIDIA)
            assert loaded is not None
            assert loaded.model_ids() == frozenset({"a", "b"})
            assert loaded.taken_at == snap.taken_at
        finally:
            await store.close()
    _run(body())


def test_snapshot_upsert_latest_wins(tmp_path):
    async def body():
        store = ScannerStore(tmp_path / "scanner.db")
        await store.init()
        try:
            await store.save_snapshot(_snap([_nv("a")], taken_at="t1"))
            await store.save_snapshot(_snap([_nv("b"), _nv("c")], taken_at="t2"))
            loaded = await store.load_snapshot(ScannerSource.NVIDIA)
            assert loaded is not None
            assert loaded.model_ids() == frozenset({"b", "c"})  # 覆盖,非合并
            assert loaded.taken_at == "t2"
        finally:
            await store.close()
    _run(body())


def test_snapshot_per_source_independent(tmp_path):
    async def body():
        store = ScannerStore(tmp_path / "scanner.db")
        await store.init()
        try:
            await store.save_snapshot(_snap([_nv("a")], source=ScannerSource.NVIDIA))
            await store.save_snapshot(
                Snapshot(
                    source=ScannerSource.OPENROUTER,
                    models=frozenset({DiscoveredModel(ScannerSource.OPENROUTER, "x:free")}),
                    taken_at="t",
                )
            )
            nv = await store.load_snapshot(ScannerSource.NVIDIA)
            orr = await store.load_snapshot(ScannerSource.OPENROUTER)
            assert nv is not None and nv.model_ids() == frozenset({"a"})
            assert orr is not None and orr.model_ids() == frozenset({"x:free"})
        finally:
            await store.close()
    _run(body())


# ── 动态条目 ──────────────────────────────────────────────────────

def test_upsert_entry_first_insert(tmp_path):
    async def body():
        store = ScannerStore(tmp_path / "scanner.db")
        await store.init()
        try:
            row = await store.upsert_entry(_nv("a"), interview_passed=True)
            assert row.model_id == "a"
            assert row.status == ENTRY_STATUS_ACTIVE
            assert row.interview_passed is True
            assert row.tier == "strong"
            assert row.first_seen == row.last_seen  # 首次相等
        finally:
            await store.close()
    _run(body())


def test_upsert_entry_keeps_first_seen_renews_last_seen(tmp_path):
    async def body():
        store = ScannerStore(tmp_path / "scanner.db")
        await store.init()
        try:
            await store.upsert_entry(_nv("a"), interview_passed=True, at="2026-06-20T01:00:00+00:00")
            await store.upsert_entry(_nv("a"), interview_passed=True, at="2026-06-20T02:00:00+00:00")
            row = await store.get_entry("a")
            assert row is not None
            assert row.first_seen == "2026-06-20T01:00:00+00:00"  # 保留首次
            assert row.last_seen == "2026-06-20T02:00:00+00:00"  # 续命
        finally:
            await store.close()
    _run(body())


def test_expire_entry_changes_status(tmp_path):
    async def body():
        store = ScannerStore(tmp_path / "scanner.db")
        await store.init()
        try:
            await store.upsert_entry(_nv("a"), interview_passed=True)
            row = await store.expire_entry("a")
            assert row is not None
            assert row.status == ENTRY_STATUS_EXPIRED
        finally:
            await store.close()
    _run(body())


def test_expire_entry_missing_returns_none_idempotent(tmp_path):
    async def body():
        store = ScannerStore(tmp_path / "scanner.db")
        await store.init()
        try:
            assert await store.expire_entry("nonexistent") is None  # 幂等不报错
        finally:
            await store.close()
    _run(body())


def test_list_entries_filter_by_status_and_source(tmp_path):
    async def body():
        store = ScannerStore(tmp_path / "scanner.db")
        await store.init()
        try:
            await store.upsert_entry(_nv("a"), interview_passed=True)
            await store.upsert_entry(_nv("b"), interview_passed=True)
            await store.upsert_entry(
                DiscoveredModel(ScannerSource.OPENROUTER, "x:free", tier="fast"),
                interview_passed=True,
            )
            await store.expire_entry("b")
            active = await store.list_entries(status=ENTRY_STATUS_ACTIVE)
            assert {r.model_id for r in active} == {"a", "x:free"}
            expired = await store.list_entries(status=ENTRY_STATUS_EXPIRED)
            assert {r.model_id for r in expired} == {"b"}
            nv_active = await store.list_entries(status=ENTRY_STATUS_ACTIVE, source=ScannerSource.NVIDIA)
            assert {r.model_id for r in nv_active} == {"a"}
        finally:
            await store.close()
    _run(body())


def test_active_models_returns_discovered_models(tmp_path):
    async def body():
        store = ScannerStore(tmp_path / "scanner.db")
        await store.init()
        try:
            await store.upsert_entry(_nv("a", tier="strong"), interview_passed=True)
            await store.upsert_entry(_nv("b", tier="fast"), interview_passed=True)
            await store.expire_entry("b")
            models = await store.active_models()
            assert {m.model_id for m in models} == {"a"}  # 只 active
            m = next(m for m in models if m.model_id == "a")
            assert m.tier == "strong"
            assert m.is_free is True  # 动态全免费
            assert m.source is ScannerSource.NVIDIA
        finally:
            await store.close()
    _run(body())


# ── 持久化 / 重启不丢 ─────────────────────────────────────────────

def test_persistence_across_reopen(tmp_path):
    async def body():
        db = tmp_path / "scanner.db"
        store = ScannerStore(db)
        await store.init()
        await store.save_snapshot(_snap([_nv("a")]))
        await store.upsert_entry(_nv("a"), interview_passed=True)
        await store.close()

        store2 = ScannerStore(db)
        await store2.init()
        try:
            snap = await store2.load_snapshot(ScannerSource.NVIDIA)
            assert snap is not None and snap.model_ids() == frozenset({"a"})
            row = await store2.get_entry("a")
            assert row is not None and row.status == ENTRY_STATUS_ACTIVE
        finally:
            await store2.close()
    _run(body())


def test_init_is_idempotent(tmp_path):
    async def body():
        store = ScannerStore(tmp_path / "scanner.db")
        await store.init()
        await store.upsert_entry(_nv("a"), interview_passed=True)
        await store.init()  # 二次 init 不丢数据
        row = await store.get_entry("a")
        assert row is not None
        await store.close()
    _run(body())


def test_not_init_raises(tmp_path):
    async def body():
        store = ScannerStore(tmp_path / "scanner.db")
        try:
            await store.load_snapshot(ScannerSource.NVIDIA)
            raise AssertionError("应抛 RuntimeError(未 init)")
        except RuntimeError:
            pass
    _run(body())


def test_entry_row_is_immutable(tmp_path):
    async def body():
        store = ScannerStore(tmp_path / "scanner.db")
        await store.init()
        try:
            row = await store.upsert_entry(_nv("a"), interview_passed=True)
            assert isinstance(row, DynamicEntryRow)
            try:
                row.status = "expired"  # type: ignore[misc]
                raise AssertionError("frozen dataclass 应不可变")
            except Exception:
                pass
        finally:
            await store.close()
    _run(body())
