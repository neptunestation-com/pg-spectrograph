"""Shared pytest fixtures: the canary Postgres containers, PG14-17
(Milestone 3 introduced pg16 alone; Milestone 12 adds the full matrix)."""

from __future__ import annotations

import shutil
import subprocess
import time
from pathlib import Path

import psycopg
import pytest

DOCKER_DIR = Path(__file__).parent / "docker"

DSNS = {
    14: "postgresql://postgres:pgspec@localhost:55414/pgspec_test",
    15: "postgresql://postgres:pgspec@localhost:55415/pgspec_test",
    16: "postgresql://postgres:pgspec@localhost:55432/pgspec_test",
    17: "postgresql://postgres:pgspec@localhost:55417/pgspec_test",
}
PG16_DSN = DSNS[16]


def _docker_bin() -> str:
    found = shutil.which("docker")
    if found:
        return found
    for candidate in ("/usr/local/bin/docker", "/opt/homebrew/bin/docker"):
        if Path(candidate).exists():
            return candidate
    return "docker"


def _bring_up(*services: str) -> None:
    docker = _docker_bin()
    try:
        subprocess.run(
            [
                docker,
                "compose",
                "-f",
                str(DOCKER_DIR / "docker-compose.yml"),
                "up",
                "-d",
                "--wait",
                *services,
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=90,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        pytest.skip(f"docker unavailable or compose failed: {exc}")


def _wait_ready(dsn: str, label: str) -> str:
    deadline = time.monotonic() + 30
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with psycopg.connect(dsn, connect_timeout=2, autocommit=True) as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT 1 FROM public.xq_secret_salaries LIMIT 1")
            return dsn
        except Exception as exc:  # noqa: BLE001 - broad while polling readiness
            last_error = exc
            time.sleep(1)
    pytest.skip(f"{label} canary fixture never became ready: {last_error}")


@pytest.fixture(scope="session")
def pg16_dsn() -> str:
    """Bring up (if needed) the canary-seeded PG16 container alone and
    return its DSN. Left running after the test session (not torn down),
    so repeated local test runs don't pay container-startup cost every
    time. Scoped to just this one service so the ~100 pre-Milestone-12
    tests that only need PG16 don't pay for starting the whole matrix.
    """
    _bring_up("pg16")
    return _wait_ready(PG16_DSN, "pg16")


FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="session")
def scenario_dsn(pg16_dsn):
    """Factory fixture (§12's scenario databases, Milestone 12): call
    scenario_dsn("f_skew") to get a DSN for that scenario, creating the
    database and loading tests/fixtures/f_skew.sql on first use only. All
    scenarios live on the pg16 container (the PG14-17 matrix is a separate
    concern, already covered by test_version_matrix.py against the canary
    fixture) and are left in place after the session, same as pg16_dsn
    itself.
    """

    def _load(name: str) -> str:
        admin_dsn = pg16_dsn.rsplit("/", 1)[0] + "/postgres"
        with psycopg.connect(admin_dsn, autocommit=True) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,))
                exists = cur.fetchone() is not None
            if not exists:
                with conn.cursor() as cur:
                    cur.execute(f'CREATE DATABASE "{name}"')

        scenario_dsn_value = pg16_dsn.rsplit("/", 1)[0] + f"/{name}"
        if not exists:
            # Loaded via psql, not psycopg's cur.execute(whole_file_text):
            # psql dispatches each statement as its own top-level message,
            # while a multi-statement string sent through the simple query
            # protocol gets wrapped in one implicit transaction -- which
            # would break f_perf_5k_tables' CALL ... with an internal
            # COMMIT the same way a multi-statement dispatch breaks DETACH
            # PARTITION CONCURRENTLY. Matches how the canary fixture itself
            # is already loaded, via docker-entrypoint-initdb.d.
            try:
                subprocess.run(
                    [
                        "psql",
                        # -X: skip ~/.psqlrc. A personal interactive-safety
                        # default there (default_transaction_read_only=on)
                        # is exactly right for a human at a prompt and
                        # exactly wrong for this scripted fixture loader,
                        # which needs real write access -- confirmed live
                        # ("cannot execute CREATE EXTENSION in a read-only
                        # transaction") rather than assumed.
                        "-X",
                        scenario_dsn_value,
                        "-v",
                        "ON_ERROR_STOP=1",
                        "-f",
                        str(FIXTURES_DIR / f"{name}.sql"),
                    ],
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=300,
                )
            except subprocess.CalledProcessError as exc:
                pytest.fail(f"loading fixture {name!r} failed: {exc.stderr}")
        return scenario_dsn_value

    return _load

@pytest.fixture(scope="session")
def pg_matrix_dsns() -> dict[int, str]:
    """Bring up all four PG14-17 canary-seeded containers (Milestone 12's
    version matrix) and return {major_version: dsn}."""
    _bring_up("pg14", "pg15", "pg16", "pg17")
    return {major: _wait_ready(dsn, f"pg{major}") for major, dsn in DSNS.items()}
