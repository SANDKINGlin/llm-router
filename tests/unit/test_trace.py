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

# 蓝图 §4 S1.1 的 12 字段 + A4(子片 0.4)per-arm `arm` 列 = 13 字段(权威字段名)。
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
    "arm",
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


# ── S1.1 增强子片 0.4(2026-06-20,A4):per-arm trace 列扩展 ──────────────
# arm = provider+model+account_key 复合标识(同 bandit_state.db arm PK 语义),
# 供 S3+ bandit 按臂统计 reward。本切片只做 schema 演化 + 读写管线
# (列预留 + acquire/commit/TraceRow/get_chain/双写透传 arm);arm 的具体编码与
# reward 归因 defer S3+(bandit_state.py:14「具体编码 S3+ 决,留 TEXT 灵活」)。
# account_key = api_key_env 名(非 secret 本身,scanner/mnfst.py:58),存储安全。


def test_arm_column_present_in_all_three_tables(tmp_path):
    """A4 schema:trace / trace_hot / trace_cold 三表均有 `arm TEXT` 列(末列)。"""
    import sqlite3

    async def body():
        store = TraceStore(tmp_path / "trace.db")
        await store.init()
        try:
            for tbl in ("trace", "trace_hot", "trace_cold"):
                cols = await store._table_columns(tbl)
                assert "arm" in cols, f"{tbl} 缺 arm 列;实际 {cols}"
        finally:
            await store.close()

    _run(body())

    with sqlite3.connect(tmp_path / "trace.db") as conn:
        for tbl in ("trace", "trace_hot", "trace_cold"):
            cols = [r[1] for r in conn.execute(f"PRAGMA table_info({tbl})")]
            assert cols[-1] == "arm", (
                f"{tbl} 的 arm 应为末列(ALTER ADD COLUMN 追加语义,迁移一致);"
                f"实际末列 {cols[-1]!r},全列 {cols}"
            )


def test_arm_migration_adds_column_to_existing_db(tmp_path):
    """A4 迁移:已存在的旧库(无 arm 列)经 init() 后三表均补 arm 列,幂等。

    CREATE TABLE IF NOT EXISTS 不会给已存在的表加列;init() 须显式 ALTER ADD
    COLUMN 补 arm。再次 init() 不报错(幂等:duplicate column name 静默跳过)。
    """
    import sqlite3

    db = tmp_path / "trace.db"
    # 旧 schema(无 arm):手工建三表,模拟 Phase 1 升级前库存。
    old_cols = (
        "trace_id TEXT PRIMARY KEY, correlation_id TEXT NOT NULL, "
        "parent_correlation_id TEXT, idempotency_key TEXT NOT NULL UNIQUE, "
        "provider TEXT NOT NULL, result TEXT, latency REAL, cost REAL, "
        "reward REAL, reward_committed_at TEXT, hop_attribution TEXT, "
        "created_at TEXT NOT NULL"
    )
    with sqlite3.connect(db) as conn:
        for tbl in ("trace", "trace_hot", "trace_cold"):
            conn.execute(f"CREATE TABLE {tbl} ({old_cols})")

    async def body():
        store = TraceStore(db)
        await store.init()
        try:
            pass
        finally:
            await store.close()
        # 幂等:再 init 一次不报错。
        store2 = TraceStore(db)
        await store2.init()
        await store2.close()

    _run(body())

    with sqlite3.connect(db) as conn:
        for tbl in ("trace", "trace_hot", "trace_cold"):
            cols = [r[1] for r in conn.execute(f"PRAGMA table_info({tbl})")]
            assert "arm" in cols, f"迁移后 {tbl} 应有 arm 列;实际 {cols}"


