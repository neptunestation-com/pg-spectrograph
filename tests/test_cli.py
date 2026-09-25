"""Parser-shape tests for the pgspec CLI (Milestone 1)."""

from __future__ import annotations

import subprocess
import sys

from pgspec.__main__ import build_parser


def test_capture_defaults():
    args = build_parser().parse_args(["capture", "postgresql://localhost/db"])
    assert args.command == "capture"
    assert args.dsn == "postgresql://localhost/db"
    assert args.mode == "point"
    assert args.interval == 900
    assert args.top_k == 500
    assert args.out == "spectrum.json.gz"
    assert args.map == "spectrum-map.json"
    assert args.salt_file == ".pgspec-salt"
    assert args.pgfr == "auto"


def test_capture_overrides():
    args = build_parser().parse_args(
        [
            "capture",
            "postgresql://localhost/db",
            "--mode",
            "two-sample",
            "--interval",
            "3600",
            "--top-k",
            "1000",
            "--out",
            "out.json.gz",
            "--map",
            "map.json",
            "--salt-file",
            "salt.bin",
            "--pgfr",
            "require",
        ]
    )
    assert args.mode == "two-sample"
    assert args.interval == 3600
    assert args.top_k == 1000
    assert args.out == "out.json.gz"
    assert args.map == "map.json"
    assert args.salt_file == "salt.bin"
    assert args.pgfr == "require"


def test_validate_requires_artifact_positional():
    args = build_parser().parse_args(["validate", "spectrum.json.gz"])
    assert args.command == "validate"
    assert args.artifact == "spectrum.json.gz"


def test_inspect_requires_artifact_positional():
    args = build_parser().parse_args(["inspect", "spectrum.json.gz"])
    assert args.command == "inspect"
    assert args.artifact == "spectrum.json.gz"


def test_deref_requires_map_flag():
    args = build_parser().parse_args(
        ["deref", "spectrum.json.gz", "--map", "spectrum-map.json"]
    )
    assert args.command == "deref"
    assert args.artifact == "spectrum.json.gz"
    assert args.map == "spectrum-map.json"


def test_bare_invocation_exits_nonzero():
    result = subprocess.run(
        [sys.executable, "-m", "pgspec"],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0


def test_help_smoke_test():
    result = subprocess.run(
        [sys.executable, "-m", "pgspec", "--help"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert "capture" in result.stdout
    assert "validate" in result.stdout
    assert "inspect" in result.stdout
    assert "deref" in result.stdout


def test_capture_not_yet_implemented():
    result = subprocess.run(
        [sys.executable, "-m", "pgspec", "capture", "postgresql://localhost/db"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    assert "Milestone 3" in result.stderr
