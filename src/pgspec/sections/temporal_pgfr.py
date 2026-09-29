"""Optional pg_flight_recorder v2 temporal augmentation (§9).

pgfr is a consumer-only augmentation: this module only ever reads
pgfr_record's already-captured data (its presentation views and rollup
tables) and pgfr_analyze's coverage functions, never writes to either, and
applies the same I1 value-free discipline to whatever it reads as the rest
of pgspec applies to a live capture. pgfr itself is not privacy-preserving
-- it stores raw query text and raw identifiers indefinitely -- so nothing
here gets a free pass because the data is already sitting in a table
(GitHub issue #3, finding 7). Everything emitted is a rate, a quantile, a
ratio, a count, a cadence label, a UTC hour, or a queryid hash: no
identifier, no query text. Per-table figures are aggregated to quantiles
before they leave this module; relids never reach the artifact.

Temporal v1.1 (issue #3): presence/health detection (finding 9), the
PG15+ version gate (finding 8), the four Group A headline rates §9 lists
(wal_bytes_rate, tps, blks_read_rate, temp_bytes_rate) with hourly-bucket
quantiles, a 24x7 seasonal profile, and a trend slope; window completeness
from pgfr_analyze.coverage()/coverage_gaps() (finding 2); and the
batch/spike detector pgfr itself doesn't have yet (finding 5, upstream as
pg_flight_recorder issue #132).

Temporal v1.2 (issue #5): Group B per-relation counters through
pgfr_record.rollup_deltas() over the daily rollups (dead-tuple generation
rate, autovacuum cadence); the maintenance rhythm §9 lists (checkpoint
spacing from Group A, dead-tuple sawtooth amplitude from the 30-day raw
archive); a queryid-only statement-mixture time series with drift and
churn, which never selects the raw text column (findings 6 and 7 by
construction; a static test enforces it); and one Group C gauge metric,
connection concurrency from the hourly pg_stat_activity rollup, carrying
pgfr's Mode A sampling regime explicitly (finding 3).

Group A counters are read raw from the presentation views: pgfr keeps them
365 days at raw resolution with no rollup (finding 1). Their reset-aware
differencing is done here rather than through pgfr_record.deltas(), whose
SETOF-record calling convention is built for interactive psql (finding 4,
deviation ratified on issue #3). Group B goes through rollup_deltas(), the
only long-horizon source pgfr has for it.
"""

from __future__ import annotations

import datetime as dt
import re
import statistics

import psycopg
from psycopg import sql as pgsql

from pgspec.capture import Capabilities
from pgspec.completeness import build_completeness

#: pgfr v2 has a hard pg_cron dependency and targets PG15+ (issue #3
#: finding 8); below this, augmentation is categorically unavailable, not
#: just undetected, so skip the live probe entirely.
MIN_PGFR_MAJOR = 15
BUCKET_SECONDS = 3600
Z_THRESHOLD = 3.0
MIN_MAGNITUDE_X_BASELINE = 2.0
GROUP_B_WINDOW_DAYS = 28
SAWTOOTH_WINDOW_DAYS = 30
MIXTURE_WINDOW_DAYS = 14
MIXTURE_TOP_N = 50
#: Hourly, examined against PBench's 30-second finding (issue #5): at 300 s
#: the mixture series alone is 4,032 buckets x top_n floats, roughly the
#: whole rest of the artifact again; at 3600 s it is 336 x top_n.
MIXTURE_BUCKET_SECONDS = 3600
CONCURRENCY_WINDOW_DAYS = 28
_MAD_TO_SIGMA = 1.4826
_UTC = dt.timezone.utc
_IDENTIFIER = re.compile(r"^[a-z_][a-z0-9_]*$")
_TYPE_NAME = re.compile(r"^[a-z_][a-z0-9_ ]*(\[\])?$")
_EXCLUDED_SCHEMAS = ["pg_catalog", "information_schema", "pgfr_record", "pgfr_analyze"]

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

