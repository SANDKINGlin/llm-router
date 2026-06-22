"""Phase B · 动态候选池热入 production Cascade(B2.1/B2.2/B4.1)。

守 spec dynamic-pool-injection / llm-routing-core / free-model-scanner:
- 三层候选池 静态真 → 动态 → mock(插入序,plan() 稳定排序保持)
- gray_percent=0 或 scanner.db 空 → 无动态(向后兼容两层)
- 红线:动态 entries is_free=True/cost=0;_rank 查得到;静态免费平局时静态在前(插入序)
- 缺 key source 的动态条目不产候选(build_dynamic_adapters 跳过)

测试 hermetic:monkeypatch _SCANNER_DB 到 tmp scanner.db(ScannerStore async 写 active 条目),
monkeypatch manifest 到 tmp(静态真 provider),monkeypatch policy gray_percent。
不碰 production data/(发 HTTP,纯结构断言)。
"""
from __future__ import annotations

import asyncio

import pytest

from llm_router.config import Policy, ProviderEntry
from llm_router.store.scanner_store import ScannerStore


def _run(coro):
    return asyncio.run(coro)


def _seed_scanner_db(db_path, models):
    """用 ScannerStore async 写 active 动态条目(供 _build_cascade 同步读)。"""
    async def body():
        store = ScannerStore(db_path)
        await store.init()
        try:
            for m in models:
                await store.upsert_entry(m, interview_passed=True)
        finally:
            await store.close()
    _run(body())


def _make_policy(gray_percent: int = 100) -> Policy:
    """policy 只含 mock(无 key,MockProvider 兜底);gray_percent 可调。"""
    return Policy(
        policy_version="test-v1",
        gray_percent=gray_percent,
        providers=[
            ProviderEntry(
                name="mock", tier="fast", quota=1000000, cooldown_s=1,
                is_free=True, cost_multiplier=0.0,
            )
        ],
    )


def _tmp_manifest(tmp_path) -> object:
    """临时 manifest:一个静态真 provider(配 key,免费,与动态同档竞争)。"""
    manifest = tmp_path / "providers.yaml"
    manifest.write_text(
        "providers:\n"
        "  - name: staticreal\n"
        "    tier: strong\n    quota: 1000000\n    cooldown_s: 1\n"
        "    is_free: true\n    cost_multiplier: 0.0\n"
        "    model: sm\n    base_url: https://test.static.invalid/v1\n"
        "    api_key_env: STATICREAL_KEY\n"
    )
    return manifest


from llm_router.scanner.snapshot import DiscoveredModel, ScannerSource


def _nv(mid, tier="strong"):
    return DiscoveredModel(source=ScannerSource.NVIDIA, model_id=mid, tier=tier)


@pytest.fixture
def app_env(monkeypatch, tmp_path):
    """hermetic 环境:tmp scanner.db + tmp manifest + STATICREAL/NVIDIA key + policy 注入。"""
    scanner_db = tmp_path / "scanner.db"
    monkeypatch.setattr("llm_router.app._SCANNER_DB", scanner_db)
    monkeypatch.setattr("llm_router.scanner.mnfst._DEFAULT_MANIFEST", _tmp_manifest(tmp_path))
    monkeypatch.setenv("STATICREAL_KEY", "sk-static")
    monkeypatch.setenv("NVIDIA_API_KEY", "sk-nv")
    return scanner_db


# ── B2.1 三层候选池构造 ──────────────────────────────────────────────

