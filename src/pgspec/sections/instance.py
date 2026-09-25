"""instance section: server version, GUC allowlist, stats_reset ages, extensions (§6.1).

The GUC allowlist is shipped as data, not code, per the spec: a curated set
of exact names plus a small set of prefix patterns for the *_ families
(autovacuum_*, bgwriter_*, max_parallel_workers*).
"""

from __future__ import annotations

import psycopg

from pgspec.capture import Capabilities
from pgspec.completeness import build_completeness

GUC_EXACT_NAMES: tuple[str, ...] = (
    "shared_buffers",
    "work_mem",
    "maintenance_work_mem",
    "effective_cache_size",
    "max_wal_size",
    "min_wal_size",
    "checkpoint_timeout",
    "checkpoint_completion_target",
    "wal_compression",
    "wal_level",
    "max_connections",
    "random_page_cost",
    "seq_page_cost",
    "effective_io_concurrency",
    "jit",
    "default_statistics_target",
    "huge_pages",
    "track_io_timing",
    "shared_preload_libraries",
    "pg_stat_statements.max",
    "pg_stat_statements.track",
    "pg_stat_statements.track_utility",
)

GUC_PREFIXES: tuple[str, ...] = ("autovacuum_", "bgwriter_", "max_parallel_workers")

#: Views carrying a stats_reset column (§6.1). Not every view exists on every
#: PostgreSQL major or configuration (pg_stat_checkpointer is PG17+;
#: pg_stat_statements_info needs the extension); each is probed independently
#: and fails soft (I5) rather than branching on version.
STATS_RESET_SOURCES: dict[str, str] = {
    "pg_stat_database": "SELECT max(stats_reset)::text FROM pg_stat_database",
    "pg_stat_bgwriter": "SELECT stats_reset::text FROM pg_stat_bgwriter",
    "pg_stat_checkpointer": "SELECT stats_reset::text FROM pg_stat_checkpointer",
    "pg_stat_wal": "SELECT stats_reset::text FROM pg_stat_wal",
    "pg_stat_statements_info": "SELECT stats_reset::text FROM pg_stat_statements_info",
}


def _guc_like_patterns() -> list[str]:
    return [f"{prefix}%" for prefix in GUC_PREFIXES]


def capture_guc_snapshot(conn: psycopg.Connection) -> dict[str, dict]:
    query = (
        "SELECT name, setting, unit, source FROM pg_settings "
        "WHERE name = ANY(%(names)s) OR name LIKE ANY(%(patterns)s) "
        "ORDER BY name"
    )
    with conn.cursor() as cur:
        cur.execute(
            query, {"names": list(GUC_EXACT_NAMES), "patterns": _guc_like_patterns()}
        )
        rows = cur.fetchall()
    return {
        name: {"setting": setting, "unit": unit, "source": source}
        for name, setting, unit, source in rows
    }


def _try_scalar(conn: psycopg.Connection, sql: str) -> str | None:
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
            row = cur.fetchone()
            return row[0] if row else None
    except psycopg.Error:
        return None


def capture_stats_reset(conn: psycopg.Connection) -> dict[str, str | None]:
    return {name: _try_scalar(conn, sql) for name, sql in STATS_RESET_SOURCES.items()}


def capture_instance(conn: psycopg.Connection, capabilities: Capabilities) -> dict:
    """Build the instance section (§6.1)."""
    guc_snapshot = capture_guc_snapshot(conn)
    stats_reset = capture_stats_reset(conn)
    return {
        "server_version_num": capabilities.server_version_num,
        "extensions": capabilities.extensions,
        "guc_snapshot": guc_snapshot,
        "stats_reset": stats_reset,
        "completeness": build_completeness(
            available=True,
            coverage={"guc_settings_captured": len(guc_snapshot)},
        ),
    }