# Checkpoint counters moved from pg_stat_bgwriter to pg_stat_checkpointer in
# PG17; pgfr captures whichever the server has.
CHECKPOINTS_BGWRITER_SQL = """
    SELECT captured_at,
           (checkpoints_timed + checkpoints_req)::numeric,
           checkpoints_timed::numeric
    FROM pgfr_record.v_pg_stat_bgwriter
    ORDER BY captured_at
"""
CHECKPOINTS_CHECKPOINTER_SQL = """
    SELECT captured_at,
           (num_timed + num_requested)::numeric,
           num_timed::numeric
    FROM pgfr_record.v_pg_stat_checkpointer
    ORDER BY captured_at
"""

COVERAGE_SQL = "SELECT cadence_tier, coverage_ratio FROM pgfr_analyze.coverage(%s, %s)"
COVERAGE_GAPS_SQL = "SELECT cadence_tier FROM pgfr_analyze.coverage_gaps(%s, %s)"

# Group B: user relations only (pgfr's own schemas are captured as free
# health telemetry and would otherwise dominate a quiet database).
USER_RELIDS_SQL = """
    SELECT DISTINCT relid
    FROM pgfr_record.v_pg_stat_all_tables
    WHERE captured_at >= %s
      AND schemaname <> ALL(%s)
      AND schemaname NOT LIKE 'pg\\_toast%%'
"""
SAWTOOTH_SQL = """
    SELECT max(n_dead_tup) - min(n_dead_tup) AS amplitude, max(n_live_tup) AS live_peak
    FROM pgfr_record.v_pg_stat_all_tables
    WHERE captured_at >= %s
      AND schemaname <> ALL(%s)
      AND schemaname NOT LIKE 'pg\\_toast%%'
    GROUP BY relid
"""
ROLLUP_BUCKET_RANGE_SQL = """
    SELECT min(bucket_start), max(bucket_start)
    FROM pgfr_record.r_pg_stat_all_tables
    WHERE bucket_start >= %s
"""
MANIFEST_SQL = """
    SELECT natural_key, rollup_granularity
    FROM pgfr_record.manifest
    WHERE source_view = %s
"""
CAPTURE_COLUMNS_SQL = """
    SELECT u.col, u.typ, cc.column_name IS NOT NULL
    FROM pgfr_record.payload_schemas ps
    JOIN unnest(ps.columns, ps.type_names) WITH ORDINALITY AS u(col, typ, ord) ON true
    LEFT JOIN pgfr_record.column_classes cc
           ON cc.source_view = ps.source_view
          AND cc.column_name = u.col
          AND cc.class IN ('counter', 'odometer')
    WHERE ps.source_view = %s AND ps.kind = 'capture'
      AND ps.schema_id = (SELECT max(schema_id) FROM pgfr_record.payload_schemas
                          WHERE source_view = %s AND kind = 'capture')
    ORDER BY u.ord
"""

# Statement mixture: queryids and call counters only. The raw text column
# of this view is never selected (tests/test_no_forbidden_sql.py).
MIXTURE_TOP_SQL = """
    SELECT queryid, sum(delta) AS calls
    FROM (
        SELECT userid, dbid, toplevel, queryid, max(calls) - min(calls) AS delta
        FROM pgfr_record.v_pg_stat_statements
        WHERE captured_at >= %s
        GROUP BY userid, dbid, toplevel, queryid
    ) AS per_key
    GROUP BY queryid
    ORDER BY calls DESC, queryid
    LIMIT %s
"""
MIXTURE_SERIES_SQL = """
    SELECT userid, dbid, toplevel, queryid,
           to_timestamp(floor(extract(epoch FROM captured_at) / %s) * %s) AS bucket_start,
           max(calls)
    FROM pgfr_record.v_pg_stat_statements
    WHERE captured_at >= %s AND queryid = ANY(%s)
    GROUP BY userid, dbid, toplevel, queryid, bucket_start
    ORDER BY bucket_start
"""

