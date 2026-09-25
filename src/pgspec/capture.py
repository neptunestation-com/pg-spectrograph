"""Capture orchestration: connection handling, capability probing, and (later)
the two-sample loop.

Every connection this module opens is read-only and bounded per I3:
`default_transaction_read_only = on`, a `statement_timeout`, and a
`lock_timeout`, set once at connect time.
"""

from __future__ import annotations

import datetime as dt
import decimal
import math
import time
from dataclasses import dataclass, field

import psycopg

from pgspec import __version__
from pgspec.pseudonym import PseudonymMap, load_or_create_salt, write_map_file

# NOTE: the section modules are imported lazily, inside capture_point()
# below, not here at module load time. sections/instance.py imports
# Capabilities back from this module (it declares the capability object
# sections are handed), so importing section modules at the top of this
# file would be a circular import: Python would still be executing this
# module's own top-level statements (this file) when instance.py tried to
# import Capabilities from it. By the time capture_point() actually runs,
# this module is already fully loaded, so the same imports succeed fine.

SIGNATURE_VERSION = "1.0"

#: Postgres pg_settings unit strings this module knows how to convert to
#: bytes (only the ones relevant to memory-shaped GUCs like shared_buffers).
_BYTE_UNIT_MULTIPLIERS = {
    "B": 1,
    "kB": 1024,
    "8kB": 8192,
    "MB": 1024**2,
    "GB": 1024**3,
    "TB": 1024**4,
}


@dataclass
class Capabilities:
    """The result of the one capability probe run at connect time (§11.4).
    Sections declare what they need against this rather than re-probing."""

    server_version_num: int
    is_in_recovery: bool
    extensions: dict[str, str] = field(default_factory=dict)

    @property
    def server_major(self) -> int:
        return self.server_version_num // 10000


def _logging_cursor_factory(query_log: list[str]) -> type[psycopg.Cursor]:
    class LoggingCursor(psycopg.Cursor):
        def execute(self, query, params=None, **kwargs):
            text = query if isinstance(query, str) else query.as_string(self)
            query_log.append(text)
            return super().execute(query, params, **kwargs)

    return LoggingCursor


def connect(
    dsn: str,
    *,
    statement_timeout_ms: int = 30_000,
    lock_timeout_ms: int = 1_000,
    query_log: list[str] | None = None,
) -> psycopg.Connection:
    """Open a read-only, bounded session (I3).

    Autocommit is used deliberately: pgspec issues one statement at a time
    and never needs a long-held transaction (I3's "at most one query at a
    time"), and it means one failing statement (an absent version-gated
    view, a denied privilege) can't abort a later one on the same
    connection -- each statement fails or succeeds independently.

    When `query_log` is given, every statement executed on this connection
    is appended to it verbatim, for the read-only wire-log test (§12).
    """
    kwargs = {}
    if query_log is not None:
        kwargs["cursor_factory"] = _logging_cursor_factory(query_log)
    conn = psycopg.connect(dsn, autocommit=True, **kwargs)
    with conn.cursor() as cur:
        cur.execute("SET default_transaction_read_only = on")
        cur.execute(f"SET statement_timeout = {int(statement_timeout_ms)}")
        cur.execute(f"SET lock_timeout = {int(lock_timeout_ms)}")
    return conn


