---
name: transform
description: Use this to author and change a dbt project: write or refactor dbt model SQL from staging to marts, add tests and docs in schema.yml, manage dependencies, and define or update the semantic layer (dbt semantic models / MetricFlow: entities, dimensions, measures, metrics). Trigger it for requests like "build a staging model for this table", "refactor this model", "add tests to this model", "create a mart for X", "define a revenue metric", or "add a dimension to this entity". Every change is a reviewable diff to the dbt project; any warehouse build is dev-target only, gated, and cost-surfaced first. It also offers a free Viz preview of the semantic layer. Do not use it to explore or profile a warehouse (use explore) or to detect drift and reconcile a project that has fallen out of sync (use maintain).
---

# Transform

Author and refactor the dbt project: both the SQL transformations (staging to
marts, tests, docs) and the semantic layer on top (entities, dimensions,
measures, metrics). Both are the same job, writing reviewable diffs to the dbt
project, which is the source of truth. This is the building half of the loop. It
writes only to the repo, as reviewable diffs, and runs against a dev target only.

## How to drive it

```bash
uv run "${CLAUDE_SKILL_DIR}/scripts/run.py" <subcommand> [flags]
```

### dbt SQL models

- `transform plan "<intent>"` returns proposed dbt edits as diffs. Nothing is
  applied yet.
- `transform apply <plan-id>` writes the diffs into the dbt project. The result is
  still a reviewable git diff for the user.
- `transform build --target dev` runs a dbt build against a dev target. The
  engine surfaces a cost preflight first and runs only with `--confirm` and a
  budget.

### The semantic layer

- `semantic define ...` and `semantic update ...` author and evolve the dbt
  semantic models (entities, dimensions, measures, metrics) as diffs to the dbt
  project. This is dex's unique layer and Viz's input.
- `emit dbt` writes or refreshes the dbt semantic YAML from those edits.
- `viz preview` emits the dbt semantic model to the free Viz preview (the taste;
  the governed serve loop is the commercial product).

Export to other formats (OSI and others) is a future capability and is not emitted
in v1.

## Guardrails (enforced in the engine, not here)

- Writes confined to the repo. dex never writes to source warehouse data.
- Dev-target only. Prod-target execution is never initiated by dex.
- Cost surfaced before any spend. A build that would spend requires explicit
  confirmation and a session budget.
- Propose, don't impose. Human edits to dbt (SQL and semantic YAML) are
  authoritative; on conflict the engine surfaces a diff and asks rather than
  overwriting.
- PII flags propagate from the cache into emitted dbt (model and column `meta`),
  never example values.
