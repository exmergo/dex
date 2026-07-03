# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
The engine version is derived from the git tag and follows
[PEP 440](https://peps.python.org/pep-0440/); the plugin follows semver. A single
tag releases both in lockstep, so entries below are keyed by the engine version.

## [Unreleased]

### Added

- `transform init "<name>" --connector <c>`: engine-owned dbt project bootstrap,
  so an empty repo no longer hits a wall on step one. Renders a deterministic
  skeleton (`dbt_project.yml`, `models/staging/` and `models/marts/`, a
  project-local `profiles.yml` with a single duckdb `dev` target wired to the
  known warehouse) and records `connector`, `dbt_project_dir`, and
  `dbt_target: dev` in `.dex/config.yml`, all reported as create diffs. Strictly
  additive: refuses wherever a dbt project already exists. Unlike the read-only
  commands, init never falls back to a default connector (it bakes the connector
  into the generated profile): `--connector` wins, a committed `connector:` in
  `.dex/config.yml` is accepted and attributed in the envelope, and bare init is
  an error listing the valid connectors. DuckDB is the supported connector
  today; the cloud connectors return an actionable not-yet-supported error until
  their dbt adapters ship.
- Safety-spine coverage for init: refuses over an existing project, no connector
  fall-through, the generated profile is dev-only with no prod-named target and
  no secret-like keys, and the generated project round-trips through the loader
  and a real gated `dbt build`.

### Changed

- `.dex/config.yml` writes now persist only fields that were explicitly loaded
  or assigned, so the committed file records choices instead of every engine
  default.

## [0.1.0a5] - 2026-07-03

The authoring half of the loop goes live on DuckDB. `transform plan|apply|build`,
`semantic define|update`, and `emit dbt` now do real work; `maintain`, `emit osi`,
and `viz preview` still report `not_implemented`.

### Added

- The dbt project reader/writer (`dbt_project.py`): loads `dbt_project.yml`, the
  source files under the model paths, and `target/manifest.json` when compiled;
  resolves profile targets to name and adapter type only (credentials never leave
  the engine); writes plan edits back all-or-nothing with sha256 conflict
  detection, so a human edit made after planning is surfaced as a diff and never
  overwritten.
- Transform plans: agent-authored edits arrive via `--edits-file <path|->` (JSON:
  `{"edits": [{"path", "kind", "content"}]}`), are validated per kind (model SQL
  must be a single read-only SELECT once jinja is stripped; YAML must parse;
  semantic YAML is validated against MetricFlow's schemas via
  dbt-semantic-interfaces), diffed against the current project, and stored under
  `.dex/plans/<plan-id>.json`. Plan ids are content-addressed, so re-planning the
  same change is idempotent.
- `transform plan --scaffold <table>` (repeatable): deterministic staging
  skeletons (`stg_<table>.sql`, per-model YAML, one shared sources file) from the
  `.dex/` cache, with key tests and PII flags propagated into column `meta`,
  never example values.
- `transform build`: dev-target `dbt build` as an isolated subprocess with
  `--target`/`--select`, summarized from `run_results.json` (no raw log text in
  the envelope). Prod-looking targets (`prod`, `production`, `prd`, `live`,
  `release`, `main`) are refused outright, before the cost gate, and config
  cannot whitelist them.
- The cost guard (`guards/cost_guard.py`): preflight-before-spend with a strict
  order (over-ceiling blocks even when confirmed; billed paradigms require a
  ceiling; unconfirmed commands return `needs_confirmation` with the cost).
  DuckDB is free but the confirm handshake still gates `transform build`.
- Semantic authoring: `semantic define` refuses names already in the project,
  `semantic update` requires them; `emit dbt [plan-id]` applies the semantic
  plan's YAML (latest unapplied by default) through the same conflict-checked
  write path.
- Unified-diff rendering (`diffs.py`) feeding the envelope's `diffs` field, and a
  `needs_confirmation` envelope builder.
- `dbt_project_dir` in `.dex/config.yml` to pin the dbt project when discovery
  would be ambiguous.

### Changed

- Transform logic moved into its own `transform/` package (commands over pure
  engine modules), mirroring the `explore/` layout; the pre-refactor top-level
  stubs (`transform.py`, `semantic.py`, and the explore-era orphans) are removed.
- The three transform-touching safety-spine tests (prod-target refused,
  cost-guard binds, changes-are-diffs) are now real assertions instead of
  `xfail` placeholders, joined by an apply-refuses-overwrite case.

## [0.1.0a4] - 2026-07-02

### Added

- `explore query "<SELECT ...>"`: guarded ad-hoc SQL. The agent authors the
  query; the engine's new query firewall refuses or bounds it. Values may cross
  the envelope only from profiled, PII-cleared columns (every value path from a
  flagged column must pass through a measuring aggregate such as COUNT or AVG;
  MIN/ANY_VALUE/STRING_AGG and unknown functions fail closed). Results are
  columnar and hard-capped (rows, cell width, payload bytes, wall time), with
  every cut announced. Requires the `.dex/` cache, so profiling precedes probing.
- `.dex/queries.jsonl`: an audit log of every query decision (allowed, refused,
  failed) with SQL text and counts, never result values.
- `--verify` on `explore relationships` and `explore map`: measures each
  inferred join with one aggregate overlap probe and adjusts its confidence;
  relationships now carry `verified` and `orphan_fraction`.
- A probe playbook shipped with the `explore` skill: recipes mapping common
  analyst questions to effective, firewall-friendly probe shapes.
- Configurable `query:` limits in `.dex/config.yml` (`max_rows`,
  `max_cell_chars`, `max_payload_bytes`, `timeout_seconds`).

### Changed

- The boundary guarantee is stated precisely: nothing reaches agent context
  except through the sanitized envelope; credentials never, and data values only
  from profiled, PII-cleared columns, bounded and capped. Previously the docs
  said "raw rows never cross", which the guarded query path deliberately
  refines.
- The adapter protocol gains `run_query` (bounded, watchdog-interrupted
  execution of firewall-approved SQL); DuckDB implements it, cloud stubs do not
  yet.

- PII detection catches common name and contact columns, not just exact tokens:
  bare `name` and generic `*_name` columns (with a denylist of technical
  qualifiers like `table_name`), camelCase names (`firstName`), and free-text
  fields (`comments`, `notes`, `message`, `feedback`) under a new `free_text`
  category. Every new flag suppresses min/max the same way existing categories do.
- Grain and data-quality interpretation in `explore profile` and `explore map`:
  a non-unique id column now produces an explicit fan-out warning with the
  duplicate count, a table with no candidate key reports "grain unknown", and
  `profile` populates `candidate_keys` and `grain` (previously `map`-only).
- `explore relationships` and `explore map` envelopes carry `notes` explaining
  what inference examined, so an empty relationships array is distinguishable
  from "did not try".
- `explore profile` accepts comma-separated object lists in addition to
  space-separated ones.

### Changed

- Relationship inference now recognizes camelCase foreign keys (`raceId`),
  strips warehouse-layer prefixes (`raw_`, `stg_`, `dim_`, ...) when matching
  parent tables, matches parents keyed on `<entity>Id` / `<entity>_id` (not just
  `id`), and refines confidence with distinct-count and numeric-range
  containment from the aggregates already profiled. A parent whose key is not
  unique still yields the join at reduced confidence instead of being dropped.
- The `.dex/` cache schema version is now 2 (new `free_text` PII category).
- The skill wrappers drop `VIRTUAL_ENV` from the engine subprocess environment,
  silencing uv's mismatch warning on every call.
- The `explore` skill description triggers on casual, artifact-first prompts
  ("what's in my duckdb") in addition to analyst phrasings.

## [0.1.0a3] - 2026-07-01

### Changed

- Skill wrappers pin only the engine version; the connector extra is now selected
  at runtime from the active connector (an explicit `--connector`, then
  `.dex/config.yml`, then DuckDB), so a published release is connector-neutral
  instead of hard-coded to `[duckdb]`. The release tooling verifies the version
  pin rather than a connector-specific string.

### Added

- An `all` extra on `exmergo-dex-core` that installs every connector at once, for
  users who drive more than one warehouse. The light default and the `[duckdb]`
  on-ramp are unchanged.

## [0.1.0a2] - 2026-07-01

The ETM taxonomy correction. The three motions are now Explore, Transform, and
Maintain (previously Explore, Transform, Model). Explore remains the only live
stage; Transform and Maintain report `not_implemented` until they land.

### Changed

- The tagline and third motion: **Explore. Transform. Maintain.** "Model" is
  retired as a verb because it is overloaded (dbt model, data modeling, semantic
  model, LookML, ML); the ETM acronym is preserved.
- Semantic-layer authoring folds into the `transform` skill as a first-class
  capability. There is no separate `model` skill; both dbt SQL models and dbt
  semantic models are authored as reviewable diffs to the dbt project.
- Reconcile is promoted from an unnamed cross-skill behavior to the `maintain`
  skill, now backed by a real command group: `snapshot` (baseline), `check`
  (sweep), the per-axis detectors `schema` / `grain` / `semantic`, and
  `reconcile` (propose fixing diffs). Detection is read-only; only reconcile
  emits diffs. Manual and free; continuous, governed maintenance stays the
  commercial product.
- The engine CLI renames the `model` command group to `semantic`
  (`semantic define|update`), removing the "model" overload from the surface.

## [0.1.0a1] - 2026-06-30

First public alpha. The Explore stage of the ETM loop runs end to end on DuckDB;
the rest of the loop is scaffolded and reports `not_implemented` until it lands.

### Added

- Explore on DuckDB, fully read-only: ranked inventory, selective column
  profiling, PII flagged as (column, category, confidence) with no example
  values, and inferred plus declared relationship discovery.
- The dex-core command contract and sanitized JSON stdout envelope; credentials
  and raw rows never cross the boundary.
- The dbt project as the source of truth, with a non-canonical `.dex/` cache for
  exploration artifacts and the reconcile snapshot.
- A dormant OSI exporter validated against a pinned `osi-schema.json`; no OSI is
  emitted in this release.
- The Tier-1 safety spine: read-only enforcement, SELECT-only generation,
  prod-target refusal, cost preflight before any spend, PII flagged not
  surfaced, and propose-don't-impose diffs.
- The three skills (`explore`, `transform`, `model`) with thin `uv run` wrappers
  pinned to the engine.
- The Tier-2 agent-eval harness (`evals/`): triggering, output-quality, and
  uplift-over-baseline scoring behind a swappable agent backend.
- Release pipeline: tag-derived versioning via hatch-vcs, wrapper-pin coupling
  verification, and PyPI publishing through Trusted Publishing (OIDC).

### Not yet implemented

- The Transform, Model, and Reconcile stages of the loop.
- The cloud and operational connectors (BigQuery, Snowflake, Databricks,
  PostgreSQL) and their cost paradigms.

[Unreleased]: https://github.com/exmergo/dex/compare/v0.1.0a3...HEAD
[0.1.0a3]: https://github.com/exmergo/dex/compare/v0.1.0a2...v0.1.0a3
[0.1.0a2]: https://github.com/exmergo/dex/compare/v0.1.0a1...v0.1.0a2
[0.1.0a1]: https://github.com/exmergo/dex/releases/tag/v0.1.0a1