# Group C: the hourly pg_stat_activity rollup (stat-shaped: one row per
# bucket and stat) plus the fast tier's tick count per hour, which turns
# "active backend-samples" into "mean active backends per sample".
CONCURRENCY_SQL = """
    SELECT bucket_start, value, sample_count
    FROM pgfr_record.r_pg_stat_activity
    WHERE stat_name = 'state_active' AND bucket_start >= %s
    ORDER BY bucket_start
"""
FAST_TICKS_SQL = """
    SELECT to_timestamp(floor(extract(epoch FROM captured_at) / %s) * %s) AS bucket_start,
           count(*)
    FROM pgfr_record.ledger_runs
    WHERE tier = 'fast' AND captured_at >= %s
    GROUP BY bucket_start
"""


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


def _now() -> dt.datetime:
    return dt.datetime.now(_UTC)


def _floor_to_bucket(t: dt.datetime, bucket_seconds: int) -> dt.datetime:
    epoch = _as_utc(t).timestamp()
    return dt.datetime.fromtimestamp(epoch - epoch % bucket_seconds, tz=_UTC)


def _fetch(conn: psycopg.Connection, query, params=()) -> list[tuple]:
    with conn.cursor() as cur:
        cur.execute(query, params)
        return cur.fetchall()


def fetch_counter_series(conn: psycopg.Connection, sql: str) -> list[tuple[dt.datetime, float]]:
    return [(_as_utc(t), float(v)) for t, v in _fetch(conn, sql) if v is not None]


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


def _counter_total(samples: list[tuple[dt.datetime, float]]) -> float:
    """Sum of positive consecutive deltas: the counter's growth over the
    series, ignoring resets."""
    return sum(v - prev_v for (_pt, prev_v), (_t, v) in zip(samples, samples[1:]) if v >= prev_v)


def quantile_summary(values: list[float]) -> dict | None:
    """p50/p95/p99/max plus the sample count. The inclusive method
    interpolates within the observed range, so a small sample can never
    report a p95 above its own max the way the default (exclusive) method's
    extrapolation does."""
    if not values:
        return None
    if len(values) == 1:
        return {"p50": values[0], "p95": values[0], "p99": values[0], "max": values[0], "n": 1}
    cut = statistics.quantiles(values, n=100, method="inclusive")
    return {"p50": cut[49], "p95": cut[94], "p99": cut[98], "max": max(values), "n": len(values)}


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
    quantiles = quantile_summary(rates)
    quantiles.pop("n")

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
        coverage_rows = _fetch(conn, COVERAGE_SQL, (start, end))
        gap_rows = _fetch(conn, COVERAGE_GAPS_SQL, (start, end))
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


def _safe_identifier(name: str) -> str:
    if not _IDENTIFIER.match(name):
        raise ValueError(f"unexpected pgfr column name: {name!r}")
    return name


def _safe_type(type_name: str) -> str:
    if not _TYPE_NAME.match(type_name):
        raise ValueError(f"unexpected pgfr type name: {type_name!r}")
    return type_name


def rollup_column_definitions(
    natural_key: list[str], columns: list[tuple[str, str, bool]]
) -> list[str]:
    """Column-definition list for pgfr_record.rollup_deltas(), which is
    SETOF record: the key columns in payload order, then one <col>_delta
    per counter/odometer column, then the two bucket bounds, mirroring the
    function's own SELECT. Built from pgfr's catalog metadata
    (payload_schemas plus column_classes) rather than a hard-coded list, so
    a PG-major column change can't silently misalign the record; every name
    and type is validated before it is spliced into SQL."""
    definitions = [
        f"{_safe_identifier(name)} {_safe_type(type_name)}"
        for name, type_name, _is_counter in columns
        if name in natural_key
    ]
    definitions += [
        f"{_safe_identifier(name)}_delta {_safe_type(type_name)}"
        for name, type_name, is_counter in columns
        if is_counter
    ]
    return [*definitions, "from_bucket timestamptz", "to_bucket timestamptz"]


