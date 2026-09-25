"""Static guard: no section's extraction SQL may ever reference
most_common_vals (§7.2, §12 "Forbidden-column test"). Scans only the SQL
string constants (module-level names ending in _SQL), not comments or
docstrings that legitimately discuss the invariant in prose. Vacuous until
Milestone 4 introduced column_stats.py's real extraction queries; load-bearing
from then on.
"""

from __future__ import annotations

import ast
from pathlib import Path

FORBIDDEN_TOKENS = ("most_common_vals",)

SECTIONS_DIR = Path(__file__).parent.parent / "src" / "pgspec" / "sections"


def _sql_constants(path: Path) -> dict[str, str]:
    tree = ast.parse(path.read_text(), filename=str(path))
    constants: dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        value = node.value
        if not (isinstance(value, ast.Constant) and isinstance(value.value, str)):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id.endswith("_SQL"):
                constants[target.id] = value.value
    return constants


def test_no_section_sql_constant_references_most_common_vals():
    offenders = []
    for path in sorted(SECTIONS_DIR.glob("*.py")):
        for name, sql in _sql_constants(path).items():
            for token in FORBIDDEN_TOKENS:
                if token in sql:
                    offenders.append((path.name, name, token))
    assert offenders == []
