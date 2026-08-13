#!/usr/bin/env python3
"""D5 切片: OpenSpec validate + inherit-link check.

目的: 接 CI (`.github/workflows/openspec-validate.yml`), 在 PR 时校验
       - 每个 openspec/changes/<change>/proposal.md 含 `## 继承自:` 段
         (B3 解法核心, 2026-07-25 三方共识: 缺段 = 警告, 缺链接 = 阻塞)
       - 每个 openspec/changes/<change>/tasks.md checkbox 状态汇总 (0 待办 vs > 0 待办)
       - 每个 openspec/changes/<change>/specs/<spec>/spec.md 不空

退出码:
  0 = ALL PASS
  1 = BLOCK (有继承自段缺失 或 链接无效)
  2 = WARN (有 spec 文件缺失, 不阻塞)

用法:
  .venv/bin/python scripts/openspec_validate.py [--change <change-name>] [--strict]
    --change  指定单个 change (默认全扫)
    --strict  warn 也算 fail

Reference:
  - v2 §3 B3 (OpenSpec 断链, 2026-07-27 三方共识)
  - D5 切片 commit: <待 commit>
"""
from __future__ import annotations
import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OPENSPEC = ROOT / "openspec" / "changes"

REQUIRED_HEADER = "## 继承自:"
LINK_PATTERN = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
ARCHIVE_HINT = re.compile(r"(?:~/ObsidianVault/|ObsidianVault/)", re.IGNORECASE)


def collect_changes(target: str | None) -> list[Path]:
    """Collect change dirs under openspec/changes/.

    跳过 archive/ 目录 (它是历史归档容器, 不是 active change).
    archive 内的子目录 (e.g. 2026-08-11-llm-router-phased) 各自带 proposal.md,
    也不应该被当 active change 扫 (8-13 R44 CI fix, 跟 R29 archive 流程一致).
    """
    if not OPENSPEC.exists():
        return []
    if target:
        return [OPENSPEC / target]
    changes = []
    for p in sorted(OPENSPEC.iterdir()):
        if not p.is_dir():
            continue
        if p.name == "archive":
            continue  # 跳过 archive 目录 (历史归档容器)
        changes.append(p)
    return changes


def check_change(change_dir: Path) -> tuple[str, list[str], list[str]]:
    """Return (status, errors, warnings) for one change dir."""
    errors: list[str] = []
    warnings: list[str] = []
    proposal = change_dir / "proposal.md"
    tasks = change_dir / "tasks.md"

    if not proposal.exists():
        errors.append(f"FAIL: {change_dir.name}/proposal.md 不存在")
        return "BLOCK", errors, warnings

    text = proposal.read_text(encoding="utf-8")
    if REQUIRED_HEADER not in text:
        errors.append(f"BLOCK: {change_dir.name}/proposal.md 缺 '## 继承自:' 段 (B3 解法核心)")
    else:
        # 校验继承自段后的链接
        inherit_section = text.split(REQUIRED_HEADER, 1)[1].split("\n##", 1)[0]
        links = LINK_PATTERN.findall(inherit_section)
        if not links:
            warnings.append(f"WARN: {change_dir.name} 继承自段无 markdown 链接")
        else:
            for label, url in links:
                if not ARCHIVE_HINT.search(url):
                    warnings.append(
                        f"WARN: {change_dir.name} 链接 '{label}' ({url}) 未指向 ObsidianVault 归档"
                    )

    # tasks.md 状态汇总
    if tasks.exists():
        task_text = tasks.read_text(encoding="utf-8")
        undone = len(re.findall(r"^- \[[ ]\]", task_text, re.MULTILINE))
        done = len(re.findall(r"^- \[[xX]\]", task_text, re.MULTILINE))
        print(f"  tasks.md: {done} done / {undone} undone / total={done + undone}")
        if done == 0 and (done + undone) > 0:
            warnings.append(
                f"WARN: {change_dir.name}/tasks.md 全是 [ ], 0 完成 (可能 OpenSpec 写法滞后)"
            )
    else:
        warnings.append(f"WARN: {change_dir.name}/tasks.md 缺失")

    # specs/ 子目录检查
    specs_dir = change_dir / "specs"
    if specs_dir.exists():
        spec_files = list(specs_dir.rglob("spec.md"))
        print(f"  specs/: {len(spec_files)} spec.md files")
        for spec in spec_files:
            spec_text = spec.read_text(encoding="utf-8")
            if len(spec_text.strip()) < 50:
                warnings.append(f"WARN: {spec.relative_to(ROOT)} 内容过短 (<50 chars)")
    else:
        warnings.append(f"WARN: {change_dir.name}/specs/ 目录缺失")

    status = "BLOCK" if errors else ("WARN" if warnings else "PASS")
    return status, errors, warnings


def main() -> int:
    parser = argparse.ArgumentParser(description="OpenSpec validate + inherit-link check")
    parser.add_argument("--change", help="指定单个 change (默认全扫)")
    parser.add_argument("--strict", action="store_true", help="warn 也算 fail")
    args = parser.parse_args()

    changes = collect_changes(args.change)
    if not changes:
        print("NO_OP: openspec/changes/ 下无 change")
        return 0

    print(f"openspec_validate: scan {len(changes)} change(s)")
    print("=" * 60)
    total_block = total_warn = total_pass = 0
    for change_dir in changes:
        if not change_dir.exists():
            print(f"\n[{change_dir.name}] SKIP: 目录不存在")
            continue
        status, errors, warnings = check_change(change_dir)
        print(f"\n[{change_dir.name}] {status}")
        for e in errors:
            print(f"  ERROR: {e}")
        for w in warnings:
            print(f"  WARN:  {w}")
        if status == "BLOCK":
            total_block += 1
        elif status == "WARN":
            total_warn += 1
        else:
            total_pass += 1

    print("\n" + "=" * 60)
    print(f"汇总: PASS={total_pass} / WARN={total_warn} / BLOCK={total_block}")
    if total_block > 0:
        return 1
    if args.strict and total_warn > 0:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())