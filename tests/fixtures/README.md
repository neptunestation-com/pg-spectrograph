# Seeded scenario databases

All seven §12 scenarios are implemented here. Six are seeded scenario
databases: `f_star.sql`, `f_flat.sql`, `f_multitenant.sql`, `f_skew.sql`,
`f_writeheavy.sql`, `f_empty.sql` (plus `f_perf_5k_tables.sql`, `f_star`
inflated for the performance and artifact-size budget test). Each is loaded on
demand into the pg16 container via `tests/conftest.py`'s `scenario_dsn` factory
fixture, into a database named after the fixture plus a hash of its content, so
an edited fixture reloads and a stale one is left behind; see
`tests/test_scenario_fixtures.py`.

`f_pgfr.sql` (Milestone 13) is the seventh and different in kind: not a
scenario database but a synthesized 7-day pg_flight_recorder v2 history,
loaded once by `conftest.py`'s `pgfr_dsn` into the pg_cron-enabled container
from `tests/docker/docker-compose.pgfr.yml` after `pgfr_record` and
`pgfr_analyze` are installed. It writes collector-shaped rows straight into
pgfr's archive tables (`a_pg_stat_wal` with a 12x weekday 13:00-13:30 UTC batch
on a ~100 kB/s baseline, `a_pg_stat_database` with smooth counters) and
`ledger_runs` (one fast-tier run per minute with a 2-hour hole), creating the
past partitions pgfr's create-ahead maintenance never made. See
`tests/test_temporal_pgfr_live.py`; the detector is also unit-tested against
synthetic bucketed series in `tests/test_temporal_pgfr.py`.

The minimal canary-literal fixture (Milestone 3) lives separately, as
`tests/docker/canary_fixture.sql` (mounted into every matrix container
directly), since it's a single-file init script, not a scenario database.
