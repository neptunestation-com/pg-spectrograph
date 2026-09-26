# Seeded scenario databases

Six of the seven §12 scenarios are implemented here: `f_star.sql`, `f_flat.sql`,
`f_multitenant.sql`, `f_skew.sql`, `f_writeheavy.sql`, `f_empty.sql`. Each is
loaded on demand into the pg16 container (a fresh database per scenario, left
in place after first load) via `tests/conftest.py`'s `scenario_dsn` factory
fixture; see `tests/test_scenario_fixtures.py`.

`f_pgfr` (any of the above plus pg_flight_recorder v2 installed with a
synthesized 7-day history and a scripted batch spike) is deferred: per the
Milestone 11 scope decision, this project does not stand up a live pg_cron +
pgfr_record installation. The batch/z-score detector itself
(`detect_batch_events` in `pgspec.sections.temporal_pgfr`) is unit-tested
directly against synthetic bucketed series instead.

The minimal canary-literal fixture (Milestone 3) lives separately, as
`tests/docker/canary_fixture.sql` (mounted into every matrix container
directly), since it's a single-file init script, not a scenario database.
