"""Integration tests for the schema section (§6.2) against the canary container."""

from __future__ import annotations

from pgspec.capture import connect
from pgspec.sections.schema import capture_schema

from canary_tokens import CANARY_IDENTIFIERS, find_canary_tokens

TEST_SALT = b"s" * 32


def _table_by_pseudonym(section: dict, pseudonym: str) -> dict:
    for table in section["tables"]:
        if table["pseudonym"] == pseudonym:
            return table
    raise AssertionError(f"no table with pseudonym {pseudonym}")


def test_capture_schema_finds_all_canary_tables(pg16_dsn):
    conn = connect(pg16_dsn)
    try:
        result = capture_schema(conn, TEST_SALT)
    finally:
        conn.close()

    real_names = {
        name
        for name in result.identifier_map
        if name.startswith("public.") and name.count(".") == 1
    }
    assert real_names == {
        "public.xq_secret_salaries",
        "public.xq_customers",
        "public.xq_orders",
        "public.xq_events",
        "public.xq_events_2026_01",
        "public.xq_events_2026_02",
    }
    assert len(result.section["tables"]) == 6


def test_capture_schema_fk_graph_resolves_correctly(pg16_dsn):
    conn = connect(pg16_dsn)
    try:
        result = capture_schema(conn, TEST_SALT)
    finally:
        conn.close()

    orders_pseudonym = result.identifier_map["public.xq_orders"]
    customers_pseudonym = result.identifier_map["public.xq_customers"]
    customer_id_pseudonym = result.identifier_map["public.xq_orders.customer_id"]
    id_pseudonym = result.identifier_map["public.xq_customers.id"]

    assert result.section["fk_graph"] == [
        {
            "from": orders_pseudonym,
            "from_cols": [customer_id_pseudonym],
            "to": customers_pseudonym,
            "to_cols": [id_pseudonym],
        }
    ]


def test_capture_schema_partitioning_shape(pg16_dsn):
    conn = connect(pg16_dsn)
    try:
        result = capture_schema(conn, TEST_SALT)
    finally:
        conn.close()

    events_pseudonym = result.identifier_map["public.xq_events"]
    events_table = _table_by_pseudonym(result.section, events_pseudonym)

    partitioning = events_table["partitioning"]
    assert partitioning["strategy"] == "r"
    assert partitioning["partition_count"] == 2
    assert partitioning["default_partition_exists"] is False
    assert partitioning["min_partition_bytes"] <= partitioning["max_partition_bytes"]
    assert partitioning["key_column_pseudonyms"] == [
        result.identifier_map["public.xq_events.event_at"]
    ]

    # Leaf partitions are captured as their own table entries too (§11.1).
    partition_01 = result.identifier_map["public.xq_events_2026_01"]
    partition_02 = result.identifier_map["public.xq_events_2026_02"]
    leaf_pseudonyms = {t["pseudonym"] for t in result.section["tables"]}
    assert {partition_01, partition_02} <= leaf_pseudonyms


def test_capture_schema_columns_carry_expected_facts(pg16_dsn):
    conn = connect(pg16_dsn)
    try:
        result = capture_schema(conn, TEST_SALT)
    finally:
        conn.close()

    orders_pseudonym = result.identifier_map["public.xq_orders"]
    orders_table = _table_by_pseudonym(result.section, orders_pseudonym)
    customer_id_pseudonym = result.identifier_map["public.xq_orders.customer_id"]

    customer_id_col = next(
        c for c in orders_table["columns"] if c["pseudonym"] == customer_id_pseudonym
    )
    assert customer_id_col["type_name"] == "integer"
    assert customer_id_col["attnotnull"] is True
    assert customer_id_col["ordinal_position"] == 2


def test_capture_schema_contains_no_canary_identifiers(pg16_dsn):
    conn = connect(pg16_dsn)
    try:
        result = capture_schema(conn, TEST_SALT)
    finally:
        conn.close()

    hits = find_canary_tokens(result.section, CANARY_IDENTIFIERS)
    assert hits == [], f"canary identifier(s) leaked into schema section: {hits}"


def test_capture_schema_identifier_map_is_deterministic_per_salt(pg16_dsn):
    conn = connect(pg16_dsn)
    try:
        first = capture_schema(conn, TEST_SALT)
        second = capture_schema(conn, TEST_SALT)
    finally:
        conn.close()

    assert first.identifier_map == second.identifier_map
    assert first.section == second.section
