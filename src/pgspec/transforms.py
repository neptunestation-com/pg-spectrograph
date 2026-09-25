"""Value-free statistical transforms: histogram normalization, MCV projection,
the skew statistic, and small-table suppression (I1, §7.1-7.3).

Every function here takes already-fetched, value-free-in-shape statistics
(counts, frequencies, widths, bounds) and returns a value-free-in-content
shape. None of these functions ever see or return an actual row value.
"""

from __future__ import annotations

import math
from enum import Enum

#: Static type-name -> type-class table. Covers both short (pg_catalog typname)
#: and long (SQL standard) spellings, since callers may source either.
_TEMPORAL_TYPE_NAMES = {
    "date",
    "time",
    "timetz",
    "time with time zone",
    "time without time zone",
    "timestamp",
    "timestamptz",
    "timestamp with time zone",
    "timestamp without time zone",
}

_NUMERIC_TYPE_NAMES = {
    "smallint",
    "int2",
    "integer",
    "int4",
    "bigint",
    "int8",
    "real",
    "float4",
    "double precision",
    "float8",
    "numeric",
    "decimal",
    "money",
    "oid",
}

_TEXT_TYPE_NAMES = {
    "text",
    "varchar",
    "character varying",
    "char",
    "character",
    "bpchar",
    "uuid",
    "bytea",
    "name",
}


class TypeClass(str, Enum):
    TEMPORAL = "temporal"
    NUMERIC = "numeric"
    TEXT = "text"
    OTHER = "other"


def classify_type(type_name: str) -> TypeClass:
    """Classify a PostgreSQL type name into the coarse class that determines
    which transform applies: temporal/numeric get histogram normalization
    (§7.1), text gets width-only treatment (§7.3), everything else is
    unsupported for shape transforms and passes through as OTHER."""
    name = type_name.lower()
    if name in _TEMPORAL_TYPE_NAMES:
        return TypeClass.TEMPORAL
    if name in _NUMERIC_TYPE_NAMES:
        return TypeClass.NUMERIC
    if name in _TEXT_TYPE_NAMES:
        return TypeClass.TEXT
    return TypeClass.OTHER


def span_descriptor(lo: float, hi: float, type_name: str) -> dict:
    """Magnitude-only span descriptor (§7.1): raw seconds for temporal types
    (low disclosure risk, high synthesis value); order-of-magnitude bucketed
    for numerics (extra caution -- a salary column's raw span is sensitive,
    its OOM is not), per the §13.3 resolution."""
    type_class = classify_type(type_name)
    if type_class is TypeClass.TEMPORAL:
        return {"type_class": "temporal", "magnitude_s": round(float(hi - lo), 6)}
    magnitude = abs(hi - lo)
    magnitude_oom = math.floor(math.log10(magnitude)) if magnitude > 0 else 0
    return {"type_class": "numeric", "magnitude_oom": magnitude_oom}


def normalize_histogram(bounds: list[float], type_name: str) -> dict | None:
    """Histogram bounds -> normalized quantile shape (§7.1). Affine
    normalization preserves skew, clustering, and gaps while revealing no
    endpoint. Returns None when there aren't enough bounds to describe a
    shape, or when the bounds are degenerate (no span) -- the same
    "no histogram" case column_stats.py falls back to MCV-freqs-only for.
    """
    if len(bounds) < 2:
        return None
    lo, hi = bounds[0], bounds[-1]
    if hi == lo:
        return None
    positions = [(b - lo) / (hi - lo) for b in bounds]
    return {
        "kind": "quantile_shape",
        "n_bounds": len(bounds),
        "positions": positions,
        "span": span_descriptor(lo, hi, type_name),
    }


def mcv_skew_gini(
    most_common_freqs: list[float] | None,
    n_distinct: float | None,
    reltuples: float | None,
) -> float | None:
    """Continuous skew statistic replacing the boolean log_scale_hint
    (issue #2 finding 2): a weighted Lorenz-curve Gini coefficient computed
    entirely from already-fetched, value-free fields -- MCV frequencies plus
    n_distinct/reltuples (both pass-through fields elsewhere in the artifact,
    so this adds no new leak surface).

    The untracked tail beyond the captured MCV list is modeled as one
    synthetic uniform-mass group (a documented approximation, not new
    information) so the Gini reflects the whole distribution, not just the
    captured head. Returns None when there's nothing to compute from.
    """
    freqs = list(most_common_freqs) if most_common_freqs else []
    mcv_mass = sum(freqs)
    remainder = max(0.0, 1.0 - mcv_mass)

    abs_distinct: int | None = None
    if n_distinct is not None and reltuples is not None:
        if n_distinct < 0:
            abs_distinct = round(-n_distinct * reltuples)
        else:
            abs_distinct = round(n_distinct)

    groups: list[tuple[float, float]] = [(f, 1.0) for f in freqs]
    if abs_distinct is not None:
        tail_count = max(abs_distinct - len(freqs), 0)
        if tail_count > 0 and remainder > 0:
            groups.append((remainder / tail_count, float(tail_count)))

    if not groups:
        return None

    total_value = sum(v * w for v, w in groups)
    total_weight = sum(w for _v, w in groups)
    if total_value <= 0 or total_weight <= 0:
        return None

    groups.sort(key=lambda g: g[0])

    cum_share = 0.0
    lorenz_area = 0.0
    for value, weight in groups:
        v_share = (value * weight) / total_value
        w_share = weight / total_weight
        new_cum_share = cum_share + v_share
        lorenz_area += w_share * (cum_share + new_cum_share)
        cum_share = new_cum_share

    return 1.0 - lorenz_area


def project_mcv_freqs(most_common_freqs: list[float] | None) -> list[float] | None:
    """Identity projection: the sole legitimate consumer of most_common_freqs.
    most_common_vals is never selected in any extraction query (§7.2); this
    function exists so the "forbidden column" test has exactly one function
    to point at as the intended reader of frequency data."""
    return list(most_common_freqs) if most_common_freqs is not None else None


def text_width_transform(avg_width: float | None) -> dict:
    """Text/uuid/bytea columns only ever expose avg_width (§7.3) -- querying
    user tables for length quantiles is forbidden even for read-only length
    stats (I3)."""
    return {"avg_width": avg_width}


def suppress_small_table_stats(
    most_common_freqs: list[float] | None,
    reltuples: float | None,
    min_rows: int = 10,
    floor_rows: int = 3,
    bucket: float = 0.1,
) -> list[float] | None:
    """Low-reltuples disclosure suppression (issue #2 finding 1): exact MCV
    frequencies on a tiny table are quasi-identifying even with zero literal
    values present (e.g. a 3-row table's exact 33/33/33% split). Below
    min_rows, frequencies are coarsened by rounding up (never down, so the
    transform never understates true concentration) to the nearest bucket;
    below floor_rows, the MCV list is dropped entirely.
    """
    if most_common_freqs is None or reltuples is None:
        return most_common_freqs
    if reltuples < floor_rows:
        return None
    if reltuples >= min_rows:
        return list(most_common_freqs)
    return [round(min(1.0, math.ceil(f / bucket) * bucket), 10) for f in most_common_freqs]
