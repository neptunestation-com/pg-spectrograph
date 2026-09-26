"""Cross-version matrix tests (§12, Milestone 12): pgspec capture + validate
must succeed cleanly on every supported PostgreSQL major (14-17), with
version-appropriate completeness for version-gated views.
"""

from __future__ import annotations

import pytest

from pgspec.capture import capture_point, write_artifact
from pgspec.validate import validate_artifact

from canary_tokens import CANARY_IDENTIFIERS, CANARY_LITERALS, find_canary_tokens


@pytest.mark.parametrize("major", [14, 15, 16, 17])
def test_capture_and_validate_succeed_on_every_supported_major(
    major, pg_matrix_dsns, tmp_path
):
    dsn = pg_matrix_dsns[major]
    artifact = capture_point(
        dsn,
        top_k=20,
        salt_file=str(tmp_path / f".pgspec-salt-{major}"),
        map_path=str(tmp_path / f"spectrum-map-{major}.json"),
    )
    assert artifact["instance"]["server_version_num"] // 10000 == major

    out_path = tmp_path / f"spectrum-{major}.json.gz"
    write_artifact(artifact, str(out_path))
    errors = validate_artifact(str(out_path))
    assert errors == []


@pytest.mark.parametrize("major", [14, 15, 16, 17])
def test_checkpointer_and_io_completeness_are_version_appropriate(
    major, pg_matrix_dsns, tmp_path
):
    dsn = pg_matrix_dsns[major]
    artifact = capture_point(
        dsn,
        top_k=10,
        salt_file=str(tmp_path / f".pgspec-salt-vc-{major}"),
        map_path=str(tmp_path / f"spectrum-map-vc-{major}.json"),
    )
    activity = artifact["activity"]
    # pg_stat_checkpointer: PG17+ only.
    assert (activity["checkpointer"] is not None) == (major >= 17)
    # pg_stat_io: PG16+.
    assert (activity["completeness"]["coverage"]["io_available"]) == (major >= 16)


@pytest.mark.parametrize("major", [14, 15, 16, 17])
def test_no_canary_tokens_leak_on_any_supported_major(major, pg_matrix_dsns, tmp_path):
    dsn = pg_matrix_dsns[major]
    artifact = capture_point(
        dsn,
        top_k=20,
        salt_file=str(tmp_path / f".pgspec-salt-nc-{major}"),
        map_path=str(tmp_path / f"spectrum-map-nc-{major}.json"),
    )
    literal_hits = find_canary_tokens(artifact, CANARY_LITERALS)
    assert literal_hits == [], f"PG{major}: canary literal(s) leaked: {literal_hits}"
    identifier_hits = find_canary_tokens(artifact, CANARY_IDENTIFIERS)
    assert identifier_hits == [], f"PG{major}: canary identifier(s) leaked: {identifier_hits}"
