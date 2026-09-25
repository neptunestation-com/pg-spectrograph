"""Two-sample capture mode (§8): reset-aware per-second rate computation
between a narrow counter snapshot (sample A) and a full point-in-time
capture taken --interval seconds later (sample B).

Every rate function here preserves the same key names and list shapes as
the section it rates, so a rate dict is a drop-in replacement for the raw
section when fed into derived.py's compute_derived(): activity/indexes/
workload's derived formulas read specific named fields regardless of
whether those fields hold lifetime cumulative values or per-second rates,
which is exactly how §8's "rates feed the derived section preferentially
over lifetime averages when present" is satisfied, with no changes needed
to derived.py itself.
"""

from __future__ import annotations

import decimal
from collections.abc import Callable


def _is_number(value) -> bool:
    # Decimal matters here, not just int/float: psycopg decodes Postgres
    # `numeric` columns (wal_bytes, most notably) as Decimal, never float.
    return isinstance(value, (int, float, decimal.Decimal)) and not isinstance(
        value, bool
    )


def rate_value(a, b, interval_s: float):
    """(b - a) / interval_s for numeric a/b. Reset-aware (§8): a negative
    delta means the counter reset mid-capture, so this returns None rather
    than a negative or fabricated rate -- never a silent wrong number.
    Non-numeric values (labels, identifiers) pass through as b unchanged.
    """
    if not (_is_number(a) and _is_number(b)):
        return b
    delta = float(b) - float(a)
    if delta < 0:
        return None
    return delta / interval_s if interval_s > 0 else None


def rate_dict(
    a: dict | None, b: dict | None, interval_s: float, exclude: frozenset[str] = frozenset()
) -> dict | None:
    """Rate every numeric key in b against a, except `exclude` (identity
    fields that happen to be numeric -- most importantly whatever key two
    matched rows were joined on, e.g. queryid: a bigint, not a counter,
    which must never be "rated" into a meaningless fractional value that
    breaks every downstream lookup keyed on it).
    """
    if a is None or b is None:
        return b
    return {
        key: (b[key] if key in exclude else rate_value(a.get(key), b.get(key), interval_s))
        for key in b
    }


def _row_key(row: dict, key: str | Callable[[dict], object]):
    return key(row) if callable(key) else row[key]


def rate_list_by_key(
    a_list: list[dict] | None,
    b_list: list[dict] | None,
    key: str | Callable[[dict], object],
    interval_s: float,
) -> tuple[list[dict], list[str], list[str]]:
    """Match rows between two samples by `key` (a field name, or a callable
    computing a composite key e.g. for pg_stat_io's backend_type/object/
    context triple), rating each matched pair. A row present in b but not a
    is returned as-is (nothing to rate against yet -- e.g. a query that
    started running during the interval). Returns (rated_rows, only_in_a,
    only_in_b): the latter two are the eviction-churn record (§8) for
    whichever caller needs it (workload's queryids, in particular).
    """
    a_list = a_list or []
    b_list = b_list or []
    a_by_key = {_row_key(r, key): r for r in a_list}
    b_by_key = {_row_key(r, key): r for r in b_list}
    # A plain field-name key must never itself be rated (see rate_dict's
    # docstring); a callable (composite) key is always built from label-
    # shaped fields in practice, so there's nothing to exclude there.
    exclude = frozenset({key}) if isinstance(key, str) else frozenset()

    rated_rows = []
    for k, b_row in b_by_key.items():
        a_row = a_by_key.get(k)
        rated_rows.append(
            rate_dict(a_row, b_row, interval_s, exclude=exclude) if a_row else b_row
        )

    only_in_a = sorted(str(k) for k in (set(a_by_key) - set(b_by_key)))
    only_in_b = sorted(str(k) for k in (set(b_by_key) - set(a_by_key)))
    return rated_rows, only_in_a, only_in_b


def compute_activity_rates(sample_a: dict, sample_b: dict, interval_s: float) -> dict:
    io_rates, _, _ = rate_list_by_key(
        sample_a.get("io"),
        sample_b.get("io"),
        key=lambda r: (r["backend_type"], r["object"], r["context"]),
        interval_s=interval_s,
    )
    tables_rates, _, _ = rate_list_by_key(
        sample_a.get("user_tables"),
        sample_b.get("user_tables"),
        key="table_pseudonym",
        interval_s=interval_s,
    )
    functions_rates, _, _ = rate_list_by_key(
        sample_a.get("user_functions"),
        sample_b.get("user_functions"),
        key="pseudonym",
        interval_s=interval_s,
    )
    return {
        "database": rate_dict(sample_a.get("database"), sample_b.get("database"), interval_s),
        "database_conflicts": rate_dict(
            sample_a.get("database_conflicts"),
            sample_b.get("database_conflicts"),
            interval_s,
        ),
        "bgwriter": rate_dict(sample_a.get("bgwriter"), sample_b.get("bgwriter"), interval_s),
        "checkpointer": rate_dict(
            sample_a.get("checkpointer"), sample_b.get("checkpointer"), interval_s
        ),
        "wal": rate_dict(sample_a.get("wal"), sample_b.get("wal"), interval_s),
        "io": io_rates,
        "user_tables": tables_rates,
        "user_functions": functions_rates,
    }


def compute_indexes_rates(sample_a: dict, sample_b: dict, interval_s: float) -> dict:
    rated, _, _ = rate_list_by_key(
        sample_a.get("indexes"), sample_b.get("indexes"), key="pseudonym", interval_s=interval_s
    )
    return {
        "indexes": rated,
        "unused_index_pseudonyms": sample_b.get("unused_index_pseudonyms", []),
    }


def compute_workload_rates(sample_a: dict, sample_b: dict, interval_s: float) -> dict:
    """Also returns evicted/new queryids (§8): queryids present in A but
    missing in B (or vice versa). High churn means pg_stat_statements.max
    is too small for this workload and coverage numbers are optimistic --
    the caller folds these into workload's completeness object.
    """
    rated, evicted, new = rate_list_by_key(
        sample_a.get("statements"),
        sample_b.get("statements"),
        key="queryid",
        interval_s=interval_s,
    )
    return {
        "statements": rated,
        "evicted_queryids": evicted,
        "new_queryids": new,
    }
