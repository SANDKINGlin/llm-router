"""S2.10-0.2 · Pollers 测试(注入 fake fetcher,零网络)+ tier_infer 单测。

守 spec「动态 diff 抓新免费模型」抓取层契约:
- NVIDIA /v1/models OpenAI 形状解析,全 is_free=True(NIM 免费档)
- OpenRouter /models 仅留免费(`:free` 后缀 或 pricing 全 0)
- key 缺失 → 空 Snapshot 降级(不抛,后台循环健壮)
- 网络/HTTP 异常 → 空 Snapshot 降级
- tier 关键词推断(strong/medium/fast,多命中取最强,无命中 medium)
- 真 API 调用 gated(SCANNER_LIVE=1 + 真 key,默认 skip)
"""
from __future__ import annotations

import asyncio
import os

import pytest

from llm_router.scanner.pollers import (
    _is_openrouter_free,
    _parse_nvidia,
    _parse_openrouter,
    poll_all,
    poll_nvidia,
    poll_openrouter,
)
from llm_router.scanner.snapshot import DiscoveredModel, ScannerSource
from llm_router.scanner.tier_infer import infer_tier, label_tier


def _run(coro):
    """项目 async 测试模式(同 test_openai_provider.py):sync 测试包 asyncio.run。"""
    return asyncio.run(coro)


# ── fake fetcher 工厂 ──────────────────────────────────────────────

def _fake_fetch_returning(payload):
    """造一个返回固定 payload 的 async fetcher。"""
    async def _fetch(url, headers, timeout):
        return payload
    return _fetch


def _fake_fetch_raising(exc):
    async def _fetch(url, headers, timeout):
        raise exc
    return _fetch


# ── tier_infer ────────────────────────────────────────────────────

class TestInferTier:
    def test_70b_is_strong(self):
        assert infer_tier("nvidia/llama-3.1-nemotron-70b-instruct") == "strong"

    def test_nemotron_is_strong(self):
        assert infer_tier("meta/llama-3.1-8b-instruct") != "strong"  # 8b → fast
        assert infer_tier("nvidia/nemotron-4-340b") == "strong"

    def test_mini_is_fast(self):
        assert infer_tier("openai/gpt-4o-mini") == "fast"

    def test_flash_is_fast(self):
        assert infer_tier("google/gemini-1.5-flash") == "fast"

    def test_unknown_defaults_medium(self):
        assert infer_tier("some-vendor/mystery-model") == "medium"

    def test_multiple_matches_take_strongest(self):
        # "70b"(strong) + "mini"(fast) 同时命中 → strong(最强档)
        assert infer_tier("vendor/70b-mini-model") == "strong"

    def test_case_insensitive(self):
        assert infer_tier("Vendor/70B-MODEL") == "strong"

    def test_gpt_oss_120b_strong(self):
        assert infer_tier("openai/gpt-oss-120b:free") == "strong"

    def test_gpt_oss_20b_fast(self):
        assert infer_tier("openai/gpt-oss-20b:free") == "fast"

    def test_label_tier_keeps_existing(self):
        m = DiscoveredModel(source=ScannerSource.NVIDIA, model_id="foo", tier="fast")
        assert label_tier(m).tier == "fast"  # 已有 tier 不覆盖

    def test_label_tier_infers_when_none(self):
        m = DiscoveredModel(source=ScannerSource.NVIDIA, model_id="foo-70b", tier=None)
        assert label_tier(m).tier == "strong"

    def test_label_tier_returns_new_instance(self):
        m = DiscoveredModel(source=ScannerSource.NVIDIA, model_id="foo-70b", tier=None)
        labeled = label_tier(m)
        assert labeled is not m  # immutable,新对象
        assert m.tier is None  # 原对象不动


# ── NVIDIA parser ─────────────────────────────────────────────────

