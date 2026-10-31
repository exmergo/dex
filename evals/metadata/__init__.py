"""The metadata-quality eval (#492): does an agent declare what its models mean?

Neither benchmark Dex runs against grades metadata: data-eng-bench and ADE-bench
compare the data a model returns, never its description, its ``meta`` or its
semantic definitions. This suite does. It hands an agent a small DuckDB fixture
project with a task phrased the way users ask ("build a mart for X", "this model
returns the wrong numbers, fix it"), then scores what it left behind:

- **build gate**: a cold ``dbt parse`` and ``dbt build`` of the result on a
  freshly generated warehouse, and no model with two YAML entries. A failure
  fails the task outright, because a declaration that breaks the parse costs more
  than every declaration it adds.
- **completeness**: each model the task asks for has a description, a grain, a
  population, a role for every column, aggregation, additivity and unit for every
  measure, and at least one assumption with an evidence tier.
- **correctness**: each declared field the fixture has a known right answer for
  (grain, population filters, additivity, unit, aggregation, the evidence tier
  the convention calls for), compared field by field, plus SQL checks on the
  built data.

Everything here is deterministic. Every field is decided by a comparison, so no
LLM judge is used; description prose is carried into the report for a human to
read, and never scored.

Unlike the Tier 2 core in ``evals/`` (stdlib only), this suite needs DuckDB,
PyYAML and dbt, so it runs in the engine's own environment, whose ``[duckdb]``
extra pins all three: the build gate then runs the dbt ``transform build`` runs.

    uv run --project packages/dex-core --extra duckdb \
        python -m evals metadata --reference
"""
