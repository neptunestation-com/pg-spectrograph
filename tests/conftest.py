"""Shared pytest fixtures: the Milestone 3 canary Postgres container."""

from __future__ import annotations

import shutil
import subprocess
import time
from pathlib import Path

import psycopg
import pytest

DOCKER_DIR = Path(__file__).parent / "docker"
PG16_DSN = "postgresql://postgres:pgspec@localhost:55432/pgspec_test"


def _docker_bin() -> str:
    found = shutil.which("docker")
    if found:
        return found
    for candidate in ("/usr/local/bin/docker", "/opt/homebrew/bin/docker"):
        if Path(candidate).exists():
            return candidate
    return "docker"


@pytest.fixture(scope="session")
def pg16_dsn() -> str:
    """Bring up (if needed) the canary-seeded PG16 container and return its DSN.

    Left running after the test session (not torn down), so repeated local
    test runs don't pay container-startup cost every time.
    """
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
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        pytest.skip(f"docker unavailable or compose failed: {exc}")

    deadline = time.monotonic() + 30
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with psycopg.connect(PG16_DSN, connect_timeout=2, autocommit=True) as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT 1 FROM public.xq_secret_salaries LIMIT 1")
            return PG16_DSN
        except Exception as exc:  # noqa: BLE001 - broad while polling readiness
            last_error = exc
            time.sleep(1)
    pytest.skip(f"pg16 canary fixture never became ready: {last_error}")
