"""Tests for pgspec.completeness (Milestone 2), written before the implementation."""

from __future__ import annotations

from pgspec.completeness import build_completeness


def test_build_completeness_available_shape():
    result = build_completeness(
        available=True,
        coverage={"columns_with_stats_fraction": 0.98},
        staleness={"oldest_analyze_age_s": 3600},
        limits={"default_statistics_target": 100},
        notes=["something worth flagging"],
    )
    assert result == {
        "available": True,
        "coverage": {"columns_with_stats_fraction": 0.98},
        "staleness": {"oldest_analyze_age_s": 3600},
        "limits": {"default_statistics_target": 100},
        "notes": ["something worth flagging"],
    }


def test_build_completeness_defaults_to_empty_shape():
    result = build_completeness(available=True)
    assert result == {
        "available": True,
        "coverage": {},
        "staleness": {},
        "limits": {},
        "notes": [],
    }


def test_build_completeness_unavailable_includes_reason_in_notes():
    result = build_completeness(
        available=False, reason="pg_stat_statements not installed"
    )
    assert result["available"] is False
    assert result["notes"] == ["pg_stat_statements not installed"]
    assert result["coverage"] == {}
    assert result["staleness"] == {}
    assert result["limits"] == {}


def test_build_completeness_unavailable_without_reason_has_no_notes():
    result = build_completeness(available=False)
    assert result["available"] is False
    assert result["notes"] == []


def test_build_completeness_unavailable_reason_precedes_other_notes():
    result = build_completeness(
        available=False, reason="missing privilege", notes=["other note"]
    )
    assert result["notes"] == ["missing privilege", "other note"]
