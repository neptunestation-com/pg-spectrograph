"""Integration tests for pgspec.capture against the Milestone 3 canary container."""

from __future__ import annotations

from pgspec.capture import Capabilities, connect, probe_capabilities


def test_connect_sets_read_only_and_timeouts(pg16_dsn):
    conn = connect(pg16_dsn, statement_timeout_ms=12_345, lock_timeout_ms=678)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT current_setting('default_transaction_read_only'), "
                "current_setting('statement_timeout'), "
                "current_setting('lock_timeout')"
            )
            read_only, statement_timeout, lock_timeout = cur.fetchone()
        assert read_only == "on"
        assert statement_timeout == "12345ms" or statement_timeout == "12345"
        assert lock_timeout == "678ms" or lock_timeout == "678"
    finally:
        conn.close()


def test_probe_capabilities_returns_expected_shape(pg16_dsn):
    conn = connect(pg16_dsn)
    try:
        capabilities = probe_capabilities(conn)
    finally:
        conn.close()

    assert isinstance(capabilities, Capabilities)
    assert capabilities.server_major == 16
    assert capabilities.is_in_recovery is False
    assert "pg_stat_statements" in capabilities.extensions
    assert "plpgsql" in capabilities.extensions


def test_connect_query_log_captures_statements(pg16_dsn):
    query_log: list[str] = []
    conn = connect(pg16_dsn, query_log=query_log)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
    finally:
        conn.close()

    assert any("SELECT 1" in q for q in query_log)
    assert any("SET default_transaction_read_only" in q for q in query_log)
