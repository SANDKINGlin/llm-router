"""S2.10-0.4 · 自动面试测试(注入 fake probe,零网络)。

守 spec「自动面试合格后纳入候选池」契约:
- 非空内容 → passed=True,tier 贴标(关键词推断)
- 空/异常/超时/非 str → passed=False(reason 记类型,不崩)
- interview_batch 并发,顺序与入参一致,任一失败不影响其他
- passed_models 取合格列表(纯函数)
- 红线:不加载本地 ollama(probe 打远端);结果只决定入池 bool + tier,不进排序键加权
"""
from __future__ import annotations

import asyncio

from llm_router.scanner.interview import (
    InterviewResult,
    interview_batch,
    interview_model,
    passed_models,
)
from llm_router.scanner.snapshot import DiscoveredModel, ScannerSource


def _run(coro):
    return asyncio.run(coro)


def _nv(mid="nvidia/foo-70b", tier=None):
    return DiscoveredModel(source=ScannerSource.NVIDIA, model_id=mid, tier=tier)


def _probe_returning(text):
    async def probe(model_id):
        return text
    return probe


def _probe_raising(exc):
    async def probe(model_id):
        raise exc
    return probe


def _probe_slow(delay, text="ok"):
    async def probe(model_id):
        await asyncio.sleep(delay)
        return text
    return probe


class TestInterviewModel:
    def test_non_empty_passes_with_tier_label(self):
        result = _run(interview_model(_nv("nvidia/foo-70b", tier=None), probe=_probe_returning("hello world")))
        assert result.passed is True
        assert result.reason == "ok"
        assert result.model.tier == "strong"  # 关键词推断贴标
        assert result.response_snippet == "hello world"

    def test_whitespace_only_fails_empty_content(self):
        result = _run(interview_model(_nv(), probe=_probe_returning("   \n  ")))
        assert result.passed is False
        assert result.reason == "empty_content"

    def test_empty_string_fails(self):
        result = _run(interview_model(_nv(), probe=_probe_returning("")))
        assert result.passed is False
        assert result.reason == "empty_content"

    def test_exception_fails_with_error_reason(self):
        result = _run(interview_model(_nv(), probe=_probe_raising(RuntimeError("net down"))))
        assert result.passed is False
        assert result.reason == "error:RuntimeError"

    def test_timeout_fails(self):
        result = _run(interview_model(_nv(), probe=_probe_slow(1.0), probe_timeout=0.05))
        assert result.passed is False
        assert result.reason == "timeout"

    def test_non_string_response_fails_bad_type(self):
        async def probe(model_id):
            return 42  # 非 str(编程 bug)
        result = _run(interview_model(_nv(), probe=probe))
        assert result.passed is False
        assert "bad_response_type" in result.reason

    def test_existing_tier_preserved(self):
        """model.tier 已设(非 None)→ label_tier 保留,不覆盖。"""
        result = _run(interview_model(_nv("nvidia/foo-70b", tier="fast"), probe=_probe_returning("ok")))
        assert result.model.tier == "fast"  # 已有 fast 保留,不被 70b 推断成 strong

    def test_snippet_truncated(self):
        long = "x" * 200
        result = _run(interview_model(_nv(), probe=_probe_returning(long)))
        assert result.response_snippet is not None
        assert result.response_snippet.endswith("...")
        assert len(result.response_snippet) <= 83  # 80 + "..."

    def test_result_is_immutable(self):
        result = _run(interview_model(_nv(), probe=_probe_returning("ok")))
        assert isinstance(result, InterviewResult)
        try:
            result.passed = False  # type: ignore[misc]
            raise AssertionError("frozen 应不可变")
        except Exception:
            pass


class TestInterviewBatch:
    def test_concurrent_results_in_order(self):
        models = [_nv("nvidia/a-70b"), _nv("nvidia/b-mini"), _nv("nvidia/c-70b")]

        async def probe(model_id):
            # 不同模型返不同内容,验证顺序对应
            return {"nvidia/a-70b": "aa", "nvidia/b-mini": "", "nvidia/c-70b": "cc"}[model_id]

        results = _run(interview_batch(models, probe=probe))
        assert len(results) == 3
        assert [r.model.model_id for r in results] == ["nvidia/a-70b", "nvidia/b-mini", "nvidia/c-70b"]
        assert results[0].passed is True
        assert results[1].passed is False  # 空内容
        assert results[2].passed is True

    def test_one_failure_does_not_affect_others(self):
        models = [_nv("nvidia/a-70b"), _nv("nvidia/b-70b"), _nv("nvidia/c-70b")]

        async def probe(model_id):
            if model_id == "nvidia/b-70b":
                raise ValueError("boom")
            return "ok"

        results = _run(interview_batch(models, probe=probe))
        assert results[0].passed is True
        assert results[1].passed is False
        assert results[1].reason == "error:ValueError"
        assert results[2].passed is True

    def test_empty_batch(self):
        assert _run(interview_batch([], probe=_probe_returning("ok"))) == []


class TestPassedModels:
    def test_filters_to_passed_only(self):
        results = [
            InterviewResult(_nv("a", tier="strong"), True, "ok", None, "a"),
            InterviewResult(_nv("b", tier="fast"), False, "empty_content", None, None),
            InterviewResult(_nv("c", tier="strong"), True, "ok", None, "c"),
        ]
        passed = passed_models(results)
        assert [m.model_id for m in passed] == ["a", "c"]
        assert all(m.tier is not None for m in passed)

    def test_all_fail_returns_empty(self):
        results = [
            InterviewResult(_nv("a"), False, "timeout", None, None),
            InterviewResult(_nv("b"), False, "error:RuntimeError", None, None),
        ]
        assert passed_models(results) == []
