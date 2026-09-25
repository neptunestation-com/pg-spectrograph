"""The no-identifiers canary scan (§12): a captured section must never
contain any of the canary fixture's real table/column names. Runs against
the instance section now (which is expected to never touch user-schema
identifiers at all); every future section that does touch schema/column
identifiers must pseudonymize them before this scan is meaningful for it.
"""

from __future__ import annotations

from pgspec.capture import connect, probe_capabilities
from pgspec.sections.instance import capture_instance

from canary_tokens import CANARY_IDENTIFIERS, find_canary_tokens


def test_instance_section_contains_no_canary_identifiers(pg16_dsn):
    conn = connect(pg16_dsn)
    try:
        capabilities = probe_capabilities(conn)
        result = capture_instance(conn, capabilities)
    finally:
        conn.close()

    hits = find_canary_tokens(result, CANARY_IDENTIFIERS)
    assert hits == [], f"canary identifier(s) leaked into instance section: {hits}"
