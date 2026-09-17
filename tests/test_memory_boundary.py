"""Граница пакета памяти: `agent_memory` не знает ни о ядре, ни о транспорте.

Память — выносимый модуль: всё внешнее (store, LLM, пороги) приходит аргументами.
Импорт `app` или aiogram превратил бы её обратно в часть этой системы, а заметить
это по диффу нельзя — поэтому проверяет AST, а не глаз.
"""
import ast
from pathlib import Path

FORBIDDEN = ("app", "skills", "aiogram", "openai", "structlog")

ROOT = Path(__file__).resolve().parent.parent / "agent_memory"


def _imported_roots(tree: ast.AST) -> set[str]:
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
    return roots


def test_memory_package_imports_nothing_from_the_system():
    offenders: dict[str, set[str]] = {}
    for path in sorted(ROOT.rglob("*.py")):
        bad = _imported_roots(ast.parse(path.read_text())) & set(FORBIDDEN)
        if bad:
            offenders[path.name] = bad
    assert offenders == {}
