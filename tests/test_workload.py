"""Integration tests for the workload section (§6.5) against the canary
container. Each test resets pg_stat_statements and seeds its own known
queries first, since the extension's state is process-global and would
otherwise accumulate cross-test noise (including pgspec's own catalog
queries from other sections' tests).

Seeding uses a plain, separate, writable connection: pgspec.capture.connect()
is deliberately read-only (I3) and cannot run the seed statements itself.
"""

from __future__ import annotations

import psycopg

from pgspec.capture import connect
from pgspec.sections.schema import capture_schema
from pgspec.sections.workload import capture_workload

from canary_tokens import CANARY_IDENTIFIERS, CANARY_LITERALS, find_canary_tokens

TEST_SALT = b"s" * 32


def _seed(pg16_dsn: str, queries: list[tuple[str, tuple]]):
    with psycopg.connect(pg16_dsn, autocommit=True) as seed_conn:
        with seed_conn.cursor() as cur:
            cur.execute("SELECT pg_stat_statements_reset()")
            for sql, params in queries:
                cur.execute(sql, params)
                try:
                    cur.fetchall()
                except Exception:
                    pass


def _statement_containing(statements, fragment: str) -> dict:
    for s in statements:
        if s["text"] and fragment in s["text"]:
            return s
    raise AssertionError(f"no statement containing {fragment!r} in {statements}")


def _statement_referencing_tables(statements, table_pseudonyms: set[str]) -> dict:
    for s in statements:
        if set(s["referenced_table_pseudonyms"]) == table_pseudonyms:
            return s
    raise AssertionError(
        f"no statement referencing exactly {table_pseudonyms} in {statements}"
    )


def test_workload_captures_join_query_with_disambiguated_columns(pg16_dsn):
    _seed(
        pg16_dsn,
        [
            (
                "SELECT o.id, c.id FROM public.xq_orders o "
                "JOIN public.xq_customers c ON c.id = o.customer_id "
                "WHERE o.id = %s",
                (1,),
            ),
        ],
    )
    conn = connect(pg16_dsn)
    try:
        schema_result = capture_schema(conn, TEST_SALT)
        result = capture_workload(conn, schema_result.identifier_map, top_k=10)
    finally:
        conn.close()

    orders_pseudonym = schema_result.identifier_map["public.xq_orders"]
    customers_pseudonym = schema_result.identifier_map["public.xq_customers"]
    orders_id_pseudonym = schema_result.identifier_map["public.xq_orders.id"]
    customers_id_pseudonym = schema_result.identifier_map["public.xq_customers.id"]

    stmt = _statement_referencing_tables(
        result["statements"], {orders_pseudonym, customers_pseudonym}
    )
    assert stmt["verb"] == "SELECT"
    assert stmt["join_count"] == 1
    # The critical regression case: two joined tables both have an "id"
    # column; each occurrence must resolve to its own table's pseudonym,
    # never leak the literal name "id".
    stripped = stmt["text"].replace(orders_id_pseudonym, "").replace(
        customers_id_pseudonym, ""
    )
    assert "id" not in stripped
    assert orders_id_pseudonym in stmt["text"]
    assert customers_id_pseudonym in stmt["text"]


def test_workload_derives_aggregate_and_limit_flags(pg16_dsn):
    _seed(
        pg16_dsn,
        [
            (
                "SELECT customer_id, sum(order_amount) FROM public.xq_orders "
                "WHERE customer_id = %s GROUP BY customer_id LIMIT 10",
                (1,),
            ),
        ],
    )
    conn = connect(pg16_dsn)
    try:
        schema_result = capture_schema(conn, TEST_SALT)
        result = capture_workload(conn, schema_result.identifier_map, top_k=10)
    finally:
        conn.close()

    stmt = _statement_containing(result["statements"], "sum(")
    assert stmt["has_aggregate"] is True
    assert stmt["has_limit"] is True
    # pg_stat_statements normalizes every constant, not just bound params, to
    # a $n placeholder -- "LIMIT 10" becomes "LIMIT $2", so max_param is 2,
    # not 1 (the customer_id comparison alone).
    assert stmt["max_param"] == 2


def test_workload_derives_update_verb_with_no_aggregate(pg16_dsn):
    _seed(
        pg16_dsn,
        [
            (
                "UPDATE public.xq_customers SET signup_amount = %s WHERE id = %s",
                (5, 1),
            ),
        ],
    )
    conn = connect(pg16_dsn)
    try:
        schema_result = capture_schema(conn, TEST_SALT)
        result = capture_workload(conn, schema_result.identifier_map, top_k=10)
    finally:
        conn.close()

    stmt = next(s for s in result["statements"] if s["verb"] == "UPDATE")
    assert stmt["has_aggregate"] is False
    assert stmt["has_limit"] is False
    assert stmt["max_param"] == 2


