-- f_skew (§12): heavy value skew (Zipfian-shaped column). Verifies the
-- skew_gini transform (issue #2 finding 2, replacing the boolean
-- log_scale_hint) actually distinguishes a heavily skewed column from a
-- uniform one.

CREATE EXTENSION IF NOT EXISTS pg_stat_statements;

CREATE TABLE skewed_events (
    id bigserial PRIMARY KEY,
    category text,
    uniform_category text,
    value numeric
);

INSERT INTO skewed_events (category, uniform_category, value)
SELECT
    CASE
        WHEN r < 0.60 THEN 'A'
        WHEN r < 0.80 THEN 'B'
        WHEN r < 0.90 THEN 'C'
        WHEN r < 0.95 THEN 'D'
        ELSE 'long_tail_' || (i % 2000)
    END,
    'category_' || (i % 10),
    (random() * 1000)::numeric(10, 2)
FROM (SELECT i, random() AS r FROM generate_series(1, 100000) i) s;

ANALYZE;
