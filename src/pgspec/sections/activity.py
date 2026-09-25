"""activity section: pg_stat_database/bgwriter/wal/io/user_tables/user_functions (§6.6).

Whole-row/whole-matrix views (bgwriter, checkpointer, wal, io) carry no
identifiers to pseudonymize -- they're cluster/database-wide counters, not
per-object. Per-table and per-function rows do carry identifiers and are
pseudonymized before leaving this module, same as every other section.
"""

from __future__ import annotations

from dataclasses import dataclass

import psycopg

from pgspec.completeness import build_completeness
from pgspec.pseudonym import assign_ordinals
from pgspec.sections.schema import fq_table

_DATABASE_SQL = """
    SELECT datid, datname, numbackends, xact_commit, xact_rollback,
           blks_read, blks_hit, tup_returned, tup_fetched, tup_inserted,
           tup_updated, tup_deleted, conflicts, temp_files, temp_bytes,
           deadlocks, checksum_failures, checksum_last_failure,
           blk_read_time, blk_write_time, session_time, active_time,
           idle_in_transaction_time, sessions, sessions_abandoned,
           sessions_fatal, sessions_killed, stats_reset
    FROM pg_stat_database
    WHERE datname = current_database()
"""

_DATABASE_CONFLICTS_SQL = """
    SELECT confl_tablespace, confl_lock, confl_snapshot, confl_bufferpin,
           confl_deadlock
    FROM pg_stat_database_conflicts
    WHERE datname = current_database()
"""

_BGWRITER_SQL = "SELECT * FROM pg_stat_bgwriter"
_CHECKPOINTER_SQL = "SELECT * FROM pg_stat_checkpointer"
_WAL_SQL = "SELECT * FROM pg_stat_wal"
_IO_SQL = "SELECT * FROM pg_stat_io"

_USER_TABLES_SQL = """
    SELECT schemaname, relname, seq_scan, seq_tup_read, idx_scan, idx_tup_fetch,
           n_tup_ins, n_tup_upd, n_tup_del, n_tup_hot_upd, n_live_tup, n_dead_tup,
           n_mod_since_analyze, last_vacuum, last_autovacuum, last_analyze,
           last_autoanalyze, vacuum_count, autovacuum_count, analyze_count,
           autoanalyze_count
    FROM pg_stat_user_tables
    WHERE schemaname NOT IN ('pg_catalog', 'information_schema')
"""

_USER_FUNCTIONS_SQL = """
    SELECT schemaname, funcname, calls, total_time, self_time
    FROM pg_stat_user_functions
    WHERE schemaname NOT IN ('pg_catalog', 'information_schema')
"""


def _fetch_rows(conn: psycopg.Connection, sql: str) -> list[dict] | None:
    """Fail soft to None (I5): a version-absent view (pg_stat_checkpointer
    on <17) or a missing privilege must not crash the section."""
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
            columns = [d.name for d in cur.description]
            rows = cur.fetchall()
        return [dict(zip(columns, row)) for row in rows]
    except psycopg.Error:
        return None


@dataclass
class ActivityCapture:
    section: dict
    identifier_map: dict[str, str]


def capture_activity(
    conn: psycopg.Connection, identifier_map: dict[str, str], salt: bytes
) -> ActivityCapture:
    database_rows = _fetch_rows(conn, _DATABASE_SQL)
    database = database_rows[0] if database_rows else None

    conflicts_rows = _fetch_rows(conn, _DATABASE_CONFLICTS_SQL)
    database_conflicts = conflicts_rows[0] if conflicts_rows else None

    bgwriter_rows = _fetch_rows(conn, _BGWRITER_SQL)
    bgwriter = bgwriter_rows[0] if bgwriter_rows else None

    checkpointer_rows = _fetch_rows(conn, _CHECKPOINTER_SQL)
    checkpointer = checkpointer_rows[0] if checkpointer_rows else None

    wal_rows = _fetch_rows(conn, _WAL_SQL)
    wal = wal_rows[0] if wal_rows else None

    io_rows = _fetch_rows(conn, _IO_SQL)

    tables_raw = _fetch_rows(conn, _USER_TABLES_SQL) or []
    tables_out = []
    for row in tables_raw:
        fq_tbl = fq_table(row["schemaname"], row["relname"])
        pseudonym = identifier_map.get(fq_tbl)
        if pseudonym is None:
            continue
        entry = {k: v for k, v in row.items() if k not in ("schemaname", "relname")}
        entry["table_pseudonym"] = pseudonym
        tables_out.append(entry)

    functions_raw = _fetch_rows(conn, _USER_FUNCTIONS_SQL) or []
    # Function names aren't in I2's literal schema/table/column/index list,
    # but a custom function name can be just as informative (e.g.
    # "calculate_executive_bonus") as a table or column name, so this
    # module holds them to the same no-raw-identifiers standard, reusing
    # the "fn" prefix already reserved for exactly this in pseudonym.py.
    function_names = sorted(
        {f"{r['schemaname']}.{r['funcname']}" for r in functions_raw}
    )
    function_pseudonyms = assign_ordinals(function_names, salt, prefix="fn")
    functions_out = [
        {
            "pseudonym": function_pseudonyms[f"{r['schemaname']}.{r['funcname']}"],
            "calls": r["calls"],
            "total_time": r["total_time"],
            "self_time": r["self_time"],
        }
        for r in functions_raw
    ]

    section = {
        "database": database,
        "database_conflicts": database_conflicts,
        "bgwriter": bgwriter,
        "checkpointer": checkpointer,
        "wal": wal,
        "io": io_rows,
        "user_tables": tables_out,
        "user_functions": functions_out,
        "completeness": build_completeness(
            available=True,
            coverage={
                "tables_captured": len(tables_out),
                "functions_captured": len(functions_out),
                "checkpointer_available": checkpointer is not None,
                "io_available": io_rows is not None,
                "conflicts_available": database_conflicts is not None,
            },
        ),
    }
    return ActivityCapture(section=section, identifier_map=function_pseudonyms)
