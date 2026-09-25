"""pgspec CLI entry point."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pgspec",
        description=(
            "Read-only, privacy-preserving performance signature extractor "
            "for PostgreSQL."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    capture = subparsers.add_parser(
        "capture", help="Capture a performance signature from a live database."
    )
    capture.add_argument("dsn", help="libpq connection string or URI.")
    capture.add_argument(
        "--mode",
        choices=("point", "two-sample"),
        default="point",
        help="Capture mode (default: point).",
    )
    capture.add_argument(
        "--interval",
        type=int,
        default=900,
        help="Seconds between samples in two-sample mode (default: 900).",
    )
    capture.add_argument(
        "--top-k",
        type=int,
        default=500,
        help="Statements captured per ranking lens in the workload section (default: 500).",
    )
    capture.add_argument(
        "--out",
        default="spectrum.json.gz",
        help="Output artifact path (default: spectrum.json.gz).",
    )
    capture.add_argument(
        "--map",
        default="spectrum-map.json",
        help="Pseudonym map output path (default: spectrum-map.json).",
    )
    capture.add_argument(
        "--salt-file",
        default=".pgspec-salt",
        help="Path to the pseudonymization salt file (default: .pgspec-salt).",
    )
    capture.add_argument(
        "--pgfr",
        choices=("auto", "off", "require"),
        default="auto",
        help="pg_flight_recorder augmentation mode (default: auto).",
    )
    capture.set_defaults(func=cmd_capture)

    validate = subparsers.add_parser(
        "validate", help="Validate an artifact against schema_v1.json and invariants."
    )
    validate.add_argument("artifact", help="Path to a spectrum.json.gz artifact.")
    validate.set_defaults(func=cmd_validate)

    inspect = subparsers.add_parser(
        "inspect", help="Render a human-readable summary of an artifact."
    )
    inspect.add_argument("artifact", help="Path to a spectrum.json.gz artifact.")
    inspect.set_defaults(func=cmd_inspect)

    deref = subparsers.add_parser(
        "deref", help="Locally de-pseudonymize an artifact using its map file."
    )
    deref.add_argument("artifact", help="Path to a spectrum.json.gz artifact.")
    deref.add_argument(
        "--map",
        required=True,
        help="Path to the spectrum-map.json file produced alongside the artifact.",
    )
    deref.set_defaults(func=cmd_deref)

    return parser


def cmd_capture(args: argparse.Namespace) -> int:
    raise NotImplementedError("pgspec capture: implemented starting Milestone 3")


def cmd_validate(args: argparse.Namespace) -> int:
    raise NotImplementedError("pgspec validate: implemented at Milestone 8")


def cmd_inspect(args: argparse.Namespace) -> int:
    raise NotImplementedError("pgspec inspect: implemented at Milestone 10")


def cmd_deref(args: argparse.Namespace) -> int:
    raise NotImplementedError("pgspec deref: implemented at Milestone 10")


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except NotImplementedError as exc:
        print(f"pgspec: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
