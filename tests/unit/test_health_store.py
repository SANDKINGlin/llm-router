"""S2.8a · health.db 探活结果存储(数据层拆片)。

独立 SQLite WAL(data/health.db,同 design.md ⑦)。守 S2.8 验收(数据层半):
  - 验收(数据层):探活结果落盘 + 每 provider 一行最新状态(UPSERT)+ 重启不丢。
  - (探活循环 + 熔断接线留 S2.8b/c)
TDD:本套件先 RED——HealthStore 未实现时 import 即失败。
"""
from __future__ import annotations

import asyncio

from llm_router.store.health_store import HealthStore

EXPECTED_COLUMNS = {
    "provider",
    "last_probe_at",
    "latency_ms",
    "alive",
    "created_at",
    "updated_at",
}


def _run(coro):
    return asyncio.run(coro)


def test_health_table_schema(tmp_path):
    """6 字段在 + provider PK + alive/last_probe_at NOT NULL(PRAGMA)。"""

    async def body():
        store = HealthStore(tmp_path / "health.db")
        await store.init()
        try:
            cols = await store.columns()
            names = {c["name"] for c in cols}
            missing = EXPECTED_COLUMNS - names
            assert not missing, f"缺字段: {missing}"

            by = {c["name"]: c for c in cols}
            assert by["provider"]["pk"] == 1, "provider 必须 PRIMARY KEY"
            assert by["alive"]["notnull"] == 1, "alive 必须 NOT NULL"
            assert by["last_probe_at"]["notnull"] == 1, "last_probe_at 必须 NOT NULL"
        finally:
            await store.close()

    _run(body())


def test_record_probe_and_get(tmp_path):
    """record_probe → get 回查字段准确;alive=True 时 latency 记入。"""

    async def body():
        store = HealthStore(tmp_path / "health.db")
        await store.init()
        try:
            row = await store.record_probe("openrouter", latency_ms=123.4, alive=True)
            assert row.provider == "openrouter"
            assert row.alive is True
            assert row.latency_ms == 123.4
            assert row.last_probe_at  # 非空

            got = await store.get("openrouter")
            assert got is not None
            assert got.alive is True
            assert got.latency_ms == 123.4
        finally:
            await store.close()

    _run(body())


def test_record_probe_upsert_keeps_created_updates_rest(tmp_path):
    """同 provider 再探活:覆盖 last_probe_at/latency/alive/updated_at,保留 created_at。"""

    async def body():
        store = HealthStore(tmp_path / "health.db")
        await store.init()
        try:
            first = await store.record_probe("groq", latency_ms=50.0, alive=True)
            # 第二次探活:延迟变高、状态翻死
            second = await store.record_probe("groq", latency_ms=None, alive=False)
            assert second.alive is False
            assert second.latency_ms is None
            assert second.updated_at != first.updated_at, "updated_at 须刷新"
            assert second.last_probe_at != first.last_probe_at
            assert second.created_at == first.created_at, "created_at 须保留(首次入池时刻)"
            # 仍只有一行(provider PK)
            all_rows = await store.latest_alive()
            assert len(all_rows) == 1
        finally:
            await store.close()

    _run(body())


def test_record_probe_dead_with_null_latency(tmp_path):
    """探活失败:alive=False + latency_ms=None(超时/连不上无延迟测量)。"""

    async def body():
        store = HealthStore(tmp_path / "health.db")
        await store.init()
        try:
            row = await store.record_probe("nvidia", latency_ms=None, alive=False)
            assert row.alive is False
            assert row.latency_ms is None
        finally:
            await store.close()

    _run(body())


def test_latest_alive_filter_and_missing(tmp_path):
    """latest_alive(providers) 只返指定 + 跳过不存在;None 返全部(按 provider 升序)。"""

    async def body():
        store = HealthStore(tmp_path / "health.db")
        await store.init()
        try:
            await store.record_probe("alpha", latency_ms=10.0, alive=True)
            await store.record_probe("beta", latency_ms=20.0, alive=False)
            await store.record_probe("gamma", latency_ms=30.0, alive=True)

            all_rows = await store.latest_alive()
            assert [r.provider for r in all_rows] == ["alpha", "beta", "gamma"]

            subset = await store.latest_alive(["gamma", "beta", "ghost"])
            assert {r.provider for r in subset} == {"beta", "gamma"}  # ghost 跳过
            assert all(r.alive for r in subset if r.provider == "gamma")
            assert next(r for r in subset if r.provider == "beta").alive is False
        finally:
            await store.close()

    _run(body())


def test_probe_history_survives_restart(tmp_path):
    """重启持久化:close + reopen 新 store,探活状态不丢(同 task_state 验收①范式)。"""

    db = tmp_path / "health.db"

    async def phase1():
        store = HealthStore(db)
        await store.init()
        try:
            await store.record_probe("openrouter", latency_ms=80.0, alive=True)
            await store.record_probe("groq", latency_ms=None, alive=False)
        finally:
            await store.close()

    async def phase2():
        store = HealthStore(db)
        await store.init()
        try:
            orow = await store.get("openrouter")
            assert orow is not None
            assert orow.alive is True
            assert orow.latency_ms == 80.0
            grow = await store.get("groq")
            assert grow is not None
            assert grow.alive is False, "重启后死状态不丢"
            assert grow.latency_ms is None
        finally:
            await store.close()

    _run(phase1())
    _run(phase2())
