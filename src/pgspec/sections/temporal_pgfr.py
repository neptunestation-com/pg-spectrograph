"""Optional pg_flight_recorder v2 temporal augmentation (§9).

pgfr is a consumer-only augmentation: this module only ever reads
pgfr_record's already-captured data (its presentation views) and
pgfr_analyze's coverage functions, never writes to either, and applies the
same I1 value-free discipline to whatever it reads as the rest of pgspec
applies to a live capture. pgfr itself is not privacy-preserving -- it
stores raw query text and raw identifiers indefinitely -- so nothing here
gets a free pass because the data is already sitting in a table (GitHub
issue #3, finding 7). Everything emitted is a rate, a quantile, a ratio, a
count, a cadence label, or a UTC hour: no identifier, no query text.

Temporal v1.1 (issue #3's ratified decisions): presence/health detection
(finding 9), the PG15+ version gate (finding 8), the four Group A headline
rates §9 lists (wal_bytes_rate, tps, blks_read_rate, temp_bytes_rate), each
with hourly-bucket quantiles, a 24x7 seasonal profile, and a trend slope;
window completeness from pgfr_analyze.coverage()/coverage_gaps() (finding
2); and the batch/spike detector pgfr itself doesn't have yet (finding 5,
upstream as pg_flight_recorder issue #132). statement_mixture and
maintenance rhythm remain unavailable_in_v1 (issue #5), the same convention
workload.py uses for parameter_distributions, rather than faked.

Group A counters are read raw from the presentation views: pgfr keeps them
365 days at raw resolution with no rollup (finding 1). The reset-aware
differencing is done here rather than through pgfr_record.deltas(), whose
SETOF-record calling convention is built for interactive psql rather than
a Python client reading one metric (finding 4, deviation ratified on issue
#3); rollup_deltas() is the committed path for Group B (issue #5).
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
BUCKET_SECONDS = 3600
Z_THRESHOLD = 3.0
MIN_MAGNITUDE_X_BASELINE = 2.0
_MAD_TO_SIGMA = 1.4826
_UTC = dt.timezone.utc

# Group A presentation views (365 days raw, no rollup). Per-database views
# are summed per captured_at so every series is cluster-wide, like
# pg_stat_wal's. All four are cumulative counters; bucket_rates() turns
# them into rates.
WAL_BYTES_SERIES_SQL = """
    SELECT captured_at, wal_bytes::numeric
    FROM pgfr_record.v_pg_stat_wal
    ORDER BY captured_at
"""
TPS_SERIES_SQL = """
    SELECT captured_at, sum(xact_commit + xact_rollback)::numeric
    FROM pgfr_record.v_pg_stat_database
    GROUP BY captured_at
    ORDER BY captured_at
"""
BLKS_READ_SERIES_SQL = """
    SELECT captured_at, sum(blks_read)::numeric
    FROM pgfr_record.v_pg_stat_database
    GROUP BY captured_at
    ORDER BY captured_at
"""
TEMP_BYTES_SERIES_SQL = """
    SELECT captured_at, sum(temp_bytes)::numeric
    FROM pgfr_record.v_pg_stat_database
    GROUP BY captured_at
    ORDER BY captured_at
