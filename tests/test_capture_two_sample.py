"""Integration tests for two-sample capture mode (§8, Milestone 9) against
the live canary container.
"""

from __future__ import annotations

import threading
import time

import psycopg

from pgspec.capture import capture_two_sample
from pgspec.validate import validate_schema

from canary_tokens import CANARY_IDENTIFIERS, CANARY_LITERALS, find_canary_tokens


def _write_during_interval(pg16_dsn: str, delay_s: float):
    def _run():
        time.sleep(delay_s)
        with psycopg.connect(pg16_dsn, autocommit=True) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE public.xq_customers SET signup_amount = signup_amount "
                    "WHERE id = 1"
                )

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    return thread


def test_capture_two_sample_produces_interval_and_rates(pg16_dsn, tmp_path):
    thread = _write_during_interval(pg16_dsn, delay_s=0.5)
    artifact = capture_two_sample(
        pg16_dsn,
        interval_s=2,
        top_k=20,
        salt_file=str(tmp_path / ".pgspec-salt"),
        map_path=str(tmp_path / "spectrum-map.json"),
    )
    thread.join(timeout=5)

    assert artifact["capture_mode"] == "two_sample"
    assert artifact["interval"]["seconds"] >= 1.5
    assert "activity" in artifact["rates"]
    assert "indexes" in artifact["rates"]
    assert "workload" in artifact["rates"]


def test_capture_two_sample_activity_rate_reflects_real_write(pg16_dsn, tmp_path):
    thread = _write_during_interval(pg16_dsn, delay_s=0.5)
    artifact = capture_two_sample(
        pg16_dsn,
        interval_s=2,
        top_k=20,
        salt_file=str(tmp_path / ".pgspec-salt"),
        map_path=str(tmp_path / "spectrum-map.json"),
    )
    thread.join(timeout=5)

    db_rates = artifact["rates"]["activity"]["database"]
    assert db_rates["xact_commit"] is not None
    assert db_rates["xact_commit"] >= 0
    assert db_rates["tup_updated"] is not None
    assert db_rates["tup_updated"] > 0


def test_capture_two_sample_workload_completeness_has_eviction_fields(pg16_dsn, tmp_path):
    artifact = capture_two_sample(
        pg16_dsn,
        interval_s=1,
        top_k=20,
        salt_file=str(tmp_path / ".pgspec-salt"),
        map_path=str(tmp_path / "spectrum-map.json"),
    )
    coverage = artifact["workload"]["completeness"]["coverage"]
    assert "evicted_queryids_count" in coverage
    assert "new_queryids_count" in coverage


def test_capture_two_sample_validates_against_schema(pg16_dsn, tmp_path):
    artifact = capture_two_sample(
        pg16_dsn,
        interval_s=1,
        top_k=10,
        salt_file=str(tmp_path / ".pgspec-salt"),
        map_path=str(tmp_path / "spectrum-map.json"),
    )
    errors = validate_schema(artifact)
    assert errors == []


def test_capture_two_sample_contains_no_canary_tokens(pg16_dsn, tmp_path):
    artifact = capture_two_sample(
        pg16_dsn,
        interval_s=1,
        top_k=20,
        salt_file=str(tmp_path / ".pgspec-salt"),
        map_path=str(tmp_path / "spectrum-map.json"),
    )
    literal_hits = find_canary_tokens(artifact, CANARY_LITERALS)
    assert literal_hits == [], f"canary literal(s) leaked: {literal_hits}"
    identifier_hits = find_canary_tokens(artifact, CANARY_IDENTIFIERS)
    assert identifier_hits == [], f"canary identifier(s) leaked: {identifier_hits}"
