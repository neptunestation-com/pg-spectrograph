"""schema section: tables, columns, FK graph, partitioning (§6.2).

Every identifier this module discovers (schema, table, column names) is
pseudonymized before it leaves the module; the raw fetch_* helpers below are
the only place real identifiers exist in memory, and they never touch a row
value (I1) -- only catalog metadata.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass

import psycopg

from pgspec.completeness import build_completeness
from pgspec.pseudonym import assign_ordinals

_TABLES_SQL = """
    SELECT n.nspname, c.relname, c.oid, c.relkind, c.reltuples, c.relpages,
           c.relrowsecurity, c.relpersistence, c.reloptions,
           pg_table_size(c.oid) AS heap_bytes,
           pg_indexes_size(c.oid) AS index_bytes,
           pg_total_relation_size(c.oid) - pg_table_size(c.oid) - pg_indexes_size(c.oid)
               AS toast_bytes
    FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE c.relkind IN ('r', 'p', 'm')
      AND n.nspname NOT IN ('pg_catalog', 'information_schema')
      AND n.nspname NOT LIKE 'pg_toast%'
    ORDER BY 1, 2
"""

#: Shared with column_stats.py (attstattarget lives here since it's fetched
#: alongside every other pg_attribute fact in one pass).
COLUMNS_SQL = """
    SELECT n.nspname, c.relname, a.attname, a.attnum, a.attnotnull,
           a.atttypid::regtype::text AS type_name, a.atttypmod, a.attstorage,
           a.atthasdef, a.attidentity, a.attgenerated, a.attstattarget
    FROM pg_attribute a
    JOIN pg_class c ON c.oid = a.attrelid
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE c.relkind IN ('r', 'p', 'm')
      AND n.nspname NOT IN ('pg_catalog', 'information_schema')
      AND n.nspname NOT LIKE 'pg_toast%'
      AND a.attnum > 0 AND NOT a.attisdropped
    ORDER BY 1, 2, a.attnum
"""

_TRIGGER_COUNTS_SQL = """
    SELECT n.nspname, c.relname, count(*)
    FROM pg_trigger t
    JOIN pg_class c ON c.oid = t.tgrelid
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE NOT t.tgisinternal
    GROUP BY 1, 2
"""

_FK_SQL = """
    SELECT rn.nspname, rc.relname, fn.nspname, fc.relname,
           (SELECT array_agg(a.attname ORDER BY ord)
            FROM unnest(con.conkey) WITH ORDINALITY AS u(attnum, ord)
            JOIN pg_attribute a ON a.attrelid = con.conrelid AND a.attnum = u.attnum) AS from_cols,
           (SELECT array_agg(a.attname ORDER BY ord)
            FROM unnest(con.confkey) WITH ORDINALITY AS u(attnum, ord)
            JOIN pg_attribute a ON a.attrelid = con.confrelid AND a.attnum = u.attnum) AS to_cols
    FROM pg_constraint con
    JOIN pg_class rc ON rc.oid = con.conrelid
    JOIN pg_namespace rn ON rn.oid = rc.relnamespace
    JOIN pg_class fc ON fc.oid = con.confrelid
    JOIN pg_namespace fn ON fn.oid = fc.relnamespace
    WHERE con.contype = 'f'
"""

_PARTITIONED_SQL = """
    SELECT n.nspname, c.relname, p.partstrat,
           (SELECT array_agg(a.attname ORDER BY ord)
            FROM unnest(p.partattrs) WITH ORDINALITY AS u(attnum, ord)
            JOIN pg_attribute a ON a.attrelid = p.partrelid AND a.attnum = u.attnum)
    FROM pg_partitioned_table p
    JOIN pg_class c ON c.oid = p.partrelid
    JOIN pg_namespace n ON n.oid = c.relnamespace
"""

_PARTITION_MEMBERS_SQL = """
    SELECT pn.nspname, pc.relname,
           pg_total_relation_size(cc.oid) AS size_bytes,
           pg_get_expr(cc.relpartbound, cc.oid) = 'DEFAULT' AS is_default
    FROM pg_inherits i
    JOIN pg_class pc ON pc.oid = i.inhparent
    JOIN pg_namespace pn ON pn.oid = pc.relnamespace
    JOIN pg_class cc ON cc.oid = i.inhrelid
