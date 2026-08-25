# dbt as a first-class input (and the only write target)

The dbt project is the source of truth. dex maintains no parallel model: it loads
the project, reasons over it together with warehouse introspection and the
`.dex/` cache, and writes changes back into the source files as reviewable diffs.

## What dex reads

- `dbt_project.yml`: the project name, profile name, and the four authored path
  families (`model-paths`, `macro-paths`, `snapshot-paths`, `seed-paths`), each
  defaulted the way dbt defaults it when the key is absent.
- The source files under those families, scanned for the suffixes each one can
  hold: `*.sql` / `*.yml` / `*.yaml` under the model, macro and snapshot paths,
  and `*.csv` / `*.yml` / `*.yaml` under the seed paths. Together they are the
  editing surface (model SQL, `schema.yml`, dbt semantic models, macros,
  snapshots, seeds). A file dex can author but does not load would hash as
  absent, so a later edit to it would register as a create and the apply after
  it would conflict on a file nobody touched; the scan covers every family for
  that reason, not for completeness.
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

dbt is the format dex ships, not the only one it can read. `maintain`'s four
detection commands read whichever format `project.format` names, so a host whose
models are not a dbt project can still be a drift baseline; `transform` and
`maintain reconcile`'s mechanical edits are dbt throughout. See
[`project.md`](project.md) for the seam and how to name another format.

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

Each edit carries an `op`: `upsert` (create or update, the default) or `delete`.
A delete removes a file as a first-class reviewable diff, pinned to the file's
hash like any other edit, so it obeys the same conflict rule: a file a human
touched after planning is never silently removed. Deletes are guarded as a unit:
a plan is refused if any file that survives it still `ref()`s a deleted model, so
the post-change project is proven to have no dangling reference before the plan
is stored (and, when dbt is available, the same post-deletion tree is confirmed
by dbt's own parser). A rename is expressed as one plan: delete the old model,
create the new one, and update every referrer, validated together.

Human edits to dbt are authoritative by construction; dex holds no competing copy
to overwrite them from. Writes are confined to the four authored path families
plus the project-root manifests dbt keeps there (`dbt_project.yml`,
`profiles.yml`, `packages.yml`, `dependencies.yml`); path escapes are refused.
Within the surface, an edit's kind and its location have to agree: a snapshot
belongs under the snapshot paths and nowhere else, a seed under the seed paths, a
macro under the macro paths, and model SQL and semantic YAML under the model
paths. `schema.yml` is the one kind several families admit, because dbt expects a
seed's column types and a snapshot's tests declared beside the thing they
describe. Filing a kind in the wrong family is refused at plan time naming both
fixes (move the file, or relabel the kind), since dbt would otherwise parse a
snapshot as a model, or never load a seed at all.

A model, a snapshot and a seed each build a relation dbt names after the file and
each is `ref()`-able, so all three count as nodes: deleting one is guarded
against surviving references exactly like deleting a model, and all three are
what `maintain` fingerprints as the transformation layer. A macro is not a node
and never was one. dex never builds to a non-dev target, and a delete only ever
removes a file from the repo, never a relation from the warehouse.

## Running dbt (build, deps, parse)

Every dbt subprocess runs with its working directory pinned to the project dir, so
relative paths in `profiles.yml` and anywhere else resolve against the project, not
the caller's shell cwd. When the project declares packages (`packages.yml`, or a
`dependencies.yml` with a `packages:` key) and `dbt_packages/` is missing or empty,
`transform build` runs `dbt deps` automatically before building; `transform deps`
is the explicit install/refresh. Package installation writes only `dbt_packages/`
and the lockfile inside the project and spends nothing against the warehouse, so it
runs without the cost gate.

Semantic plans are validated up to and including dbt's own parser before they are
stored: dex copies the project (minus warehouse files, target, and logs) into a
throwaway directory, overlays the proposed YAML, and runs `dbt parse` there, so
nothing the parser writes touches the real project. When dbt is unavailable the
parse degrades to a warning rather than a hard failure.
