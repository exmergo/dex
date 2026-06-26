# dex: driving the engine from any agent

dex is the agent-native analytics engineering toolkit. All logic lives in one
portable engine, `exmergo-dex-core`; this file tells any coding agent how to drive
it. On Claude Code the three skills (`explore`, `transform`, `model`) auto-trigger
and do this for you. On other agents, follow the contract below directly. The
guardrails and outputs are identical because they live in the engine, not here.

## The loop: Explore, Transform, Model (ETM)

1. **Explore** an unfamiliar warehouse or DuckDB database: rank what matters,
   profile selectively, infer joins, persist a draft map.
2. **Transform** raw data into dbt models (staging to marts) with tests and docs.
3. **Model** a semantic layer on top (entities, dimensions, measures, metrics),
   written as dbt semantic models (MetricFlow YAML) in the dbt project.
4. **Reconcile** is a cross-skill behavior: diff the warehouse and dbt against the
   last `.dex/` snapshot and propose edits.

## The command contract

The engine exposes one small, stable command surface. Run a subcommand, read the
single JSON envelope it prints to stdout, decide the next step. State persists in
`.dex/`, so subcommands are stateless and you orchestrate multi-step flows.

```bash
uv run python -m exmergo_dex_core <subcommand> [flags]
# or, with the pinned wrapper a skill ships:
uv run scripts/run.py <subcommand> [flags]
```

| Subcommand | Returns |
|---|---|
| `connect test` | capabilities, dialect, `read_only: true` |
| `explore inventory [--rank]` | ranked object summary (counts, sizes; no rows) |
| `explore profile <objects>` | column profiles + PII flags (column, category, confidence) |
| `explore relationships` | inferred + declared joins |
| `explore map` | writes/updates the `.dex/` map; prints a summary |
| `transform plan "<intent>"` | proposed dbt edits as diffs (nothing applied) |
| `transform apply <plan-id>` | writes diffs into the dbt project (a reviewable git diff) |
| `transform build --target dev` | cost preflight first; runs only with `--confirm` and a budget |
| `model define\|maintain ...` | dbt semantic model edits as diffs |
| `emit dbt` | write/refresh dbt semantic YAML from the model edits |
| `reconcile` | diff warehouse + dbt vs the last `.dex/` snapshot; propose edits |
| `viz preview` | emit the dbt semantic model to the free Viz preview |

### The envelope

Every command prints exactly one JSON object and nothing else:

```json
{ "status", "data", "cost": { "estimate", "ceiling", "paradigm" }, "warnings", "diffs", "errors" }
```

Cost is a preflight estimate surfaced **before** any spend. Any command that would
spend requires an explicit `--confirm` and a session budget. Credentials and raw
rows never appear in `data`.

## Guardrails (non-negotiable, enforced in the engine)

1. Sense-making, not enumeration. Never dump a schema.
2. Profile, don't exfiltrate. Understanding is built from aggregates, not raw rows.
3. Read-only against data; writes confined to the repo. DuckDB opens read-only;
   generated SQL is SELECT-only; builds run against a dev target only, never prod.
4. Cost-aware by connector. Nothing runs without a ceiling.
5. Credentials and raw data never enter agent context. Only the envelope does.
6. PII is flagged (column, category, confidence), never surfaced.
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
