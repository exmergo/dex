"""Per-dialect golden SQL for a SLayer metric query (#683).

One query, rendered through dex's own translation and SLayer's dry run, for
every connector dex lets SLayer render for. The goldens are what decided that
table: a connector joins once its golden has been read for that dialect and
found to mean what the query asks, and is refused by name otherwise.

- **DuckDB, BigQuery, Snowflake** were in from the start (ADR-0011); their
  goldens pin what was read.
- **Postgres and Redshift** joined on their goldens: ``DATE_TRUNC('MONTH', ts)``
  buckets a timestamp at every grain, ``DOUBLE PRECISION`` is the cast both
  accept, and ``NULLS LAST`` keeps their default descending order (nulls first)
  from moving the empty bucket to the top.
- **Databricks** stayed out. SLayer buckets a Databricks timestamp with Spark's
  ``TRUNC``, which knows only year, quarter, month and week and answers NULL for
  any finer grain, and renders a median with ``PERCENTILE_APPROX``. Both run and
  both return the wrong numbers, which is the one failure dex will not ship.
  :func:`test_databricks_stays_out_while_slayer_buckets_with_spark_s_trunc` is
  the tripwire that says when to read it again.

A golden is compared exactly, so a SLayer release that changes what it emits
fails here first, and the change is read rather than shipped. The statement is
then held to dex's own guards in the connector's dialect: the SELECT-only
assertion, and the relation pre-check's reading of what it reads.

Needs the real `[slayer]` extra, so it skips without it; the `tests` CI job
is where it runs.
"""

# The list goldens are SLayer's output verbatim, and some of its lines are long.
# ruff: noqa: E501

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

import pytest
import yaml

from exmergo_dex_core.slayer import dependency
from exmergo_dex_core.slayer.dependency import SLAYER_VERSION

if dependency.installed_version() != SLAYER_VERSION:
    pytest.skip(
        f"the [slayer] extra (SLayer {SLAYER_VERSION}) is not installed",
        allow_module_level=True,
    )

from exmergo_dex_core.adapters import get_dialect
from exmergo_dex_core.cache import match_identifier
from exmergo_dex_core.explore.semantic import SemanticBackendError
from exmergo_dex_core.explore.semantic.model import SemanticQuery
from exmergo_dex_core.guards.sql_guard import assert_select_only, referenced_relations
from exmergo_dex_core.slayer import render
from exmergo_dex_core.slayer.catalog import semantic_catalog
from exmergo_dex_core.slayer.loader import FORMAT_VERSION, validate_model_contents
from exmergo_dex_core.slayer.render import SLAYER_TYPES, SlayerRenderer
from exmergo_dex_core.slayer.translate import GRAINS, to_slayer_query

NOW = datetime(2026, 1, 15, 12)

#: The relation each connector's model sits on, spelt the way a model author on
#: that warehouse would write it: a hyphenated BigQuery project, a lowercase
#: Snowflake name that has to stay unquoted to resolve case-insensitively.
TABLES = {
    "duckdb": "shop.main.orders",
    "bigquery": "dex-ci-project.shop.orders",
    "snowflake": "shop.public.orders",
    "postgres": "shop.public.orders",
    "redshift": "dev.public.orders",
}

#: Two saved measures grouped by a column, bucketed by month, filtered on top of
#: the model's own filter, ordered by a measure and limited: one query reaching
#: every clause the v1 translation emits.
QUERY = SemanticQuery(
    metrics=["orders.revenue", "orders.order_count"],
    group_by=["orders.status"],
    grain="month",
    where=["status = 'paid'"],
    order_by=["-orders.revenue"],
    limit=10,
)

KEYS = ["orders.status", "orders.ordered_at", "orders.revenue", "orders.order_count"]

