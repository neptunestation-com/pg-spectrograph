"""workload section: pg_stat_statements top-K, verb/join/aggregate derivation (§6.5).

Top-K selection is the union of four ranking lenses (§13.2, refined per a
2026-09-26 note on GitHub issue #1): total_exec_time, calls, wal_bytes, and
shared_blks_read. The fourth lens exists because total_exec_time is
confounded by CPU speed, concurrent load, and cache warmth at capture time,
while block I/O counts are a physical, hardware-invariant measure of data
movement for the same query against the same data distribution -- exactly
the "sufficient statistic for performance" bet §1 makes.
"""

from __future__ import annotations

import pglast
from pglast.visitors import Visitor

from pgspec.completeness import build_completeness
from pgspec.pseudonym import rewrite_query_text

_TOTALS_SQL = """
    SELECT sum(total_exec_time), sum(calls), sum(wal_bytes), sum(shared_blks_read)
    FROM pg_stat_statements
    WHERE dbid = (SELECT oid FROM pg_database WHERE datname = current_database())
"""

_TOPK_SQL = """
    WITH ranked AS (
        SELECT *,
               row_number() OVER (ORDER BY total_exec_time DESC) AS rn_time,
               row_number() OVER (ORDER BY calls DESC) AS rn_calls,
               row_number() OVER (ORDER BY wal_bytes DESC) AS rn_wal,
               row_number() OVER (ORDER BY shared_blks_read DESC) AS rn_blks
        FROM pg_stat_statements
        WHERE dbid = (SELECT oid FROM pg_database WHERE datname = current_database())
    )
    SELECT queryid, query, toplevel, plans, calls, total_exec_time, min_exec_time,
           max_exec_time, mean_exec_time, stddev_exec_time, rows,
           shared_blks_hit, shared_blks_read, shared_blks_dirtied, shared_blks_written,
           local_blks_hit, local_blks_read, local_blks_dirtied, local_blks_written,
           temp_blks_read, temp_blks_written, wal_records, wal_fpi, wal_bytes
    FROM ranked
    WHERE rn_time <= %(top_k)s OR rn_calls <= %(top_k)s
       OR rn_wal <= %(top_k)s OR rn_blks <= %(top_k)s
    ORDER BY queryid
"""

_DEALLOC_SQL = "SELECT dealloc, stats_reset FROM pg_stat_statements_info"
_TRACKED_COUNT_SQL = """
    SELECT count(*) FROM pg_stat_statements
    WHERE dbid = (SELECT oid FROM pg_database WHERE datname = current_database())
"""

#: Syntactic proxy for "this call is an aggregate": pglast (parse-tree-only)
#: has no catalog access, so it can't ask pg_proc whether a function is
#: actually an aggregate. agg_star (COUNT(*)) is a reliable grammar signal;
#: this name list is a heuristic for everything else, same maintenance
#: posture as pgfr's column_classes override list.
_AGGREGATE_FUNCTION_NAMES = {
    "count",
    "sum",
    "avg",
    "min",
    "max",
    "array_agg",
    "string_agg",
    "bool_and",
    "bool_or",
    "every",
    "variance",
    "var_pop",
    "var_samp",
    "stddev",
    "stddev_pop",
    "stddev_samp",
    "json_agg",
    "jsonb_agg",
    "xmlagg",
}

_VERB_BY_STMT_TYPE = {
    "SelectStmt": "SELECT",
    "InsertStmt": "INSERT",
    "UpdateStmt": "UPDATE",
    "DeleteStmt": "DELETE",
}

_DDL_STMT_TYPES = {
    "CreateStmt",
    "CreateTableAsStmt",
    "AlterTableStmt",
    "DropStmt",
    "IndexStmt",
    "CreateSchemaStmt",
    "CreateFunctionStmt",
    "CreateExtensionStmt",
    "CreateStatsStmt",
    "TruncateStmt",
    "CommentStmt",
    "GrantStmt",
    "GrantRoleStmt",
    "VacuumStmt",
    "ViewStmt",
    "RenameStmt",
    "CreateSeqStmt",
    "AlterSeqStmt",
    "CreateTrigStmt",
    "CreatedbStmt",
    "DropdbStmt",
    "CreateRoleStmt",
    "AlterRoleStmt",
}


class _StatementAnalyzer(Visitor):
    """One pass over a parsed statement collecting every syntactic fact
    workload.py's derived fields need. table_aliases maps both a table's
    alias (or bare name, if unaliased) and its bare name to its real
    fully-qualified name, so referenced-table resolution works whichever
    form a later ColumnRef qualifier uses."""

    def __init__(self):
        super().__init__()
        self.table_aliases: dict[str, str] = {}
        self.join_count = 0
        self.has_aggregate = False
        self.max_param = 0

    def visit_RangeVar(self, ancestors, node):
        schema = node.schemaname or "public"
        fq = f"{schema}.{node.relname}"
        alias = node.alias.aliasname if node.alias else node.relname
        self.table_aliases[alias] = fq
        self.table_aliases[node.relname] = fq

    def visit_JoinExpr(self, ancestors, node):
        self.join_count += 1

    def visit_FuncCall(self, ancestors, node):
        if node.agg_star:
            self.has_aggregate = True
            return
        if node.funcname:
            name = node.funcname[-1].sval.lower()
            if name in _AGGREGATE_FUNCTION_NAMES:
                self.has_aggregate = True

    def visit_ParamRef(self, ancestors, node):
        if node.number and node.number > self.max_param:
            self.max_param = node.number


