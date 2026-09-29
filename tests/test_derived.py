"""Tests for pgspec.sections.derived (Milestone 7), written before the
implementation. Pure computation over already-captured section dicts: no
database needed, unlike every other section.
"""

from __future__ import annotations

import math

import pytest

from pgspec.sections.derived import (
    cache_hit_ratios,
    compute_derived,
    dead_tuple_pressure,
    fk_fanout,
    fk_graph_summary,
    hot_update_fraction,
    index_redundancy,
    index_scan_share,
    read_write_ratio,
    statement_concentration,
    statement_recurrence,
    table_size_distribution,
    temp_spill,
    wal_metrics,
)


def _activity(**overrides):
    base = {
        "database": {
            "xact_commit": 90,
            "xact_rollback": 10,
            "tup_returned": 1000,
            "tup_fetched": 500,
            "tup_inserted": 100,
            "tup_updated": 50,
            "tup_deleted": 10,
            "blks_hit": 900,
            "blks_read": 100,
            "temp_bytes": 0,
        },
        "wal": {"wal_records": 1000, "wal_fpi": 100, "wal_bytes": "500000"},
        "io": [
            {"backend_type": "client backend", "hits": 800, "reads": 200},
            {"backend_type": "autovacuum worker", "hits": 100, "reads": 0},
        ],
        "user_tables": [
            {
                "table_pseudonym": "t_0000",
                "seq_scan": 10,
                "seq_tup_read": 1000,
                "idx_scan": 90,
                "idx_tup_fetch": 9000,
                "n_tup_upd": 100,
                "n_tup_hot_upd": 60,
                "n_live_tup": 1000,
                "n_dead_tup": 50,
            },
            {
                "table_pseudonym": "t_0001",
                "seq_scan": 0,
                "seq_tup_read": 0,
                "idx_scan": 10,
                "idx_tup_fetch": 100,
                "n_tup_upd": 10,
                "n_tup_hot_upd": 0,
                "n_live_tup": 10,
                "n_dead_tup": 5,
            },
        ],
    }
    base.update(overrides)
    return base


def _workload(**overrides):
    base = {
        "statements": [
            {
                "verb": "SELECT",
                "total_exec_time": 100.0,
                "shared_blks_read": 10,
                "temp_blks_read": 0,
                "temp_blks_written": 0,
            },
            {
                "verb": "SELECT",
                "total_exec_time": 50.0,
                "shared_blks_read": 5,
                "temp_blks_read": 0,
                "temp_blks_written": 0,
            },
            {
                "verb": "UPDATE",
                "total_exec_time": 50.0,
                "shared_blks_read": 85,
                "temp_blks_read": 1,
                "temp_blks_written": 1,
            },
        ]
    }
    base.update(overrides)
    return base


def _schema(**overrides):
    base = {
        "tables": [
            {"pseudonym": "t_0000", "heap_bytes": 800, "index_bytes": 100, "toast_bytes": 0},
            {"pseudonym": "t_0001", "heap_bytes": 100, "index_bytes": 20, "toast_bytes": 0},
        ],
        "fk_graph": [{"from": "t_0000", "from_cols": ["c_0000"], "to": "t_0001", "to_cols": ["c_0001"]}],
    }
    base.update(overrides)
    return base


def _indexes(**overrides):
    base = {
        "indexes": [
            {"pseudonym": "i_0000", "index_bytes": 100, "idx_scan": 10},
            {"pseudonym": "i_0001", "index_bytes": 20, "idx_scan": 0},
        ],
        "unused_index_pseudonyms": ["i_0001"],
    }
    base.update(overrides)
    return base


def test_read_write_ratio_tup_and_statement_level():
    result = read_write_ratio(_activity(), _workload())
    assert result["tup_level"]["reads"] == 1000 + 500
    assert result["tup_level"]["writes"] == 100 + 50 + 10
    assert result["tup_level"]["ratio"] == pytest.approx(1500 / 160)
    assert result["statement_level"]["reads"] == 2
    assert result["statement_level"]["writes"] == 1
    assert result["statement_level"]["ratio"] == pytest.approx(2 / 1)


