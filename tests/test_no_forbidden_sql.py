"""Static guard: no section's extraction SQL may ever reference
most_common_vals (§7.2, §12 "Forbidden-column test"). Vacuous until Milestone
4 introduces column_stats.py's real extraction queries; load-bearing from
then on.
"""

from __future__ import annotations

from pathlib import Path

FORBIDDEN_TOKENS = ("most_common_vals",)

SECTIONS_DIR = Path(__file__).parent.parent / "src" / "pgspec" / "sections"


def test_no_section_module_references_most_common_vals():
    offenders = []
    for path in sorted(SECTIONS_DIR.glob("*.py")):
        text = path.read_text()
        for token in FORBIDDEN_TOKENS:
            if token in text:
                offenders.append((path.name, token))
    assert offenders == []
