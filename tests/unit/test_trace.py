"""S1.1 · trace.db 持久化 + 幂等 CAS + correlation 一对多(Phase 1 基础版)。

守两个 CRITICAL 回归门:
  - BUG-幂等-01:并发/重复同 idempotency_key 请求,provider 只调一次
    (UNIQUE(idempotency_key) 是并发闸,acquire() 做 compare-and-swap)
  - BUG-correlation-03:跨 provider fallback 链可由 correlation_id 重建
    (一对多:每 hop 新 trace_id,parent_correlation_id 指向上一 hop)

Phase 1 不做:热冷表分离(WAL-02)、bandit_state 预留(均 Phase 2 S1.1增强)。
reward/reward_committed_at/hop_attribution 在本切片只建字段(可 NULL),
hop 计数语义留给 S1.5a(B-1 精度点,勿在此写死)。

TDD:本套件先 RED——TraceStore 未实现时,import 即失败。
"""
from __future__ import annotations

import asyncio

from llm_router.store.trace import AcquireStatus, TraceStore

# 蓝图 §4 S1.1 的 12 字段(权威字段名,类型由实施定)。
EXPECTED_COLUMNS = {
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
}


def _run(coro):
    """同步测试函数包 asyncio 事件循环(免 pytest-asyncio 依赖,不动 hash 锁)。"""
    return asyncio.run(coro)


def test_trace_table_schema(tmp_path):
    """12 字段全在;idempotency_key NOT NULL + UNIQUE(PRAGMA 证明)。

    UNIQUE 不用"重复 acquire 抛 IntegrityError"验证——acquire() 的 CAS 设计是
    捕获 IntegrityError 后轮询返缓存(那才是幂等语义)。故 UNIQUE 用 PRAGMA
    index_list/index_info 证明存在覆盖 idempotency_key 的 unique 索引(CAS 并发闸)。
    """

    async def body():
        store = TraceStore(tmp_path / "trace.db")
        await store.init()
        try:
            cols = await store.columns()  # PRAGMA table_info
            names = {c["name"] for c in cols}
            missing = EXPECTED_COLUMNS - names
            assert not missing, f"缺字段: {missing}"

            idem = next(c for c in cols if c["name"] == "idempotency_key")
            assert idem["notnull"] == 1, "idempotency_key 必须 NOT NULL"

            uniques = await store.unique_indexes()  # PRAGMA index_list/info
            assert any("idempotency_key" in u["columns"] for u in uniques), (
                "idempotency_key 须有 UNIQUE 约束(CAS 并发闸)"
            )
        finally:
            await store.close()

    _run(body())


def test_idempotency_cas_dedup(tmp_path):
    """验收①:同 idempotency_key 提交两次 → provider 只调一次,第二次返缓存。

    BUG-幂等-01 回归门(串行版;并发版见 integration/test_trace_concurrency)。
    """

    calls = {"n": 0}

    async def provider_fn():
        calls["n"] += 1
        return "real-result"

    async def body():
        store = TraceStore(tmp_path / "trace.db")
        await store.init()
        try:
            r1 = await store.execute_idempotent(
                idempotency_key="k",
                correlation_id="cid",
                provider="pa",
                provider_fn=provider_fn,
            )
            r2 = await store.execute_idempotent(
                idempotency_key="k",
                correlation_id="cid",
                provider="pa",
                provider_fn=provider_fn,
            )
            assert r1 == "real-result"
            assert r2 == r1, "第二次应返回缓存 result,与首次一致"
            assert calls["n"] == 1, f"provider 应只调一次,实际 {calls['n']}"
        finally:
            await store.close()

    _run(body())


def test_reward_fields_nullable_phase1(tmp_path):
    """reward/reward_committed_at/hop_attribution 可全 NULL 插入。

    Phase 1 不填这三个字段(schema 预留),但字段必须在、且可空。
    hop_attribution 计数语义留 S1.5a(B-1):本切片只建字段,不写 hop 逻辑。
    """

    async def body():
        store = TraceStore(tmp_path / "trace.db")
        await store.init()
        try:
            out = await store.acquire(
                correlation_id="cid", idempotency_key="k", provider="pa"
            )
            # 只 commit result,不动 reward/reward_committed_at/hop_attribution。
            await store.commit(trace_id=out.trace_id, result="r")
            rows = await store.get_chain("cid")
            assert len(rows) == 1
            row = rows[0]
            assert row.reward is None
            assert row.reward_committed_at is None
            assert row.hop_attribution is None
        finally:
            await store.close()

    _run(body())


