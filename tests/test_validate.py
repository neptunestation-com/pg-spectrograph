"""Tests for pgspec.validate (Milestone 8)."""

from __future__ import annotations

import gzip
import json

import pytest

from pgspec.capture import capture_point, write_artifact
from pgspec.validate import (
    scan_for_suspicious_strings,
    validate_artifact,
    validate_schema,
)


def _write_gz(tmp_path, obj) -> str:
    path = tmp_path / "artifact.json.gz"
    with gzip.open(path, "wt", encoding="utf-8") as f:
        json.dump(obj, f)
    return str(path)


def test_validate_accepts_a_real_captured_artifact(pg16_dsn, tmp_path):
    artifact = capture_point(
        pg16_dsn,
        top_k=20,
        salt_file=str(tmp_path / ".pgspec-salt"),
        map_path=str(tmp_path / "spectrum-map.json"),
    )
    out_path = tmp_path / "spectrum.json.gz"
    write_artifact(artifact, str(out_path))

    errors = validate_artifact(str(out_path))
    assert errors == []


def test_validate_schema_rejects_missing_required_top_level_key():
    artifact = {"signature_version": "1.0"}
    errors = validate_schema(artifact)
    assert any("captured_at" in e for e in errors)
    assert any("instance" in e for e in errors)


def test_validate_schema_rejects_wrong_capture_mode_enum():
    artifact = {
        "signature_version": "1.0",
        "captured_at": "now",
        "capture_mode": "not_a_real_mode",
        "capture_duration_s": 1.0,
        "extractor": {"name": "pgspec", "version": "0.1.0"},
        "instance": {"completeness": _ok_completeness()},
        "schema": {"completeness": _ok_completeness()},
        "column_stats": {"completeness": _ok_completeness()},
        "indexes": {"completeness": _ok_completeness()},
        "workload": {"completeness": _ok_completeness()},
        "activity": {"completeness": _ok_completeness()},
        "derived": {},
        "temporal": None,
        "pseudonym_map_digest": "sha256:abc",
        "warnings": [],
    }
    errors = validate_schema(artifact)
    assert any("capture_mode" in e for e in errors)


def _ok_completeness():
    return {"available": True, "coverage": {}, "staleness": {}, "limits": {}, "notes": []}


def test_validate_schema_rejects_missing_completeness_field():
    artifact = {
        "signature_version": "1.0",
        "captured_at": "now",
        "capture_mode": "point",
        "capture_duration_s": 1.0,
        "extractor": {"name": "pgspec", "version": "0.1.0"},
        "instance": {},  # missing "completeness"
        "schema": {"completeness": _ok_completeness()},
        "column_stats": {"completeness": _ok_completeness()},
        "indexes": {"completeness": _ok_completeness()},
        "workload": {"completeness": _ok_completeness()},
        "activity": {"completeness": _ok_completeness()},
        "derived": {},
        "temporal": None,
        "pseudonym_map_digest": "sha256:abc",
        "warnings": [],
    }
    errors = validate_schema(artifact)
    assert any("instance" in e and "completeness" in e for e in errors)


def test_scan_for_suspicious_strings_flags_email():
    findings = scan_for_suspicious_strings({"leaked": "alice@example.com"})
    assert any("email" in f for f in findings)


def test_scan_for_suspicious_strings_flags_uuid():
    findings = scan_for_suspicious_strings(
        {"leaked": "11111111-1111-1111-1111-111111111111"}
    )
    assert any("uuid" in f for f in findings)


def test_scan_for_suspicious_strings_silent_on_pseudonyms():
    clean = {
        "table_pseudonym": "t_0007",
        "column_pseudonym": "c_0142",
        "digest": "sha256:5a1b7f5d0b37549ba8bb778b8fe5c27a9fe21de419e6490f2dc00223ec509ea0",
        "captured_at": "2026-09-25T23:10:49.443520Z",
    }
    assert scan_for_suspicious_strings(clean) == []


def test_scan_for_suspicious_strings_flags_forbidden_key():
    findings = scan_for_suspicious_strings({"most_common_vals": ["should never be here"]})
    assert any("forbidden key" in f for f in findings)


def test_validate_artifact_reports_both_schema_and_string_scan_problems(tmp_path):
    bad = {"most_common_vals": ["x"], "leaked_email": "bob@example.com"}
    path = _write_gz(tmp_path, bad)
    errors = validate_artifact(path)
    assert any("missing required key" in e for e in errors)
    assert any("forbidden key" in e for e in errors)
    assert any("email" in e for e in errors)