def fetch_rollup_deltas(
    conn: psycopg.Connection, source_view: str, from_bucket: dt.datetime, to_bucket: dt.datetime
) -> tuple[list[str], list[tuple], dt.timedelta]:
    """One reset-aware delta per key between two rollup buckets, via pgfr's
    own rollup_deltas(). Returns (column names, rows, rollup granularity)."""
    (natural_key, granularity), = _fetch(conn, MANIFEST_SQL, (source_view,))
    columns = _fetch(conn, CAPTURE_COLUMNS_SQL, (source_view, source_view))
    definitions = rollup_column_definitions(
        list(natural_key or []), [(name, typ, bool(flag)) for name, typ, flag in columns]
    )
    query = pgsql.SQL("SELECT * FROM pgfr_record.rollup_deltas(%s, %s, %s) AS d({})").format(
        pgsql.SQL(", ").join(pgsql.SQL(definition) for definition in definitions)
    )
    rows = _fetch(conn, query, (source_view, from_bucket, to_bucket))
    return [definition.split(" ", 1)[0] for definition in definitions], rows, granularity


def locf_bucket_deltas(
    rows: list[tuple[object, dt.datetime, float]], bucket_starts: list[dt.datetime]
) -> dict[object, list[float]]:
    """Per-key counter deltas on a bucket grid from pgfr's debounced rows,
    where each row is (key, bucket_start, max cumulative value seen in that
    bucket). A bucket with no row for a key carries the last value forward
    (pgfr's debounce: no row means no change); the first observation has no
    prior to diff against; a decrease is a reset and contributes nothing.
    Rows outside the grid are ignored."""
    index = {bucket: i for i, bucket in enumerate(bucket_starts)}
    observed: dict[object, dict[int, float]] = {}
    for key, bucket, value in rows:
        i = index.get(bucket)
        if i is None:
            continue
        by_index = observed.setdefault(key, {})
        by_index[i] = max(value, by_index.get(i, value))

    deltas: dict[object, list[float]] = {}
    for key, by_index in observed.items():
        series = [0.0] * len(bucket_starts)
        last: float | None = None
        for i in range(len(bucket_starts)):
            if i in by_index:
                value = by_index[i]
                if last is not None and value >= last:
                    series[i] = value - last
                last = value
        deltas[key] = series
    return deltas


def _jaccard_distance(a: set, b: set) -> float:
    if not a and not b:
        return 0.0
    return 1.0 - len(a & b) / len(a | b)


