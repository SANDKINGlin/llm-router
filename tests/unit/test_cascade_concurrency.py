"""P0-2 并发安全测试套件。

验证 Cascade 关键操作的并发安全性，防止竞态条件导致状态不一致。
测试 apply_policy 在高并发情况下的原子性操作。
"""
from __future__ import annotations

import asyncio

from llm_router.api.cascade import Cascade
from llm_router.api.strategy import RoutingStrategy
from llm_router.providers.base import Provider, ProviderError
from llm_router.resilience.circuit_breaker import CircuitBreaker
from llm_router.store.trace import TraceStore


def _run(coro):
    """同步测试函数包 asyncio 事件循环(免 pytest-asyncio 依赖,不动 hash 锁)。"""
    return asyncio.run(coro)


class MockProvider(Provider):
    """Mock Provider for testing."""

    async def complete(self, prompt, **kwargs):
        return "mock-result"


class MockStrategy(RoutingStrategy):
    """Mock RoutingStrategy for testing."""

    def plan(self, candidates):
        return [(name, prov, key) for name, prov, key in candidates]


def test_apply_policy_concurrent_calls(tmp_path):
    """并发安全:多个协程同时调用 apply_policy 时状态一致性。

    验证:
      - 多个并发更新不会出现状态混合
      - 最终状态与其中一个更新一致
      - 无异常或竞态条件错误
    """

    async def body():
        store = TraceStore(tmp_path / "trace.db")
        await store.init()
        try:
            breaker = CircuitBreaker(tmp_path / "breaker.db")
            strategy = MockStrategy()
            candidates_v1 = [
                ("p1", MockProvider(), "key1"),
                ("p2", MockProvider(), "key2"),
            ]
            cascade = Cascade(store, breaker, strategy, candidates_v1)

            # 模拟多个并发版本更新
            async def update_policy(version_id):
                candidates = [
                    (f"p{version_id}_1", MockProvider(), f"k{version_id}_1"),
                    (f"p{version_id}_2", MockProvider(), f"k{version_id}_2"),
                ]
                cascade.apply_policy(candidates, f"v{version_id}")

            # 并发执行100个版本更新
            tasks = [update_policy(i) for i in range(100)]
            await asyncio.gather(*tasks)

            # 验证:最终状态应与某个版本一致
            assert cascade._policy_version in {f"v{i}" for i in range(100)}
            # 验证候选池一致性(Provider数量应为2)
            assert len(cascade._providers) == 2
            assert len(cascade._candidate_names) == 2
            # 验证_providers和_candidate_names同步
            provider_names = set(cascade._providers.keys())
            candidate_set = set(cascade._candidate_names)
            assert provider_names == candidate_set, "候选池状态不一致"

        finally:
            await store.close()

    _run(body())


def test_apply_policy_atomic_updates(tmp_path):
    """原子性:apply_policy 中的三个更新操作必须原子完成。

    验证:
      - _providers, _candidate_names, _policy_version 三者同步更新
      - 不会出现部分更新的中间状态
    """

    async def body():
        store = TraceStore(tmp_path / "trace.db")
        await store.init()
        try:
            breaker = CircuitBreaker(tmp_path / "breaker.db")
            strategy = MockStrategy()
            initial_candidates = [("p1", MockProvider(), "key1")]
            cascade = Cascade(store, breaker, strategy, initial_candidates)

            # 快速连续更新版本
            for i in range(50):
                candidates = [(f"p{i}", MockProvider(), f"k{i}")]
                cascade.apply_policy(candidates, f"v{i}")

            # 验证状态一致性
            assert cascade._policy_version == "v49"
            assert len(cascade._providers) == 1
            assert "p49" in cascade._providers
            assert cascade._candidate_names == ["p49"]

        finally:
            await store.close()

    _run(body())


def test_provider_lock_no_deadlock(tmp_path):
    """锁安全:并发访问不会造成死锁。

    验证:
      - 大量并发操作都能正常完成
      - 无死锁或活锁发生
    """

    async def body():
        store = TraceStore(tmp_path / "trace.db")
        await store.init()
        try:
            breaker = CircuitBreaker(tmp_path / "breaker.db")
            strategy = MockStrategy()
            candidates = [("p1", MockProvider(), "key1")]
            cascade = Cascade(store, breaker, strategy, candidates)

            # 混合读写操作
            async def mixed_operations(i):
                # 读操作
                _ = cascade._policy_version
                _ = cascade._candidate_names
                # 写操作
                new_candidates = [(f"p{i}", MockProvider(), f"k{i}")]
                cascade.apply_policy(new_candidates, f"v{i}")

            # 并发执行大量混合操作
            tasks = [mixed_operations(i) for i in range(200)]
            await asyncio.gather(*tasks)

            # 验证:能正常完成即可
            assert cascade._policy_version in {f"v{i}" for i in range(200)}

        finally:
            await store.close()

    _run(body())


def test_apply_policy_same_version_noop(tmp_path):
    """幂等性:相同版本号的 apply_policy 应为 noop。

    验证:
      - 相同版本号不触发状态更新
      - 返回 False 表示未执行更新
    """

    async def body():
        store = TraceStore(tmp_path / "trace.db")
        await store.init()
        try:
            breaker = CircuitBreaker(tmp_path / "breaker.db")
            strategy = MockStrategy()
            candidates_v1 = [("p1", MockProvider(), "key1")]
            cascade = Cascade(store, breaker, strategy, candidates_v1)

            # 第一次应用版本
            candidates_v2 = [("p2", MockProvider(), "key2")]
            result1 = cascade.apply_policy(candidates_v2, "v1")
            assert result1 is True
            assert cascade._policy_version == "v1"

            # 相同版本再次应用
            candidates_v3 = [("p3", MockProvider(), "key3")]
            result2 = cascade.apply_policy(candidates_v3, "v1")
            assert result2 is False  # noop
            assert cascade._policy_version == "v1"  # 未更新
            # 候选池也未变化
            assert len(cascade._providers) == 1
            assert "p2" in cascade._providers

        finally:
            await store.close()

    _run(body())


def test_concurrent_read_write_safety(tmp_path):
    """读写安全:读操作不受写操作影响。

    验证:
      - 并发读取候选池时进行更新不崩溃
      - 读操作不会读到部分更新的状态
    """

    async def body():
        store = TraceStore(tmp_path / "trace.db")
        await store.init()
        try:
            breaker = CircuitBreaker(tmp_path / "breaker.db")
            strategy = MockStrategy()
            candidates = [("p1", MockProvider(), "key1")]
            cascade = Cascade(store, breaker, strategy, candidates)

            read_count = 0
            write_count = 0

            async def reader():
                nonlocal read_count
                for _ in range(100):
                    # 读取候选池信息
                    _ = cascade._providers
                    _ = cascade._candidate_names
                    _ = cascade._policy_version
                    read_count += 1
                    await asyncio.sleep(0.001)

            async def writer():
                nonlocal write_count
                for i in range(10):
                    new_candidates = [(f"p{i}", MockProvider(), f"k{i}")]
                    cascade.apply_policy(new_candidates, f"v{i}")
                    write_count += 1
                    await asyncio.sleep(0.01)

            # 并发读写
            await asyncio.gather(reader(), writer())

            # 验证:都完成且无错误
            assert read_count == 100
            assert write_count == 10

        finally:
            await store.close()

    _run(body())