def test_acquire_writes_arm_into_trace(tmp_path):
    """A4:acquire(arm=...) 把 arm 写进 trace 行(acquire 时刻已知 arm 的路径)。

    acquire 后 trace_hot 仍空(只有 commit 双写),与 B1 一致。
    """
    import sqlite3

    async def body():
        store = TraceStore(tmp_path / "trace.db")
        await store.init()
        try:
            o = await store.acquire(
                correlation_id="cid-arm-acq",
                idempotency_key="idem-arm-acq",
                provider="p-arm",
                arm="p-arm/m-arm/k-arm",
            )
            assert o.status == AcquireStatus.OWNER
            async with store._db.execute("SELECT arm FROM trace WHERE trace_id=?",
                                         (o.trace_id,)) as cur:
                assert (await cur.fetchone())[0] == "p-arm/m-arm/k-arm"
            async with store._db.execute("SELECT COUNT(*) FROM trace_hot") as cur:
                assert (await cur.fetchone())[0] == 0, "acquire 不应写 hot"
        finally:
            await store.close()

    _run(body())
    # 重新连直查确认落盘
    with sqlite3.connect(tmp_path / "trace.db") as conn:
        assert conn.execute("SELECT arm FROM trace").fetchone()[0] == "p-arm/m-arm/k-arm"


def test_commit_arm_flows_to_trace_and_hot(tmp_path):
    """A4:commit(arm=...) 回填 trace.arm 并经 _dual_write_hot 透传到 trace_hot。

    commit 时刻 model 已知(provider.complete() 返回),arm 在此构造并落库。
    逐列比对 trace 与 trace_hot 的 arm 一致(守 B1 双写一致性)。
    """
    import sqlite3

    from llm_router.store.trace import TRACE_COLUMNS

    async def body():
        store = TraceStore(tmp_path / "trace.db")
        await store.init()
        try:
            o = await store.acquire(
                correlation_id="cid-arm-commit",
                idempotency_key="idem-arm-commit",
                provider="p-arm2",
            )
            await store.commit(
                trace_id=o.trace_id,
                result="r-arm",
                latency=9.0,
                cost=0.01,
                arm="p-arm2/m-arm2/k-arm2",
            )
        finally:
            await store.close()

    _run(body())

    with sqlite3.connect(tmp_path / "trace.db") as conn:
        trace_arm = conn.execute("SELECT arm FROM trace").fetchone()[0]
        hot_arm = conn.execute("SELECT arm FROM trace_hot").fetchone()[0]
        assert trace_arm == "p-arm2/m-arm2/k-arm2"
        assert hot_arm == "p-arm2/m-arm2/k-arm2", (
            f"trace_hot 应透传 arm(B1 双写 + A4 透传);实际 {hot_arm!r}"
        )
        # 完整逐列一致性(TRACE_COLUMNS 含 arm,B1 契约扩展)
        trace_row = dict(zip(
            [r[1] for r in conn.execute("PRAGMA table_info(trace)")],
            conn.execute("SELECT * FROM trace").fetchone(),
        ))
        hot_row = dict(zip(
            [r[1] for r in conn.execute("PRAGMA table_info(trace_hot)")],
            conn.execute("SELECT * FROM trace_hot").fetchone(),
        ))
        for col in TRACE_COLUMNS:
            assert trace_row[col] == hot_row[col], (
                f"列 {col} 不一致: trace={trace_row[col]!r} hot={hot_row[col]!r}"
            )


def test_commit_without_arm_zero_regression(tmp_path):
    """A4 零回归:不传 arm → trace/trace_hot 的 arm 均为 NULL(现有调用点不变)。"""
    import sqlite3

    async def body():
        store = TraceStore(tmp_path / "trace.db")
        await store.init()
        try:
            o = await store.acquire(
                correlation_id="cid-noarm",
                idempotency_key="idem-noarm",
                provider="p-noarm",
            )
            await store.commit(trace_id=o.trace_id, result="r-noarm")
        finally:
            await store.close()

    _run(body())

    with sqlite3.connect(tmp_path / "trace.db") as conn:
        assert conn.execute("SELECT arm FROM trace").fetchone()[0] is None
        assert conn.execute("SELECT arm FROM trace_hot").fetchone()[0] is None


