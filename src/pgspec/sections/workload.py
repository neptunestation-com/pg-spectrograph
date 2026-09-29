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

import math
import re

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

# Representativity (issue #7, DIAMetrics): feature quantiles over the whole
# population, computed server-side so nothing but aggregates leaves the
# database, and a first-token verb mix (the first alphabetic run of the
# normalized text; classify_first_token() applies the same rule to the
# captured side) weighted three ways.
_POPULATION_SQL = """
    SELECT count(*),
           percentile_cont(ARRAY[0.5, 0.9, 0.99]) WITHIN GROUP (ORDER BY mean_exec_time),
           percentile_cont(ARRAY[0.5, 0.9, 0.99]) WITHIN GROUP (ORDER BY calls),
           percentile_cont(ARRAY[0.5, 0.9, 0.99])
               WITHIN GROUP (ORDER BY rows::float8 / greatest(calls, 1)),
           percentile_cont(ARRAY[0.5, 0.9, 0.99])
               WITHIN GROUP (ORDER BY (shared_blks_hit + shared_blks_read)::float8 / greatest(calls, 1))
    FROM pg_stat_statements
    WHERE dbid = (SELECT oid FROM pg_database WHERE datname = current_database())
"""
_POPULATION_VERBS_SQL = """
    SELECT CASE upper(substring(query FROM '^\\s*([A-Za-z]+)'))
               WHEN 'SELECT' THEN 'SELECT' WHEN 'INSERT' THEN 'INSERT'
               WHEN 'UPDATE' THEN 'UPDATE' WHEN 'DELETE' THEN 'DELETE'
               WHEN 'WITH' THEN 'WITH' ELSE 'OTHER' END AS verb,
           count(*), sum(calls), sum(total_exec_time)
    FROM pg_stat_statements
    WHERE dbid = (SELECT oid FROM pg_database WHERE datname = current_database())
    GROUP BY 1
"""

# The fifth lens (issue #7, decision A2): a uniform random sample of
# statements outside the top-K union, so the light tail the four "top"
# lenses can never reach is represented. Seeded so the same
# pg_stat_statements state yields the same sample.
_TAIL_SAMPLE_SQL = """
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
    WHERE rn_time > %(top_k)s AND rn_calls > %(top_k)s
      AND rn_wal > %(top_k)s AND rn_blks > %(top_k)s
    ORDER BY random()
    LIMIT %(tail_sample)s
"""
_SEED_SQL = "SELECT setseed(0.5)"

_FEATURES = ("mean_exec_time", "calls", "rows_per_call", "blks_per_call")
_VERB_TOKEN = re.compile(r"^\s*([A-Za-z]+)")
_COMPARISON_OPS = {
    "=", "<>", "!=", "<", ">", "<=", ">=",
    "~~", "!~~", "~~*", "!~~*", "~", "!~", "~*", "!~*",
}
_OP_LABELS = {"!=": "<>", "~~": "LIKE", "!~~": "NOT LIKE", "~~*": "ILIKE", "!~~*": "NOT ILIKE"}
_EQUALITY_OPS = {"=", "IN", "= ANY"}

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

#: Only these verbs get their text pseudonymized and kept (§7.4); every
#: other verb (DDL, OTHER) gets its text dropped entirely.
_DML_VERBS = frozenset(_VERB_BY_STMT_TYPE.values())

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


def _column_ref_fields(node) -> tuple[str, ...] | None:
    if node is None or type(node).__name__ != "ColumnRef":
        return None
    return tuple(getattr(field, "sval", "*") for field in node.fields)


class _HasParam(Visitor):
    def __init__(self):
        super().__init__()
        self.found = False

    def visit_ParamRef(self, ancestors, node):
        self.found = True


