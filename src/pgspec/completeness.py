"""Uniform per-section completeness object builder (I4, §10).

Every artifact section carries one of these, always in the same shape,
whether the section captured cleanly or degraded (I5: fail soft, record the
failure, never a crash, never a silent omission).
"""

from __future__ import annotations


def build_completeness(
    available: bool,
    coverage: dict | None = None,
    staleness: dict | None = None,
    limits: dict | None = None,
    notes: list[str] | None = None,
    reason: str | None = None,
) -> dict:
    """Build a §10-shaped completeness object.

    `reason` is the I5 failure reason (missing extension, insufficient
    privilege, version-absent view); when given it's prepended to `notes` so
    it always surfaces first. The shape is identical whether `available` is
    True or False, so downstream consumers never need an existence check
    before reading `coverage`/`staleness`/`limits`.
    """
    all_notes = list(notes or [])
    if reason:
        all_notes = [reason, *all_notes]
    return {
        "available": available,
        "coverage": coverage or {},
        "staleness": staleness or {},
        "limits": limits or {},
        "notes": all_notes,
    }
