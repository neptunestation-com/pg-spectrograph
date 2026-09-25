# Docker fixtures

`docker-compose.yml` + `canary_fixture.sql`: a single PG16 container seeded
with the canary table `public.xq_secret_salaries` (Milestone 3), used by the
read-only wire-log test and the no-values/no-identifiers canary scans.
`tests/conftest.py`'s `pg16_dsn` fixture brings it up automatically (and
leaves it running between local test runs).

The full PG14-17 compose matrix arrives at Milestone 12.