GOLDENS = {
    "duckdb": """\
SELECT
  orders.status AS "orders.status",
  DATE_TRUNC('MONTH', orders.ordered_at) AS "orders.ordered_at",
  CAST(SUM(orders.amount) AS DOUBLE) AS "orders.revenue",
  COUNT(*) AS "orders.order_count"
FROM (
  SELECT
    orders.*
  FROM shop.main.orders AS orders
  WHERE
    orders.status <> 'test'
) AS orders
WHERE
  orders.status = 'paid'
GROUP BY
  orders.status,
  DATE_TRUNC('MONTH', orders.ordered_at)
ORDER BY
  "orders.revenue" DESC
LIMIT 10""",
    # A dotted output column is an error on BigQuery, so every alias is
    # mangled to `___` and the result keys carry the decoding back.
    "bigquery": """\
SELECT
  orders.status AS `orders___status`,
  DATE_TRUNC(orders.ordered_at, MONTH) AS `orders___ordered_at`,
  CAST(SUM(orders.amount) AS FLOAT64) AS `orders___revenue`,
  COUNT(*) AS `orders___order_count`
FROM (
  SELECT
    orders.*
  FROM `dex-ci-project`.shop.orders AS orders
  WHERE
    orders.status <> 'test'
) AS orders
WHERE
  orders.status = 'paid'
GROUP BY
  `orders___status`,
  `orders___ordered_at`
ORDER BY
  `orders___revenue` DESC
LIMIT 10""",
    "snowflake": """\
SELECT
  orders.status AS "orders.status",
  DATE_TRUNC('MONTH', orders.ordered_at) AS "orders.ordered_at",
  CAST(SUM(orders.amount) AS DOUBLE) AS "orders.revenue",
  COUNT(*) AS "orders.order_count"
FROM (
  SELECT
    orders.*
  FROM shop.public.orders AS orders
  WHERE
    orders.status <> 'test'
) AS orders
WHERE
  orders.status = 'paid'
GROUP BY
  orders.status,
  DATE_TRUNC('MONTH', orders.ordered_at)
ORDER BY
  "orders.revenue" DESC NULLS LAST
LIMIT 10""",
    "postgres": """\
SELECT
  orders.status AS "orders.status",
  DATE_TRUNC('MONTH', orders.ordered_at) AS "orders.ordered_at",
  CAST(SUM(orders.amount) AS DOUBLE PRECISION) AS "orders.revenue",
  COUNT(*) AS "orders.order_count"
FROM (
  SELECT
    orders.*
  FROM shop.public.orders AS orders
  WHERE
    orders.status <> 'test'
) AS orders
WHERE
  orders.status = 'paid'
GROUP BY
  orders.status,
  DATE_TRUNC('MONTH', orders.ordered_at)
ORDER BY
  "orders.revenue" DESC NULLS LAST
LIMIT 10""",
    "redshift": """\
SELECT
  orders.status AS "orders.status",
  DATE_TRUNC('MONTH', orders.ordered_at) AS "orders.ordered_at",
  CAST(SUM(orders.amount) AS DOUBLE PRECISION) AS "orders.revenue",
  COUNT(*) AS "orders.order_count"
FROM (
  SELECT
    orders.*
  FROM dev.public.orders AS orders
  WHERE
    orders.status <> 'test'
) AS orders
WHERE
  orders.status = 'paid'
GROUP BY
  orders.status,
  DATE_TRUNC('MONTH', orders.ordered_at)
ORDER BY
  "orders.revenue" DESC NULLS LAST
LIMIT 10""",
}


def orders(table: str, **fields: Any) -> dict[str, Any]:
    return {
        "version": FORMAT_VERSION,
        "name": "orders",
        "sql_table": table,
        "data_source": "wh",
        "default_time_dimension": "ordered_at",
        "filters": ["status <> 'test'"],
        "columns": [
            {"name": "order_id", "type": "INT", "primary_key": True},
            {"name": "status", "type": "TEXT"},
            {"name": "amount", "type": "DOUBLE"},
            {"name": "ordered_at", "type": "TIMESTAMP"},
        ],
        "measures": [
            {"name": "revenue", "formula": "amount:sum"},
            {"name": "order_count", "formula": "*:count"},
        ],
        **fields,
    }


