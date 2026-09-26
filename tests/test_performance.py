"""Performance acceptance test (§12): f_star inflated to 5,000 tables
captures in under 120 seconds. Validates the single-pass, set-based
catalog query design (§11.5's "huge schemas" edge case) actually scales
with table count rather than doing a per-table round trip.
"""

from __future__ import annotations

import time

from pgspec.capture import capture_point, write_artifact
from pgspec.validate import validate_artifact


def test_capture_5000_tables_completes_under_120_seconds(scenario_dsn, tmp_path):
    dsn = scenario_dsn("f_perf_5k_tables")

    start = time.monotonic()
    artifact = capture_point(
        dsn,
        top_k=50,
        salt_file=str(tmp_path / ".salt"),
        map_path=str(tmp_path / "map.json"),
    )
    elapsed = time.monotonic() - start

    assert len(artifact["schema"]["tables"]) == 5000
    assert elapsed < 120, f"capture took {elapsed:.1f}s, exceeding the 120s budget"

    out_path = tmp_path / "spectrum.json.gz"
    write_artifact(artifact, str(out_path))
    assert validate_artifact(str(out_path)) == []
