# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
The engine version is derived from the git tag and follows
[PEP 440](https://peps.python.org/pep-0440/); the plugin follows semver. A single
tag releases both in lockstep, so entries below are keyed by the engine version.

## [Unreleased]

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