class TestThreeLayerCascade:
    def test_three_layer_order_static_dynamic_mock(self, app_env, monkeypatch):
        """scanner.db 有 active 动态条目 + gray=100 → candidates = [static, dynamic, mock]。"""
        import llm_router.app as app_mod
        _seed_scanner_db(app_env, [_nv("nvidia/llama-70b")])
        monkeypatch.setattr(app_mod, "policy", lambda: _make_policy(gray_percent=100))

        cascade = app_mod._build_cascade()
        names = cascade._candidate_names
        # 静态真(staticreal)→ 动态(dyn-nvidia-...)→ mock
        assert names[0] == "staticreal"
        assert names[1].startswith("dyn-nvidia-")
        assert names[-1] == "mock"
        # 动态夹在静态与 mock 之间
        dyn_idx = next(i for i, n in enumerate(names) if n.startswith("dyn-"))
        assert names.index("staticreal") < dyn_idx < names.index("mock")

    def test_no_dynamic_when_scanner_db_empty(self, app_env, monkeypatch):
        """scanner.db 空 → 无动态,退化两层 [static, mock](向后兼容)。"""
        import llm_router.app as app_mod
        monkeypatch.setattr(app_mod, "policy", lambda: _make_policy(gray_percent=100))

        cascade = app_mod._build_cascade()
        names = cascade._candidate_names
        assert names == ["staticreal", "mock"]
        assert not any(n.startswith("dyn-") for n in names)

    def test_no_dynamic_when_scanner_db_missing(self, app_env, monkeypatch):
        """scanner.db 文件不存在 → fail-open 无动态(不崩 import)。"""
        import llm_router.app as app_mod
        # 不 seed → scanner.db 不存在
        monkeypatch.setattr(app_mod, "policy", lambda: _make_policy(gray_percent=100))

        cascade = app_mod._build_cascade()
        assert cascade._candidate_names == ["staticreal", "mock"]

    def test_dynamic_entries_in_strategy(self, app_env, monkeypatch):
        """动态 entries 进 EpsilonGreedy entries dict(供 _rank 按 name 查)。"""
        import llm_router.app as app_mod
        _seed_scanner_db(app_env, [_nv("nvidia/llama-70b", tier="strong")])
        monkeypatch.setattr(app_mod, "policy", lambda: _make_policy(gray_percent=100))

        cascade = app_mod._build_cascade()
        dyn_name = "dyn-nvidia-nvidia:llama-70b"
        assert dyn_name in cascade._strategy._entries
        entry = cascade._strategy._entries[dyn_name]
        assert entry.is_free is True
        assert entry.cost_multiplier == 0.0
        assert entry.tier == "strong"

    def test_missing_key_source_no_dynamic_candidate(self, app_env, monkeypatch):
        """动态条目所属 source 缺 key → 不产候选(build_dynamic_adapters 跳过),不崩。

        仅 OpenRouter 模型,不设 OPENROUTER_API_KEY → candidates 无动态。
        """
        import llm_router.app as app_mod
        or_model = DiscoveredModel(
            source=ScannerSource.OPENROUTER, model_id="openai/gpt-oss:free", tier="strong"
        )
        _seed_scanner_db(app_env, [or_model])
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        monkeypatch.setattr(app_mod, "policy", lambda: _make_policy(gray_percent=100))

        cascade = app_mod._build_cascade()
        assert not any(n.startswith("dyn-") for n in cascade._candidate_names)
        # 静态 + mock 仍正常
        assert "staticreal" in cascade._candidate_names
        assert "mock" in cascade._candidate_names


# ── B2.2 红线守门 ────────────────────────────────────────────────────

