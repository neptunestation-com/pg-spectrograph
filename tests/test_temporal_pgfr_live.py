"""Live tests against the pgfr v2 fixture (Milestone 13): pgfr_record and
pgfr_analyze installed at the pinned SHA in a pg_cron-enabled container,
with the synthesized 7-day history from tests/fixtures/f_pgfr.sql. These
exercise the positive paths Milestone 11 could only reason about: probe
success, real series through pgfr's presentation views, completeness from
pgfr_analyze, and §14's acceptance criterion for the batch detector.
"""

from __future__ import annotations

import time

import pytest

from pgspec.capture import (
    capture_point,
    capture_two_sample,
    connect,
    probe_capabilities,
    write_artifact,
)
from pgspec.inspect import render_inspect
from pgspec.sections.temporal_pgfr import capture_temporal_pgfr, probe_pgfr
from pgspec.validate import validate_artifact


def _temporal(pgfr_dsn: str, **kwargs) -> dict:
    conn = connect(pgfr_dsn)
    try:
        return capture_temporal_pgfr(conn, probe_capabilities(conn), **kwargs)
    finally:
        conn.close()


def test_probe_pgfr_reports_installed_and_capturing(pgfr_dsn):
    conn = connect(pgfr_dsn)
    try:
        status = probe_pgfr(conn, probe_capabilities(conn))
    finally:
        conn.close()

    assert status["available"] is True
    cron_rows = [row for row in status["health"] if row["check_name"].startswith("cron_job:")]
    assert cron_rows
    assert all(row["status"] == "ok" for row in cron_rows)


def test_temporal_window_completeness_comes_from_pgfr_analyze(pgfr_dsn):
    temporal = _temporal(pgfr_dsn)

    assert temporal["available"] is True
    window = temporal["window"]
    assert window["source"] == "pgfr_v2"
    assert window["bucket_seconds"] == 3600
    assert 0.9 <= window["completeness_fraction"] <= 1.0
    assert window["capture_ledger_gaps"] >= 1
    assert "fast" in window["coverage_by_tier"]
    assert temporal["completeness"]["available"] is True
    assert temporal["completeness"]["coverage"]["completeness_fraction"] == (
        window["completeness_fraction"]
    )


def test_temporal_metrics_cover_the_four_group_a_rates(pgfr_dsn):
    temporal = _temporal(pgfr_dsn)

    for metric in ("wal_bytes_rate", "tps", "blks_read_rate", "temp_bytes_rate"):
        summary = temporal["metrics"][metric]
        assert summary["bucket_count"] >= 24 * 6, metric
        assert len(summary["seasonal_24x7"]) == 7, metric
        assert all(len(row) == 24 for row in summary["seasonal_24x7"]), metric
        assert summary["trend_slope_per_day"] is not None, metric

    wal = temporal["metrics"]["wal_bytes_rate"]["quantiles"]
    assert 80_000 <= wal["p50"] <= 120_000
    assert wal["max"] > 4 * wal["p50"]
    tps = temporal["metrics"]["tps"]["quantiles"]
    assert 45 <= tps["p50"] <= 56
    blks = temporal["metrics"]["blks_read_rate"]["quantiles"]
    assert 180 <= blks["p50"] <= 220


def test_batch_detector_locates_the_scripted_weekday_spike(pgfr_dsn):
    # §14 definition of done: "the batch detector locates the scripted
    # weekday spike with correct phase."
    temporal = _temporal(pgfr_dsn)

    wal_events = [e for e in temporal["events"] if e["metric"] == "wal_bytes_rate"]
    assert len(wal_events) == 1
    event = wal_events[0]
    assert event["kind"] == "batch_spike"
    assert event["phase_hour_utc"] == 13
    assert event["cadence"] == "weekdays"
    # Five weekdays in any 7-day window; six when the window's edges both
    # cut through the same weekday's 13:00-13:30 batch.
    assert 5 <= event["occurrences"] <= 6
    assert event["duration_s"] == 3600
    assert event["magnitude_x_baseline"] > 4.0
    assert event["evidence"] == ["wal_bytes_rate"]

    smooth_metric_events = [e for e in temporal["events"] if e["metric"] != "wal_bytes_rate"]
    assert smooth_metric_events == []


def test_temporal_section_carries_no_identifiers_or_query_text(pgfr_dsn):
    temporal = _temporal(pgfr_dsn)

    def strings(node):
        if isinstance(node, dict):
            for key, value in node.items():
                yield key
                yield from strings(value)
        elif isinstance(node, list):
            for value in node:
                yield from strings(value)
        elif isinstance(node, str):
            yield node

    leaked = [s for s in strings(temporal) if "pgfr_record" in s or "SELECT" in s.upper()]
    assert leaked == []


def test_capture_point_with_pgfr_present_is_pgfr_mode_and_validates(pgfr_dsn, tmp_path):
    artifact = capture_point(
        pgfr_dsn,
        top_k=20,
        salt_file=str(tmp_path / ".pgspec-salt"),
        map_path=str(tmp_path / "spectrum-map.json"),
    )

    assert artifact["capture_mode"] == "pgfr"
    assert artifact["temporal"]["available"] is True

    out_path = tmp_path / "spectrum.json.gz"
    write_artifact(artifact, str(out_path))
    assert validate_artifact(str(out_path)) == []

    report = render_inspect(artifact)
    assert "## Temporal signature (pgfr v2)" in report
    # Coverage before conclusions (issue #3 finding 10).
    assert report.index("completeness (fast tier)") < report.index("weekdays")


