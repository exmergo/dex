# dbt as a first-class input (and the only write target)

The dbt project is the source of truth. dex maintains no parallel model: it loads
the project, reasons over it together with warehouse introspection and the
`.dex/` cache, and writes changes back into the source files as reviewable diffs.

## What dex reads

- `dbt_project.yml`: the project name, profile name, and `model-paths`.
- Every `*.sql` / `*.yml` / `*.yaml` under the model paths: the editing surface
  (model SQL, `schema.yml`, dbt semantic models).
- `target/manifest.json` when the project has been compiled; a fresh project
  loads fine without one.
- `profiles.yml` (searched the way dbt searches: `$DBT_PROFILES_DIR`, the project
  directory, `~/.dbt`), but only to resolve a target's **name and adapter type**.
  Connection fields, credentials included, never leave the engine.

The project is discovered automatically (the repo root, or a unique child
directory holding a `dbt_project.yml`); `dbt_project_dir` in `.dex/config.yml`
pins it when discovery would be ambiguous. Absent a dbt project, explore still
works (writing only to the `.dex/` cache); transform and maintain require one,
since dbt is what they edit and diff.

## How dex writes

Every proposed change is a **plan**: the agent-authored file contents, validated
by the engine, pinned to the sha256 of each file they would change, and stored
under `.dex/plans/`. Applying a plan re-hashes every file first:

- Hash matches (or the file is a clean create): apply, all-or-nothing.
- File already carries the proposed content: a no-op, not a conflict.
- Anything else means a human edited the file after the plan was made. That is a
  **conflict**: nothing is written, the divergence is surfaced as a diff, and the
  caller either re-plans against current state or overrides with an explicit
  `--confirm`.

Human edits to dbt are authoritative by construction; dex holds no competing copy
to overwrite them from. Writes are confined to the project's model paths; path
escapes are refused. dex never builds to a non-dev target.