def _verb_class(stmt) -> str:
    type_name = type(stmt).__name__
    if type_name in _VERB_BY_STMT_TYPE:
        return _VERB_BY_STMT_TYPE[type_name]
    if type_name in _DDL_STMT_TYPES:
        return "DDL"
    return "OTHER"


def _table_columns_by_bare_name(
    identifier_map: dict[str, str],
) -> dict[str, dict[str, str]]:
    """fq_table -> {bare_column_name: pseudonym}, derived from schema.py's
    flat schema.table.column -> pseudonym map."""
    result: dict[str, dict[str, str]] = {}
    for fq_name, pseudonym in identifier_map.items():
        parts = fq_name.split(".")
        if len(parts) == 3:
            fq_tbl = f"{parts[0]}.{parts[1]}"
            result.setdefault(fq_tbl, {})[parts[2]] = pseudonym
    return result


def analyze_statement(
    sql: str,
    identifier_map: dict[str, str],
    columns_by_table: dict[str, dict[str, str]],
) -> dict:
    """Parse one normalized statement text and derive verb class, join
    count, aggregate/limit flags, max parameter number, referenced-table
    pseudonyms, and a per-query rewrite map for pseudonymizing the text
    itself (§6.5). Returns unparsed=True (with every other field None/empty)
    when the text doesn't parse, matching §7.4's utility-statement handling.
    """
    try:
        tree = pglast.parse_sql(sql)
    except pglast.parser.ParseError:
        return {
            "verb": None,
            "referenced_table_pseudonyms": [],
            "join_count": None,
            "has_aggregate": None,
            "has_limit": None,
            "max_param": None,
            "text": None,
            "unparsed": True,
        }

    stmt = tree[0].stmt
    analyzer = _StatementAnalyzer()
    analyzer(tree)

    referenced_tables = sorted(set(analyzer.table_aliases.values()))
    referenced_table_pseudonyms = [
        identifier_map[fq]
        for fq in referenced_tables
        if fq in identifier_map
    ]

    query_identifier_map: dict[str, str] = {}

    # Qualified lookups (alias.column / realtablename.column): always
    # unambiguous, since the qualifier pins down exactly which table's
    # column this is even when two joined tables share a bare column name
    # (pseudonym.py's rewriter tries "qualifier.column" before falling back
    # to the bare name alone).
    for alias, fq_tbl in analyzer.table_aliases.items():
        for bare_column, pseudonym in columns_by_table.get(fq_tbl, {}).items():
            query_identifier_map[f"{alias}.{bare_column}"] = pseudonym

    # Bare (unqualified) lookups: only added when the bare name resolves to
    # exactly one pseudonym across every table this query actually
    # references. A real collision here would mean either an already-
    # qualified reference (handled above) or an unqualified ambiguity
    # Postgres itself would have rejected at parse time; either way,
    # dropping the bare key is the safe default over guessing or leaking
    # the real column name.
    bare_candidates: dict[str, set[str]] = {}
    for fq_tbl in referenced_tables:
        for bare_column, pseudonym in columns_by_table.get(fq_tbl, {}).items():
            bare_candidates.setdefault(bare_column, set()).add(pseudonym)
    for bare_column, pseudonyms in bare_candidates.items():
        if len(pseudonyms) == 1:
            query_identifier_map.setdefault(bare_column, next(iter(pseudonyms)))

    for fq_tbl in referenced_tables:
        if fq_tbl in identifier_map:
            query_identifier_map[fq_tbl] = identifier_map[fq_tbl]
            query_identifier_map[fq_tbl.split(".", 1)[1]] = identifier_map[fq_tbl]

    text, unparsed = rewrite_query_text(sql, query_identifier_map)

    has_limit = bool(getattr(stmt, "limitCount", None) is not None)
    has_aggregate = analyzer.has_aggregate or bool(
        getattr(stmt, "groupClause", None) or getattr(stmt, "havingClause", None)
    )

    return {
        "verb": _verb_class(stmt),
        "referenced_table_pseudonyms": referenced_table_pseudonyms,
        "join_count": analyzer.join_count,
        "has_aggregate": has_aggregate,
        "has_limit": has_limit,
        "max_param": analyzer.max_param,
        "text": text,
        "unparsed": unparsed,
    }


def fetch_totals(conn) -> dict:
    with conn.cursor() as cur:
        cur.execute(_TOTALS_SQL)
        total_exec_time, total_calls, total_wal_bytes, total_blks_read = cur.fetchone()
    return {
        "total_exec_time": total_exec_time or 0,
        "total_calls": total_calls or 0,
        "total_wal_bytes": total_wal_bytes or 0,
        "total_blks_read": total_blks_read or 0,
    }


