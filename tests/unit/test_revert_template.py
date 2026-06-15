"""S1.4 · revert.sh 切片回滚模板骨架 — 冒烟测试。

revert.sh 是破坏性操作脚本(删 DB / 杀进程),本套件只验【安全骨架】,不触发真破坏:
  --check        自检退出 0
  --dry-run      无 manifest 时退出 2;有 manifest 时退出 0 + 打印清单
  --exec 无 --yes 拒绝执行(退出 3)—— 防误触的关键安全门
  无参/-h         打印 usage

不测 --exec --yes 的真实破坏(删 DB/杀进程)——那是运行时操作,留给手动/exec,
单测里触它会污染 data/ 与杀掉真实 :8789(若有)。守 surgical:只验骨架契约。
"""
from __future__ import annotations

import os
import subprocess

SCRIPT = os.path.join(
    os.path.dirname(__file__), "..", "..", "scripts", "revert.sh"
)
SCRIPT = os.path.abspath(SCRIPT)


def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", SCRIPT, *args],
        capture_output=True,
        text=True,
        timeout=15,
    )


def test_check_exits_zero():
    """--check 自检(语法 + 依赖)退出 0。"""
    r = _run("--check")
    assert r.returncode == 0, r.stderr
    assert "自检通过" in r.stdout


def test_no_args_prints_usage():
    """无参打印 usage(非崩溃)。"""
    r = _run()
    assert r.returncode == 0
    assert "revert.sh" in r.stdout
    assert "--exec" in r.stdout


def test_dry_run_missing_manifest_exits_nonzero():
    """--dry-run 一个没填 manifest 的切片 → 退出 2(提示复制模板)。"""
    r = _run("--dry-run", "NONEXISTENT_SLICE_XYZ")
    assert r.returncode != 0
    assert "NONEXISTENT_SLICE_XYZ" in r.stderr or "NONEXISTENT_SLICE_XYZ" in r.stdout


def test_dry_run_with_manifest_exits_zero(tmp_path, monkeypatch):
    """建一个临时 archive/<slice>/revert_manifest.md → --dry-run 退出 0 并打印清单。"""
    # 用环境变量把 PROJ_ROOT 指到 tmp(脚本用 BASH_SOURCE 相对路径定位 PROJ_ROOT,
    # 故直接在真实 archive 下建临时 slice,测完清理)
    import pathlib
    archive = pathlib.Path(SCRIPT).parent.parent / "archive" / "_smoketest_slice"
    archive.mkdir(parents=True, exist_ok=True)
    manifest = archive / "revert_manifest.md"
    try:
        manifest.write_text("# test manifest\n- 改动:无\n")
        r = _run("--dry-run", "_smoketest_slice")
        assert r.returncode == 0, r.stderr
        assert "test manifest" in r.stdout
        assert "dry-run" in r.stdout.lower()
        assert "8789" in r.stdout
    finally:
        manifest.unlink(missing_ok=True)
        archive.rmdir()


def test_exec_without_yes_is_refused():
    """--exec 不带 --yes → 拒绝执行(退出 3)—— 防误触安全门。"""
    r = _run("--exec", "_smoketest_slice")
    assert r.returncode == 3
    assert "--yes" in r.stderr or "二次确认" in r.stderr
