"""共享 pytest fixtures + 测试环境 hermetic 化。

两件事:
1. _no_proxy_env(autouse):本机 clash-verge 在 env 注入 SOCKS/HTTP 代理,泄漏进 httpx 致
   AsyncOpenAI 构造 socks transport(socksio 未装)ImportError。每条测试前清代理 env。
2. 模块级清 provider API key env:app._build_cascade 在 **import 期** 读 os.environ 建候选池,
   若主机 env 泄漏了真 key(如 OPENROUTER_API_KEY)→ 建真 adapter → 基线测试(mock-only 假设)
   失败(真 provider 抢答)。故 conftest import 时(早于 test 模块 import app)清掉 provider key,
   保证 app import 期是 mock-only。真 key 测试另写(显式注入 env + 重建 cascade,不走 import 单例)。
"""
from __future__ import annotations

import os

import pytest

# 模块级:conftest import 时(早于 test 模块 import llm_router.app)清 provider key env,
# 保证 app import 期 _build_cascade 是 mock-only(hermetic 基线)。
_PROVIDER_KEY_ENV = (
    "OPENROUTER_API_KEY", "GROQ_API_KEY", "NVIDIA_API_KEY",
    "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "MISTRAL_API_KEY",
)
for _k in _PROVIDER_KEY_ENV:
    os.environ.pop(_k, None)

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

