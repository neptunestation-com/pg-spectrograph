"""Integration tests for the workload section (§6.5) against the canary
container. Each test resets pg_stat_statements and seeds its own known
queries first, since the extension's state is process-global and would
otherwise accumulate cross-test noise (including pgspec's own catalog
queries from other sections' tests).

Seeding uses a plain, separate, writable connection: pgspec.capture.connect()
is deliberately read-only (I3) and cannot run the seed statements itself.
"""

from __future__ import annotations

import math

import psycopg
import pytest

from pgspec.capture import connect
from pgspec.sections.activity import capture_activity
from pgspec.sections.column_stats import capture_column_stats
from pgspec.sections.schema import capture_schema
from pgspec.sections.workload import (
    analyze_statement,
    capture_workload,
    classify_first_token,
    representativity,
)

from canary_tokens import CANARY_IDENTIFIERS, CANARY_LITERALS, find_canary_tokens

TEST_SALT = b"s" * 32


def test_classify_first_token_handles_whitespace_and_ctes():
    assert classify_first_token("\n   SELECT 1") == "SELECT"
    assert classify_first_token("with x as (select 1) select * from x") == "WITH"
    assert classify_first_token("INSERT INTO t VALUES ($1)") == "INSERT"
    assert classify_first_token("SET search_path = public") == "OTHER"
    assert classify_first_token(None) == "OTHER"


def test_analyze_statement_extracts_predicates_with_catalog_selectivity():
    identifier_map = {"public.t": "t_0001", "public.t.c": "c_0001", "public.t.d": "c_0002"}
    columns_by_table = {"public.t": {"c": "c_0001", "d": "c_0002"}}
    column_stats = {
        "c_0001": {
            "n_distinct": 100,
            "null_frac": 0.1,
            "most_common_freqs": [0.5, 0.25],
            "reltuples": 1000,
        }
    }
    result = analyze_statement(
        "SELECT c FROM public.t WHERE c = $1 AND d > $2 AND c IN ($3, $4) "
        "AND d IS NULL AND c + 1 = $5 AND c LIKE $6",
        identifier_map,
        columns_by_table,
        column_stats=column_stats,
    )
    by_op = {(p["column"], p["op"]): p for p in result["predicates"]}

    equality = by_op[("c_0001", "=")]
    assert equality["parameterized"] is True
    assert equality["selectivity_est"]["kind"] == "equality"
    assert equality["selectivity_est"]["uniform"] == pytest.approx(0.01)
    # sum of squared MCV frequencies plus the uniform tail's share.
    assert equality["selectivity_est"]["mcv_weighted"] == pytest.approx(
        0.5**2 + 0.25**2 + 0.25**2 / 98
    )
    assert by_op[("c_0001", "IN")]["selectivity_est"]["kind"] == "equality"
    assert by_op[("c_0002", ">")]["selectivity_est"] == {
        "kind": "unavailable",
        "reason": "needs parameter values",
    }
    assert by_op[("c_0001", "LIKE")]["selectivity_est"]["kind"] == "unavailable"
    assert by_op[("c_0002", "IS NULL")]["selectivity_est"] == {
        "kind": "unavailable",
        "reason": "no column statistics",
    }
    # Arithmetic on a column is not a predicate on it.
    assert not any(p["op"] == "+" for p in result["predicates"])
    assert ("c_0001", "+") not in by_op


def test_analyze_statement_predicates_skip_unresolvable_columns():
    result = analyze_statement(
        "SELECT relname FROM pg_class WHERE oid = $1 AND relkind = $2",
        {},
        {},
        column_stats={},
    )
    assert result["predicates"] == []


