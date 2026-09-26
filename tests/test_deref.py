"""Tests for pgspec.deref (Milestone 10)."""

from __future__ import annotations

import hashlib
import json

from pgspec.capture import capture_point, write_artifact
from pgspec.deref import deref_artifact, load_map_file, verify_map_digest
from pgspec.validate import load_artifact


def test_deref_replaces_whole_field_pseudonyms():
    artifact = {"table_pseudonym": "t_0007", "nested": {"pseudonym": "c_0142"}}
    pseudonym_map = {"t_0007": "public.orders", "c_0142": "public.orders.customer_id"}
    result = deref_artifact(artifact, pseudonym_map)
    assert result["table_pseudonym"] == "public.orders"
    assert result["nested"]["pseudonym"] == "public.orders.customer_id"


def test_deref_replaces_pseudonyms_embedded_in_query_text():
    artifact = {"text": "SELECT c_0000, sum(c_0010) FROM t_0000 WHERE c_0000 = $1"}
    pseudonym_map = {
        "t_0000": "public.orders",
        "c_0000": "public.orders.customer_id",
        "c_0010": "public.orders.order_amount",
    }
    result = deref_artifact(artifact, pseudonym_map)
    assert result["text"] == (
        "SELECT public.orders.customer_id, sum(public.orders.order_amount) "
        "FROM public.orders WHERE public.orders.customer_id = $1"
    )


def test_deref_does_not_touch_unrelated_strings():
    artifact = {"verb": "SELECT", "notes": ["a genuinely unrelated sentence"]}
    result = deref_artifact(artifact, {"t_0000": "public.orders"})
    assert result == artifact


def test_deref_leaves_list_and_scalar_structures_intact():
    artifact = {"referenced_table_pseudonyms": ["t_0000", "t_0001"], "count": 3, "flag": True}
    pseudonym_map = {"t_0000": "public.orders", "t_0001": "public.customers"}
    result = deref_artifact(artifact, pseudonym_map)
    assert result["referenced_table_pseudonyms"] == ["public.orders", "public.customers"]
    assert result["count"] == 3
    assert result["flag"] is True


def test_verify_map_digest_matches(tmp_path):
    map_path = tmp_path / "spectrum-map.json"
    map_path.write_text('{"salt_digest": "x", "map": {"t_0000": "public.orders"}}')
    digest = hashlib.sha256(map_path.read_bytes()).hexdigest()
    assert verify_map_digest(str(map_path), f"sha256:{digest}") is True
    assert verify_map_digest(str(map_path), "sha256:wrongdigest") is False


def test_load_map_file_returns_pseudonym_to_name_map(tmp_path):
    map_path = tmp_path / "spectrum-map.json"
    map_path.write_text(
        json.dumps({"salt_digest": "x", "map": {"t_0000": "public.orders"}})
    )
    assert load_map_file(str(map_path)) == {"t_0000": "public.orders"}


def test_deref_round_trips_a_real_captured_artifact(pg16_dsn, tmp_path):
    map_path = tmp_path / "spectrum-map.json"
    out_path = tmp_path / "spectrum.json.gz"
    artifact = capture_point(
        pg16_dsn,
        top_k=20,
        salt_file=str(tmp_path / ".pgspec-salt"),
        map_path=str(map_path),
    )
    write_artifact(artifact, str(out_path))

    reloaded = load_artifact(str(out_path))
    assert verify_map_digest(str(map_path), reloaded["pseudonym_map_digest"])

    pseudonym_map = load_map_file(str(map_path))
    dereffed = deref_artifact(reloaded, pseudonym_map)

    real_table_names = {name for name in pseudonym_map.values() if name.count(".") == 1}
    assert "public.xq_orders" in real_table_names

    # After deref, no pseudonym token should survive anywhere in the
    # artifact -- neither as a whole field value nor embedded in
    # pseudonymized query text -- and the real names should be visible.
    serialized = json.dumps(dereffed)
    for pseudonym in pseudonym_map:
        assert pseudonym not in serialized, f"pseudonym {pseudonym} was not dereffed"
    assert "xq_orders" in serialized
    assert "customer_id" in serialized


def test_deref_rejects_mismatched_map_file(pg16_dsn, tmp_path):
    map_path_a = tmp_path / "map_a.json"
    map_path_b = tmp_path / "map_b.json"
    artifact_a = capture_point(
        pg16_dsn,
        top_k=5,
        salt_file=str(tmp_path / "salt_a"),
        map_path=str(map_path_a),
    )
    capture_point(
        pg16_dsn,
        top_k=5,
        salt_file=str(tmp_path / "salt_b"),
        map_path=str(map_path_b),
    )
    # map_b was produced by a different salt/capture: its digest must not
    # match artifact_a's recorded digest.
    assert not verify_map_digest(str(map_path_b), artifact_a["pseudonym_map_digest"])
