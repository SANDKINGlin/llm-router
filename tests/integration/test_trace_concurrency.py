"""S1.1 · 并发幂等回归门(BUG-幂等-01 的并发核心)。

N 个并发请求同 idempotency_key → provider 恰好调一次。
UNIQUE(idempotency_key) + CAS acquire() 是并发闸:
  - 第一个 acquire 成 OWNER,负责调 provider 并 commit
  - 其余 acquire 命中 UNIQUE,落入 in-flight poll,等 OWNER commit 后返缓存
无 CAS 时 N 个请求会各自调 provider → N 次(BUG-幂等-01 重现)。

每个并发请求用独立 TraceStore/连接,模拟真实并发请求(非共享连接序列化)。

R40 P158 治本: 改用 pytest-asyncio 模式, 不再自己 asyncio.run().
  - 之前 5/20 flake (25%) "Event loop is closed" = pytest-asyncio fixture loop 跟 asyncio.run() 新 loop 冲突
  - 现在用 pytest-asyncio @pytest.mark.asyncio + asyncio.gather, fixture loop 自动管
"""
from __future__ import annotations

import asyncio
import pytest

from llm_router.store.trace import TraceStore

N = 12


@pytest.mark.asyncio
async def test_concurrent_same_key_executes_once(tmp_path):
    """验收①核心:N 并发同 idempotency_key → provider 恰好一次。"""

    calls = {"n": 0}
    db_path = tmp_path / "trace.db"

    async def provider_fn() -> str:
        calls["n"] += 1
        # 轻微延迟,确保 loser 落入 in-flight poll 路径(OWNER 尚未 commit),
        # 而非直接命中已 commit 的快路径——更能暴露并发竞态。
        await asyncio.sleep(0.03)
        return "ok"

    # R40 P158: 不用 asyncio.run(), 直接 await 协程 (pytest-asyncio fixture loop 自动管)
    store = TraceStore(db_path)
    await store.init()
    try:
        results = await asyncio.gather(*(
            store.execute_idempotent(
                idempotency_key="shared-key",
                correlation_id="CID",
                provider="pA",
                provider_fn=provider_fn,
            )
            for _ in range(N)
        ))
    finally:
        await store.close()

    assert all(r == "ok" for r in results), "所有并发请求应拿到同一结果"
    assert calls["n"] == 1, (
        f"provider 应恰好调一次(UNIQUE+CAS 拦并发),实际 {calls['n']} 次"
    )
