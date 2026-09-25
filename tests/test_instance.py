"""Integration tests for the instance section (§6.1) against the canary container."""

from __future__ import annotations

from pgspec.capture import connect, probe_capabilities
from pgspec.sections.instance import (
    capture_guc_snapshot,
    capture_instance,
    capture_stats_reset,
)


def test_capture_guc_snapshot_includes_known_keys(pg16_dsn):
    conn = connect(pg16_dsn)
    try:
        snapshot = capture_guc_snapshot(conn)
    finally:
        conn.close()

    assert "shared_buffers" in snapshot
    assert "wal_level" in snapshot
    assert any(name.startswith("autovacuum_") for name in snapshot)
    assert all({"setting", "unit", "source"} <= set(v) for v in snapshot.values())


def test_capture_stats_reset_handles_missing_views_gracefully(pg16_dsn):
    conn = connect(pg16_dsn)
    try:
        stats_reset = capture_stats_reset(conn)
    finally:
        conn.close()

    # pg_stat_checkpointer doesn't exist before PG17: fails soft to None,
    # not an exception (I5).
    assert stats_reset["pg_stat_checkpointer"] is None
    # pg_stat_bgwriter/wal/database exist on every supported major.
    assert stats_reset["pg_stat_bgwriter"] is not None
    assert stats_reset["pg_stat_wal"] is not None


def test_capture_instance_full_shape(pg16_dsn):
    conn = connect(pg16_dsn)
    try:
        capabilities = probe_capabilities(conn)
        result = capture_instance(conn, capabilities)
    finally:
        conn.close()

    assert result["server_version_num"] == capabilities.server_version_num
    assert result["extensions"] == capabilities.extensions
    assert "shared_buffers" in result["guc_snapshot"]
    assert result["stats_reset"]["pg_stat_bgwriter"] is not None
    assert result["completeness"]["available"] is True
    assert result["completeness"]["coverage"]["guc_settings_captured"] == len(
        result["guc_snapshot"]
    )