def mixture_summary(
    per_queryid: dict[object, list[float]], bucket_starts: list[dt.datetime], bucket_seconds: int
) -> dict:
    """Statement mixture over a bucket grid (§9 value item 3): each
    queryid's share of the calls made by the considered statements per
    bucket, plus three churn figures. share_drift_mean_l1 is the mean L1
    distance between consecutive buckets' share vectors (0 stable, 2 a
    complete swap); set_churn_day_over_day is the mean Jaccard distance
    between consecutive days' active sets; churn_week_over_week is the
    Jaccard distance between the last two weeks' active sets, None with
    fewer than 14 days. Shares are rounded to 4 decimals (§13.4)."""
    n = len(bucket_starts)
    order = sorted(per_queryid, key=lambda q: (-sum(per_queryid[q]), str(q)))
    totals = [sum(per_queryid[q][i] for q in order) for i in range(n)]
    shares = {
        q: [(per_queryid[q][i] / totals[i]) if totals[i] > 0 else 0.0 for i in range(n)]
        for q in order
    }

    l1 = [
        sum(abs(shares[q][i] - shares[q][i - 1]) for q in order)
        for i in range(1, n)
        if totals[i] > 0 and totals[i - 1] > 0
    ]
    per_day = max(1, 86400 // bucket_seconds)
    days = n // per_day
    day_sets = [
        {q for q in order if sum(per_queryid[q][d * per_day : (d + 1) * per_day]) > 0}
        for d in range(days)
    ]
    day_over_day = [_jaccard_distance(day_sets[d - 1], day_sets[d]) for d in range(1, days)]
    week_over_week = None
    if days >= 14:
        previous = set().union(*day_sets[days - 14 : days - 7])
        latest = set().union(*day_sets[days - 7 : days])
        week_over_week = _jaccard_distance(previous, latest)
    return {
        "bucket_starts": [bucket.isoformat() for bucket in bucket_starts],
        "series": [{"queryid": q, "shares": [round(s, 4) for s in shares[q]]} for q in order],
        "share_drift_mean_l1": statistics.fmean(l1) if l1 else 0.0,
        "set_churn_day_over_day": statistics.fmean(day_over_day) if day_over_day else 0.0,
        "churn_week_over_week": week_over_week,
    }


def checkpoint_interval_summary(
    buckets: list[tuple[dt.datetime, float]], timed_total: float | None, total: float | None
) -> dict:
    """Checkpoint spacing (§9 value item 5) from hourly checkpoint rates:
    the mean interval within each bucket that had at least one checkpoint,
    summarized as quantiles, plus the share of checkpoints that were timed
    rather than requested."""
    intervals = [1.0 / rate for _t, rate in buckets if rate > 0]
    return {
        "interval_s": quantile_summary(intervals),
        "timed_fraction": (timed_total / total) if total else None,
        "buckets_observed": len(buckets),
    }


def mean_active_series(
    rows: list[tuple[dt.datetime, float, int]], ticks_by_bucket: dict[dt.datetime, int]
) -> tuple[list[tuple[dt.datetime, float]], dict]:
    """Mean active backends per fast-tier sample, per hourly bucket, from
    pgfr's stat-shaped pg_stat_activity rollup (state_active counts active
    backend-samples over the bucket) divided by the fast tier's tick count
    in that bucket. This is a Mode A sampled estimate (pgfr STATISTICS.md,
    issue #3 finding 3), so the sampling regime travels with it."""
    series: list[tuple[dt.datetime, float]] = []
    ticks_seen: list[int] = []
    for bucket, value, _sample_count in rows:
        ticks = ticks_by_bucket.get(bucket)
        if not ticks:
            continue
        series.append((bucket, float(value) / ticks))
        ticks_seen.append(ticks)
    sampling = {
        "regime": "mode_a_sampled",
        "interval_s": (BUCKET_SECONDS / statistics.median(ticks_seen)) if ticks_seen else None,
        "samples_per_bucket_median": int(statistics.median_high(ticks_seen)) if ticks_seen else None,
        "detection_probability": (
            "about duration / interval_s for a state shorter than interval_s "
            "(pgfr STATISTICS.md, Mode A)"
        ),
    }
    return series, sampling


def _group_b(conn: psycopg.Connection, notes: list[str]) -> tuple[dict | None, dict | None]:
    """dead_tup_growth (per-table dead-tuple generation rate) and the
    autovacuum cadence quantiles, from one rollup_deltas() call spanning the
    closed daily buckets in the window."""
    since = _now() - dt.timedelta(days=GROUP_B_WINDOW_DAYS)
    (from_bucket, to_bucket), = _fetch(conn, ROLLUP_BUCKET_RANGE_SQL, (since,))
    if from_bucket is None:
        notes.append("Group B: no closed pg_stat_all_tables rollup buckets in the window")
        return None, None
    user_relids = {relid for (relid,) in _fetch(conn, USER_RELIDS_SQL, (since, _EXCLUDED_SCHEMAS))}
    names, rows, granularity = fetch_rollup_deltas(
        conn, "pg_catalog.pg_stat_all_tables", from_bucket, to_bucket
    )
    index = {name: i for i, name in enumerate(names)}
    needed = ("relid", "n_tup_upd_delta", "n_tup_del_delta", "autovacuum_count_delta")
    missing = [name for name in needed if name not in index]
    if missing:
        notes.append(f"Group B: rollup_deltas output lacks {', '.join(missing)}")
        return None, None
    elapsed_s = (_as_utc(to_bucket) + granularity - _as_utc(from_bucket)).total_seconds()
    if elapsed_s <= 0:
        return None, None

    growth_rates: list[float] = []
    autovacuums_per_day: list[float] = []
    for row in rows:
        if row[index["relid"]] not in user_relids:
            continue
        updated, deleted = row[index["n_tup_upd_delta"]], row[index["n_tup_del_delta"]]
        if updated is not None and deleted is not None:
            growth_rates.append((float(updated) + float(deleted)) / elapsed_s)
        autovacuums = row[index["autovacuum_count_delta"]]
        if autovacuums is not None:
            autovacuums_per_day.append(float(autovacuums) / (elapsed_s / 86400))

    window = {
        "from_bucket": _as_utc(from_bucket).isoformat(),
        "to_bucket": _as_utc(to_bucket).isoformat(),
        "bucket_seconds": int(granularity.total_seconds()),
        "window_days": elapsed_s / 86400,
    }
    dead_tup_growth = {
        "quantiles": quantile_summary(growth_rates),
        "unit": "dead tuples generated per second per table (n_tup_upd + n_tup_del)",
        "tables_observed": len(growth_rates),
        "source": "rollup_deltas(pg_catalog.pg_stat_all_tables)",
        **window,
    }
    return dead_tup_growth, quantile_summary(autovacuums_per_day)


def _sawtooth(conn: psycopg.Connection) -> dict | None:
    since = _now() - dt.timedelta(days=SAWTOOTH_WINDOW_DAYS)
    ratios = [
        float(amplitude) / float(live_peak)
        for amplitude, live_peak in _fetch(conn, SAWTOOTH_SQL, (since, _EXCLUDED_SCHEMAS))
        if amplitude is not None and live_peak
    ]
    return quantile_summary(ratios)


def _checkpoints(conn: psycopg.Connection, capabilities: Capabilities) -> dict:
    sql = CHECKPOINTS_CHECKPOINTER_SQL if capabilities.server_major >= 17 else CHECKPOINTS_BGWRITER_SQL
    rows = _fetch(conn, sql)
    total_series = [(_as_utc(t), float(total)) for t, total, _timed in rows if total is not None]
    timed_series = [(_as_utc(t), float(timed)) for t, _total, timed in rows if timed is not None]
    return checkpoint_interval_summary(
        bucket_rates(total_series, BUCKET_SECONDS),
        _counter_total(timed_series),
        _counter_total(total_series),
    )


def _statement_mixture(conn: psycopg.Connection, top_n: int) -> dict:
    now = _now()
    start = _floor_to_bucket(now, MIXTURE_BUCKET_SECONDS) - dt.timedelta(days=MIXTURE_WINDOW_DAYS)
    grid = [
        start + dt.timedelta(seconds=i * MIXTURE_BUCKET_SECONDS)
        for i in range(MIXTURE_WINDOW_DAYS * 86400 // MIXTURE_BUCKET_SECONDS)
    ]
    queryids = [queryid for queryid, _calls in _fetch(conn, MIXTURE_TOP_SQL, (start, top_n))]
    # Every considered statement gets a series, all zeros if its only rows
    # fall in the current, still-open bucket, so len(series) always equals
    # statements_considered.
    per_queryid: dict[object, list[float]] = {queryid: [0.0] * len(grid) for queryid in queryids}
    if queryids:
        rows = _fetch(
            conn,
            MIXTURE_SERIES_SQL,
            (MIXTURE_BUCKET_SECONDS, MIXTURE_BUCKET_SECONDS, start, queryids),
        )
        keyed = [
            ((userid, dbid, toplevel, queryid), _as_utc(bucket), float(calls))
            for userid, dbid, toplevel, queryid, bucket, calls in rows
            if calls is not None
        ]
        for (_u, _d, _t, queryid), deltas in locf_bucket_deltas(keyed, grid).items():
            acc = per_queryid.setdefault(queryid, [0.0] * len(grid))
            for i, delta in enumerate(deltas):
                acc[i] += delta
    summary = mixture_summary(per_queryid, grid, MIXTURE_BUCKET_SECONDS)
    summary.update(
        {
            "top_n": top_n,
            "statements_considered": len(queryids),
            "window_days": MIXTURE_WINDOW_DAYS,
            "share_timeseries_bucket_seconds": MIXTURE_BUCKET_SECONDS,
        }
    )
    return summary


def _concurrency(conn: psycopg.Connection) -> dict | None:
    since = _now() - dt.timedelta(days=CONCURRENCY_WINDOW_DAYS)
    rows = [
        (_as_utc(bucket), float(value), int(sample_count or 0))
        for bucket, value, sample_count in _fetch(conn, CONCURRENCY_SQL, (since,))
        if value is not None
    ]
    ticks = {
        _as_utc(bucket): int(count)
        for bucket, count in _fetch(conn, FAST_TICKS_SQL, (BUCKET_SECONDS, BUCKET_SECONDS, since))
    }
    series, sampling = mean_active_series(rows, ticks)
    if not series:
        return None
    summary = rate_summary(series)
    summary.update(
        {
            "unit": "mean active backends per sample",
            "source": "r_pg_stat_activity",
            "window_days": CONCURRENCY_WINDOW_DAYS,
            "sampling": sampling,
        }
    )
    return summary


def _unavailable(reason: str) -> dict:
    return {
        "available": False,
        "window": None,
        "metrics": {},
        "statement_mixture": None,
        "events": [],
        "maintenance": None,
        "completeness": build_completeness(available=False, reason=reason),
    }


def capture_temporal_pgfr(
    conn: psycopg.Connection, capabilities: Capabilities, *, mixture_top_n: int = MIXTURE_TOP_N
) -> dict:
    """Build the optional temporal section (§9). Returns available: False
    with a reason (I5) whenever pgfr isn't installed, isn't capturing, or
    is on a PostgreSQL version it doesn't support. Each block past the
    Group A rates degrades independently to None plus a note, since a
    pgfr_record-only or freshly installed pgfr can legitimately lack any of
    them."""
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

    maintenance: dict = {
        "autovacuum_events_per_day_by_table_quantiles": None,
        "checkpoint_interval_quantiles": None,
        "dead_tuple_sawtooth_amplitude_quantiles": None,
        "window_days": GROUP_B_WINDOW_DAYS,
    }
    try:
        dead_tup_growth, autovacuum = _group_b(conn, notes)
        if dead_tup_growth is not None:
            metrics["dead_tup_growth"] = dead_tup_growth
        maintenance["autovacuum_events_per_day_by_table_quantiles"] = autovacuum
    except (psycopg.Error, ValueError) as exc:
        notes.append(f"Group B rollups unavailable ({type(exc).__name__})")
    try:
        maintenance["dead_tuple_sawtooth_amplitude_quantiles"] = _sawtooth(conn)
    except psycopg.Error as exc:
        notes.append(f"dead-tuple sawtooth unavailable ({type(exc).__name__})")
    try:
        maintenance["checkpoint_interval_quantiles"] = _checkpoints(conn, capabilities)
    except psycopg.Error as exc:
        notes.append(f"checkpoint spacing unavailable ({type(exc).__name__})")

    statement_mixture = None
    try:
        statement_mixture = _statement_mixture(conn, mixture_top_n)
    except psycopg.Error as exc:
        notes.append(f"statement mixture unavailable ({type(exc).__name__})")

    try:
        concurrency = _concurrency(conn)
        if concurrency is not None:
            metrics["connection_concurrency"] = concurrency
        else:
            notes.append("connection_concurrency: no pg_stat_activity rollup buckets with fast-tier ticks")
    except psycopg.Error as exc:
        notes.append(f"connection_concurrency unavailable ({type(exc).__name__})")

    return {
        "available": True,
        "window": window,
        "metrics": metrics,
        "statement_mixture": statement_mixture,
        "events": events,
        "maintenance": maintenance,
        "completeness": build_completeness(
            available=True,
            coverage={
                "buckets_per_metric": buckets_per_metric,
                "completeness_fraction": completeness_fraction,
                "capture_ledger_gaps": ledger_gaps,
                "mixture_statements_considered": (
                    statement_mixture["statements_considered"] if statement_mixture else None
                ),
            },
            notes=notes,
        ),
    }