def test_correlation_reconstructs_fallback_chain(tmp_path):
    """验收②:3-hop 链 A→B→C,按 correlation_id 重建(parent 链对、顺序对)。

    BUG-correlation-03 回归门:correlation_id 一对多,
    每 hop 新 trace_id,parent_correlation_id 指向上一 hop。
    """

    async def body():
        store = TraceStore(tmp_path / "trace.db")
        await store.init()
        try:
            # Hop A(首 hop,parent=None)
            a = await store.acquire(
                correlation_id="CID", idempotency_key="kA", provider="pA"
            )
            await store.commit(
                trace_id=a.trace_id, result="rA", latency=10.0, cost=0.1
            )
            # Hop B(parent=A,fallback 到下一 provider)
            b = await store.acquire(
                correlation_id="CID",
                parent_correlation_id=a.trace_id,
                idempotency_key="kB",
                provider="pB",
            )
            await store.commit(
                trace_id=b.trace_id, result="rB", latency=20.0, cost=0.2
            )
            # Hop C(parent=B,fallback 再下一层)
            c = await store.acquire(
                correlation_id="CID",
                parent_correlation_id=b.trace_id,
                idempotency_key="kC",
                provider="pC",
            )
            await store.commit(
                trace_id=c.trace_id, result="rC", latency=30.0, cost=0.3
            )

            chain = await store.get_chain("CID")
            assert len(chain) == 3
            # 顺序:hop 提交顺序 = fallback 链顺序(A→B→C)
            assert [r.provider for r in chain] == ["pA", "pB", "pC"]
            # parent 链:A 无 parent,B→A,C→B
            by_prov = {r.provider: r for r in chain}
            assert by_prov["pA"].parent_correlation_id is None
            assert by_prov["pB"].parent_correlation_id == by_prov["pA"].trace_id
            assert by_prov["pC"].parent_correlation_id == by_prov["pB"].trace_id
        finally:
            await store.close()

    _run(body())


# ── S1.1 增强子片 0.2(2026-06-19,A2):热冷表 schema 预留(WAL-02 优化路径) ──


def test_hot_cold_tables_coexist_with_trace_phase2_schema_reservation(tmp_path):
    """A2 契约:`trace_hot` / `trace_cold` 两表与 `trace` 共存,字段一致。

    **本切片范围**:仅建表,不接读写路径。`trace` 表保留为 Phase 1 写入入口
    (向后兼容,329p 测试不动);Phase 2 后续子片接 commit() 双写 hot + 异步迁移 cold。

    断言:
      - 三表都存在(sqlite_master 查询)
      - hot/cold 表 columns 与 trace 表完全一致(PRAGMA table_info)
      - hot/cold 表现在为空(无写入路径触动)
      - 现有 trace 写入(acquire+commit)只入 trace 表,不污染 hot/cold(隔离守门)
    """
    import sqlite3

    from llm_router.store.trace import (
        TRACE_COLD_COLUMNS,
        TRACE_COLUMNS,
        TRACE_HOT_COLUMNS,
    )

    async def body():
        store = TraceStore(tmp_path / "trace.db")
        await store.init()
        try:
            # 现有 trace 写入路径(Phase 1 不动):acquire + commit
            o = await store.acquire(
                correlation_id="cid-A2",
                idempotency_key="idem-A2",
                provider="p-A2",
            )
            assert o.status == AcquireStatus.OWNER
            await store.commit(trace_id=o.trace_id, result="r-A2")
        finally:
            await store.close()

    _run(body())

    # 三表共存(sqlite3 直查 sqlite_master)
    with sqlite3.connect(tmp_path / "trace.db") as conn:
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'trace%'"
            )
        }
        assert tables == {"trace", "trace_hot", "trace_cold"}, (
            f"三表(trace/trace_hot/trace_cold)应共存;实际 {tables}"
        )

        # hot/cold 字段与 trace 完全一致(共享 TRACE_COLUMNS)
        for tbl in ("trace_hot", "trace_cold"):
            cols = [r[1] for r in conn.execute(f"PRAGMA table_info({tbl})")]
            assert tuple(cols) == TRACE_COLUMNS, (
                f"{tbl} 字段应与 TRACE_COLUMNS 一致;实际 {cols}"
            )

        # 静态契约:HOT_COLUMNS / COLD_COLUMNS 元组就是 TRACE_COLUMNS 同源(共享标识)
        assert TRACE_HOT_COLUMNS is TRACE_COLUMNS
        assert TRACE_COLD_COLUMNS is TRACE_COLUMNS

        # 隔离守门:现有 trace 写入只入 trace 表,**不污染** hot/cold
        # (本切片范围:hot/cold 仅 schema,无双写路径)
        trace_count = conn.execute("SELECT COUNT(*) FROM trace").fetchone()[0]
        hot_count = conn.execute("SELECT COUNT(*) FROM trace_hot").fetchone()[0]
        cold_count = conn.execute("SELECT COUNT(*) FROM trace_cold").fetchone()[0]
        assert trace_count == 1, f"trace 应有 1 行(acquire+commit);实际 {trace_count}"
        assert hot_count == 0, (
            f"trace_hot 不该被现有路径写入(本切片仅 schema);实际 {hot_count} 行"
        )
        assert cold_count == 0, (
            f"trace_cold 不该被现有路径写入(本切片仅 schema);实际 {cold_count} 行"
        )