def probe_capabilities(conn: psycopg.Connection) -> Capabilities:
    """The one capability probe run at connect time (§11.4): server version,
    replica status, and extension inventory. Sections declare required
    capabilities against this rather than re-probing themselves."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT current_setting('server_version_num')::int, pg_is_in_recovery()"
        )
        version_num, is_in_recovery = cur.fetchone()
        cur.execute("SELECT extname, extversion FROM pg_extension")
        extensions = dict(cur.fetchall())
    return Capabilities(
        server_version_num=version_num,
        is_in_recovery=is_in_recovery,
        extensions=extensions,
    )


def _pg_setting_bytes(setting: str | None, unit: str | None) -> int | None:
    """Convert a pg_settings (setting, unit) pair to bytes, when the unit is
    byte-shaped (used for the derived section's crude working-set bound
    against shared_buffers). Returns None for non-byte units (e.g. "ms") or
    an unset value."""
    if setting is None or unit is None:
        return None
    multiplier = _BYTE_UNIT_MULTIPLIERS.get(unit)
    if multiplier is None:
        return None
    try:
        return int(setting) * multiplier
    except ValueError:
        return None


def _normalize_for_json(obj):
    """Recursively normalize a captured structure into pure JSON-native
    types: round every float (and Decimal, e.g. pg_stat_statements.wal_bytes,
    which psycopg decodes as Decimal, not float) to 6 significant digits per
    §5's serialization rule, and stringify every datetime/date/time (e.g.
    stats_reset, last_analyze come straight from psycopg as real datetime
    objects, never strings) to ISO-8601 text. The function returned from
    capture_point() is fully JSON-safe on its own; write_artifact doesn't
    need a json.dump `default` fallback for anything this misses."""
    if isinstance(obj, bool):
        return obj
    if isinstance(obj, (float, decimal.Decimal)):
        value = float(obj)
        if value == 0 or not math.isfinite(value):
            return value
        return float(f"{value:.6g}")
    if isinstance(obj, (dt.datetime, dt.date, dt.time)):
        return obj.isoformat()
    if isinstance(obj, dict):
        return {k: _normalize_for_json(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_normalize_for_json(v) for v in obj]
    return obj


def _write_merged_pseudonym_map(
    salt: bytes, *identifier_maps: dict[str, str], map_path: str
) -> str:
    """Merge every section's own real-name -> pseudonym map into one, for
    the local-only map file (§7.5), and write it. The artifact itself only
    ever carries the returned digest, never the map. Shared by capture_point
    and capture_two_sample so a future fix to this merge logic can't be
    applied to one and forgotten in the other."""
    full_identifier_map: dict[str, str] = {}
    for identifier_map in identifier_maps:
        full_identifier_map.update(identifier_map)
    pmap = PseudonymMap(
        salt=salt,
        mapping={pseudonym: name for name, pseudonym in full_identifier_map.items()},
    )
    return write_map_file(pmap, map_path)


def capture_point(
    dsn: str,
    *,
    top_k: int = 500,
    salt_file: str = ".pgspec-salt",
    map_path: str = "spectrum-map.json",
) -> dict:
    """Point-in-time capture (§5): all seven sections plus derived, assembled
    into the top-level artifact shape. Two-sample and pgfr capture modes
    build on top of this at Milestones 9 and 11.
    """
    from pgspec.sections.activity import capture_activity
    from pgspec.sections.column_stats import capture_column_stats
    from pgspec.sections.derived import compute_derived
    from pgspec.sections.indexes import capture_indexes
    from pgspec.sections.instance import capture_instance
    from pgspec.sections.schema import capture_schema
    from pgspec.sections.workload import capture_workload

    start = time.monotonic()
    salt = load_or_create_salt(salt_file)

    conn = connect(dsn)
    try:
        capabilities = probe_capabilities(conn)
        instance_section = capture_instance(conn, capabilities)
        schema_result = capture_schema(conn, salt)
        column_stats_section = capture_column_stats(conn, schema_result.identifier_map)
        indexes_result = capture_indexes(conn, schema_result.identifier_map, salt)
        workload_section = capture_workload(
            conn, schema_result.identifier_map, top_k=top_k
        )
        activity_result = capture_activity(conn, schema_result.identifier_map, salt)
    finally:
        conn.close()

    shared_buffers_setting = instance_section["guc_snapshot"].get("shared_buffers", {})
    shared_buffers_bytes = _pg_setting_bytes(
        shared_buffers_setting.get("setting"), shared_buffers_setting.get("unit")
    )

    derived_section = compute_derived(
        schema_section=schema_result.section,
        indexes_section=indexes_result.section,
        workload_section=workload_section,
        activity_section=activity_result.section,
        shared_buffers_bytes=shared_buffers_bytes,
    )

    pseudonym_map_digest = _write_merged_pseudonym_map(
        salt,
        schema_result.identifier_map,
        indexes_result.identifier_map,
        activity_result.identifier_map,
        map_path=map_path,
    )

    duration_s = time.monotonic() - start

    artifact = {
        "signature_version": SIGNATURE_VERSION,
        "captured_at": dt.datetime.now(dt.timezone.utc)
        .isoformat()
        .replace("+00:00", "Z"),
        "capture_mode": "point",
        "capture_duration_s": round(duration_s, 3),
        "extractor": {"name": "pgspec", "version": __version__},
        "instance": instance_section,
        "schema": schema_result.section,
        "column_stats": column_stats_section,
        "indexes": indexes_result.section,
        "workload": workload_section,
        "activity": activity_result.section,
        "derived": derived_section,
        "temporal": None,
        "pseudonym_map_digest": f"sha256:{pseudonym_map_digest}",
        "warnings": [],
    }
    return _normalize_for_json(artifact)


def capture_two_sample(
    dsn: str,
    *,
    interval_s: int = 900,
    top_k: int = 500,
    salt_file: str = ".pgspec-salt",
    map_path: str = "spectrum-map.json",
) -> dict:
    """Two-sample capture (§8): a narrow counter snapshot now (sample A: the
    activity section plus the counter subset of indexes/workload), a full
    point-in-time capture --interval seconds later (sample B), and
    reset-aware per-second rates between them. Schema is captured once,
    before sample A, and reused for both samples so pseudonym assignment
    stays identical across the whole two-sample window.
    """
    from pgspec.sections.activity import capture_activity
    from pgspec.sections.column_stats import capture_column_stats
    from pgspec.sections.derived import compute_derived
    from pgspec.sections.indexes import capture_indexes
    from pgspec.sections.instance import capture_instance
    from pgspec.sections.schema import capture_schema
    from pgspec.sections.workload import capture_workload
    from pgspec.two_sample import (
        compute_activity_rates,
        compute_indexes_rates,
        compute_workload_rates,
    )

    start = time.monotonic()
    salt = load_or_create_salt(salt_file)

    conn = connect(dsn)
    try:
        schema_result = capture_schema(conn, salt)
        sample_a_activity = capture_activity(conn, schema_result.identifier_map, salt)
        sample_a_indexes = capture_indexes(conn, schema_result.identifier_map, salt)
        sample_a_workload = capture_workload(
            conn, schema_result.identifier_map, top_k=top_k
        )
    finally:
        conn.close()
    sample_a_time = dt.datetime.now(dt.timezone.utc)

    time.sleep(interval_s)

    conn = connect(dsn)
    try:
        capabilities = probe_capabilities(conn)
        instance_section = capture_instance(conn, capabilities)
        column_stats_section = capture_column_stats(conn, schema_result.identifier_map)
        sample_b_indexes = capture_indexes(conn, schema_result.identifier_map, salt)
        sample_b_workload = capture_workload(
            conn, schema_result.identifier_map, top_k=top_k
        )
        sample_b_activity = capture_activity(conn, schema_result.identifier_map, salt)
    finally:
        conn.close()
    sample_b_time = dt.datetime.now(dt.timezone.utc)

    interval_seconds = (sample_b_time - sample_a_time).total_seconds()

    activity_rates = compute_activity_rates(
        sample_a_activity.section, sample_b_activity.section, interval_seconds
    )
    indexes_rates = compute_indexes_rates(
        sample_a_indexes.section, sample_b_indexes.section, interval_seconds
    )
    workload_rates = compute_workload_rates(
        sample_a_workload, sample_b_workload, interval_seconds
    )

    # §8: high eviction churn means pg_stat_statements.max is too small for
    # this workload and coverage numbers are optimistic -- surfaced in the
    # workload section's own completeness, not just the standalone rates.
    workload_section = dict(sample_b_workload)
    workload_completeness = dict(workload_section["completeness"])
    workload_coverage = dict(workload_completeness.get("coverage", {}))
    workload_coverage["evicted_queryids_count"] = len(workload_rates["evicted_queryids"])
    workload_coverage["new_queryids_count"] = len(workload_rates["new_queryids"])
    workload_completeness["coverage"] = workload_coverage
    workload_section["completeness"] = workload_completeness

    shared_buffers_setting = instance_section["guc_snapshot"].get("shared_buffers", {})
    shared_buffers_bytes = _pg_setting_bytes(
        shared_buffers_setting.get("setting"), shared_buffers_setting.get("unit")
    )

    # Rates feed derived preferentially over lifetime averages (§8): the
    # rate dicts share the same key shapes as the raw sections (§two_sample
    # module docstring), so compute_derived() consumes them as drop-in
    # replacements with no changes needed there.
    derived_section = compute_derived(
        schema_section=schema_result.section,
        indexes_section=indexes_rates,
        workload_section=workload_rates,
        activity_section=activity_rates,
        shared_buffers_bytes=shared_buffers_bytes,
    )

    pseudonym_map_digest = _write_merged_pseudonym_map(
        salt,
        schema_result.identifier_map,
        sample_b_indexes.identifier_map,
        sample_b_activity.identifier_map,
        map_path=map_path,
    )

    duration_s = time.monotonic() - start

    artifact = {
        "signature_version": SIGNATURE_VERSION,
        "captured_at": sample_b_time.isoformat().replace("+00:00", "Z"),
        "capture_mode": "two_sample",
        "capture_duration_s": round(duration_s, 3),
        "extractor": {"name": "pgspec", "version": __version__},
        "instance": instance_section,
        "schema": schema_result.section,
        "column_stats": column_stats_section,
        "indexes": sample_b_indexes.section,
        "workload": workload_section,
        "activity": sample_b_activity.section,
        "derived": derived_section,
        "temporal": None,
        "interval": {
            "start": sample_a_time.isoformat().replace("+00:00", "Z"),
            "end": sample_b_time.isoformat().replace("+00:00", "Z"),
            "seconds": round(interval_seconds, 3),
        },
        "rates": {
            "activity": activity_rates,
            "indexes": indexes_rates,
            "workload": workload_rates,
        },
        "pseudonym_map_digest": f"sha256:{pseudonym_map_digest}",
        "warnings": [],
    }
    return _normalize_for_json(artifact)


def write_artifact(artifact: dict, path: str) -> None:
    """Serialize and gzip an artifact (§5): sorted keys, so a diff on two
    decompressed artifacts is meaningful. Expects an already-normalized
    artifact (i.e. capture_point()'s return value) -- no json.dump default
    fallback here, deliberately: if this hits a non-JSON-native type, that's
    a real bug in whatever produced the artifact, not something to paper
    over silently."""
    import gzip
    import json

    with gzip.open(path, "wt", encoding="utf-8") as f:
        json.dump(artifact, f, sort_keys=True, indent=2)
