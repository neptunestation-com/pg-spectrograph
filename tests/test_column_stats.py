"""Integration tests for the column_stats section (§6.3) against the canary
container."""

from __future__ import annotations

from pgspec.capture import connect
from pgspec.sections.column_stats import capture_column_stats
from pgspec.sections.schema import capture_schema

from canary_tokens import CANARY_IDENTIFIERS, CANARY_LITERALS, find_canary_tokens

TEST_SALT = b"s" * 32


def _column_by_pseudonym(columns: list[dict], pseudonym: str) -> dict:
    for col in columns:
        if col["pseudonym"] == pseudonym:
            return col
    raise AssertionError(f"no column with pseudonym {pseudonym}")


def _capture(pg16_dsn):
    conn = connect(pg16_dsn)
    try:
        schema_result = capture_schema(conn, TEST_SALT)
        column_stats = capture_column_stats(conn, schema_result.identifier_map)
    finally:
        conn.close()
    return schema_result, column_stats


def test_column_stats_uses_schema_pseudonyms(pg16_dsn):
    schema_result, column_stats = _capture(pg16_dsn)

    customer_id_pseudonym = schema_result.identifier_map["public.xq_orders.customer_id"]
    col = _column_by_pseudonym(column_stats["columns"], customer_id_pseudonym)
    assert col["table_pseudonym"] == schema_result.identifier_map["public.xq_orders"]


def test_column_stats_projects_histogram_for_numeric_column(pg16_dsn):
    schema_result, column_stats = _capture(pg16_dsn)

    id_pseudonym = schema_result.identifier_map["public.xq_customers.id"]
    col = _column_by_pseudonym(column_stats["columns"], id_pseudonym)

    assert col["histogram"] is not None
    assert col["histogram"]["kind"] == "quantile_shape"
    assert col["histogram"]["positions"][0] == 0.0
    assert col["histogram"]["positions"][-1] == 1.0
    assert col["histogram"]["span"]["type_class"] == "numeric"
    assert col["text_width"] is None


def test_column_stats_projects_histogram_for_temporal_column(pg16_dsn):
    schema_result, column_stats = _capture(pg16_dsn)

    event_at_pseudonym = schema_result.identifier_map["public.xq_events.event_at"]
    col = _column_by_pseudonym(column_stats["columns"], event_at_pseudonym)

    assert col["histogram"] is not None
    assert col["histogram"]["span"]["type_class"] == "temporal"
    assert col["histogram"]["span"]["magnitude_s"] > 0


def test_column_stats_uses_text_width_for_text_column(pg16_dsn):
    schema_result, column_stats = _capture(pg16_dsn)

    name_pseudonym = schema_result.identifier_map["public.xq_customers.customer_name"]
    col = _column_by_pseudonym(column_stats["columns"], name_pseudonym)

    assert col["histogram"] is None
    assert col["text_width"] is not None
    assert col["text_width"]["avg_width"] is not None


def test_column_stats_low_cardinality_column_has_mcv_and_skew(pg16_dsn):
    schema_result, column_stats = _capture(pg16_dsn)

    signup_pseudonym = schema_result.identifier_map["public.xq_customers.signup_amount"]
    col = _column_by_pseudonym(column_stats["columns"], signup_pseudonym)

    assert col["most_common_freqs"] is not None
    assert len(col["most_common_freqs"]) == 50
    assert col["skew_gini"] is not None
    assert 0.0 <= col["skew_gini"] <= 1.0


def test_column_stats_suppresses_tiny_table_mcv(pg16_dsn):
    schema_result, column_stats = _capture(pg16_dsn)

    # xq_secret_salaries has only 3 rows: below the small-table floor, so its
    # MCV list must be dropped entirely (issue #2 finding 1), not projected.
    salary_pseudonym = schema_result.identifier_map[
        "public.xq_secret_salaries.salary_dollars"
    ]
    col = _column_by_pseudonym(column_stats["columns"], salary_pseudonym)
    assert col["most_common_freqs"] is None


def test_extended_stats_defined_and_pseudonymized(pg16_dsn):
    schema_result, column_stats = _capture(pg16_dsn)

    extended = column_stats["extended_stats"]
    assert extended["defined"] == 1
    entry = extended["entries"][0]
    assert entry["table_pseudonym"] == schema_result.identifier_map["public.xq_orders"]
    assert set(entry["column_pseudonyms"]) == {
        schema_result.identifier_map["public.xq_orders.customer_id"],
        schema_result.identifier_map["public.xq_orders.order_amount"],
    }
    assert set(entry["kinds"]) <= {"d", "f", "m", "e"}


def test_column_stats_completeness_reflects_partial_coverage(pg16_dsn):
    _schema_result, column_stats = _capture(pg16_dsn)

    completeness = column_stats["completeness"]
    assert completeness["available"] is True
    assert (
        completeness["coverage"]["columns_with_stats"]
        <= completeness["coverage"]["columns_total"]
    )
    assert completeness["staleness"]["oldest_analyze_age_s"] >= 0


def test_column_stats_contains_no_canary_literals_or_identifiers(pg16_dsn):
    _schema_result, column_stats = _capture(pg16_dsn)

    literal_hits = find_canary_tokens(column_stats, CANARY_LITERALS)
    assert literal_hits == [], f"canary literal(s) leaked: {literal_hits}"

    identifier_hits = find_canary_tokens(column_stats, CANARY_IDENTIFIERS)
    assert identifier_hits == [], f"canary identifier(s) leaked: {identifier_hits}"


def test_column_stats_never_selects_most_common_vals_at_runtime(pg16_dsn):
    """Runtime companion to the static test_no_forbidden_sql.py check: even
    with the module free of the literal string, confirm no captured column
    entry carries an actual value list under any key."""
    _schema_result, column_stats = _capture(pg16_dsn)
    for col in column_stats["columns"]:
        assert "most_common_vals" not in col
