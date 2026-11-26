"""Where the Elementary package's schema holds raw source values (#1310).

The Elementary dbt package writes its tables into a schema of its own (dbt names
it ``<target schema>_elementary`` under the ``+schema: elementary`` its install
instructions set, or ``elementary`` where a project overrides the naming). Most
of what it writes is metadata about a run: names, statuses, timings, counts.
Three columns are not, and nothing in the column itself says so:

- ``test_result_rows.result_row``: a failing row of a test, serialized as text;
- ``result_rows`` on ``elementary_test_results`` and the ``alerts_*`` views
  over it: the same rows, collected per test;
- ``dimension_value`` on ``data_monitoring_metrics`` and the
  ``metrics_anomaly_score`` view over it: the raw values of the dimension a
  ``dimension_anomalies`` test groups by.

Three more text columns can quote a value, though they do not hold rows:
``test_results_description`` (a test's result message), ``test_results_query``
(its compiled SQL, literals included) and ``test_params`` (its arguments, an
``accepted_values`` list among them). A profile reads none of their values
(#1471), so no minimum, maximum or value domain of them is computed or cached.
The query firewall does not hold them: projecting one still follows the cache's
PII flags, as any other column does.

A relation in that schema that is not one of the package's models can hold
more: with ``tests_use_temp_tables`` its test materialization writes a test's
whole result to a table there (``macros/edr/materializations/test/test.sql``).

This module says which relations and columns those are, from the relation's
identifier and the column's name alone, so the query firewall and the profiler
can hold them whatever the exploration cache flagged. A ``pii_overrides`` entry
does not clear them: they are protected for where their values come from, not
for what their names look like, and a reviewer of the column cannot review
every row a later test failure will write into it. ``dimension_value`` is the
exception the issue asks for: it carries the flag of the columns it came from,
read where the firewall resolves it (``query_firewall``).

The schema is recognised by its name, ``elementary`` or ending ``_elementary``,
as dbt renders the package's documented setup. A project that gives the package
another schema name is not recognised as one here; its row-value columns are
still held, since those are recognised by model and column name in any schema.
"""

from __future__ import annotations

from collections.abc import Iterable

#: The package's own models, every one released from 0.15.2 to 0.26.1 (read
#: from the ``models/`` directory of each install; 0.26.1 adds ``dbt_groups``).
#: A relation of one of these names is documented: its columns are what the
#: package declares, and only the ones named below carry row values.
DOCUMENTED_MODELS = frozenset(
    {
        "alerts_anomaly_detection",
        "alerts_dbt_models",
        "alerts_dbt_source_freshness",
        "alerts_dbt_tests",
        "alerts_schema_changes",
        "anomaly_threshold_sensitivity",
        "data_monitoring_metrics",
        "dbt_artifacts_hashes",
        "dbt_columns",
        "dbt_exposures",
        "dbt_groups",
        "dbt_invocations",
        "dbt_metrics",
        "dbt_models",
        "dbt_run_results",
        "dbt_seeds",
        "dbt_snapshots",
        "dbt_source_freshness_results",
        "dbt_sources",
        "dbt_tests",
        "elementary_test_results",
        "job_run_results",
        "metadata",
        "metrics_anomaly_score",
        "model_run_results",
        "monitors_runs",
        "schema_columns_snapshot",
        "seed_run_results",
        "snapshot_run_results",
        "test_result_rows",
    }
)

#: Columns of the documented models that hold failing rows, serialized.
ROW_VALUE_COLUMNS = frozenset({"result_row", "result_rows"})

#: The column of the documented models that holds a monitored dimension's raw
#: values, and the one that names which dimension each row's value belongs to.
DIMENSION_VALUE_COLUMN = "dimension_value"

#: Columns of the documented models whose text can quote a value: a result
#: message, compiled SQL, a test's arguments. A profile withholds their values;
#: the firewall does not hold them.
QUOTING_COLUMNS = frozenset(
    {"test_results_description", "test_results_query", "test_params"}
)

SCHEMA_NAME = "elementary"


def _parts(identifier: str) -> list[str]:
    return [p.strip('"`[]').lower() for p in identifier.split(".")]


def table_name(identifier: str) -> str:
    """The relation's own name, lowered, without its namespace or quoting."""

    return _parts(identifier)[-1]


def in_elementary_schema(identifier: str) -> bool:
    """Whether the relation sits in a schema named for the package.

    Any part between the database and the relation's name counts, not only the
    one before the last: a connector that joins the parts unquoted (DuckDB)
    turns a table named ``tmp.rows`` into ``dev.main_elementary.tmp.rows``, and
    reading only ``tmp`` as the schema would let it out.
    """

    parts = _parts(identifier)
    if len(parts) < 2:
        return False
    namespace = parts[1:-1] if len(parts) > 2 else parts[:-1]
    return any(
        part == SCHEMA_NAME or part.endswith(f"_{SCHEMA_NAME}") for part in namespace
    )


def documented(identifier: str) -> bool:
    """Whether the relation is one of the package's own models, by name."""

    return table_name(identifier) in DOCUMENTED_MODELS


def protected_relation(identifier: str) -> bool:
    """A relation in the package's schema that is none of its models.

    Such a relation can hold a test's whole result (``tests_use_temp_tables``),
    so it is held as a whole: no column of it is read, not even counted.
    """

    return in_elementary_schema(identifier) and not documented(identifier)


def row_value_column(identifier: str, column: str) -> bool:
    """Whether the column holds failing rows of a test, serialized."""

    return documented(identifier) and column.lower() in ROW_VALUE_COLUMNS


def dimension_value_column(identifier: str, column: str) -> bool:
    """Whether the column holds raw values of a monitored dimension."""

    return documented(identifier) and column.lower() == DIMENSION_VALUE_COLUMN


def withheld_columns(identifier: str, columns: Iterable[str]) -> set[str]:
    """The columns whose values a profile of this relation never reads.

    Every column of a protected relation; on a documented model, the row-value
    columns, ``dimension_value`` and the columns that can quote a value
    (:data:`QUOTING_COLUMNS`). Their minimum, maximum and value domain are
    values, so none is computed or cached.
    """

    names = list(columns)
    if protected_relation(identifier):
        return set(names)
    return {
        name
        for name in names
        if row_value_column(identifier, name)
        or dimension_value_column(identifier, name)
        or (documented(identifier) and name.lower() in QUOTING_COLUMNS)
    }
