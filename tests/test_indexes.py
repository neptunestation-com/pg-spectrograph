"""Integration tests for the indexes section (§6.4) against the canary container."""

from __future__ import annotations

from pgspec.capture import connect
from pgspec.sections.indexes import capture_indexes
from pgspec.sections.schema import capture_schema

from canary_tokens import CANARY_IDENTIFIERS, CANARY_LITERALS, find_canary_tokens

TEST_SALT = b"s" * 32


def _index_by_pseudonym(indexes: list[dict], pseudonym: str) -> dict:
    for idx in indexes:
        if idx["pseudonym"] == pseudonym:
            return idx
    raise AssertionError(f"no index with pseudonym {pseudonym}")


def _capture(pg16_dsn):
    conn = connect(pg16_dsn)
    try:
        schema_result = capture_schema(conn, TEST_SALT)
        indexes_section = capture_indexes(conn, schema_result.identifier_map, TEST_SALT).section
    finally:
        conn.close()
    return schema_result, indexes_section


def test_capture_indexes_finds_all_fixture_indexes(pg16_dsn):
    _schema_result, section = _capture(pg16_dsn)
    assert len(section["indexes"]) == 8


def test_multi_column_index_preserves_order_and_opclasses(pg16_dsn):
    schema_result, section = _capture(pg16_dsn)
    orders_pseudonym = schema_result.identifier_map["public.xq_orders"]
    multi = next(
        idx
        for idx in section["indexes"]
        if idx["table_pseudonym"] == orders_pseudonym and len(idx["key_column_pseudonyms"]) == 2
    )
    assert multi["key_column_pseudonyms"] == [
        schema_result.identifier_map["public.xq_orders.customer_id"],
        schema_result.identifier_map["public.xq_orders.order_amount"],
    ]
    assert multi["opclasses"] == ["int4_ops", "numeric_ops"]
    assert multi["is_unique"] is False
    assert multi["is_primary"] is False


def test_expression_index_reduces_to_referenced_column_only(pg16_dsn):
    schema_result, section = _capture(pg16_dsn)
    customers_pseudonym = schema_result.identifier_map["public.xq_customers"]
    expr_idx = next(
        idx
        for idx in section["indexes"]
        if idx["table_pseudonym"] == customers_pseudonym and idx["is_expression"]
    )
    assert expr_idx["is_expression"] is True
    assert expr_idx["key_column_pseudonyms"] == [None]
    assert expr_idx["expression_columns"] == [
        schema_result.identifier_map["public.xq_customers.customer_name"]
    ]


def test_partial_index_predicate_reduces_to_column_never_literal(pg16_dsn):
    schema_result, section = _capture(pg16_dsn)
    events_pseudonym = schema_result.identifier_map["public.xq_events"]
    partial_idx = next(
        idx
        for idx in section["indexes"]
        if idx["table_pseudonym"] == events_pseudonym and idx["is_partial"]
    )
    assert partial_idx["is_partial"] is True
    assert partial_idx["predicate_columns"] == [
        schema_result.identifier_map["public.xq_events.event_type"]
    ]

    literal_hits = find_canary_tokens(section, ["canary_checkout"])
    assert literal_hits == [], "partial-index predicate literal leaked into section"


def test_unused_indexes_are_flagged(pg16_dsn):
    # Deliberately does not assert that any *specific* real index remains
    # unused: this container is long-lived across the whole test session
    # (and ad hoc manual verification runs before it), so idx_scan on any
    # given index is whatever cumulative activity has touched it by the
    # time this test happens to run. What's actually under test is the
    # flagging mechanism itself: idx_scan == 0 iff its pseudonym is listed.
    _schema_result, section = _capture(pg16_dsn)
    unused = set(section["unused_index_pseudonyms"])
    for idx in section["indexes"]:
        assert (idx["idx_scan"] == 0) == (idx["pseudonym"] in unused)


def test_indexes_section_contains_no_canary_tokens(pg16_dsn):
    _schema_result, section = _capture(pg16_dsn)
    literal_hits = find_canary_tokens(section, CANARY_LITERALS)
    assert literal_hits == [], f"canary literal(s) leaked: {literal_hits}"
    identifier_hits = find_canary_tokens(section, CANARY_IDENTIFIERS)
    assert identifier_hits == [], f"canary identifier(s) leaked: {identifier_hits}"


def test_capture_indexes_returns_its_own_identifier_map(pg16_dsn):
    conn = connect(pg16_dsn)
    try:
        schema_result = capture_schema(conn, TEST_SALT)
        result = capture_indexes(conn, schema_result.identifier_map, TEST_SALT)
    finally:
        conn.close()

    assert set(result.identifier_map.values()) == {
        idx["pseudonym"] for idx in result.section["indexes"]
    }
    assert all(name.startswith("public.") for name in result.identifier_map)
