"""Live tests against the pgfr v2 fixture (Milestone 13): pgfr_record and
pgfr_analyze installed at the pinned SHA in a pg_cron-enabled container,
with the synthesized 7-day history from tests/fixtures/f_pgfr.sql. These
exercise the positive paths Milestone 11 could only reason about: probe
success, real series through pgfr's presentation views, completeness from
pgfr_analyze, and §14's acceptance criterion for the batch detector.
"""

from __future__ import annotations

import time

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


def _temporal(pgfr_dsn: str) -> dict:
    conn = connect(pgfr_dsn)
    try:
        return capture_temporal_pgfr(conn, probe_capabilities(conn))
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
