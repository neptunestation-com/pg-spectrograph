"""derived section: read/write ratio, HOT fraction, FK-graph summary, and
friends (§6.7). Pure computation over already-captured sections -- no
database access here, so every formula is directly unit-testable.

Statement concentration is computed over both exec-time share and block-I/O
share (not exec-time alone): the two "disagree informatively" in exactly the
way §6.7 already expects read/write ratio's tup-level and statement-level
variants to, and for the same reason noted on the workload top-K lenses
(issue #1): exec_time is confounded by CPU speed and cache warmth at capture
time, while block I/O is a physical, hardware-invariant measure of data
movement.
"""

from __future__ import annotations

import math
import statistics


def read_write_ratio(activity_section: dict, workload_section: dict) -> dict:
    db = activity_section.get("database") or {}
    reads = (db.get("tup_returned") or 0) + (db.get("tup_fetched") or 0)
    writes = (
        (db.get("tup_inserted") or 0)
        + (db.get("tup_updated") or 0)
        + (db.get("tup_deleted") or 0)
    )
    tup_ratio = reads / writes if writes else None

    statements = workload_section.get("statements") or []
    read_statements = sum(1 for s in statements if s.get("verb") == "SELECT")
    write_statements = sum(
        1 for s in statements if s.get("verb") in ("INSERT", "UPDATE", "DELETE")
    )
    stmt_ratio = (
        read_statements / write_statements if write_statements else None
    )

    return {
        "tup_level": {"reads": reads, "writes": writes, "ratio": tup_ratio},
        "statement_level": {
            "reads": read_statements,
            "writes": write_statements,
            "ratio": stmt_ratio,
        },
    }


def hot_update_fraction(activity_section: dict) -> dict:
    tables = activity_section.get("user_tables") or []
    total_hot = sum(t.get("n_tup_hot_upd") or 0 for t in tables)
    total_upd = sum(t.get("n_tup_upd") or 0 for t in tables)
    aggregate = total_hot / total_upd if total_upd else None

    per_table = [
        (t.get("n_tup_hot_upd") or 0) / t["n_tup_upd"]
        for t in tables
        if t.get("n_tup_upd")
    ]
    return {
        "aggregate": aggregate,
        "per_table_min": min(per_table) if per_table else None,
        "per_table_median": statistics.median(per_table) if per_table else None,
        "per_table_max": max(per_table) if per_table else None,
    }


def index_scan_share(activity_section: dict) -> float | None:
    tables = activity_section.get("user_tables") or []
    total_idx_tup = sum(t.get("idx_tup_fetch") or 0 for t in tables)
    total_seq_tup = sum(t.get("seq_tup_read") or 0 for t in tables)
    denom = total_idx_tup + total_seq_tup
    return total_idx_tup / denom if denom else None


def wal_metrics(activity_section: dict) -> dict:
    db = activity_section.get("database") or {}
    wal = activity_section.get("wal") or {}
    xacts = (db.get("xact_commit") or 0) + (db.get("xact_rollback") or 0)
    wal_bytes = float(wal.get("wal_bytes") or 0)
    wal_records = wal.get("wal_records") or 0
    wal_fpi = wal.get("wal_fpi") or 0
    return {
        "wal_bytes_per_xact": wal_bytes / xacts if xacts else None,
        "wal_fpi_fraction": wal_fpi / wal_records if wal_records else None,
    }


def temp_spill(activity_section: dict, workload_section: dict) -> dict:
    db = activity_section.get("database") or {}
    xacts = (db.get("xact_commit") or 0) + (db.get("xact_rollback") or 0)
    temp_bytes = db.get("temp_bytes") or 0
    per_xact = temp_bytes / xacts if xacts else None

    statements = workload_section.get("statements") or []
    with_temp = sum(
        1
        for s in statements
        if (s.get("temp_blks_read") or 0) > 0 or (s.get("temp_blks_written") or 0) > 0
    )
    fraction = with_temp / len(statements) if statements else None

    return {
        "temp_bytes_per_xact": per_xact,
        "statements_with_temp_fraction": fraction,
    }


def cache_hit_ratios(activity_section: dict) -> dict:
    db = activity_section.get("database") or {}
    blks_hit = db.get("blks_hit") or 0
    blks_read = db.get("blks_read") or 0
    db_denom = blks_hit + blks_read
    db_level = blks_hit / db_denom if db_denom else None

    io_rows = activity_section.get("io") or []
    total_hits = sum(r.get("hits") or 0 for r in io_rows)
    total_reads = sum(r.get("reads") or 0 for r in io_rows)
    io_denom = total_hits + total_reads
    io_level = total_hits / io_denom if io_denom else None

    return {"db_level": db_level, "io_level": io_level}


def working_set_bound(schema_section: dict, shared_buffers_bytes: int | None) -> dict:
    """Crude working-set bound vs. shared_buffers (§6.7): the ratio of total
    captured table+index bytes to shared_buffers. A ratio near or below 1.0
    suggests the working set plausibly fits in cache; well above 1.0 does
    not -- a bound, not a promise (no access-pattern information here)."""
    tables = schema_section.get("tables") or []
    total_bytes = sum(
        (t.get("heap_bytes") or 0)
        + (t.get("index_bytes") or 0)
        + (t.get("toast_bytes") or 0)
        for t in tables
    )
    ratio = (
        total_bytes / shared_buffers_bytes
        if shared_buffers_bytes
        else None
    )
    return {"total_relation_bytes": total_bytes, "ratio_to_shared_buffers": ratio}


