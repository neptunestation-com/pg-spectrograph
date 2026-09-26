"""pgspec inspect: human-readable one-page summary of a captured artifact
(§13.6 resolution). No pseudonym map needed -- every value here is already
value-free and identifier-pseudonymized by construction.

Coverage is stated before conclusions, the same convention pgfr_analyze's
report() uses (issue #3 finding 10): a reader should see how much of the
picture is actually filled in before trusting what it says. Extended-stats
coverage and read/write ratio are given headline placement, not buried
among the rest of derived, per issue #2's findings 3 and 4.
"""

from __future__ import annotations


def _fmt(value, digits: int = 4) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:.{digits}g}"
    return str(value)


def _pct(value) -> str:
    if value is None:
        return "n/a"
    return f"{value * 100:.1f}%"


def render_coverage_table(artifact: dict) -> str:
    lines = ["| Section | Available | Notes |", "|---|---|---|"]
    for name in ("instance", "schema", "column_stats", "indexes", "workload", "activity"):
        section = artifact.get(name) or {}
        completeness = section.get("completeness") or {}
        available = completeness.get("available")
        notes = "; ".join(completeness.get("notes", [])) or "-"
        lines.append(f"| {name} | {'yes' if available else 'no'} | {notes} |")
    return "\n".join(lines)


def _human_bytes(n: int | None) -> str:
    if n is None:
        return "n/a"
    value = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(value) < 1024:
            return f"{value:.0f}{unit}" if unit == "B" else f"{value:.1f}{unit}"
        value /= 1024
    return f"{value:.1f}PB"


def render_instance_highlights(artifact: dict) -> str:
    from pgspec.capture import pg_setting_bytes

    instance = artifact.get("instance") or {}
    version_num = instance.get("server_version_num")
    major = version_num // 10000 if version_num else None
    extensions = instance.get("extensions") or {}
    guc = instance.get("guc_snapshot") or {}

    lines = [
        f"- PostgreSQL major version: {major if major else 'unknown'} "
        f"(server_version_num={version_num})",
        f"- Extensions: {', '.join(sorted(extensions)) or 'none'}",
    ]
    # Memory-shaped GUCs: convert (setting, unit) to actual bytes before
    # display -- "16384" + "8kB" concatenated as text reads as a nonsense
    # number ("163848kB"), not the 128MB it actually means.
    for key in ("shared_buffers", "work_mem"):
        setting = guc.get(key)
        if setting:
            byte_value = pg_setting_bytes(setting.get("setting"), setting.get("unit"))
            lines.append(f"- {key}: {_human_bytes(byte_value)}")
    for key in ("max_connections", "wal_level"):
        setting = guc.get(key)
        if setting:
            lines.append(f"- {key}: {setting.get('setting')}")
    return "\n".join(lines)


def render_extended_stats_headline(artifact: dict) -> str:
    column_stats = artifact.get("column_stats") or {}
    extended = column_stats.get("extended_stats") or {}
    defined = extended.get("defined", 0)
    if defined == 0:
        return (
            "No extended statistics (CREATE STATISTICS) are defined on this "
            "database -- cross-column correlation is a known blind spot for "
            "any synthesis downstream of this signature."
        )
    return (
        f"{defined} extended-statistics object(s) defined, capturing "
        "cross-column correlation beyond marginal per-column statistics."
    )


