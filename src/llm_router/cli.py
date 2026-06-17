"""Phase A · 最小 CLI(对应 OpenSpec spec/phase-a-cli)。

用法:
    python -m llm_router.cli test-provider [--mock] [--provider NAME]

Phase A 范围内只做 mock 模式(免烧真实额度);真实模式留 stub 入口,
后续 Phase B 接入完整 ProviderRegistry 时再实装。
"""
from __future__ import annotations

import argparse
import sys

# Phase A 支持的 provider(只 mock,接真实在 Phase B)
SUPPORTED_PROVIDERS = frozenset({"openai", "anthropic"})


def _cmd_test_provider(args: argparse.Namespace) -> int:
    """test-provider 子命令:验证 provider 连通性(mock 模式默认)。"""
    provider = args.provider
    mock = args.mock or not args.real  # 默认 mock;只有 --real 才走真实

    if provider not in SUPPORTED_PROVIDERS:
        print(
            f"error: provider '{provider}' is not supported (Phase A: {sorted(SUPPORTED_PROVIDERS)})",
            file=sys.stderr,
        )
        return 1

    if mock:
        print(f"[mock] {provider} OK")
        return 0

    # 真实模式 stub:Phase A 不实现,留给 Phase B
    print(f"error: real mode not implemented in Phase A, use --mock", file=sys.stderr)
    return 2


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m llm_router.cli",
        description="llm-router Phase A 最小 CLI",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("test-provider", help="验证 provider 连通性(默认 mock)")
    p.add_argument(
        "--provider",
        default="openai",
        help=f"provider 名(Phase A 支持: {sorted(SUPPORTED_PROVIDERS)})",
    )
    p.add_argument(
        "--mock",
        action="store_true",
        default=True,
        help="mock 模式,不消耗真实额度(默认)",
    )
    p.add_argument(
        "--real",
        action="store_true",
        help="真实模式(Phase A 不实现,留 stub)",
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI 入口。返回 exit code。"""
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "test-provider":
        return _cmd_test_provider(args)

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
