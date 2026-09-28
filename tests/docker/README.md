# Docker fixtures

`docker-compose.yml` + `canary_fixture.sql`: the PG14-17 matrix (Milestone 12),
every container seeded with the canary table `public.xq_secret_salaries`
(Milestone 3), used by the read-only wire-log test, the no-values and
no-identifiers canary scans, and the version-matrix test. `tests/conftest.py`'s
`pg16_dsn` fixture brings up pg16 alone (most tests need only that one);
`pg_matrix_dsns` brings up all four. Containers are left running between
local test runs.

`docker-compose.pgfr.yml`: the pgfr v2 fixture (Milestone 13), a PG16 image
built from pg_flight_recorder's own Dockerfile (pg_cron and pgTAP compiled in)
at the SHA pinned in `conftest.py` (`PGFR_SHA`). `conftest.py`'s `pgfr_dsn`
fixture shallow-clones that SHA into `.pgfr-src/` (gitignored; it is both the
build context and the source of `install.sql`), brings the container up,
installs `pgfr_record` and `pgfr_analyze`, and loads
`tests/fixtures/f_pgfr.sql`. The first run builds the image, which takes a few
minutes. To start over: `docker compose -f tests/docker/docker-compose.pgfr.yml down -v`.
