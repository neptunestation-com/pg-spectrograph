"""Optional pg_flight_recorder v2 temporal augmentation (§9).

pgfr is a consumer-only augmentation: this module only ever reads from
pgfr_record's already-captured data (its presentation views and rollup
tables), never writes to it, and applies the exact same pseudonymization
and I1 value-free transforms to whatever it reads as the rest of pgspec
applies to a live capture. pgfr itself is not privacy-preserving -- it
stores raw query text and raw identifiers indefinitely -- so nothing here
gets a free pass just because the data is already sitting in a table
(GitHub issue #3, finding 7).

Scope note: pgfr v2 is not yet merged to its own project's main branch,
and standing up a full pg_cron + pgfr_record installation is a
substantial dependency this module does not take on for v1. What's
implemented here is real: presence/health detection (finding 9), the
PG15+ version gate (finding 8), and one representative rollup-backed
metric (WAL byte rate) plus the batch/spike detector pgfr itself doesn't
have yet (finding 5, tracked upstream as pg_flight_recorder issue #132).
statement_mixture and maintenance rhythm are honestly marked
unavailable_in_v1, the same convention workload.py already uses for
parameter_distributions, rather than faked.
"""

from __future__ import annotations

import datetime as dt
import statistics

import psycopg

from pgspec.capture import Capabilities
from pgspec.completeness import build_completeness

#: pgfr v2 has a hard pg_cron dependency and targets PG15+ (issue #3
#: finding 8); below this, augmentation is categorically unavailable, not
#: just undetected, so skip the live probe entirely.
MIN_PGFR_MAJOR = 15


def probe_pgfr(conn: psycopg.Connection, capabilities: Capabilities) -> dict:
    """Detect whether pgfr_record is installed AND actively capturing.
    Schema existence alone only proves "installed", not "capturing"
    (issue #3 finding 9) -- health_check()'s cron_job rows distinguish
    installed-but-disabled from actually running.
    """
    if capabilities.server_major < MIN_PGFR_MAJOR:
        return {
            "available": False,
            "reason": f"pgfr v2 requires PostgreSQL {MIN_PGFR_MAJOR}+ (server is {capabilities.server_major})",
        }

    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('pgfr_record.manifest') IS NOT NULL")
        manifest_exists = cur.fetchone()[0]
    if not manifest_exists:
        return {"available": False, "reason": "pgfr_record not installed"}

    try:
        with conn.cursor() as cur:
            cur.execute("SELECT check_name, status FROM pgfr_record.health_check()")
            health_rows = cur.fetchall()
    except psycopg.Error:
        return {
            "available": False,
            "reason": "pgfr_record installed but health_check() failed",
        }

    cron_rows = [row for row in health_rows if row[0].startswith("cron_job:")]
    active = bool(cron_rows) and all(status == "ok" for _name, status in cron_rows)
    if not active:
        return {
            "available": False,
            "reason": "pgfr_record installed but no tier jobs are active (health_check())",
        }
    return {
        "available": True,
        "health": [{"check_name": name, "status": status} for name, status in health_rows],
    }


