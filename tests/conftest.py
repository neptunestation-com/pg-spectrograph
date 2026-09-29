"""Shared pytest fixtures: the canary Postgres containers, PG14-17
(Milestone 3 introduced pg16 alone; Milestone 12 adds the full matrix)."""

from __future__ import annotations

import hashlib
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

    The database name carries a short hash of the fixture file's content,
    so editing a fixture gets a fresh database on the next run instead of
    silently reusing the stale one; superseded databases just linger in
    the container.
    """

    def _load(name: str) -> str:
        fixture_path = FIXTURES_DIR / f"{name}.sql"
        digest = hashlib.sha256(fixture_path.read_bytes()).hexdigest()[:8]
        db_name = f"{name}_{digest}"

        admin_dsn = pg16_dsn.rsplit("/", 1)[0] + "/postgres"
        with psycopg.connect(admin_dsn, autocommit=True) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (db_name,))
                exists = cur.fetchone() is not None
            if not exists:
                with conn.cursor() as cur:
                    cur.execute(f'CREATE DATABASE "{db_name}"')

        scenario_dsn_value = pg16_dsn.rsplit("/", 1)[0] + f"/{db_name}"
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
                        str(fixture_path),
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


PGFR_REPO_URL = "https://github.com/dventimisupabase/pg_flight_recorder.git"
# Head of feat/pgfr-v2 on 2026-09-28 (pgfr v2 is not yet merged to its own
# main). Keep in sync with the image tag in docker-compose.pgfr.yml.
PGFR_SHA = "fff22cd96d18d36a63a4ad3f39f8610b668ae7c9"
PGFR_SRC_DIR = DOCKER_DIR / ".pgfr-src"
PGFR_COMPOSE = DOCKER_DIR / "docker-compose.pgfr.yml"
PGFR_DSN = "postgresql://postgres:pgspec@localhost:55433/postgres"


def _ensure_pgfr_checkout() -> None:
    """Shallow-fetch pgfr at the pinned SHA into a gitignored cache dir. The
    checkout serves both the compose build context (pgfr's own Dockerfile,
    postgres:16 plus pg_cron) and the psql-driven install (install.sql uses
    \\ir includes, so it needs real files on disk). Reused as-is when its
    HEAD already matches the pin."""
    git = ["git", "-C", str(PGFR_SRC_DIR)]
    try:
        if not (PGFR_SRC_DIR / ".git").exists():
            PGFR_SRC_DIR.mkdir(parents=True, exist_ok=True)
            subprocess.run([*git, "init", "-q"], check=True, capture_output=True, timeout=30)
            subprocess.run(
                [*git, "remote", "add", "origin", PGFR_REPO_URL],
                check=True,
                capture_output=True,
                timeout=30,
            )
        head = subprocess.run(
            [*git, "rev-parse", "HEAD"], capture_output=True, text=True, timeout=30
        )
        if head.returncode == 0 and head.stdout.strip() == PGFR_SHA:
            return
        subprocess.run(
            [*git, "fetch", "-q", "--depth", "1", "origin", PGFR_SHA],
            check=True,
            capture_output=True,
            timeout=300,
        )
        subprocess.run(
            [*git, "checkout", "-q", "--detach", "FETCH_HEAD"],
            check=True,
            capture_output=True,
            timeout=60,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        pytest.skip(f"pgfr checkout at {PGFR_SHA[:8]} unavailable: {exc}")


def _bring_up_pgfr() -> None:
    docker = _docker_bin()
    try:
        subprocess.run(
            [docker, "compose", "-f", str(PGFR_COMPOSE), "up", "-d", "--wait", "pg16-pgfr"],
            check=True,
            capture_output=True,
            text=True,
            # First run builds pgfr's image, compiling pg_cron and pgTAP.
            timeout=900,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        pytest.skip(f"pgfr container unavailable: {exc}")


def _psql(dsn: str, *args: str) -> None:
    subprocess.run(
        ["psql", "-X", "-v", "ON_ERROR_STOP=1", dsn, *args],
        check=True,
        capture_output=True,
        text=True,
        timeout=600,
    )


@pytest.fixture(scope="session")
def pgfr_dsn() -> str:
    """A PG16 container built from pgfr's own Dockerfile (pg_cron preloaded),
    with pgfr_record and pgfr_analyze installed at the pinned SHA and the
    synthesized 7-day history from tests/fixtures/f_pgfr.sql loaded once
    (§14: "a synthesized 7-day history with a scripted weekday batch
    spike"). Left running like the other containers; rebuild with
    `docker compose -f tests/docker/docker-compose.pgfr.yml down -v`.
    """
    _ensure_pgfr_checkout()
    _bring_up_pgfr()

    deadline = time.monotonic() + 60
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with psycopg.connect(PGFR_DSN, connect_timeout=2, autocommit=True) as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT 1")
            break
        except Exception as exc:  # noqa: BLE001 - broad while polling readiness
            last_error = exc
            time.sleep(1)
    else:
        pytest.skip(f"pgfr container never became ready: {last_error}")

    with psycopg.connect(PGFR_DSN, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("CREATE EXTENSION IF NOT EXISTS pg_cron")
        cur.execute("CREATE EXTENSION IF NOT EXISTS pg_stat_statements")
        cur.execute("SELECT to_regclass('pgfr_record.manifest') IS NOT NULL")
        installed = cur.fetchone()[0]
    if not installed:
        try:
            _psql(
                PGFR_DSN,
                "--single-transaction",
                "-f",
                str(PGFR_SRC_DIR / "pgfr_record" / "install.sql"),
            )
            _psql(
                PGFR_DSN,
                "--single-transaction",
                "-f",
                str(PGFR_SRC_DIR / "pgfr_analyze" / "install.sql"),
            )
        except subprocess.CalledProcessError as exc:
            pytest.fail(f"installing pgfr failed: {exc.stderr}")

    # The fixture records its own content digest as the database comment
    # (a table would be captured by pgfr as a user relation), so an edited
    # fixture reloads and an unchanged one is skipped.
    fixture_path = FIXTURES_DIR / "f_pgfr.sql"
    digest = "pgspec-fixture:" + hashlib.sha256(fixture_path.read_bytes()).hexdigest()[:16]
    with psycopg.connect(PGFR_DSN, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT shobj_description(oid, 'pg_database') FROM pg_database "
            "WHERE datname = current_database()"
        )
        loaded = cur.fetchone()[0]
    if loaded != digest:
        try:
            _psql(PGFR_DSN, "-v", f"digest={digest}", "-f", str(fixture_path))
        except subprocess.CalledProcessError as exc:
            pytest.fail(f"loading f_pgfr history failed: {exc.stderr}")
    return PGFR_DSN
