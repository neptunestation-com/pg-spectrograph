"""Tests for pgspec.inspect (Milestone 10). Pure rendering over a synthetic
artifact dict: no database needed."""

from __future__ import annotations

from pgspec.inspect import (
    render_coverage_table,
    render_derived_summary,
    render_extended_stats_headline,
    render_inspect,
    render_instance_highlights,
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
        "workload": {"completeness": _ok_completeness()},
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
        },
        "temporal": None,
        "pseudonym_map_digest": "sha256:abc",
        "warnings": [],
    }
    base.update(overrides)
    return base


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