class TestParseNvidia:
    def test_parses_openai_shape(self):
        data = {
            "data": [
                {"id": "nvidia/llama-3.1-nemotron-70b-instruct", "name": "Nemotron 70B"},
                {"id": "meta/llama-3.1-8b-instruct", "name": "Llama 8B"},
            ]
        }
        models = _parse_nvidia(data)
        assert {m.model_id for m in models} == {
            "nvidia/llama-3.1-nemotron-70b-instruct",
            "meta/llama-3.1-8b-instruct",
        }
        # 全 is_free=True(NIM 免费档)
        assert all(m.is_free for m in models)
        assert all(m.source is ScannerSource.NVIDIA for m in models)
        # tier 贴标
        by_id = {m.model_id: m for m in models}
        assert by_id["nvidia/llama-3.1-nemotron-70b-instruct"].tier == "strong"
        assert by_id["meta/llama-3.1-8b-instruct"].tier == "fast"

    def test_display_name_falls_back_to_id(self):
        data = {"data": [{"id": "nvidia/foo"}]}
        models = _parse_nvidia(data)
        assert models[0].name == "nvidia/foo"

    def test_skips_rows_without_id(self):
        data = {"data": [{"id": ""}, {"name": "no-id"}, {"id": "valid/id"}]}
        models = _parse_nvidia(data)
        assert {m.model_id for m in models} == {"valid/id"}

    def test_malformed_no_data_key_returns_empty(self):
        assert _parse_nvidia({"foo": "bar"}) == []
        assert _parse_nvidia({}) == []

    def test_data_not_list_returns_empty(self):
        assert _parse_nvidia({"data": "not-a-list"}) == []

    def test_skips_non_dict_rows(self):
        data = {"data": ["string", 42, None, {"id": "valid/id"}]}
        models = _parse_nvidia(data)
        assert {m.model_id for m in models} == {"valid/id"}


# ── OpenRouter free filter ────────────────────────────────────────

class TestIsOpenrouterFree:
    def test_free_suffix_is_free(self):
        assert _is_openrouter_free({"id": "openai/gpt-oss-120b:free"}, "openai/gpt-oss-120b:free")

    def test_pricing_zero_is_free(self):
        row = {"id": "x", "pricing": {"prompt": "0", "completion": "0"}}
        assert _is_openrouter_free(row, "x")

    def test_pricing_nonzero_not_free(self):
        row = {"id": "x", "pricing": {"prompt": "0.001", "completion": "0.002"}}
        assert not _is_openrouter_free(row, "x")

    def test_no_pricing_not_free(self):
        assert not _is_openrouter_free({"id": "x"}, "x")

    def test_partial_pricing_zero_not_free(self):
        row = {"id": "x", "pricing": {"prompt": "0", "completion": "0.001"}}
        assert not _is_openrouter_free(row, "x")


class TestParseOpenrouter:
    def test_keeps_only_free_models(self):
        data = {
            "data": [
                {"id": "openai/gpt-oss-120b:free", "name": "GPT-OSS 120B Free"},
                {"id": "anthropic/claude-3.5-sonnet", "name": "Claude", "pricing": {"prompt": "0.003", "completion": "0.015"}},
                {"id": "vendor/free-zero", "name": "Zero", "pricing": {"prompt": "0", "completion": "0"}},
                {"id": "vendor/paid", "name": "Paid", "pricing": {"prompt": "0.001", "completion": "0"}},
            ]
        }
        models = _parse_openrouter(data)
        assert {m.model_id for m in models} == {"openai/gpt-oss-120b:free", "vendor/free-zero"}
        assert all(m.is_free for m in models)
        assert all(m.source is ScannerSource.OPENROUTER for m in models)

    def test_tier_labels_applied(self):
        data = {"data": [{"id": "openai/gpt-oss-120b:free"}, {"id": "openai/gpt-oss-20b:free"}]}
        models = _parse_openrouter(data)
        by_id = {m.model_id: m for m in models}
        assert by_id["openai/gpt-oss-120b:free"].tier == "strong"
        assert by_id["openai/gpt-oss-20b:free"].tier == "fast"

    def test_malformed_returns_empty(self):
        assert _parse_openrouter({"foo": "bar"}) == []
        assert _parse_openrouter({}) == []
        assert _parse_openrouter({"data": "no"}) == []

    def test_skips_rows_without_id(self):
        data = {"data": [{"id": ""}, {"name": "x"}, {"id": "ok:free"}]}
        models = _parse_openrouter(data)
        assert {m.model_id for m in models} == {"ok:free"}


# ── poll_nvidia end-to-end (fake fetcher) ─────────────────────────

