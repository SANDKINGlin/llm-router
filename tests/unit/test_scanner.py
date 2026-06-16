"""S2.3 · Scanner 静态清单 + 真 provider 接入候选池(⑧ Scanner + ⑤ 适配 + ④ Cascade 接线)。

验收(见 specs/free-model-scanner):Scanner 读 mnfst 静态清单 → ProviderEntry 配置;
真 provider(配了 api_key_env 且环境有该 key)→ 建 OpenAIProvider → 入 Cascade 候选池。
缺 key 的 provider 跳过(不崩,可渐进加 key);mock 始终在池(test_health 守绿)。
"""
from __future__ import annotations

import httpx
import pytest
import respx

from llm_router.api.cascade import Cascade
from llm_router.api.epsilon_greedy import EpsilonGreedy
from llm_router.config import ProviderEntry
from llm_router.providers.mock import MockProvider
from llm_router.providers.openai import OpenAIProvider
from llm_router.resilience.circuit_breaker import CircuitBreaker
from llm_router.scanner.mnfst import build_adapters, load_manifest
from llm_router.store.trace import TraceStore

_VALID_MNFST = """\
providers:
  - name: groq
    tier: fast
    quota: 1000000
    cooldown_s: 30
    is_free: true
    cost_multiplier: 0.0
    model: llama-3.3-70b-versatile
    base_url: https://api.groq.com/openai/v1
    api_key_env: GROQ_API_KEY
  - name: nvidia
    tier: strong
    quota: 500000
    cooldown_s: 30
    is_free: true
    cost_multiplier: 0.0
    model: nvidia/llama-3.1-nemotron-70b-instruct
    base_url: https://integrate.api.nvidia.com/v1
    api_key_env: NVIDIA_API_KEY
"""


# ── load_manifest(⑧ Scanner:清单 → ProviderEntry)───────────────────────────


class TestLoadManifest:
    def test_valid_manifest_parses_to_entries(self, tmp_path):
        p = tmp_path / "providers.yaml"
        p.write_text(_VALID_MNFST)
        entries = load_manifest(p)
        assert len(entries) == 2
        groq = next(e for e in entries if e.name == "groq")
        assert isinstance(groq, ProviderEntry)
        assert groq.tier == "fast"
        assert groq.base_url == "https://api.groq.com/openai/v1"
        assert groq.api_key_env == "GROQ_API_KEY"
        assert groq.model == "llama-3.3-70b-versatile"  # S2.3: D4 解锁的 model 字段
        assert groq.is_free is True

    def test_missing_manifest_returns_empty(self, tmp_path):
        """清单缺失 → [](Phase1 无真 provider 时不崩,降级 mock-only)。"""
        assert load_manifest(tmp_path / "nope.yaml") == []

    def test_malformed_manifest_raises(self, tmp_path):
        """畸形(tier 非枚举)→ ValueError(fail-fast,不静默吞坏配置)。"""
        p = tmp_path / "providers.yaml"
        p.write_text(_VALID_MNFST.replace("tier: strong", "tier: ultra"))
        with pytest.raises(ValueError):
            load_manifest(p)


# ── build_adapters(清单 + env key → OpenAIProvider 候选)────────────────────


class TestBuildAdapters:
    def _entry(self, name="groq", api_key_env="GROQ_API_KEY", base_url="https://t.invalid/v1", model="m1"):
        return ProviderEntry(
            name=name, tier="fast", quota=1, cooldown_s=1,
            is_free=True, cost_multiplier=0.0, model=model,
            base_url=base_url, api_key_env=api_key_env,
        )

    def test_key_present_builds_openai_adapter(self):
        """api_key_env 在 env → 建 OpenAIProvider(注入 env 确定性)。"""
        entries = [self._entry()]
        cands = build_adapters(entries, env={"GROQ_API_KEY": "sk-fake"})
        assert len(cands) == 1
        name, provider, key = cands[0]
        assert name == "groq"
        assert isinstance(provider, OpenAIProvider)
        assert provider.model == "m1"
        assert key == "GROQ_API_KEY"  # breaker 按稳定的 env 名记账,非 secret 本身

    def test_key_absent_skipped(self):
        """api_key_env 不在 env → 跳过(不崩,可渐进配 key)。"""
        entries = [self._entry(api_key_env="MISSING_KEY")]
        assert build_adapters(entries, env={}) == []

    def test_no_api_key_env_skipped(self):
        """entry 无 api_key_env(mock/无 key)→ 跳过(由 app.py 单独加 MockProvider)。"""
        entries = [self._entry(api_key_env=None)]
        assert build_adapters(entries, env={}) == []

    def test_mixed_env_builds_only_configured(self):
        entries = [self._entry("groq", "GROQ_API_KEY"), self._entry("nvidia", "NVIDIA_API_KEY")]
        cands = build_adapters(entries, env={"GROQ_API_KEY": "k1"})  # 只有 groq 配了
        assert len(cands) == 1
        assert cands[0][0] == "groq"


# ── 端到端:Cascade 经真 OpenAIProvider(respx)调到免费 provider ──────────────


