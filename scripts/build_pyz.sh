#!/usr/bin/env bash
# Build a single-file pgspec.pyz, runnable with nothing but Python 3.11+
# (§14's definition of done): `python3 pgspec.pyz capture ...`
#
# Uses shiv, not stdlib zipapp: both pglast and psycopg[binary] ship
# compiled .so extensions, which CPython's zipimport cannot load from
# inside a zip -- confirmed against the actual built wheels, not assumed.
# shiv's bootstrap extracts the bundled dependencies to an on-disk cache
# directory on first run before importing, which sidesteps the limitation
# entirely. shiv itself is a build-time tool, not a pgspec runtime
# dependency, so it's run via `uvx` rather than added to pyproject.toml.
set -euo pipefail
cd "$(dirname "$0")/.."

uvx shiv \
    --output-file pgspec.pyz \
    --console-script pgspec \
    --python "/usr/bin/env python3" \
    .

echo "Built pgspec.pyz ($(du -h pgspec.pyz | cut -f1))"
echo "Run it with: python3 pgspec.pyz capture <DSN> [...]"