"""
RATE_SOURCES = (
    ("wal_bytes_rate", "pg_stat_wal", WAL_BYTES_SERIES_SQL),
    ("tps", "pg_stat_database", TPS_SERIES_SQL),
    ("blks_read_rate", "pg_stat_database", BLKS_READ_SERIES_SQL),
    ("temp_bytes_rate", "pg_stat_database", TEMP_BYTES_SERIES_SQL),
)

COVERAGE_SQL = "SELECT cadence_tier, coverage_ratio FROM pgfr_analyze.coverage(%s, %s)"
COVERAGE_GAPS_SQL = "SELECT cadence_tier FROM pgfr_analyze.coverage_gaps(%s, %s)"


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


def _as_utc(t: dt.datetime) -> dt.datetime:
    if t.tzinfo is None:
        return t.replace(tzinfo=_UTC)
    return t.astimezone(_UTC)


def fetch_counter_series(conn: psycopg.Connection, sql: str) -> list[tuple[dt.datetime, float]]:
    with conn.cursor() as cur:
        cur.execute(sql)
        rows = cur.fetchall()
    return [(_as_utc(t), float(v)) for t, v in rows if v is not None]


def bucket_rates(
    samples: list[tuple[dt.datetime, float]], bucket_seconds: int = BUCKET_SECONDS
) -> list[tuple[dt.datetime, float]]:
    """Consecutive-sample counter deltas, reset-aware (a decreased counter
    is a reset, not a negative rate: that interval is dropped, the same rule
    pgfr's own deltas() applies), accumulated into epoch-aligned buckets as
    total delta over total elapsed seconds, i.e. a time-weighted mean rate
    per bucket. Each interval is attributed to the bucket containing its
    midpoint. Returns (bucket_start_utc, rate) sorted by time.
    """
    totals: dict[dt.datetime, list[float]] = {}
    for (prev_t, prev_v), (t, v) in zip(samples, samples[1:]):
        elapsed = (t - prev_t).total_seconds()
        if elapsed <= 0 or v < prev_v:
            continue
        midpoint = (_as_utc(prev_t).timestamp() + _as_utc(t).timestamp()) / 2
        bucket_start = dt.datetime.fromtimestamp(midpoint - midpoint % bucket_seconds, tz=_UTC)
        acc = totals.setdefault(bucket_start, [0.0, 0.0])
        acc[0] += v - prev_v
        acc[1] += elapsed
    return sorted((start, delta / seconds) for start, (delta, seconds) in totals.items())


def _trend_slope_per_day(buckets: list[tuple[dt.datetime, float]]) -> float | None:
    if len(buckets) < 2:
        return None
    origin = buckets[0][0]
    xs = [(t - origin).total_seconds() / 86400 for t, _rate in buckets]
    ys = [rate for _t, rate in buckets]
    mean_x, mean_y = statistics.fmean(xs), statistics.fmean(ys)
    sxx = sum((x - mean_x) ** 2 for x in xs)
    if sxx == 0:
        return None
    return sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys)) / sxx


def rate_summary(buckets: list[tuple[dt.datetime, float]]) -> dict:
    """Quantiles over hourly buckets (§9 value item 1: the peak operating
    point), a 24x7 seasonal profile (item 2: rows are Monday..Sunday UTC,
    columns hour-of-day 0..23, mean rate per cell, None where the window
    never covered that cell), and an ordinary least-squares trend slope in
    rate units per day."""
    rates = [rate for _t, rate in buckets]
    if not rates:
        return {
            "bucket_count": 0,
            "quantiles": None,
            "seasonal_24x7": None,
            "trend_slope_per_day": None,
        }
    if len(rates) >= 2:
        cut = statistics.quantiles(rates, n=100)
        quantiles = {"p50": cut[49], "p95": cut[94], "p99": cut[98], "max": max(rates)}
    else:
        quantiles = {"p50": rates[0], "p95": rates[0], "p99": rates[0], "max": rates[0]}

    cells: dict[tuple[int, int], list[float]] = {}
    for t, rate in buckets:
        cells.setdefault((t.weekday(), t.hour), []).append(rate)
    seasonal = [
        [statistics.fmean(cells[(dow, hour)]) if (dow, hour) in cells else None for hour in range(24)]
        for dow in range(7)
    ]
    return {
        "bucket_count": len(rates),
        "quantiles": quantiles,
        "seasonal_24x7": seasonal,
        "trend_slope_per_day": _trend_slope_per_day(buckets),
    }


def detect_batch_events(
    series: list[tuple[dt.datetime, float]],
    z_threshold: float = Z_THRESHOLD,
    *,
    min_magnitude: float = MIN_MAGNITUDE_X_BASELINE,
    bucket_seconds: int = BUCKET_SECONDS,
    metric: str | None = None,
) -> list[dict]:
    """Threshold-crossing batch/spike detector (§9 point 4), deliberately
    dumb: each bucket is scored against the whole series' median and MAD (a
    robust global baseline), flagged buckets are merged into occurrences when
    adjacent, and occurrences sharing a phase hour are folded into one
    recurring event with an inferred cadence.

    The baseline is global rather than per hour-of-day on purpose: a batch
    that runs every weekday at 13:00 *is* hour 13's baseline, so scoring it
    against other days' hour 13 (Milestone 11's first cut) can only ever
    find a one-off anomaly, never the recurring cadence §9 wants surfaced.
    The 24x7 profile in rate_summary() carries the seasonal shape itself;
    this detector's job is the discrete events on top of it. A bucket must
    also clear min_magnitude times the median, so a tiny MAD on a nearly
    flat series can't promote noise to an event.
    """
    if len(series) < 2:
        return []
    rates = [rate for _t, rate in series]
    baseline = statistics.median(rates)
    if baseline <= 0:
        return []
    mad = statistics.median(abs(rate - baseline) for rate in rates) * _MAD_TO_SIGMA

    flagged: list[tuple[dt.datetime, float]] = []
    for t, rate in series:
        if rate < min_magnitude * baseline:
            continue
        if mad > 0 and (rate - baseline) / mad < z_threshold:
            continue
        flagged.append((t, rate))
    if not flagged:
        return []

    flagged.sort(key=lambda row: row[0])
    occurrences: list[list[tuple[dt.datetime, float]]] = [[flagged[0]]]
    for row in flagged[1:]:
        gap = (row[0] - occurrences[-1][-1][0]).total_seconds()
        if 0 < gap <= bucket_seconds * 1.5:
            occurrences[-1].append(row)
        else:
            occurrences.append([row])

    by_phase: dict[int, list[list[tuple[dt.datetime, float]]]] = {}
    for occurrence in occurrences:
        by_phase.setdefault(occurrence[0][0].hour, []).append(occurrence)
    return [
        _describe_event(phase_hour, occs, baseline, bucket_seconds, metric)
        for phase_hour, occs in sorted(by_phase.items())
    ]


def _cadence(starts: list[dt.datetime]) -> str:
    if len(starts) == 1:
        return "once"
    weekdays = {t.weekday() for t in starts}
    if len(weekdays) >= 6:
        return "daily"
    if len(weekdays) == 1:
        return "weekly"
    if weekdays <= {0, 1, 2, 3, 4} and len(weekdays) >= 3:
        return "weekdays"
    if weekdays <= {5, 6}:
        return "weekends"
    return "irregular"


def _describe_event(
    phase_hour: int,
    occurrences: list[list[tuple[dt.datetime, float]]],
    baseline: float,
    bucket_seconds: int,
    metric: str | None,
) -> dict:
    starts = [occurrence[0][0] for occurrence in occurrences]
    duration_buckets = statistics.median_high(len(o) for o in occurrences)
    peak = max(rate for occurrence in occurrences for _t, rate in occurrence)
    return {
        "kind": "batch_spike",
        "metric": metric,
        "cadence": _cadence(starts),
        "phase_hour_utc": phase_hour,
        "occurrences": len(occurrences),
        "duration_buckets": duration_buckets,
        "duration_s": duration_buckets * bucket_seconds,
        "magnitude_x_baseline": peak / baseline,
        "first_seen": min(starts).isoformat(),
        "last_seen": max(starts).isoformat(),
        "evidence": [metric] if metric else [],
    }


def fetch_window_coverage(conn: psycopg.Connection, start: dt.datetime, end: dt.datetime) -> dict:
    """completeness_fraction and gap count straight from pgfr_analyze's own
    coverage()/coverage_gaps() (finding 2), for the fast tier, which is
    where every Group A rate above is captured. pgfr_analyze is optional (a
    pgfr_record-only install is valid), so its absence degrades to None
    with a note rather than failing the section (I5). coverage_ratio can
    legitimately exceed 1.0 right after a profile change; it is capped so
    completeness_fraction keeps §10's 0..1 meaning.
    """
    try:
        with conn.cursor() as cur:
            cur.execute(COVERAGE_SQL, (start, end))
            coverage_rows = cur.fetchall()
            cur.execute(COVERAGE_GAPS_SQL, (start, end))
            gap_rows = cur.fetchall()
    except psycopg.Error as exc:
        return {
            "completeness_fraction": None,
            "capture_ledger_gaps": None,
            "coverage_by_tier": {},
            "note": (
                f"pgfr_analyze coverage unavailable ({type(exc).__name__}); "
                "completeness_fraction not computed"
            ),
        }
    coverage_by_tier = {
        tier: (float(ratio) if ratio is not None else None) for tier, ratio in coverage_rows
    }
    fast = coverage_by_tier.get("fast")
    return {
        "completeness_fraction": min(1.0, fast) if fast is not None else None,
        "capture_ledger_gaps": sum(1 for (tier,) in gap_rows if tier == "fast"),
        "coverage_by_tier": coverage_by_tier,
        "note": None,
    }


def _unavailable(reason: str) -> dict:
    return {
        "available": False,
        "window": None,
        "metrics": {},
        "statement_mixture": "unavailable_in_v1",
        "events": [],
        "maintenance": "unavailable_in_v1",
        "completeness": build_completeness(available=False, reason=reason),
    }


def capture_temporal_pgfr(conn: psycopg.Connection, capabilities: Capabilities) -> dict:
    """Build the optional temporal section (§9). Returns available: False
    with a reason (I5) whenever pgfr isn't installed, isn't capturing, or
    is on a PostgreSQL version it doesn't support."""
    status = probe_pgfr(conn, capabilities)
    if not status["available"]:
        return _unavailable(status["reason"])

    metrics: dict[str, dict] = {}
    events: list[dict] = []
    notes: list[str] = []
    buckets_per_metric: dict[str, int] = {}
    window_start: dt.datetime | None = None
    window_end: dt.datetime | None = None
    sample_start: dt.datetime | None = None
    sample_end: dt.datetime | None = None

    for metric, source_view, sql in RATE_SOURCES:
        try:
            samples = fetch_counter_series(conn, sql)
        except psycopg.Error as exc:
            notes.append(f"{metric}: source view unavailable ({type(exc).__name__})")
            buckets_per_metric[metric] = 0
            continue
        buckets = bucket_rates(samples, BUCKET_SECONDS)
        buckets_per_metric[metric] = len(buckets)
        if not buckets:
            continue
        summary = rate_summary(buckets)
        summary["source_view"] = source_view
        metrics[metric] = summary
        events.extend(detect_batch_events(buckets, metric=metric, bucket_seconds=BUCKET_SECONDS))
        first, last = buckets[0][0], buckets[-1][0] + dt.timedelta(seconds=BUCKET_SECONDS)
        window_start = first if window_start is None else min(window_start, first)
        window_end = last if window_end is None else max(window_end, last)
        sample_start = samples[0][0] if sample_start is None else min(sample_start, samples[0][0])
        sample_end = samples[-1][0] if sample_end is None else max(sample_end, samples[-1][0])

    window = None
    completeness_fraction = None
    ledger_gaps = None
    if window_start is not None and window_end is not None:
        # Coverage is measured over the span of actual samples, not the
        # bucket-aligned window: the first and last buckets are only partly
        # covered by construction, and counting their overhang as missed
        # ticks would report gaps pgfr never had.
        coverage = fetch_window_coverage(conn, sample_start, sample_end)
        if coverage["note"]:
            notes.append(coverage["note"])
        completeness_fraction = coverage["completeness_fraction"]
        ledger_gaps = coverage["capture_ledger_gaps"]
        window = {
            "start": window_start.isoformat(),
            "end": window_end.isoformat(),
            "bucket_seconds": BUCKET_SECONDS,
            "source": "pgfr_v2",
            "completeness_fraction": completeness_fraction,
            "capture_ledger_gaps": ledger_gaps,
            "coverage_by_tier": coverage["coverage_by_tier"],
        }
    else:
        notes.append("no rate series available yet: pgfr has captured no Group A history")

    return {
        "available": True,
        "window": window,
        "metrics": metrics,
        "statement_mixture": "unavailable_in_v1",
        "events": events,
        "maintenance": "unavailable_in_v1",
        "completeness": build_completeness(
            available=True,
            coverage={
                "buckets_per_metric": buckets_per_metric,
                "completeness_fraction": completeness_fraction,
                "capture_ledger_gaps": ledger_gaps,
            },
            notes=notes,
        ),
    }
