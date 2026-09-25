-- Minimal canary fixture (Milestone 3): a small schema seeded with
-- distinctive literal values and identifier names, per §12's "no-values" and
-- "no-identifiers" canary scans. Every table/column/identifier name here, and
-- every literal value, is checked to never appear anywhere in a captured
-- artifact.

CREATE EXTENSION IF NOT EXISTS pg_stat_statements;

CREATE TABLE public.xq_secret_salaries (
    id serial PRIMARY KEY,
    employee_email text,
    employee_name text,
    salary_dollars numeric,
    external_uuid uuid
);

INSERT INTO public.xq_secret_salaries
    (employee_email, employee_name, salary_dollars, external_uuid)
VALUES
    ('alice.wonderland@canary-example.test', 'Alice Wonderland', 123456.78,
     '11111111-1111-1111-1111-111111111111'),
    ('bob.builder@canary-example.test', 'Bob Builder', 98765.43,
     '22222222-2222-2222-2222-222222222222'),
    ('carol.danvers@canary-example.test', 'Carol Danvers', 55555.55,
     '33333333-3333-3333-3333-333333333333');

ANALYZE public.xq_secret_salaries;

-- Milestone 4 additions: an FK relationship and a partitioned table, so
-- schema.py has real structure to extract and pseudonymize.

CREATE TABLE public.xq_customers (
    id serial PRIMARY KEY,
    customer_name text,
    signup_amount numeric
);

INSERT INTO public.xq_customers (customer_name, signup_amount)
SELECT 'canary_customer_' || i, (i % 50)::numeric
FROM generate_series(1, 500) AS i;

CREATE TABLE public.xq_orders (
    id serial PRIMARY KEY,
    customer_id integer NOT NULL REFERENCES public.xq_customers(id),
    order_amount numeric
);

INSERT INTO public.xq_orders (customer_id, order_amount)
SELECT (i % 500) + 1, (i % 733)::numeric
FROM generate_series(1, 2000) AS i;

CREATE TABLE public.xq_events (
    id bigserial,
    event_at timestamptz NOT NULL,
    event_type text NOT NULL
) PARTITION BY RANGE (event_at);

CREATE TABLE public.xq_events_2026_01 PARTITION OF public.xq_events
    FOR VALUES FROM ('2026-01-01') TO ('2026-02-01');
CREATE TABLE public.xq_events_2026_02 PARTITION OF public.xq_events
    FOR VALUES FROM ('2026-02-01') TO ('2026-03-01');

INSERT INTO public.xq_events (event_at, event_type)
SELECT '2026-01-01'::timestamptz + (i || ' minutes')::interval,
       CASE WHEN i % 10 = 0 THEN 'canary_checkout' ELSE 'canary_pageview' END
FROM generate_series(1, 1000) AS i;

CREATE STATISTICS public.xq_orders_customer_amount_stats (dependencies, ndistinct)
    ON customer_id, order_amount FROM public.xq_orders;

ANALYZE public.xq_customers;
ANALYZE public.xq_orders;
ANALYZE public.xq_events;
