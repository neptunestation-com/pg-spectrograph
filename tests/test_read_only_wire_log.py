"""The read-only wire-log test (§12): capturing the instance section must
never send a non-SELECT statement over the wire, excepting SETs. This is
the project's conscience alongside the no-values/no-identifiers scans below,
and it is fully meaningful starting now (not just once every section exists),
since capture.py already issues real queries.
"""

from __future__ import annotations

from pgspec.capture import connect, probe_capabilities
from pgspec.sections.instance import capture_instance

ALLOWED_NON_SELECT_PREFIXES = ("SET ",)


def test_capture_instance_wire_log_is_read_only(pg16_dsn):
    query_log: list[str] = []
    conn = connect(pg16_dsn, query_log=query_log)
    try:
        capabilities = probe_capabilities(conn)
        capture_instance(conn, capabilities)
    finally:
        conn.close()

    assert query_log, "expected at least one statement to have been logged"

    offenders = []
    for statement in query_log:
        normalized = statement.strip()
        upper = normalized.upper()
        if upper.startswith("SELECT"):
            continue
        if any(upper.startswith(prefix) for prefix in ALLOWED_NON_SELECT_PREFIXES):
            continue
        offenders.append(statement)

    assert offenders == [], f"non-SELECT, non-SET statements sent: {offenders}"
