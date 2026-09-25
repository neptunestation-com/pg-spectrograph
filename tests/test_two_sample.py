"""Tests for pgspec.two_sample (Milestone 9), written before the
implementation. Pure computation over synthetic sample dicts: no database
needed.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from pgspec.two_sample import (
    compute_activity_rates,
    compute_indexes_rates,
    compute_workload_rates,
    rate_dict,
    rate_list_by_key,
    rate_value,
)


def test_rate_value_computes_per_second_rate():
    assert rate_value(100, 200, 10.0) == pytest.approx(10.0)


def test_rate_value_handles_decimal_like_psycopg_wal_bytes():
    # psycopg decodes Postgres `numeric` columns (wal_bytes, most notably)
    # as Decimal, never float or str.
    assert rate_value(Decimal("1000"), Decimal("3000"), 10.0) == pytest.approx(200.0)


def test_rate_value_detects_reset_as_none():
    # b < a: a counter reset happened mid-interval.
    assert rate_value(500, 100, 10.0) is None


def test_rate_value_passes_through_non_numeric():
    assert rate_value("public", "public", 10.0) == "public"
    assert rate_value(None, "x", 10.0) == "x"


def test_rate_value_bool_is_not_treated_as_numeric():
    assert rate_value(True, True, 10.0) is True


def test_rate_dict_rates_numeric_keys_and_passes_through_others():
    a = {"datname": "db", "xact_commit": 100, "xact_rollback": 5}
    b = {"datname": "db", "xact_commit": 150, "xact_rollback": 5}
    result = rate_dict(a, b, 10.0)
    assert result["datname"] == "db"
    assert result["xact_commit"] == pytest.approx(5.0)
    assert result["xact_rollback"] == pytest.approx(0.0)


def test_rate_dict_none_inputs_pass_through_b():
    assert rate_dict(None, {"x": 1}, 10.0) == {"x": 1}
    assert rate_dict({"x": 1}, None, 10.0) is None


def test_rate_list_by_key_matches_by_field_name():
    a = [{"table_pseudonym": "t_0000", "seq_scan": 10}, {"table_pseudonym": "t_0001", "seq_scan": 5}]
    b = [{"table_pseudonym": "t_0000", "seq_scan": 20}, {"table_pseudonym": "t_0001", "seq_scan": 5}]
    rated, only_a, only_b = rate_list_by_key(a, b, "table_pseudonym", 10.0)
    assert only_a == []
    assert only_b == []
    by_pseudonym = {r["table_pseudonym"]: r for r in rated}
    assert by_pseudonym["t_0000"]["seq_scan"] == pytest.approx(1.0)
    assert by_pseudonym["t_0001"]["seq_scan"] == pytest.approx(0.0)


def test_rate_list_by_key_detects_new_and_missing_rows():
    a = [{"queryid": 1, "calls": 10}, {"queryid": 2, "calls": 5}]
    b = [{"queryid": 2, "calls": 8}, {"queryid": 3, "calls": 1}]
    rated, only_a, only_b = rate_list_by_key(a, b, "queryid", 10.0)
    assert only_a == ["1"]
    assert only_b == ["3"]
    assert len(rated) == 2


def test_rate_list_by_key_composite_callable_key():
    a = [{"backend_type": "client backend", "object": "relation", "context": "normal", "reads": 10}]
    b = [{"backend_type": "client backend", "object": "relation", "context": "normal", "reads": 30}]
    rated, only_a, only_b = rate_list_by_key(
        a, b, key=lambda r: (r["backend_type"], r["object"], r["context"]), interval_s=10.0
    )
    assert rated[0]["reads"] == pytest.approx(2.0)


def test_compute_activity_rates_shape():
    sample_a = {
        "database": {"xact_commit": 100},
        "bgwriter": {"buffers_clean": 10},
        "checkpointer": None,
        "wal": {"wal_bytes": Decimal("1000")},
        "database_conflicts": None,
        "io": [{"backend_type": "client backend", "object": "relation", "context": "normal", "reads": 5}],
        "user_tables": [{"table_pseudonym": "t_0000", "seq_scan": 1}],
        "user_functions": [{"pseudonym": "fn_0000", "calls": 1}],
    }
    sample_b = {
        "database": {"xact_commit": 200},
        "bgwriter": {"buffers_clean": 30},
        "checkpointer": None,
        "wal": {"wal_bytes": Decimal("3000")},
        "database_conflicts": None,
        "io": [{"backend_type": "client backend", "object": "relation", "context": "normal", "reads": 15}],
        "user_tables": [{"table_pseudonym": "t_0000", "seq_scan": 3}],
        "user_functions": [{"pseudonym": "fn_0000", "calls": 5}],
    }
    rates = compute_activity_rates(sample_a, sample_b, 10.0)
    assert rates["database"]["xact_commit"] == pytest.approx(10.0)
    assert rates["bgwriter"]["buffers_clean"] == pytest.approx(2.0)
    assert rates["wal"]["wal_bytes"] == pytest.approx(200.0)
    assert rates["io"][0]["reads"] == pytest.approx(1.0)
    assert rates["user_tables"][0]["seq_scan"] == pytest.approx(0.2)
    assert rates["user_functions"][0]["calls"] == pytest.approx(0.4)


def test_compute_indexes_rates_preserves_unused_list_from_b():
    sample_a = {"indexes": [{"pseudonym": "i_0000", "idx_scan": 0}], "unused_index_pseudonyms": ["i_0000"]}
    sample_b = {"indexes": [{"pseudonym": "i_0000", "idx_scan": 4}], "unused_index_pseudonyms": []}
    rates = compute_indexes_rates(sample_a, sample_b, 10.0)
    assert rates["indexes"][0]["idx_scan"] == pytest.approx(0.4)
    assert rates["unused_index_pseudonyms"] == []


def test_compute_workload_rates_detects_eviction_churn():
    sample_a = {"statements": [{"queryid": 1, "calls": 10}, {"queryid": 2, "calls": 5}]}
    sample_b = {"statements": [{"queryid": 2, "calls": 15}, {"queryid": 3, "calls": 2}]}
    rates = compute_workload_rates(sample_a, sample_b, 10.0)
    assert rates["evicted_queryids"] == ["1"]
    assert rates["new_queryids"] == ["3"]
    by_queryid = {r["queryid"]: r for r in rates["statements"]}
    assert by_queryid[2]["calls"] == pytest.approx(1.0)