def test_read_write_ratio_handles_zero_writes():
    activity = _activity()
    activity["database"]["tup_inserted"] = 0
    activity["database"]["tup_updated"] = 0
    activity["database"]["tup_deleted"] = 0
    result = read_write_ratio(activity, _workload(statements=[]))
    assert result["tup_level"]["ratio"] is None
    assert result["statement_level"]["ratio"] is None


def test_hot_update_fraction_aggregate_and_distribution():
    result = hot_update_fraction(_activity())
    assert result["aggregate"] == pytest.approx(60 / 110)
    assert result["per_table_min"] == pytest.approx(0.0)
    assert result["per_table_max"] == pytest.approx(0.6)


def test_hot_update_fraction_skips_tables_with_no_updates():
    activity = _activity()
    activity["user_tables"][1]["n_tup_upd"] = 0
    result = hot_update_fraction(activity)
    # Only one table (t_0000) has updates; its own fraction (0.6) is both
    # the min and the max of the distribution.
    assert result["per_table_min"] == pytest.approx(0.6)
    assert result["per_table_max"] == pytest.approx(0.6)


def test_index_scan_share_weighted_by_tuples():
    result = index_scan_share(_activity())
    total_idx = 9000 + 100
    total_seq = 1000 + 0
    assert result == pytest.approx(total_idx / (total_idx + total_seq))


def test_wal_metrics_per_xact_and_fpi_fraction():
    result = wal_metrics(_activity())
    assert result["wal_bytes_per_xact"] == pytest.approx(500000 / 100)
    assert result["wal_fpi_fraction"] == pytest.approx(100 / 1000)


def test_temp_spill_rate_and_statement_fraction():
    result = temp_spill(_activity(), _workload())
    assert result["temp_bytes_per_xact"] == pytest.approx(0 / 100)
    assert result["statements_with_temp_fraction"] == pytest.approx(1 / 3)


def test_cache_hit_ratios_db_and_io_level():
    result = cache_hit_ratios(_activity())
    assert result["db_level"] == pytest.approx(900 / 1000)
    assert result["io_level"] == pytest.approx(900 / 1100)


def test_statement_concentration_entropy_and_shares_time_and_blocks():
    result = statement_concentration(_workload())
    # Three statements with exec_time shares 100/200, 50/200, 50/200.
    expected_entropy = -sum(
        p * math.log2(p) for p in (100 / 200, 50 / 200, 50 / 200)
    )
    assert result["exec_time"]["entropy_bits"] == pytest.approx(expected_entropy)
    assert result["exec_time"]["top1_share"] == pytest.approx(0.5)
    # Block-I/O share: 10/100, 5/100, 85/100 -- top1 is the UPDATE (85%),
    # not the slowest SELECT, exactly the "disagree informatively" case §6.7
    # already expects from read/write ratio's two variants.
    assert result["blks_read"]["top1_share"] == pytest.approx(0.85)


def test_statement_concentration_handles_empty_workload():
    result = statement_concentration(_workload(statements=[]))
    assert result["exec_time"]["entropy_bits"] is None
    assert result["blks_read"]["entropy_bits"] is None


def _recurrent_workload():
    # Redset's shape: one hot statement accounts for 80% of all calls.
    return _workload(
        statements=[
            {"calls": 800, "total_exec_time": 10.0, "shared_blks_read": 1},
            {"calls": 100, "total_exec_time": 100.0, "shared_blks_read": 50},
            {"calls": 100, "total_exec_time": 100.0, "shared_blks_read": 50},
        ]
    )


def test_statement_recurrence_calls_lens_point_mode():
    coverage = {"statements_captured": 3, "statements_tracked": 3, "dealloc_count": 12}
    result = statement_recurrence(_recurrent_workload(), coverage)
    expected_entropy = -sum(p * math.log2(p) for p in (0.8, 0.1, 0.1))
    assert result["calls"]["entropy_bits"] == pytest.approx(expected_entropy)
    assert result["calls"]["top1_share"] == pytest.approx(0.8)
    assert result["calls"]["top10_share"] == pytest.approx(1.0)
    assert result["dealloc_count"] == 12
    # Point mode has no second sample, so churn is unknown, not zero.
    assert result["eviction_churn_fraction"] is None