class TestRedLineSortKey:
    def test_rank_finds_dynamic_entry(self, app_env, monkeypatch):
        """epsilon_greedy._rank 对动态 name 查得到 entry(不抛 KeyError)。"""
        import llm_router.app as app_mod
        _seed_scanner_db(app_env, [_nv("nvidia/llama-70b", tier="strong")])
        monkeypatch.setattr(app_mod, "policy", lambda: _make_policy(gray_percent=100))

        cascade = app_mod._build_cascade()
        dyn_name = "dyn-nvidia-nvidia:llama-70b"
        # _rank 返回 (not matched, not is_free, cost_multiplier);不抛 = entry 查得到
        key = cascade._strategy._rank(dyn_name, None)
        assert key == (False, False, 0.0)  # matched, is_free=True, cost=0

    def test_static_free_before_dynamic_free_on_tie(self, app_env, monkeypatch):
        """静态免费 vs 动态免费排序键全平局 → plan() 稳定排序保持插入序:静态在前。

        task_type=None → 全对口(首槽 False);两者 is_free=True/cost=0 → 全平局。
        sorted() 稳定 → 保持 candidates 顺序(staticreal 在 dyn 前)。
        chooser=1.0(纯利用)→ plan()[0] = ordered[0] = staticreal。
        """
        import llm_router.app as app_mod
        _seed_scanner_db(app_env, [_nv("nvidia/llama-70b", tier="strong")])
        monkeypatch.setattr(app_mod, "policy", lambda: _make_policy(gray_percent=100))

        cascade = app_mod._build_cascade()
        # 注入纯利用 chooser(>= epsilon 必利用)
        cascade._strategy._chooser = lambda: 1.0
        chain = cascade._strategy.plan(list(cascade._candidate_names), {})
        # 静态真 provider 必须排动态前(平局时插入序)
        assert chain[0] == "staticreal"
        dyn_idx = next(i for i, n in enumerate(chain) if n.startswith("dyn-"))
        assert chain.index("staticreal") < dyn_idx
        # mock 最后(平局 + 插入序)
        assert chain[-1] == "mock"

    def test_dynamic_entries_free_zero_cost(self, app_env, monkeypatch):
        """静态断言:所有动态 entries is_free=True/cost_multiplier=0.0(与静态免费同档)。"""
        import llm_router.app as app_mod
        _seed_scanner_db(app_env, [_nv("nvidia/a-70b", tier="strong"),
                                   _nv("nvidia/b-mini", tier="fast")])
        monkeypatch.setattr(app_mod, "policy", lambda: _make_policy(gray_percent=100))

        cascade = app_mod._build_cascade()
        dyn_entries = {n: e for n, e in cascade._strategy._entries.items()
                       if n.startswith("dyn-")}
        assert len(dyn_entries) == 2
        for e in dyn_entries.values():
            assert e.is_free is True
            assert e.cost_multiplier == 0.0


# ── B4.1 灰度可禁用 ──────────────────────────────────────────────────

class TestGrayDisable:
    def test_gray_zero_no_dynamic(self, app_env, monkeypatch):
        """gray_percent=0 → 不加载动态条目(纯静态+mock),即使 scanner.db 有 active。"""
        import llm_router.app as app_mod
        _seed_scanner_db(app_env, [_nv("nvidia/llama-70b")])
        monkeypatch.setattr(app_mod, "policy", lambda: _make_policy(gray_percent=0))

        cascade = app_mod._build_cascade()
        names = cascade._candidate_names
        assert names == ["staticreal", "mock"]
        assert not any(n.startswith("dyn-") for n in names)
        # entries dict 也无动态(gray=0 完全不构造)
        assert not any(n.startswith("dyn-") for n in cascade._strategy._entries)

    def test_gray_full_has_dynamic(self, app_env, monkeypatch):
        """gray_percent=100 + scanner.db 有 active → 有动态条目。"""
        import llm_router.app as app_mod
        _seed_scanner_db(app_env, [_nv("nvidia/llama-70b")])
        monkeypatch.setattr(app_mod, "policy", lambda: _make_policy(gray_percent=100))

        cascade = app_mod._build_cascade()
        assert any(n.startswith("dyn-") for n in cascade._candidate_names)


# ── B4.2 失败回滚:动态全失败 fallback 静态/mock ──────────────────────

