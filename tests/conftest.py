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

import glob
import os
import sqlite3
from collections.abc import Iterator

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
    # D7 fix: 集成测试需要 X-Test-Token 旁路工作 (admin/auth.py:41 +
    # admin/auth_enhanced.py:267), 显式开 ENV flag (硬门禁, 默认 off, 生产永远 off).
    # 单测自动 on, 不影响生产路径.
    monkeypatch.setenv("LLM_ROUTER_TEST_TOKEN_BYPASS", "on")


def _checkpoint_data_dbs() -> None:
    """Checkpoint(TRUNCATE)所有 data/*.db,清脏 WAL。

    防"脏 WAL 间歇性挂起全量 pytest"(交班 §三.12/§六.4 实犯多次):被中断的 pytest 在
    真 data/ 留 .db-wal,下次全量开库(test_health.py 经模块级 _cascade 单例碰真库)可能卡
    死被 timeout 杀。session 开跑前 + 跑完后各清一次,根除间歇性假绿。busy 忽略:有连接持
    有时 checkpoint 返回 busy=1 跳过该次,不抛错中断测试。
    """
    for f in glob.glob("data/*.db"):
        try:
            conn = sqlite3.connect(f)
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            conn.close()
        except sqlite3.Error:
            pass


@pytest.fixture(scope="session", autouse=True)
def _wal_checkpoint_guard() -> Iterator[None]:
    """会话开跑前 + 跑完后 checkpoint data/*.db(防脏 WAL 间歇性挂起,见 _checkpoint_data_dbs)。"""
    _checkpoint_data_dbs()
    yield
    _checkpoint_data_dbs()

