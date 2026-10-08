---
name: explore
description: 'Use this whenever you need to know what is actually in a database, warehouse, or DuckDB file before you trust it: ranked inventory of what exists, column profiles, PII detection, grain and data-quality problems, verified join inference, Mermaid ER diagrams, guarded ad-hoc SQL probes, k-means segmentation, and reading the semantic layer a repo declares (dbt semantic models, a hosted dbt Cloud layer, or native Apache Ossie documents), producing a draft map without dumping the whole schema into context. Trigger it on an unmet precondition, not on any particular phrasing: if you are about to write or fix SQL against tables whose columns, types, grain, or join keys you have not verified in this session, use this FIRST. That includes dbt work: building a staging or mart model, fixing a broken model, or debugging wrong numbers, whenever the ticket names source tables without spelling out their schema. It also applies mid-task: if you are partway through and hit a table you have not inspected, stop and use this rather than guessing column names or firing off one-off SELECTs. Also use it for direct questions like "what''s in my duckdb", "which tables matter", "how do these tables relate", "is this data any good", "any PII in here", "how many orders have no customer", "cluster my customers", or "what metrics does this semantic layer define". Explore is read-only and writes nothing but the .dex/ cache. It does not author the model: pair it with transform, which writes the change once you know what you are writing against. To reconcile a project that has fallen out of sync, use maintain.'
---

# Explore

Make sense of a warehouse or a local DuckDB database the way an analytics
engineer does: rank what matters, drill selectively, and persist a draft map in
`.dex/`. This skill is read-only: it writes nothing but the `.dex/` cache.

<!-- dex:procedure:begin -->
## Procedure

dex works in one loop: explore, transform, maintain. Every step runs one engine
command, reads the single JSON envelope it prints, and decides the next step
from that envelope.

1. **Explore before you write.** Before writing or fixing SQL against a table
   whose columns, types, grain, or join keys you have not verified in this
   session, run `explore map` (or `explore profile <tables>` for a few named
   ones) and read its grain, keys, PII flags, and data-quality findings. Answer
   ad-hoc questions with `explore query`, never with a raw database client.
2. **Plan the change.** Author the file content and hand it to `transform plan`
   (or `semantic define|update|plan` for the semantic layer). Nothing is written
   yet. Read the diffs, the `warnings`, and, on a model that already exists,
   `data.row_attribution`; re-plan until they describe the change you meant.
3. **Apply it.** `transform apply <plan-id>` writes the plan as a reviewable git
   diff. A human edit made since planning comes back as `needs_confirmation`:
   re-plan against the current files.
4. **Build and verify on dev.** Run `transform build --target dev --verify`. Read
   `data.verification.ran` first, then relay the findings and anything under
   `suppressed`. A green build says dbt ran, not that the rows are right.
5. **Maintain.** `maintain check` compares the warehouse and the project with the
   `.dex/snapshot.json` baseline, and `maintain verify` checks the project as it
   is now with no baseline. `maintain reconcile` proposes the fix as a plan,
   applied with `transform apply`. Take `maintain snapshot` after a known-good
   build.

At every step: a `needs_confirmation` envelope waits on the user's spend
decision, never on yours; only a human clears a PII flag; and you never edit the
dbt project, `.dex/cache.json`, or `.dex/plans/` by hand in place of the command
that guards them.
<!-- dex:procedure:end -->

## Running the engine

```bash
uv run --no-project --script "${CLAUDE_SKILL_DIR}/scripts/run.py" <subcommand> [flags]
```

- `uv` is a prerequisite that Claude Code does not install. If the shell reports
  `uv: command not found`, stop and tell the user to install it
  (`curl -LsSf https://astral.sh/uv/install.sh | sh`, `brew install uv`, or
  `pipx install uv`), then re-run. Never fall back to raw Python, `pip`, or a
  database CLI: the guardrails live in the engine, so any other path is
  unguarded.
- The first command in a fresh environment installs the engine and can take tens
  of seconds. Offer `run.py --warm` once at setup to pay that up front; do not run
  it before ordinary commands.
- With no warehouse to point at, `demo` generates a seeded local DuckDB warehouse
  and its `.dex/config.yml`, after which every command runs with no flags. Offer
  it rather than assume it: a user who has a warehouse wants that one read. It
  only ever creates, and refuses rather than touch an existing file.

## Exploring, step by step

1. `connect test` (`--path <file.duckdb>`, or `--connector <name>` for a
   warehouse) confirms a read-only connection. To set up a warehouse connector,
   read `${CLAUDE_SKILL_DIR}/references/connectors.md`.
2. `explore inventory --rank` returns a ranked shortlist: counts and sizes, never
   rows.
3. `explore map` profiles the top-ranked objects, infers joins, writes `.dex/`,
   and returns each object's grain, best key, notable columns, PII flags, and
   findings, plus the join edges. **Read that payload instead of chaining
   `profile` and `relationships` to re-derive it.** In a repo with a dbt project
   or semantic layer, add `--use-project`: declared grain and joins count, and
   each object names the `semantic_models` that read it (empty means nothing in
   the layer does). Every cut is counted in `notes`, so an empty `notes` means
   nothing was left out. `--detail` widens the view and spends nothing; `--full`
   profiles more and does spend. On a large or metered warehouse, start with
   `--scope <schema>`.
