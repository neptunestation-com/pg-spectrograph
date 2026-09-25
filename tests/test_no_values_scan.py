"""The no-values canary scan (§12): the project's conscience. A captured
section must never contain any of the distinctive literal values seeded into
the canary fixture's data. Runs against the instance section now; every
future section (schema, column_stats, workload, ...) must be scanned the
same way as it lands.
"""

from __future__ import annotations

from pgspec.capture import connect, probe_capabilities
from pgspec.sections.instance import capture_instance

from canary_tokens import CANARY_LITERALS, find_canary_tokens


def test_instance_section_contains_no_canary_literals(pg16_dsn):
    conn = connect(pg16_dsn)
    try:
        capabilities = probe_capabilities(conn)
        result = capture_instance(conn, capabilities)
    finally:
        conn.close()

    hits = find_canary_tokens(result, CANARY_LITERALS)
    assert hits == [], f"canary literal(s) leaked into instance section: {hits}"
