"""Performance acceptance test (§12): f_star inflated to 5,000 tables
captures in under 120 seconds. Validates the single-pass, set-based
catalog query design (§11.5's "huge schemas" edge case) actually scales
with table count rather than doing a per-table round trip.

Also validates the §13.4 artifact size budget (5 MB gzipped at 10k tables,
so 2.5 MB at 5k). That assertion is only meaningful if the fixture's
histograms are full-resolution, which the n_bounds check pins: a 10-row
table yields 10-bound histograms and an artifact roughly 10x smaller than
a real database's.
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

    n_bounds = {
        col["histogram"]["n_bounds"]
        for col in artifact["column_stats"]["columns"]
        if col["histogram"]
    }
    assert 101 in n_bounds, f"fixture histograms are not full-resolution: {n_bounds}"

    gz_bytes = out_path.stat().st_size
    budget = 2.5 * 1024 * 1024
    assert gz_bytes < budget, (
        f"artifact is {gz_bytes / 1024 / 1024:.2f} MB gzipped for 5,000 tables, "
        f"over the {budget / 1024 / 1024:.1f} MB soft budget (§13.4)"
    )
