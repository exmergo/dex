# Semantic-model-maintenance benchmark (own the gap)

Semantic-model maintenance is the least-benchmarked of the three jobs, and it is
confirmed uncovered by ADE-bench and Spider 2.0 (v8 §14.5). This is dex's unique
layer, so dex defines its own measurement.

Plan (Phase 5): contribute semantic-model-maintenance tasks to ADE-bench first
(it accepts new tasks as a directory with a `task.yaml`, setup and solution
scripts, dbt tests, and answer-key seeds), then extract a standalone benchmark
once the task set is rich. It grades, against a warehouse plus a dbt project, how
well an agent maintains a correct dbt semantic model (MetricFlow) under drift:
metric correctness, valid semantic YAML, and reconciliation behavior. Because the
semantic model is plain dbt, tasks grade with dbt's own tooling.

A public name for the standalone benchmark is deliberately deferred: it gets named
properly (with domain and trademark diligence) only if and when it becomes a
standalone artifact worth marketing.