def test_get_chain_returns_arm(tmp_path):
    """A4:get_chain() 返回的 TraceRow 携带 arm 字段。"""
    async def body():
        store = TraceStore(tmp_path / "trace.db")
        await store.init()
        try:
            o = await store.acquire(
                correlation_id="cid-chain",
                idempotency_key="idem-chain",
                provider="p-chain",
                arm="p-chain/m-chain/k-chain",
            )
            await store.commit(trace_id=o.trace_id, result="r-chain")
            chain = await store.get_chain("cid-chain")
            assert len(chain) == 1
            assert chain[0].arm == "p-chain/m-chain/k-chain"
        finally:
            await store.close()

    _run(body())


# ── S1.1 增强子片 0.5(2026-06-20,A5):trace 异步迁移 cold(WAL-02 热表有界)──
# 后台把老热行从 trace_hot 迁 trace_cold,守热表不膨胀。本切片交付迁移原语
# `migrate_cold_once` + 后台循环原语 `run_cold_migrator_loop`;lifespan 自动接线
# defer Phase B(TraceStore 懒初始化 + S1.0 零污染 lifespan 红线 + Phase1 近零流量)。

import datetime as _dt  # noqa: E402

from llm_router.store.trace import run_cold_migrator_loop  # noqa: E402

_UTC = _dt.timezone.utc


async def _insert_hot(store, *, trace_id, created_at, arm=None, result="r"):
    """测试辅助:直接往 trace_hot 插一行(带可控 created_at,绕过 _now_iso)。"""
    await store._db.execute(
        "INSERT INTO trace_hot "
        "(trace_id, correlation_id, parent_correlation_id, idempotency_key, "
        " provider, result, latency, cost, reward, reward_committed_at, "
        " hop_attribution, created_at, arm) "
        "VALUES (?, ?, NULL, ?, ?, ?, 1.0, 0.01, NULL, NULL, NULL, ?, ?)",
        (trace_id, f"cid-{trace_id}", f"idem-{trace_id}", "p", result, created_at, arm),
    )


def test_migrate_cold_moves_old_rows_to_cold(tmp_path):
    """A5:老热行(created_at < cutoff)迁 trace_cold,新行留 trace_hot;返回迁移数。"""
    import sqlite3

    now = _dt.datetime(2026, 6, 20, 10, 1, 0, tzinfo=_UTC)

    async def body():
        store = TraceStore(tmp_path / "trace.db")
        await store.init()
        try:
            await _insert_hot(store, trace_id="old-1", created_at="2026-06-20T09:58:00+00:00")
            await _insert_hot(store, trace_id="old-2", created_at="2026-06-20T09:59:30+00:00")
            await _insert_hot(store, trace_id="new-1", created_at="2026-06-20T10:00:30+00:00")
            n = await store.migrate_cold_once(max_age_seconds=60.0, now=now)
            assert n == 2, f"应迁 2 行(2 老);实际 {n}"
        finally:
            await store.close()

    _run(body())

    with sqlite3.connect(tmp_path / "trace.db") as conn:
        hot = {r[0] for r in conn.execute("SELECT trace_id FROM trace_hot")}
        cold = {r[0] for r in conn.execute("SELECT trace_id FROM trace_cold")}
        assert hot == {"new-1"}, f"hot 应只剩新行;实际 {hot}"
        assert cold == {"old-1", "old-2"}, f"cold 应含 2 老行;实际 {cold}"