def test_representativity_compares_population_and_captured_distributions():
    population = {
        "statements": 1000,
        "quantiles": {
            "mean_exec_time": {"p50": 0.1, "p90": 5.0, "p99": 30.0},
            "calls": {"p50": 1.0, "p90": 2.0, "p99": 9.0},
            "rows_per_call": {"p50": 7.0, "p90": 1000.0, "p99": 100000.0},
            "blks_per_call": {"p50": 33.0, "p90": 1600.0, "p99": 1700.0},
        },
        "verb_mix": {
            "SELECT": {"statements": 990, "calls": 1980, "exec_time": 990.0},
            "INSERT": {"statements": 10, "calls": 20, "exec_time": 10.0},
        },
    }
    captured = [
        {"text": "SELECT 1", "verb": "SELECT", "mean_exec_time": 10.0, "calls": 4,
         "rows": 40, "shared_blks_hit": 1600, "shared_blks_read": 0, "total_exec_time": 40.0,
         "sampled_tail": False},
        {"text": "SELECT 2", "verb": "SELECT", "mean_exec_time": 10.0, "calls": 4,
         "rows": 40, "shared_blks_hit": 1700, "shared_blks_read": 0, "total_exec_time": 40.0,
         "sampled_tail": False},
        {"text": "SELECT 3", "verb": "SELECT", "mean_exec_time": 0.1, "calls": 1,
         "rows": 7, "shared_blks_hit": 33, "shared_blks_read": 0, "total_exec_time": 0.1,
         "sampled_tail": True},
    ]
    result = representativity(captured, population)

    assert result["population"]["statements"] == 1000
    assert result["captured"]["statements"] == 2
    assert result["tail_sample"]["statements"] == 1
    assert result["captured"]["quantiles"]["mean_exec_time"]["p50"] == pytest.approx(10.0)
    assert result["median_log10_ratio"]["mean_exec_time"] == pytest.approx(math.log10(100))
    assert result["median_log10_ratio"]["calls"] == pytest.approx(math.log10(4))
    # Population is 1% INSERT by calls; the captured set is all SELECT, so
    # the divergence is small but strictly positive.
    assert 0 < result["verb_mix_kl_bits"]["calls"] < 0.1
    assert result["population"]["verb_mix"]["INSERT"]["calls_share"] == pytest.approx(0.01)


def test_representativity_without_population_is_none():
    assert representativity([], None) is None


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


def _capture_with_stats(pg16_dsn: str, **kwargs) -> tuple[dict, dict]:
    conn = connect(pg16_dsn)
    try:
        schema_result = capture_schema(conn, TEST_SALT)
        column_stats = capture_column_stats(conn, schema_result.identifier_map)
        result = capture_workload(
            conn,
            schema_result.identifier_map,
            schema_section=schema_result.section,
            column_stats_section=column_stats,
            function_map=schema_result.function_map,
            **kwargs,
        )
    finally:
        conn.close()
    return schema_result.identifier_map, result


def test_workload_pseudonymizes_user_function_names_in_query_text(pg16_dsn):
    _seed(
        pg16_dsn,
        [
            ("SELECT public.xq_calculate_bonus(%s)", (100,)),
            (
                "SELECT xq_calculate_bonus(signup_amount) FROM public.xq_customers WHERE id = %s",
                (1,),
            ),
        ],
    )
    conn = connect(pg16_dsn)
    try:
        schema_result = capture_schema(conn, TEST_SALT)
        result = capture_workload(
            conn,
            schema_result.identifier_map,
            top_k=50,
            tail_sample=0,
            function_map=schema_result.function_map,
        )
        activity = capture_activity(
            conn, schema_result.identifier_map, TEST_SALT, function_map=schema_result.function_map
        )
    finally:
        conn.close()

    fn_pseudonym = schema_result.function_map["public.xq_calculate_bonus"]
    calls = [s for s in result["statements"] if s["text"] and fn_pseudonym in s["text"]]
    assert len(calls) == 2
    assert find_canary_tokens(result, CANARY_IDENTIFIERS) == []
    # activity's per-function counters draw from the same pseudonym universe,
    # so a call in the workload and a row in user_functions agree.
    assert any(f["pseudonym"] == fn_pseudonym for f in activity.section["user_functions"])


_EIGHT_SHAPES = [
    ("SELECT count(*) FROM public.xq_orders WHERE customer_id = %s", (1,)),
    ("SELECT id FROM public.xq_orders WHERE order_amount > %s", (5,)),
    ("SELECT customer_id FROM public.xq_orders WHERE id = %s", (1,)),
    ("SELECT max(order_amount) FROM public.xq_orders", ()),
    ("SELECT id FROM public.xq_customers WHERE id = %s", (1,)),
    ("SELECT customer_name FROM public.xq_customers WHERE signup_amount > %s", (1,)),
    ("SELECT count(*) FROM public.xq_customers", ()),
    ("SELECT event_type FROM public.xq_events WHERE id = %s", (1,)),
]


