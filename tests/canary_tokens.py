"""Shared canary literal/identifier tokens matching tests/docker/canary_fixture.sql
(§12: "the project's conscience"). Any future section's tests should scan
their output against these, in addition to test_no_values_scan.py's own
generic pass over the instance section.
"""

from __future__ import annotations

import json
from typing import Any

CANARY_LITERALS: tuple[str, ...] = (
    "alice.wonderland@canary-example.test",
    "Alice Wonderland",
    "bob.builder@canary-example.test",
    "Bob Builder",
    "carol.danvers@canary-example.test",
    "Carol Danvers",
    "123456.78",
    "98765.43",
    "55555.55",
    "11111111-1111-1111-1111-111111111111",
    "22222222-2222-2222-2222-222222222222",
    "33333333-3333-3333-3333-333333333333",
)

CANARY_IDENTIFIERS: tuple[str, ...] = (
    "xq_secret_salaries",
    "employee_email",
    "employee_name",
    "salary_dollars",
    "external_uuid",
)


def find_canary_tokens(obj: Any, tokens: tuple[str, ...]) -> list[str]:
    """Serialize obj and report which of tokens appear anywhere in it."""
    text = json.dumps(obj, default=str)
    return [token for token in tokens if token in text]
