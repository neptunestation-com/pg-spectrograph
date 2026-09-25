"""Capture orchestration: connection handling, capability probing, and (later)
the two-sample loop.

Every connection this module opens is read-only and bounded per I3:
`default_transaction_read_only = on`, a `statement_timeout`, and a
`lock_timeout`, set once at connect time.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import psycopg


@dataclass
class Capabilities:
    """The result of the one capability probe run at connect time (§11.4).
    Sections declare what they need against this rather than re-probing."""

    server_version_num: int
    is_in_recovery: bool
    extensions: dict[str, str] = field(default_factory=dict)

    @property
    def server_major(self) -> int:
        return self.server_version_num // 10000


def _logging_cursor_factory(query_log: list[str]) -> type[psycopg.Cursor]:
    class LoggingCursor(psycopg.Cursor):
        def execute(self, query, params=None, **kwargs):
            text = query if isinstance(query, str) else query.as_string(self)
            query_log.append(text)
            return super().execute(query, params, **kwargs)

    return LoggingCursor


def connect(
    dsn: str,
    *,
    statement_timeout_ms: int = 30_000,
    lock_timeout_ms: int = 1_000,
    query_log: list[str] | None = None,
) -> psycopg.Connection:
    """Open a read-only, bounded session (I3).

    Autocommit is used deliberately: pgspec issues one statement at a time
    and never needs a long-held transaction (I3's "at most one query at a
    time"), and it means one failing statement (an absent version-gated
    view, a denied privilege) can't abort a later one on the same
    connection -- each statement fails or succeeds independently.

    When `query_log` is given, every statement executed on this connection
    is appended to it verbatim, for the read-only wire-log test (§12).
    """
    kwargs = {}
    if query_log is not None:
        kwargs["cursor_factory"] = _logging_cursor_factory(query_log)
    conn = psycopg.connect(dsn, autocommit=True, **kwargs)
    with conn.cursor() as cur:
        cur.execute("SET default_transaction_read_only = on")
        cur.execute(f"SET statement_timeout = {int(statement_timeout_ms)}")
        cur.execute(f"SET lock_timeout = {int(lock_timeout_ms)}")
    return conn


def probe_capabilities(conn: psycopg.Connection) -> Capabilities:
    """The one capability probe run at connect time (§11.4): server version,
    replica status, and extension inventory. Sections declare required
    capabilities against this rather than re-probing themselves."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT current_setting('server_version_num')::int, pg_is_in_recovery()"
        )
        version_num, is_in_recovery = cur.fetchone()
        cur.execute("SELECT extname, extversion FROM pg_extension")
        extensions = dict(cur.fetchall())
    return Capabilities(
        server_version_num=version_num,
        is_in_recovery=is_in_recovery,
        extensions=extensions,
    )
