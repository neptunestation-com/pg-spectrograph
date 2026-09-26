# pg_spectrograph (pgspec)

A read-only, privacy-preserving performance signature extractor for PostgreSQL.

pgspec connects to a PostgreSQL database, reads only its catalogs and statistics
views, and produces a single portable artifact (`spectrum.json.gz`) describing
the *shape* of the database: schema topology, per-column statistical
distributions, index structure, workload mixture, and derived performance
metrics. It never reads a row of your data, and every schema, table, column,
index, and function name in the artifact is replaced with a deterministic
pseudonym before the artifact ever leaves your database's session.

The idea: two databases with the same schema topology, similar per-column
distributions, similar index structure, and a similar workload mixture are
performance-indistinguishable to the query planner and the buffer cache, even
if every row's actual content differs completely. The signature captures the
statistics that predict performance; it never captures the values they were
computed from.

Full design rationale lives in [`frozen/claude-code-context-pack-pg-spectrograph.md`](frozen/claude-code-context-pack-pg-spectrograph.md),
a point-in-time design spec that is not edited as decisions evolve; see the
project's GitHub issues for what has since been resolved or refined.

## What is captured

One artifact, seven sections plus a derived section, each carrying its own
completeness metadata (so a consumer can tell "this database is small" from
"this fingerprint is dim"):

| Section | What it captures |
|---|---|
| `instance` | Server version, a curated GUC allowlist, extension inventory, stats-reset ages |
| `schema` | Tables, columns, partitioning, the foreign-key graph |
| `column_stats` | Per-column null fraction, width, distinctness, correlation, MCV frequencies, histogram shape, extended (cross-column) statistics |
| `indexes` | Access method, key columns, uniqueness, size, scan counts, partial/expression-index structure |
| `workload` | Top statements from `pg_stat_statements` (by execution time, call count, WAL bytes, and physical block reads), pseudonymized query text, verb/join/aggregate shape |
| `activity` | Database, background-writer, WAL, and I/O counters; per-table and per-function activity |
| `derived` | Read/write ratio, HOT-update fraction, cache hit ratios, statement concentration, FK-graph summary, table-size distribution, dead-tuple pressure, index redundancy, and more, computed from the sections above |

Two capture modes sharpen the above:

- **Two-sample** (`--mode two-sample --interval 900`) takes a narrow counter
  snapshot, sleeps, samples again, and reports reset-aware per-second rates
  instead of lifetime cumulative averages, plus statement-eviction churn
  between the two samples.
- **pgfr** (`--pgfr auto`, the default) detects an installed and actively
  capturing [pg_flight_recorder v2](https://github.com/dventimisupabase/pg_flight_recorder)
  and, if present, adds a `temporal` section (a WAL-byte-rate time series and
  batch/spike detection) and skips the two-sample sleep entirely, since pgfr's
  own history already supplies rates. `--pgfr off` never probes for it;
  `--pgfr require` fails the capture if it isn't available.

## What is provably not captured

- **No row values, ever.** Every field in the artifact is a count, a
  frequency, a ratio, a width, a normalized position, or an enum. Postgres's
  own `most_common_vals` column (the actual most-common data values) is never
  selected by any extraction query in this codebase, statically enforced by
  a test that parses every section module's SQL constants.
- **No real identifiers.** Every schema, table, column, index, and function
  name is replaced with a deterministic pseudonym (`s_00`, `t_0007`,
  `c_0142`, `i_0033`, `fn_0001`) before the artifact is assembled. Query text
  captured from `pg_stat_statements` is parsed and rewritten the same way;
  utility statements (anything that isn't `SELECT`/`INSERT`/`UPDATE`/`DELETE`)
  have their text dropped entirely rather than risk an unpseudonymized DDL
  fragment.
- **The pseudonym map never leaves your machine.** `pgspec capture` writes it
  to a separate local file (`spectrum-map.json`, mode `0600`); the artifact
  itself carries only a `sha256` digest of that file, never the mapping.

### How to verify this yourself

- `pgspec inspect spectrum.json.gz` renders a human-readable summary using
  only the artifact, no map file needed, because nothing in it requires
  de-pseudonymizing to be meaningful.
- `pgspec validate spectrum.json.gz` runs a regex scan over every string
  value in the artifact for anything that looks like an email address, a
  UUID, or an IP address, on top of validating the artifact's shape against
  `schema_v1.json`.
- `pgspec deref spectrum.json.gz --map spectrum-map.json` restores real
  names from the map, entirely locally; it first checks the map file's
  digest against the artifact's own `pseudonym_map_digest` and refuses to
  proceed on a mismatch, so you can't accidentally de-pseudonymize one
  capture with another capture's map.
- Read the source: every extraction query lives in `src/pgspec/sections/*.py`
  as a small, readable SQL string constant.

## Installation

**Single file, nothing else required:**

```bash
./scripts/build_pyz.sh
python3 pgspec.pyz capture "postgresql://user:pass@host/db"
```

**As a package, for development:**

```bash
uv sync
uv run pgspec capture "postgresql://user:pass@host/db"
```

Requires Python 3.11+. Reads only catalogs and statistics views; the
connecting role needs `CONNECT` and ideally `pg_read_all_stats` (partial
visibility degrades gracefully and is recorded in the artifact's
completeness metadata, never a hard failure).

## CLI reference

```
pgspec capture DSN [--mode point|two-sample] [--interval 900]
               [--top-k 500] [--out spectrum.json.gz] [--map spectrum-map.json]
               [--salt-file .pgspec-salt] [--pgfr auto|off|require]
pgspec validate spectrum.json.gz
pgspec inspect  spectrum.json.gz
pgspec deref    spectrum.json.gz --map spectrum-map.json
```

## Development

```bash
uv sync
uv run pytest
```

Integration tests need Docker (a PG14-17 matrix, seeded with a canary
fixture whose distinctive literal values and identifier names are checked to
never appear anywhere in a captured artifact) and are skipped cleanly if
Docker is unavailable. See `tests/docker/` and `tests/fixtures/`.
