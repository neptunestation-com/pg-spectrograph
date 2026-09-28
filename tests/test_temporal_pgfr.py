"""Tests for pgspec.sections.temporal_pgfr (Milestone 11), written before
the implementation. The batch detector is pure (no database); probe_pgfr
and capture_temporal_pgfr are tested against the live canary container,
which correctly has no pgfr_record installed -- exercising the fail-soft
"pgfr absent" path, the common case for most customers per §9's framing.
"""

from __future__ import annotations

import datetime as dt

import pytest

from pgspec.capture import Capabilities, connect
from pgspec.sections.temporal_pgfr import (
    bucket_rates,
    capture_temporal_pgfr,
    detect_batch_events,
    probe_pgfr,
    rate_summary,
)

UTC = dt.timezone.utc


def _bucket(day: int, hour: int, value: float) -> tuple[dt.datetime, float]:
    return (dt.datetime(2026, 1, day, hour, tzinfo=UTC), value)


def test_detect_batch_events_flags_a_clear_spike():
    # Three weeks of baseline at ~10, one day with a 13:00 spike to 200.
    series = []
    for day in range(1, 22):
        for hour in range(24):
            value = 200.0 if (day == 21 and hour == 13) else 10.0
            series.append(_bucket(day, hour, value))

    events = detect_batch_events(series, z_threshold=3.0, metric="wal_bytes_rate")
    assert len(events) == 1
    event = events[0]
    assert event["kind"] == "batch_spike"
    assert event["metric"] == "wal_bytes_rate"
    assert event["phase_hour_utc"] == 13
    assert event["cadence"] == "once"
    assert event["occurrences"] == 1
    assert event["magnitude_x_baseline"] > 5.0
    assert event["evidence"] == ["wal_bytes_rate"]


def test_detect_batch_events_merges_adjacent_flagged_buckets():
    series = []
    for day in range(1, 15):
        for hour in range(24):
            value = 150.0 if (day == 14 and hour in (13, 14)) else 10.0
            series.append(_bucket(day, hour, value))

    events = detect_batch_events(series, z_threshold=3.0)
    assert len(events) == 1
    assert events[0]["duration_buckets"] == 2
    assert events[0]["duration_s"] == 7200


def test_detect_batch_events_silent_on_flat_series():
    series = [_bucket(day, hour, 10.0) for day in range(1, 8) for hour in range(24)]
    events = detect_batch_events(series, z_threshold=3.0)
    assert events == []


def test_detect_batch_events_handles_empty_series():
    assert detect_batch_events([], z_threshold=3.0) == []


def test_detect_batch_events_finds_a_recurring_weekday_batch():
    # 2026-01-05 is a Monday. Two full weeks; every weekday at 13:00 the
    # rate is 8x a mildly wobbling baseline. An hour-of-day baseline would
    # absorb this entirely (the batch *is* hour 13's baseline); the robust
    # global baseline must surface it as one recurring event.
    series = []
    for day in range(5, 19):
        for hour in range(24):
            t = dt.datetime(2026, 1, day, hour, tzinfo=UTC)
            value = 100.0 + 5.0 * ((hour * 7 + day) % 3)
            if t.weekday() < 5 and hour == 13:
                value = 800.0
            series.append((t, value))

    events = detect_batch_events(series, z_threshold=3.0, metric="wal_bytes_rate")
    assert len(events) == 1
    event = events[0]
    assert event["cadence"] == "weekdays"
    assert event["phase_hour_utc"] == 13
    assert event["occurrences"] == 10
    assert event["duration_buckets"] == 1
    assert event["magnitude_x_baseline"] > 5.0


def test_detect_batch_events_ignores_small_wobble_above_median():
    series = []
    for day in range(1, 8):
        for hour in range(24):
            series.append(_bucket(day, hour, 100.0 + 10.0 * (hour % 2)))
    assert detect_batch_events(series, z_threshold=3.0) == []


def _sample(base: dt.datetime, minutes: int, value: float) -> tuple[dt.datetime, float]:
    return (base + dt.timedelta(minutes=minutes), value)


def test_bucket_rates_is_time_weighted_and_reset_aware():
    base = dt.datetime(2026, 1, 5, 12, 0, tzinfo=UTC)
    samples = []
    # Hour one: 10 units/s, sampled every 10 minutes.
    for i in range(7):
        samples.append(_sample(base, 10 * i, 6000.0 * i))
    # Hour two: 20 units/s, with a counter reset at 13:30.
    running = 36000.0
    for i in range(1, 7):
        minutes = 60 + 10 * i
        if minutes == 90:
            running = 0.0
        else:
            running += 12000.0
        samples.append(_sample(base, minutes, running))

    buckets = bucket_rates(samples, bucket_seconds=3600)
    assert [t for t, _r in buckets] == [base, base + dt.timedelta(hours=1)]
    assert buckets[0][1] == pytest.approx(10.0)
    assert buckets[1][1] == pytest.approx(20.0)