def _contains_param(node) -> bool:
    if node is None:
        return False
    if isinstance(node, (list, tuple)):
        return any(_contains_param(child) for child in node)
    if type(node).__name__ == "ParamRef":
        return True
    finder = _HasParam()
    try:
        finder(node)
    except Exception:  # noqa: BLE001 - a leaf that isn't walkable has no ParamRef
        return False
    return finder.found


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
        # (column fields, operator label, parameterized) for every
        # comparison whose one side is a plain column reference.
        self.predicates: list[tuple[tuple[str, ...], str, bool]] = []

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

    def visit_A_Expr(self, ancestors, node):
        left = _column_ref_fields(node.lexpr)
        column = left or _column_ref_fields(node.rexpr)
        if column is None:
            return
        other = node.rexpr if left else node.lexpr
        kind = getattr(node.kind, "name", str(node.kind))
        name = "".join(getattr(part, "sval", "") for part in (node.name or ()))
        if kind == "AEXPR_OP":
            if name not in _COMPARISON_OPS:
                return
            op = _OP_LABELS.get(name, name)
        elif kind == "AEXPR_IN":
            op = "IN" if name == "=" else "NOT IN"
        elif kind == "AEXPR_LIKE":
            op = "LIKE" if name == "~~" else "NOT LIKE"
        elif kind == "AEXPR_ILIKE":
            op = "ILIKE" if name == "~~*" else "NOT ILIKE"
        elif kind == "AEXPR_OP_ANY":
            op = f"{_OP_LABELS.get(name, name)} ANY"
        elif kind == "AEXPR_OP_ALL":
            op = f"{_OP_LABELS.get(name, name)} ALL"
        elif kind == "AEXPR_BETWEEN":
            op = "BETWEEN"
        elif kind == "AEXPR_NOT_BETWEEN":
            op = "NOT BETWEEN"
        else:
            return
        self.predicates.append((column, op, _contains_param(other)))

    def visit_NullTest(self, ancestors, node):
        column = _column_ref_fields(node.arg)
        if column is None:
            return
        is_null = getattr(node.nulltesttype, "name", "") == "IS_NULL"
        self.predicates.append((column, "IS NULL" if is_null else "IS NOT NULL", False))


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


def _abs_distinct(n_distinct, reltuples) -> float | None:
    if n_distinct is None:
        return None
    n_distinct = float(n_distinct)
    if n_distinct < 0:
        return -n_distinct * float(reltuples) if reltuples else None
    return n_distinct or None


def _selectivity_estimate(op: str, stats: dict | None) -> dict:
    """Catalog-derived selectivity for one predicate (issue #7, decision C),
    never from parameter values: equality-shaped operators get the uniform
    1/n_distinct and the MCV-weighted sum of squared frequencies (the
    expected selectivity when the parameter follows the column's own
    distribution, with the untracked tail as one uniform mass); null tests
    get null_frac; range and pattern operators are marked as needing values
    rather than guessed."""
    if op in ("IS NULL", "IS NOT NULL"):
        if not stats or stats.get("null_frac") is None:
            return {"kind": "unavailable", "reason": "no column statistics"}
        null_frac = float(stats["null_frac"])
        if op == "IS NULL":
            return {"kind": "null", "value": null_frac}
        return {"kind": "not_null", "value": 1.0 - null_frac}
    if op not in _EQUALITY_OPS:
        return {"kind": "unavailable", "reason": "needs parameter values"}
    abs_distinct = _abs_distinct(stats.get("n_distinct"), stats.get("reltuples")) if stats else None
    if not abs_distinct or abs_distinct <= 0:
        return {"kind": "unavailable", "reason": "no column statistics"}
    freqs = [float(f) for f in (stats.get("most_common_freqs") or [])]
    remainder = max(0.0, 1.0 - sum(freqs))
    tail_count = max(abs_distinct - len(freqs), 0)
    mcv_weighted = sum(f * f for f in freqs)
    if tail_count > 0:
        mcv_weighted += remainder**2 / tail_count
    return {"kind": "equality", "uniform": 1.0 / abs_distinct, "mcv_weighted": mcv_weighted}


def _resolve_predicates(
    raw: list[tuple[tuple[str, ...], str, bool]],
    query_identifier_map: dict[str, str],
    column_stats: dict[str, dict] | None,
) -> list[dict]:
    predicates = []
    for fields, op, parameterized in raw:
        if len(fields) >= 2:
            pseudonym = query_identifier_map.get(f"{fields[-2]}.{fields[-1]}")
        else:
            pseudonym = query_identifier_map.get(fields[-1])
        if pseudonym is None:
            # Not a user column pgspec knows (a catalog column, a CTE
            # output, an alias): nothing value-free to say about it.
            continue
        predicates.append(
            {
                "column": pseudonym,
                "op": op,
                "parameterized": parameterized,
                "selectivity_est": _selectivity_estimate(op, (column_stats or {}).get(pseudonym)),
            }
        )
    return predicates


def _function_lookup(function_map: dict[str, str] | None) -> dict[str, str]:
    """"schema.function" keys plus bare names where the bare name maps to
    exactly one pseudonym across schemas, mirroring the column rule: an
    ambiguous bare name is left out rather than guessed."""
    lookup = dict(function_map or {})
    bare: dict[str, set[str]] = {}
    for fq_name, pseudonym in (function_map or {}).items():
        bare.setdefault(fq_name.rsplit(".", 1)[-1], set()).add(pseudonym)
    for name, pseudonyms in bare.items():
        if len(pseudonyms) == 1:
            lookup.setdefault(name, next(iter(pseudonyms)))
    return lookup