def test_group_b_dead_tup_growth_comes_from_rollup_deltas(pgfr_dsn):
    # f_pgfr synthesizes 12 user tables whose (n_tup_upd + n_tup_del) per
    # hour is 150 * i, i = 1..12, so per-second rates span 0.04 to 0.5.
    # pg_cron's own tables (cron.job, cron.job_run_details) are real,
    # lightly active user-schema tables in this database and may join them
    # once their daily bucket closes, exactly as they would in the schema
    # section, so the count is bounded rather than exact.
    temporal = _temporal(pgfr_dsn)
    growth = temporal["metrics"]["dead_tup_growth"]
    assert growth["source"] == "rollup_deltas(pg_catalog.pg_stat_all_tables)"
    assert 12 <= growth["tables_observed"] <= 14
    assert growth["quantiles"]["n"] == growth["tables_observed"]
    assert 0.03 <= growth["quantiles"]["p50"] <= 0.45
    assert 0.4 <= growth["quantiles"]["max"] <= 0.6


def test_maintenance_rhythm_quantiles(pgfr_dsn):
    temporal = _temporal(pgfr_dsn)
    maintenance = temporal["maintenance"]

    # Table i autovacuums every (6 + i) hours: 3.4/day down to 1.3/day.
    # (pg_cron's tables may add one or two real entries; see the Group B
    # test above.)
    autovacuum = maintenance["autovacuum_events_per_day_by_table_quantiles"]
    assert 12 <= autovacuum["n"] <= 14
    assert 1.0 <= autovacuum["p50"] <= 3.5

    # One timed checkpoint per 5-minute sample, plus requested ones during
    # the weekday batch, so the typical spacing is 300 s and the timed share
    # is high but below 1.
    checkpoints = maintenance["checkpoint_interval_quantiles"]
    assert 280 <= checkpoints["interval_s"]["p50"] <= 320
    assert 0.9 <= checkpoints["timed_fraction"] < 1.0

    # n_dead_tup climbs and drops each autovacuum cycle; amplitude relative
    # to the live-tuple peak is a percent or two.
    sawtooth = maintenance["dead_tuple_sawtooth_amplitude_quantiles"]
    assert 12 <= sawtooth["n"] <= 14
    assert 0.005 <= sawtooth["p50"] <= 0.1


def test_statement_mixture_is_queryid_only_with_measured_churn(pgfr_dsn):
    # 25 synthetic statements over 14 days; five stop and five start exactly
    # one week in, so the week-over-week top-set churn is 10 / 25 = 0.4.
    # Restricting top_n to the synthetic count keeps pgfr's own collector
    # statements (real captures since install) out of the set.
    temporal = _temporal(pgfr_dsn, mixture_top_n=25)
    mixture = temporal["statement_mixture"]
    assert mixture["top_n"] == 25
    assert mixture["share_timeseries_bucket_seconds"] == 3600
    assert mixture["window_days"] == 14
    assert len(mixture["series"]) == 25
    assert len(mixture["bucket_starts"]) == 24 * 14
    for entry in mixture["series"]:
        assert set(entry) == {"queryid", "shares"}
        assert len(entry["shares"]) == 24 * 14
    assert mixture["churn_week_over_week"] == pytest.approx(0.4, abs=0.02)
    assert mixture["share_drift_mean_l1"] > 0
    assert mixture["set_churn_day_over_day"] > 0

    default = _temporal(pgfr_dsn)["statement_mixture"]
    assert default["top_n"] == 50
    assert default["statements_considered"] == 50


def test_connection_concurrency_is_sampled_with_an_error_model(pgfr_dsn):
    # r_pg_stat_activity is synthesized with 8 active backends during
    # weekday business hours (09:00-17:00 UTC) and 2 otherwise, at 60 fast
    # ticks per hour.
    temporal = _temporal(pgfr_dsn)
    concurrency = temporal["metrics"]["connection_concurrency"]
    assert concurrency["source"] == "r_pg_stat_activity"
    assert concurrency["sampling"]["regime"] == "mode_a_sampled"
    assert 50 <= concurrency["sampling"]["interval_s"] <= 70
    assert "detection_probability" in concurrency["sampling"]
    profile = concurrency["seasonal_24x7"]
    assert 7.0 <= profile[0][12] <= 9.0  # Monday noon
    assert 1.5 <= profile[0][3] <= 2.5  # Monday 03:00
    assert 1.5 <= profile[6][12] <= 2.5  # Sunday noon


def test_capture_two_sample_delegates_to_pgfr_without_sleeping(pgfr_dsn, tmp_path):
    start = time.monotonic()
    artifact = capture_two_sample(
        pgfr_dsn,
        interval_s=600,
        top_k=20,
        salt_file=str(tmp_path / ".pgspec-salt"),
        map_path=str(tmp_path / "spectrum-map.json"),
    )
    assert time.monotonic() - start < 120
    assert artifact["capture_mode"] == "pgfr"
    assert artifact["temporal"]["available"] is True