def test_bucket_rates_supports_sub_hour_buckets():
    base = dt.datetime(2026, 1, 5, 12, 0, tzinfo=UTC)
    samples = [_sample(base, 5 * i, 300.0 * i) for i in range(13)]
    buckets = bucket_rates(samples, bucket_seconds=900)
    assert len(buckets) == 4
    assert all(rate == pytest.approx(1.0) for _t, rate in buckets)


def test_bucket_rates_needs_two_samples():
    base = dt.datetime(2026, 1, 5, 12, 0, tzinfo=UTC)
    assert bucket_rates([], bucket_seconds=3600) == []
    assert bucket_rates([(base, 5.0)], bucket_seconds=3600) == []


def test_rate_summary_quantiles_seasonal_profile_and_trend():
    monday = dt.datetime(2026, 1, 5, 0, 0, tzinfo=UTC)
    buckets = [(monday + dt.timedelta(hours=h), 10.0 + h) for h in range(24)]

    summary = rate_summary(buckets)
    assert summary["bucket_count"] == 24
    assert summary["quantiles"]["max"] == 33.0
    assert summary["quantiles"]["p50"] == pytest.approx(21.5, abs=0.5)
    assert len(summary["seasonal_24x7"]) == 7
    assert all(len(row) == 24 for row in summary["seasonal_24x7"])
    assert summary["seasonal_24x7"][0][5] == 15.0
    assert summary["seasonal_24x7"][1][0] is None
    assert summary["trend_slope_per_day"] == pytest.approx(24.0)


def test_rate_summary_handles_empty_and_single_bucket():
    assert rate_summary([])["bucket_count"] == 0
    assert rate_summary([])["quantiles"] is None
    single = rate_summary([(dt.datetime(2026, 1, 5, tzinfo=UTC), 7.0)])
    assert single["quantiles"] == {"p50": 7.0, "p95": 7.0, "p99": 7.0, "max": 7.0}
    assert single["trend_slope_per_day"] is None


def test_probe_pgfr_reports_unavailable_when_not_installed(pg16_dsn):
    conn = connect(pg16_dsn)
    try:
        capabilities = Capabilities(server_version_num=160011, is_in_recovery=False)
        status = probe_pgfr(conn, capabilities)
    finally:
        conn.close()
    assert status["available"] is False
    assert "not installed" in status["reason"]


def test_probe_pgfr_gates_on_pg15_without_querying(pg16_dsn):
    conn = connect(pg16_dsn)
    try:
        capabilities = Capabilities(server_version_num=140005, is_in_recovery=False)
        status = probe_pgfr(conn, capabilities)
    finally:
        conn.close()
    assert status["available"] is False
    assert "15" in status["reason"]


def test_capture_temporal_pgfr_fails_soft_when_absent(pg16_dsn):
    conn = connect(pg16_dsn)
    try:
        capabilities = Capabilities(server_version_num=160011, is_in_recovery=False)
        section = capture_temporal_pgfr(conn, capabilities)
    finally:
        conn.close()

    assert section["available"] is False
    assert section["completeness"]["available"] is False
    assert section["statement_mixture"] == "unavailable_in_v1"
    assert section["events"] == []


def test_capture_point_pgfr_auto_leaves_temporal_none_when_absent(pg16_dsn, tmp_path):
    from pgspec.capture import capture_point

    artifact = capture_point(
        pg16_dsn,
        top_k=10,
        salt_file=str(tmp_path / ".pgspec-salt"),
        map_path=str(tmp_path / "spectrum-map.json"),
        pgfr="auto",
    )
    assert artifact["capture_mode"] == "point"
    assert artifact["temporal"] is None


def test_capture_point_pgfr_require_raises_when_absent(pg16_dsn, tmp_path):
    from pgspec.capture import capture_point

    with pytest.raises(RuntimeError, match="pgfr required but unavailable"):
        capture_point(
            pg16_dsn,
            top_k=10,
            salt_file=str(tmp_path / ".pgspec-salt"),
            map_path=str(tmp_path / "spectrum-map.json"),
            pgfr="require",
        )


def test_capture_two_sample_pgfr_require_raises_without_sleeping(pg16_dsn, tmp_path):
    """pgfr wins when present (§9): capture_two_sample probes pgfr up front
    and, since it's required-but-absent here, must fail fast rather than
    running the full sample-A/sleep/sample-B flow with a long interval."""
    from pgspec.capture import capture_two_sample

    with pytest.raises(RuntimeError, match="pgfr required but unavailable"):
        capture_two_sample(
            pg16_dsn,
            interval_s=9999,
            top_k=10,
            salt_file=str(tmp_path / ".pgspec-salt"),
            map_path=str(tmp_path / "spectrum-map.json"),
            pgfr="require",
        )
