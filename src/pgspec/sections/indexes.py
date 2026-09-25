"""indexes section: access methods, column lists, partial predicates, pgvector (§6.4).

Expression-index and partial-index-predicate columns are resolved via
pg_depend, never via the actual expression/predicate text (pg_get_expr on
indpred embeds any literal the predicate compares against, e.g. `WHERE
status = 'shipped'` -- exactly the kind of value I1 forbids). Only the
referenced-column set is captured, never the expression or predicate itself.
"""

from __future__ import annotations

import psycopg

from pgspec.completeness import build_completeness
from pgspec.pseudonym import assign_ordinals
from pgspec.sections.schema import fq_column, fq_table

_INDEXES_SQL = """
    SELECT n.nspname, t.relname AS table_name, c.relname AS index_name,
           am.amname, i.indisunique, i.indisprimary,
           (SELECT array_agg(a.attname ORDER BY ord)
            FROM unnest(i.indkey) WITH ORDINALITY AS u(attnum, ord)
            LEFT JOIN pg_attribute a
                   ON a.attrelid = i.indrelid AND a.attnum = u.attnum) AS key_columns,
           (SELECT array_agg(opc.opcname ORDER BY ord)
            FROM unnest(i.indclass) WITH ORDINALITY AS u(opcoid, ord)
            JOIN pg_opclass opc ON opc.oid = u.opcoid) AS opclasses,
           i.indexprs IS NOT NULL AS has_expression,
           i.indpred IS NOT NULL AS has_predicate,
           pg_relation_size(i.indexrelid) AS index_bytes,
           c.reloptions,
           s.idx_scan, s.idx_tup_read, s.idx_tup_fetch
    FROM pg_index i
    JOIN pg_class c ON c.oid = i.indexrelid
    JOIN pg_class t ON t.oid = i.indrelid
    JOIN pg_namespace n ON n.oid = c.relnamespace
    JOIN pg_am am ON am.oid = c.relam
    LEFT JOIN pg_stat_user_indexes s ON s.indexrelid = i.indexrelid
    WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')
      AND n.nspname NOT LIKE 'pg_toast%'
    ORDER BY 1, 2, 3
"""

#: Expression-index and partial-index referenced columns, resolved via
#: pg_depend rather than by reading indexprs/indpred text. A single query
#: covers both cases; the pack draws them as two conceptually separate
#: fields (§6.4), but an index that is both expression-keyed and partial
#: would have both fields backed by the same underlying dependency set --
#: an accepted, documented approximation (this tool never parses expression
#: trees, by design).
_DEPEND_COLUMNS_SQL = """
    SELECT n.nspname, ic.relname AS index_name, a.attname
    FROM pg_depend dep
    JOIN pg_class ic ON ic.oid = dep.objid
    JOIN pg_namespace n ON n.oid = ic.relnamespace
    JOIN pg_index i ON i.indexrelid = dep.objid
    JOIN pg_attribute a ON a.attrelid = dep.refobjid AND a.attnum = dep.refobjsubid
    WHERE dep.classid = 'pg_class'::regclass
      AND dep.refclassid = 'pg_class'::regclass
      AND dep.refobjsubid > 0
      AND (i.indexprs IS NOT NULL OR i.indpred IS NOT NULL)
      AND n.nspname NOT IN ('pg_catalog', 'information_schema')
      AND n.nspname NOT LIKE 'pg_toast%'
"""

#: reloptions keys worth surfacing for vector index access methods: these
#: are structural (index-build parameters), not data (§6.4).
_VECTOR_AMS = {"hnsw", "ivfflat"}


def _parse_reloptions(reloptions: list[str] | None) -> dict[str, str]:
    if not reloptions:
        return {}
    parsed = {}
    for opt in reloptions:
        if "=" in opt:
            key, _sep, value = opt.partition("=")
            parsed[key] = value
    return parsed


