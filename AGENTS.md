# dex: driving the engine from any agent

dex is the agent-native analytics engineering toolkit. All logic lives in one
portable engine, `exmergo-dex-core`; this file tells any coding agent how to drive
it. On Claude Code the three skills (`explore`, `transform`, `maintain`)
auto-trigger and do this for you. On other agents, follow the contract below
directly. The guardrails and outputs are identical because they live in the
engine, not here.

## The loop: Explore, Transform, Maintain (ETM)

1. **Explore** an unfamiliar warehouse or DuckDB database: rank what matters,
   profile selectively, infer joins, persist a draft map.
2. **Transform** the dbt project: author and refactor dbt SQL models (staging to
   marts) with tests and docs, and author the semantic layer on top (entities,
   dimensions, measures, metrics) as dbt semantic models (MetricFlow YAML). Both
   are the same job, reviewable diffs to the dbt project.
3. **Maintain** the project as the world changes: diff the current warehouse and
   dbt against the last `.dex/` snapshot, surface schema and definition drift, and
   propose the reconciling edits.

## The command contract

The engine exposes one small, stable command surface. Run a subcommand, read the
single JSON envelope it prints to stdout, decide the next step. State persists in
`.dex/`, so subcommands are stateless and you orchestrate multi-step flows.

```bash
uv run python -m exmergo_dex_core <subcommand> [flags]
# or, with the pinned wrapper a skill ships:
uv run scripts/run.py <subcommand> [flags]
```

Install the engine with the connector extra you use: `exmergo-dex-core[duckdb]`
for the zero-credential on-ramp, or `[snowflake]`, `[bigquery]`, `[databricks]`,
`[postgres]`, or `[all]` for every connector at once. The shipped wrapper pins
only the engine version and selects that extra for you at runtime from the active
connector (an explicit `--connector`, then `.dex/config.yml`, then DuckDB), so a
release is connector-neutral.

| Subcommand | Returns |
|---|---|
| `connect test` | capabilities, dialect, `read_only: true` |
| `explore inventory [--rank]` | ranked object summary (counts, sizes; no rows) |
| `explore profile <objects>` | column profiles + PII flags (column, category, confidence) + candidate keys, grain, data-quality warnings |
| `explore relationships [--verify]` | inferred + declared joins with confidences, plus notes on what inference examined; `--verify` measures each join with an aggregate overlap probe |
| `explore map [--verify]` | writes/updates the `.dex/` map; prints a summary |
| `explore query "<SELECT ...>"` | runs one agent-authored SELECT through the query firewall: columnar, capped result; values only from profiled, PII-cleared columns; requires the `.dex/` cache (`explore map` first) |
| `transform init "<name>" --connector <c>` | bootstrap a dbt project skeleton (`dbt_project.yml`, `models/staging/` + `models/marts/`, a dev-only `profiles.yml`), reported as create diffs; refuses if any dbt project exists; the connector never defaults, so bare init errors (an explicit flag or a committed `connector:` in `.dex/config.yml` is required) |
| `transform plan "<intent>" --edits-file <f>` | proposed dbt edits as diffs (nothing applied); `--scaffold <table>` adds a staging skeleton from the cache |
| `transform apply <plan-id>` | writes diffs into the dbt project (a reviewable git diff); a human edit since planning returns `needs_confirmation`, never an overwrite |
| `transform build --target dev` | cost preflight first; runs only with `--confirm` and a budget; prod-looking targets refused outright |
| `semantic define\|update ... --edits-file <f>` | dbt semantic model edits as diffs (MetricFlow-validated) |
| `emit dbt [plan-id]` | write the semantic plan's YAML into the dbt project (latest unapplied plan by default) |
| `maintain snapshot` | capture/refresh the known-good baseline in `.dex/snapshot.json` |
| `maintain check` | sweep every drift axis vs the snapshot; ranked drift report (read-only) |
| `maintain schema [<objects>]` | structural drift: columns/tables added, dropped, retyped, renamed |
| `maintain grain [<objects>]` | cardinality/identity drift: lost key uniqueness, changed grain, fanout |
| `maintain semantic [<objects>]` | definition drift: metric/measure/dimension/entity defs, new values, dangling refs |
| `maintain reconcile [<class>]` | propose the dbt edits that reconcile detected drift, as diffs (never applied) |
| `viz preview` | emit the dbt semantic model to the Viz preview (not yet implemented) |

Skill-to-subcommand mapping: `explore` fronts `connect`/`explore`; `transform`
fronts `transform`, `semantic`, `emit`, and `viz`; `maintain` fronts the whole
`maintain` group. Within `maintain`, detection (`check`, `schema`, `grain`,
`semantic`) is read-only; only `reconcile` emits diffs. The engine does not care
which skill fronts a subcommand.

Authored content reaches the engine through `--edits-file <path>` (or `-` for
stdin): a JSON payload of `{"edits": [{"path", "kind", "content"}, ...]}` with
`kind` one of `model_sql`, `schema_yml`, `semantic_yml`. The engine validates,
diffs, and stores the plan under `.dex/plans/`; nothing touches the dbt project
until `transform apply` / `emit dbt`. See `references/command-contract.md`.

### The envelope

Every command prints exactly one JSON object and nothing else:

```json
{ "status", "data", "cost": { "estimate", "ceiling", "paradigm" }, "warnings", "diffs", "errors" }
```

Cost is a preflight estimate surfaced **before** any spend. Any command that would
spend requires an explicit `--confirm` and a session budget. Credentials never
appear in `data`, and result values appear only in `explore query`'s columnar
payload after the query firewall has cleared them.

## Guardrails (non-negotiable, enforced in the engine)

1. Sense-making, not enumeration. Never dump a schema.
2. Profile, don't exfiltrate. Understanding is built from aggregates, not raw rows.
3. Read-only against data; writes confined to the repo. DuckDB opens read-only;
   generated SQL is SELECT-only; agent-authored SQL runs only through the query
   firewall; builds run against a dev target only, never prod.
4. Cost-aware by connector. Nothing runs without a ceiling.
5. Nothing reaches agent context except through the sanitized envelope.
   Credentials never; data values only from profiled, PII-cleared columns,
   bounded and capped.
6. PII is flagged (column, category, confidence), never surfaced. The query
   firewall enforces this on agent SQL: any expression that would carry a
   flagged column's values is refused.
7. Persistence is git, not a service. The dbt project is the source of truth; the
   `.dex/` directory is a non-canonical cache (exploration artifacts and the
   reconcile snapshot).
8. Propose, don't impose. Every change is a reviewable diff. Human dbt edits are
   authoritative; on conflict the engine surfaces the divergence and asks.

## Where things live

- Engine: `packages/dex-core/` (PyPI: `exmergo-dex-core`, Apache-2.0).
- Connector and methodology notes: `references/`.
- The contract in full: `references/command-contract.md`.
- The source of truth (dbt) and `.dex/` cache: `references/canonical-model.md`.
