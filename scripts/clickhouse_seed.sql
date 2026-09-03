-- Seed for the dex ClickHouse dogfood/integration database.
--
-- Applied by scripts/setup_clickhouse_dev.sh, which is also what the clickhouse
-- job in .github/workflows/integration.yml runs: unlike the Postgres pair, there
-- is exactly one seeding path in the repo, so the local dogfood target and the
-- CI target cannot drift apart. Run through `clickhouse-client --multiquery`;
-- the HTTP interface refuses multi-statement bodies, so this file cannot be
-- POSTed.
--
-- ClickHouse namespaces are two-part (database.table). Both database names are
-- clickhouse-client query parameters so this exact data fixture is used by the
-- local container and the dedicated Cloud CI service. Each environment creates
-- its own users and grants after applying the shared data fixture.
--
-- The shape is a small realistic analytical schema that exercises every
-- explore/transform/maintain surface, plus five ClickHouse-specific hazards
-- that only a live server can prove:
--   - customers: PII columns (email, phone, names, address) for flag-not-surface,
--     with Nullable(String) and LowCardinality(Nullable(String)) spellings so the
--     type unwrapper is exercised in both nesting orders
--   - products: Enum8, Decimal, Array(String) and a JSON-in-String column
--   - orders -> customers, order_items -> orders, payments -> orders: inference
--     targets (ClickHouse has no foreign keys at all, so every relationship dex
--     finds here is name-and-shape inference, never a declaration)
--   - order_items.product_id: 40 rows deliberately reference products that do
--     not exist, so the shared LEFT JOIN overlap probe has real orphans to find.
--     With ClickHouse's default join_use_nulls=0 an unmatched row yields the type
--     default (0) instead of NULL and the probe reports zero: this table is what
--     proves the adapter sets join_use_nulls=1.
--   - events.occurred_at: a deliberate 3-day hole, so temporal continuity has a
--     largest_gap to report. LAG() does not exist in ClickHouse and the naive
--     lagInFrame rewrite silently returns the default, so a clean report here
--     means the window frame is wrong.
--   - events.recorded_at: DateTime, whose spelling makes the shared
--     is_date_only_type substring check skip hour granularity unless fixed
--   - order_events_raw: ReplacingMergeTree holding duplicates pending merge, the
--     shape that makes a key_lost_uniqueness finding a false alarm without a note
--   - signups: an empty table (volume drift, zero-row profiling)
--   - v_order_totals: a view (no stored rows, NULL total_rows in system.tables)
--   - dbt_dev: the empty dev database dbt builds into (never a source)

DROP DATABASE IF EXISTS {app_database:Identifier};
DROP DATABASE IF EXISTS {dev_database:Identifier};

CREATE DATABASE {app_database:Identifier};
CREATE DATABASE {dev_database:Identifier};

CREATE TABLE {app_database:Identifier}.customers
(
    id         UInt64,
    email      String,
    first_name String,
    last_name  String,
    phone      Nullable(String),
    address    String,
    city       LowCardinality(Nullable(String)),
    country    LowCardinality(String),
    created_at DateTime
)
ENGINE = MergeTree
ORDER BY id;

INSERT INTO {app_database:Identifier}.customers
SELECT
    number + 1                                              AS id,
    format('user{}@example.com', toString(number + 1))      AS email,
    format('First{}', toString(number + 1))                 AS first_name,
    format('Last{}', toString(number + 1))                  AS last_name,
    if((number + 1) % 7 = 0, NULL,
       format('+1-555-{}', toString(1000 + number + 1)))    AS phone,
    format('{} Main Street', toString(number + 1))          AS address,
    ['Amsterdam', 'Berlin', 'Lisbon', 'Milan', 'Porto'][1 + (number % 5)] AS city,
    ['NL', 'DE', 'PT', 'IT', 'PT'][1 + (number % 5)]        AS country,
    now() - toIntervalDay(number % 400)                     AS created_at
FROM numbers(500);

CREATE TABLE {app_database:Identifier}.products
(
    id       UInt64,
    name     String,
    category Enum8('electronics' = 1, 'grocery' = 2, 'apparel' = 3, 'home' = 4),
    price    Decimal(10, 2),
    tags     Array(String),
    attrs    String
)
ENGINE = MergeTree
ORDER BY id;

INSERT INTO {app_database:Identifier}.products
SELECT
    number + 1                                        AS id,
    format('Product {}', toString(number + 1))        AS name,
    toUInt8(1 + (number % 4))                         AS category,
    toDecimal64(1 + (number % 200) + 0.99, 2)         AS price,
    [format('tag{}', toString(number % 10)),
     format('tag{}', toString(number % 3))]           AS tags,
    concat('{"weight_g":', toString((number + 1) * 10),
           ',"in_stock":', if(number % 2 = 0, 'true', 'false'), '}') AS attrs
FROM numbers(80);

CREATE TABLE {app_database:Identifier}.orders
(
    id          UInt64,
    customer_id UInt64,
    status      Enum8('pending' = 1, 'paid' = 2, 'shipped' = 3, 'cancelled' = 4),
    total       Decimal(12, 2),
    ordered_at  DateTime
)
ENGINE = MergeTree
ORDER BY id;

