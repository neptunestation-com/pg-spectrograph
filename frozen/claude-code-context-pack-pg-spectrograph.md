# Claude Code Context Pack: pg_spectrograph (pgspec)

## A read-only, privacy-preserving performance signature extractor for PostgreSQL

**Provisional name.** `pg_spectrograph`, CLI `pgspec`. Rationale: a spectrograph captures
the complete physical signature of a star — composition, temperature, velocity — without
ever touching it, and spectral classification (OBAFGKM) is exactly the persona-clustering
move this project builds toward. Veto freely; nothing below depends on the name.

**Status of this document.** Design is settled at the level described here; this pack is
the input to an implementation session in Claude Code. Section 13 lists the questions
deliberately left open for that session. Everything else should be treated as decided
unless implementation reveals a contradiction.

---

## §1 — Thesis and context

SLOs at Supabase are currently stated over the wrong object: "four nines" is a predicate
on the service, but the customer experiences a predicate on *their workload over their
data on given resources*. The long-term program is: capture a performance-sufficient
signature of a database, synthesize statistically equivalent databases and workloads,
benchmark across compute/config grids, cluster signatures into personas, and state SLIs
per-persona with measured performance surfaces behind them.

**This tool is step one only:** the signature extractor. It must be so cheap, so safe,
and so obviously read-only that running it against a production customer database (or
handing it to a prospect to run themselves) is a non-decision.

The epistemic core: the signature is intended as a **sufficient statistic for
performance**. Actual row values are nuisance parameters. Two databases sharing schema
topology, volumes, per-column distributions, index structure, physical correlation, and
workload mixture are performance-indistinguishable to the planner and the buffer cache.
The extractor captures the statistics; it never captures the values they were drawn from.

Known insufficiency (recorded honestly, not papered over): marginal statistics miss
cross-column correlation, join-key co-occurrence, and parameter selectivity
distributions. §7 and §11 say what we do about each.

---

## §2 — Goals and non-goals

### Goals (v1)

1. Produce a single, versioned, portable artifact (`spectrum-v1.json.gz`) from any
   PostgreSQL 14+ database, using only SELECTs against catalogs and statistics views.
2. Zero customer data values in the artifact. Zero real identifiers in the artifact.
3. Runnable by a customer against their own database with nothing installed server-side.
4. Two-sample capture mode for rate estimation (lifetime counters are aliased).
5. Optional pgfr v2 temporal augmentation when pg_flight_recorder is present.
6. Per-section completeness metadata — the selection function is built into the
   instrument from day one.
7. Deterministic output: same database state → byte-identical artifact (modulo
   timestamps), so captures diff cleanly.

### Non-goals (v1)

- **No synthesis.** This tool writes fingerprints; a future tool reads them.
- **No clustering / persona assignment.** Downstream.
- **No parameter distribution capture.** pg_stat_statements normalizes literals away;
  the artifact records `"parameter_distributions": "unavailable_in_v1"` explicitly.
  (Candidate v2 sources: `pg_stat_statements` with a sampling extension, log-based
  capture, eBPF. Out of scope now.)
- **No server-side objects.** No extension, no schema, no functions, no temp tables
  requiring write access. The operator constraint is hard read-only.
- **No EXPLAIN execution against customer queries.** Even EXPLAIN (without ANALYZE)
  plans leak literal values in some cases and require the query text with parameters;
  out of scope.

---

## §3 — Design invariants

**I1 — No values, only shapes.** Every field in the artifact is a count, frequency,
ratio, width, normalized position, or enum. Where PostgreSQL hands us actual values
(MCV lists, histogram bounds, partial-index predicates), the transform to a value-free
shape happens *in the extraction query or immediately in the driver*, and the raw form
is never serialized, never logged, never written to disk.

**I2 — Pseudonymized identifiers, customer-held map.** All schema/table/column/index
identifiers are replaced by deterministic pseudonyms (`s_01`, `t_0007`, `c_0142`,
`i_0033`). The pseudonym map is written to a separate local file (`spectrum-map.json`)
that stays with the customer; the artifact carries only `sha256` of the map. Mapping is
deterministic within a capture (HMAC-SHA256 over the fully qualified identifier with a
per-capture random salt, then ordinal assignment by sorted digest) so that re-running
against an unchanged database with the same salt reproduces the same pseudonyms.

