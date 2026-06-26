---
name: transform
description: Use this to author, refactor, test, and document dbt models, from staging to marts: write or change model SQL, add or update tests in schema.yml, manage dependencies, and validate against a dev target. Trigger it for requests like "build a staging model for this table", "refactor this model", "add tests to this model", "create a mart for X", or "clean up this transformation". Every change is a reviewable diff to the dbt project; any warehouse build is dev-target only, gated, and cost-surfaced first. Do not use it to explore or profile a warehouse (use explore) or to define metrics and the semantic layer (use model).
---

# Transform

Author and refactor dbt models, tests, and docs. This closes the building half of
the loop. It writes only to the repo, as reviewable diffs, and runs against a dev
target only.

## How to drive it

```bash
uv run "${CLAUDE_SKILL_DIR}/scripts/run.py" <subcommand> [flags]
```

- `transform plan "<intent>"` returns proposed dbt edits as diffs. Nothing is
  applied yet.
- `transform apply <plan-id>` writes the diffs into the dbt project. The result is
  still a reviewable git diff for the user.
- `transform build --target dev` runs a dbt build against a dev target. The
  engine surfaces a cost preflight first and runs only with `--confirm` and a
  budget.

## Guardrails (enforced in the engine, not here)

- Writes confined to the repo. dex never writes to source warehouse data.
- Dev-target only. Prod-target execution is never initiated by dex.
- Cost surfaced before any spend. A build that would spend requires explicit
  confirmation and a session budget.
- Propose, don't impose. Human edits to dbt are authoritative; on conflict the
  engine surfaces a diff and asks rather than overwriting.
