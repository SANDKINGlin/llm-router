"""共享 pytest fixtures。

_no_proxy_env(autouse):本机 clash-verge 在 env 注入 SOCKS/HTTP 代理
(all_proxy=socks5://..., http(s)_proxy=http://...),会泄漏进 httpx。
openai.AsyncOpenAI 构造时 httpx 读到 socks 代理 → 尝试 socks transport
(socksio 未装)→ ImportError,且与 respx 传输层 mock 无关(请求还没发就在构造期崩)。
测试必须 hermetic:respx 在传输层 mock,不需要真代理。故每条测试前清掉代理 env。
不影响既有 83 基线:那些测试用 FastAPI TestClient(进程内 ASGI),不发真 socket。
"""
from __future__ import annotations

import pytest

_PROXY_ENV_KEYS = (
    "ALL_PROXY", "all_proxy",
    "HTTP_PROXY", "http_proxy",
    "HTTPS_PROXY", "https_proxy",
    "NO_PROXY", "no_proxy",
)


@pytest.fixture(autouse=True)
def _no_proxy_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in _PROXY_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