def fetch_wal_bytes_rate_series(
    conn: psycopg.Connection, bucket_seconds: int = 3600
) -> list[tuple[dt.datetime, float]]:
    """Consecutive-sample WAL byte rate, bucketed to bucket_seconds, read
    from pgfr_record's own presentation view (Group A: 365 days retention
    at raw resolution, no rollup needed -- issue #3 finding 1). This reads
    one specific, well-known counter directly rather than calling
    pgfr_record.deltas() by name: deltas()'s calling convention (a
    caller-supplied column-definition list) is built for interactive psql
    use, not for a generic Python client reading one metric.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT captured_at, wal_bytes FROM pgfr_record.v_pg_stat_wal "
            "ORDER BY captured_at"
        )
        rows = cur.fetchall()

    buckets: dict[dt.datetime, tuple[dt.datetime, float]] = {}
    for i in range(1, len(rows)):
        prev_t, prev_bytes = rows[i - 1]
        t, wal_bytes = rows[i]
        elapsed = (t - prev_t).total_seconds()
        if elapsed <= 0 or wal_bytes < prev_bytes:
            continue  # reset-aware: a decreased counter is a reset, not a rate
        rate = (wal_bytes - prev_bytes) / elapsed
        bucket_start = t.replace(
            minute=0, second=0, microsecond=0
        ) if bucket_seconds >= 3600 else t
        buckets[bucket_start] = (bucket_start, rate)
    return sorted(buckets.values())


def detect_batch_events(
    series: list[tuple[dt.datetime, float]], z_threshold: float = 3.0
) -> list[dict]:
    """Threshold-crossing batch/spike detector (§9 point 4): z-score against
    an hour-of-day baseline, merging adjacent flagged buckets into one
    event. Deliberately dumb, per §9's own instruction ("keep the detector
    dumb in v1") -- this is pgspec's own responsibility, not pgfr's
    (tracked upstream as pg_flight_recorder issue #132, not yet built).
    """
    if len(series) < 2:
        return []

    by_hour: dict[int, list[float]] = {}
    for timestamp, value in series:
        by_hour.setdefault(timestamp.hour, []).append(value)

    baseline: dict[int, tuple[float, float]] = {}
    for hour, values in by_hour.items():
        mean = statistics.fmean(values)
        stdev = statistics.pstdev(values) if len(values) > 1 else 0.0
        baseline[hour] = (mean, stdev)

    flagged: list[tuple[dt.datetime, float, float]] = []  # (t, value, magnitude_x_baseline)
    for timestamp, value in series:
        mean, stdev = baseline[timestamp.hour]
        if stdev > 0:
            z = (value - mean) / stdev
        else:
            z = 0.0 if value <= mean else float("inf")
        if z >= z_threshold and mean > 0:
            flagged.append((timestamp, value, value / mean))

    if not flagged:
        return []

    flagged.sort(key=lambda row: row[0])
    events: list[dict] = []
    current = [flagged[0]]
    for row in flagged[1:]:
        prev_t = current[-1][0]
        gap = (row[0] - prev_t).total_seconds()
        if 0 < gap <= 3600 * 1.5:
            current.append(row)
        else:
            events.append(_merge_event(current))
            current = [row]
    events.append(_merge_event(current))
    return events


def _merge_event(rows: list[tuple[dt.datetime, float, float]]) -> dict:
    start = rows[0][0]
    return {
        "kind": "batch_spike",
        "phase_hour": start.hour,
        "start": start.isoformat(),
        "duration_buckets": len(rows),
        "magnitude_x_baseline": max(r[2] for r in rows),
    }


def capture_temporal_pgfr(
    conn: psycopg.Connection, capabilities: Capabilities
) -> dict:
    """Build the optional temporal section (§9). Returns available: False
    with a reason (I5) whenever pgfr isn't installed, isn't capturing, or
    is on a PostgreSQL version it doesn't support."""
    status = probe_pgfr(conn, capabilities)
    if not status["available"]:
        return {
            "available": False,
            "window": None,
            "metrics": {},
            "statement_mixture": "unavailable_in_v1",
            "events": [],
            "maintenance": "unavailable_in_v1",
            "completeness": build_completeness(
                available=False, reason=status["reason"]
            ),
        }

    series = fetch_wal_bytes_rate_series(conn)
    events = detect_batch_events(series)
    rates = [rate for _t, rate in series]

    metrics = {}
    if rates:
        quantile_values = (
            statistics.quantiles(rates, n=100)
            if len(rates) >= 2
            else [rates[0]] * 99
        )
        metrics["wal_bytes_rate"] = {
            "quantiles": {
                "p50": quantile_values[49],
                "p95": quantile_values[94],
                "p99": quantile_values[98],
                "max": max(rates),
            }
        }

    window = None
    if series:
        window = {
            "start": series[0][0].isoformat(),
            "end": series[-1][0].isoformat(),
            "bucket_seconds": 3600,
        }

    return {
        "available": True,
        "window": window,
        "metrics": metrics,
        "statement_mixture": "unavailable_in_v1",
        "events": events,
        "maintenance": "unavailable_in_v1",
        "completeness": build_completeness(
            available=True,
            coverage={"wal_bytes_rate_buckets": len(rates)},
        ),
    }