def analyze_statement(
    sql: str,
    identifier_map: dict[str, str],
    columns_by_table: dict[str, dict[str, str]],
    column_stats: dict[str, dict] | None = None,
    function_map: dict[str, str] | None = None,
) -> dict:
    """Parse one normalized statement text and derive verb class, join
    count, aggregate/limit flags, max parameter number, referenced-table
    pseudonyms, the predicate list with catalog-derived selectivity
    estimates (issue #7), and a per-query rewrite map for pseudonymizing
    the text itself (§6.5). Returns unparsed=True (with every other field
    None/empty) when the text doesn't parse, matching §7.4's
    utility-statement handling.
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
            "predicates": [],
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

    verb = _verb_class(stmt)
    if verb in _DML_VERBS:
        text, unparsed = rewrite_query_text(sql, query_identifier_map, function_map=function_map)
    else:
        # §7.4: utility statements (DDL, SET, and everything else that
        # isn't SELECT/INSERT/UPDATE/DELETE) get their text dropped
        # entirely, verb class only. This isn't just caution: pg_stat_
        # statements' literal normalization only covers DML reliably, and
        # DDL's own grammar has AST node types (ColumnDef.colname,
        # IndexStmt.idxname, IndexElem.name, and surely others no one has
        # hit yet) that the rewriter has no visitor for at all -- found
        # live, by the version-matrix test, when a CREATE TABLE/CREATE
        # INDEX from this project's own fixture setup turned up in
        # pg_stat_statements (track_utility defaults to on) with column
        # definitions and index names left completely unpseudonymized.
        # Dropping the text is the safe default over trying to enumerate
        # every DDL node type by hand.
        text, unparsed = None, False

    has_limit = bool(getattr(stmt, "limitCount", None) is not None)
    has_aggregate = analyzer.has_aggregate or bool(
        getattr(stmt, "groupClause", None) or getattr(stmt, "havingClause", None)
    )

    return {
        "verb": verb,
        "referenced_table_pseudonyms": referenced_table_pseudonyms,
        "join_count": analyzer.join_count,
        "has_aggregate": has_aggregate,
        "has_limit": has_limit,
        "max_param": analyzer.max_param,
        "predicates": _resolve_predicates(analyzer.predicates, query_identifier_map, column_stats),
        "text": text,
        "unparsed": unparsed,
    }


def classify_first_token(text: str | None) -> str:
    """The population-side verb classifier's exact rule, applied to the
    captured side: the first alphabetic run of the text."""
    match = _VERB_TOKEN.match(text or "")
    token = match.group(1).upper() if match else ""
    return token if token in ("SELECT", "INSERT", "UPDATE", "DELETE", "WITH") else "OTHER"


def _quantiles3(values: list[float]) -> dict | None:
    """p50/p90/p99 by linear interpolation, matching percentile_cont."""
    if not values:
        return None
    ordered = sorted(values)
    last = len(ordered) - 1

    def at(p: float) -> float:
        position = last * p
        low = math.floor(position)
        high = min(low + 1, last)
        return ordered[low] + (ordered[high] - ordered[low]) * (position - low)

    return {"p50": at(0.5), "p90": at(0.9), "p99": at(0.99)}


def _feature_values(statements: list[dict]) -> dict[str, list[float]]:
    def calls(s):
        return max(float(s.get("calls") or 0), 1.0)

    return {
        "mean_exec_time": [float(s.get("mean_exec_time") or 0) for s in statements],
        "calls": [float(s.get("calls") or 0) for s in statements],
        "rows_per_call": [float(s.get("rows") or 0) / calls(s) for s in statements],
        "blks_per_call": [
            (float(s.get("shared_blks_hit") or 0) + float(s.get("shared_blks_read") or 0)) / calls(s)
            for s in statements
        ],
    }


def _verb_mix_shares(rows: dict[str, dict]) -> dict[str, dict]:
    total_calls = sum(float(r.get("calls") or 0) for r in rows.values())
    total_time = sum(float(r.get("exec_time") or 0) for r in rows.values())
    return {
        verb: {
            "statements": int(r.get("statements") or 0),
            "calls_share": (float(r.get("calls") or 0) / total_calls) if total_calls else None,
            "exec_time_share": (float(r.get("exec_time") or 0) / total_time) if total_time else None,
        }
        for verb, r in sorted(rows.items())
    }


def _verb_mix_rows(statements: list[dict]) -> dict[str, dict]:
    rows: dict[str, dict] = {}
    for s in statements:
        verb = classify_first_token(s.get("text")) if s.get("text") else "OTHER"
        row = rows.setdefault(verb, {"statements": 0, "calls": 0.0, "exec_time": 0.0})
        row["statements"] += 1
        row["calls"] += float(s.get("calls") or 0)
        row["exec_time"] += float(s.get("total_exec_time") or 0)
    return rows


def _kl_bits(p_shares: dict[str, float | None], q_shares: dict[str, float | None]) -> float | None:
    """KL(p || q) in bits over verb classes, with 1e-3 additive smoothing so
    a class absent from one side is a finite penalty, not infinity."""
    keys = sorted(set(p_shares) | set(q_shares))
    if not keys:
        return None
    eps = 1e-3

    def smooth(shares):
        raw = [float(shares.get(k) or 0.0) for k in keys]
        total = sum(raw) + eps * len(keys)
        return [(v + eps) / total for v in raw]

    p, q = smooth(p_shares), smooth(q_shares)
    return sum(pk * math.log2(pk / qk) for pk, qk in zip(p, q))


def representativity(statements: list[dict], population: dict | None) -> dict | None:
    """DIAMetrics' second axis (issue #7, decision A1): coverage says how
    much of the work the capture holds; this says whether the captured
    statements *look like* the population. Feature quantiles population
    vs captured (systematic lenses only) vs the tail sample, a log10 ratio
    of medians per feature, and a verb-mix KL divergence."""
    if population is None:
        return None
    systematic = [s for s in statements if not s.get("sampled_tail")]
    tail = [s for s in statements if s.get("sampled_tail")]
    captured_quantiles = {f: _quantiles3(v) for f, v in _feature_values(systematic).items()}
    population_quantiles = population.get("quantiles") or {}
    population_mix = _verb_mix_shares(population.get("verb_mix") or {})
    captured_mix = _verb_mix_shares(_verb_mix_rows(systematic))

    ratios: dict[str, float | None] = {}
    for feature in _FEATURES:
        captured_p50 = (captured_quantiles.get(feature) or {}).get("p50")
        population_p50 = (population_quantiles.get(feature) or {}).get("p50")
        if captured_p50 and population_p50 and captured_p50 > 0 and population_p50 > 0:
            ratios[feature] = math.log10(captured_p50 / population_p50)
        else:
            ratios[feature] = None

    return {
        "population": {
            "statements": population.get("statements"),
            "quantiles": population_quantiles,
            "verb_mix": population_mix,
        },
        "captured": {
            "statements": len(systematic),
            "quantiles": captured_quantiles,
            "verb_mix": captured_mix,
        },
        "tail_sample": {
            "statements": len(tail),
            "quantiles": {f: _quantiles3(v) for f, v in _feature_values(tail).items()},
        },
        "median_log10_ratio": ratios,
        "verb_mix_kl_bits": {
            "calls": _kl_bits(
                {v: r["calls_share"] for v, r in population_mix.items()},
                {v: r["calls_share"] for v, r in captured_mix.items()},
            ),
            "exec_time": _kl_bits(
                {v: r["exec_time_share"] for v, r in population_mix.items()},
                {v: r["exec_time_share"] for v, r in captured_mix.items()},
            ),
        },
    }


def fetch_population(conn) -> dict | None:
    with conn.cursor() as cur:
        cur.execute(_POPULATION_SQL)
        count, q_time, q_calls, q_rows, q_blks = cur.fetchone()
        cur.execute(_POPULATION_VERBS_SQL)
        verb_rows = cur.fetchall()
    if not count:
        return None

    def triple(values):
        if values is None:
            return None
        p50, p90, p99 = (float(v) for v in values)
        return {"p50": p50, "p90": p90, "p99": p99}

    return {
        "statements": int(count),
        "quantiles": {
            "mean_exec_time": triple(q_time),
            "calls": triple(q_calls),
            "rows_per_call": triple(q_rows),
            "blks_per_call": triple(q_blks),
        },
        "verb_mix": {
            verb: {"statements": int(n), "calls": float(calls or 0), "exec_time": float(exec_time or 0)}
            for verb, n, calls, exec_time in verb_rows
        },
    }


def fetch_tail_sample(conn, top_k: int, tail_sample: int) -> list[dict]:
    if tail_sample <= 0:
        return []
    with conn.cursor() as cur:
        cur.execute(_SEED_SQL)
        cur.execute(_TAIL_SAMPLE_SQL, {"top_k": top_k, "tail_sample": tail_sample})
        columns = [d.name for d in cur.description]
        rows = cur.fetchall()
    return [dict(zip(columns, row)) for row in rows]


def _column_stats_by_pseudonym(
    schema_section: dict | None, column_stats_section: dict | None
) -> dict[str, dict]:
    """Column pseudonym -> the value-free statistics a selectivity estimate
    needs, with the owning table's reltuples folded in."""
    reltuples = {
        t["pseudonym"]: t.get("reltuples") for t in (schema_section or {}).get("tables") or []
    }
    return {
        c["pseudonym"]: {
            "n_distinct": c.get("n_distinct"),
            "null_frac": c.get("null_frac"),
            "most_common_freqs": c.get("most_common_freqs"),
            "reltuples": reltuples.get(c.get("table_pseudonym")),
        }
        for c in (column_stats_section or {}).get("columns") or []
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


_PARAMETER_DISTRIBUTIONS = {
    "status": "catalog_derived",
    "detail": (
        "per-statement predicates with selectivity estimates derived from catalog "
        "statistics of the referenced column; parameter values are never read"
    ),
}


def capture_workload(
    conn,
    identifier_map: dict[str, str],
    top_k: int = 500,
    *,
    tail_sample: int = 50,
    schema_section: dict | None = None,
    column_stats_section: dict | None = None,
    function_map: dict[str, str] | None = None,
) -> dict:
    """Build the workload section (§6.5). `tail_sample` statements drawn
    uniformly from outside the top-K union are appended with
    sampled_tail=True (issue #7); coverage fractions and derived's
    concentration metrics describe the systematic capture only. The
    schema and column_stats sections, when given, feed the per-predicate
    selectivity estimates and output selectivity."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT extname FROM pg_extension WHERE extname = 'pg_stat_statements'"
        )
        if cur.fetchone() is None:
            return {
                "statements": [],
                "coverage": {},
                "representativity": None,
                "parameter_distributions": {
                    "status": "unavailable",
                    "reason": "pg_stat_statements extension not installed",
                },
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
    tail_rows = fetch_tail_sample(conn, top_k, tail_sample)
    totals = fetch_totals(conn)
    population = fetch_population(conn)
    columns_by_table = _table_columns_by_bare_name(identifier_map)
    function_lookup = _function_lookup(function_map)
    column_stats = _column_stats_by_pseudonym(schema_section, column_stats_section)
    reltuples_by_table = {
        t["pseudonym"]: t.get("reltuples") for t in (schema_section or {}).get("tables") or []
    }

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

    for row, sampled_tail in [(r, False) for r in top_rows] + [(r, True) for r in tail_rows]:
        analysis = analyze_statement(
            row["query"],
            identifier_map,
            columns_by_table,
            column_stats=column_stats,
            function_map=function_lookup,
        )
        if not sampled_tail:
            if analysis["unparsed"]:
                unparsed_count += 1
            captured_exec_time += row["total_exec_time"] or 0
            captured_calls += row["calls"] or 0
            captured_wal_bytes += row["wal_bytes"] or 0
            captured_blks_read += row["shared_blks_read"] or 0

        rows_per_call = (row["rows"] / row["calls"]) if row["calls"] else None
        output_selectivity = None
        if (
            rows_per_call is not None
            and analysis["verb"] == "SELECT"
            and len(analysis["referenced_table_pseudonyms"]) == 1
        ):
            reltuples = reltuples_by_table.get(analysis["referenced_table_pseudonyms"][0])
            if reltuples and float(reltuples) > 0:
                output_selectivity = rows_per_call / float(reltuples)

        statements.append(
            {
                "queryid": row["queryid"],
                "sampled_tail": sampled_tail,
                "predicates": analysis["predicates"],
                "rows_per_call": rows_per_call,
                "output_selectivity": output_selectivity,
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
        "statements_captured": len(top_rows),
        "tail_sampled": len(tail_rows),
        "statements_tracked": tracked_count,
        "unparsed_fraction": (
            unparsed_count / len(top_rows) if top_rows else 0.0
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
        "representativity": representativity(statements, population),
        "parameter_distributions": dict(_PARAMETER_DISTRIBUTIONS),
        "completeness": build_completeness(
            available=True,
            coverage=coverage,
            staleness={"stats_reset": str(stats_reset) if stats_reset else None},
        ),
    }