**I3 — Read-only, low-impact, bounded.** Session sets
`default_transaction_read_only = on`, `statement_timeout` (default 30s per query),
`lock_timeout` (default 1s), and runs at most one query at a time. No query may take
locks stronger than AccessShare. Catalog queries must be written to avoid seq-scanning
user data (they touch catalogs and stats views only). Target: full capture of a
10k-table database in under two minutes, excluding the two-sample sleep.

**I4 — Completeness is first-class.** Every section carries a `completeness` object.
Downstream consumers must be able to distinguish "this fingerprint is dim" from "this
database is small" — Malmquist-bias hygiene, encoded in the schema.

**I5 — Fail soft, record the failure.** Missing extension, insufficient privilege,
version-absent view → the section is emitted with `"available": false` and a reason
string, never a crash, never a silent omission.

---

## §4 — Architecture and packaging

**Decision: client-side Python driver.** Not a psql script (transforms like histogram
normalization and query-text pseudonymization are painful in pure SQL without server-side
functions, which I3 forbids), not an extension (I3, and prospects can't install
extensions before they're customers).

- Language: Python 3.11+, dependencies: `psycopg[binary]` (v3), `pglast` (libpg_query
  bindings, for parse-tree-based identifier rewriting of normalized query texts),
  stdlib otherwise (`hashlib`, `hmac`, `json`, `gzip`, `statistics`).
- Distribution: single installable package `pgspec`, and additionally a
  `zipapp`/single-file build so a customer can run `python pgspec.pyz "$DATABASE_URL"`
  with nothing but Python present.
- Repo layout:

```
pg_spectrograph/
  pyproject.toml
  src/pgspec/
    __main__.py          # CLI
    capture.py           # orchestration, two-sample loop
    sections/
      instance.py
      schema.py
      column_stats.py
      indexes.py
      workload.py
      activity.py
      derived.py
      temporal_pgfr.py   # optional augmentation
    pseudonym.py         # HMAC map, query-text rewriting via pglast
    transforms.py        # histogram normalization, MCV frequency-only, etc.
    schema_v1.json       # JSON Schema for the artifact, shipped in-package
    completeness.py
  tests/
    docker/              # compose files for PG 14/15/16/17 fixtures
    fixtures/            # seeded scenario databases (see §12)
    test_*.py
```

- CLI surface (v1):

```
pgspec capture DSN [--mode point|two-sample] [--interval 900]
               [--top-k 500] [--out spectrum.json.gz] [--map spectrum-map.json]
               [--salt-file .pgspec-salt] [--pgfr auto|off|require]
pgspec validate spectrum.json.gz          # against schema_v1.json + invariant checks
pgspec inspect  spectrum.json.gz          # human-readable summary, no map needed
pgspec deref    spectrum.json.gz --map spectrum-map.json   # local de-pseudonymization
```

- Connection: standard libpq DSN/URI; plays with `pg_service.conf` / `.pgpass` as
  established in prior tooling.

---

## §5 — Artifact schema (top level)

```json
{
  "signature_version": "1.0",
  "captured_at": "2026-09-25T18:04:11Z",
  "capture_mode": "point | two_sample | pgfr",
  "capture_duration_s": 41.2,
  "extractor": {"name": "pgspec", "version": "0.1.0"},
  "instance":     { "...": "...", "completeness": {} },
  "schema":       { "...": "...", "completeness": {} },
  "column_stats": { "...": "...", "completeness": {} },
  "indexes":      { "...": "...", "completeness": {} },
  "workload":     { "...": "...", "completeness": {} },
  "activity":     { "...": "...", "completeness": {} },
  "derived":      { "...": "..." },
  "temporal":     null,
  "pseudonym_map_digest": "sha256:...",
  "warnings": []
}
```

A formal JSON Schema (`schema_v1.json`) is a deliverable, not documentation-after-the-
fact; `pgspec validate` enforces it plus the invariants machine-checkably (e.g., a
regex/entropy scan asserting no field contains strings that look like identifiers or
values — belt and suspenders on I1/I2).

Serialization rules: keys sorted, floats rounded to 6 significant digits, arrays in
deterministic order (pseudonym ordinal), so `diff` on two decompressed artifacts is
meaningful.

---

## §6 — Sections and their extraction sources

### 6.1 instance

- `server_version_num`, platform hints from `pg_settings` only.
- GUC snapshot restricted to a curated allowlist (ship the list as data, not code):
  `shared_buffers, work_mem, maintenance_work_mem, effective_cache_size, max_wal_size,
  min_wal_size, checkpoint_timeout, checkpoint_completion_target, wal_compression,
  wal_level, max_connections, random_page_cost, seq_page_cost, effective_io_concurrency,
  jit, default_statistics_target, autovacuum_* (all), bgwriter_*, max_parallel_workers*,
  huge_pages, track_io_timing, shared_preload_libraries` — plus
  `pg_stat_statements.max`, `pg_stat_statements.track`, `pg_stat_statements.track_utility`.
- All `stats_reset` timestamps: `pg_stat_database.stats_reset`,
  `pg_stat_bgwriter`/`pg_stat_checkpointer` reset, `pg_stat_statements_info.stats_reset`
  (PG14+), `pg_stat_wal.stats_reset`. Counters without epochs are meaningless.
- Extension inventory: names and versions only, from `pg_extension` (extension *names*
  are not customer-sensitive and are strongly performance-relevant: postgis, pgvector,
  timescaledb, pg_cron each imply a workload species).

### 6.2 schema

Per table (from `pg_class`, `pg_partitioned_table`, `pg_inherits`, `pg_stat_user_tables`,
`pg_total_relation_size` and friends):

- pseudonymized identity, `relkind`, `reltuples`, `relpages`, heap size, TOAST size,
  total index size, `relfillfactor` (from reloptions), `relrowsecurity`,
  trigger count (`pg_trigger`, non-internal), `relpersistence`.
- Partitioning: strategy, partition-key column pseudonyms, partition count,
  min/median/max partition size, default-partition-exists flag.
- **FK graph**: adjacency list `[{from: "t_0007", from_cols: ["c_0142"], to: "t_0002",
  to_cols: ["c_0009"]}]`. Highest-value structural feature for both synthesis and
  clustering — star vs. snowflake vs. denormalized-blob live here.
- Per column (`pg_attribute` + `pg_type`): pseudonym, type OID + canonical type name +
  typmod, `attnotnull`, storage strategy, has-default flag, identity/generated flags,
  ordinal position. **Not** the default expression (I1 — defaults can embed values).

### 6.3 column_stats

From `pg_stats` (respects privileges; see §11.6) and `pg_statistic_ext` /
`pg_stats_ext`:

Pass-through (already value-free): `null_frac`, `avg_width`, `n_distinct`,
`correlation`, MCV **frequencies only** (`most_common_freqs`, values dropped in the
SELECT — the query projects the freq array and never selects `most_common_vals`),
`most_common_elem_freqs` + `elem_count_histogram` for arrays.

Transformed (see §7 for exact transforms):
- `histogram_bounds` → normalized quantile shape + span descriptor, per type class.
- Text columns → length quantiles + n_distinct + MCV freqs + entropy-of-first-bytes
  uniformity score (computed from... no — computing entropy requires values; instead:
  uniformity inferred from `n_distinct` vs. row count and MCV freq flatness only).
- Extended statistics: kind (`d`, `f`, `m`), column-group pseudonyms, and for
  functional dependencies the dependency degrees; for extended n_distinct the counts.
  These pass through untouched — pure numbers, and they are the **only cross-column
  correlation signal available without touching data**. If a database defines none,
  emit `"extended_stats": {"defined": 0}` — a recorded blind spot, per I5 spirit.

Also per column: `attstattarget` override if set, and `last_analyze`/`last_autoanalyze`
age from `pg_stat_user_tables` at table grain (staleness qualifies the whole section's
completeness).

### 6.4 indexes

From `pg_index`, `pg_class`, `pg_am`, `pg_stat_user_indexes`:

- pseudonym, table ref, access method, column pseudonym list (with ordering/opclass
  where cheaply available), uniqueness, primary flag, expression-index flag
  (expression **not** captured — I1; capture only "expression over columns [c_x, c_y]"
  via `pg_depend`/`indexprs` column extraction), partial flag with predicate reduced to
  referenced-column pseudonyms only, index size, `idx_scan`, `idx_tup_read`,
  `idx_tup_fetch`.
- For pgvector indexes (`hnsw`, `ivfflat`): am-specific reloptions (m, ef_construction,
  lists) — these are structural, not data, and given the pgvector work they matter.
- Derived here or in §6.7: unused-index inventory (idx_scan = 0 with age qualifier from
  stats_reset).

### 6.5 workload

From `pg_stat_statements`, top-K by `total_exec_time` (default K=500, flag-tunable),
plus coverage record.

Per statement:
- `queryid` (already a stable opaque hash), pseudonymized normalized text (§7.4),
  `plans/calls`, `total/mean/stddev/min/max_exec_time`, `rows`,
  shared/local/temp `blks_hit/read/dirtied/written`, `temp_blks_*`,
  `wal_records`, `wal_fpi`, `wal_bytes`,
  and on PG17+ `jit_*` if present.
- Derived per statement: verb class (SELECT/INSERT/UPDATE/DELETE/DDL/other, from the
  parse tree), referenced table pseudonym set (parse tree resolved against schema
  section), join count, aggregate-present flag, LIMIT-present flag, parameter count
  (`$n` max).
- Coverage record: fraction of `pg_stat_statements` totals (exec time, calls,
  wal_bytes) represented by the captured K; `pg_stat_statements_info.dealloc` count
  (evictions — the selection function again); statements-tracked vs. `.max`.

Explicit field: `"parameter_distributions": "unavailable_in_v1"`.

### 6.6 activity

Cumulative stats snapshot(s):
- `pg_stat_database` (current DB only): xact_commit/rollback, blks_hit/read, tup_*,
  temp_files/temp_bytes, deadlocks, conflicts breakdown (`pg_stat_database_conflicts`
  if replica), checksum failures, session counters (PG14+), `active_time`/`idle_in_
  transaction_time` where available.
- `pg_stat_bgwriter` + `pg_stat_checkpointer` (PG17 split; handle both shapes).
- `pg_stat_wal`: wal_records, wal_fpi, wal_bytes, wal_buffers_full, wal_write/sync
  counts and times if `track_wal_io_timing`.
- `pg_stat_io` (PG16+): per backend_type/object/context read/write/extend/hit/evict —
  the richest single view for cache-behavior fingerprinting; emit whole matrix.
- Per-table `pg_stat_user_tables`: seq_scan, seq_tup_read, idx_scan, n_tup_ins/upd/del,
  n_tup_hot_upd, n_live_tup, n_dead_tup, n_mod_since_analyze, vacuum/autovacuum/
  analyze/autoanalyze counts and last-times.
- `pg_stat_user_functions` if `track_functions` is on (counts/times only).

**Two-sample mode** (§8) stores this section twice plus computed deltas-as-rates.

### 6.7 derived

Computed by the driver at capture time so consumers need no re-derivation. All formulas
implemented in one module with unit tests; each value carries the names of its inputs.

- read/write ratio (tup-level and statement-level variants — they disagree
  informatively)
- HOT update fraction: `n_tup_hot_upd / nullif(n_tup_upd,0)` aggregate + per-table
  distribution summary
- index-scan share: `idx_scan / (idx_scan + seq_scan)` weighted by tuples
- WAL bytes per transaction; WAL FPI fraction
- temp-spill rate: temp_bytes per xact; statements-with-temp fraction
- cache hit ratio (db-level and pg_stat_io-level) + crude working-set bound vs.
  `shared_buffers`
- statement concentration: Shannon entropy and top-1/top-10 share of exec time over
  the statement mixture — one hot query vs. a thousand lukewarm ones is a first-order
  persona axis
- FK-graph summary: node/edge counts, degree distribution quantiles, connected
  components, max fan-in (fact-table detector)
- table-size distribution: Zipf-like fit slope or just size-share quantiles —
  "one giant fact table" vs. "500 medium tables"
- dead-tuple pressure: n_dead_tup/n_live_tup distribution, tables past autovacuum
  threshold count
- index redundancy: unused-index count and size share

---

## §7 — Privacy transforms (the actual design content)

### 7.1 Histogram bounds → normalized shape

For orderable numeric/temporal types: given bounds `b_0..b_n` (equi-frequency
quantiles), emit:

```json
{"kind": "quantile_shape",
 "n_bounds": 101,
 "positions": [0.0, 0.0012, 0.0031, "...", 1.0],
 "span": {"type_class": "timestamptz", "magnitude": "1.6e8 s"},
 "log_scale_hint": true}
```

- `positions[i] = (b_i - b_0) / (b_n - b_0)` — affine normalization; preserves skew,
  clustering, gaps; reveals no endpoint.
- `span` is magnitude-only (seconds for temporal, absolute range for numerics,
  order-of-magnitude bucketed for extra caution on numerics: `"magnitude_oom": 8`).
  **Open question 13.3**: exact span precision — raw span for temporal (low risk,
  high synthesis value) vs. OOM-bucketed for numerics (salary columns etc.).
- `log_scale_hint`: true if positions are markedly convex (heavy right skew), guiding
  synthesizers to log-normal generation.
- Degenerate cases: n_distinct small → no histogram exists; emit MCV freqs only.

### 7.2 MCV lists

Project `most_common_freqs` only. The extraction SQL must never reference
`most_common_vals` — enforce with a code-review-able constant: section queries are
string constants in one module, and a test asserts none of them mention the forbidden
columns (`most_common_vals`, `histogram_bounds` may be referenced only inside the
normalization CTE... no — normalization is client-side, so `histogram_bounds` IS
selected, transformed in memory, and discarded. Therefore: transform immediately on
fetch, never write raw rows to any buffer/log; the test instead asserts the artifact
contains no `most_common_vals`-derived content and the transform function is the only
consumer of the raw column).

Correction to hold in implementation: **raw `histogram_bounds` and `most_common_vals`
handling**: `most_common_vals` is never selected at all; `histogram_bounds` is selected
(required for normalization), transformed in the fetch loop, and the raw array is not
retained. For paranoid deployments add `--no-histograms` to skip bounds entirely and
emit MCV freqs + n_distinct only.

### 7.3 Text / uuid / bytea columns

- length quantiles from `avg_width` (only the mean is available without touching data;
  emit `avg_width` and accept the loss — do NOT query user tables for length quantiles;
  I3 forbids touching user data even for lengths)
- n_distinct, null_frac, MCV freq flatness
- uniformity score: heuristic from n_distinct ≈ reltuples ∧ no MCVs → "key-like"

### 7.4 Query-text pseudonymization

`pg_stat_statements.query` is already literal-normalized (`$1` placeholders), so the
remaining leak surface is identifiers and comments.

- Strip comments (`pglast` tokenizer).
- Parse with `pglast`; walk RangeVar, ColumnRef, ResTarget, FuncCall(?) nodes; rewrite
  relation and column identifiers through the pseudonym map; **function names**: keep
  pg_catalog functions verbatim, pseudonymize user-defined function names (they're
  identifiers too).
- Unparsable statements (utility fragments, syntax pg_stat_statements mangled): do NOT
  emit raw text. Emit `{"queryid": ..., "text": null, "unparsed": true}` with counters
  intact. Coverage record tracks unparsed fraction.
- String literals: pg_stat_statements should have normalized them, but `track_utility`
  statements can retain literals — utility statements get text dropped entirely
  (verb class only).

### 7.5 Pseudonym map

- Salt: 32 random bytes per capture, persisted to `--salt-file` so repeat captures of
  the same database are pseudonym-stable (enables longitudinal diffing).
- `pseudonym = prefix + zero-padded ordinal`, ordinals assigned by sorting
  HMAC-SHA256(salt, "schema.table.column") digests — deterministic, order-leak-free
  (alphabetical ordinals would leak name ordering).
- Map file: `{"salt_digest": "...", "map": {"t_0007": "public.orders", ...}}`,
  written 0600, with a loud header comment: KEEP THIS FILE; NOT NEEDED BY SUPABASE.
- Artifact carries `sha256(map file canonical form)` only.

---

## §8 — Two-sample capture mode

Lifetime cumulative counters are aliased — they cannot distinguish "averages 40 WAL
MB/min over 200 days" from "doing 400 right now." Two-sample mode:

1. Capture all of §6.6 (and the counter subset of 6.4/6.5: idx_scan, calls,
   total_exec_time, wal_bytes per statement) → sample A.
2. Sleep `--interval` (default 900s; document that longer is better; the customer can
   run `--interval 3600` over lunch).
3. Capture again → sample B.
4. Emit: `lifetime` (sample B raw), `interval` `{start, end, seconds}`, and
   `rates` = (B−A)/Δt for every counter, with counter-reset detection (any negative
   delta → mark that counter family `reset_during_capture`, exclude from rates).

Statement-level rates additionally catch **eviction churn**: queryids present in A but
missing in B (or vice versa) are recorded; high churn means pg_stat_statements.max is
too small for this workload and coverage numbers are optimistic — into the
completeness object it goes.

Rates feed the `derived` section preferentially over lifetime averages when present.

---

## §9 — pgfr v2 temporal augmentation (optional)

Precondition: pg_flight_recorder v2 installed and readable. Detection: presence of the
pgfr schema and its manifest table; `--pgfr auto` (default) uses it if present,
`require` fails without it, `off` ignores it.

Because pgfr v2's data model is the cumulative statistics system presented as a time
series, the augmentation is definitionally "sections 6.5/6.6 with a time axis." The
extractor is a **consumer of pgfr_record's output only** — all judgment below lives in
pgspec, consistent with the pgfr_record/pgfr_analyze definitional/judgmental split. No
pgfr schema changes are required or proposed by this project.

`temporal` section contents (all computed summaries — raw pgfr rows never ship):

```json
{"window": {"start": "...", "end": "...", "bucket_seconds": 60,
            "completeness_fraction": 0.994,
            "source": "pgfr_v2", "capture_ledger_gaps": 3},
 "metrics": { "wal_bytes_rate":  {"quantiles": {"p50":..., "p95":..., "p99":..., "max":...},
                                  "seasonal_24x7": [[...24 floats...] x 7],
                                  "trend_slope_per_day": ...},
              "tps": {...}, "temp_bytes_rate": {...}, "blks_read_rate": {...},
              "dead_tup_growth": {...} },
 "statement_mixture": {"top_n": 50,
   "share_timeseries_bucket_seconds": 3600,
   "churn_week_over_week": 0.07,
   "series": [{"queryid": ..., "shares": [...]}]},
 "events": [{"kind": "batch_write", "cadence": "weekdays",
             "phase_local_time": "13:05", "duration_s": 1500,
             "magnitude_x_baseline": 11.2, "evidence": ["wal_bytes_rate","n_tup_ins"]}],
 "maintenance": {"autovacuum_events_per_day_by_table_quantiles": {...},
                 "checkpoint_interval_quantiles": {...},
                 "dead_tuple_sawtooth_amplitude_quantiles": {...}}}
```

Value ordering (implement in this order):
1. **Quantiles over time buckets** for headline rates — SLI surfaces must be
   benchmarked at the peak operating point; only the time series locates it.
2. **24×7 seasonal profiles** (hour-of-day × day-of-week means, simple aggregation;
   STL is overkill for v1) — the strongest persona feature; temporal shape likely
   classifies more cleanly than static structure (cf. the startup archetype result).
3. **Statement-mixture drift/churn** — stable mixtures are predictable; churning ones
   widen every downstream error bar; churn becomes a confidence qualifier on the whole
   signature.
4. **Batch detection** — threshold-crossing on rate series vs. rolling baseline;
   cadence/phase/magnitude. The workload feature most likely to break naive synthetic
   replay (Lauca's continuity finding, from the temporal side). Keep the detector dumb
   in v1: z-score > k against hour-of-day baseline, merge adjacent buckets.
5. **Maintenance rhythm** — autovacuum cadence, checkpoint spacing, dead-tuple
   sawtooth amplitude: the background load an honest sustained-performance benchmark
   must reproduce.

Completeness: pgfr's capture ledger supplies gap accounting directly — surface
`completeness_fraction` and gap count verbatim.

Interaction with two-sample mode: if pgfr is present, two-sample is redundant for the
overlapping counters; capture_mode becomes `pgfr` and the interval sleep is skipped
(rates come from the time series). Keep the code paths separate and simple: pgfr wins
when present.

---

## §10 — Completeness objects (per section)

Uniform shape:

```json
{"available": true,
 "coverage": {"...section-specific fractions..."},
 "staleness": {"stats_reset_age_s": ..., "oldest_analyze_age_s": ...},
 "limits": {"default_statistics_target": 100, "pg_stat_statements_max": 5000,
            "top_k_captured": 500, "dealloc_count": 812},
 "notes": []}
```

Section-specific coverage: workload → exec-time/calls/WAL fractions captured + unparsed
fraction + eviction churn; column_stats → fraction of columns with stats present,
fraction analyzed within 7 days; temporal → window completeness; schema → tables
skipped for privilege reasons (count).

---

## §11 — Edge cases (decided handling)

1. **Partitioned tables.** Capture both the partitioned parent (topology, key) and
   leaf partitions (stats live on leaves). Derived section aggregates per partition
   *family*. pg_stat_statements text references parents; table-ref resolution must map
   both.
2. **Stats reset mid-life / recently.** stats_reset ages in completeness; a reset
   younger than 24h emits a warning ("counters describe a short epoch").
3. **pg_stat_statements absent or track=none.** Workload section
   `available: false`, everything else proceeds (a schema+stats-only fingerprint is
   still clusterable).
4. **PG version drift (14→17).** Version-gated queries per section
   (pg_stat_checkpointer split, pg_stat_io presence, JIT columns). One capability probe
   at connect; sections declare required capabilities.
5. **Huge schemas (10k+ tables).** Single-pass set-based catalog queries only (no
   per-table round trips); per-column stats fetched in batches; `--max-tables` guard
   that samples uniformly and records sampling in completeness (selection function!).
6. **Privilege shortfalls.** `pg_stats` filters rows by column privilege; run as a
   role with `pg_read_all_stats` ideally; otherwise per-row absence is counted into
   coverage, not fatal. `pg_stat_statements` needs `pg_read_all_stats` for other
   users' queries — without it, coverage records "own-role statements only."
7. **Replicas.** Detect `pg_is_in_recovery()`; some views empty/different on standby;
   record `captured_on: replica` (a replica-side fingerprint is a valid but distinct
   object — read-mixture only).
8. **RLS.** Irrelevant to catalogs/stats (we never query user tables), but record
   `relrowsecurity` per table as a structural feature.
9. **Ongoing capture interference.** statement_timeout aborts a slow catalog query →
   section marked partial, capture continues (I5).
10. **Identifier collisions after pseudonymization.** Impossible by construction
    (ordinals), but the map writer asserts bijectivity anyway.
11. **Same table name in multiple schemas.** Pseudonyms are over fully qualified
    names; schema pseudonyms are separate (`s_01.t_0007` renders as distinct tables).
12. **Extensions with their own stats (pgvector, timescaledb).** v1 captures their
    presence, their indexes' reloptions (6.4), nothing extension-internal. Recorded
    as a known v2 direction.

---

## §12 — Test plan

Framework: pytest + dockerized PostgreSQL matrix (14, 15, 16, 17), fixture databases
seeded by SQL scripts. pgTAP is not the right tool here — the unit under test is the
client-side driver.

**Fixture scenarios** (each a seeded database + optional synthetic pg_stat_statements
load generated by running a small workload script before capture):

- `f_star`: star schema, one fact table 10M rows, 6 dimensions, FK graph fan-in.
- `f_flat`: denormalized single-wide-table app.
- `f_multitenant`: LIST-partitioned tenant-per-partition (pgpm-shaped).
- `f_skew`: heavy value skew (Zipfian column), verifies histogram shape transform
  preserves convexity (`log_scale_hint`).
- `f_writeheavy`: update-churn table with low HOT fraction and dead-tuple pressure.
- `f_empty`: fresh database (degenerate-path coverage).
- `f_pgfr`: any of the above + pgfr v2 installed with a synthesized 7-day history
  including a scripted weekday 13:00 batch spike — batch detector must find it, with
  correct cadence and phase.

**Invariant tests (the ones that matter most):**

- *No-values scan*: decompressed artifact contains no token from a canary set of
  distinctive literals seeded into every fixture's data (names, emails, UUIDs,
  dollar amounts). This test is the project's conscience; it runs on every fixture.
- *No-identifiers scan*: no fixture table/column/index/schema name appears anywhere
  in the artifact (canary identifier names like `xq_secret_salaries` seeded on
  purpose).
- *Forbidden-column test*: static assertion that no extraction SQL references
  `most_common_vals`; runtime assertion that `histogram_bounds` raw arrays are not
  retained post-transform (transform function is sole consumer).
- *Determinism*: two captures of an idle fixture with same salt → identical artifacts
  modulo `captured_at`/`capture_duration_s`.
- *Read-only*: capture succeeds with `default_transaction_read_only=on` under a role
  with only `pg_read_all_stats` + CONNECT; a wire-log test asserts zero non-SELECT
  statements (excepting SETs).
- *Schema validation*: every fixture artifact validates against `schema_v1.json`.
- *Fail-soft matrix*: drop pg_stat_statements / revoke privileges / kill pgfr →
  sections degrade with `available:false` + reason, exit code still 0 with warnings.
- *Two-sample*: scripted load between samples → rates within tolerance of the
  scripted rates; scripted stats reset between samples → counter family excluded.
- *Round-trip pseudonyms*: `pgspec deref` over the map restores real identifiers
  exactly; map digest matches.
- *Performance*: `f_star` inflated to 5k tables captures in < 120s.

---

## §13 — Open questions for the build session

1. **Package/tool naming** — pg_spectrograph/pgspec is a proposal.
2. **Top-K default and ranking key** — 500 by total_exec_time proposed; consider a
   union of top-K by exec_time, by calls, and by wal_bytes (three lenses; dedupe).
3. **Span precision policy** (§7.1) — raw span for temporal types, OOM-bucketed for
   numerics is the proposed default; is a per-column override / global `--paranoid`
   flag worth the surface area?
4. **Artifact size budget** — target < 5 MB gzipped for a 10k-table DB? Drives
   per-column histogram retention policy (all columns vs. columns referenced by
   captured workload only — the latter is attractive and self-focusing).
5. **pglast version pinning vs. server version** — libpg_query grammar vs. PG17
   syntax; how much unparsed fraction is acceptable before we care?
6. **Should `pgspec inspect` render a one-page "spectral summary"** (the derived
   section pretty-printed) for use in customer conversations? Cheap, high leverage —
   probably yes; scope it.
7. **License/repo home** — personal repo (like pgpm) vs. supabase org from day one.

---

## §14 — Definition of done (v1)

- `pgspec capture` produces a validating artifact against PG 14–17 fixtures with all
  invariant tests green, including the no-values and no-identifiers canary scans.
- Two-sample mode produces correct rates under scripted load, with reset detection.
- pgfr augmentation produces the temporal section from a synthesized 7-day history,
  and the batch detector locates the scripted weekday spike with correct phase.
- `validate`, `inspect`, `deref` subcommands implemented.
- Single-file `pgspec.pyz` build runs against a fresh Supabase project with nothing
  but Python 3.11 and network access.
- README covering: what is captured, what is provably NOT captured, how to verify
  that claim yourself (`pgspec inspect` + the map-stays-local design), and the
  two-sample and pgfr modes.

---

## Appendix A — Reference SQL sketches

(Starting points, not final; the build session owns these. All queries run under
`default_transaction_read_only=on`, `statement_timeout`, `lock_timeout` per I3.)

```sql
-- A.1 tables (single pass)
SELECT n.nspname, c.relname, c.relkind, c.reltuples, c.relpages,
       c.relrowsecurity, c.relpersistence, c.reloptions,
       pg_table_size(c.oid) AS heap_bytes,
       pg_indexes_size(c.oid) AS index_bytes,
       pg_total_relation_size(c.oid)
         - pg_table_size(c.oid) - pg_indexes_size(c.oid) AS toast_bytes
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('r','p','m')
  AND n.nspname NOT IN ('pg_catalog','information_schema')
  AND n.nspname NOT LIKE 'pg_toast%';

-- A.2 FK graph
SELECT con.conrelid, con.confrelid, con.conkey, con.confkey
FROM pg_constraint con WHERE con.contype = 'f';

-- A.3 column stats (NOTE: most_common_vals intentionally absent)
SELECT schemaname, tablename, attname, null_frac, avg_width, n_distinct,
       most_common_freqs, histogram_bounds::text::text[] AS raw_bounds, correlation,
       most_common_elem_freqs, elem_count_histogram
FROM pg_stats
WHERE schemaname NOT IN ('pg_catalog','information_schema');
-- raw_bounds transformed in fetch loop, never retained (I1, §7.2)

-- A.4 workload top-K with coverage
WITH totals AS (
  SELECT sum(total_exec_time) t, sum(calls) c, sum(wal_bytes) w
  FROM pg_stat_statements WHERE dbid = (SELECT oid FROM pg_database
                                        WHERE datname = current_database()))
SELECT s.*, (SELECT t FROM totals) AS grand_exec_time,
       (SELECT c FROM totals) AS grand_calls,
       (SELECT w FROM totals) AS grand_wal
FROM pg_stat_statements s
WHERE s.dbid = (SELECT oid FROM pg_database WHERE datname = current_database())
ORDER BY s.total_exec_time DESC
LIMIT %(top_k)s;

-- A.5 pgfr temporal quantiles (shape depends on pgfr v2 final schema; sketch)
--   read pgfr partitions for pg_stat_wal series, bucket to 60s,
--   compute rate = delta(wal_bytes)/delta(ts), then quantiles + 24x7 profile
--   client-side over the fetched buckets (keep SQL dumb, judgment in pgspec).
```

## Appendix B — Provenance

Designed in a Claude web session, 2026-09-25, from David's dictated concept:
per-workload SLIs require signatures sufficient for performance but empty of customer
data; personas emerge from clustering signatures at fleet scale; pgfr v2 supplies the
optional temporal axis. This pack is step one (extraction) of the larger program
(synthesis → benchmarking → clustering → per-persona SLO surfaces), which is explicitly
out of scope here.
