---
name: transform
description: Use this to author and change a dbt project: bootstrap a new dbt project in a repo that has none (`transform init`), write or refactor dbt model SQL from staging to marts, add tests and docs in schema.yml, manage dependencies, and define or update the semantic layer (dbt semantic models / MetricFlow: entities, dimensions, measures, metrics). Trigger it for requests like "set up a dbt project in this repo", "build a staging model for this table", "refactor this model", "add tests to this model", "create a mart for X", "define a revenue metric", or "add a dimension to this entity". Every change is a reviewable diff to the dbt project; any warehouse build is dev-target only, gated, and cost-surfaced first. Do not use it to explore or profile a warehouse (use explore) or to detect drift and reconcile a project that has fallen out of sync (use maintain).
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

You author the dbt file content; the engine validates it, computes the diffs,
and stores the proposal as a plan. Hand content over with `--edits-file <path>`
(or `-` to read stdin), a JSON payload:

```json
{"edits": [
  {"path": "models/staging/stg_orders.sql", "kind": "model_sql", "content": "..."},
  {"path": "models/staging/stg_orders.yml", "kind": "schema_yml", "content": "..."}
]}
```

`kind` is `model_sql`, `schema_yml`, or `semantic_yml` (optional on
`semantic define|update`, which imply it). Model SQL must be a single read-only
SELECT once its jinja is stripped; semantic YAML is validated against
MetricFlow's schemas.

### Bootstrapping a project

If no dbt project exists in the repo, offer `transform init` before anything
else: `transform plan` needs a project to edit. Ask the user for the project
name and **confirm the connector with them**, then run:

```bash
uv run "${CLAUDE_SKILL_DIR}/scripts/run.py" transform init "<name>" --connector <c>
```

The engine renders the whole skeleton (`dbt_project.yml`, `models/staging/` and
`models/marts/`, a `profiles.yml` with a single `dev` target and no secrets) and
records `connector`, `dbt_project_dir`, and `dbt_target: dev` in
`.dex/config.yml`; do not hand-write these files yourself. Init never assumes a
connector: it errors rather than defaulting, so always pass the user's confirmed
choice (a `connector:` already committed in `.dex/config.yml` also counts).
DuckDB needs a warehouse path (`--path`, or the `duckdb.path` config); it is the
supported connector today, and the cloud connectors return an actionable
not-yet-supported error. Init refuses if any dbt project already exists.

### dbt SQL models

- `transform plan "<intent>" --edits-file <path|->` validates the edits and
  returns them as diffs with a plan id. Nothing is applied yet. Add
  `--scaffold <table>` (repeatable) to generate a staging skeleton
  (`stg_<table>.sql` plus per-model YAML with key tests and PII meta) from the
  `.dex/` cache instead of, or on top of, hand-authored edits.
- `transform apply <plan-id>` writes the plan into the dbt project. The result is
  still a reviewable git diff for the user. If a human edited a file after the
  plan was made, nothing is written: the divergence comes back as diffs with
  `needs_confirmation`, and you should re-plan against current state (or, only
  when the user says so, re-run with `--confirm`).
- `transform build --target dev` runs `dbt build` against a dev target. The
  engine surfaces a cost preflight first and runs only with `--confirm` (plus a
  `--budget` on billed connectors). Production-looking targets are refused
  outright; `--confirm` cannot override that.

### The semantic layer

- `semantic define ... --edits-file <path|->` and `semantic update ...` author
  and evolve the dbt semantic models (entities, dimensions, measures, metrics)
  as plans. `define` refuses names that already exist (use `update`); `update`
  refuses names that do not (use `define`).
- `emit dbt [plan-id]` writes the semantic plan's YAML into the dbt project (the
  latest unapplied semantic plan when no id is given).
- dbt cannot parse semantic models in a project without a MetricFlow **time
  spine**; the engine warns when one is missing. Author it like any other model
  (a day-grain date model plus YAML with a `time_spine:` config) in the same or
  a separate plan.
- `viz preview` is not yet implemented (it returns `not_implemented`); the Viz
  integration arrives later.

Export to other formats (OSI and others) is a future capability and is not emitted
in v1.

## Guardrails (enforced in the engine, not here)

- Writes confined to the repo, and within it to the dbt project's model paths.
  dex never writes to source warehouse data.
- Dev-target only. Prod-target execution is never initiated by dex.
- Cost surfaced before any spend. A build that would spend requires explicit
  confirmation and a session budget.
- Propose, don't impose. Human edits to dbt (SQL and semantic YAML) are
  authoritative; on conflict the engine surfaces a diff and asks rather than
  overwriting.
- PII flags propagate from the cache into emitted dbt (model and column `meta`),
  never example values.
