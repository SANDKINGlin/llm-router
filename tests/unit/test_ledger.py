"""S1.2 · token_ledger.db 计量(Phase 1 基础版)。

独立 SQLite WAL(design.md:与 trace 同频但语义独立,合并收益小,**保持独立** ledger.db)。
守两条验收:
  - 验收①:成功请求 token 准确计入
  - 验收②:流式中断不丢已计 token(begin_stream 边收边存,中断时行已含累计)

供 S2.4 Cost Budget Gate 消费 total() 聚合。
TDD:本套件先 RED——LedgerStore 未实现时 import 即失败。
"""
from __future__ import annotations

import asyncio

from llm_router.store.token_ledger import LedgerRow, LedgerStore

EXPECTED_COLUMNS = {
    "provider",
    "model",
    "prompt_tokens",
    "completion_tokens",
    "cost",
    "timestamp",
}


def _run(coro):
    return asyncio.run(coro)


def test_ledger_table_schema(tmp_path):
    """6 字段在 + prompt_tokens/completion_tokens NOT NULL(PRAGMA)。"""

    async def body():
        store = LedgerStore(tmp_path / "ledger.db")
        await store.init()
        try:
            cols = await store.columns()
            names = {c["name"] for c in cols}
            missing = EXPECTED_COLUMNS - names
            assert not missing, f"缺字段: {missing}"

            by = {c["name"]: c for c in cols}
            assert by["prompt_tokens"]["notnull"] == 1, "prompt_tokens 必须 NOT NULL"
            assert (
                by["completion_tokens"]["notnull"] == 1
            ), "completion_tokens 必须 NOT NULL"
        finally:
            await store.close()

    _run(body())


def test_record_successful_request(tmp_path):
    """验收①:完整请求 record → 回查 tokens + cost 准确。"""

    async def body():
        store = LedgerStore(tmp_path / "ledger.db")
        await store.init()
        try:
            lid = await store.record(
                provider="openrouter",
                model="free-model-x",
                prompt_tokens=120,
                completion_tokens=80,
                cost=0.0007,
            )
            row = await store.get(lid)
            assert row is not None
            assert row.provider == "openrouter"
            assert row.model == "free-model-x"
            assert row.prompt_tokens == 120
            assert row.completion_tokens == 80
            assert row.cost == 0.0007
            assert row.timestamp  # 非空
        finally:
            await store.close()

    _run(body())


def test_streaming_interruption_keeps_counted_tokens(tmp_path):
    """验收②:流式中断不丢已计 token。

    begin_stream 建行(completion_tokens=0)→ 边收边 add_completion_tokens
    → 模拟中断(不 finalize)→ 回查行,completion_tokens == 累计值(80)。
    token 边收边存是关键:中断时已计 token 已落盘,不丢。
    """

    async def body():
        store = LedgerStore(tmp_path / "ledger.db")
        await store.init()
        try:
            lid = await store.begin_stream(
                provider="openrouter",
                model="free-model-y",
                prompt_tokens=50,
            )
            # 流式:两个 chunk 到达就更新账本
            await store.add_completion_tokens(lid, 50)
            await store.add_completion_tokens(lid, 30)
            # ⚠ 模拟中断:连接断开,不调 finalize。已计 80 token 必须仍在账本。
            row = await store.get(lid)
            assert row is not None
            assert row.prompt_tokens == 50, "prompt_tokens 已在 begin_stream 计入"
            assert row.completion_tokens == 80, (
                "流式中断后已计 completion_tokens 必须保留(不丢)"
            )
        finally:
            await store.close()

    _run(body())


def test_total_aggregation(tmp_path):
    """total() 聚合求和(供 S2.4 Cost Budget Gate 消费)。"""

    async def body():
        store = LedgerStore(tmp_path / "ledger.db")
        await store.init()
        try:
            await store.record(
                provider="p1", model="m1", prompt_tokens=100, completion_tokens=40, cost=0.1
            )
            await store.record(
                provider="p1", model="m2", prompt_tokens=200, completion_tokens=60, cost=0.2
            )
            await store.record(
                provider="p2", model="m3", prompt_tokens=50, completion_tokens=10, cost=0.05
            )

            all_total = await store.total()
            assert all_total["rows"] == 3
            assert all_total["prompt_tokens"] == 350
            assert all_total["completion_tokens"] == 110
            assert abs(all_total["cost"] - 0.35) < 1e-9

            p1 = await store.total(provider="p1")
            assert p1["rows"] == 2
            assert p1["prompt_tokens"] == 300
            assert p1["completion_tokens"] == 100
            assert abs(p1["cost"] - 0.3) < 1e-9
        finally:
            await store.close()

    _run(body())
