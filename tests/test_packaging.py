"""Packaging test (§14 definition of done): the single-file pgspec.pyz
build runs with nothing but Python 3.11+, verified against the live
container, not just asserted to work."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent


@pytest.fixture(scope="module")
def pyz_path(tmp_path_factory) -> Path:
    if shutil.which("uvx") is None:
        pytest.skip("uvx not available; cannot build the .pyz")

    out_dir = tmp_path_factory.mktemp("pyz")
    out_path = out_dir / "pgspec.pyz"
    result = subprocess.run(
        [
            "uvx",
            "shiv",
            "--output-file",
            str(out_path),
            "--console-script",
            "pgspec",
            "--python",
            "/usr/bin/env python3",
            str(REPO_ROOT),
        ],
        capture_output=True,
        text=True,
        timeout=180,
    )
    if result.returncode != 0:
        pytest.fail(f"shiv build failed: {result.stderr}")
    return out_path


def test_pyz_help_runs_with_bare_python(pyz_path):
    result = subprocess.run(
        [sys.executable, str(pyz_path), "--help"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0
    assert "capture" in result.stdout


def test_pyz_capture_and_validate_against_live_container(pyz_path, pg16_dsn, tmp_path):
    out_path = tmp_path / "spectrum.json.gz"
    map_path = tmp_path / "spectrum-map.json"
    salt_path = tmp_path / ".pgspec-salt"

    capture_result = subprocess.run(
        [
            sys.executable,
            str(pyz_path),
            "capture",
            pg16_dsn,
            "--top-k",
            "10",
            "--out",
            str(out_path),
            "--map",
            str(map_path),
            "--salt-file",
            str(salt_path),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert capture_result.returncode == 0, capture_result.stderr
    assert out_path.exists()

    validate_result = subprocess.run(
        [sys.executable, str(pyz_path), "validate", str(out_path)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert validate_result.returncode == 0, validate_result.stderr
    assert "OK" in validate_result.stderr