def test_statement_recurrence_eviction_churn_in_two_sample_mode():
    coverage = {
        "statements_captured": 3,
        "dealloc_count": 12,
        "evicted_queryids_count": 1,
        "new_queryids_count": 2,
    }
    result = statement_recurrence(_recurrent_workload(), coverage)
    # One of the four statements seen across both samples was evicted.
    assert result["eviction_churn_fraction"] == pytest.approx(1 / 4)


def test_statement_recurrence_handles_missing_inputs():
    result = statement_recurrence(_workload(statements=[]), None)
    assert result["calls"]["entropy_bits"] is None
    assert result["dealloc_count"] is None
    assert result["eviction_churn_fraction"] is None


def test_statement_metrics_ignore_the_sampled_tail():
    systematic = [
        {"verb": "SELECT", "calls": 800, "total_exec_time": 10.0, "shared_blks_read": 1,
         "temp_blks_read": 0, "temp_blks_written": 0},
        {"verb": "UPDATE", "calls": 200, "total_exec_time": 100.0, "shared_blks_read": 50,
         "temp_blks_read": 0, "temp_blks_written": 0},
    ]
    tail = [
        {"verb": "INSERT", "calls": 1, "total_exec_time": 0.1, "shared_blks_read": 1,
         "temp_blks_read": 5, "temp_blks_written": 5, "sampled_tail": True}
        for _ in range(10)
    ]
    with_tail = _workload(statements=systematic + tail)
    without = _workload(statements=systematic)

    assert statement_concentration(with_tail) == statement_concentration(without)
    assert statement_recurrence(with_tail, None) == statement_recurrence(without, None)
    assert temp_spill(_activity(), with_tail) == temp_spill(_activity(), without)
    assert read_write_ratio(_activity(), with_tail) == read_write_ratio(_activity(), without)


def _fanout_inputs():
    schema = {
        "tables": [
            {"pseudonym": "t_fact", "reltuples": 200000.0},
            {"pseudonym": "t_dim", "reltuples": 1000.0},
            {"pseudonym": "t_dim2", "reltuples": 10.0},
        ],
        "fk_graph": [
            {"from": "t_fact", "from_cols": ["c_cust"], "to": "t_dim", "to_cols": ["c_id"]},
            {"from": "t_fact", "from_cols": ["c_promo"], "to": "t_dim2", "to_cols": ["c_id2"]},
            {"from": "t_fact", "from_cols": ["c_a", "c_b"], "to": "t_dim", "to_cols": ["c_x", "c_y"]},
            {"from": "t_fact", "from_cols": ["c_nostats"], "to": "t_dim", "to_cols": ["c_id"]},
        ],
    }
    column_stats = {
        "columns": [
            {"pseudonym": "c_cust", "n_distinct": -0.005, "null_frac": 0.0,
             "most_common_freqs": [0.0016, 0.0015], "skew_gini": 0.006},
            {"pseudonym": "c_promo", "n_distinct": 10, "null_frac": 0.3,
             "most_common_freqs": [0.08, 0.07], "skew_gini": 0.1},
            {"pseudonym": "c_a", "n_distinct": 50, "null_frac": 0.0,
             "most_common_freqs": None, "skew_gini": None},
            {"pseudonym": "c_b", "n_distinct": 400, "null_frac": 0.0,
             "most_common_freqs": None, "skew_gini": None},
        ]
    }
    return schema, column_stats