def test_migrate_cold_preserves_all_fields_including_arm(tmp_path):
    """A5:迁移逐列保真(含 A4 的 arm),cold 行字段 == 原 hot 行字段。"""
    import sqlite3

    from llm_router.store.trace import TRACE_COLUMNS

    now = _dt.datetime(2026, 6, 20, 10, 1, 0, tzinfo=_UTC)

    async def body():
        store = TraceStore(tmp_path / "trace.db")
        await store.init()
        try:
            await _insert_hot(
                store, trace_id="full-1", created_at="2026-06-20T09:00:00+00:00",
                arm="p/m/k", result="r-full",
            )
            await store.migrate_cold_once(max_age_seconds=60.0, now=now)
        finally:
            await store.close()

    _run(body())

    with sqlite3.connect(tmp_path / "trace.db") as conn:
        hot_count = conn.execute("SELECT COUNT(*) FROM trace_hot").fetchone()[0]
        assert hot_count == 0, "hot 应空(已迁)"
        cold_row = dict(zip(
            [r[1] for r in conn.execute("PRAGMA table_info(trace_cold)")],
            conn.execute("SELECT * FROM trace_cold").fetchone(),
        ))
        for col in TRACE_COLUMNS:
            assert cold_row[col] is not None or col in (
                "parent_correlation_id", "reward", "reward_committed_at", "hop_attribution"
            ), f"列 {col} 意外 None:{cold_row[col]!r}"
        # 关键保真断言(含 A4 arm)
        assert cold_row["trace_id"] == "full-1"
        assert cold_row["arm"] == "p/m/k", f"arm 应保真迁移;实际 {cold_row['arm']!r}"
        assert cold_row["result"] == "r-full"
        assert cold_row["latency"] == 1.0
        assert cold_row["cost"] == 0.01


def test_migrate_cold_idempotent(tmp_path):
    """A5:重复迁移不重复入 cold(ON CONFLICT DO NOTHING),hot 不残留。"""
    import sqlite3

    now = _dt.datetime(2026, 6, 20, 10, 1, 0, tzinfo=_UTC)

    async def body():
        store = TraceStore(tmp_path / "trace.db")
        await store.init()
        try:
            await _insert_hot(store, trace_id="old-1", created_at="2026-06-20T09:00:00+00:00")
            n1 = await store.migrate_cold_once(max_age_seconds=60.0, now=now)
            n2 = await store.migrate_cold_once(max_age_seconds=60.0, now=now)
            assert n1 == 1
            assert n2 == 0, f"二次迁移应 0(hot 已空);实际 {n2}"
        finally:
            await store.close()

    _run(body())

    with sqlite3.connect(tmp_path / "trace.db") as conn:
        assert conn.execute("SELECT COUNT(*) FROM trace_cold").fetchone()[0] == 1, "cold 不应重复"
        assert conn.execute("SELECT COUNT(*) FROM trace_hot").fetchone()[0] == 0


def test_migrate_cold_batch_size_limits_per_tick(tmp_path):
    """A5:batch_size 限单次 INSERT 行数;剩余老行下次 tick 迁(渐进有界)。"""
    import sqlite3

    now = _dt.datetime(2026, 6, 20, 10, 1, 0, tzinfo=_UTC)

    async def body():
        store = TraceStore(tmp_path / "trace.db")
        await store.init()
        try:
            for i in range(5):
                await _insert_hot(
                    store, trace_id=f"old-{i}", created_at=f"2026-06-20T09:00:0{i}+00:00",
                )
            n1 = await store.migrate_cold_once(max_age_seconds=60.0, batch_size=2, now=now)
            assert n1 == 2, f"首批应迁 2(batch_size=2);实际 {n1}"
            n2 = await store.migrate_cold_once(max_age_seconds=60.0, batch_size=2, now=now)
            assert n2 == 2
            n3 = await store.migrate_cold_once(max_age_seconds=60.0, batch_size=2, now=now)
            assert n3 == 1, f"末批应迁 1(剩 1);实际 {n3}"
        finally:
            await store.close()

    _run(body())

    with sqlite3.connect(tmp_path / "trace.db") as conn:
        assert conn.execute("SELECT COUNT(*) FROM trace_cold").fetchone()[0] == 5
        assert conn.execute("SELECT COUNT(*) FROM trace_hot").fetchone()[0] == 0


