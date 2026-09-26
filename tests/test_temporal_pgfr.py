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
    capture_temporal_pgfr,
    detect_batch_events,
    probe_pgfr,
)


def _bucket(day: int, hour: int, value: float) -> tuple[dt.datetime, float]:
    return (dt.datetime(2026, 1, day, hour, tzinfo=dt.timezone.utc), value)


def test_detect_batch_events_flags_a_clear_spike():
    # Three weeks of baseline at ~10, one day with a 13:00 spike to 200.
    series = []
    for day in range(1, 22):
        for hour in range(24):
            value = 200.0 if (day == 21 and hour == 13) else 10.0
            series.append(_bucket(day, hour, value))

    events = detect_batch_events(series, z_threshold=3.0)
    assert len(events) == 1
    event = events[0]
    assert event["phase_hour"] == 13
    assert event["magnitude_x_baseline"] > 5.0


def test_detect_batch_events_merges_adjacent_flagged_buckets():
    series = []
    for day in range(1, 15):
        for hour in range(24):
            value = 150.0 if (day == 14 and hour in (13, 14)) else 10.0
            series.append(_bucket(day, hour, value))

    events = detect_batch_events(series, z_threshold=3.0)
    assert len(events) == 1
    assert events[0]["duration_buckets"] == 2


def test_detect_batch_events_silent_on_flat_series():
    series = [_bucket(day, hour, 10.0) for day in range(1, 8) for hour in range(24)]
    events = detect_batch_events(series, z_threshold=3.0)
    assert events == []


def test_detect_batch_events_handles_empty_series():
    assert detect_batch_events([], z_threshold=3.0) == []


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