def rendered(connector: str, query: SemanticQuery = QUERY, **fields: Any):
    contents = {
        "models/wh/orders.yaml": yaml.safe_dump(orders(TABLES[connector], **fields))
    }
    result = validate_model_contents(contents)
    assert not result.errors, result.notes()
    view = semantic_catalog(result.models, connector=connector)
    renderer = SlayerRenderer(result.models, connector=connector, now=NOW)
    return renderer.render(to_slayer_query(query, view))


def test_every_supported_connector_has_a_golden():
    """A connector added to the table without one fails here, not in a warehouse."""

    assert set(GOLDENS) == set(SLAYER_TYPES)


@pytest.mark.parametrize("connector", sorted(GOLDENS))
def test_the_query_renders_to_its_golden(connector):
    assert rendered(connector).sql == GOLDENS[connector]


@pytest.mark.parametrize("connector", sorted(GOLDENS))
def test_dex_s_guards_read_the_golden_in_its_dialect(connector):
    """The spine parses what SLayer emitted with dex's own dialect for the
    connector: it must find it read-only, and find the one relation the model
    sits on, or the pre-check would refuse a statement that runs or wave
    through one that reads something else."""

    dialect = get_dialect(connector)
    sql = GOLDENS[connector]

    assert_select_only(sql, dialect=dialect)
    (relation,) = referenced_relations(sql, dialect=dialect)
    assert match_identifier(relation, [TABLES[connector]]) == [TABLES[connector]]


@pytest.mark.parametrize("connector", sorted(GOLDENS))
def test_the_result_keys_name_every_column_as_slayer_would(connector):
    """Whatever the statement had to emit, the names a caller reads are the
    ``model.column`` keys, the same on every connector."""

    result = rendered(connector)
    emitted = re.findall(r" AS [`\"]([^`\"]+)[`\"],?$", result.sql, flags=re.M)

    assert [result.result_keys.get(name, name) for name in emitted] == KEYS
    if connector == "bigquery":
        assert set(result.result_keys) == {key.replace(".", "___") for key in KEYS}
    else:
        assert result.result_keys == {}


@pytest.mark.parametrize("connector", sorted(GOLDENS))
@pytest.mark.parametrize("grain", GRAINS)
def test_no_supported_dialect_buckets_time_with_spark_s_trunc(connector, grain):
    """Every grain, not only the golden's month: a bare ``TRUNC`` is a date
    function that answers NULL below a week, and is what kept Databricks out."""

    sql = rendered(
        connector, SemanticQuery(metrics=["orders.revenue"], grain=grain)
    ).sql

    assert not re.search(r"(?<!DATE_)TRUNC\(", sql), sql


def test_databricks_stays_out_while_slayer_buckets_with_spark_s_trunc(monkeypatch):
    """The tripwire for the exclusion. When this fails, SLayer has changed how it
    renders Databricks: read a fresh golden, and if it means what it asks, move
    Databricks from ``EXCLUDED`` into ``SLAYER_TYPES``."""

    assert "databricks" in render.EXCLUDED
    monkeypatch.setitem(SLAYER_TYPES, "databricks", "databricks")
    monkeypatch.setitem(TABLES, "databricks", "shop.default.orders")

    day = rendered("databricks", SemanticQuery(metrics=["orders.revenue"], grain="day"))
    median = rendered(
        "databricks",
        SemanticQuery(metrics=["orders.median_amount"]),
        measures=[{"name": "median_amount", "formula": "amount:median"}],
    )

    assert "TRUNC(orders.ordered_at, 'DAY')" in day.sql
    assert "PERCENTILE_APPROX(orders.amount, 0.5)" in median.sql


# --- Snowflake weeks (#699) --------------------------------------------------------