def test_migrate_cold_zero_old_rows_noop(tmp_path):
    """A5:无老行 → 迁 0,cold 空,hot 不变(空操作不报错)。"""
    import sqlite3

    now = _dt.datetime(2026, 6, 20, 10, 1, 0, tzinfo=_UTC)

    async def body():
        store = TraceStore(tmp_path / "trace.db")
        await store.init()
        try:
            await _insert_hot(store, trace_id="new-1", created_at="2026-06-20T10:00:30+00:00")
            n = await store.migrate_cold_once(max_age_seconds=60.0, now=now)
            assert n == 0
        finally:
            await store.close()

    _run(body())

    with sqlite3.connect(tmp_path / "trace.db") as conn:
        assert conn.execute("SELECT COUNT(*) FROM trace_cold").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM trace_hot").fetchone()[0] == 1


def test_migrate_cold_age_threshold_boundary_exclusive(tmp_path):
    """A5:created_at == cutoff 不迁(< 严格);恰早一秒迁。"""
    import sqlite3

    # cutoff = now - 60s = 10:00:00。boundary 行 created_at=10:00:00 → 不迁;
    # 早一秒 09:59:59 → 迁。
    now = _dt.datetime(2026, 6, 20, 10, 1, 0, tzinfo=_UTC)

    async def body():
        store = TraceStore(tmp_path / "trace.db")
        await store.init()
        try:
            await _insert_hot(store, trace_id="boundary", created_at="2026-06-20T10:00:00+00:00")
            await _insert_hot(store, trace_id="just-before", created_at="2026-06-20T09:59:59+00:00")
            n = await store.migrate_cold_once(max_age_seconds=60.0, now=now)
            assert n == 1, f"仅 just-before 迁(boundary == cutoff 不迁);实际 {n}"
        finally:
            await store.close()

    _run(body())

    with sqlite3.connect(tmp_path / "trace.db") as conn:
        cold = {r[0] for r in conn.execute("SELECT trace_id FROM trace_cold")}
        hot = {r[0] for r in conn.execute("SELECT trace_id FROM trace_hot")}
        assert cold == {"just-before"}
        assert hot == {"boundary"}


def test_cold_migrator_loop_migrates_and_stops(tmp_path):
    """A5:run_cold_migrator_loop 后台周期迁移;stop_event 优雅停。"""
    import sqlite3

    async def body():
        store = TraceStore(tmp_path / "trace.db")
        await store.init()
        # 老行(created_at 远早于 now),max_age=0 → 全部老行迁。
        await _insert_hot(store, trace_id="old-1", created_at="2020-01-01T00:00:00+00:00")
        stop = asyncio.Event()
        task = asyncio.create_task(run_cold_migrator_loop(
            store, stop, interval_seconds=0.05, max_age_seconds=0.0,
        ))
        # 等一两个 tick 让迁移发生(interval 50ms)。
        await asyncio.sleep(0.2)
        stop.set()
        await asyncio.wait_for(task, timeout=2.0)
        await store.close()

    _run(body())

    with sqlite3.connect(tmp_path / "trace.db") as conn:
        assert conn.execute("SELECT COUNT(*) FROM trace_cold").fetchone()[0] == 1, "loop 应迁老行入 cold"
        assert conn.execute("SELECT COUNT(*) FROM trace_hot").fetchone()[0] == 0


def test_cold_migrator_loop_skips_uninitialized_store(tmp_path):
    """A5:store 未 init 时 loop 不崩(跳过 tick,等 Cascade 懒初始化)。"""
    store = TraceStore(tmp_path / "trace.db")  # 未 init

    async def body():
        stop = asyncio.Event()
        task = asyncio.create_task(run_cold_migrator_loop(
            store, stop, interval_seconds=0.01, max_age_seconds=60.0,
        ))
        await asyncio.sleep(0.05)
        stop.set()
        await asyncio.wait_for(task, timeout=2.0)  # 不抛即过

    _run(body())
