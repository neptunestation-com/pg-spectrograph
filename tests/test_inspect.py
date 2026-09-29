"""Tests for pgspec.inspect (Milestone 10). Pure rendering over a synthetic
artifact dict: no database needed."""

from __future__ import annotations

from pgspec.inspect import (
    render_coverage_table,
    render_derived_summary,
    render_extended_stats_headline,
    render_inspect,
    render_instance_highlights,
    render_temporal_summary,
)


def _ok_completeness(**overrides):
    base = {"available": True, "coverage": {}, "staleness": {}, "limits": {}, "notes": []}
    base.update(overrides)
    return base


def _artifact(**overrides):
    base = {
        "signature_version": "1.0",
        "captured_at": "2026-09-26T00:00:00Z",
        "capture_mode": "point",
        "capture_duration_s": 1.234,
        "instance": {
            "server_version_num": 160011,
            "extensions": {"pg_stat_statements": "1.10", "plpgsql": "1.0"},
            "guc_snapshot": {
                "shared_buffers": {"setting": "16384", "unit": "8kB", "source": "configuration file"},
                "work_mem": {"setting": "4096", "unit": "kB", "source": "default"},
            },
            "completeness": _ok_completeness(),
        },
        "schema": {"completeness": _ok_completeness()},
        "column_stats": {
            "extended_stats": {"defined": 0},
            "completeness": _ok_completeness(),
        },
        "indexes": {"completeness": _ok_completeness()},
        "workload": {
            "completeness": _ok_completeness(),
            "representativity": {
                "population": {"statements": 1554, "quantiles": {}, "verb_mix": {}},
                "captured": {"statements": 132, "quantiles": {}, "verb_mix": {}},
                "tail_sample": {"statements": 50, "quantiles": {}},
                "median_log10_ratio": {
                    "mean_exec_time": 1.565,
                    "calls": 0.0,
                    "rows_per_call": 0.155,
                    "blks_per_call": 1.7,
                },
                "verb_mix_kl_bits": {"calls": 0.012, "exec_time": 0.001},
            },
        },
        "activity": {"completeness": _ok_completeness()},
        "derived": {
            "read_write_ratio": {
                "tup_level": {"reads": 1000, "writes": 100, "ratio": 10.0},
                "statement_level": {"reads": 8, "writes": 2, "ratio": 4.0},
            },
            "hot_update_fraction": {
                "aggregate": 0.5,
                "per_table_min": 0.1,
                "per_table_median": 0.5,
                "per_table_max": 0.9,
            },
            "index_scan_share": 0.75,
            "wal": {"wal_bytes_per_xact": 5000.0, "wal_fpi_fraction": 0.05},
            "temp_spill": {
                "temp_bytes_per_xact": 0.0,
                "statements_with_temp_fraction": 0.0,
            },
            "cache_hit_ratios": {"db_level": 0.99, "io_level": 0.98},
            "working_set_bound": {
                "total_relation_bytes": 1000000,
                "ratio_to_shared_buffers": 0.01,
            },
            "statement_concentration": {
                "exec_time": {"entropy_bits": 2.5, "top1_share": 0.4, "top10_share": 0.9},
                "blks_read": {"entropy_bits": 1.5, "top1_share": 0.7, "top10_share": 0.95},
            },
            "statement_recurrence": {
                "calls": {"entropy_bits": 0.8, "top1_share": 0.8, "top10_share": 1.0},
                "dealloc_count": 12,
                "eviction_churn_fraction": None,
            },
            "fk_graph_summary": {
                "node_count": 3,
                "edge_count": 2,
                "max_fan_in": 2,
                "connected_components": 1,
            },
            "table_size_distribution": {
                "total_bytes": 500000,
                "largest_table_share": 0.6,
            },
            "dead_tuple_pressure": {"max_ratio": 0.3, "tables_over_threshold": 1},
            "index_redundancy": {"unused_count": 2, "unused_size_share": 0.25},
            "fk_fanout": {
                "edges": [],
                "edges_total": 6,
                "edges_with_stats": 6,
                "mean_fanout_quantiles": {"p50": 548.0, "p90": 10000.0, "max": 14033.0},
            },
        },
        "temporal": None,
        "pseudonym_map_digest": "sha256:abc",
        "warnings": [],
    }
    base.update(overrides)
    return base