class TestPollNvidia:
    def test_returns_snapshot_with_models(self):
        payload = {"data": [{"id": "nvidia/llama-3.1-nemotron-70b-instruct", "name": "Nemotron"}]}
        snap = _run(poll_nvidia(api_key="k", fetcher=_fake_fetch_returning(payload)))
        assert snap.source is ScannerSource.NVIDIA
        assert {m.model_id for m in snap.models} == {"nvidia/llama-3.1-nemotron-70b-instruct"}

    def test_missing_key_returns_empty(self):
        snap = _run(poll_nvidia(api_key="", fetcher=_fake_fetch_returning({"data": [{"id": "x"}]})))
        assert len(snap) == 0  # key 缺失不调 fetcher,直接降级空

    def test_fetch_error_returns_empty(self):
        snap = _run(poll_nvidia(api_key="k", fetcher=_fake_fetch_raising(RuntimeError("net"))))
        assert len(snap) == 0  # 网络错降级空,不抛

    def test_malformed_payload_returns_empty(self):
        snap = _run(poll_nvidia(api_key="k", fetcher=_fake_fetch_returning({"nope": 1})))
        assert len(snap) == 0


# ── poll_openrouter end-to-end (fake fetcher) ─────────────────────

class TestPollOpenrouter:
    def test_returns_only_free(self):
        payload = {
            "data": [
                {"id": "openai/gpt-oss-120b:free"},
                {"id": "paid/model", "pricing": {"prompt": "0.001", "completion": "0"}},
            ]
        }
        snap = _run(poll_openrouter(api_key="k", fetcher=_fake_fetch_returning(payload)))
        assert snap.source is ScannerSource.OPENROUTER
        assert {m.model_id for m in snap.models} == {"openai/gpt-oss-120b:free"}

    def test_missing_key_returns_empty(self):
        snap = _run(poll_openrouter(api_key="", fetcher=_fake_fetch_returning({"data": [{"id": "x:free"}]})))
        assert len(snap) == 0

    def test_fetch_error_returns_empty(self):
        snap = _run(poll_openrouter(api_key="k", fetcher=_fake_fetch_raising(ConnectionError("net"))))
        assert len(snap) == 0


# ── poll_all concurrency ──────────────────────────────────────────

class TestPollAll:
    def test_polls_both_sources(self):
        async def fetch(url, headers, timeout):
            if "nvidia" in url:
                return {"data": [{"id": "nvidia/foo-70b"}]}
            return {"data": [{"id": "or/bar:free"}]}
        out = _run(poll_all(fetcher=fetch, nvidia_key="k", openrouter_key="k"))
        assert set(out) == {ScannerSource.NVIDIA, ScannerSource.OPENROUTER}
        assert {m.model_id for m in out[ScannerSource.NVIDIA].models} == {"nvidia/foo-70b"}
        assert {m.model_id for m in out[ScannerSource.OPENROUTER].models} == {"or/bar:free"}

    def test_both_missing_keys_returns_empties(self):
        out = _run(poll_all(fetcher=_fake_fetch_returning({}), nvidia_key="", openrouter_key=""))
        assert len(out[ScannerSource.NVIDIA]) == 0
        assert len(out[ScannerSource.OPENROUTER]) == 0


# ── gated real network ────────────────────────────────────────────
# conftest 模块级清掉 NVIDIA_API_KEY(hermetic 基线,防真 key 泄漏进 import 期 _build_cascade),
# 故 gated 真测试不读 NVIDIA_API_KEY(已被清),改读 conftest 不清的 SCANNER_LIVE_KEY 作 gate +
# 运行时 monkeypatch 注回 env 给 poll_nvidia。这样既守 hermetic 基线又能跑真测。

@pytest.mark.skipif(
    not (os.environ.get("SCANNER_LIVE") and os.environ.get("SCANNER_LIVE_KEY")),
    reason="需 SCANNER_LIVE=1 + SCANNER_LIVE_KEY=<nvidia key>(真 API,默认 skip)",
)
class TestPollNvidiaLive:
    def test_real_nvidia_models_endpoint(self, monkeypatch):
        # conftest 已清 NVIDIA_API_KEY(hermetic);运行时注回 SCANNER_LIVE_KEY 的值给 poll_nvidia。
        key = os.environ["SCANNER_LIVE_KEY"]
        monkeypatch.setenv("NVIDIA_API_KEY", key)
        snap = _run(poll_nvidia())  # 真 httpx + 真 key
        # 不断言具体模型(随时间变),只断言抓到了非空且形状对
        assert snap.source is ScannerSource.NVIDIA
        assert len(snap) > 0
        assert all(m.is_free for m in snap.models)