#: SLayer renders both weeks through ``DATE_TRUNC('WEEK', ...)``, whose start day
#: is the session's ``WEEK_START``; dex pins it to the Monday SLayer means, so an
#: account set to Sunday buckets the same as one on the default.
SNOWFLAKE_WEEKS = {
    "week": """\
SELECT
  DATEADD(DAY, 1 - DAYOFWEEKISO(orders.ordered_at), DATE_TRUNC('DAY', orders.ordered_at)) AS "orders.ordered_at",
  COUNT(*) AS "orders.order_count"
FROM (
  SELECT
    orders.*
  FROM shop.public.orders AS orders
  WHERE
    orders.status <> 'test'
) AS orders
GROUP BY
  DATEADD(DAY, 1 - DAYOFWEEKISO(orders.ordered_at), DATE_TRUNC('DAY', orders.ordered_at))
ORDER BY
  "orders.ordered_at" ASC""",
    # The Monday week of the next day, moved back one: Sunday, once the Monday
    # it is built from no longer depends on the account.
    "week_sunday": """\
SELECT
  DATEADD(
    DAY,
    -1,
    DATEADD(
      DAY,
      1 - DAYOFWEEKISO(CAST(DATEADD(DAY, 1, orders.ordered_at) AS TIMESTAMP)),
      DATE_TRUNC('DAY', CAST(DATEADD(DAY, 1, orders.ordered_at) AS TIMESTAMP))
    )
  ) AS "orders.ordered_at",
  COUNT(*) AS "orders.order_count"
FROM (
  SELECT
    orders.*
  FROM shop.public.orders AS orders
  WHERE
    orders.status <> 'test'
) AS orders
GROUP BY
  DATEADD(
    DAY,
    -1,
    DATEADD(
      DAY,
      1 - DAYOFWEEKISO(CAST(DATEADD(DAY, 1, orders.ordered_at) AS TIMESTAMP)),
      DATE_TRUNC('DAY', CAST(DATEADD(DAY, 1, orders.ordered_at) AS TIMESTAMP))
    )
  )
ORDER BY
  "orders.ordered_at" ASC""",
}


@pytest.mark.parametrize("grain", sorted(SNOWFLAKE_WEEKS))
def test_a_snowflake_week_is_pinned_to_the_day_slayer_means(grain):
    query = SemanticQuery(metrics=["orders.order_count"], grain=grain)

    assert rendered("snowflake", query).sql == SNOWFLAKE_WEEKS[grain]


@pytest.mark.parametrize("grain", sorted(SNOWFLAKE_WEEKS))
def test_the_pinned_week_still_passes_dex_s_guards(grain):
    sql = SNOWFLAKE_WEEKS[grain]

    assert_select_only(sql, dialect="snowflake")
    (relation,) = referenced_relations(sql, dialect="snowflake")
    assert match_identifier(relation, [TABLES["snowflake"]]) == [TABLES["snowflake"]]


@pytest.mark.parametrize("grain", GRAINS)
def test_no_snowflake_grain_leaves_a_week_to_week_start(grain):
    """Every grain, not only the two weeks: a bare week truncation anywhere in a
    Snowflake statement would answer from the account's ``WEEK_START``."""

    sql = rendered(
        "snowflake", SemanticQuery(metrics=["orders.revenue"], grain=grain)
    ).sql

    assert not re.search(r"DATE_TRUNC\(\s*'WEEK'", sql, flags=re.I), sql


def test_a_statement_with_no_week_is_left_byte_for_byte():
    """The month golden is SLayer's own output: the pin regenerates nothing it
    does not have to."""

    assert render.pin_snowflake_weeks(GOLDENS["snowflake"]) == GOLDENS["snowflake"]


@pytest.mark.parametrize("connector", sorted(set(GOLDENS) - {"snowflake"}))
def test_only_snowflake_s_weeks_are_rewritten(connector):
    """Elsewhere a week is SLayer's own spelling: BigQuery anchors it, and
    DuckDB, Postgres and Redshift truncate to an ISO Monday unconditionally."""

    sql = rendered(
        connector, SemanticQuery(metrics=["orders.revenue"], grain="week")
    ).sql

    assert "DAYOFWEEKISO" not in sql


