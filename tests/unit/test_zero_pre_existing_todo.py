"""R44 — 0 pre-existing TODO 验证 (8-13 三方共识)

目的: 锁定 src/llm_router/ 全仓 0 TODO 残留 (除 tests/ + docs/ + openspec/ 文档区)
历史: 8-12 R43 + R42 9 件 P+ follow-up 闭环中, master 含 2 个 prod TODO:
  - src/llm_router/admin/keys.py:21 (require_permission dead code, 0 callees)
  - src/llm_router/providers/openai.py:31 (S2.x 错误细分, _provider_error 已实装)

R44 实施 (8-13 三方真验后):
  - 删 keys.py require_permission 装饰器 + from functools import wraps
  - 删 openai.py:31 TODO 注释行, 改 docstring 反映实际实装

本测试锁状态:
  - src/llm_router/ 下 0 TODO 残留
  - require_permission 已从 admin.keys module 移除
  - openai.py OpenAIProvider docstring 不含 TODO
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest


SRC_ROOT = Path(__file__).resolve().parents[2] / "src" / "llm_router"


def _scan_src_for_todo() -> list[tuple[str, int, str]]:
    """扫 src/llm_router/ 全部 .py 找遗留 TODO (排除 docstring 内描述性提及).

    Returns:
        [(file_path, line_no, line_content), ...]
    """
    findings: list[tuple[str, int, str]] = []
    # 匹配 # TODO/FIXME/XXX (注释行) 或 docstring 内的 TODO
    # 排除中文描述里提到的 TODO/XXX (比如提到 "xxx+00:00" 时间格式)
    todo_re = re.compile(r"#\s*(TODO|FIXME|XXX)\b|^\s*\".*?\b(TODO|FIXME|XXX)\b", re.IGNORECASE)
    for py_file in SRC_ROOT.rglob("*.py"):
        # 排除 __pycache__
        if "__pycache__" in str(py_file):
            continue
        try:
            content = py_file.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for line_no, line in enumerate(content.splitlines(), start=1):
            if todo_re.search(line):
                rel = py_file.relative_to(SRC_ROOT.parent.parent)
                findings.append((str(rel), line_no, line.strip()))
    return findings


class TestZeroPreExistingTodo:
    """R44 锁 src/llm_router/ 0 TODO 残留 (跟 R30/R32/R34 治本 precedent 6/5)."""

    def test_no_todo_in_src_llm_router(self):
        """src/llm_router/ 下 0 TODO 残留."""
        findings = _scan_src_for_todo()
        assert findings == [], (
            f"R44: 发现 {len(findings)} 个 TODO 残留:\n"
            + "\n".join(f"  {path}:{line}: {content}" for path, line, content in findings)
        )

    def test_require_permission_removed_from_keys(self):
        """admin/keys.py: require_permission 装饰器已删 (dead code 清理)."""
        keys_py = SRC_ROOT / "admin" / "keys.py"
        assert keys_py.exists(), "admin/keys.py 不存在"
        content = keys_py.read_text(encoding="utf-8")
        assert "require_permission" not in content, (
            "require_permission 仍在 admin/keys.py 中 (R44 dead code 应已删除)"
        )
        assert "from functools import wraps" not in content, (
            "functools.wraps import 已无用, 应删除 (dead code 依赖)"
        )

    def test_openai_provider_docstring_no_todo(self):
        """providers/openai.py: OpenAIProvider docstring 不含 TODO."""
        openai_py = SRC_ROOT / "providers" / "openai.py"
        assert openai_py.exists(), "providers/openai.py 不存在"
        content = openai_py.read_text(encoding="utf-8")
        # 找 OpenAIProvider class 的 docstring (""" 块)
        m = re.search(r'class OpenAIProvider.*?:\s*\n\s*"""(.*?)"""', content, re.DOTALL)
        assert m, "OpenAIProvider class + docstring 未找到"
        docstring = m.group(1)
        assert "TODO" not in docstring, (
            f"OpenAIProvider docstring 仍含 TODO: {docstring[:200]}"
        )

    def test_require_enhanced_permission_still_used(self):
        """require_enhanced_permission (R18/R30/R34 已实装) 保持 4 处调用不变."""
        from llm_router.admin import auth_enhanced
        assert hasattr(auth_enhanced, "require_enhanced_permission"), (
            "require_enhanced_permission 应保留 (admin/app.py 4 处调用)"
        )
        # admin/app.py 必须仍引用 (R30/R34 治本链)
        admin_app_py = (SRC_ROOT / "admin" / "app.py").read_text(encoding="utf-8")
        usage_count = admin_app_py.count("require_enhanced_permission(")
        assert usage_count >= 4, (
            f"admin/app.py require_enhanced_permission 调用数 {usage_count} < 4 (R30/R34 治本链应保留)"
        )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])