def fetch_indexes(conn: psycopg.Connection) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(_INDEXES_SQL)
        rows = cur.fetchall()
    return [
        {
            "schema": schema,
            "table": table,
            "index": index,
            "amname": amname,
            "is_unique": is_unique,
            "is_primary": is_primary,
            "key_columns": key_columns or [],
            "opclasses": opclasses or [],
            "has_expression": has_expression,
            "has_predicate": has_predicate,
            "index_bytes": index_bytes,
            "reloptions": reloptions,
            "idx_scan": idx_scan,
            "idx_tup_read": idx_tup_read,
            "idx_tup_fetch": idx_tup_fetch,
        }
        for (
            schema,
            table,
            index,
            amname,
            is_unique,
            is_primary,
            key_columns,
            opclasses,
            has_expression,
            has_predicate,
            index_bytes,
            reloptions,
            idx_scan,
            idx_tup_read,
            idx_tup_fetch,
        ) in rows
    ]


def fetch_depend_columns(conn: psycopg.Connection) -> dict[str, set[str]]:
    """fq index name -> set of real column names referenced by its
    expression and/or partial predicate, per pg_depend."""
    with conn.cursor() as cur:
        cur.execute(_DEPEND_COLUMNS_SQL)
        rows = cur.fetchall()
    result: dict[str, set[str]] = {}
    for schema, index_name, attname in rows:
        result.setdefault(fq_table(schema, index_name), set()).add(attname)
    return result


def capture_indexes(
    conn: psycopg.Connection, identifier_map: dict[str, str], salt: bytes
) -> dict:
    """Build the indexes section (§6.4). Index pseudonyms are assigned here
    (schema.py doesn't cover them); table/column pseudonyms are reused from
    identifier_map."""
    indexes = fetch_indexes(conn)
    depend_columns = fetch_depend_columns(conn)

    index_names = sorted({fq_table(idx["schema"], idx["index"]) for idx in indexes})
    index_pseudonyms = assign_ordinals(index_names, salt, prefix="i")

    entries = []
    unused_index_pseudonyms = []
    for idx in indexes:
        fq_idx = fq_table(idx["schema"], idx["index"])
        fq_tbl = fq_table(idx["schema"], idx["table"])
        pseudonym = index_pseudonyms[fq_idx]

        key_column_pseudonyms = [
            identifier_map.get(fq_column(idx["schema"], idx["table"], c))
            if c is not None
            else None
            for c in idx["key_columns"]
        ]

        referenced = depend_columns.get(fq_idx, set())
        referenced_pseudonyms = sorted(
            identifier_map[fq_column(idx["schema"], idx["table"], c)]
            for c in referenced
            if fq_column(idx["schema"], idx["table"], c) in identifier_map
        )

        entry = {
            "pseudonym": pseudonym,
            "table_pseudonym": identifier_map.get(fq_tbl),
            "access_method": idx["amname"],
            "key_column_pseudonyms": key_column_pseudonyms,
            "opclasses": idx["opclasses"],
            "is_unique": idx["is_unique"],
            "is_primary": idx["is_primary"],
            "is_expression": idx["has_expression"],
            "expression_columns": referenced_pseudonyms if idx["has_expression"] else None,
            "is_partial": idx["has_predicate"],
            "predicate_columns": referenced_pseudonyms if idx["has_predicate"] else None,
            "index_bytes": idx["index_bytes"],
            "idx_scan": idx["idx_scan"],
            "idx_tup_read": idx["idx_tup_read"],
            "idx_tup_fetch": idx["idx_tup_fetch"],
        }
        if idx["amname"] in _VECTOR_AMS:
            entry["vector_options"] = _parse_reloptions(idx["reloptions"])

        entries.append(entry)
        if idx["idx_scan"] == 0:
            unused_index_pseudonyms.append(pseudonym)

    section = {
        "indexes": entries,
        "unused_index_pseudonyms": unused_index_pseudonyms,
        "completeness": build_completeness(
            available=True,
            coverage={"indexes_captured": len(entries)},
        ),
    }
    return section
