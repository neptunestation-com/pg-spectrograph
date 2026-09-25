"""Tests for pgspec.transforms (Milestone 2), written before the implementation."""

from __future__ import annotations

import math

import pytest

from pgspec.transforms import (
    TypeClass,
    classify_type,
    mcv_skew_gini,
    normalize_histogram,
    project_mcv_freqs,
    span_descriptor,
    suppress_small_table_stats,
    text_width_transform,
)


def test_classify_type():
    assert classify_type("timestamptz") is TypeClass.TEMPORAL
    assert classify_type("timestamp") is TypeClass.TEMPORAL
    assert classify_type("date") is TypeClass.TEMPORAL
    assert classify_type("int4") is TypeClass.NUMERIC
    assert classify_type("numeric") is TypeClass.NUMERIC
    assert classify_type("text") is TypeClass.TEXT
    assert classify_type("uuid") is TypeClass.TEXT
    assert classify_type("bytea") is TypeClass.TEXT
    assert classify_type("point") is TypeClass.OTHER


def test_normalize_histogram_basic_affine():
    result = normalize_histogram([0, 10, 20, 100], "int4")
    assert result["kind"] == "quantile_shape"
    assert result["n_bounds"] == 4
    assert result["positions"] == [0.0, 0.1, 0.2, 1.0]
    assert result["span"] == {"type_class": "numeric", "magnitude_oom": 2}


def test_normalize_histogram_temporal_span():
    result = normalize_histogram([1_700_000_000.0, 1_700_086_400.0], "timestamptz")
    assert result["span"] == {"type_class": "temporal", "magnitude_s": 86400.0}


def test_normalize_histogram_degenerate_returns_none():
    assert normalize_histogram([], "int4") is None
    assert normalize_histogram([5.0], "int4") is None


def test_normalize_histogram_equal_bounds_returns_none():
    assert normalize_histogram([5.0, 5.0], "int4") is None


def test_normalize_histogram_preserves_clustering():
    result = normalize_histogram([0, 1, 2, 3, 4, 90, 100], "int4")
    assert result["positions"][:6] == [0.0, 0.01, 0.02, 0.03, 0.04, 0.9]


def test_span_descriptor_zero_magnitude_numeric():
    assert span_descriptor(5, 5, "int4") == {"type_class": "numeric", "magnitude_oom": 0}


def test_mcv_skew_gini_uniform_is_zero():
    assert mcv_skew_gini([0.25] * 4, 4, 4) == pytest.approx(0.0, abs=1e-9)


def test_mcv_skew_gini_two_group_even_split():
    assert mcv_skew_gini([0.5, 0.5], 2, 2) == pytest.approx(0.0, abs=1e-9)


def test_mcv_skew_gini_extreme_skew_exact_oracle():
    assert mcv_skew_gini([0.99], -0.02, 1000) == pytest.approx(0.94, abs=1e-9)


def test_mcv_skew_gini_boolean_no_tail():
    assert mcv_skew_gini([0.7, 0.3], 2, 2) == pytest.approx(0.2, abs=1e-9)


def test_mcv_skew_gini_returns_none_when_uninformative():
    assert mcv_skew_gini(None, None, None) is None


def test_project_mcv_freqs_identity():
    assert project_mcv_freqs([0.4, 0.1]) == [0.4, 0.1]
    assert project_mcv_freqs(None) is None


def test_text_width_transform():
    assert text_width_transform(24.0) == {"avg_width": 24.0}
    assert text_width_transform(None) == {"avg_width": None}


def test_suppress_small_table_stats_passthrough_above_threshold():
    freqs = [0.6, 0.3]
    assert suppress_small_table_stats(freqs, reltuples=100) == freqs


def test_suppress_small_table_stats_coarsens_below_threshold():
    result = suppress_small_table_stats([0.05, 0.15, 0.25], reltuples=5)
    assert result == [0.1, 0.2, 0.3]


def test_suppress_small_table_stats_drops_entirely_below_floor():
    assert suppress_small_table_stats([0.6, 0.3], reltuples=2) is None


def test_suppress_small_table_stats_passes_through_missing_inputs():
    assert suppress_small_table_stats(None, reltuples=100) is None
    assert suppress_small_table_stats([0.5], reltuples=None) == [0.5]