def fetch_top_k(conn, top_k: int) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(_TOPK_SQL, {"top_k": top_k})
        columns = [d.name for d in cur.description]
        rows = cur.fetchall()
    return [dict(zip(columns, row)) for row in rows]


def capture_workload(
    conn, identifier_map: dict[str, str], top_k: int = 500
) -> dict:
    """Build the workload section (§6.5)."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT extname FROM pg_extension WHERE extname = 'pg_stat_statements'"
        )
        if cur.fetchone() is None:
            return {
                "statements": [],
                "coverage": {},
                "parameter_distributions": "unavailable_in_v1",
                "completeness": build_completeness(
                    available=False,
                    reason="pg_stat_statements extension not installed",
                ),
            }

    # Top-K first, totals second: pgspec's own catalog queries (and any
    # concurrent workload activity) keep landing in pg_stat_statements
    # between these two reads, so the population is never perfectly frozen.
    # Reading top-K first means the totals read afterward can only be equal
    # to or larger than what was actually captured, keeping every coverage
    # fraction below at or under 1.0, never an artifact of self-observation
    # timing pushing it above (the same self-observation phenomenon pgfr
    # documents and self-measures rather than hides).
    top_rows = fetch_top_k(conn, top_k)
    totals = fetch_totals(conn)
    columns_by_table = _table_columns_by_bare_name(identifier_map)

    with conn.cursor() as cur:
        cur.execute(_TRACKED_COUNT_SQL)
        tracked_count = cur.fetchone()[0]
        dealloc_count = None
        stats_reset = None
        try:
            cur.execute(_DEALLOC_SQL)
            row = cur.fetchone()
            if row is not None:
                dealloc_count, stats_reset = row
        except Exception:
            pass

    statements = []
    captured_exec_time = 0
    captured_calls = 0
    captured_wal_bytes = 0
    captured_blks_read = 0
    unparsed_count = 0

    for row in top_rows:
        analysis = analyze_statement(row["query"], identifier_map, columns_by_table)
        if analysis["unparsed"]:
            unparsed_count += 1

        captured_exec_time += row["total_exec_time"] or 0
        captured_calls += row["calls"] or 0
        captured_wal_bytes += row["wal_bytes"] or 0
        captured_blks_read += row["shared_blks_read"] or 0

        statements.append(
            {
                "queryid": row["queryid"],
                "text": analysis["text"],
                "unparsed": analysis["unparsed"],
                "verb": analysis["verb"],
                "referenced_table_pseudonyms": analysis["referenced_table_pseudonyms"],
                "join_count": analysis["join_count"],
                "has_aggregate": analysis["has_aggregate"],
                "has_limit": analysis["has_limit"],
                "max_param": analysis["max_param"],
                "plans": row["plans"],
                "calls": row["calls"],
                "total_exec_time": row["total_exec_time"],
                "min_exec_time": row["min_exec_time"],
                "max_exec_time": row["max_exec_time"],
                "mean_exec_time": row["mean_exec_time"],
                "stddev_exec_time": row["stddev_exec_time"],
                "rows": row["rows"],
                "shared_blks_hit": row["shared_blks_hit"],
                "shared_blks_read": row["shared_blks_read"],
                "shared_blks_dirtied": row["shared_blks_dirtied"],
                "shared_blks_written": row["shared_blks_written"],
                "local_blks_hit": row["local_blks_hit"],
                "local_blks_read": row["local_blks_read"],
                "local_blks_dirtied": row["local_blks_dirtied"],
                "local_blks_written": row["local_blks_written"],
                "temp_blks_read": row["temp_blks_read"],
                "temp_blks_written": row["temp_blks_written"],
                "wal_records": row["wal_records"],
                "wal_fpi": row["wal_fpi"],
                "wal_bytes": row["wal_bytes"],
            }
        )

    coverage = {
        "statements_captured": len(statements),
        "statements_tracked": tracked_count,
        "unparsed_fraction": (
            unparsed_count / len(statements) if statements else 0.0
        ),
        "exec_time_fraction": (
            captured_exec_time / totals["total_exec_time"]
            if totals["total_exec_time"]
            else None
        ),
        "calls_fraction": (
            captured_calls / totals["total_calls"] if totals["total_calls"] else None
        ),
        "wal_bytes_fraction": (
            float(captured_wal_bytes) / float(totals["total_wal_bytes"])
            if totals["total_wal_bytes"]
            else None
        ),
        "blks_read_fraction": (
            captured_blks_read / totals["total_blks_read"]
            if totals["total_blks_read"]
            else None
        ),
        "dealloc_count": dealloc_count,
    }

    return {
        "statements": statements,
        "coverage": coverage,
        "parameter_distributions": "unavailable_in_v1",
        "completeness": build_completeness(
            available=True,
            coverage=coverage,
            staleness={"stats_reset": str(stats_reset) if stats_reset else None},
        ),
    }