INSERT INTO {app_database:Identifier}.orders
SELECT
    number + 1                                   AS id,
    1 + ((number * 7) % 500)                     AS customer_id,
    toUInt8(1 + (number % 4))                    AS status,
    toDecimal64(0, 2)                            AS total,
    now() - toIntervalDay(number % 365)          AS ordered_at
FROM numbers(2000);

-- product_id carries no declaration (ClickHouse has none to carry) and 40 of the
-- 5000 rows point at products 900-939, which do not exist. Relationship
-- verification and maintain grain's join-fanout half both read this.
CREATE TABLE {app_database:Identifier}.order_items
(
    id         UInt64,
    order_id   UInt64,
    product_id UInt64,
    quantity   UInt8,
    unit_price Decimal(10, 2)
)
ENGINE = MergeTree
ORDER BY id;

INSERT INTO {app_database:Identifier}.order_items
SELECT
    number + 1                                        AS id,
    1 + ((number * 3) % 2000)                         AS order_id,
    if(number < 40, 900 + number, 1 + ((number * 11) % 80)) AS product_id,
    toUInt8(1 + (number % 5))                         AS quantity,
    toDecimal64(1 + (number % 150) + 0.49, 2)         AS unit_price
FROM numbers(5000);

CREATE TABLE {app_database:Identifier}.payments
(
    id          UInt64,
    order_id    UInt64,
    method      LowCardinality(String),
    checksum    FixedString(32),
    paid_at     DateTime
)
ENGINE = MergeTree
ORDER BY id;

INSERT INTO {app_database:Identifier}.payments
SELECT
    rowNumberInAllBlocks() + 1                        AS id,
    o.id                                              AS order_id,
    ['card', 'transfer', 'wallet'][1 + (o.id % 3)]    AS method,
    toFixedString(hex(MD5(toString(o.id))), 32)       AS checksum,
    o.ordered_at + toIntervalHour(1)                  AS paid_at
FROM {app_database:Identifier}.orders AS o
WHERE o.status IN ('paid', 'shipped');

-- Query parameters are substituted while parsing the DDL statement, but a
-- parameter embedded in a stored view body is retained and later resolves as
-- empty. Pin the current database for this statement and keep the stored body
-- unqualified so the same parameterized fixture works locally and in Cloud.
USE {app_database:Identifier};
CREATE VIEW v_order_totals AS
SELECT
    o.id          AS order_id,
    o.customer_id AS customer_id,
    o.status      AS status,
    sum(i.quantity * i.unit_price) AS computed_total
FROM orders AS o
INNER JOIN order_items AS i ON i.order_id = o.id
GROUP BY o.id, o.customer_id, o.status;

-- Empty on purpose: volume drift's emptied-table case, and a zero-row profile.
CREATE TABLE {app_database:Identifier}.signups
(
    id         UInt64,
    email      String,
    signed_up  DateTime
)
ENGINE = MergeTree
ORDER BY id;

-- ReplacingMergeTree holds every version until a merge collapses them, so
-- order_id is genuinely non-unique in the stored data while being the declared
-- grain. Two of the 300 ids are double-loaded. A key_lost_uniqueness finding
-- here is engine behavior, not a defect, which is what table_notes has to say.
CREATE TABLE {app_database:Identifier}.order_events_raw
(
    order_id   UInt64,
    state      LowCardinality(String),
    version    UInt32,
    updated_at DateTime
)
ENGINE = ReplacingMergeTree(version)
ORDER BY order_id;

INSERT INTO {app_database:Identifier}.order_events_raw
SELECT
    number + 1                                  AS order_id,
    ['new', 'picked', 'shipped'][1 + (number % 3)] AS state,
    1                                           AS version,
    now() - toIntervalHour(number % 48)         AS updated_at
FROM numbers(300);

INSERT INTO {app_database:Identifier}.order_events_raw VALUES
    (7,  'shipped', 2, now()),
    (42, 'shipped', 2, now());

-- The big table: sampling threshold, statement caps, and the temporal-continuity
-- surface. occurred_at spans 90 days with days 30-32 removed, so span,
-- distinct_periods, missing_periods and largest_gap all have something to
-- report. recorded_at is a DateTime, whose spelling is what makes the shared
-- date-only check skip hour granularity unless the substring test excludes it.
CREATE TABLE {app_database:Identifier}.events
(
    id          UInt64,
    customer_id UInt64,
    event_type  LowCardinality(String),
    session_id  String,
    payload     String,
    occurred_at Date,
    recorded_at DateTime
)
ENGINE = MergeTree
ORDER BY (occurred_at, id);

INSERT INTO {app_database:Identifier}.events
SELECT
    number + 1                                             AS id,
    1 + (number % 500)                                     AS customer_id,
    ['page_view', 'add_to_cart', 'checkout', 'search', 'login'][1 + (number % 5)] AS event_type,
    lower(hex(MD5(toString(number % 10000))))              AS session_id,
    concat('{"session":', toString(number % 10000),
           ',"step":', toString(number % 7), '}')          AS payload,
    toDate(now()) - toIntervalDay(number % 90)             AS occurred_at,
    now() - toIntervalHour(number % 2160)                  AS recorded_at
FROM numbers(100000)
WHERE (number % 90) NOT IN (30, 31, 32);

OPTIMIZE TABLE {app_database:Identifier}.customers FINAL;
