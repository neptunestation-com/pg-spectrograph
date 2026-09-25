"""column_stats section: pg_stats, extended stats, histogram/MCV transforms (§6.3).

Reuses schema.py's pseudonym map rather than assigning its own: assign_ordinals
is a pure function of a name universe, and pg_stats only lists columns that
have actually been ANALYZEd (a strict subset of pg_attribute's full column
list), so independently re-assigning ordinals here over a smaller universe
would silently produce different pseudonyms for the same real column. The
schema section's identifier_map is threaded through instead.
"""

from __future__ import annotations

import datetime as dt

import psycopg

from pgspec.completeness import build_completeness
from pgspec.sections.schema import fetch_columns, fetch_tables, fq_column, fq_table
from pgspec.transforms import (
    TypeClass,
    classify_type,
    mcv_skew_gini,
    normalize_histogram,
    project_mcv_freqs,
    suppress_small_table_stats,
    text_width_transform,
)

_STATS_SQL = """
    SELECT schemaname, tablename, attname, null_frac, avg_width, n_distinct,
           correlation, most_common_freqs, histogram_bounds::text::text[],
           most_common_elem_freqs, elem_count_histogram
    FROM pg_stats
    WHERE schemaname NOT IN ('pg_catalog', 'information_schema')
    ORDER BY schemaname, tablename, attname
"""
# histogram_bounds is declared anyarray in pg_stats, so psycopg has no
# element-type adapter for it; the double cast round-trips it through
# Postgres's own array-literal parser (text -> text[]) so every bound comes
# back as a plain string regardless of the underlying column type, parsed
# per-type below (§7.1's Appendix A.3 sketch: "raw_bounds transformed in
# fetch loop, never retained").
# NOTE: most_common_vals is never selected here or anywhere else in this
# module (§7.2); a static test asserts this.

_EXTENDED_STATS_SQL = """
    SELECT schemaname, tablename, statistics_name, attnames, kinds,
           n_distinct, dependencies, most_common_freqs
    FROM pg_stats_ext
    WHERE schemaname NOT IN ('pg_catalog', 'information_schema')
"""

_LAST_ANALYZE_SQL = """
    SELECT schemaname, relname, last_analyze, last_autoanalyze
    FROM pg_stat_user_tables
    WHERE schemaname NOT IN ('pg_catalog', 'information_schema')
"""


def _parse_bound(text_value: str, type_class: TypeClass) -> float:
    """Parse one histogram_bounds element (always text, per _STATS_SQL's
    double cast) into a float, per the column's type class."""
    if type_class is TypeClass.TEMPORAL:
        return dt.datetime.fromisoformat(text_value).timestamp()
    return float(text_value)


def fetch_raw_stats(conn: psycopg.Connection) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(_STATS_SQL)
        rows = cur.fetchall()
    return [
        {
            "schema": schema,
            "table": table,
            "column": column,
            "null_frac": null_frac,
            "avg_width": avg_width,
            "n_distinct": n_distinct,
            "correlation": correlation,
            "most_common_freqs": most_common_freqs,
            "histogram_bounds": histogram_bounds,
            "most_common_elem_freqs": most_common_elem_freqs,
            "elem_count_histogram": elem_count_histogram,
        }
        for (
            schema,
            table,
            column,
            null_frac,
            avg_width,
            n_distinct,
            correlation,
            most_common_freqs,
            histogram_bounds,
            most_common_elem_freqs,
            elem_count_histogram,
        ) in rows
    ]


def fetch_extended_stats(conn: psycopg.Connection) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(_EXTENDED_STATS_SQL)
        rows = cur.fetchall()
    return [
        {
            "schema": schema,
            "table": table,
            "name": name,
            "attnames": attnames,
            "kinds": kinds,
            "n_distinct": n_distinct,
            "dependencies": dependencies,
            "most_common_freqs": most_common_freqs,
        }
        for (
            schema,
            table,
            name,
            attnames,
            kinds,
            n_distinct,
            dependencies,
            most_common_freqs,
        ) in rows
    ]


def fetch_last_analyze(conn: psycopg.Connection) -> dict[str, dict]:
    with conn.cursor() as cur:
        cur.execute(_LAST_ANALYZE_SQL)
        rows = cur.fetchall()
    return {
        fq_table(schema, table): {
            "last_analyze": last_analyze,
            "last_autoanalyze": last_autoanalyze,
        }
        for schema, table, last_analyze, last_autoanalyze in rows
    }


