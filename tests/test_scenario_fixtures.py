"""Scenario fixture integration tests (§12, Milestone 12). Each scenario
loads once per test session (via the scenario_dsn factory fixture) and gets
a focused assertion proving the scenario's own distinctive signal, not a
re-test of extraction mechanics already covered exhaustively elsewhere.
"""

from __future__ import annotations

from pgspec.capture import capture_point, write_artifact
from pgspec.validate import validate_artifact


def test_f_star_fk_graph_shows_fact_table_via_fan_out(scenario_dsn, tmp_path):
    dsn = scenario_dsn("f_star")
    artifact = capture_point(
        dsn,
        top_k=10,
        salt_file=str(tmp_path / ".salt"),
        map_path=str(tmp_path / "map.json"),
    )
    fk = artifact["derived"]["fk_graph_summary"]
    assert fk["node_count"] == 7  # 6 dimensions + 1 fact table
    assert fk["edge_count"] == 6
    assert fk["max_fan_out"] == 6  # the fact table itself
    assert fk["max_fan_in"] == 1  # every dimension is referenced once
    assert fk["connected_components"] == 1

    out_path = tmp_path / "spectrum.json.gz"
    write_artifact(artifact, str(out_path))
    assert validate_artifact(str(out_path)) == []


def test_f_flat_has_no_foreign_keys(scenario_dsn, tmp_path):
    dsn = scenario_dsn("f_flat")
    artifact = capture_point(
        dsn,
        top_k=10,
        salt_file=str(tmp_path / ".salt"),
        map_path=str(tmp_path / "map.json"),
    )
    assert artifact["schema"]["fk_graph"] == []
    assert len(artifact["schema"]["tables"]) == 1
    assert len(artifact["schema"]["tables"][0]["columns"]) == 20  # id + 19 fields


def test_f_multitenant_has_default_partition(scenario_dsn, tmp_path):
    dsn = scenario_dsn("f_multitenant")
    artifact = capture_point(
        dsn,
        top_k=10,
        salt_file=str(tmp_path / ".salt"),
        map_path=str(tmp_path / "map.json"),
    )
    partitioned = [
        t for t in artifact["schema"]["tables"] if "partitioning" in t
    ]
    assert len(partitioned) == 1
    partitioning = partitioned[0]["partitioning"]
    assert partitioning["strategy"] == "l"
    assert partitioning["partition_count"] == 4
    assert partitioning["default_partition_exists"] is True


def test_f_skew_gini_exceeds_uniform_column(scenario_dsn, tmp_path):
    dsn = scenario_dsn("f_skew")
    artifact = capture_point(
        dsn,
        top_k=10,
        salt_file=str(tmp_path / ".salt"),
        map_path=str(tmp_path / "map.json"),
    )
    columns = artifact["column_stats"]["columns"]
    # Identify by table_pseudonym (there's exactly one table) and shape
    # (the skewed column has a much higher n_distinct estimate given the
    # long tail) -- resolve both via the schema section's column list.
    schema_columns = {
        c["pseudonym"]: c for t in artifact["schema"]["tables"] for c in t["columns"]
    }
    skew_by_pseudonym = {c["pseudonym"]: c["skew_gini"] for c in columns if c["skew_gini"] is not None}

    # category has a dominant value (60%) plus a long tail; uniform_category
    # is a flat 10-way split. The skewed column's Gini must be clearly
    # higher.
    text_columns = [
        p for p, c in schema_columns.items() if c["type_name"] == "text"
    ]
    ginis = {p: skew_by_pseudonym[p] for p in text_columns if p in skew_by_pseudonym}
    assert len(ginis) == 2
    assert max(ginis.values()) - min(ginis.values()) > 0.2


def test_f_writeheavy_shows_low_hot_fraction_and_dead_tuples(scenario_dsn, tmp_path):
    dsn = scenario_dsn("f_writeheavy")
    artifact = capture_point(
        dsn,
        top_k=10,
        salt_file=str(tmp_path / ".salt"),
        map_path=str(tmp_path / "map.json"),
    )
    hot = artifact["derived"]["hot_update_fraction"]
    dead = artifact["derived"]["dead_tuple_pressure"]
    assert hot["aggregate"] is not None
    assert hot["aggregate"] < 0.5  # updating an indexed column defeats HOT
    assert dead["max_ratio"] is not None
    assert dead["max_ratio"] > 0  # repeated UPDATEs with no VACUUM in between


def test_f_empty_degenerate_path_captures_cleanly(scenario_dsn, tmp_path):
    dsn = scenario_dsn("f_empty")
    artifact = capture_point(
        dsn,
        top_k=10,
        salt_file=str(tmp_path / ".salt"),
        map_path=str(tmp_path / "map.json"),
    )
    assert artifact["schema"]["tables"] == []
    assert artifact["schema"]["fk_graph"] == []
    assert artifact["column_stats"]["columns"] == []
    assert artifact["indexes"]["indexes"] == []

    out_path = tmp_path / "spectrum.json.gz"
    write_artifact(artifact, str(out_path))
    assert validate_artifact(str(out_path)) == []
