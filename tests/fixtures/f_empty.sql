-- f_empty (§12): a fresh database with no user tables at all. Degenerate-
-- path coverage: every section must handle "nothing to capture" cleanly,
-- never crash on empty result sets.

CREATE EXTENSION IF NOT EXISTS pg_stat_statements;