"""

_RELFILLFACTOR_PREFIX = "fillfactor="


def fq_table(schema: str, table: str) -> str:
    return f"{schema}.{table}"


def fq_column(schema: str, table: str, column: str) -> str:
    return f"{schema}.{table}.{column}"


def _extract_fillfactor(reloptions: list[str] | None) -> int | None:
    if not reloptions:
        return None
    for opt in reloptions:
        if opt.startswith(_RELFILLFACTOR_PREFIX):
            return int(opt[len(_RELFILLFACTOR_PREFIX) :])
    return None


def fetch_tables(conn: psycopg.Connection) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(_TABLES_SQL)
        rows = cur.fetchall()
    return [
        {
            "schema": schema,
            "table": table,
            "oid": oid,
            "relkind": relkind,
            "reltuples": reltuples,
            "relpages": relpages,
            "relrowsecurity": relrowsecurity,
            "relpersistence": relpersistence,
            "relfillfactor": _extract_fillfactor(reloptions),
            "heap_bytes": heap_bytes,
            "index_bytes": index_bytes,
            "toast_bytes": toast_bytes,
        }
        for (
            schema,
            table,
            oid,
            relkind,
            reltuples,
            relpages,
            relrowsecurity,
            relpersistence,
            reloptions,
            heap_bytes,
            index_bytes,
            toast_bytes,
        ) in rows
    ]


def fetch_columns(conn: psycopg.Connection) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(COLUMNS_SQL)
        rows = cur.fetchall()
    return [
        {
            "schema": schema,
            "table": table,
            "column": column,
            "attnum": attnum,
            "attnotnull": attnotnull,
            "type_name": type_name,
            "typmod": typmod,
            "attstorage": attstorage,
            "atthasdef": atthasdef,
            "attidentity": attidentity,
            "attgenerated": attgenerated,
            "attstattarget": attstattarget,
        }
        for (
            schema,
            table,
            column,
            attnum,
            attnotnull,
            type_name,
            typmod,
            attstorage,
            atthasdef,
            attidentity,
            attgenerated,
            attstattarget,
        ) in rows
    ]


def fetch_trigger_counts(conn: psycopg.Connection) -> dict[str, int]:
    with conn.cursor() as cur:
        cur.execute(_TRIGGER_COUNTS_SQL)
        rows = cur.fetchall()
    return {fq_table(schema, table): count for schema, table, count in rows}


def fetch_fk_graph(conn: psycopg.Connection) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(_FK_SQL)
        rows = cur.fetchall()
    return [
        {
            "from": fq_table(from_schema, from_table),
            "from_cols": [
                fq_column(from_schema, from_table, c) for c in from_cols
            ],
            "to": fq_table(to_schema, to_table),
            "to_cols": [fq_column(to_schema, to_table, c) for c in to_cols],
        }
        for from_schema, from_table, to_schema, to_table, from_cols, to_cols in rows
    ]


def fetch_partitioning(conn: psycopg.Connection) -> dict[str, dict]:
    with conn.cursor() as cur:
        cur.execute(_PARTITIONED_SQL)
        partitioned_rows = cur.fetchall()
        cur.execute(_PARTITION_MEMBERS_SQL)
        member_rows = cur.fetchall()

    members: dict[str, list[tuple[int, bool]]] = {}
    for parent_schema, parent_table, size_bytes, is_default in member_rows:
        key = fq_table(parent_schema, parent_table)
        members.setdefault(key, []).append((size_bytes, is_default))

    result: dict[str, dict] = {}
    for schema, table, strategy, key_cols in partitioned_rows:
        fq = fq_table(schema, table)
        sizes_and_defaults = members.get(fq, [])
        sizes = [s for s, _default in sizes_and_defaults]
        result[fq] = {
            "strategy": strategy,
            "key_columns": [fq_column(schema, table, c) for c in (key_cols or [])],
            "partition_count": len(sizes_and_defaults),
            "min_partition_bytes": min(sizes) if sizes else None,
            "median_partition_bytes": statistics.median(sizes) if sizes else None,
            "max_partition_bytes": max(sizes) if sizes else None,
            "default_partition_exists": any(d for _s, d in sizes_and_defaults),
        }
    return result


@dataclass
class SchemaCapture:
    section: dict
    identifier_map: dict[str, str]


def capture_schema(conn: psycopg.Connection, salt: bytes) -> SchemaCapture:
    """Build the schema section (§6.2), pseudonymizing every discovered
    schema/table/column identifier. Returns both the section dict and the
    real-name -> pseudonym map for those identifiers, so later sections that
    need to correlate against the same tables/columns (column_stats,
    indexes, workload) can reuse identical pseudonyms without re-deriving
    them: assign_ordinals is a pure function of (names, salt, prefix), so
    calling it again elsewhere over the same name universe yields the same
    result."""
    tables = fetch_tables(conn)
    columns = fetch_columns(conn)
    trigger_counts = fetch_trigger_counts(conn)
    fk_edges = fetch_fk_graph(conn)
    partitioning = fetch_partitioning(conn)

    schema_names = sorted({t["schema"] for t in tables})
    table_names = sorted({fq_table(t["schema"], t["table"]) for t in tables})
    column_names = sorted(
        {fq_column(c["schema"], c["table"], c["column"]) for c in columns}
    )

    schema_pseudonyms = assign_ordinals(schema_names, salt, prefix="s")
    table_pseudonyms = assign_ordinals(table_names, salt, prefix="t")
    column_pseudonyms = assign_ordinals(column_names, salt, prefix="c")

    identifier_map: dict[str, str] = {
        **schema_pseudonyms,
        **table_pseudonyms,
        **column_pseudonyms,
    }

    columns_by_table: dict[str, list[dict]] = {}
    for c in columns:
        fq_tbl = fq_table(c["schema"], c["table"])
        fq_col = fq_column(c["schema"], c["table"], c["column"])
        columns_by_table.setdefault(fq_tbl, []).append(
            {
                "pseudonym": column_pseudonyms[fq_col],
                "type_name": c["type_name"],
                "typmod": c["typmod"],
                "attnotnull": c["attnotnull"],
                "storage": c["attstorage"],
                "has_default": c["atthasdef"],
                "identity": c["attidentity"] or None,
                "generated": c["attgenerated"] or None,
                "ordinal_position": c["attnum"],
            }
        )

    table_entries = []
    for t in tables:
        fq_tbl = fq_table(t["schema"], t["table"])
        entry = {
            "schema_pseudonym": schema_pseudonyms[t["schema"]],
            "pseudonym": table_pseudonyms[fq_tbl],
            "relkind": t["relkind"],
            "reltuples": t["reltuples"],
            "relpages": t["relpages"],
            "heap_bytes": t["heap_bytes"],
            "index_bytes": t["index_bytes"],
            "toast_bytes": t["toast_bytes"],
            "relfillfactor": t["relfillfactor"],
            "relrowsecurity": t["relrowsecurity"],
            "relpersistence": t["relpersistence"],
            "trigger_count": trigger_counts.get(fq_tbl, 0),
            "columns": columns_by_table.get(fq_tbl, []),
        }
        if fq_tbl in partitioning:
            part = partitioning[fq_tbl]
            entry["partitioning"] = {
                "strategy": part["strategy"],
                "key_column_pseudonyms": [
                    column_pseudonyms[c] for c in part["key_columns"]
                ],
                "partition_count": part["partition_count"],
                "min_partition_bytes": part["min_partition_bytes"],
                "median_partition_bytes": part["median_partition_bytes"],
                "max_partition_bytes": part["max_partition_bytes"],
                "default_partition_exists": part["default_partition_exists"],
            }
        table_entries.append(entry)

    fk_graph = [
        {
            "from": table_pseudonyms[edge["from"]],
            "from_cols": [column_pseudonyms[c] for c in edge["from_cols"]],
            "to": table_pseudonyms[edge["to"]],
            "to_cols": [column_pseudonyms[c] for c in edge["to_cols"]],
        }
        for edge in fk_edges
    ]

    section = {
        "tables": table_entries,
        "fk_graph": fk_graph,
        "completeness": build_completeness(
            available=True,
            coverage={"tables_captured": len(table_entries)},
        ),
    }
    return SchemaCapture(section=section, identifier_map=identifier_map)