def render_derived_summary(artifact: dict) -> str:
    derived = artifact.get("derived") or {}
    lines: list[str] = []

    rw = derived.get("read_write_ratio") or {}
    tup = rw.get("tup_level") or {}
    stmt = rw.get("statement_level") or {}
    lines.append("### Read/write ratio")
    lines.append(
        f"- tuple-level: {_fmt(tup.get('ratio'))} "
        f"(reads={tup.get('reads')}, writes={tup.get('writes')})"
    )
    lines.append(
        f"- statement-level: {_fmt(stmt.get('ratio'))} "
        f"(reads={stmt.get('reads')}, writes={stmt.get('writes')})"
    )

    hot = derived.get("hot_update_fraction") or {}
    lines.append("\n### HOT update fraction")
    lines.append(f"- aggregate: {_pct(hot.get('aggregate'))}")
    lines.append(
        f"- per-table range: {_pct(hot.get('per_table_min'))} - "
        f"{_pct(hot.get('per_table_max'))}"
    )

    lines.append("\n### Index usage")
    lines.append(
        f"- index-scan share (tuple-weighted): {_pct(derived.get('index_scan_share'))}"
    )

    wal = derived.get("wal") or {}
    lines.append("\n### WAL")
    lines.append(f"- bytes per transaction: {_fmt(wal.get('wal_bytes_per_xact'))}")
    lines.append(f"- full-page-image fraction: {_pct(wal.get('wal_fpi_fraction'))}")

    temp = derived.get("temp_spill") or {}
    lines.append("\n### Temp spill")
    lines.append(f"- bytes per transaction: {_fmt(temp.get('temp_bytes_per_xact'))}")
    lines.append(
        f"- statements with temp usage: {_pct(temp.get('statements_with_temp_fraction'))}"
    )

    cache = derived.get("cache_hit_ratios") or {}
    lines.append("\n### Cache hit ratio")
    lines.append(f"- database-level: {_pct(cache.get('db_level'))}")
    lines.append(f"- pg_stat_io-level: {_pct(cache.get('io_level'))}")

    working_set = derived.get("working_set_bound") or {}
    lines.append("\n### Working-set bound (crude)")
    lines.append(
        f"- total relation bytes: {_fmt(working_set.get('total_relation_bytes'))}, "
        f"ratio to shared_buffers: {_fmt(working_set.get('ratio_to_shared_buffers'))}"
    )

    concentration = derived.get("statement_concentration") or {}
    exec_time = concentration.get("exec_time") or {}
    blks_read = concentration.get("blks_read") or {}
    lines.append("\n### Statement concentration")
    lines.append(
        f"- by exec time: entropy={_fmt(exec_time.get('entropy_bits'))} bits, "
        f"top1={_pct(exec_time.get('top1_share'))}, top10={_pct(exec_time.get('top10_share'))}"
    )
    lines.append(
        f"- by block I/O (hardware-invariant lens): "
        f"entropy={_fmt(blks_read.get('entropy_bits'))} bits, "
        f"top1={_pct(blks_read.get('top1_share'))}, top10={_pct(blks_read.get('top10_share'))}"
    )

    fk = derived.get("fk_graph_summary") or {}
    lines.append("\n### FK graph")
    lines.append(
        f"- nodes={fk.get('node_count')}, edges={fk.get('edge_count')}, "
        f"max fan-in={fk.get('max_fan_in')}, "
        f"connected components={fk.get('connected_components')}"
    )

    sizes = derived.get("table_size_distribution") or {}
    lines.append("\n### Table size distribution")
    lines.append(
        f"- total bytes={_fmt(sizes.get('total_bytes'))}, "
        f"largest table share={_pct(sizes.get('largest_table_share'))}"
    )

    dead = derived.get("dead_tuple_pressure") or {}
    lines.append("\n### Dead-tuple pressure")
    lines.append(
        f"- max ratio={_pct(dead.get('max_ratio'))}, "
        f"tables over threshold={dead.get('tables_over_threshold')}"
    )

    redundancy = derived.get("index_redundancy") or {}
    lines.append("\n### Index redundancy")
    lines.append(
        f"- unused count={redundancy.get('unused_count')}, "
        f"unused size share={_pct(redundancy.get('unused_size_share'))}"
    )

    return "\n".join(lines)


def render_inspect(artifact: dict) -> str:
    lines = [
        "# pgspec spectral summary",
        "",
        f"Captured: {artifact.get('captured_at')} "
        f"(mode: {artifact.get('capture_mode')}, "
        f"duration: {artifact.get('capture_duration_s')}s)",
        "",
        "## Coverage",
        "",
        render_coverage_table(artifact),
        "",
        "## Instance",
        "",
        render_instance_highlights(artifact),
        "",
        "## Extended statistics coverage",
        "",
        render_extended_stats_headline(artifact),
        "",
        "## Derived performance signature",
        "",
        render_derived_summary(artifact),
        "",
    ]
    return "\n".join(lines)
