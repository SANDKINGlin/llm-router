"""Phase A · 最小 CLI 单测(对应 OpenSpec spec/phase-a-cli)。

CLI:python -m llm_router.cli test-provider [--mock] [--provider NAME]
- mock 模式默认走(免烧额度)
- 非 mock 走真实调用(本次 Phase A 不测真实,留 stub 入口)
"""
from __future__ import annotations

import subprocess
import sys

import pytest


def _run_cli(*args: str) -> subprocess.CompletedProcess:
    """跑 python -m llm_router.cli <args>。cwd 必须在仓库根,PYTHONPATH=src。"""
    return subprocess.run(
        [sys.executable, "-m", "llm_router.cli", *args],
        capture_output=True,
        text=True,
        cwd="/home/lin/projects/llm-router",
        env={"PYTHONPATH": "src", "PATH": "/usr/bin:/usr/local/bin"},
        timeout=10,
    )


class TestCliMockMode:
    """mock 模式(默认)— 不消耗真实额度。"""

    def test_test_provider_mock_default_openai(self):
        """test-provider 不传 --mock → 默认 mock → 输出 [mock] openai OK,exit 0。"""

        res = _run_cli("test-provider")
        assert res.returncode == 0, f"stderr={res.stderr}"
        assert "[mock] openai OK" in res.stdout

    def test_test_provider_mock_explicit_flag(self):
        """test-provider --mock → 输出 [mock] openai OK,exit 0。"""

        res = _run_cli("test-provider", "--mock")
        assert res.returncode == 0, f"stderr={res.stderr}"
        assert "[mock] openai OK" in res.stdout

    def test_test_provider_mock_anthropic(self):
        """test-provider --provider anthropic --mock → 输出 [mock] anthropic OK。"""

        res = _run_cli("test-provider", "--mock", "--provider", "anthropic")
        assert res.returncode == 0, f"stderr={res.stderr}"
        assert "[mock] anthropic OK" in res.stdout

    def test_test_provider_invalid_provider_fails(self):
        """test-provider --provider invalid → 报错非 0 退出。"""

        res = _run_cli("test-provider", "--mock", "--provider", "invalid-provider")
        assert res.returncode != 0
        assert "invalid" in res.stderr.lower() or "unsupported" in res.stderr.lower()


class TestCliModuleImport:
    """模块可导入 — 不引入新依赖(只用 stdlib argparse)。"""

    def test_cli_module_importable(self):
        """llm_router.cli 可 import。"""

        from llm_router import cli  # noqa: F401

        assert hasattr(cli, "main")
