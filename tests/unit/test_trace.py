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
    """A2 契约(B1 翻面):`trace_hot` / `trace_cold` 两表与 `trace` 共存,字段一致。

    **范围演化**:A2(子片 0.2)仅建 schema,断言 hot/cold 为空;B1(子片 0.3)接
    `commit()` 双写 hot 后,本测试翻面——commit 后 `trace_hot` 有行(双写激活),
    `trace_cold` 仍为空(cold 迁移 defer A5)。字段一致 + 共存断言不变。

    断言:
      - 三表都存在(sqlite_master 查询)
      - hot/cold 表 columns 与 trace 表完全一致(PRAGMA table_info)
      - commit 后:trace 1 行 + trace_hot 1 行(双写);trace_cold 0 行(迁移 defer A5)
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

        # B1 双写守门:commit 后 trace 与 trace_hot 各 1 行(同事务双写);
        # trace_cold 仍 0 行(cold 迁移 defer A5,本切片不碰)。
        trace_count = conn.execute("SELECT COUNT(*) FROM trace").fetchone()[0]
        hot_count = conn.execute("SELECT COUNT(*) FROM trace_hot").fetchone()[0]
        cold_count = conn.execute("SELECT COUNT(*) FROM trace_cold").fetchone()[0]
        assert trace_count == 1, f"trace 应有 1 行(acquire+commit);实际 {trace_count}"
        assert hot_count == 1, (
            f"trace_hot 应被 commit() 双写 1 行(B1 激活);实际 {hot_count} 行"
        )
        assert cold_count == 0, (
            f"trace_cold 不该被写入(cold 迁移 defer A5);实际 {cold_count} 行"
        )


# ── S1.1 增强子片 0.3(2026-06-20,B1):commit() 双写 trace_hot(WAL-02 激活)──


def test_commit_dual_writes_trace_and_hot_identical_fields(tmp_path):
    """B1 核心契约:commit() 同事务双写 trace + trace_hot,两表行**所有字段一致**。

    acquire() 不写 hot(只有 commit 时刻 result/latency/cost 齐全才双写)。
    字段一致性逐列比对(防双写漏字段/值漂移)。
    """
    import sqlite3

    from llm_router.store.trace import TRACE_COLUMNS

    async def body():
        store = TraceStore(tmp_path / "trace.db")
        await store.init()
        try:
            o = await store.acquire(
                correlation_id="cid-B1",
                idempotency_key="idem-B1",
                provider="p-B1",
            )
            # acquire 后 hot 仍空(只有 commit 双写)
            async with store._db.execute("SELECT COUNT(*) FROM trace_hot") as cur:
                assert (await cur.fetchone())[0] == 0, "acquire 不应写 hot"
            await store.commit(
                trace_id=o.trace_id, result="r-B1", latency=12.5, cost=0.03
            )
        finally:
            await store.close()

    _run(body())

    with sqlite3.connect(tmp_path / "trace.db") as conn:
        trace_row = dict(
            zip(
                [r[1] for r in conn.execute("PRAGMA table_info(trace)")],
                conn.execute(
                    "SELECT * FROM trace WHERE trace_id = (SELECT trace_id FROM trace LIMIT 1)"
                ).fetchone(),
            )
        )
        hot_row = dict(
            zip(
                [r[1] for r in conn.execute("PRAGMA table_info(trace_hot)")],
                conn.execute(
                    "SELECT * FROM trace_hot WHERE trace_id = (SELECT trace_id FROM trace_hot LIMIT 1)"
                ).fetchone(),
            )
        )
        # 逐列比对(TRACE_COLUMNS 是权威字段清单)
        for col in TRACE_COLUMNS:
            assert trace_row[col] == hot_row[col], (
                f"列 {col} 不一致: trace={trace_row[col]!r} hot={hot_row[col]!r}"
            )
        # 关键值核对
        assert hot_row["result"] == "r-B1"
        assert hot_row["latency"] == 12.5
        assert hot_row["cost"] == 0.03
        assert hot_row["reward"] is None  # Phase1 预留
        assert hot_row["hop_attribution"] is None  # 默认分支


def test_commit_dual_write_hop_attribution_branch(tmp_path):
    """B1:hop_attribution 非 None 分支也双写 hot(覆盖 commit() 两个 SQL 分支)。"""
    import sqlite3

    async def body():
        store = TraceStore(tmp_path / "trace.db")
        await store.init()
        try:
            o = await store.acquire(
                correlation_id="cid-hop", idempotency_key="idem-hop", provider="p-hop"
            )
            await store.commit(
                trace_id=o.trace_id,
                result="r-hop",
                latency=5.0,
                cost=0.01,
                hop_attribution='{"depth":1,"reason":"breaker"}',
            )
        finally:
            await store.close()

    _run(body())

    with sqlite3.connect(tmp_path / "trace.db") as conn:
        hot_hop = conn.execute("SELECT hop_attribution FROM trace_hot").fetchone()[0]
        trace_hop = conn.execute("SELECT hop_attribution FROM trace").fetchone()[0]
    assert hot_hop == '{"depth":1,"reason":"breaker"}'
    assert hot_hop == trace_hop, "hop_attribution 分支双写值应一致"


def test_commit_dual_write_idempotent_on_repeat(tmp_path):
    """B1:重复 commit 同 trace_id 不在 hot 留重复行(hot PK=trace_id,UPDATE 幂等)。

    现有 4 调用点不重复 commit,但守门防未来误用:hot 不该因重复 commit 膨胀。
    """
    import sqlite3

    async def body():
        store = TraceStore(tmp_path / "trace.db")
        await store.init()
        try:
            o = await store.acquire(
                correlation_id="cid-rep", idempotency_key="idem-rep", provider="p-rep"
            )
            await store.commit(trace_id=o.trace_id, result="r1")
            # 重复 commit(非正常路径,但守门)
            await store.commit(trace_id=o.trace_id, result="r2")
        finally:
            await store.close()

    _run(body())

    with sqlite3.connect(tmp_path / "trace.db") as conn:
        hot_n = conn.execute("SELECT COUNT(*) FROM trace_hot").fetchone()[0]
        trace_n = conn.execute("SELECT COUNT(*) FROM trace").fetchone()[0]
    assert hot_n == 1, f"重复 commit 后 trace_hot 应仍 1 行(PK 幂等);实际 {hot_n}"
    assert trace_n == 1


def test_execute_idempotent_dual_writes_hot(tmp_path):
    """B1:execute_idempotent(OWNER 路径)内部 commit 也双写 hot(端到端守门)。"""
    import sqlite3

    async def provider_fn():
        return "exec-result"

    async def body():
        store = TraceStore(tmp_path / "trace.db")
        await store.init()
        try:
            r = await store.execute_idempotent(
                idempotency_key="k-exec",
                correlation_id="cid-exec",
                provider="p-exec",
                provider_fn=provider_fn,
            )
            assert r == "exec-result"
        finally:
            await store.close()

    _run(body())

    with sqlite3.connect(tmp_path / "trace.db") as conn:
        hot_n = conn.execute("SELECT COUNT(*) FROM trace_hot").fetchone()[0]
        hot_result = conn.execute("SELECT result FROM trace_hot").fetchone()[0]
    assert hot_n == 1
    assert hot_result == "exec-result"
