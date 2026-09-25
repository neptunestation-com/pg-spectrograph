"""Integration tests for full point-in-time artifact assembly (§5, Milestone 8)."""

from __future__ import annotations

import gzip
import json

from pgspec.capture import capture_point, write_artifact

from canary_tokens import CANARY_IDENTIFIERS, CANARY_LITERALS, find_canary_tokens


def test_capture_point_assembles_all_sections(pg16_dsn, tmp_path):
    artifact = capture_point(
        pg16_dsn,
        top_k=50,
        salt_file=str(tmp_path / ".pgspec-salt"),
        map_path=str(tmp_path / "spectrum-map.json"),
    )

    assert artifact["signature_version"] == "1.0"
    assert artifact["capture_mode"] == "point"
    assert artifact["extractor"]["name"] == "pgspec"
    for section_name in (
        "instance",
        "schema",
        "column_stats",
        "indexes",
        "workload",
        "activity",
    ):
        assert artifact[section_name]["completeness"]["available"] is True
    assert "read_write_ratio" in artifact["derived"]
    assert artifact["temporal"] is None
    assert artifact["pseudonym_map_digest"].startswith("sha256:")
    assert artifact["warnings"] == []


def test_capture_point_writes_readable_map_file(pg16_dsn, tmp_path):
    map_path = tmp_path / "spectrum-map.json"
    artifact = capture_point(
        pg16_dsn,
        top_k=10,
        salt_file=str(tmp_path / ".pgspec-salt"),
        map_path=str(map_path),
    )

    payload = json.loads(map_path.read_text())
    assert "salt_digest" in payload
    assert "public.xq_orders" in payload["map"].values()
    # The digest recorded in the artifact matches the file actually written.
    import hashlib

    assert artifact["pseudonym_map_digest"] == "sha256:" + hashlib.sha256(
        map_path.read_bytes()
    ).hexdigest()


def test_capture_point_pseudonym_map_digest_is_deterministic_per_salt(
    pg16_dsn, tmp_path
):
    salt_file = str(tmp_path / ".pgspec-salt")
    artifact_a = capture_point(
        pg16_dsn, top_k=10, salt_file=salt_file, map_path=str(tmp_path / "map_a.json")
    )
    artifact_b = capture_point(
        pg16_dsn, top_k=10, salt_file=salt_file, map_path=str(tmp_path / "map_b.json")
    )
    # The map's content depends only on discovered identifiers plus the
    # salt, never on time-varying counters, so it's identical across two
    # captures even though other sections (workload/activity) legitimately
    # differ due to pgspec's own queries being self-observed in between.
    assert artifact_a["pseudonym_map_digest"] == artifact_b["pseudonym_map_digest"]


def test_capture_point_full_artifact_contains_no_canary_tokens(pg16_dsn, tmp_path):
    artifact = capture_point(
        pg16_dsn,
        top_k=50,
        salt_file=str(tmp_path / ".pgspec-salt"),
        map_path=str(tmp_path / "spectrum-map.json"),
    )
    literal_hits = find_canary_tokens(artifact, CANARY_LITERALS)
    assert literal_hits == [], f"canary literal(s) leaked into full artifact: {literal_hits}"
    identifier_hits = find_canary_tokens(artifact, CANARY_IDENTIFIERS)
    assert identifier_hits == [], (
        f"canary identifier(s) leaked into full artifact: {identifier_hits}"
    )


def test_write_artifact_round_trips_and_sorts_keys(pg16_dsn, tmp_path):
    artifact = capture_point(
        pg16_dsn,
        top_k=5,
        salt_file=str(tmp_path / ".pgspec-salt"),
        map_path=str(tmp_path / "spectrum-map.json"),
    )
    out_path = tmp_path / "spectrum.json.gz"
    write_artifact(artifact, str(out_path))

    with gzip.open(out_path, "rt", encoding="utf-8") as f:
        raw_text = f.read()
    reloaded = json.loads(raw_text)
    assert reloaded == artifact

    # Sorted keys (§5): the top-level key order in the serialized text
    # matches alphabetical order.
    top_level_keys = list(json.loads(raw_text).keys())
    assert top_level_keys == sorted(top_level_keys)
