# Seeded scenario databases

The minimal canary-literal fixture landed at Milestone 3 as
`tests/docker/canary_fixture.sql` (mounted into the PG16 container directly,
rather than living here) since it's a single-file init script, not a
scenario database in its own right.

The full scenario set (`f_star`, `f_flat`, `f_multitenant`, `f_skew`,
`f_writeheavy`, `f_empty`, `f_pgfr`) per the frozen context pack's §12 arrives
at Milestone 12.
