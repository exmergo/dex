# The dex-core command contract

This is the integration keystone. It is the boundary between the agent
and the engine, and it is what keeps every agent surface thin: each surface calls
the same subcommands and reads the same envelope, so no surface re-implements
logic.

## Shape of the boundary

- A surface (SKILL.md or AGENTS.md) tells the agent which subcommand to run.
- A thin PEP 723 wrapper (`skills/<skill>/scripts/run.py`) runs it via `uv run`
  against the pinned engine.
- The engine prints **exactly one** sanitized JSON envelope to stdout and nothing
  else. Diagnostics go to stderr.
- The agent reads the envelope and decides the next step.

State persists in the dbt project (the source of truth) and the `.dex/` cache, so
subcommands are **stateless**: the agent orchestrates multi-step flows by
re-reading them between calls. Credentials and raw rows never cross this boundary.

## The command surface

Capabilities, not final spelling. Implemented incrementally: `connect test` is
real in Phase 0; the rest return a valid `not_implemented` envelope until their
phase lands.

```
dex connect test                  -> {capabilities, dialect, read_only: true}
dex explore inventory [--rank]    -> ranked object summary (counts, sizes; no rows)
dex explore profile <objects>     -> column profiles + PII flags (column, category, confidence)
dex explore relationships         -> inferred + declared joins
dex explore map                   -> write/update the .dex cache; print a summary
dex transform plan "<intent>"     -> proposed dbt edits as diffs (nothing applied yet)
dex transform apply <plan-id>     -> write diffs into the dbt project (a reviewable git diff)
dex transform build --target dev  -> cost preflight FIRST; runs only with --confirm and a budget
dex semantic define|update ...    -> dbt semantic model edits as diffs (fronted by transform)
dex emit dbt                      -> write/refresh dbt semantic YAML from the semantic edits
dex maintain snapshot             -> capture/refresh the known-good baseline in .dex/snapshot.json
dex maintain check                -> sweep every drift axis vs the snapshot; ranked drift report (read-only)
dex maintain schema [<objects>]   -> structural drift: columns/tables added, dropped, retyped, renamed; nullability
dex maintain grain [<objects>]    -> cardinality/identity drift: lost key uniqueness, changed grain, join fanout
dex maintain semantic [<objects>] -> definition drift: metric/measure/dimension/entity defs, new values, dangling refs
dex maintain reconcile [<class>]  -> propose the dbt edits that reconcile detected drift, as diffs (never applied)
dex viz preview                   -> emit the dbt semantic model to the free Viz preview
```

Skill-to-subcommand mapping: `explore` fronts `connect`/`explore`; `transform`
fronts `transform`, `semantic`, `emit`, and `viz`; `maintain` fronts the whole
`maintain` group. Within `maintain`, `snapshot` manages the baseline, `check`
plus `schema`/`grain`/`semantic` detect drift (read-only), and `reconcile` is the
only verb that emits diffs.

Global flags (shared resolution path): `--connector`, `--path` (DuckDB),
`--repo-root`, `--confirm`, `--budget`.

## The envelope

Every command prints one object of this shape (`exmergo_dex_core.envelope`):

```json
{
  "status": "ok | not_implemented | error | needs_confirmation",
  "data": {},
  "cost": { "estimate": null, "ceiling": null, "paradigm": "free_local" },
  "warnings": [],
  "diffs": [],
  "errors": []
}
```

Rules the envelope enforces, all of them Tier-2 eval targets:

- **Cost before spend.** `cost` is a preflight estimate. Any command that would
  spend returns `needs_confirmation` unless given `--confirm` and a `--budget`.
- **Diffs, not silent writes.** Proposed changes appear in `diffs`; being there
  does not apply them. The user applies through their normal review and PR flow.
- **No secrets, no raw rows.** `data` is scanned before printing
  (`envelope.sanitize`): a secret-like key or a raw-row payload is a hard failure,
  never a silent scrub. This is a release-blocking safety guarantee.

## Why this is the first artifact

Because every skill, test, and benchmark depends on it, the contract is locked
before the engine logic is built. Phase 0 ships the contract, a real `connect
test`, and the sanitized envelope; Phases 1 through 3 fill in the subcommands
against this fixed boundary.
