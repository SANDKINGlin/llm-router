"""S2 helper 单测: is_test_token_bypass_allowed (auth.py) 三门禁 defense-in-depth.

物证 (2026-08-04 S2 真验):
- 三门禁同时满足才 True: header x-test-token=r8-test-token + ENV LLM_ROUTER_TEST_TOKEN_BYPASS=on + host ∈ loopback set
- 任一不满足 → False
- 单源真相 (auth.py + auth_enhanced.py 共用), 避免 X-Test-Token 逻辑漂移
"""
from unittest.mock import patch

import pytest
from fastapi import Request

from llm_router.admin.auth import is_test_token_bypass_allowed


def _mk_request(headers=None, host="testclient"):
    """构造 mock Request (足够 helper 三字段访问)."""
    req = Request(scope={
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": [(k.lower().encode(), str(v).encode()) for k, v in (headers or {}).items()],
        "client": (host, 1234),
    })
    return req


class TestIsTestTokenBypassAllowed:
    """三门禁独立性 — 任一不满足返 False."""

    def test_all_three_satisfied_returns_true(self):
        """header + env on + loopback host → True."""
        req = _mk_request(
            headers={"x-test-token": "r8-test-token"},
            host="127.0.0.1",
        )
        with patch.dict("os.environ", {"LLM_ROUTER_TEST_TOKEN_BYPASS": "on"}):
            assert is_test_token_bypass_allowed(req) is True

    def test_missing_header_returns_false(self):
        """无 header → False (即使 env + host 满足)."""
        req = _mk_request(host="127.0.0.1")
        with patch.dict("os.environ", {"LLM_ROUTER_TEST_TOKEN_BYPASS": "on"}):
            assert is_test_token_bypass_allowed(req) is False

    def test_wrong_header_value_returns_false(self):
        """header 值错误 → False."""
        req = _mk_request(
            headers={"x-test-token": "wrong-token"},
            host="127.0.0.1",
        )
        with patch.dict("os.environ", {"LLM_ROUTER_TEST_TOKEN_BYPASS": "on"}):
            assert is_test_token_bypass_allowed(req) is False

    def test_env_off_returns_false(self):
        """env 默认 off (未设置) → False, 跟生产默认一致."""
        req = _mk_request(
            headers={"x-test-token": "r8-test-token"},
            host="127.0.0.1",
        )
        with patch.dict("os.environ", {}, clear=True):
            assert is_test_token_bypass_allowed(req) is False

    def test_env_on_capitalized_returns_true(self):
        """ENV 大写 ON 也接受 (.lower() 兼容)."""
        req = _mk_request(
            headers={"x-test-token": "r8-test-token"},
            host="127.0.0.1",
        )
        with patch.dict("os.environ", {"LLM_ROUTER_TEST_TOKEN_BYPASS": "ON"}):
            assert is_test_token_bypass_allowed(req) is True

    def test_remote_host_returns_false_even_with_env_on(self):
        """远程 host 即使 env on → False (软门禁拦截)."""
        req = _mk_request(
            headers={"x-test-token": "r8-test-token"},
            host="203.0.113.7",  # 文档示例 remote IP
        )
        with patch.dict("os.environ", {"LLM_ROUTER_TEST_TOKEN_BYPASS": "on"}):
            assert is_test_token_bypass_allowed(req) is False

    @pytest.mark.parametrize("host", ["127.0.0.1", "::1", "localhost", "testclient"])
    def test_all_allowed_hosts_return_true(self, host):
        """白名单 host 全生效."""
        req = _mk_request(
            headers={"x-test-token": "r8-test-token"},
            host=host,
        )
        with patch.dict("os.environ", {"LLM_ROUTER_TEST_TOKEN_BYPASS": "on"}):
            assert is_test_token_bypass_allowed(req) is True


class TestRepeatedEvilThings:
    """模拟攻击者 / 三方误用, 确认 helper fail-closed."""

    def test_all_three_pieces_attacker_tries(self):
        """攻击者同时改全部 3 个条件测试集 — 没意义, 没法绕. 验证第三次缺一就 fail."""
        # 真生产路径: env unset, header 有, host 任意 → False
        req = _mk_request(
            headers={"x-test-token": "r8-test-token"},
            host="8.8.8.8",
        )
        with patch.dict("os.environ", {}, clear=True):
            assert is_test_token_bypass_allowed(req) is False
