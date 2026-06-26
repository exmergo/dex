---
name: model
description: Use this to define and maintain a dbt semantic layer (dbt semantic models / MetricFlow): entities, dimensions, measures, and metrics, kept coherent and in sync as the warehouse changes. Trigger it for requests like "define a revenue metric", "add a dimension to this entity", "maintain the semantic model", "build dbt semantic models", or "preview this model in Viz". It writes dbt semantic YAML into your dbt project and offers a free Viz preview. Do not use it to author plain dbt transformations (use transform) or to explore and profile a warehouse (use explore).
---

# Model

Define and maintain the semantic layer: entities, dimensions, measures, and
metrics. This is dex's unique layer and Viz's input. The semantic model lives in
your dbt project as dbt semantic models (MetricFlow YAML); dbt is the source of
truth, and dex edits it as reviewable diffs.

## How to drive it

```bash
uv run "${CLAUDE_SKILL_DIR}/scripts/run.py" <subcommand> [flags]
```

- `model define ...` and `model maintain ...` edit the dbt semantic models
  (entities, dimensions, measures, metrics) as diffs to the dbt project.
- `emit dbt` writes or refreshes the dbt semantic YAML from those edits.
- `viz preview` emits the dbt semantic model to the free Viz preview (the taste;
  the governed serve loop is the commercial product).

Export to other formats (OSI and others) is a future capability and is not emitted
in v1.

## Guardrails (enforced in the engine, not here)

- One source of truth: the dbt project. dex holds no competing copy.
- Propose, don't impose. Human edits to dbt semantic models are authoritative;
  conflicts surface as a diff, never a silent overwrite.
- PII flags propagate from the cache into emitted dbt (model and column `meta`),
  never example values.
