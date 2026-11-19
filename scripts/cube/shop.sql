-- The rows every Cube test and dogfood runs on: dex's own DuckDB fixture
-- (tests/cube_vendor/cube_cases.py:write_shop), the executing Cube Core
-- (scripts/setup_cube_dev.sh --execute), and the Cube Cloud fixture deployment
-- (scripts/cube_cloud/cube.py), so a number Cube executed and one dex executed
-- are computed from the same table. Idempotent: Cube runs it on every DuckDB
-- instance it opens.
CREATE SCHEMA IF NOT EXISTS app;

CREATE OR REPLACE TABLE app.customers AS
SELECT
    CAST(id AS BIGINT) AS id,
    CAST(email AS VARCHAR) AS email,
    CAST(city AS VARCHAR) AS city,
    CAST(created_at AS TIMESTAMPTZ) AS created_at
FROM (VALUES
    (1, 'a@example.com', 'Lisbon', '2025-01-01 00:00:00+00'),
    (2, 'b@example.com', 'Porto', '2025-02-01 00:00:00+00'),
    (3, 'c@example.com', 'Milan', '2025-03-01 00:00:00+00')
) AS t (id, email, city, created_at);

CREATE OR REPLACE TABLE app.orders AS
SELECT
    CAST(id AS BIGINT) AS id,
    CAST(customer_id AS BIGINT) AS customer_id,
    CAST(status AS VARCHAR) AS status,
    CAST(total AS DECIMAL(12, 2)) AS total,
    CAST(ordered_at AS TIMESTAMPTZ) AS ordered_at
FROM (VALUES
    (1, 1, 'paid', '10.00', '2026-01-03 10:00:00+00'),
    (2, 1, 'paid', '5.50', '2026-01-20 08:30:00+00'),
    (3, 2, 'shipped', '7.00', '2026-02-01 12:00:00+00'),
    (4, 2, 'paid', '2.50', '2026-02-14 09:15:00+00'),
    (5, 3, 'pending', '4.00', '2026-03-09 17:45:00+00'),
    (6, 3, 'cancelled', '1000.00', '2025-03-10 11:00:00+00'),
    (7, 1, 'shipped', '12.25', '2026-03-30 23:30:00+00'),
    (8, 2, 'paid', '3.75', '2025-12-31 23:59:00+00')
) AS t (id, customer_id, status, total, ordered_at);

CREATE OR REPLACE TABLE app.products AS
SELECT
    CAST(id AS BIGINT) AS id,
    CAST(name AS VARCHAR) AS name,
    CAST(category AS VARCHAR) AS category,
    CAST(price AS DECIMAL(10, 2)) AS price
FROM (VALUES
    (1, 'p1', 'grocery', '1'),
    (2, 'p2', 'home', '2'),
    (3, 'p3', 'grocery', '3')
) AS t (id, name, category, price);

CREATE OR REPLACE TABLE app.order_items AS
SELECT
    CAST(id AS BIGINT) AS id,
    CAST(order_id AS BIGINT) AS order_id,
    CAST(product_id AS BIGINT) AS product_id,
    CAST(quantity AS INTEGER) AS quantity,
    CAST(unit_price AS DECIMAL(10, 2)) AS unit_price
FROM (VALUES
    (1, 1, 1, 2, '1.50'),
    (2, 1, 2, 1, '4.00'),
    (3, 3, 3, 5, '0.50'),
    (4, 4, 2, 1, '2.50'),
    (5, 7, 1, 3, '1.25'),
    (6, 5, 3, 1, '4.00')
) AS t (id, order_id, product_id, quantity, unit_price);
