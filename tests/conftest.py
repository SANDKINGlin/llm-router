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
    "AGNES_API_KEY", "MODELSCOPE_API_KEY",
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
    # S2 (2026-08-04): X-Test-Token 旁路 (auth.is_test_token_bypass_allowed) 需要
    # 三门禁同时满足 (header + env + host). 单测 autouse 显式开 ENV flag, 默认 off 生产永远 off.
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
def _init_placeholder_dbs() -> Iterator[None]:
    """R35 (2026-08-09): 防 phase1_load_and_recovery test_lifespan_with_no_probe_targets_zero_pollution
    因 production data dir 缺 trace.db/ledger.db/circuit.db/health.db 而假绿.
    session 开跑前预建 4 个空 SQLite 占位 db, 让测试能在 conftest 清 key 的环境
    下仍能跑通 sanity check.
    """
    os.makedirs("data", exist_ok=True)
    for name in ("trace.db", "ledger.db", "circuit.db", "health.db", "keys.db", "scanner.db"):
        path = os.path.join("data", name)
        if not os.path.exists(path):
            conn = sqlite3.connect(path)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.commit()
            conn.close()
    yield


@pytest.fixture(scope="session", autouse=True)
def _wal_checkpoint_guard() -> Iterator[None]:
    """会话开跑前 + 跑完后 checkpoint data/*.db(防脏 WAL 间歇性挂起,见 _checkpoint_data_dbs)。"""
    _checkpoint_data_dbs()
    yield
    _checkpoint_data_dbs()


# ===== R39 (2026-08-10) 三方共识 A2 实施 =====
# 修 test_circuit_breaker_status_accuracy + test_concurrent_same_key_executes_once
# 2 个 pre-existing flaky test (单跑 PASS, 全 integration 5/5 fail = 100% 复现).
# 三方共识 (Hermes+Codex+OpenCode, MD5 互异 3/3) 推荐 A2 = P158 + P140 + P167 3 修法叠加.
#
# ⚠️ 实施约束 (2026-08-10 第一版超时教训):
# - 不加 _isolated_event_loop (autouse set_event_loop 跟 pytest-asyncio AUTO mode 冲突 → 60s timeout)
# - 不 monkeypatch TestClient.__exit__ (改全局行为风险高, pytest-asyncio 内部用 TestClient 多次)
# - 只加 P140 "data dir 隔离 fixture": 每条 test 临时 LLM_ROUTER_DATA_DIR 隔离 sqlite 写入,
#   避免 circuit.db / health.db 等跨 test 状态污染 (跟 R29 R32 同样思路)


@pytest.fixture(autouse=True)
def _isolated_data_dir_per_test(tmp_path, monkeypatch):
    """R39 P140 实施: 每条 test 用 tmp_path 隔离 LLM_ROUTER_DATA_DIR.

    修法 = 每条 test 自动建一个空 sqlite db dir, 避免前一条 test 残留的 WAL/lock
    影响下一条. TestClient 走 LLM_ROUTER_DATA_DIR 读 circuit.db/health.db, 隔离后
    各 test 状态完全干净.
    """
    data_dir = tmp_path / "r39_data"
    data_dir.mkdir(exist_ok=True)
    # 预建 R35 占位 db (跟 _init_placeholder_dbs 同步)
    for name in ("trace.db", "ledger.db", "circuit.db", "health.db", "keys.db", "scanner.db"):
        path = data_dir / name
        if not path.exists():
            conn = sqlite3.connect(str(path))
            conn.execute("PRAGMA journal_mode=WAL")
            conn.commit()
            conn.close()
    monkeypatch.setenv("LLM_ROUTER_DATA_DIR", str(data_dir))
    # 给当前 test yield tmp data dir
    yield str(data_dir)