class TestDynamicFailFallback:
    def test_dynamic_fail_falls_back_to_mock(self, tmp_path):
        """动态 adapter 全失败(ProviderError HARD)→ Cascade fallback 到 mock,请求仍成功。

        D5:复用现有 fallback 链,不额外加逻辑。验三层熔断 + fallback 对动态条目同样生效。
        已知 breaker 语义(同 test_fallback_e2e):fallback 兄弟 provider 须先 seed CLOSED-known,
        否则 global_open 冻结。生产里 mock 由探活/首次成功 seed;此处显式 seed 模拟。
        """
        from llm_router.api.cascade import Cascade
        from llm_router.api.epsilon_greedy import EpsilonGreedy
        from llm_router.config import ProviderEntry
        from llm_router.providers.base import Provider, ProviderError
        from llm_router.providers.mock import MockProvider
        from llm_router.resilience.circuit_breaker import CircuitBreaker, TripReason
        from llm_router.store.trace import TraceStore

        class _BoomDyn(Provider):
            name = "dyn-nvidia-fail"
            async def complete(self, messages, *, tools=None, tool_choice=None):
                raise ProviderError("dynamic provider down")

        entries = {
            "dyn-nvidia-fail": ProviderEntry(
                name="dyn-nvidia-fail", tier="strong", quota=500000, cooldown_s=30,
                is_free=True, cost_multiplier=0.0,
            ),
            "mock": ProviderEntry(
                name="mock", tier="fast", quota=1000000, cooldown_s=1,
                is_free=True, cost_multiplier=0.0,
            ),
        }
        # 动态在前(排序键平局→插入序),mock 兜底在后。
        candidates = [
            ("dyn-nvidia-fail", _BoomDyn(), "NVIDIA_API_KEY"),
            ("mock", MockProvider(), "mock"),
        ]
        breaker = CircuitBreaker(tmp_path / "circuit.db")  # 默认阈值 3
        # seed mock CLOSED-known(防 dyn 失败后 global_open 冻结 mock,同 e2e 纪律)
        breaker.record_failure("mock", "mock", TripReason.HARD)  # 1 硬失败 < 阈值 → CLOSED
        cascade = Cascade(
            store=TraceStore(tmp_path / "trace.db"),
            breaker=breaker,
            strategy=EpsilonGreedy(entries, chooser=lambda: 1.0),  # 纯利用→链首 dyn
            candidates=candidates,
            budget=6,
        )
        result = _run(cascade.run([{"role":"user","content":"hi"}], correlation_id="c1"))
        assert result.success is True
        assert "[mock]" in (result.final_text or "")  # fallback 到 mock
        assert result.hops_attempted >= 2  # dyn 试过(失败) + mock 成功
        # dyn 失败被记账(HARD;阈值 3 → 仍 CLOSED 但 hard_failures>=1)
        st = cascade._breaker.get_key_state("dyn-nvidia-fail", "NVIDIA_API_KEY")
        assert st.hard_failures >= 1


# ── B4.3 清退回滚:expire 全部 → 重建纯静态 ──────────────────────────

class TestExpireRollback:
    def test_expire_all_rebuild_pure_static(self, tmp_path, monkeypatch):
        """全部动态条目 expire_entry → 重建回调产纯静态候选池(无动态)。

        守 D5 清退回滚:expire 全部 active → active_models 空 → 三层候选池退化为 [static, mock]。
        """
        import llm_router.app as app_mod
        from llm_router.scanner.snapshot import DiscoveredModel, ScannerSource
        from llm_router.store.scanner_store import ScannerStore

        scanner_db = tmp_path / "scanner.db"
        monkeypatch.setattr(app_mod, "_SCANNER_DB", scanner_db)
        monkeypatch.setattr("llm_router.scanner.mnfst._DEFAULT_MANIFEST", _tmp_manifest(tmp_path))
        monkeypatch.setenv("STATICREAL_KEY", "sk-static")
        monkeypatch.setenv("NVIDIA_API_KEY", "sk-nv")
        monkeypatch.setattr(app_mod, "policy", lambda: _make_policy(gray_percent=100))

        # 1. seed 1 active 动态条目 → _build_cascade 含动态
        m = DiscoveredModel(source=ScannerSource.NVIDIA, model_id="nvidia/a-70b", tier="strong")
        _seed_scanner_db(scanner_db, [m])
        cascade = app_mod._build_cascade()
        assert any(n.startswith("dyn-") for n in cascade._candidate_names)

        # 2. expire 全部 → 重建回调 → 候选池纯静态
        async def expire_and_rebuild():
            store = ScannerStore(scanner_db)
            await store.init()
            try:
                # expire 全部 active
                for row in await store.list_entries(status="active"):
                    await store.expire_entry(row.model_id)
                # 触发重建回调(模拟 tick 后)
                rebuild = app_mod._make_rebuild_callback(cascade, store)
                await rebuild(None)
            finally:
                await store.close()
        _run(expire_and_rebuild())

        # 重建后候选池无动态(纯 staticreal + mock)
        assert not any(n.startswith("dyn-") for n in cascade._candidate_names)
        assert "staticreal" in cascade._candidate_names
        assert "mock" in cascade._candidate_names