def test_workload_tail_sample_marks_random_statements_outside_top_k(pg16_dsn):
    _seed(pg16_dsn, _EIGHT_SHAPES)
    _map, result = _capture_with_stats(pg16_dsn, top_k=1, tail_sample=3)

    systematic = [s for s in result["statements"] if not s["sampled_tail"]]
    tail = [s for s in result["statements"] if s["sampled_tail"]]
    assert 1 <= len(systematic) <= 4
    assert len(tail) == 3
    assert result["coverage"]["tail_sampled"] == 3
    assert result["coverage"]["statements_captured"] == len(systematic)
    assert not ({s["queryid"] for s in tail} & {s["queryid"] for s in systematic})
    # Tail rows go through the same pseudonymization as everything else.
    assert find_canary_tokens(tail, CANARY_LITERALS + CANARY_IDENTIFIERS) == []


def test_workload_tail_sample_zero_disables_the_lens(pg16_dsn):
    _seed(pg16_dsn, _EIGHT_SHAPES)
    _map, result = _capture_with_stats(pg16_dsn, top_k=1, tail_sample=0)
    assert not any(s["sampled_tail"] for s in result["statements"])
    assert result["coverage"]["tail_sampled"] == 0


def test_workload_representativity_is_measured_against_the_population(pg16_dsn):
    _seed(pg16_dsn, _EIGHT_SHAPES)
    _map, result = _capture_with_stats(pg16_dsn, top_k=1, tail_sample=2)
    rep = result["representativity"]

    assert rep["population"]["statements"] > rep["captured"]["statements"]
    for side in ("population", "captured", "tail_sample"):
        assert set(rep[side]["quantiles"]) == {
            "mean_exec_time", "calls", "rows_per_call", "blks_per_call",
        }
        assert set(rep[side]["quantiles"]["calls"]) == {"p50", "p90", "p99"}
    assert set(rep["median_log10_ratio"]) == {
        "mean_exec_time", "calls", "rows_per_call", "blks_per_call",
    }
    assert rep["verb_mix_kl_bits"]["calls"] >= 0
    assert "SELECT" in rep["population"]["verb_mix"]


def test_workload_predicates_carry_catalog_selectivity(pg16_dsn):
    _seed(
        pg16_dsn,
        [
            ("SELECT count(*) FROM public.xq_orders WHERE customer_id = %s", (7,)),
            ("SELECT id FROM public.xq_orders WHERE order_amount > %s", (5,)),
            ("SELECT count(*) FROM public.xq_orders WHERE customer_id IN (%s, %s)", (1, 2)),
            ("SELECT count(*) FROM public.xq_orders WHERE order_amount IS NULL", ()),
            ("SELECT * FROM public.xq_orders WHERE customer_id = %s", (3,)),
        ],
    )
    identifier_map, result = _capture_with_stats(pg16_dsn, top_k=50, tail_sample=0)
    customer_id = identifier_map["public.xq_orders.customer_id"]
    order_amount = identifier_map["public.xq_orders.order_amount"]
    assert result["parameter_distributions"]["status"] == "catalog_derived"

    equality = next(
        s
        for s in result["statements"]
        if s["text"] and "count(*)" in s["text"] and f"{customer_id} = $1" in s["text"]
    )
    preds = {(p["column"], p["op"]): p for p in equality["predicates"]}
    # 2000 orders over 500 customers: 1/500 either way, since the fixture's
    # customer_id distribution is uniform.
    est = preds[(customer_id, "=")]["selectivity_est"]
    assert est["kind"] == "equality"
    assert est["uniform"] == pytest.approx(1 / 500, rel=0.05)
    assert est["mcv_weighted"] == pytest.approx(1 / 500, rel=0.2)
    assert preds[(customer_id, "=")]["parameterized"] is True
    assert equality["rows_per_call"] == 1.0

    ranged = _statement_containing(result["statements"], f"{order_amount} > $1")
    assert ranged["predicates"][0]["selectivity_est"]["kind"] == "unavailable"

    in_list = _statement_containing(result["statements"], f"{customer_id} IN (")
    assert in_list["predicates"][0]["op"] == "IN"
    assert in_list["predicates"][0]["selectivity_est"]["kind"] == "equality"

    null_test = _statement_containing(result["statements"], f"{order_amount} IS NULL")
    assert null_test["predicates"][0]["selectivity_est"] == {"kind": "null", "value": 0.0}

    star = _statement_containing(result["statements"], f"SELECT * FROM t_")
    # 4 rows per customer out of 2000: output selectivity 1/500.
    assert star["rows_per_call"] == pytest.approx(4.0)
    assert star["output_selectivity"] == pytest.approx(1 / 500, rel=0.05)


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
    assert result["parameter_distributions"]["status"] == "catalog_derived"


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