@respx.mock
def test_cascade_routes_to_real_adapter(tmp_path):
    """真集成:manifest 的 provider 经 build_adapters 入 Cascade,429 自动回退到 mock。

    验"接进候选池"链路通:真 adapter 被构造 → Cascade 调它 → 429 → 回退。
    respx 模拟传输层,零 key 零成本(conftest 已清代理 env)。
    """
    base = "https://test.real.invalid/v1"
    entries = [ProviderEntry(
        name="realprov", tier="fast", quota=1, cooldown_s=1,
        is_free=True, cost_multiplier=0.0, model="rm",
        base_url=base, api_key_env="REALPROV_KEY",
    )]
    cands = build_adapters(entries, env={"REALPROV_API_KEY_PLACEHOLDER": "x", "REALPROV_KEY": "sk-real"})
    assert len(cands) == 1

    # 真 provider 第一次 429(HARD)→ Cascade 回退。固定序把 realprov 放首跳。
    respx.post(f"{base}/chat/completions").mock(
        return_value=httpx.Response(429, json={"error": {"message": "rl", "type": "rate_limit_error"}})
    )

    # Cascade:候选只有 realprov;429 → record HARD → 链耗尽 → success=False。
    store = TraceStore(tmp_path / "trace.db")
    breaker = CircuitBreaker(tmp_path / "circuit.db")
    entries_map = {e.name: e for e in entries}
    strategy = EpsilonGreedy(entries_map, chooser=lambda: 1.0)  # 纯利用
    cascade = Cascade(store, breaker, strategy, cands, budget=6)
    result = await_sync(cascade.run("hi", correlation_id="c1"))
    assert result.success is False
    assert result.last_reason == "hard_failure"  # 429→ProviderError→HARD,链耗尽


def await_sync(coro):
    import asyncio
    return asyncio.run(coro)


@respx.mock
def test_real_provider_preferred_over_mock_when_key_set(tmp_path):
    """B1 回归守卫:配了 key 的真 provider 必须排在 mock 前,被优先调用(修 mock 支配 bug)。

    模拟 app.py _build_cascade 产物:候选 = [真 adapter 在前, mock 最后兜底]。
    真 provider 返 "real-ok";mock 返 "[mock] canned"。纯利用模式链首必须是真 provider。
    若哪天候选顺序退化成 mock 在前,此测试会红(mock 答了 → final_text 含 [mock])。
    """
    base = "https://test.pref.invalid/v1"
    entries = [
        ProviderEntry(name="mock", tier="fast", quota=1, cooldown_s=1, is_free=True, cost_multiplier=0.0),
        ProviderEntry(name="realprov", tier="fast", quota=1, cooldown_s=1, is_free=True, cost_multiplier=0.0,
                      model="rm", base_url=base, api_key_env="REALPROV_KEY"),
    ]
    real = build_adapters(entries, env={"REALPROV_KEY": "sk-real"})
    assert len(real) == 1
    # 同 app.py(修 B1 后):真 provider 在前,mock 最后兜底。
    candidates = [*real, ("mock", MockProvider(), "mock")]

    respx.post(f"{base}/chat/completions").mock(return_value=httpx.Response(200, json={
        "id": "x", "object": "chat.completion", "created": 1, "model": "realprov-m",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "real-ok"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }))

    store = TraceStore(tmp_path / "trace.db")
    breaker = CircuitBreaker(tmp_path / "circuit.db")
    entries_map = {e.name: e for e in entries}
    strategy = EpsilonGreedy(entries_map, chooser=lambda: 1.0)  # 纯利用 → 必取链首
    cascade = Cascade(store, breaker, strategy, candidates, budget=6)
    result = await_sync(cascade.run("hi", correlation_id="c1"))
    assert result.success is True
    assert result.final_text == "real-ok", "真 provider 必须优先被调(非 mock)"
    assert "[mock]" not in (result.final_text or ""), "mock 不该在真 provider 可用时应答(B1)"


def test_app_build_cascade_orders_real_before_mock(monkeypatch, tmp_path):
    """B1 结构守卫:app._build_cascade 产的候选序必须是 [真..., mock](真在前)。

    不发 HTTP,只验候选顺序。注入临时 manifest + 临时 key,确认真 adapter 在 mock 前。
    """
    import llm_router.app as app_mod

    # 临时 manifest 指向 tmp_path,临时设 key。
    manifest = tmp_path / "providers.yaml"
    manifest.write_text(
        "providers:\n"
        "  - name: realprov\n"
        "    tier: fast\n    quota: 1\n    cooldown_s: 1\n    is_free: true\n    cost_multiplier: 0.0\n"
        "    model: rm\n    base_url: https://test.order.invalid/v1\n    api_key_env: REALPROV_KEY\n"
    )
    monkeypatch.setattr("llm_router.scanner.mnfst._DEFAULT_MANIFEST", manifest)
    monkeypatch.setenv("REALPROV_KEY", "sk-real")
    # policy 的 mock 仍来自 router-policy.yaml(无 key,MockProvider)。

    cascade = app_mod._build_cascade()
    names = [name for name, _prov, _key in cascade._providers.keys()] if False else None
    # _candidate_names 是 plan 输入序;_providers 是 dict(无序),改看 _candidate_names。
    assert cascade._candidate_names[0] == "realprov", "真 provider 必须在候选序首位(修 B1)"
    assert cascade._candidate_names[-1] == "mock", "mock 必须在最后兜底(修 B1)"

