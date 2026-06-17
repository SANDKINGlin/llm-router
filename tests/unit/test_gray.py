"""S4.1 · 灰度分流键纯函数测试(policy 灰度发布机制,D9 design.md line 138/152)。

Phase1 范围 A(机制就位):gray_release 纯函数 + derive_session_id 派生。
Phase1 只一策略/一 policy_version,灰度内/外走同一策略——本测试覆盖**机制正确性**
(确定性/边界/分布/派生优先级/安全),不假装"新旧切换行为"(Phase1 无第二策略)。

TDD red:先于 gray.py 实现。纯函数无 DB,不碰 WAL,测试干净。
"""
from llm_router.api.gray import gray_release, derive_session_id


class TestGrayRelease:
    """gray_release(session_id, gray_percent) -> bool:hash % 100 < gray_percent。"""

    def test_deterministic_same_input_same_result(self):
        # 同 session_id 同 percent → 同结果(session 钉定/灰度稳定的基础)
        assert gray_release("sess-abc", 30) == gray_release("sess-abc", 30)

    def test_zero_percent_never_released(self):
        for sid in ["a", "b", "c", "long-session-id-xyz", "yet-another"]:
            assert gray_release(sid, 0) is False

    def test_hundred_percent_always_released(self):
        for sid in ["a", "b", "c", "long-session-id-xyz", "yet-another"]:
            assert gray_release(sid, 100) is True

    def test_clamp_above_100_equivalent_to_100(self):
        sid = "sess-clamp-high"
        assert gray_release(sid, 150) is True
        assert gray_release(sid, 150) == gray_release(sid, 100)

    def test_clamp_below_zero_equivalent_to_0(self):
        sid = "sess-clamp-low"
        assert gray_release(sid, -5) is False
        assert gray_release(sid, -5) == gray_release(sid, 0)

    def test_distribution_roughly_matches_percent(self):
        # 大样本确定性序列(gray_percent=30 → 落桶比例 ~30%,tolerance ±5%)
        released = sum(1 for i in range(2000) if gray_release(f"sess-{i}", 30))
        # 2000 × 30% = 600,tolerance ±5%(100)→ [500, 700]
        assert 500 <= released <= 700, f"distribution off: {released}/2000"

    def test_monotonic_in_gray_percent(self):
        # 灰度比例单调:gray_percent 越大,落桶集是扩集(30⊆50⊆80)。同 session_id。
        sid = "sess-mono"
        r30 = gray_release(sid, 30)
        r50 = gray_release(sid, 50)
        r80 = gray_release(sid, 80)
        # 若 r30 True 则 r50/r80 必 True(桶集扩张,不收缩)
        assert not r30 or r50
        assert not r50 or r80

    def test_stable_hash_no_pythonhashseed_dependency(self):
        # blake2b 是确定性哈希(标准库契约),不受 PYTHONHASHSEED 影响
        # (Python 内置 hash() 会随机化→不可用)。显式断言纯度。
        from llm_router.api.gray import _stable_int

        assert _stable_int("restart-test") == _stable_int("restart-test")
        assert isinstance(_stable_int("restart-test"), int)


class TestDeriveSessionId:
    """derive_session_id(api_key, explicit) -> str | None(D9 按 agent 灰度)。"""

    def test_explicit_overrides_api_key(self):
        # X-Session-Id header 显式传 → 优先于 api_key 派生
        assert derive_session_id(api_key="sk-real-key", explicit="explicit-sess") == "explicit-sess"

    def test_api_key_derivation_deterministic(self):
        # 同 key → 同派生串(按 agent 灰度的基础:同 agent 同桶)
        assert derive_session_id(api_key="sk-real-key") == derive_session_id(api_key="sk-real-key")

    def test_different_keys_different_sessions(self):
        assert derive_session_id(api_key="sk-key-a") != derive_session_id(api_key="sk-key-b")

    def test_both_none_returns_none(self):
        assert derive_session_id(api_key=None, explicit=None) is None

    def test_explicit_only(self):
        assert derive_session_id(api_key=None, explicit="x-session-id") == "x-session-id"

    def test_empty_strings_treated_as_absent(self):
        # 空串等同缺失(回退下一优先级或 None)
        assert derive_session_id(api_key="", explicit="x") == "x"
        # 空 explicit 回退到 api_key 派生(与仅传 api_key 一致;OpenCode LOW#3)
        assert derive_session_id(api_key="sk-key", explicit="") == derive_session_id(api_key="sk-key")
        assert derive_session_id(api_key="", explicit="") is None

    def test_derived_does_not_leak_raw_key(self):
        # 安全:派生串不含原始 key(防 key 进 log/trace)
        secret = "sk-SECRET-TOKEN-12345"
        derived = derive_session_id(api_key=secret)
        assert derived is not None
        assert "SECRET" not in derived
        assert secret not in derived