def _shannon_entropy_and_shares(weights: list[float]) -> dict:
    total = sum(weights)
    if not weights or total <= 0:
        return {"entropy_bits": None, "top1_share": None, "top10_share": None}
    shares = sorted((w / total for w in weights if w > 0), reverse=True)
    entropy = -sum(p * math.log2(p) for p in shares)
    return {
        "entropy_bits": entropy,
        "top1_share": shares[0] if shares else 0.0,
        "top10_share": sum(shares[:10]),
    }


def statement_concentration(workload_section: dict) -> dict:
    statements = workload_section.get("statements") or []
    exec_time_weights = [s.get("total_exec_time") or 0 for s in statements]
    blks_read_weights = [s.get("shared_blks_read") or 0 for s in statements]
    return {
        "exec_time": _shannon_entropy_and_shares(exec_time_weights),
        "blks_read": _shannon_entropy_and_shares(blks_read_weights),
    }


def fk_graph_summary(schema_section: dict) -> dict:
    """FK-graph summary (§6.7). fan_in (in-degree: distinct tables whose FK
    points at this one) detects a shared/hub dimension referenced by
    several other tables; fan_out (out-degree: distinct tables this one
    references) is the actual "fact-table detector" the spec names --
    a fact table in a star schema is characterized by many *outgoing* FKs
    to its dimensions, not incoming ones. Both are exposed rather than
    guessing which single direction the spec meant: a 6-dimension star
    schema's fact table has fan_out=6 but fan_in=0 for every table, which
    max_fan_in alone would completely miss.
    """
    edges = schema_section.get("fk_graph") or []
    nodes: set[str] = set()
    fan_in: dict[str, int] = {}
    fan_out: dict[str, int] = {}
    adjacency: dict[str, set[str]] = {}
    for edge in edges:
        src, dst = edge["from"], edge["to"]
        nodes.add(src)
        nodes.add(dst)
        fan_in[dst] = fan_in.get(dst, 0) + 1
        fan_out[src] = fan_out.get(src, 0) + 1
        adjacency.setdefault(src, set()).add(dst)
        adjacency.setdefault(dst, set()).add(src)

    # Connected components over the undirected adjacency (simple BFS).
    unvisited = set(nodes)
    components = 0
    while unvisited:
        components += 1
        stack = [unvisited.pop()]
        while stack:
            node = stack.pop()
            for neighbor in adjacency.get(node, ()):
                if neighbor in unvisited:
                    unvisited.discard(neighbor)
                    stack.append(neighbor)

    in_degrees = sorted(fan_in.values())
    return {
        "node_count": len(nodes),
        "edge_count": len(edges),
        "max_fan_in": max(fan_in.values()) if fan_in else 0,
        "max_fan_out": max(fan_out.values()) if fan_out else 0,
        "fan_in_median": statistics.median(in_degrees) if in_degrees else None,
        "connected_components": components,
    }


def table_size_distribution(schema_section: dict) -> dict:
    tables = schema_section.get("tables") or []
    sizes = [
        (t.get("heap_bytes") or 0)
        + (t.get("index_bytes") or 0)
        + (t.get("toast_bytes") or 0)
        for t in tables
    ]
    total = sum(sizes)
    shares = sorted((s / total for s in sizes), reverse=True) if total else []
    return {
        "total_bytes": total,
        "largest_table_share": shares[0] if shares else None,
        "size_share_quantiles": {
            "p50": statistics.median(shares) if shares else None,
            "p90": (
                statistics.quantiles(shares, n=10)[8]
                if len(shares) >= 2
                else (shares[0] if shares else None)
            ),
        },
    }


def dead_tuple_pressure(
    activity_section: dict, autovacuum_threshold_ratio: float = 0.2
) -> dict:
    tables = activity_section.get("user_tables") or []
    ratios = [
        (t.get("n_dead_tup") or 0) / t["n_live_tup"]
        for t in tables
        if t.get("n_live_tup")
    ]
    over_threshold = sum(1 for r in ratios if r > autovacuum_threshold_ratio)
    return {
        "max_ratio": max(ratios) if ratios else None,
        "median_ratio": statistics.median(ratios) if ratios else None,
        "tables_over_threshold": over_threshold,
    }


def index_redundancy(indexes_section: dict) -> dict:
    indexes = indexes_section.get("indexes") or []
    unused = set(indexes_section.get("unused_index_pseudonyms") or [])
    total_size = sum(idx.get("index_bytes") or 0 for idx in indexes)
    unused_size = sum(
        idx.get("index_bytes") or 0 for idx in indexes if idx["pseudonym"] in unused
    )
    return {
        "unused_count": len(unused),
        "unused_size_bytes": unused_size,
        "unused_size_share": unused_size / total_size if total_size else None,
    }


def compute_derived(
    schema_section: dict,
    indexes_section: dict,
    workload_section: dict,
    activity_section: dict,
    shared_buffers_bytes: int | None = None,
) -> dict:
    """Assemble the full derived section (§6.7) from already-captured
    sections. No database access; every value here is a pure function of
    its inputs, per the spec's "each value carries the names of its
    inputs" instruction (encoded as this function's own parameter list)."""
    return {
        "read_write_ratio": read_write_ratio(activity_section, workload_section),
        "hot_update_fraction": hot_update_fraction(activity_section),
        "index_scan_share": index_scan_share(activity_section),
        "wal": wal_metrics(activity_section),
        "temp_spill": temp_spill(activity_section, workload_section),
        "cache_hit_ratios": cache_hit_ratios(activity_section),
        "working_set_bound": working_set_bound(schema_section, shared_buffers_bytes),
        "statement_concentration": statement_concentration(workload_section),
        "fk_graph_summary": fk_graph_summary(schema_section),
        "table_size_distribution": table_size_distribution(schema_section),
        "dead_tuple_pressure": dead_tuple_pressure(activity_section),
        "index_redundancy": index_redundancy(indexes_section),
    }
