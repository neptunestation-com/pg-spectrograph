"""Integration tests for the activity section (§6.6) against the canary container."""

from __future__ import annotations

from pgspec.capture import connect
from pgspec.sections.activity import capture_activity
from pgspec.sections.schema import capture_schema

from canary_tokens import CANARY_IDENTIFIERS, CANARY_LITERALS, find_canary_tokens

TEST_SALT = b"s" * 32


def _capture(pg16_dsn):
    conn = connect(pg16_dsn)
    try:
        schema_result = capture_schema(conn, TEST_SALT)
        activity = capture_activity(conn, schema_result.identifier_map, TEST_SALT)
    finally:
        conn.close()
    return schema_result, activity


def test_activity_database_row_present_for_current_database(pg16_dsn):
    _schema_result, activity = _capture(pg16_dsn)
    assert activity["database"] is not None
    assert activity["database"]["datname"] == "pgspec_test"
    assert activity["database"]["xact_commit"] > 0


def test_activity_checkpointer_fails_soft_on_pg16(pg16_dsn):
    _schema_result, activity = _capture(pg16_dsn)
    # pg_stat_checkpointer is PG17+; on PG16 this must be None, not a crash.
    assert activity["checkpointer"] is None
    assert activity["bgwriter"] is not None
    assert "checkpoints_timed" in activity["bgwriter"]


def test_activity_io_matrix_captured(pg16_dsn):
    _schema_result, activity = _capture(pg16_dsn)
    assert activity["io"] is not None
    assert len(activity["io"]) > 0
    assert {"backend_type", "object", "context", "reads", "writes"} <= set(
        activity["io"][0]
    )


def test_activity_user_tables_pseudonymized(pg16_dsn):
    schema_result, activity = _capture(pg16_dsn)
    orders_pseudonym = schema_result.identifier_map["public.xq_orders"]
    orders_row = next(
        t for t in activity["user_tables"] if t["table_pseudonym"] == orders_pseudonym
    )
    assert orders_row["n_live_tup"] >= 0
    assert "relname" not in orders_row
    assert "schemaname" not in orders_row


def test_activity_completeness_reflects_capability_gaps(pg16_dsn):
    _schema_result, activity = _capture(pg16_dsn)
    completeness = activity["completeness"]
    assert completeness["available"] is True
    assert completeness["coverage"]["checkpointer_available"] is False
    assert completeness["coverage"]["io_available"] is True
    assert completeness["coverage"]["tables_captured"] == 6


def test_activity_contains_no_canary_tokens(pg16_dsn):
    _schema_result, activity = _capture(pg16_dsn)
    literal_hits = find_canary_tokens(activity, CANARY_LITERALS)
    assert literal_hits == [], f"canary literal(s) leaked: {literal_hits}"
    identifier_hits = find_canary_tokens(activity, CANARY_IDENTIFIERS)
    assert identifier_hits == [], f"canary identifier(s) leaked: {identifier_hits}"
