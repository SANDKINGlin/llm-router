"""Phase B · B5.1 端到端集成:动态候选池真切流量闭环。

完整流程(fake fetcher + fake probe + respx 模拟 provider 传输层):
  poll → diff → interview(入池)→ on_tick_complete 重建 → 候选池含动态 →
  路由请求命中动态条目 → 动态失败(500)→ fallback 静态 provider 成功。

设计(守 routing-priority-principle 字典序):
  - 静态 provider = 付费(is_free=False, cost=0.5)→ 排序键劣于动态免费
  - 动态条目 = 免费(is_free=True, cost=0)→ 排序键首跳命中
  - 动态 NVIDIA 端点 respx 500 → ProviderError HARD → Cascade fallback
  - 静态端点 respx 200 → 成功
  故 plan() 链首 = 动态(免费优先),失败后 fallback 静态(付费兜底)。

hermetic:tmp scanner.db/manifest/policy(respx 模拟传输,零真网络零 key 成本)。
"""
from __future__ import annotations

import asyncio

import httpx
import pytest
import respx

from llm_router.config import Policy, ProviderEntry
from llm_router.scanner.dynamic import DynamicScanner
from llm_router.scanner.snapshot import DiscoveredModel, ScannerSource
from llm_router.store.scanner_store import ScannerStore


def _run(coro):
    return asyncio.run(coro)


STATIC_BASE = "https://test.static.invalid/v1"
NVIDIA_BASE = "https://integrate.api.nvidia.com/v1"


def _fake_fetcher(nv_models):
    """fake poller fetcher:返回指定 NVIDIA /models payload(OpenRouter 空,缺 key 降级)。"""
    payload = {"data": [{"id": m.model_id, "name": m.model_id} for m in nv_models]}

    async def fetch(url, headers, timeout):
        if "nvidia" in url:
            return payload
        return {"data": []}
    return fetch


def _passing_probe_factory():
    """fake probe_factory:每个 model 返合格 probe(零网络,模拟面试通过)。"""
    def factory(model):
        async def probe(model_id):
            return "smoke-ok"
        return probe
    return factory


@pytest.fixture
def phase_b_env(monkeypatch, tmp_path):
    """hermetic Phase B 环境:tmp 数据目录(trace/circuit/health/ledger/scanner)+ tmp manifest
    (静态付费 provider)+ key + policy。

    必须把 _DATA_DIR 指到 tmp(否则 _build_cascade 用 production data/trace.db →
    跨测试 correlation_id replay 污染 + 写生产库)。
    """
    monkeypatch.setattr("llm_router.app._DATA_DIR", tmp_path)
    scanner_db = tmp_path / "scanner.db"
    monkeypatch.setattr("llm_router.app._SCANNER_DB", scanner_db)

    manifest = tmp_path / "providers.yaml"
    manifest.write_text(
        "providers:\n"
        "  - name: staticreal\n"
        "    tier: strong\n    quota: 1000000\n    cooldown_s: 1\n"
        "    is_free: false\n    cost_multiplier: 0.5\n"
        "    model: sm\n    base_url: " + STATIC_BASE + "\n"
        "    api_key_env: STATICREAL_KEY\n"
    )
    monkeypatch.setattr("llm_router.scanner.mnfst._DEFAULT_MANIFEST", manifest)
    monkeypatch.setenv("STATICREAL_KEY", "sk-static")
    monkeypatch.setenv("NVIDIA_API_KEY", "sk-nv")
    # policy 无 mock(纯净测动态→静态 fallback;mock 兜底由他处覆盖),gray=100 启用动态池
    monkeypatch.setattr(
        "llm_router.app.policy",
        lambda: Policy(policy_version="e2e-v1", gray_percent=100, providers=[]),
    )
    return scanner_db


@respx.mock
def test_phase_b_e2e_dynamic_then_fallback_static(phase_b_env, tmp_path):
    """完整闭环:tick 入池 → 重建 → 动态首跳失败 → fallback 静态成功。"""
    import llm_router.app as app_mod

    # ① 建生产 cascade(初始 scanner.db 空 → [staticreal])
    cascade = app_mod._build_cascade()
    assert "staticreal" in cascade._candidate_names
    assert not any(n.startswith("dyn-") for n in cascade._candidate_names)

    # ② DynamicScanner:fake fetcher(NVIDIA 1 个 strong 模型)+ fake probe + 重建回调
    store = ScannerStore(phase_b_env)
    _run(store.init())
    try:
        ds = DynamicScanner(
            store,
            probe_factory=_passing_probe_factory(),
            fetcher=_fake_fetcher([DiscoveredModel(
                source=ScannerSource.NVIDIA,
                model_id="nvidia/llama-3.1-nemotron-70b-instruct",
                tier="strong",
            )]),
            nvidia_key="sk-nv",
            openrouter_key="",  # OpenRouter 缺 key → 降级空
            on_tick_complete=app_mod._make_rebuild_callback(cascade, store),
        )
        # ③ tick:poll → diff(added)→ interview(入池 active)→ on_tick_complete 重建
        result = _run(ds.tick())
        assert result.ok is True
        assert result.stats[ScannerSource.NVIDIA].passed == 1

        # ④ 重建后候选池含动态(三层:staticreal → dyn → 无 mock)
        dyn_names = [n for n in cascade._candidate_names if n.startswith("dyn-")]
        assert len(dyn_names) == 1
        assert "staticreal" in cascade._candidate_names

        # ⑤ respx:动态 NVIDIA 500(失败),静态 200(成功)
        respx.post(f"{NVIDIA_BASE}/chat/completions").mock(
            return_value=httpx.Response(500, json={"error": {"message": "down", "type": "server_error"}})
        )
        respx.post(f"{STATIC_BASE}/chat/completions").mock(
            return_value=httpx.Response(200, json={
                "id": "x", "object": "chat.completion", "created": 1, "model": "sm",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "static-ok"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            })
        )

        # ⑥ 路由:动态(免费)排序首跳 → 500 HARD → fallback 静态(付费)→ 200 成功
        #    纯利用(chooser=1.0)→ 链首 = 动态(免费优先于付费静态)
        cascade._strategy._chooser = lambda: 1.0
        result = _run(cascade.run("hi", correlation_id="e2e-c1"))
        assert result.success is True
        assert result.final_text == "static-ok", "动态失败后应 fallback 到静态 provider"
        assert result.hops_attempted >= 2  # 动态试过(失败) + 静态成功
    finally:
        _run(store.close())