# --- Redshift identifier case (#699) -----------------------------------------------


def title(name: str) -> str:
    return "_".join(part.capitalize() for part in name.split("_"))


def mixed_case(table: str) -> dict[str, Any]:
    """The golden's model spelt the way a model author might: ``Orders.Status``."""

    return {
        **orders(table),
        "name": "Orders",
        "default_time_dimension": "Ordered_At",
        "filters": ["Status <> 'test'"],
        "columns": [
            {"name": "Order_Id", "type": "INT", "primary_key": True},
            {"name": "Status", "type": "TEXT"},
            {"name": "Amount", "type": "DOUBLE"},
            {"name": "Ordered_At", "type": "TIMESTAMP"},
        ],
        "measures": [
            {"name": "Revenue", "formula": "Amount:sum"},
            {"name": "Order_Count", "formula": "*:count"},
        ],
    }


MIXED_QUERY = SemanticQuery(
    metrics=["Orders.Revenue", "Orders.Order_Count"],
    group_by=["Orders.Status"],
    grain="month",
)
MIXED_KEYS = [
    "Orders.Status",
    "Orders.Ordered_At",
    "Orders.Revenue",
    "Orders.Order_Count",
]


def rendered_model(connector: str, spelt: dict[str, Any], query: SemanticQuery):
    path = f"models/wh/{spelt['name']}.yaml"
    result = validate_model_contents({path: yaml.safe_dump(spelt)})
    assert not result.errors, result.notes()
    view = semantic_catalog(result.models, connector=connector)
    renderer = SlayerRenderer(result.models, connector=connector, now=NOW)
    return renderer.render(to_slayer_query(query, view))


def test_a_mixed_case_model_on_redshift_reads_back_as_model_column():
    """Redshift returns ``"Orders.Status"`` as ``orders.status`` unless the
    cluster is case-sensitive, so the keys are what it returns, and a
    case-sensitive cluster's own spelling needs no entry at all."""

    result = rendered_model("redshift", mixed_case(TABLES["redshift"]), MIXED_QUERY)
    returned = [key.lower() for key in MIXED_KEYS]

    assert [result.result_keys.get(name, name) for name in returned] == MIXED_KEYS
    assert [result.result_keys.get(name, name) for name in MIXED_KEYS] == MIXED_KEYS


@pytest.mark.parametrize("connector", ["duckdb", "postgres", "snowflake"])
def test_a_quoted_name_that_keeps_its_case_needs_no_decoding(connector):
    result = rendered_model(connector, mixed_case(TABLES[connector]), MIXED_QUERY)

    assert result.result_keys == {}


def test_two_keys_redshift_would_fold_together_are_refused_by_name():
    spelt = orders(
        TABLES["redshift"],
        measures=[
            {"name": "revenue", "formula": "amount:sum"},
            {"name": "Revenue", "formula": "amount:max"},
        ],
    )
    query = SemanticQuery(metrics=["orders.revenue", "orders.Revenue"])

    with pytest.raises(SemanticBackendError) as caught:
        rendered_model("redshift", spelt, query)

    message = str(caught.value)
    assert "'orders.Revenue' and 'orders.revenue'" in message
    assert "differ only in case" in message


# --- list columns (#1246) ----------------------------------------------------------

#: The model above with a list column, `type: ARRAY` of TEXT.
LIST_COLUMNS = [
    *orders("")["columns"],
    {"name": "tags", "type": "ARRAY", "element_type": "TEXT"},
]

