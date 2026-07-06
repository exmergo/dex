-- Seed for the dex PostgreSQL dogfood/integration database.
--
-- Applied by scripts/setup_postgres_dev.sh (local Docker dogfood) and by the
-- postgres job in .github/workflows/integration.yml (service container). Run
-- against the dex_dogfood database as a superuser; scripts/setup_postgres_dev.sh
-- handles creating the database and re-running this file idempotently.
--
-- The shape is a small realistic operational schema that exercises every
-- explore/transform/maintain surface:
--   - customers: PII columns (email, phone, names, address) for flag-not-surface
--   - products: enum, numeric, text[] and jsonb columns for type degradation
--   - orders -> customers, order_items -> orders, payments -> orders: declared FKs
--   - order_items.product_id: deliberately NO declared FK (inference target)
--   - v_order_totals: a view (no stored rows)
--   - events: ~100k rows via generate_series (sampling / statement_timeout target)
--   - dbt_dev: the empty dev schema dbt builds into (never a source)
-- Roles:
--   - dex_ro: the read-only role dex connects as (USAGE + SELECT on app only)
--   - dbt_dev: reads app, writes only schema dbt_dev, statement_timeout pinned

BEGIN;

DROP SCHEMA IF EXISTS app CASCADE;
DROP SCHEMA IF EXISTS dbt_dev CASCADE;

CREATE SCHEMA app;
CREATE SCHEMA dbt_dev;

CREATE TYPE app.product_category AS ENUM ('electronics', 'grocery', 'apparel', 'home');
CREATE TYPE app.order_status AS ENUM ('pending', 'paid', 'shipped', 'cancelled');

CREATE TABLE app.customers (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    email       text NOT NULL UNIQUE,
    first_name  text NOT NULL,
    last_name   text NOT NULL,
    phone       text,
    address     text,
    city        text,
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE app.products (
    id        bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name      text NOT NULL,
    category  app.product_category NOT NULL,
    price     numeric(10, 2) NOT NULL,
    tags      text[] NOT NULL DEFAULT '{}',
    attrs     jsonb NOT NULL DEFAULT '{}'
);

CREATE TABLE app.orders (
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    customer_id  bigint NOT NULL REFERENCES app.customers (id),
    status       app.order_status NOT NULL DEFAULT 'pending',
    total        numeric(12, 2) NOT NULL DEFAULT 0,
    ordered_at   timestamptz NOT NULL DEFAULT now()
);

-- product_id deliberately carries no foreign key: relationship inference must
-- find the app.order_items.product_id -> app.products.id join on its own.
CREATE TABLE app.order_items (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    order_id    bigint NOT NULL REFERENCES app.orders (id),
    product_id  bigint NOT NULL,
    quantity    integer NOT NULL,
    unit_price  numeric(10, 2) NOT NULL
);

CREATE TABLE app.payments (
    id       bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    order_id bigint NOT NULL REFERENCES app.orders (id),
    method   text NOT NULL,
    payload  bytea,
    paid_at  timestamptz NOT NULL DEFAULT now()
);

CREATE VIEW app.v_order_totals AS
SELECT o.id AS order_id,
       o.customer_id,
       o.status,
       sum(i.quantity * i.unit_price) AS computed_total
FROM app.orders o
JOIN app.order_items i ON i.order_id = o.id
GROUP BY o.id, o.customer_id, o.status;

INSERT INTO app.customers (email, first_name, last_name, phone, address, city)
SELECT format('user%s@example.com', g),
       format('First%s', g),
       format('Last%s', g),
       CASE WHEN g % 7 = 0 THEN NULL ELSE format('+1-555-%s', 1000 + g) END,
       format('%s Main Street', g),
       (ARRAY['Amsterdam', 'Berlin', 'Lisbon', 'Milan', 'Porto'])[1 + g % 5]
FROM generate_series(1, 500) AS g;

INSERT INTO app.products (name, category, price, tags, attrs)
SELECT format('Product %s', g),
       (ARRAY['electronics', 'grocery', 'apparel', 'home'])[1 + g % 4]::app.product_category,
       round((random() * 200 + 1)::numeric, 2),
       ARRAY['tag' || (g % 10), 'tag' || (g % 3)],
       jsonb_build_object('weight_g', g * 10, 'in_stock', g % 2 = 0)
FROM generate_series(1, 80) AS g;

INSERT INTO app.orders (customer_id, status, total, ordered_at)
SELECT 1 + (g * 7) % 500,
       (ARRAY['pending', 'paid', 'shipped', 'cancelled'])[1 + g % 4]::app.order_status,
       0,
       now() - (g % 365) * interval '1 day'
FROM generate_series(1, 2000) AS g;

INSERT INTO app.order_items (order_id, product_id, quantity, unit_price)
SELECT 1 + (g * 3) % 2000,
       1 + (g * 11) % 80,
       1 + g % 5,
       round((random() * 200 + 1)::numeric, 2)
FROM generate_series(1, 5000) AS g;

UPDATE app.orders o
SET total = t.computed_total
FROM (
    SELECT order_id, sum(quantity * unit_price) AS computed_total
    FROM app.order_items
    GROUP BY order_id
) AS t
WHERE o.id = t.order_id;

INSERT INTO app.payments (order_id, method, payload, paid_at)
SELECT o.id,
       (ARRAY['card', 'transfer', 'wallet'])[1 + o.id % 3],
       decode(md5(o.id::text), 'hex'),
       o.ordered_at + interval '1 hour'
FROM app.orders o
WHERE o.status IN ('paid', 'shipped');

-- The big table: sampling threshold and statement_timeout exercises.
CREATE TABLE app.events (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    customer_id bigint NOT NULL,
    event_type  text NOT NULL,
    payload     jsonb NOT NULL,
    occurred_at timestamptz NOT NULL
);

INSERT INTO app.events (customer_id, event_type, payload, occurred_at)
SELECT 1 + g % 500,
       (ARRAY['page_view', 'add_to_cart', 'checkout', 'search', 'login'])[1 + g % 5],
       jsonb_build_object('session', g % 10000, 'step', g % 7),
       now() - (g % 90) * interval '1 hour'
FROM generate_series(1, 100000) AS g;

COMMIT;

-- Roles (idempotent: created once per cluster, re-granted per seed run).
DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'dex_ro') THEN
        CREATE ROLE dex_ro LOGIN PASSWORD 'dex_ro';
    END IF;
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'dbt_dev') THEN
        CREATE ROLE dbt_dev LOGIN PASSWORD 'dbt_dev';
    END IF;
END
$$;

GRANT CONNECT ON DATABASE dex_dogfood TO dex_ro, dbt_dev;

-- dex_ro: read the app schema, nothing else. No access to dbt_dev.
GRANT USAGE ON SCHEMA app TO dex_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA app TO dex_ro;
ALTER DEFAULT PRIVILEGES IN SCHEMA app GRANT SELECT ON TABLES TO dex_ro;

-- dbt_dev: read sources, write only the dev schema; server-side time cap.
GRANT USAGE ON SCHEMA app TO dbt_dev;
GRANT SELECT ON ALL TABLES IN SCHEMA app TO dbt_dev;
ALTER DEFAULT PRIVILEGES IN SCHEMA app GRANT SELECT ON TABLES TO dbt_dev;
GRANT USAGE, CREATE ON SCHEMA dbt_dev TO dbt_dev;
ALTER ROLE dbt_dev SET statement_timeout = '600s';

ANALYZE;