def capture_column_stats(
    conn: psycopg.Connection, identifier_map: dict[str, str]
) -> dict:
    """Build the column_stats section (§6.3) using the schema section's
    identifier_map for pseudonym lookups."""
    columns = fetch_columns(conn)
    tables = fetch_tables(conn)
    raw_stats = fetch_raw_stats(conn)
    extended_raw = fetch_extended_stats(conn)
    last_analyze = fetch_last_analyze(conn)

    type_by_fq_column = {
        fq_column(c["schema"], c["table"], c["column"]): c["type_name"]
        for c in columns
    }
    stattarget_by_fq_column = {
        fq_column(c["schema"], c["table"], c["column"]): c["attstattarget"]
        for c in columns
    }
    reltuples_by_fq_table = {
        fq_table(t["schema"], t["table"]): t["reltuples"] for t in tables
    }

    columns_out = []
    for row in raw_stats:
        fq_col = fq_column(row["schema"], row["table"], row["column"])
        fq_tbl = fq_table(row["schema"], row["table"])
        pseudonym = identifier_map.get(fq_col)
        if pseudonym is None:
            # A column pg_stats knows about but schema.py didn't enumerate
            # (shouldn't happen against a consistent snapshot); skip it
            # rather than emit an unpseudonymized identifier.
            continue

        type_name = type_by_fq_column.get(fq_col, "")
        type_class = classify_type(type_name)
        reltuples = reltuples_by_fq_table.get(fq_tbl)

        suppressed_freqs = suppress_small_table_stats(
            row["most_common_freqs"], reltuples
        )
        skew = mcv_skew_gini(row["most_common_freqs"], row["n_distinct"], reltuples)

        histogram = None
        if type_class in (TypeClass.TEMPORAL, TypeClass.NUMERIC) and row[
            "histogram_bounds"
        ]:
            bounds = [_parse_bound(b, type_class) for b in row["histogram_bounds"]]
            histogram = normalize_histogram(bounds, type_name)

        text_width = None
        if type_class is TypeClass.TEXT:
            text_width = text_width_transform(row["avg_width"])

        columns_out.append(
            {
                "pseudonym": pseudonym,
                "table_pseudonym": identifier_map.get(fq_tbl),
                "null_frac": row["null_frac"],
                "n_distinct": row["n_distinct"],
                "correlation": row["correlation"],
                "most_common_freqs": project_mcv_freqs(suppressed_freqs),
                "most_common_elem_freqs": project_mcv_freqs(
                    row["most_common_elem_freqs"]
                ),
                "elem_count_histogram": row["elem_count_histogram"],
                "skew_gini": skew,
                "attstattarget": stattarget_by_fq_column.get(fq_col),
                "histogram": histogram,
                "text_width": text_width,
            }
        )

    extended_entries = []
    for ext in extended_raw:
        fq_tbl = fq_table(ext["schema"], ext["table"])
        column_pseudonyms = [
            identifier_map.get(fq_column(ext["schema"], ext["table"], c))
            for c in ext["attnames"]
        ]
        extended_entries.append(
            {
                "table_pseudonym": identifier_map.get(fq_tbl),
                "column_pseudonyms": column_pseudonyms,
                "kinds": ext["kinds"],
                "n_distinct": ext["n_distinct"],
                "dependencies": ext["dependencies"],
                "most_common_freqs": project_mcv_freqs(ext["most_common_freqs"]),
            }
        )

    extended_stats_section: dict = (
        {"defined": len(extended_entries), "entries": extended_entries}
        if extended_entries
        else {"defined": 0}
    )

    now = dt.datetime.now(dt.timezone.utc)
    ages = []
    for info in last_analyze.values():
        ts = info["last_analyze"] or info["last_autoanalyze"]
        if ts is not None:
            ages.append((now - ts).total_seconds())

    section = {
        "columns": columns_out,
        "extended_stats": extended_stats_section,
        "completeness": build_completeness(
            available=True,
            coverage={
                "columns_with_stats": len(columns_out),
                "columns_total": len(columns),
                "extended_stats_defined": extended_stats_section["defined"],
            },
            staleness={"oldest_analyze_age_s": max(ages) if ages else None},
        ),
    }
    return section
