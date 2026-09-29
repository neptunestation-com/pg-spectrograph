# Seeded scenario databases

All seven §12 scenarios are implemented here. Six are seeded scenario
databases: `f_star.sql`, `f_flat.sql`, `f_multitenant.sql`, `f_skew.sql`,
`f_writeheavy.sql`, `f_empty.sql` (plus `f_perf_5k_tables.sql`, `f_star`
inflated for the performance and artifact-size budget test). Each is loaded on
demand into the pg16 container via `tests/conftest.py`'s `scenario_dsn` factory
fixture, into a database named after the fixture plus a hash of its content, so
an edited fixture reloads and a stale one is left behind; see
`tests/test_scenario_fixtures.py`.

`f_pgfr.sql` (Milestones 13 and 14) is the seventh and different in kind: not
a scenario database but a synthesized pg_flight_recorder v2 history, loaded
by `conftest.py`'s `pgfr_dsn` into the pg_cron-enabled container from
`tests/docker/docker-compose.pgfr.yml` after `pgfr_record` and `pgfr_analyze`
are installed, and reloaded whenever the file's content digest (recorded as
the database comment) changes. It writes collector-shaped rows straight into
pgfr's archive tables: 7 days of Group A (`a_pg_stat_wal` with a 12x weekday
13:00-13:30 UTC batch on a ~100 kB/s baseline, `a_pg_stat_database`,
`a_pg_stat_bgwriter`), 28 days of `a_pg_stat_all_tables` for 12 synthetic
tables with a dead-tuple sawtooth and periodic autovacuums, 14 days of
`a_pg_stat_statements` for 25 synthetic statements with a scripted mixture
shift one week in, 28 days of the hourly `r_pg_stat_activity` rollup, and
`ledger_runs` for the fast and medium tiers with one hole each. It creates the
past partitions pgfr's create-ahead maintenance never made, then calls
`pgfr_record.run_tier('medium')` so pgfr's own collector closes the daily
Group B rollups from those raw rows. See `tests/test_temporal_pgfr_live.py`;
the pure helpers are unit-tested in `tests/test_temporal_pgfr.py`.

The minimal canary-literal fixture (Milestone 3) lives separately, as
`tests/docker/canary_fixture.sql` (mounted into every matrix container
directly), since it's a single-file init script, not a scenario database.