4. `explore profile <objects>` gives one object in full, or a value domain.
   Read `key_evidence` before you trust a composite key: a combination that is
   unique only because one member is near-unique, or because a money column
   completes it, is suppressed with its reason. Where `data_quality` says a
   column is unique on almost every row, the count of rows to remove is a source
   defect to report, not a key to work around. A `~` marks an approximate number;
   one without it is exact. A fresh cached profile is reused for free;
   `--refresh` re-scans when the data changed but the schema did not.
5. `explore relationships [--verify] [--use-project]` returns the joins alone.
   `--verify` measures each join's orphan fraction. An empty list comes with
   notes saying what was examined, so it is an answer.
6. `explore diagram [--full]` renders the cached map as Mermaid, free and
   connectionless. Reproduce `data.mermaid` verbatim in a fenced ```mermaid
   block, and write it to a file only when the user asks (the engine writes
   none). **Never redraw or tidy it by hand.** The glyphs are claims derived from
   evidence, and a cardinality you supplied is exactly the overclaim this
   command exists to prevent. Read `notes` before presenting it.
7. `explore query "<SELECT ...>" ["<SELECT ...>" ...]` answers questions the
   fixed commands do not. Read `${CLAUDE_SKILL_DIR}/references/probe-playbook.md`
   before writing a probe, and follow the query rules below.
8. `explore cluster <object> [--features a,b] [-k N]` finds segments. Run `map`
   first, so the inferred joins keep foreign keys out of the features. Read the
   notes before trusting a result: a cluster under 1% of the sample is an outlier
   pocket, so report it as outlier detection or re-run with `-k`, and when
   `sample_repeatable` is false, never compare one run with another.
9. `explore semantic list|values|query` reads the semantic layer. Read
   `${CLAUDE_SKILL_DIR}/references/semantic-playbook.md` before running a metric
   query, because a metric's `time_axis`, `filter`, and measures decide what the
   number is. Narrow `list` with `--metric`, `--for-dimension`, or `--search`
   rather than `--full`. Run `values <dimension>` before writing a `--where`
   filter. On a native Ossie layer, `values` and `query` refuse and name the
   physical route instead (`explore profile`, then `explore query` on the
   relation): take it.

Per-command fields, caps, and backend detail are in
`${CLAUDE_SKILL_DIR}/references/commands.md`. Read it when a field or flag you
need is not described here.

## Rules that change what you do

### Queries and PII

- Prefer a fixed command when one answers the question. One probe answers one
  question. Send related statements in one call: each is judged on its own, so a
  refusal on one does not cost you the others.
- An aggregate over a PII-flagged column must measure (`COUNT`,
  `APPROX_COUNT_DISTINCT`, `AVG(LENGTH(...))`), never carry a value (`MIN`,
  `ANY_VALUE`, `STRING_AGG`).
- A refusal names the offending column and the fix. Rewrite once; do not retry
  the same shape.
- An unnest in the FROM clause must expand a column of a table in the query, and
  its outputs inherit that column's PII flags. The per-connector idioms are in
  the probe playbook.
- A warning that a flag sits below the blocking threshold is information to pass
  to the user, not an error to fix.
- If the user says a refused column is not personal data, recommend a
  `pii_overrides` entry in `.dex/config.yml` (the fully qualified column, with an
  optional reason). Never hand-edit `.dex/cache.json` to clear a flag, and never
  suggest weakening detection.
- Never propose a write to source data, and never paste a full schema into
  context.

### Cost

- On a metered connector, the scanning commands (`profile`, `map`,
  `relationships`, `query`, `cluster`, and a local `semantic values` or `query`)
  first return `needs_confirmation` with an estimate. Surface it in human units,
  get an explicit budget from the user, then re-issue the same command with
  `--confirm --budget <magnitude>` in the unit the estimate names. Metadata
  (`connect test`, `inventory`) is free.
- Never invent a budget. After an over-ceiling refusal, never retry with a raised
  budget without asking. Relay the refusal's calibration line verbatim, and point
  out that the ceiling binds on the estimate, so a budget set at the observed
  fraction of the estimate is refused again.
- A `suggested_session_ceiling` is the project's one-time ask for a daily cap.
  Surface it, get the user's answer, and add `--session-ceiling <value>` or
  `--no-session-ceiling` to the same re-issue. Never answer it for them.
- On BigQuery, pass on the `reserved_bytes` split: whether the number is scan or
  reserve decides whether a higher budget buys work or only headroom.
- When an estimate is larger than the work deserves, narrow with `--scope`
  instead of raising the budget.
- `--api` (a hosted dbt Cloud layer) runs where no cost guard can reach. Relay
  the warning every such result carries.

### Credentials

Credentials are discovered, never asked for. When an envelope reports missing or
expired credentials, relay the fix it names
(`${CLAUDE_SKILL_DIR}/references/connectors.md` lists them per connector). Never
ask the user to paste a key, token, or password.

## References

- `${CLAUDE_SKILL_DIR}/references/probe-playbook.md`: probe shapes for common
  questions, unnest idioms, and what to do when a probe is refused.
- `${CLAUDE_SKILL_DIR}/references/semantic-playbook.md`: the discovery order and
  the traps in metric queries.
- `${CLAUDE_SKILL_DIR}/references/commands.md`: each subcommand's fields, caps,
  and flags.
- `${CLAUDE_SKILL_DIR}/references/connectors.md`: warehouse setup, credential
  fixes, cost units, and `--scope` vocabulary.
