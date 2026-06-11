"""只读保护测试 + 静态扫描。"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

FORBIDDEN_METHODS = {"placeOrder", "cancelOrder", "reqGlobalCancel", "exerciseOptions"}

PROJECT_ROOT = Path(__file__).parent.parent


def _get_all_py_files() -> list[Path]:
    """获取 app/ 目录下所有 Python 文件。"""
    app_dir = PROJECT_ROOT / "app"
    return list(app_dir.rglob("*.py"))


class TestReadOnlyGuards:
    """静态扫描：确保业务代码中不包含禁止的交易方法调用。"""

    def test_no_forbidden_calls_in_app(self):
        """扫描 app/ 目录，确保没有调用禁止的交易方法。"""
        violations = []
        for py_file in _get_all_py_files():
            if py_file.name == "__init__.py":
                continue
            try:
                tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
            except (SyntaxError, UnicodeDecodeError):
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    func = node.func
                    name = ""
                    if isinstance(func, ast.Attribute):
                        name = func.attr
                    elif isinstance(func, ast.Name):
                        name = func.id
                    if name in FORBIDDEN_METHODS:
                        violations.append(f"{py_file}:{node.lineno} calls {name}")
        assert violations == [], f"发现禁止的交易方法调用:\n" + "\n".join(violations)

    def test_no_forbidden_imports(self):
        """确保没有导入交易相关的封装模块。"""
        violations = []
        for py_file in _get_all_py_files():
            if py_file.name == "__init__.py":
                continue
            try:
                tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
            except (SyntaxError, UnicodeDecodeError):
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Attribute):
                    if node.attr in FORBIDDEN_METHODS:
                        violations.append(f"{py_file}:{node.lineno} references {node.attr}")
        assert violations == [], f"发现禁止的方法引用:\n" + "\n".join(violations)

    def test_read_only_default(self):
        """验证 read_only 默认为 true。"""
        from app.config import Settings
        s = Settings()
        assert s.read_only is True

    def test_read_only_false_rejected(self, monkeypatch):
        """验证 read_only=false 时拒绝启动。"""
        monkeypatch.setenv("READ_ONLY", "false")
        from app.config import Settings
        with pytest.raises(SystemExit):
            Settings()
