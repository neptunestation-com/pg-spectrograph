-- f_star (§12): star schema, one fact table, six dimensions, FK fan-in.
-- Row counts are scaled down from the spec's "10M rows" for practical test
-- runtime; the point of this fixture is exercising star-schema FK-graph
-- topology (max fan-in, connected components) and a real fact/dimension
-- size skew, not literally hitting 10M rows.

CREATE EXTENSION IF NOT EXISTS pg_stat_statements;

CREATE TABLE dim_customer (id serial PRIMARY KEY, name text);
CREATE TABLE dim_product (id serial PRIMARY KEY, name text);
CREATE TABLE dim_store (id serial PRIMARY KEY, name text);
CREATE TABLE dim_date (id serial PRIMARY KEY, day date);
CREATE TABLE dim_promo (id serial PRIMARY KEY, name text);
CREATE TABLE dim_employee (id serial PRIMARY KEY, name text);

CREATE TABLE fact_sales (
    id bigserial PRIMARY KEY,
    customer_id integer NOT NULL REFERENCES dim_customer(id),
    product_id integer NOT NULL REFERENCES dim_product(id),
    store_id integer NOT NULL REFERENCES dim_store(id),
    date_id integer NOT NULL REFERENCES dim_date(id),
    promo_id integer REFERENCES dim_promo(id),
    employee_id integer NOT NULL REFERENCES dim_employee(id),
    amount numeric
);

INSERT INTO dim_customer (name) SELECT 'customer_' || i FROM generate_series(1, 1000) i;
INSERT INTO dim_product (name) SELECT 'product_' || i FROM generate_series(1, 200) i;
INSERT INTO dim_store (name) SELECT 'store_' || i FROM generate_series(1, 20) i;
INSERT INTO dim_date (day) SELECT date '2026-01-01' + i FROM generate_series(0, 364) i;
INSERT INTO dim_promo (name) SELECT 'promo_' || i FROM generate_series(1, 10) i;
INSERT INTO dim_employee (name) SELECT 'employee_' || i FROM generate_series(1, 50) i;

INSERT INTO fact_sales (customer_id, product_id, store_id, date_id, promo_id, employee_id, amount)
SELECT
    (random() * 999 + 1)::int,
    (random() * 199 + 1)::int,
    (random() * 19 + 1)::int,
    (random() * 364 + 1)::int,
    CASE WHEN random() < 0.7 THEN (random() * 9 + 1)::int ELSE NULL END,
    (random() * 49 + 1)::int,
    (random() * 500)::numeric(10, 2)
FROM generate_series(1, 200000);

ANALYZE;