def _temporal():
    return {
        "available": True,
        "window": {
            "start": "2026-09-21T10:00:00+00:00",
            "end": "2026-09-28T09:00:00+00:00",
            "bucket_seconds": 3600,
            "source": "pgfr_v2",
            "completeness_fraction": 0.988,
            "capture_ledger_gaps": 1,
            "coverage_by_tier": {"fast": 0.988, "medium": 0.0},
        },
        "metrics": {
            "wal_bytes_rate": {
                "bucket_count": 168,
                "quantiles": {"p50": 100000.0, "p95": 110000.0, "p99": 650000.0, "max": 660000.0},
                "seasonal_24x7": [[None] * 24 for _ in range(7)],
                "trend_slope_per_day": 12.5,
                "source_view": "pg_stat_wal",
            }
        },
        "statement_mixture": {
            "top_n": 50,
            "statements_considered": 25,
            "window_days": 14,
            "share_timeseries_bucket_seconds": 3600,
            "bucket_starts": [],
            "series": [],
            "share_drift_mean_l1": 0.012,
            "set_churn_day_over_day": 0.05,
            "churn_week_over_week": 0.4,
        },
        "events": [
            {
                "kind": "batch_spike",
                "metric": "wal_bytes_rate",
                "cadence": "weekdays",
                "phase_hour_utc": 13,
                "occurrences": 5,
                "duration_buckets": 1,
                "duration_s": 3600,
                "magnitude_x_baseline": 6.5,
                "first_seen": "2026-09-21T13:00:00+00:00",
                "last_seen": "2026-09-25T13:00:00+00:00",
                "evidence": ["wal_bytes_rate"],
            }
        ],
        "maintenance": {
            "window_days": 28,
            "autovacuum_events_per_day_by_table_quantiles": {
                "p50": 1.9, "p95": 3.2, "p99": 3.4, "max": 3.4, "n": 12,
            },
            "checkpoint_interval_quantiles": {
                "interval_s": {"p50": 300.0, "p95": 300.0, "p99": 300.0, "max": 300.0, "n": 168},
                "timed_fraction": 0.98,
            },
            "dead_tuple_sawtooth_amplitude_quantiles": {
                "p50": 0.015, "p95": 0.026, "p99": 0.027, "max": 0.027, "n": 12,
            },
        },
        "completeness": _ok_completeness(),
    }


def test_render_temporal_summary_covers_mixture_and_maintenance():
    text = render_temporal_summary(_artifact(temporal=_temporal()))
    assert "statement mixture" in text
    assert "week-over-week" in text and "40.0%" in text
    assert "autovacuum" in text and "checkpoint interval" in text and "sawtooth" in text
    assert text.index("completeness (fast tier)") < text.index("statement mixture")


def test_render_coverage_states_workload_representativity():
    report = render_inspect(_artifact())
    coverage_block = report[report.index("## Coverage") : report.index("## Instance")]
    assert "representativity" in coverage_block
    assert "132 of 1554" in coverage_block and "50 sampled from the tail" in coverage_block
    assert "36.7x" in coverage_block


def test_render_derived_summary_includes_fk_fanout():
    text = render_derived_summary(_artifact())
    assert "### FK fanout" in text
    assert "p50=548" in text and "max=14033" in text and "6 edges" in text


def test_render_derived_summary_reads_recurrence_against_cache_locality():
    text = render_derived_summary(_artifact())
    assert "### Statement recurrence" in text
    assert "by calls" in text and "top1=80.0%" in text
    assert "deallocations: 12" in text
    assert text.index("### Cache hit ratio") < text.index("### Statement recurrence")


def test_render_temporal_summary_is_absent_without_pgfr():
    assert render_temporal_summary(_artifact()) is None
    assert "Temporal signature" not in render_inspect(_artifact())


def test_render_temporal_summary_states_coverage_before_events():
    text = render_temporal_summary(_artifact(temporal=_temporal()))
    assert "completeness (fast tier): 98.8%" in text
    assert "ledger gaps: 1" in text
    assert text.index("completeness (fast tier)") < text.index("wal_bytes_rate") < text.index(
        "weekdays"
    )

    report = render_inspect(_artifact(temporal=_temporal(), capture_mode="pgfr"))
    assert "## Temporal signature (pgfr v2)" in report
    assert report.index("## Coverage") < report.index("## Temporal signature (pgfr v2)")


def test_render_coverage_table_lists_all_sections():
    table = render_coverage_table(_artifact())
    assert "instance" in table
    assert "workload" in table
    assert "| yes |" in table


def test_render_coverage_table_surfaces_unavailable_reason():
    artifact = _artifact()
    artifact["workload"] = {
        "completeness": _ok_completeness(
            available=False, notes=["pg_stat_statements extension not installed"]
        )
    }
    table = render_coverage_table(artifact)
    assert "| no |" in table
    assert "pg_stat_statements extension not installed" in table


def test_render_instance_highlights_shows_version_and_extensions():
    text = render_instance_highlights(_artifact())
    assert "16" in text
    assert "pg_stat_statements" in text
    assert "shared_buffers" in text


def test_render_extended_stats_headline_flags_blind_spot_when_none_defined():
    text = render_extended_stats_headline(_artifact())
    assert "blind spot" in text


def test_render_extended_stats_headline_reports_count_when_defined():
    artifact = _artifact()
    artifact["column_stats"]["extended_stats"] = {"defined": 2}
    text = render_extended_stats_headline(artifact)
    assert "2 extended-statistics" in text


def test_render_derived_summary_includes_both_concentration_lenses():
    text = render_derived_summary(_artifact())
    assert "by exec time" in text
    assert "by block I/O" in text
    assert "hardware-invariant" in text


def test_render_inspect_produces_a_complete_report():
    report = render_inspect(_artifact())
    assert report.startswith("# pgspec spectral summary")
    assert "## Coverage" in report
    assert "## Instance" in report
    assert "## Extended statistics coverage" in report
    assert "## Derived performance signature" in report


def test_render_inspect_handles_missing_sections_gracefully():
    # A minimal, mostly-empty artifact (e.g. workload unavailable) must not
    # crash the renderer.
    report = render_inspect({"captured_at": "now", "capture_mode": "point"})
    assert "# pgspec spectral summary" in report