#: A measure grouped by a list's elements, filtered by membership: the element
#: walk (a lateral unnest), SLayer's `associate` handling (the measure counted
#: per element over each parent once, never the whole total on every row), and
#: `contains_any`, in each dialect with native lists.
LIST_QUERY = SemanticQuery(
    metrics=["orders.order_count"],
    group_by=["orders.tags.value"],
    where=["contains_any(tags, ['a', 'b'])"],
)

LIST_GOLDENS = {
    "duckdb": """\
WITH _base AS (
  SELECT
    tags.value AS "orders.tags.value"
  FROM (
    SELECT
      orders.*
    FROM shop.main.orders AS orders
    WHERE
      orders.status <> 'test'
  ) AS orders
  CROSS JOIN LATERAL (
    SELECT
      __slayer_u.value
    FROM UNNEST(orders.tags) AS __slayer_u(value)
    WHERE
      __slayer_u.value IS NOT NULL
  ) AS tags
  WHERE
    CASE
      WHEN orders.tags IS NULL OR ['a', 'b'] IS NULL
      THEN NULL
      ELSE LIST_HAS_ANY(
        [__slayer_e FOR __slayer_e IN orders.tags IF __slayer_e IS NOT NULL],
        [__slayer_e FOR __slayer_e IN ['a', 'b'] IF __slayer_e IS NOT NULL]
      )
    END
  GROUP BY
    tags.value
), _cm_orders___count AS (
  SELECT
    _base."orders.tags.value" AS "orders.tags.value",
    COUNT(*) AS "orders._count_host"
  FROM (
    SELECT
      tags.value AS "orders.tags.value"
    FROM (
      SELECT
        orders.*
      FROM shop.main.orders AS orders
      WHERE
        orders.status <> 'test'
    ) AS orders
    CROSS JOIN LATERAL (
      SELECT DISTINCT
        __slayer_u.value
      FROM UNNEST(orders.tags) AS __slayer_u(value)
      WHERE
        __slayer_u.value IS NOT NULL
    ) AS tags
    WHERE
      CASE
        WHEN orders.tags IS NULL OR ['a', 'b'] IS NULL
        THEN NULL
        ELSE LIST_HAS_ANY(
          [__slayer_e FOR __slayer_e IN orders.tags IF __slayer_e IS NOT NULL],
          [__slayer_e FOR __slayer_e IN ['a', 'b'] IF __slayer_e IS NOT NULL]
        )
      END
  ) AS _base
  GROUP BY
    _base."orders.tags.value"
)
SELECT
  _base."orders.tags.value",
  COALESCE(_cm_orders___count."orders._count_host", 0) AS "orders.order_count"
FROM _base
LEFT JOIN _cm_orders___count
  ON _base."orders.tags.value" IS NOT DISTINCT FROM _cm_orders___count."orders.tags.value"
ORDER BY
  _base."orders.tags.value" ASC""",
    "bigquery": """\
WITH _base AS (
  SELECT
    tags.value AS `orders___tags___value`
  FROM (
    SELECT
      orders.*
    FROM `dex-ci-project`.shop.orders AS orders
    WHERE
      orders.status <> 'test'
  ) AS orders
  CROSS JOIN UNNEST(ARRAY(
    SELECT AS STRUCT
      __slayer_e AS value
    FROM UNNEST(orders.tags) AS __slayer_e
    WHERE
      __slayer_e IS NOT NULL
  )) AS tags
  WHERE
    CASE
      WHEN orders.tags IS NULL OR ['a', 'b'] IS NULL
      THEN NULL
      ELSE EXISTS(
        SELECT
          __slayer_w
        FROM UNNEST(orders.tags) AS __slayer_w
        WHERE
          EXISTS(
            SELECT
              __slayer_e
            FROM UNNEST(['a', 'b']) AS __slayer_e
            WHERE
              __slayer_e = __slayer_w
          )
      )
    END
  GROUP BY
    tags.value
), _cm_orders___count AS (
  SELECT
    _base.`orders___tags___value` AS `orders___tags___value`,
    COUNT(*) AS `orders____count_host`
  FROM (
    SELECT
      tags.value AS `orders___tags___value`
    FROM (
      SELECT
        orders.*
      FROM `dex-ci-project`.shop.orders AS orders
      WHERE
        orders.status <> 'test'
    ) AS orders
    CROSS JOIN UNNEST(ARRAY(
      SELECT DISTINCT AS STRUCT
        __slayer_e AS value
      FROM UNNEST(orders.tags) AS __slayer_e
      WHERE
        __slayer_e IS NOT NULL
    )) AS tags
    WHERE
      CASE
        WHEN orders.tags IS NULL OR ['a', 'b'] IS NULL
        THEN NULL
        ELSE EXISTS(
          SELECT
            __slayer_w
          FROM UNNEST(orders.tags) AS __slayer_w
          WHERE
            EXISTS(
              SELECT
                __slayer_e
              FROM UNNEST(['a', 'b']) AS __slayer_e
              WHERE
                __slayer_e = __slayer_w
            )
        )
      END
  ) AS _base
  GROUP BY
    _base.`orders___tags___value`
)
SELECT
  _base.`orders___tags___value`,
  COALESCE(_cm_orders___count.`orders____count_host`, 0) AS `orders___order_count`
FROM _base
LEFT JOIN _cm_orders___count
  ON _base.`orders___tags___value` IS NOT DISTINCT FROM _cm_orders___count.`orders___tags___value`
ORDER BY
  _base.`orders___tags___value` ASC NULLS LAST""",
    "snowflake": """\
WITH _base AS (
  SELECT
    tags.value AS "orders.tags.value"
  FROM (
    SELECT
      orders.*
    FROM shop.public.orders AS orders
    WHERE
      orders.status <> 'test'
  ) AS orders
  CROSS JOIN TABLE(
    FLATTEN(INPUT => CAST(ARRAY_COMPACT(CAST(orders.tags AS ARRAY)) AS ARRAY(VARCHAR)))
  ) AS tags
  WHERE
    CASE
      WHEN orders.tags IS NULL OR ['a', 'b'] IS NULL
      THEN NULL
      ELSE ARRAYS_OVERLAP(
        ARRAY_COMPACT(CAST(orders.tags AS ARRAY)),
        ARRAY_COMPACT(CAST(['a', 'b'] AS ARRAY))
      )
    END
  GROUP BY
    tags.value
), _cm_orders___count AS (
  SELECT
    _base."orders.tags.value" AS "orders.tags.value",
    COUNT(*) AS "orders._count_host"
  FROM (
    SELECT
      tags.value AS "orders.tags.value"
    FROM (
      SELECT
        orders.*
      FROM shop.public.orders AS orders
      WHERE
        orders.status <> 'test'
    ) AS orders
    CROSS JOIN TABLE(
      FLATTEN(
        INPUT => CAST(ARRAY_DISTINCT(ARRAY_COMPACT(CAST(orders.tags AS ARRAY))) AS ARRAY(VARCHAR))
      )
    ) AS tags
    WHERE
      CASE
        WHEN orders.tags IS NULL OR ['a', 'b'] IS NULL
        THEN NULL
        ELSE ARRAYS_OVERLAP(
          ARRAY_COMPACT(CAST(orders.tags AS ARRAY)),
          ARRAY_COMPACT(CAST(['a', 'b'] AS ARRAY))
        )
      END
  ) AS _base
  GROUP BY
    _base."orders.tags.value"
)
SELECT
  _base."orders.tags.value",
  COALESCE(_cm_orders___count."orders._count_host", 0) AS "orders.order_count"
FROM _base
LEFT JOIN _cm_orders___count
  ON _base."orders.tags.value" IS NOT DISTINCT FROM _cm_orders___count."orders.tags.value"
ORDER BY
  _base."orders.tags.value" ASC""",
    "postgres": """\
WITH _base AS (
  SELECT
    tags.value AS "orders.tags.value"
  FROM (
    SELECT
      orders.*
    FROM shop.public.orders AS orders
    WHERE
      orders.status <> 'test'
  ) AS orders
  CROSS JOIN LATERAL (
    SELECT
      __slayer_u.value
    FROM UNNEST(orders.tags) AS __slayer_u(value)
    WHERE
      __slayer_u.value IS NOT NULL
  ) AS tags
  WHERE
    CASE
      WHEN CAST(orders.tags AS TEXT[]) IS NULL OR CAST(ARRAY['a', 'b'] AS TEXT[]) IS NULL
      THEN NULL
      ELSE CAST(orders.tags AS TEXT[]) && CAST(ARRAY['a', 'b'] AS TEXT[])
    END
  GROUP BY
    tags.value
), _cm_orders___count AS (
  SELECT
    _base."orders.tags.value" AS "orders.tags.value",
    COUNT(*) AS "orders._count_host"
  FROM (
    SELECT
      tags.value AS "orders.tags.value"
    FROM (
      SELECT
        orders.*
      FROM shop.public.orders AS orders
      WHERE
        orders.status <> 'test'
    ) AS orders
    CROSS JOIN LATERAL (
      SELECT DISTINCT
        __slayer_u.value
      FROM UNNEST(orders.tags) AS __slayer_u(value)
      WHERE
        __slayer_u.value IS NOT NULL
    ) AS tags
    WHERE
      CASE
        WHEN CAST(orders.tags AS TEXT[]) IS NULL OR CAST(ARRAY['a', 'b'] AS TEXT[]) IS NULL
        THEN NULL
        ELSE CAST(orders.tags AS TEXT[]) && CAST(ARRAY['a', 'b'] AS TEXT[])
      END
  ) AS _base
  GROUP BY
    _base."orders.tags.value"
)
SELECT
  _base."orders.tags.value",
  COALESCE(_cm_orders___count."orders._count_host", 0) AS "orders.order_count"
FROM _base
LEFT JOIN _cm_orders___count
  ON _base."orders.tags.value" IS NOT DISTINCT FROM _cm_orders___count."orders.tags.value"
ORDER BY
  _base."orders.tags.value" ASC""",
}