def test_workload_top_k_union_covers_distinct_dominant_statements(pg16_dsn):
    _seed(
        pg16_dsn,
        [
            # High call count, everything else negligible.
            *[("SELECT 1", ()) for _ in range(20)],
            # Dominates wal_bytes (a real write).
            ("UPDATE public.xq_customers SET signup_amount = signup_amount", ()),
            # Dominates shared_blks_read among the cheap statements above
            # (a full sequential scan of the largest table).
            ("SELECT count(*) FROM public.xq_orders", ()),
        ],
    )
    conn = connect(pg16_dsn)
    try:
        schema_result = capture_schema(conn, TEST_SALT)
        # top_k=1 per lens: only the union (not a single global top-1)
        # could surface both the call-heavy and the write-heavy statement,
        # since neither dominates the other's lens.
        result = capture_workload(conn, schema_result.identifier_map, top_k=1)
    finally:
        conn.close()

    texts = [s["text"] for s in result["statements"]]
    assert any(t == "SELECT $1" for t in texts), texts
    assert any("UPDATE" in (t or "") for t in texts), texts
    assert len(result["statements"]) >= 2


def test_workload_coverage_fractions_stay_at_or_under_one(pg16_dsn):
    _seed(pg16_dsn, [("SELECT 1", ())])
    conn = connect(pg16_dsn)
    try:
        schema_result = capture_schema(conn, TEST_SALT)
        result = capture_workload(conn, schema_result.identifier_map, top_k=500)
    finally:
        conn.close()

    coverage = result["coverage"]
    assert coverage["statements_captured"] >= 1
    assert coverage["exec_time_fraction"] <= 1.0
    assert coverage["calls_fraction"] <= 1.0
    # wal_bytes/blks_read fractions are None, not a number, when nothing in
    # the window produced any WAL/physical reads at all (division by zero is
    # undefined, not zero) -- a plain "SELECT 1" against a warm cache is
    # exactly that case for wal_bytes.
    assert coverage["wal_bytes_fraction"] is None or coverage["wal_bytes_fraction"] <= 1.0
    assert coverage["blks_read_fraction"] is None or coverage["blks_read_fraction"] <= 1.0
    assert coverage["unparsed_fraction"] == 0.0
    assert result["parameter_distributions"] == "unavailable_in_v1"


def test_workload_contains_no_canary_literals_or_identifiers(pg16_dsn):
    _seed(
        pg16_dsn,
        [
            (
                "SELECT * FROM public.xq_secret_salaries "
                "WHERE employee_email = %s",
                ("alice.wonderland@canary-example.test",),
            ),
        ],
    )
    conn = connect(pg16_dsn)
    try:
        schema_result = capture_schema(conn, TEST_SALT)
        result = capture_workload(conn, schema_result.identifier_map, top_k=500)
    finally:
        conn.close()

    literal_hits = find_canary_tokens(result, CANARY_LITERALS)
    assert literal_hits == [], f"canary literal(s) leaked into workload section: {literal_hits}"

    identifier_hits = find_canary_tokens(result, CANARY_IDENTIFIERS)
    assert identifier_hits == [], (
        f"canary identifier(s) leaked into workload section: {identifier_hits}"
    )


def test_workload_drops_ddl_text_entirely_per_74(pg16_dsn):
    # §7.4: utility statements (DDL, here) get their text dropped entirely,
    # verb class only -- found live by the version-matrix test, where this
    # project's own fixture DDL (CREATE TABLE/CREATE INDEX) turned up in
    # pg_stat_statements with column definitions and index names left
    # completely unpseudonymized, since pg_stat_statements' literal
    # normalization only reliably covers DML, not every DDL grammar shape.
    _seed(
        pg16_dsn,
        [
            (
                "CREATE TEMP TABLE xq_ddl_canary_table "
                "(id serial, xq_ddl_canary_column text)",
                (),
            ),
        ],
    )
    conn = connect(pg16_dsn)
    try:
        schema_result = capture_schema(conn, TEST_SALT)
        result = capture_workload(conn, schema_result.identifier_map, top_k=500)
    finally:
        conn.close()

    ddl_statements = [s for s in result["statements"] if s["verb"] == "DDL"]
    assert ddl_statements, "expected at least one DDL statement to be captured"
    for stmt in ddl_statements:
        assert stmt["text"] is None
        assert stmt["unparsed"] is False

    literal_hits = find_canary_tokens(result, ["xq_ddl_canary_table", "xq_ddl_canary_column"])
    assert literal_hits == [], f"DDL identifier(s) leaked: {literal_hits}"