def test_fk_fanout_per_edge_from_existing_sections():
    schema, column_stats = _fanout_inputs()
    result = fk_fanout(schema, column_stats)
    edges = {(e["from"], e["to"], tuple(e["from_cols"])): e for e in result["edges"]}

    cust = edges[("t_fact", "t_dim", ("c_cust",))]
    # n_distinct -0.005 of 200,000 rows = 1,000 distinct parents.
    assert cust["mean_fanout"] == pytest.approx(200.0)
    assert cust["max_fanout_est"] == pytest.approx(0.0016 * 200000)
    assert cust["null_fraction"] == 0.0
    assert cust["skew_gini"] == 0.006
    assert cust["composite"] is False

    promo = edges[("t_fact", "t_dim2", ("c_promo",))]
    # 70% of 200,000 rows spread over 10 parents.
    assert promo["mean_fanout"] == pytest.approx(14000.0)
    assert promo["null_fraction"] == pytest.approx(0.3)

    composite = edges[("t_fact", "t_dim", ("c_a", "c_b"))]
    # Approximated by the most selective column (400 distinct), and flagged.
    assert composite["composite"] is True
    assert composite["mean_fanout"] == pytest.approx(500.0)
    assert composite["max_fanout_est"] is None

    nostats = edges[("t_fact", "t_dim", ("c_nostats",))]
    assert nostats["mean_fanout"] is None

    assert result["edges_total"] == 4
    assert result["edges_with_stats"] == 3
    assert result["mean_fanout_quantiles"]["max"] == pytest.approx(14000.0)
    assert result["mean_fanout_quantiles"]["p50"] == pytest.approx(500.0)


def test_fk_fanout_handles_no_edges():
    result = fk_fanout({"tables": [], "fk_graph": []}, {"columns": []})
    assert result == {
        "edges": [],
        "edges_total": 0,
        "edges_with_stats": 0,
        "mean_fanout_quantiles": None,
    }


def test_fk_graph_summary_counts_and_max_fan_in():
    result = fk_graph_summary(_schema())
    assert result["node_count"] == 2
    assert result["edge_count"] == 1
    assert result["max_fan_in"] == 1
    assert result["max_fan_out"] == 1
    assert result["connected_components"] == 1


def test_fk_graph_summary_star_schema_fan_out_detects_fact_table():
    # One fact table referencing three dimensions: the fact table has
    # fan_out=3 (the actual "fact-table detector"), while every dimension
    # has fan_in=1 -- max_fan_in alone would never surface the fact table.
    schema = {
        "fk_graph": [
            {"from": "t_fact", "from_cols": ["c_a"], "to": "t_dim1", "to_cols": ["c_1"]},
            {"from": "t_fact", "from_cols": ["c_b"], "to": "t_dim2", "to_cols": ["c_2"]},
            {"from": "t_fact", "from_cols": ["c_c"], "to": "t_dim3", "to_cols": ["c_3"]},
        ]
    }
    result = fk_graph_summary(schema)
    assert result["max_fan_out"] == 3
    assert result["max_fan_in"] == 1


def test_fk_graph_summary_empty_graph():
    result = fk_graph_summary(_schema(fk_graph=[]))
    assert result["edge_count"] == 0
    assert result["node_count"] == 0
    assert result["connected_components"] == 0


def test_table_size_distribution_quantiles():
    result = table_size_distribution(_schema())
    assert result["total_bytes"] == 900 + 120
    assert result["largest_table_share"] == pytest.approx(900 / 1020)


def test_dead_tuple_pressure_distribution():
    result = dead_tuple_pressure(_activity())
    # t_0000: 50/1000 = 0.05; t_0001: 5/10 = 0.5 -- the max is t_0001's.
    assert result["max_ratio"] == pytest.approx(5 / 10)
    assert result["tables_over_threshold"] >= 0


def test_index_redundancy_unused_count_and_size_share():
    result = index_redundancy(_indexes())
    assert result["unused_count"] == 1
    assert result["unused_size_bytes"] == 20
    assert result["unused_size_share"] == pytest.approx(20 / 120)


def test_compute_derived_assembles_everything():
    result = compute_derived(
        schema_section=_schema(),
        indexes_section=_indexes(),
        workload_section=_workload(),
        activity_section=_activity(),
    )
    assert "read_write_ratio" in result
    assert "hot_update_fraction" in result
    assert "index_scan_share" in result
    assert "wal" in result
    assert "temp_spill" in result
    assert "cache_hit_ratios" in result
    assert "statement_concentration" in result
    assert "statement_recurrence" in result
    assert "fk_graph_summary" in result
    assert "fk_fanout" in result
    assert "table_size_distribution" in result
    assert "dead_tuple_pressure" in result
    assert "index_redundancy" in result