def test_every_connector_with_native_lists_has_a_list_golden():
    """Redshift has no native lists: SLayer refuses there, and dex refuses first."""

    assert set(LIST_GOLDENS) == set(SLAYER_TYPES) - {"redshift"}


@pytest.mark.parametrize("connector", sorted(LIST_GOLDENS))
def test_a_list_query_renders_to_its_golden(connector):
    result = rendered(connector, LIST_QUERY, columns=LIST_COLUMNS)

    assert result.sql == LIST_GOLDENS[connector]


@pytest.mark.parametrize("connector", sorted(LIST_GOLDENS))
def test_dex_s_guards_read_the_list_golden_in_its_dialect(connector):
    """The unnest is not a relation: the pre-check finds only the model's table."""

    dialect = get_dialect(connector)
    sql = LIST_GOLDENS[connector]

    assert_select_only(sql, dialect=dialect)
    (relation,) = referenced_relations(sql, dialect=dialect)
    assert match_identifier(relation, [TABLES[connector]]) == [TABLES[connector]]


@pytest.mark.parametrize("connector", sorted(LIST_GOLDENS))
def test_a_list_query_is_sent_associated(connector):
    """Each element counts its own parents: the measure is grouped by the element
    inside its own CTE and joined back on it, not computed once and broadcast."""

    sql = LIST_GOLDENS[connector]

    assert "_count_host" in sql
    assert "IS NOT DISTINCT FROM" in sql


def test_slayer_still_refuses_list_elements_on_redshift():
    """The tripwire for dex's own Redshift refusal: a SLayer release that reads
    lists on Redshift fails here, and its SQL is read before dex lets it through."""

    with pytest.raises(Exception, match="native lists"):
        rendered("redshift", LIST_QUERY, columns=LIST_COLUMNS)
