# The dex-core command contract

This is the integration keystone. It is the boundary between the agent
and the engine, and it is what keeps every agent surface thin: each surface calls
the same subcommands and reads the same envelope, so no surface re-implements
logic.

## Shape of the boundary

- A surface (SKILL.md or AGENTS.md) tells the agent which subcommand to run.
- A thin PEP 723 wrapper (`skills/<skill>/scripts/run.py`) runs it via
  `uv run --no-project` against the pinned engine version, installing the connector
  extra it resolves at runtime (an explicit `--connector`, then the `connector:` in the `.dex/config.yml`
  found by walking up from the run directory to the git root, then DuckDB), so the
  pin stays connector-neutral. `uv` is therefore a prerequisite, and the wrapper
  holds the envelope contract even there: with no `uv` on `PATH` it refuses with a
  `reason: prerequisite` error envelope naming the install command, rather than
  failing the exec. It is the one refusal built by hand, because the engine that
  would otherwise build it is what is missing. Two commands need more than a
  warehouse client, and both are resolved the same way, from the command being run
  rather than installed always: `explore cluster` adds `[cluster]`, and
  `explore semantic` adds `[semantic-api]` plus, where a statement might be
  rendered locally (any mode but `list`, without `--api`), `[semantic]`, and
  `[slayer]` where `semantic.vendor` is `slayer`, in place of `[semantic]`, since
  SLayer renders its own `query` and `values`. `[cube]` follows the same
  `semantic.vendor` read for the same commands where the vendor is `cube`, is
  always taken by `semantic cube`, and a
  Cube `query` or `values` never takes `[semantic]` either. A repo that runs
  neither resolves neither scikit-learn nor MetricFlow.
- That environment is uv's own, and `--no-project` is what keeps it so. Without it
  uv discovers whatever Python project the caller is standing in, builds it, leaves
  a `.venv/` and a `uv.lock` in their repo, and puts their dependencies on the
  engine's import path: unreviewed writes into a tree dex was asked only to read,
  and an engine no longer running against the closure it pinned. The invariant is
  asserted structurally on the commands the wrapper builds, in the safety spine.
- `--warm` is the wrapper's own flag and the only one it answers itself, stripped
  before the argv reaches the engine. It resolves extras through the same path a
  real run does, installs them, prints one envelope naming what it installed, and
  exits without running a command. That is what lets a container build, a CI setup
  step, or a first-time install pay a cold resolution once instead of leaving it on
  an interactive caller's clock, and resolving through the shared path is what
  keeps it from warming an environment the next command would contradict.
- The engine prints **exactly one** sanitized JSON envelope to stdout and nothing
  else. Diagnostics go to stderr.
- Every envelope carries a constant `connection` block. On success it names the
  resolved connector, its non-secret target coordinates, and the source of that
  resolution (`flag`, `.dex/config.yml`, `environment variable`,
  `dbt profiles.yml`, or `directory-local inference`). Commands that resolve no
  warehouse keep the same shape with null/empty fields. This includes the
  wrapper's missing-uv refusal and warm-up responses: their connection fields are
  null/empty and all cost fields are null, because no warehouse was contacted or
  query priced. The block is assembled from facts already resolved by the command
  and never opens a connection or spends merely to describe one.
- `DBT_PROFILES_DIR` locates `profiles.yml` for dbt operations and the
  last-resort credential fallback. It does **not** select dex's connector or
  override `--connector`/`--path` or `.dex/config.yml`; help and no-connector
  refusals say so explicitly.
- The agent reads the envelope and decides the next step.

State persists in the dbt project (the source of truth) and the `.dex/` cache, so
subcommands are **stateless**: the agent orchestrates multi-step flows by
re-reading them between calls. Credentials never cross this boundary, and nothing
reaches agent context except through the sanitized envelope: values cross only
from profiled, PII-cleared columns, bounded and capped by the query firewall.
Every `errors` and `warnings` entry is redacted as the envelope is printed
(`key=value` and `key: value` secrets, `Bearer` and `Authorization:` credentials,
query-string secrets, and URL passwords), since a message often quotes text dex
did not write.

## The command surface

Capabilities, not final spelling. Implemented incrementally: `demo`, `procedure`, `connect test`,
the `config` group, the `explore` group, the authoring surface (`transform`,
`semantic`), and the `maintain` group are live; `viz preview` returns a valid
`not_implemented` envelope until the Viz integration lands.

```
dex demo [path]                   -> generate a seeded local DuckDB warehouse (7 tables,
                                     29,512 rows) plus a .dex/config.yml beside it, so a
                                     first run needs no warehouse and no credentials;
                                     reports both under data.created and names what to
                                     run next under data.next_steps. A relative path
                                     resolves against --repo-root and is reported
                                     relative to it; the next steps carry the same
                                     --repo-root so they run as printed. Create-only
                                     and not confirmable: an existing target refuses, no
                                     directory is ever created, and an existing config
                                     at or above the target is left alone with a warning
dex procedure [--block] [--check <file>...]
                                  -> the agent procedure every skill and AGENTS.md
                                     carry, held once in the engine: data.text,
                                     data.digest, data.engine_version, data.markers.
                                     --block adds data.block, the text between the
                                     dex:procedure markers, to paste into CLAUDE.md or
                                     AGENTS.md (dex never writes either). --check reads
                                     each file and reports its copy current, stale,
                                     missing, malformed, absent or unreadable under
                                     data.copies, never echoing content; any copy that
                                     is not current is status error (reason request).
                                     Routed before any engine: no config, no connector
                                     extra, no connection; allowed in values-free mode
dex connect test                  -> {capabilities, dialect, read_only: true}
dex config get [key]              -> the effective value of every setting, or of those
                                     under key, as flat rows under data.settings: value,
                                     source (file, default, derived, or environment), the
                                     line it was written on, and as_written when the
                                     file's spelling was normalized. A value under a
                                     credential-like name is withheld. Reads only the
                                     file, before any engine exists: no connection, no
                                     connector extra. Refuses a file no command could
                                     load, naming its problems in data.problems
dex config validate               -> every problem in .dex/config.yml by setting and line,
                                     running nothing else: syntax, values the schema
                                     refuses, keys dex does not read (with the key each
                                     was probably meant to be), keys written twice,
                                     credential-like keys. problems[].fatal marks the ones
                                     that stop every other command. Any problem is status
                                     error, reason configuration, so it gates CI alone
dex config set <key> <value>      -> an in-place edit that keeps every comment, the key
  [<key> <value>...]                 order and every other line: needs_confirmation with
                                     the diff and nothing written, then the same command
                                     with --confirm writes it. <value> is YAML read the
                                     way the field reads it. Several pairs make one
                                     change, for keys only valid together. Refuses a
                                     change that adds a problem stopping commands; one
                                     already in the file is a warning, so a broken file
                                     is repaired one key at a time. Refuses a
                                     credential-like name in the key or inside the
                                     value. data.changes is one row per value (key,
                                     before, after, or withheld)
dex config unset <key> [<key>...] -> removes settings with the same in-place edit and
                                     handshake as set. Takes a key dex does not read and
                                     a whole block; a block left empty goes too, with
                                     one blank-line separator. A key the file does not
                                     write is ok, changed false, with a note naming the
                                     nearest key it does. A key inside one list item is
                                     refused: the list changes whole, through set.
                                     Refuses a removal that adds a problem stopping
                                     commands
dex explore inventory [--rank]    -> ranked object summary (counts, sizes; no rows). --rank caps at 30
  [--limit N] [--all]                objects by default (kept by rank); --limit widens it, --all lifts
                                     it; both no-ops without --rank
dex explore profile <objects>     -> column profiles + PII flags + candidate keys, grain, data-quality warnings.
  [--columns all]                    Verdict fields (grain, keys, data quality, row count) lead the
                                     payload, columns trails; by default columns is summarized to the
                                     ones carrying a finding, with the rest in elided_column_count.
                                     --columns all restores every column. A per-column field that is
                                     null on every column shown is dropped from each and named once
                                     in suppressed_fields (always present; empty means nothing was
                                     dropped). Each value_domain carries its most frequent
                                     profile_value_domain_cap values (.dex/config.yml, default 25)
                                     with the rest counted in its elided. Both reduce the payload
                                     only, never the cached profile
  [--target dev]                     also reads the dev namespace for this one command (#893), as
                                     maintain verify --target dev does: a model whose only build is
                                     the dev one becomes profileable without widening the committed
                                     allowlist. --scope still only narrows, nothing is written back,
                                     the cost guard prices the scan; prod-looking targets refused.
                                     DuckDB: its dev target is the file already read, so the flag
                                     widens nothing and a note says so
dex explore relationships         -> inferred + declared joins with confidences + inference notes
                                     (declared covers both a relationships test and a join the
                                     semantic layer declares; `semantic_join_count` splits them)
dex explore map [--detail]        -> write/update the .dex cache, and return the map:
                                     per top-ranked object its grain, key, notable
                                     columns, PII flags and data-quality findings,
                                     plus the join edges, alongside the counts. Every
                                     cap binds in every mode and every elision is
                                     counted in notes; --detail widens the selection,
                                     never the caps. No column value ever appears
dex explore diagram               -> the cached map as a Mermaid erDiagram, in `data.mermaid`;
                                     free and connectionless (a store read, no warehouse);
                                     declared joins solid (a relationships test or a shared
                                     semantic-layer entity, the latter naming the entity in
                                     its label), inferred dotted, and a cardinality
                                     drawn only where the cache proved it; draws profiled,
                                     joined objects with their grain/key/join/PII columns
                                     (--full for every eligible object and column); every
                                     elision is counted in notes; dex writes no file
dex explore query "<SELECT ...>"  -> firewall-approved SELECTs, capped and row-major
  [more...] | --sql-file <path>      (variadic: one call answers several questions; a file takes
  [--across <ds,ds>]                 one statement per line or semicolon-separated statements;
                                     --across runs each statement once per mirrored dataset,
                                     named as {ds} in the SQL, under one handshake)
dex explore cluster <object>      -> k-means over a bounded sample of numeric non-PII non-key columns
                                     (a key is a unique column, a column that joins out, or one named like one);
                                     returns cluster sizes + centroids (means) + silhouette, no rows
                                     (--features to choose columns, -k to fix the cluster count)
dex explore semantic list         -> the semantic layer as the object graph it is, in one shape from either
  [--metric <m>]                     backend: semantic models, metrics (composition, the measures behind the
  [--for-dimension <d>]              number, the grains it can be queried at, and the time column a time
  [--search <t>] [--full]            grouping resolves to), dimensions, entities with one declaration per
  [--local|--api]                    semantic model, and measures. Each semantic model carries the physical
                                     relation it sits on and each element the column behind it, which is what
                                     connects a metric to the objects `explore map` describes; the hosted API
                                     exposes no relation and declares that gap. Costs no warehouse query on
                                     either backend.
                                     Three ways to narrow it, all free and all composable, and all named in
                                     the payload so a subset is never mistaken for the layer. --metric keeps
                                     those metrics and what they reach; --for-dimension is the reverse lookup,
                                     the metrics groupable by all the named tokens, which is also the cheapest
                                     way to find the metrics that share an axis; --search matches a word
                                     against every element's name and the project's own words about it, and
                                     resolves to the metrics it touches. A search term that matches nothing is
                                     named in a note rather than refusing, unlike an unknown metric name.
                                     Budgeted like `explore map`: 50 semantic models, 60 metrics, 150
                                     dimension rows, 50 entities, 60 measures, 40 groupable tokens per metric.
                                     Every cut is counted in `elided` and named in `notes`, `elided` is present
                                     with its zeros so a complete catalog says so, and --full lifts the caps.
                                     The defaults leave an ordinary layer uncut, so a cap only bites one that
                                     was already too large to read in one payload
dex explore semantic values <d>   -> one dimension's value domain: what a filter on it may be filtered to,
  [--metric <m>] [--local|--api]     capped and columnar like `explore query`. A PII-flagged dimension refuses
                                     the command rather than being screened, because the whole output is
                                     values. `scoped_to` says whether these are the column's own values or the
                                     ones present for a metric, which is the only way a dimension behind a
                                     join can be read at all
dex explore semantic query <m,m>  -> one governed metric query, capped and row-major like `explore query`.
  [--group-by <d>] [--where <f>]     --local renders the SQL with MetricFlow and executes it through dex's own
  [--order-by <c>] [--grain <g>]     connector, PII gate, read-only assertion, relation pre-check and cost
  [--limit N] [--local|--api]        handshake; --api sends it to a hosted dbt Cloud deployment, which executes
                                     server-side where dex's cost guard is structurally unavailable and every
                                     result says so
dex transform init "<name>"       -> bootstrap a dbt project skeleton; requires an explicit
                                     --connector (never defaults); refuses if a project exists;
                                     --layered-schemas routes staging/intermediate/marts to their
                                     own <layer>_<target name> schemas; warns when a target
                                     namespace already holds objects (free metadata check)
dex transform plan "<intent>"     -> proposed dbt edits as diffs (nothing applied yet)
dex transform apply [plan-id]     -> write diffs into the dbt project (a reviewable git diff);
                                     no id means the latest unapplied plan of any kind; once
                                     something is written, data.commit_trailer is the
                                     Co-authored-by line the commit of those files ends with
dex transform plans               -> list stored plans, pending and applied, newest first
dex transform references <name>   -> where each name is used: model SQL, schema.yml, dbt_project.yml,
  [more...] [--kind K] [--full]      macros, semantic YAML, seed headers, installed packages. Repo-only
                                     and free on every connector; it opens no connection and needs no
                                     extra, so it is routed ahead of the dialect gate the rest of the
                                     authoring surface passes. Jinja-aware, so a var() read inside a
                                     macro counts; a reference it cannot resolve statically is reported
                                     under data.indeterminate rather than dropped. data.completeness is
                                     `complete` only once every reason to doubt it is ruled out, and
                                     data.limits names the rest. Capped, with every elision in notes;
                                     --full lifts the caps. --usage adds, for each model.column, how
                                     other models filter, branch on or aggregate it, grouped and
                                     counted (a bare column name is refused)
dex transform rename <kind>       -> every edit the rename needs, as one plan: the definition, every
  <old> <new> [--edits-file <f>]     model that selects it, every schema.yml that documents or tests it,
                                     every semantic reference, and a seed header. <kind> is one of
                                     column, var, model, seed, snapshot, macro, source. A column must
                                     be named `model.column`: a bare name is refused, because a report
                                     may answer imprecisely and a rewrite may not. Scoped to the
                                     defining node and its ref() descendants. Repo-only and free.
                                     REFUSES rather than partially applying: on a reference dex could
                                     not resolve statically, on a name an installed package also
                                     defines (renaming this project's copy un-shadows the package's),
                                     on a column handed to a macro as a literal string, and on a model
                                     whose SELECT list it cannot read. A bare `select *` is not a
                                     refusal: it provably carries the column through, needs no edit,
                                     and the plan says so. --edits-file carries related hand-authored
                                     edits into the same atomic plan
dex transform remove <kind> <name>   -> the definition removed, and every read verified gone. Same kinds
  [--edits-file <f>]                 as rename, same refusals. dex authors the removal of the
                                     definition and REFUSES while any read survives, naming each with
                                     a file and a line. For a scaffolded model ending in a
                                     trailing `select * from <cte>`, a removed column's definition
                                     is deleted from that CTE's projection (including its comma).
                                     Other reads, such as filters or downstream projections, still
                                     refuse: dex never guesses what those expressions should become.
                                     Pass hand-authored read edits with --edits-file and they are
                                     validated and stored in this same plan
dex transform place <column>      -> where a derived column shared by several models belongs: the
  --targets <m,m> --expr "<sql>"     lowest model in the ref() graph that every target descends from
  [--explain]                        and that already projects the inputs the expression reads. The
                                     inputs are parsed out of --expr, so they cannot disagree with it.
                                     Defines the column there and threads it down every chain, with a
                                     schema.yml entry at the ancestor and at each target and none in
                                     between. data.reasoning names the ancestor, why it is the lowest,
                                     which targets descend from it, and the chain, because a proposal
                                     has to be arguable. Where there is no common ancestor, where the
                                     lowest one lacks an input, or where two tie, data.strategy is
                                     `per_target` and the reason is stated rather than the worse thing
                                     being done quietly. --explain returns the reasoning and stores no
                                     plan. Repo-only and free
dex transform declare [<model>]   -> declarations for models that already exist, opt-in. One stored
  [--edits-file <f>] [--limit N]     plan proposing, per selected model, only the measured facts its
  [--intent "<text>"]                YAML does not state: the grain, a key or foreign_key role, a
                                     references to a model/seed/snapshot of this project, and
                                     semi_additive for a running total, each under data.proposed with
                                     its evidence. Selector as `maintain verify` (names, repeatable,
                                     comma-separated; none = every model), read from the repo alone.
                                     A declared field is never proposed, so a second run after apply
                                     is a no-op. Unit shape, NULL facts, PII and value domains stay
                                     evidence in data.declarations.<model>.drafted; intent stays in
                                     missing unless the payload's declarations supply it (they win
                                     field by field; edits are refused). Every model not proposed is
                                     in data.skipped with a reason (not_a_model, python_model,
                                     versioned, sql_unreadable, unprofiled_inputs + a profile
                                     command that can reach the input, --target dev for a
                                     dev-only build outside the allowlist, none with the reason
                                     for a name matching several profiled relations, nothing_drafted, complete, not_writable). Capped at 25
                                     proposals (--limit only narrows); skipped models do not count,
                                     the rest are data.elided_count and a note. Nothing to propose
                                     stores no plan (plan_id null). Rendered, parse-gated and stored as
                                     a transform plan declaration. Repo-and-cache only and free
dex transform build --target dev  -> cost preflight FIRST; runs only with --confirm and a budget;
             [--verify]              auto-runs dbt deps when packages are declared but not installed.
             [--for-plan <id>]       --verify sweeps the nodes this build ran for correctness and
             [--for-plan-file <f>]   reports the findings in the same envelope, under
             [--no-install-deps]     data.verification; findings never change the status
                                     data.outcome says what the run established, which success cannot:
                                     success is dbt's exit code and an empty selection exits zero, so
                                     empty_selection, unrelated, partial, skipped, stale, failed and
                                     not_run are all distinguishable from validated. data.evidence
                                     carries the invocation, typed per-node statuses, the selection
                                     asked for against what it got, the manifest and run-results
                                     digests, the relations generated, and the principal errors.
                                     --for-plan (a stored id) or --for-plan-file (an exported document,
                                     which is what a sandbox holds) adds coverage: the nodes the change
                                     required, which of them ran, and how each requirement was derived.
                                     --no-install-deps refuses a declared-but-uninstalled package by
                                     name, before any subprocess and before pricing, instead of
                                     installing it
dex transform deps                -> install/refresh dbt packages (repo-confined; no warehouse spend)
dex transform export [plan-id]    -> one stored plan as a portable document a second process can check
                                     and apply: every edit's operation, kind, preimage hash, content
                                     hash, content and classification, plus a digest over the whole
                                     plan, reported beside the document because it is the one field
                                     that has to travel by a route the caller trusts. Repo-only, free
dex transform apply               -> applies a plan document in this checkout, with no plan store, no
    --plan-file <f>                  connector, no dbt, no network and no SQL parsing. Content that
    [--expect-digest <hex>]          does not hash to what the document records is refused by path; a
                                     digest that does not recompute is refused; --expect-digest refuses
                                     a document that verifies on its own terms and is not the plan the
                                     caller pinned. Containment and kind placement are re-checked
                                     against this checkout's own surface. A file this checkout edited
                                     first is a conflict, not an overwrite
dex transform ground [plan-id]    -> what the plan depends on and how finished the answer is:
                                     dependencies, the relations behind them, what could not be
                                     resolved (with file and line), what more than one thing defines,
                                     what nothing defines once the plan lands (missing; each
                                     dependency's defined says plan, project or package), how fresh
                                     the compiled artifacts are, and a fingerprint of the plan, the
                                     source tree, the config, the packages and the engine.
                                     completeness is computed from limits and unresolved, never
                                     asserted, so "depends on nothing" and "resolution did not
                                     finish" are different answers. A missing name does not lower
                                     it: admit on complete and an empty missing together.
                                     Repo-only and free on every connector
dex transform classify [plan-id]  -> what each edit's content contains, from the content and the
    [--edits-file <f>]               operation and never from the declared kind or the filename:
                                     declarative, executable, authority_bearing, data, or unknown, with
                                     the signal that decided it and where it sits. Content that does
                                     not parse is unknown and never declarative. A dependency entry
                                     is executable; dispatch, flags, a profile, a dex guard key, and
                                     an env_var() read are authority_bearing; an env_var() with a
                                     computed name is unknown. Repo-only and free
dex transform preflight           -> what the warehouse will enforce on the next guarded build, read
                                     from the project's rendered profiles.yml rather than from what dex
                                     would write there: the provider-side controls, what each binds,
                                     where each came from, and what this connector cannot be asked for.
                                     binding is false more often than expected, and on DuckDB it is
                                     always false and says why. Free and connectionless
dex transform macro [name]        -> list the shipped dbt macros, or plan scaffolding one into the
                                     project's macro directory (dbt-parse-checked; apply like any plan)
dex transform compare [plan-id]   -> whether a stored, unapplied plan keeps one model's results: each
                                     side compiled in its own project copy with every touched model
                                     inlined, then output names and types, then the exact row multiset,
                                     as counts and never rows. --model picks the model, --map old=new
                                     states a rename, --key (or the declared grain) adds per-key counts.
                                     data.status is equivalent, different or not_checked with
                                     data.reason_code; equivalent holds for data.input_scope at
                                     data.compared_at only. Priced before spend on metered connectors
dex transform test --scaffold <m> -> plan a unit_tests: skeleton for model <m>: a given block per
                                     ref()/source() input with only the columns <m> reads, typed from
                                     the exploration cache; expect: is an empty stub that fails until
                                     filled in (dbt-parse-checked; apply like any plan)
dex transform test --mutate <m>   -> measure the tests <m> already has: plant standard analytics
                                     defects in its SQL one at a time, run its own tests against each,
                                     and report which defects nothing caught. Dev-target only, and
                                     every mutant builds as an ephemeral model in a throwaway copy, so
                                     nothing is written to the project and nothing is materialized.
                                     Capped at 20 mutants; --max-mutants only narrows. On a metered
                                     connector the whole batch is priced and confirmed as one number.
                                     Each survivor is compared with the model on the dev data and
                                     labelled equivalent or distinguishable: free and automatic on
                                     DuckDB, --check-equivalence on BigQuery and Snowflake.
                                     --downstream also runs the tests of the models that read <m>,
                                     routed through them as ephemeral models, and reports a defect
                                     only those caught as caught_downstream: on by default on
                                     DuckDB, opt-in and priced into the same batch elsewhere
dex semantic define|update|plan   -> dbt semantic model edits as diffs (fronted by transform);
                                     validated up to and including dbt's own parser; applied with
                                     transform apply like any other plan
dex semantic ossie define|update|plan
                                  -> native Ossie whole-document edits as diffs; limited to the exact
                                     files in semantic.ossie.files, validated as one prospective
                                     layer, and applied atomically with transform apply
dex semantic slayer define|update|plan
                                  -> SLayer model files as whole-file diffs; limited to the exact
                                     files in semantic.slayer.models, validated as one prospective
                                     set with SLayer's own model, and applied atomically with
                                     transform apply
dex semantic cube define|update|plan
                                  -> Cube model files as whole-file diffs; limited to the plain YAML
                                     files in semantic.cube.files (JS, globals.py, cube.py/cube.js and
                                     compiled-only files refused by name), validated as one prospective
                                     set with dex's port of Cube's model rules, each cube and view
                                     classified defined, updated or unchanged, and applied atomically
                                     with transform apply
dex maintain snapshot [--project-only]
                                  -> capture/refresh the known-good baseline in .dex/snapshot.json;
                                     --project-only re-fingerprints only project layers and carries the
                                     existing warehouse baseline and its timestamps forward without opening
                                     a warehouse connection, reporting data.baseline.carried_forward: true;
                                     each model's declaration (descriptions and config.meta.dex, per model
                                     and per column) is fingerprinted as one hash per field, counted in
                                     transform_layer.declared_model_count
dex maintain check                -> sweep every drift axis vs the snapshot; ranked report (read-only;
                                     two-phase on billed connectors: free axes now, one estimate for scans)
dex maintain schema [<objects>]   -> structural drift: columns/tables added, dropped, retyped, renamed;
                                     nullability; dangling sources; a model added, removed, or
                                     content-changed since the baseline (metadata, free everywhere);
                                     under semantic.vendor: slayer, the models held to the warehouse
                                     by SLayer's own validate-models rules on dex's metadata; under
                                     semantic.vendor: cube, the cubes and views held to it by dex's
                                     own rules (declared_column_missing|type_conflict|
                                     relation_missing|dependency_dropped)
dex maintain volume [<objects>]   -> freshness drift: row counts that collapsed, emptied, or spiked (free)
dex maintain grain [<objects>]    -> cardinality/identity drift: lost key uniqueness, changed grain, join
                                     fanout, the grains the project declares re-verified, and declared
                                     join arity (SLayer cardinality, Cube relationship) measured
                                     (join_cardinality_exceeded)
                                     (aggregates; gated by --confirm --budget on billed connectors)
dex maintain semantic [<objects>] -> definition drift and dangling refs (free) + declaration drift
                                     (declaration_added|removed|changed naming the model, column and
                                     field, population/grain/additivity/unit ranked above a description;
                                     free; data.declaration_drift says whether it was checked) +
                                     categorical dimension cardinality change on any model naming its
                                     table (a scan; gated on billed connectors); a compiled-only Cube
                                     definition is compared by content alone (compiled_only_changed)
dex maintain reconcile [<class>]  -> propose the edits that reconcile detected drift, as a stored plan
                                     of diffs tagged mechanical/advisory (applied with transform apply);
                                     a SLayer or Cube layer's own removals as a second plan,
                                     semantic_plan_id;
                                     cardinality advisories distinguish expansion from contraction
                                     using available counts, and flag approximate counts as uncertain
dex maintain verify [<selector>]  -> is the project correct right now, no .dex/snapshot.json baseline
                                     required (unlike every subcommand above). Build status:
                                     failed/skipped build nodes (naming the failed cause, walking back
                                     through transitively skipped parents), nodes that warned rather
                                     than failed, and models with no relation in the warehouse. Row population: row_loss and row_fanout against
                                     each model's driving parent, the FROM-clause relation read out of
                                     the compiled SQL through its CTE chain, naming each join beside
                                     its own key and stating both counts; a fan-out on a model whose
                                     declared grain (declaration, unique or composite test, primary
                                     entity; never the naming guess) the same run proved unique is
                                     capped at low (data.grain_proven), and one through joins with no
                                     readable key (a range or inequality join) at medium
                                     (data.join_key_read: false) (#841); a model with a filter, aggregate,
                                     de-duplication or set operation is never reported for loss, and an
                                     incremental model is skipped. A model generated in SQL with no parent
                                     relation is named in notes. A real parent relation missing from the
                                     warehouse is still a warning. Free on metadata alone, and every
                                     count made exact where counting bills nothing; a relation the
                                     warehouse keeps no count for (any view) is priced as one batched
                                     aggregate and returned as an offer beside findings already final.
                                     Grain: the grain a declaration states first, then a unique test,
                                     a unique_combination_of_columns test, a primary entity, or a
                                     naming guess, one source per model. Declared facts, checked with
                                     aggregate-only statements: unit_out_of_range (a fraction outside
                                     0..1, a percent wholly inside it, with the observed min and max),
                                     additivity_contradicted (an additive column the cumulative
                                     detector reads as a running total), population_contradicted (an
                                     excluded value still present, as a count), each class priced into
                                     the same single offer on a metered connector. A project that fails
                                     to compile is reported first and suppresses every other check;
                                     data.suppressed names each finding class that did not run and why,
                                     declared-fact classes (unit, additivity, population) included
dex viz preview                   -> emit the dbt semantic model to the Viz preview (not yet implemented;
                                     the Viz integration arrives later)
```

### How authored content reaches the engine

The engine has no model of its own; the agent authors the dbt file content and
hands it over via `--edits-file <path>` (or `-` for stdin), a JSON payload:

```json
{"edits": [
  {"path": "models/staging/stg_orders.sql", "kind": "model_sql", "content": "..."},
  {"path": "models/staging/stg_orders.yml", "kind": "schema_yml", "content": "..."},
  {"path": "snapshots/snap_orders.sql", "kind": "snapshot_sql", "content": "..."},
  {"path": "seeds/country_vat.csv", "kind": "seed_csv", "content": "..."},
  {"path": "tests/assert_totals_reconcile.sql", "kind": "test_sql", "content": "..."},
  {"path": "analyses/email_skew.sql", "kind": "analysis_sql", "content": "..."},
  {"path": "models/marts/dim_orders.sql", "kind": "model_sql", "op": "delete"}
]}
```

Beside `edits`, `transform plan` and `semantic define|update|plan` accept a
`declarations` list: per model, what one row is, which rows are in, what each column
is, and the assumptions the model rests on with their evidence. Each is rendered
into the model's existing YAML entry as a `schema_yml` edit in the same plan, and
`edits` may be omitted when the payload only declares models that exist. The plan,
apply and build envelopes then carry `data.decisions`, every assumption declared on
the models the command touched. The plan envelope also carries
`data.declarations.<model>` for every model the plan creates, edits or declares:
`drafted` (facts the `.dex/` cache and the SQL support, each with its evidence),
`declared` (what the post-change YAML says) and `missing` (intent fields nobody
supplied, each also a warning). For measures, warnings name only the missing
`aggregation`, `additivity`, or `unit` fields and group columns with the same
missing fields; the `missing` list keeps its per-column paths unchanged.
`drafted.precedent` says what the project's other models do with each column
that the model filters, branches on or aggregates, and a
literal compared to a column whose complete cached domain does not hold it is a
warning. On a model that already exists, `contradictions` lists each declared rule
(population filter, grain, declared column, an upstream column's additivity) that
the edit and the post-change declaration disagree on, each also a warning naming
the side that looks stale and both fixes; it never refuses. It reads the cache and
the repository only and opens no connection; a contradicted population cites the
delta row attribution measured, and measures nothing of its own. A model under
`transform.marts_paths` (default `[models/marts]`) whose post-change YAML carries a
declaration also gets `data.semantic_proposal`: the semantic definition the
declaration states in the configured layer, through the one per-vendor derivation
hook (`SemanticDerivationSource`), with `vendor` naming which. Under `dbt` it is a
MetricFlow semantic model (primary entity from a single-column grain, foreign
entities, categorical and time dimensions, measures with their declared
aggregation, and `non_additive_dimension` for a `semi_additive` measure), with every
gap named rather than guessed and no metric ever derived. In a project with a dbt
semantic layer (a semantic model and a time spine) a derivation that passes
MetricFlow's schemas, the metric-input check and dbt's parse of a throwaway copy is
added to the same plan; in one without, nothing is added and the object is ready to
pass to `semantic plan --definitions-file`. The parse is never skipped for a
derivation, whatever `--no-parse` says. Under `slayer` it is a SLayer model file
(`primary_key` from the grain, `many_to_one` joins from foreign keys, saved
measures `<column>_<agg>`), validated by `semantic slayer`'s own checks and read
back through its catalog, then stored as a second plan on the SLayer layer
(`plan_id`, beside the dbt plan; apply both) where its path is already under
`semantic.slayer.models`, and proposed with the `dex config set` that adopts it
otherwise. Under `cube` it is a Cube model file of its own, `<dir>/<mart>.yml`
beside the configured files (`primary_key` on the grain dimensions, other roles as
dimensions, declared aggregations as measures named after the column,
`many_to_one` joins from foreign keys), validated by `semantic cube`'s own checks
and read back through its catalog, and included or proposed on the same terms
against `semantic.cube.files`. Under `ossie` it is a dataset (a field per declared
column, `primary_key` on every grain field, declared aggregations as dataset-scoped
metrics `<column>_<agg>`, a relationship from each foreign key to the parent's
field) appended to the configured document holding the datasets its foreign keys
reference, every other byte kept, and stored as a second plan (`plan_id`); where
no configured document can hold it, it is proposed as a document of its own. In
every layer a population is carried as metadata (`config.meta.dex`, `meta.dex`, or
Ossie's `ai_context`, and one sentence in the description)
and never as a filter, since the mart's SQL already enforces it; per model,
`carried` and `not_carried` account for every declared field.
`semantic slayer define|update|plan`, `semantic cube define|update|plan` and
`semantic ossie define|update|plan` take `declarations` too, annotating a model,
cube or dataset the payload or the layer defines. A command that takes no declarations refuses a payload
carrying them rather than dropping them. `transform declare [<model>...]` takes the
same payload for models that already exist: it proposes only the measured facts
each selected model does not yet declare, lays the supplied `declarations` over
them field by field, and stores the lot as one capped plan, naming every model it
skipped and why. The full shape, the refusals and
what each field renders into:
[`skills/transform/references/declarations.md`](../skills/transform/references/declarations.md).

`kind` is one of `model_sql`, `schema_yml`, `semantic_yml` (optional on
`semantic define|update`, which imply `semantic_yml`), `packages_yml`,
`macro_sql`, `snapshot_sql`, `seed_csv`, `test_sql`, `analysis_sql`,
`project_yml`, `profiles_yml`, or `semantic_document` (optional on `semantic
ossie define|update|plan`). Each edit also has an `op`:
`upsert` (create or update, the default, carrying `content`) or `delete` (remove
the file, no `content`). A delete is a reviewable diff pinned to the file's hash
like any other edit, and it is guarded: the plan is refused if any surviving file
still `ref()`s a deleted model (the offenders are named, with the line), so the
post-deletion project is proven free of dangling references before the plan is
stored, and, when dbt is available, the same post-deletion tree is confirmed by
dbt's parser. The guard reads the reference index behind `transform references`,
which is why it sees the two-argument `ref('package', 'model')` form, why a seed's
data rows no longer count as source, and why a reference dex could not resolve
statically *warns* rather than refusing: it may or may not name the deleted node,
and no edit the caller could make would settle it.
Control-flow model compilation requires dbt and a dev profile. Dex compiles a
throwaway copy containing every proposed edit, validates fresh SQL, and keeps the
original Jinja in the stored plan. Compile-time SELECTs are governed through
Dex's query path; direct warehouse handles and unsupported adapter introspection
refuse. Dynamic queries share the command ceiling, with partial estimates and
completed spend disclosed. See [dbt projects](dbt-project.md#compiling-authored-control-flow)
and [cost controls](cost-controls.md#queries-discovered-during-model-compilation).

A rename is one plan: `delete` the old model, `create` the new, `update` the
referrers, validated together. `transform rename` generates exactly that plan, and
the rest of the rename's edits with it, so hand-assembling one is now the fallback
rather than the route. Note the two guards read the same index and answer
differently on purpose: a reference dex cannot resolve *warns* on a delete and
*refuses* on a rename. A dangling dynamic ref left by a delete is unsatisfiable,
so refusing would block a legitimate delete forever; the same reference in a
rename's path is satisfiable, because the caller can resolve it by hand and
re-run. The engine validates each edit
(model SQL must be a single read-only SELECT, checked on fresh dbt-compiled SQL
for Jinja `for`/`if` control flow and with Jinja stripped otherwise; YAML must
parse; semantic YAML must satisfy MetricFlow's schemas; a `packages_yml` edit
must carry a `packages:` or `dependencies:` list and targets the project-root
`packages.yml` or `dependencies.yml`; a `macro_sql` edit must hold only macro
definitions and jinja comments and must target the project's macro paths, where
no other kind may go; a `snapshot_sql` edit must hold exactly one
`{% snapshot %}` block whose `config()` names a `unique_key` and a `strategy` of
`timestamp` (with `updated_at`) or `check` (with `check_cols`), whose body is a
single read-only SELECT, and must target the project's snapshot paths; a
`seed_csv` edit must parse as CSV with a named, duplicate-free header and one
field per column on every row, must stay under 5,000 data rows and 1 MiB, must
not name columns that look like personal data, and must target the project's
seed paths; a `test_sql` edit must target the project's test paths and must be
either a generic test definition (only `{% test %}` blocks and jinja comments,
balanced) or a singular test that is a single read-only SELECT once its jinja is
stripped; an `analysis_sql` edit must target the project's analysis paths and be
a single read-only SELECT on the same terms; a `project_yml` edit targets the
project-root
`dbt_project.yml` and must keep a `name`; a `profiles_yml` edit targets the
project-root `profiles.yml` and must reference every secret via
`{{ env_var('NAME') }}`, never a literal), pins it to the sha256 of the file it
would change, computes the diffs, and stores the plan under
`.dex/plans/<plan-id>.json`. Nothing touches the dbt project until an apply.
`packages_yml` is the guarded way to declare dbt package dependencies: a
reviewable diff like any other edit, then `transform deps` (or the automatic
deps step in `transform build`) installs them. `macro_sql` is how a macro is
repaired or customized by hand; `transform macro <name>` is the scaffolding path
for the macros dex ships. `snapshot_sql` and `seed_csv` bring slowly-changing
dimension capture and small reference tables into the same flow, each confined to
its own path family (`snapshot-paths` and `seed-paths` in `dbt_project.yml`, read
with dbt's own defaults) and each gated by dbt's parser at plan time. A
`schema_yml` edit is accepted beside either one, which is where dbt expects a
seed's column types and a snapshot's tests and docs to be declared. `dbt build`
runs seeds and snapshots natively, so nothing new has to be run after an apply;
a snapshot writes a table and is priced in the cost handshake, while a seed
loads a local CSV, scans nothing, and is deliberately unpriced.

`test_sql` and `analysis_sql` do the same for the two remaining families
(`test-paths` and `analysis-paths`, again with dbt's defaults). A singular test
is an arbitrary SELECT that must return no rows, which is where most
project-specific assertions live, and generic test definitions share the same
directory, so `test_sql` reads the file to tell them apart: a `{% test %}` block
is checked structurally like a macro, anything else is checked like a model. A
singular test naming no `ref()` or `source()` warns rather than refuses, since it
runs against nothing and passes unconditionally. An analysis is held to the same
read-only SELECT even though dbt only ever compiles it, because read-only against
data describes what dex writes rather than what dbt runs. `schema_yml` is
accepted beside each. Neither kind builds a relation and nothing can `ref()`
either, so neither is a node: neither enters the drift baseline, and deleting one
raises no dangling-reference guard. `dbt build` runs a singular test natively and
prices it like any other test; an analysis is compiled by `dbt compile`, never
built, and costs nothing.

**Three different things here are called a test.** Generic tests are declared
inside a `schema.yml` (`data_tests:` on a model or a column). Unit tests are
scaffolded by `transform test --scaffold <model>` into a `unit_tests:` block,
also `schema_yml`. Singular tests and generic test *definitions* are files under
`test-paths`, and those are what `test_sql` authors. `transform test --mutate`
measures all three together, because dbt runs all three and a defect only has to
get past every one of them to ship.

### `transform test --mutate <model>`: what the tests are worth

A passing suite says the tests ran. It does not say they would notice if the
model were wrong, and no count of tests distinguishes the two. This plants one
defect at a time in the model's compiled SQL and reports which ones nothing
caught.

The defect classes are the ones that recur in analytics code: a boundary
comparison flipped to include or exclude its edge, a `WHERE` predicate dropped or
negated, an inner join swapped for a left join or the reverse, a `CASE` branch
removed, a ratio inverted, a window frame bound shifted by one, and `sum`
swapped with `max`. Each finding is written as the defect rather than as a diff,
and carries `suggested_test`, because the reader's next action is to write a
test.

**Where a mutant lives.** In a throwaway copy of the project, and nowhere else.
Each one is written with `materialized='ephemeral'`, so dbt inlines it into each
test as a CTE and materializes nothing: no relation is created, replaced or
dropped, and there is nothing to clean up afterwards. The run uses `dbt test`
rather than `dbt build`, which is what keeps a failing unit test from skipping
the model and cascading that skip onto every data test attached to it. The
project's `on-run-start` and `on-run-end` hooks are stripped from the copy,
since dbt is invoked once per mutant and a hook that grants or audits should not
fire N+1 times; a note says so when it happens.

**What the verdicts mean.** `killed` is a defect at least one test caught, named
in `caught_by`. `survived` is one nothing caught. `rejected` is one the warehouse
refused outright, kept separate because a build would have failed on it anyway
and counting it as caught would flatter the suite. `not_run` is a mutant the
budget stopped. Every verdict is relative to the tests that passed against the
unmutated model, and any test that did not is listed in `baseline.excluded` with
the reason, so a suite measured against its own broken tests cannot read as
clean. A run where nothing passes at baseline is an error, not a clean sweep.

**Equivalent on the dev data.** A survivor is not always a gap in the
assertions. Some mutants produce exactly the model's rows on the data the dev
target holds (an inner join where every key resolves, a window frame no
partition is long enough to reach), and no test over that data could tell them
apart. So each survivor is compared with the unmutated model on the dev target,
and `equivalence` on the mutant carries the answer: `status`, then
`only_in_mutant` and `only_in_baseline` (rows one side has that the other does
not, counted as a multiset, so duplicated rows count), and `mutant_rows` and
`baseline_rows`. Counts only; no row value leaves the warehouse. `equivalent`
means both difference counts are zero, so only a unit test fixture that reaches
the case can catch it. `distinguishable` means the data already differs, so an
assertion over that data would catch it today. Distinguishable survivors are
listed first, then any that could not be compared, then equivalent ones, and
`suggested_test` follows the label: a fixture for an equivalent survivor, an
assertion for a distinguishable one. `not_checked` carries a `reason`. An
equivalent survivor is still `survived` and still counts in `score`, since a
fixture can catch it.

Before any survivor is compared, the unmutated model is compared with itself.
A model that does not reproduce its own output (a `random()`, a uuid, a
`row_number()` over ties) would make every mutant look different from it, so in
that case no survivor is labelled and `data.equivalence.self_check` says
`not_reproducible`. `data.equivalence` also reports `checked`, the
`distinguishable`, `equivalent` and `not_checked` counts, and a `reason` when
the comparison did not run. The comparison is free and automatic on DuckDB. On
BigQuery and Snowflake it is opt-in, with `--check-equivalence`, because it is
spend; `--no-check-equivalence` turns it off anywhere. Other connectors report
survivors as `not_checked` until dex has a verified whole-row comparison for
them.

**Cost.** Free on DuckDB. On a metered connector each mutant is priced as the
statements the warehouse will actually run, by splicing it into each test's
compiled SQL, because a mutant that drops a partition predicate scans more than
the model it came from. The whole batch is one estimate and one confirmation:
`per_table_bytes` names `(baseline)` and each mutant, so the caller sees the
total and the breakdown before anything executes. With `--check-equivalence`,
two more lines join the same estimate: `(equivalence self-check)` and
`(equivalence checks, if every mutant survives)`. The checks are priced as if
every mutant survives, because the suite's strength is not known until the run,
so the settled spend is at most that line. If the confirmed budget runs out
partway, the run stops and the remaining mutants are reported `not_run` rather
than the budget being exceeded. Spend settles per run under
`command: "transform test"` in the ledger, each comparison included.

**The cap.** 20 mutants, and `--max-mutants` may only lower it. Mutants are
ordered round robin across the defect classes, so a capped run on a model with
forty comparisons and one join still tests the join; what the cap cut is reported
in `data.cap.elided`, per class.

**Downstream tests.** A defect ships only if every test in the project misses it,
and the tests that pin what an intermediate model feeds are often on the mart that
reads it. `--downstream` runs those too. Each model downstream of the mutated one
that can carry the mutant is made ephemeral in the same throwaway copy, so dbt
inlines the whole chain into each of its tests, mutant at the bottom, and still
nothing is materialized. The walk is breadth first through the manifest, nearest
models first, capped at 50; it stops on each branch at a node that cannot be made
ephemeral (a snapshot, a Python model, a package's model, a model with a
`sql_header`), since below it every relation still holds the unmutated rows.
`data.downstream.cut` names each such node and why, and `elided` counts what the
cap left out.

Which downstream tests run: the generic and singular tests on the routed models,
listed in `data.downstream.tests`. Two kinds are excluded, each named in
`data.downstream.excluded` with its reason: a unit test on a downstream model,
which reads its fixtures and never the mutant, and a test that also reads a
downstream relation the mutant cannot reach, which would compare the mutant with
the unmutated model and report a catch that a real change would not produce. The
model's own tests are never excluded.

A defect only downstream tests caught is `caught_downstream`, with the tests in
`caught_downstream_by`; `caught_by` stays the model's own tests, and a `killed`
mutant also carries `caught_downstream_by` when a downstream test caught it too.
`score` counts a `caught_downstream` defect as a miss, because it answers for the
model's own suite and means exactly what it meant before downstream tests ran.
`score_with_downstream` counts it as caught, and is null when the downstream tests
did not run. `data.counts.caught_downstream` is always present, zero when off.
Equivalence checks run only for true survivors.

On by default on DuckDB, where it is free. Elsewhere each downstream test
recomputes the chain beneath it, so it is opt-in with `--downstream`, priced into
the same single estimate (each mutant's line grows by the downstream tests it
will run) and confirmed once; `--no-downstream` turns it off anywhere. When it is
off, `data.downstream.reason` says why: not asked for on a metered connector, no
model reads this one, the models that read it have no data test that can see the
mutant, or the routed models would not compile as ephemeral, in which case every
file is put back and only the model's own tests run. A model with no tests of its
own is measurable when downstream tests read it.

`seed_csv` is the first kind that puts **values**, not logic, into a reviewable
diff, and a diff goes into git and stays there. So a seed's header is checked
both against the PII detector `explore` profiles warehouse columns with and
against the flags already in the `.dex/` cache. A column at or above the
blocking threshold is refused, and the refusal names the `pii_overrides` entry
that would clear it, never a value ([`pii-policy.md`](pii-policy.md)). The
standing limit is worth knowing: dex detects PII from names and types and never
from values, everywhere, so a seed column named `code` full of email addresses
passes this gate.

`project_yml` and `profiles_yml` bring the two
project-root config files into the same plan/diff/apply flow; because they carry
project-wide settings and connection targets, they are gated by dbt's own parser
at plan time, and a `profiles_yml` edit is refused if it (or the file it would
replace) inlines a literal credential, so no secret ever reaches the diff. A
key reads as a credential when its name contains one (`db_password`,
`jwt_token`), and its value is safe only when the whole value is one
`{{ env_var('NAME') }}` reference, with an empty default at most
(`{{ env_var('NAME', '') }}`): a literal beside the reference, a non-empty
default, or a filter or operator after it is refused. A key naming a location
(`private_key_path`, `token_uri`) holds no secret and is exempt.

- `transform init "<name>" --connector <duckdb|snowflake|bigquery|databricks|postgres>`
  bootstraps a dbt project when none exists: `dbt_project.yml`, `models/staging/`
  and `models/marts/`, and a project-local `profiles.yml` with a single `dev`
  target wired to the warehouse dex already knows (`--path`, or `duckdb.path` in
  `.dex/config.yml`). The profile is written owner-only (`0600`), as is any
  `profiles.yml` a later apply rewrites: dex renders only `env_var()` references
  and key paths into it, but it is the file a password gets pasted into. It is strictly additive (any existing dbt project is a
  refusal, so nothing is ever overwritten), and everything created is reported
  as `create` diffs. **Init never defaults the connector**: the DuckDB on-ramp a
  read-only command may take (an explicit `--path`, or a committed config that
  omits `connector:`) is wrong here, because init bakes the connector into the
  generated profile. `--connector` wins, a
  `connector:` already committed in `.dex/config.yml` is accepted (the envelope
  names which source was used), and bare init is an error listing the valid
  connectors. On success init writes `connector`, `dbt_project_dir`, and
  `dbt_target: dev` back to `.dex/config.yml`, so the choice is made once and is
  ambient for every later command. Every connector renders: DuckDB, BigQuery,
  Snowflake, Databricks, Postgres, and Redshift.

  `--layered-schemas` opts into per-layer schema routing: init additionally
  scaffolds `models/intermediate/`, the shipped `generate_schema_name` macro
  override, and a `models:` block with `+schema: staging|intermediate|marts`.
  The override composes `<custom schema>_<target name>` (layer first, target
  last), so a dev build lands in `staging_dev` / `intermediate_dev` /
  `marts_dev`; a model with no custom schema falls back to `target.schema`. On
  BigQuery the layer namespaces are sibling datasets in the profile's project;
  on Snowflake and Databricks they are sibling schemas inside the dev
  database/catalog; on Postgres and Redshift sibling schemas in the database;
  on DuckDB schemas inside the target file. The same macro is available to
  existing projects via `transform macro generate_schema_name`.

  Init also runs a content preflight over every namespace the new project
  would build into (the base dev namespace, plus each layer namespace when
  `--layered-schemas` is on): a free, metadata-only listing on every connector
  (never a query, never a warehouse wake-up). A namespace that already exists
  *with* tables or views produces a warning naming it, the object count, and up
  to five object names, because a later build would write alongside that
  content and replace same-named relations; init is the one point where the
  name is still trivial to change. It is advisory by design: init still
  succeeds, an empty or absent namespace stays silent, and when dex cannot open
  a connection the check degrades to a single note (init is credential-optional
  and stays that way). DuckDB's base namespace is exempt, since the dev target
  is the same file as the source warehouse; only the layer schemas are probed
  there.
- `transform plan` also accepts `--scaffold <table>` (repeatable): a
  deterministic staging skeleton (`stg_<table>.sql` plus per-model YAML with key
  tests under `data_tests:` and PII flags in model and column `config.meta`)
  generated from the `.dex/` cache.
- `transform plan` reports the **row-population consequence** of an edit to a
  model that already exists, under `data.row_attribution`. Validation proves an
  edit is well formed; this is the only plan-time check that asks whether it
  behaves the same. In scope is everything that can change which rows enter a
  model: `WHERE`, `HAVING` and `QUALIFY` predicates, a join added, removed or
  retyped between inner and left, a swapped driving relation, and `DISTINCT` or
  `GROUP BY` changes. Out of scope, and silent by construction rather than by
  filtering, are column expressions, aliases, casts and ordering, none of which
  can move a row.

  Each change is measured by applying it, alone, to the prior model and counting,
  so a delta belongs to one change rather than to the edit as a whole. That
  matters because a ticket routinely *requires* a row-population change, and a
  net figure cannot tell a requested change from a silent side effect. The
  whole-model net is reported alongside, measured on the authored model rather
  than summed from the parts; when the two disagree the changes interact and
  `interacts` says so, because isolated counterfactuals do not compose.

  **This is a warning and never a refusal**, and the plan is built and stored
  before any of it runs, so nothing here can stop a plan from existing. Naming a
  change is free and opens no connection, so it always happens. Measuring one is
  a `COUNT` aggregate over the relations the model already reads, free on DuckDB
  and spend elsewhere, so counting runs unasked only on DuckDB; on a billed
  connector it needs `--attribute-rows` and then goes through the ordinary
  estimate and `--confirm --budget` handshake, with the priced ask returned
  beside the stored plan so a confirmed re-run measures without re-planning.
  `--no-attribute-rows` turns counting off anywhere.

  Every counting statement is one `SELECT COUNT(*)` over the model, cleared by
  the query firewall like any agent-authored SQL, so the model's parents must be
  profiled and the PII policy applies (a count projects no column, so a filter
  over a flagged column is still attributable and no value crosses the envelope).
  Nothing is materialized and no relation is created. Static `{{ var() }}` calls
  with a scalar value in `dbt_project.yml` (or a scalar default argument) are
  resolved in place; a change dex cannot isolate or measure reports
  `attributed: false` and names why: macro-generated SQL, a jinja statement
  block, an unresolved or non-scalar `var()`, a renamed or added CTE that cannot
  be paired with a prior scope, a relation absent from the cache, or a
  counterfactual that is not valid on its own because it depends on something
  else in the same edit.
  Authoring a model that does not exist yet produces no findings and opens
  nothing.
- `transform plan` raises two **advisory warnings about the shape of what was
  authored**, both free, both static, and neither able to refuse a plan. The
  first compares the authored SELECT list against the columns the model's
  `schema.yml` declares, in both directions: a declared column the SELECT does
  not produce, and a produced column the declaration does not name. A model with
  no declared columns has no contract and produces nothing; a SELECT list that
  cannot be resolved statically (a `select *`, a `t.*`, an unaliased macro
  standing in for a column) says so rather than guessing, and an *aliased* macro
  call is resolved by its alias. The declaration is read with this same plan's
  own `schema.yml` edits overlaid, so a model and its documentation edited
  together are compared against each other rather than against a stale file.

  The second reads a **house convention out of the project's own models**: an
  authored model exposing a raw foreign key where its siblings all resolve the
  equivalent key to a descriptive attribute. Siblings are the models sharing the
  authored one's folder and its layer prefix, widening to that prefix
  project-wide when the folder is too small to hold a precedent. At least three
  of them must resolve a key of the same id-suffix shape and none may pass one
  through, since one counter-example is enough to say the house has not settled
  the question (a fact table beside the dimensions is that counter-example, and
  a `*_key` convention says nothing about a `*_id`). A key naming the model's
  own entity is its identity, not a foreign key. And the project must hold a
  parent to resolve against, because the fix is a `ref()`. The warning names the
  siblings and the parent so the inference can be argued with.

  Both read the project as this plan will leave it, so models authored together
  inform each other, and both judge only what the plan authors: an existing
  violation elsewhere in the project is not this plan's warning. The convention
  check is the only one dex raises on a style judgment rather than on a fact,
  which is why it is the only one a project can decline:
  `conventions.resolved_keys: false` in `.dex/config.yml`, named in the warning
  itself.
- `transform apply [plan-id]` re-hashes every file first. A file edited by a
  human after the plan was made is a **conflict**: nothing is written, the
  divergence is returned as diffs with `needs_confirmation`, and only an explicit
  `--confirm` overrides it. An apply is all-or-nothing across the whole plan: one
  conflict refuses the set, so the clean edits beside it do not land either. The
  stored paths are re-checked against the project format's editing surface at the
  same time, and a path outside it is refused outright rather than surfaced for
  confirmation, since nobody can accept a write into a region the format says it
  does not own. With no plan id it
  applies the latest unapplied plan of any kind (semantic plans included; `emit
  dbt` remains the semantic-scoped spelling). `transform plans` lists what is
  stored, pending and applied.
- **A commit of what dex wrote credits Dex.** dex never commits, so an apply
  that wrote at least one file, from the store or from `--plan-file`, hands the
  credit to whoever does: `data.commit_trailer` is
  `Co-authored-by: Dex <dex@exmergo.com>`, the last paragraph of that commit's
  message. The forge credits the account with that email verified. An apply
  that wrote nothing, or stopped at `needs_confirmation`, carries no trailer.
- **A plan can leave the process that made it.** `transform export` returns the
  stored plan as a document, and `transform apply --plan-file` applies one in a
  checkout whose plan store has never seen it. That path needs a repo root and
  nothing else: no store, no connector, no cache, no dbt, no jinja, no SQL
  parser, no network. The only repository-controlled content it reads is
  `dbt_project.yml`, parsed with YAML's non-constructing loader, so it can learn
  where files may live.

  The document carries a digest, and what that digest is worth is stated rather
  than implied. It proves the document is internally consistent: recompute every
  content hash from the content carried, recompute the digest from those, and a
  byte changed anywhere fails. **It is not a signature and cannot be**, because
  anything that can rewrite the content can rewrite the digest beside it. The
  caller closes that gap by carrying the digest across the boundary by a route it
  trusts and passing it as `--expect-digest`, which is the one check that catches
  a document re-signed after the fact. A digest presented as tamper-proof is
  worse than no digest, since it invites skipping the pinning that does the work.

  Containment and kind placement are re-checked against the applying checkout's
  own declared surface rather than trusted from the document, for the same reason
  the preimage hashes are re-checked: the document is an artifact that crossed a
  boundary, and what it was validated against is not what it is being written
  into. Both are hard refusals, and `--confirm` does not reach them; confirmation
  is the handshake for a human edit somebody can look at and accept.

  `transform ground` and `transform classify` are the other two halves a caller
  needs before it decides: what the plan depends on and how complete that answer
  is, and what its edits actually contain. Both are repo-only and free. See
  `references/host-integration.md` for the whole seam, including the
  conformance vectors shipped for a consumer to assert its own reader against.
- `transform build` accepts `--target`, `--select` and `--verify`. The target must be `dev`
  (or the `dbt_target` named in `.dex/config.yml`); production-looking targets
  are refused outright, before the cost gate, and `--confirm` cannot override
  the refusal. On a billed connector the cost gate is priced upfront: dex runs a
  free `dbt compile` and dry-runs each compiled node through the connector's own
  estimator (the same one `explore` uses), summing the result into the
  `needs_confirmation` estimate. It is a partial floor when a cold dev target has
  not built a node's inputs yet, and degrades to no estimate (with a note) when
  dex cannot open its own connection; the ceiling and the server-side
  per-statement cap bind regardless. On BigQuery, an incremental node whose
  batch filter is bounded by a subquery (the usual watermark over
  `{{ this }}`) is an upper bound rather than a measurement: a dry run prunes
  partitions only on a constant bound, and the job prunes on the subquery's
  result when it runs. Its `per_table_bytes` line is kept, since it is still
  what the job can bill at most, and `data.upper_bounds` maps each such line to
  the column it bounds; a note, and the over-ceiling refusal, say how much of
  the estimate those lines are. dbt runs with cwd pinned to the project dir
  (relative `profiles.yml` paths resolve there, never against the caller's
  shell). When
  `packages.yml` (or a `dependencies.yml` with packages) is declared and
  `dbt_packages/` is missing, `dbt deps` runs automatically first; `transform
  deps` is the explicit install/refresh. A missing dev DuckDB database is an
  actionable refusal when the project reads from sources (seed it first), and a
  warning otherwise. On failure the envelope's `errors[0]` carries the first
  real dbt message; the rest land in `warnings`, per-entry capped, deduplicated,
  with a pointer to the full log when anything was trimmed.

  Each node in `data.nodes` carries dbt's `unique_id` alongside a readable
  `name`. The two are distinct because a generic test's id ends in a content
  hash, so a name taken from the last segment reads as `3249b83c15`; a green
  build reporting seventeen warning tests has to be able to say which
  seventeen.
- **`transform build`'s `success` is dbt's exit code, and `data.outcome` is what
  the run established.** The two are not the same question and the gap between
  them is not small: a build whose `--select` matched no nodes at all exits zero,
  and so does a build of a model the change never touched. `success` keeps
  meaning exactly what it meant, because that is the right meaning for a command
  line and because consumers read it. `outcome` is `validated` only when every
  node the change required ran and passed against artifacts that still describe
  the tree; otherwise it is `empty_selection`, `unrelated`, `partial`, `skipped`,
  `stale`, `failed`, or `not_run`, each of which a caller acts on differently.
  `data.evidence` is where the reasoning lives: the dbt invocation, per-node
  status and materialization and relation, what the selection asked for against
  what it matched, the manifest and run-results digests, the relations the run
  generated, and the principal errors.

  `coverage` needs to be told which change the build is for, through `--for-plan`
  (a stored plan id) or `--for-plan-file` (an exported plan document, which is
  what a build sandbox holds instead of a store). It reports the nodes the change
  required, which of them ran, and how each requirement was derived: a model edit
  requires its own node, a `schema.yml` the nodes it documents, and a semantic
  YAML the dbt model its semantic model sits on. Without either flag there is no
  coverage and the key is absent rather than empty, because an empty coverage
  block reads as "nothing required was built", which is the opposite of "nobody
  asked what was required".

  `selection.complete` is `null` for any selector using dbt's graph operators or
  method selectors. dex does not evaluate dbt's selector language, and a second
  implementation of it would disagree with dbt's own.

  `stale` is worth reading twice. `stale_artifacts` is the obvious version, a
  manifest older than the model sources, and it is close to unreachable from a
  build that just ran, because a successful dbt build rewrites its own manifest.
  `plan_drift` is the reachable one: a file the plan wrote no longer holds what
  the plan wrote, so the run validated a tree the plan does not describe. It
  fires on builds that otherwise look perfect.
- **`transform build --no-install-deps` refuses a missing package instead of
  installing it.** The default is unchanged and still runs `dbt deps` post-gate,
  which is what an interactive user wants and what keeps a first build from
  failing on a step the agent has no verb for. In a sandbox with no network and a
  pinned dependency set the correct outcome is the opposite, and a dbt failure to
  reach a registry names nothing useful. The refusal (`reason: prerequisite`)
  names each declared-but-uninstalled package and its version, and it fires
  immediately after the dev-target check, before any subprocess and before the
  free `dbt compile` that prices the run, on the same argument the dev-target
  check already makes: work that cannot succeed should not be priced first. A
  declaration dex cannot check by name (a git URL names a repository, and dbt
  installs under the package's own name) is reported separately rather than named
  on a guess.
- **`transform build` rebuilds a seed whose CSV header changed.** dbt loads a
  seed into the table it already built, so a header that gained or lost a
  column fails to load and every model reading the seed is skipped behind it,
  and one that reordered its columns loads each value under the wrong name
  (dbt-duckdb loads by position). On DuckDB, before the build, dex asks
  `dbt ls` which seeds the build selects (dex does not evaluate the selector,
  and a default selector in `selectors.yml` counts), compares each one's header
  with the columns of its table in the DuckDB file the dev target writes, and
  runs `dbt seed --full-refresh` for each seed that differs. `dbt ls` parses
  afresh for the target, so neither a cleaned target directory nor a manifest
  left by another target decides which table a seed loads into; it adds one dbt
  invocation to every DuckDB build whose dev database file already exists (a
  file that does not exist yet holds no seed table to compare).
  `data.full_refresh` names the rebuilt seeds (`seeds`, `success`) and a line
  in `warnings` says why; a refresh that fails stops the build, with
  `dbt seed --full-refresh failed: ...` as `errors[0]`. A dev target dex cannot
  read is named in `warnings` and the build runs as it did before the check
  existed. Billed connectors do not run the check: the refresh would spend
  warehouse time outside the spend the build settles.
- `transform build --verify` runs the `maintain verify` sweep over the nodes
  this build touched and reports it under `data.verification`, in one envelope
  with the build result. Opt-in on every connector, free ones included.

  `data.verification.ran` is always present: a build that did not verify and a
  build that verified and found nothing are different answers, and only the
  second means the models are clean. When it ran, the payload adds `scope` (the
  models this build ran), `findings` and `finding_count` in the shape `maintain
  verify` returns them, and `suppressed`, naming every finding class that did
  not run and why.

  **Model names.** A model is named the way dbt selects it, here and in `maintain
  verify`, in the build's `nodes`, and in the per-node estimate. A versioned
  model carries its version, `dim_customers.v2`, the latest one included, because
  two versions are two relations that share one `name` and either can fail, lose
  rows, or go missing on its own. The sweep's scope is the exact nodes the build
  ran, so a build of `dim_customers.v2` never judges `dim_customers.v1`. As in
  dbt's `--select`, `maintain verify dim_customers` selects every version and
  `maintain verify dim_customers.v2` selects one.

  **Findings never fail the build.** A build dbt completed is a build that
  completed; whether a `row_loss` should gate a pipeline is a caller's policy,
  so findings stay out of `errors` and never change the status. A pointer line
  in `warnings` names the count so a caller reading warnings alone still learns
  they are there. A build that failed partway still reports the build-status
  half, which is when "which node failed, and what did it take down with it" is
  worth most; row population and the declared facts are suppressed there,
  because a half-built dev target is a mix of this run's output and the last
  one's.

  **An intended expansion ranks low.** A `row_fanout` on a model whose grain
  this build proved, by its own passing `unique` or
  `unique_combination_of_columns` test or by the declared-fact count, is capped
  at `low`, with `data.grain_proven` and `data.severity_cap_applied:
  grain_proven` (#841). The finding stays listed, so a gate on `high` needs no
  allowlist for a model that fans out by design, and a later fan-out on a wrong
  key elsewhere still ranks on its counts.

  **Declared facts.** The sweep checks what each built model's declaration
  says (#496): `unit_out_of_range`, `additivity_contradicted` and
  `population_contradicted`, exactly as `maintain verify` reports them, plus a
  declared grain no `unique` test in the build already checks (a composite
  grain always), reported as `grain_broken` with `source: declaration`. A
  single-column declared grain renders as a `unique` test, which the build
  itself runs, and `null_rule: never` renders as `not_null`, so neither is
  checked twice. Every class that did not run is named in `suppressed`
  (`grain`, `unit`, `additivity`, `population`).

  `no_relation` is never reported from a build: dbt's run results are
  authoritative for the nodes it just ran, and a node it reports as `success`
  has a relation whether or not dex's source scope covers it.

  **Cost.** The row counts a verdict needs are priced during the build's own
  pricing pass, off the manifest the free `dbt compile` wrote, and folded into
  the same estimate under a `(row counts)` entry in the per-table breakdown, so
  one `--budget` covers both phases. The declared-fact statements are priced the
  same way under `(declared facts)`, and gated after the build as their own
  phase of the same reservation, never as a second budget. Only relations the warehouse keeps no row
  count for cost anything, which is any view, and a view is dbt's default
  materialization; a table's count is free metadata. On a connector with no
  cost gate every count is made exact instead, because doing so bills nothing.
  Where the counts cannot be priced upfront (a cold dev target has nothing to
  dry-run against yet) a note says so, and they are priced again after the
  build as a phase of it, drawn against the reservation the build is already
  holding rather than as a second command. A phase that does not fit returns
  `ok` with the counts priced in `data.offer`, never `needs_confirmation`: the
  build is finished and billed, and reporting otherwise would invite a caller
  to pay for it twice.

  **Read scope.** Every other command refuses the namespace dbt writes to as a
  source, so exploration can never mistake a built model for a source table.
  `--verify` also reads that namespace for the length of the command, because
  the relations dbt just wrote are its subject. It is held apart from the
  committed allowlist and bounded by its own rules:

  - It need not exist. dbt creates it on a first build, so before that build it
    reads as empty, and the build is priced on its sources as usual. A committed
    allowlist entry that names nothing is still refused.
  - It is listed after dbt has run, so a first build sees the relations it
    created, and a rebuild is judged on the row counts dbt just wrote rather
    than on the ones listed while pricing.
  - `--scope` narrows the sources and nothing else: it can neither name the dev
    namespace nor drop it from the build that is judging it.
  - An allowlist that already covers it (the whole dev database, or an empty
    allowlist) reads it once.

  The dev namespace shows in the envelope's `connection.target` while it is
  read, and nothing is written back to `.dex/config.yml`. Where the built
  relations still cannot be seen (a model built into a custom schema lands
  outside the configured dev namespace), row population is suppressed with a
  reason that names the gap rather than comparing nothing and reporting clean.
- `semantic define` refuses names that already exist in the project (use
  `update`); `update` refuses names that do not (use `define`); `semantic plan`
  accepts a mix and classifies per name, reporting `defined`, `updated`,
  `unchanged`, and `removed` in the envelope. `updated` means the definition's
  parsed content actually differs from the project's; a definition re-stated identically in the
  file that already holds it is `unchanged`. The distinction matters because a
  whole-file payload restates every definition in the file, so without it a
  two-metric change reports thirty objects as updated and the real blast radius
  is invisible in the one place a reviewer checks it. Key order and formatting
  are not changes; list order is, and identical content written to a different
  file is a move, so both read as `updated`.
- **Two payload units.** `--edits-file` carries whole files. `--definitions-file`
  carries individual definitions:
  `{"definitions": [{"kind", "path", "content"}, ...]}`, `kind` being
  `semantic_model` or `metric`, `content` that definition's YAML body (a mapping,
  written without the leading `- `). The name is read from the content, so the
  two cannot disagree. `path` is optional for a name the project has,
  defaulting to the file that declares it. For a new name it defaults to the one
  file that declares every existing definition of that kind or, when none of
  that kind exists yet, the project's only semantic file, with a warning naming
  the file, and is required when there is no such file or more than one.
  An explicit path that would relocate an existing definition is refused, because
  writing it to a second file duplicates the name. The engine writes each
  definition in place and preserves every other byte, comments included, then
  re-parses the result and compares it against what was sent. A file whose
  layout it cannot span safely (a flow-style sequence, anchors or aliases,
  multiple documents, tab indentation) is refused with `--edits-file` named as
  the way to edit it. Classification is scoped to the definitions named, so a
  spliced file's other definitions are reported in no class at all. Names
  implicitly created by `create_metric: true` measures count as existing metrics
  everywhere. Beyond MetricFlow's schemas, the engine
  resolves every metric input (ratio and derived inputs must reference metrics,
  not measures) and then runs the emitted YAML through dbt's own parser against
  a throwaway copy of the project; a plan that fails parse is never stored.
  When dbt is unavailable (or the project has no time spine yet) the parse
  degrades to a warning; `--no-parse` skips it. A stored semantic plan is applied
  with `transform apply` like any other plan (no id applies the latest unapplied
  one).
- **Removing a definition** is an entry with `"op": "delete"`, which carries the
  `name` (there is no body to read one from) and no `content`. It is always
  declared: a definitions payload never removes a definition for having gone
  unmentioned, because an unmentioned definition is what the unit promises to
  leave alone. `semantic define` refuses a removal; `update` and `plan` accept
  one, and it is reported as a fourth class, `removed`. One payload names each
  definition once, so what it asks for cannot depend on the order it is read in.
  A removal the surviving project still reads is refused, naming the reader and
  its file: a metric whose input is a removed metric, or a measure of a removed
  semantic model (`create_metric` names included). The check is over the state
  the payload leaves behind, so adding those readers' own removals or updates to
  the same payload satisfies it, in any order; what a reference index cannot
  resolve statically (a `Metric()` call inside a filter string) is left to dbt's
  parser. A removal that would leave a file declaring no semantic model or metric
  is refused as well, pointing at `transform plan --edits-file`: emptying or
  deleting a file is a file-level act, and the semantic verbs delete no files.
  The file comes back without that one definition, as an ordinary content edit,
  so the plan store, the diffs, and `transform apply` see the unit they always
  have.
- `semantic ossie define|update|plan` is the corresponding native-file route.
  Its namespace guard is the semantic-model namespace across every configured
  document: `define` rejects existing names, `update` rejects missing names,
  and `plan` reports a mixed change under `defined` and `updated`. Each edit is
  a complete configured `.ossie.yaml`, `.ossie.yml`, or `.ossie.json` document,
  one semantic model flat at its root (the earlier wrapped `semantic_model` list
  is refused with the migration named);
  Dex overlays all edits in memory and validates the prospective configured set
  before storing anything. It writes the accepted content byte-for-byte, with
  no YAML/JSON reformatting, and never changes a configured document absent from
  the payload. File removal is not part of this command.
- `semantic slayer define|update|plan` is the same route for SLayer model files.
  Each edit is one whole model file keyed as `semantic.slayer.models` names it,
  relative to `semantic.slayer.base_dir`, and nothing else under the storage root
  is writable. The prospective set is validated with the pinned release's own
  `SlayerModel` (current format `version`, unique names, path agreeing with name
  and data source, joins resolving within the set onto columns both sides have)
  and, for an edited model, a `default_time_dimension` that names a column, no
  key the pinned release does not declare at its level (SLayer ignores one, so it
  would be written and do nothing), and saved-measure formulas that parse. Every
  refusal names the file, the element, and for an unknown key the pinned
  release. Apply writes the authored bytes, all files or none, and a file
  SLayer's own storage rewrote after planning is a conflict that says so.
- `semantic cube define|update|plan` is the same route for Cube model files.
  Each edit is one whole plain-YAML file keyed as `semantic.cube.files` names
  it, relative to the repository root. A path off that list, a JavaScript
  model, `globals.py`, `cube.py`/`cube.js`, a configured file that is
  compiled-only on disk, and authored content that is compiled-only (Jinja, or
  `extends`) are refused by name, a compiled-only one with the reason the
  catalog's `compiled_only` gives. The prospective set is validated with the
  reader's own port of Cube's model rules (structure, unique names across cubes
  and views, join targets and `{member}` references resolving, view
  `join_path`s following declared joins, one join between a pair of cubes),
  and a key Cube does not declare is refused in a file the plan writes rather
  than warned about. A summing or counting cube on the one side of a join with
  no `primary_key` is a `fan_out` warning. Each cube and view an edited file
  carries is reported `defined`, `updated` or `unchanged`. Apply writes the
  authored bytes, all files or none, and a file changed after planning is a
  conflict.


Skill-to-subcommand mapping: `explore` fronts `connect`/`explore`; `transform`
fronts `transform`, `semantic`, and `viz`; `maintain` fronts the whole
`maintain` group. Within `maintain`, `snapshot` manages the baseline, `check`
plus `schema`/`volume`/`grain`/`semantic` detect drift (read-only), and
`reconcile` is the only verb that emits diffs (applied through `transform
apply`). Detection is read-only on every connector, but read-only is not free:
`schema`, `volume`, and the reference half of `semantic` read metadata and run
immediately, while `grain` and the dimension-cardinality half of `semantic` scan
the warehouse and take the `--confirm --budget` handshake on billed connectors;
`check` runs the free axes first and returns one combined estimate for the
scanning axes.

That estimate arrives as an **offer on a complete answer**
([`cost-controls.md`](cost-controls.md)), not as a pending charge. `check` and
`semantic` finish their free axes on every call and return `ok`, with the price
of the scanning axes under `data.offer`, which carries `axes` naming what the
estimate would add. `data.axes_run` names what completed, and reading both is
how a caller tells "grain found nothing" from "grain did not run", which the
status used to imply.

**A baseline reports its own coverage, and the axes only compare what it
covers.** `maintain snapshot` pins the exploration cache, and a cache is thin
whenever `explore map` stopped at its rank cutoff: past 50 objects it profiles
the top 25 and enters the rest as metadata alone. An object the baseline holds no
columns for has an *unknown* column set, not an empty one, so the schema axis
compares no columns for it rather than reporting every live column as added, and
the grain axis has no keys to probe. Both `snapshot` and the detectors warn and
name the objects, and `snapshot` reports `column_detail_count` beside
`dataset_count`, so partial coverage is never mistaken for a clean bill. Run
`explore map --full` before snapshotting to cover everything. The detectors also
warn when the baseline was pinned from a cache older than
`profile_freshness_hours`, judged on the capture time recorded in the baseline
rather than on which file was written last, so re-pinning cannot silence it.

**A changed declaration is drift, even with the SQL untouched.** A model's
declaration ([#491](https://github.com/exmergo/dex/issues/491)) records which
rows belong, what one row is and how each measure adds up, so changing it
changes what the model's numbers mean. `maintain snapshot` fingerprints it per
model and per column, one hash per field over a canonical form, so reflowing a
description or reordering a grain is not a change and no declared value lands
in `.dex/`. `maintain semantic` and `maintain check` report
`declaration_changed` (one field, with `data.change` of `changed`, `added` or
`removed`), `declaration_added` and `declaration_removed` on the semantic axis.
A change to the population, grain, additivity, unit or aggregation is `high`,
a description `low`. The comparison reads the repository and the snapshot
only, so it is free on every connector. `data.declaration_drift.checked` says
whether it ran: a baseline pinned before declarations were fingerprinted, or a
project format that does not read them, is `false` with a `reason` and a
warning, never an empty, clean-looking result. `maintain snapshot
--project-only` baselines the declarations without re-measuring the warehouse.

**`explore semantic` queries the semantic layer; `transform` and the `semantic`
group author it, and `maintain semantic` detects drift in it.** Three backends
answer the same three subcommands through one abstraction, chosen ambiently by
`semantic.vendor` and `semantic.deployment` in `.dex/config.yml` (the released
`semantic.backend` spelling of the two is still accepted) and overridable per
command with `--local` / `--api`. Those two flags name **who executes**, not which
vendor: every catalog and every result reports it as `execution` (`dex` or
`vendor`), and that is the axis the guards read. A vendor-executed backend owns the
warehouse connection, so dex never holds a statement it could price or cap, and
every hosted result carries a warning saying exactly that.

**`vendor: ossie` reads native Apache Ossie documents from the repository**, with
no dbt project and no MetricFlow in the path. It is catalog-first because the
format is: Ossie specifies interchange metadata and not a portable query runtime,
so `list` answers and `query` and `values` refuse by name rather than inventing
filter grammar, join planning, and execution semantics the document's author never
stated. `--for-dimension` refuses too, off the backend's own declared
`unavailable` block rather than off a vendor name: Ossie states no
metric-to-dimension relationship, and answering "no metric can be grouped that
way" would be a claim about the layer where the truth is that this backend was
never told. The documents are named in `semantic.ossie.files`, including for an
Ossie-only repository. They are confined to the repository, and selecting Ossie
without the `[ossie]` extra refuses and names it. dex pins the Ossie schema by
content hash rather than by the version string the document carries, and records
that hash as the layer's `format_version` in a snapshot, so a moved pin is one
`semantic_format_changed` finding rather than a layer of changed definitions. Keys
and relationship columns name fields and reach the warehouse through each
field's expression. A dataset-scoped metric is listed as `dataset.metric`. See
`references/semantic-layer.md`, and `references/ossie-compatibility.md` for what
dex accepts, checks, links, and declines to claim under that pin.

**`vendor: slayer` reads SLayer model files from the repository**, the ones named
in `semantic.slayer.models` under `semantic.slayer.base_dir`, each validated by
SLayer's own `SlayerModel` from the release the `[slayer]` extra pins. A file in
any format version but that release's current one is refused before SLayer can
migrate it, and every refusal names the file and the element. `list` answers:
each model is a semantic model, each column a dimension keyed `model.column`, and
each saved measure a `formula` metric groupable by its own model's columns, so
`--for-dimension` answers too. Measures and entities are declared `unavailable`,
because a saved measure declares no aggregation and SLayer has no entities; its
keys and joins reach `explore` through the declaration channel instead. `query`
and `values` run locally with `execution: dex`: SLayer renders the SQL as a dry
run and dex executes it through the same PII gate, relation pre-check, cost
handshake and columnar cap as the local MetricFlow backend, so SLayer never opens
a datasource, never reads a credential, and never touches the warehouse. A query's
metrics are saved measures on one model, grouped by that model's own columns, with
`--where` in SLayer's own filter syntax; an ad-hoc formula is refused. DuckDB,
BigQuery, Snowflake, Postgres and Redshift are supported, and Databricks,
ClickHouse and Trino are refused with the reason. Values SLayer caches in a model file
(`sampled_values`) are never read. See `references/semantic-layer.md`.
`maintain` reads the same models on every axis without opening a SLayer
datasource or SLayer storage: the baseline records the SLayer release and model
format version, declared keys and join `cardinality` are measured on the grain
axis, and the schema axis runs SLayer's own `validate-models` comparison on the
metadata dex captured, whose removals `maintain reconcile` plans through the
`semantic slayer update` route.

**`vendor: cube` reads Cube model files from the repository**, the exact paths
named in `semantic.cube.files`, statically and with no Node runtime, no Cube
server and no warehouse connection. Each plain-YAML cube and view is validated by
dex's own port of Cube's model rules (`CubeValidator.ts`, pinned to one Cube
release), and every refusal names the file and the element. A `.js` file, a YAML
file carrying Jinja, and a cube or view using `extends` are compiled-only: named
in the catalog's `notes` and its `compiled_only` list (`file`, `reason`,
`element`), never translated. `list` answers. Each cube is a semantic model on
the relation its `sql_table` (or a plain single-table `sql`) names, and each view
a semantic model with no relation whose members keep the cube member they expose.
Each dimension is keyed `cube.member`. Each aggregating measure is a measure and
a `simple` (or, with `rolling_window`, `cumulative`) metric, and each calculated
measure is a `ratio` or `derived` metric over the measures it references. A
metric is groupable by its cube's dimensions and those its declared joins reach,
so `--for-dimension` answers. Entities are declared `unavailable`: `primary_key`
and joins reach `explore` through the declaration channel instead, at confidence
1.0. `query` and `values` are governed: Cube has no SQL renderer outside a Cube
server, so dex asks the server at `semantic.cube.url` to render over `/v1/sql`
(planning, never executing), holds every member the query names to the server's
`/v1/meta` first, refuses a plan from a pre-aggregation, of more than one
statement, or rendered for another warehouse, binds Cube's parameters as literals
of dex's dialect, and runs the statement through the governed spine in a UTC
session, `execution: dex` (see
[Querying: Cube renders, Dex executes](cube-compatibility.md#querying-cube-renders-dex-executes)). `maintain`
runs every axis on the layer without starting a Cube server: the baseline records
`vendor: cube` and each compiled-only definition by its content (a change is
`compiled_only_changed`, never compared structurally, and a warning says so on
every run), `primary_key`s and each join's `relationship` are measured on the
grain axis, categorical dimensions are re-counted on the semantic axis, and the
schema axis holds each member's columns and type to dex's own metadata, whose
removals `maintain reconcile` plans through the `semantic cube update` route. `semantic cube define|update|plan` authors the files, and a
declared dbt mart derives a cube of its own (see `transform plan` above). Selecting the vendor without the `[cube]` extra (the dialect
engine) refuses and names it. See `references/semantic-layer.md`.

`list` costs no warehouse query on either backend, and neither does the reverse
lookup, which inverts the dimension list each metric already carries rather than
asking the layer a second question. `values` and `query` each execute one, and both
screen every dimension a request would touch before it is sent: on `query` a
flagged dimension is refused from the grouping or the filter, and on `values` it
refuses the command outright, since there is no aggregate to fall back to when the
whole output is values. Where only the name heuristic could screen a dimension, the
result says so, so weaker screening is never mistaken for evidence. The
field-by-field catalog contract, the two backends' declared asymmetries, and what
`dimension_scope` and `scoped_to` mean are in
[`semantic-layer.md`](semantic-layer.md).

`explore relationships` and `explore map` accept `--verify`, which measures each
inferred join with one aggregate overlap probe (non-null foreign keys, orphan
count) and adjusts its confidence; the result carries `verified` and
`orphan_fraction`.

When a dev or replica schema is in scope beside its sources (the connector's
configured dev namespace, or a schema that mirrors another's entities and
columns), inference finds every foreign key twice over: once inside each schema
and once across them. Both commands keep each foreign key's join to the parent
in its own schema and fold the same join to a parent of that entity in another
schema, along with every cross-schema join between two tables of one entity
(raw `customers` and the built `stg_customers` are copies, not a parent and its
child). A built
model's foreign key therefore keeps its join to the built parent beside it, which
is the one a declaration can `ref()`, and keeps a join to the raw table only when
nothing was built for that entity. The notes count what was folded.

Exploration starts bare: by default the warehouse is observed as-is, and a dbt
project in the repo earns only a discovery note. `explore profile`,
`explore relationships`, and `explore map` accept `--use-project`, which folds
the project's declared definitions into the result: `relationships` tests
become declared joins at confidence 1.0 (resolved against the connection's
inventory; a declared join that matches nothing or more than one object is
reported in `notes`, never guessed, and a declared edge wins over the same
inferred one), a semantic model's primary entity overrides the heuristic grain
(disagreements and contradicted `unique` tests land in `data_quality`), a
model's own declaration (#491) folds in as described below, and
models reachable from metric definitions rank higher alongside the configured
`ranking_hints`. The compiled manifest resolves names exactly when present;
an uncompiled project falls back to name-based resolution and says so. A
stale manifest (older than the model sources) is noted, not trusted silently.

**A model's declaration folds in on the same flag (#498).** Each profiled or
mapped object a declared model resolves to carries `declaration`: `model`,
`source` (`manifest` or `yaml`), `grain`, `grain_status`, `population` (`rule`
and `filters`, each `{column, includes, excludes}`), `columns` (each `{name,
role, unit, additivity, references}`), `elided_column_count`, and
`withheld_fields`. The key is absent rather than null where no declaration
resolved, so output without `--use-project` is unchanged.

- The declared grain is reported as `grain`, ahead of a semantic primary entity
  or a composite test, because it is the model's own statement of what one row
  is. `candidate_keys` stays measurement-only, and `grain_status` compares the
  two: `proven` (a measured key is exactly the declared grain), `contradicted`
  (a single declared column profiled with duplicates), `unmeasured`,
  `missing_columns` (the relation lacks a declared column, so the grain is not
  applied), or `unprofiled`. A data-quality note states where the grain came
  from whenever it differs from the measured one.
- A column's `references: <model>.<column>` becomes a declared join at
  confidence 1.0 with `declared_by: declaration <model>.<column>`. The #491
  writer renders a `relationships` test beside every `references`, so the usual
  case is one edge with both sources in `declaration_sources`; a test naming a
  different column for the same two relations is reported in `data.conflicts`.
- The declaration is read from the repository on every run and never written to
  the cache: a profile served from the cache is annotated as accurately as a
  fresh one, and a removed declaration clears on the next run. Over a stale
  manifest the YAML is read instead, since a declaration is plain YAML a human
  edits.
- A native semantic layer is a declaration source too, with no dbt project needed.
  Its `primary_key` is the declared grain at either arity, reported ahead of a
  measured key; an Ossie `unique_keys` entry, a SLayer `unique` flag and a dbt
  `unique` test are measured and never reported as the grain. Population, role,
  unit, additivity and `references` come from `dex` metadata (`ai_context.dex` on
  Ossie, `meta.dex` on SLayer and Cube), and a document annotated before these
  were stored reports no role until it is annotated again. The metadata is
  untrusted text: a part of the wrong shape, or outside the vocabulary, is left
  out and named in `notes`, and the note never repeats the value.
- Where a dbt model and a native layer declare one relation, the dbt declaration
  is reported whole, since a layer's is derived from it, and `notes` names both
  and any grain they disagree on.
- Declared columns are capped at 12 per object (`explore profile --columns all`
  lifts the cap there; `explore map` binds it in every mode), with the cut
  counted in `elided_column_count` and in `notes`.
- A population's values and its free-text rule are column values all the same:
  values-free mode withholds the rule and every filter value. In full mode,
  any filter on a PII-flagged column withholds the entire rule and that filter's
  values, at any confidence. The null fields are named in `withheld_fields`.

`explore relationships` and `explore map` also accept the independent
`--use-hosted-semantic-layer` opt-in. It permits a configured hosted layer read;
when that layer cannot expose physical relations (as dbt Cloud currently cannot),
dex reports the partial linkage and does not invent warehouse edges or exposure.

**The project's semantic layer folds in on the same flag**, in both directions,
and neither direction costs a warehouse query.

Every entity two semantic models share is a join the layer states outright, with
the physical key named per model, so those arrive as **declared** joins at
confidence 1.0 beside the `relationships` tests, through the same endpoint
resolution and the same never-guess rule. `declared_by` on the edge names the
entity, which is the part a reader can look up with `explore semantic list` and
the only part the edge does not already carry; a `relationships` test leaves it
unset, because it declares exactly the two columns the edge already names. An edge
both channels declare is counted once. `notes` says how many came from the layer
and, separately, how many of those name-based inference did not find, which is the
case that matters: the layer routinely joins columns that share no name.

In the other direction, each object in `data.objects` carries `semantic_models`,
the models that sit on that relation. Empty is an answer: a relation nothing in the
layer reads is a different object from one several metrics are built on, and row
counts and PII flags cannot tell them apart. Every object in view is rewritten
whenever the layer was read, so a model dropped from the layer clears rather than
leaving a stale claim. A project with no compiled semantic layer contributes
neither direction and is not an error on this path; `explore semantic list` is the
command whose subject is the layer, and it is the one that refuses by name.

**A key is ranked, and the reason travels with it.** `candidate_keys` is ordered,
tightest proven key first, so the first entry is the one `grain` elects and the
rest are alternatives rather than an unordered set a caller has to guess through.
`key_evidence` carries one entry per combination the profile considered, each with
its `columns`, a `status` of `reported` or `suppressed`, and a `reason` in the
profile's own words. The reported entries are in the same order as
`candidate_keys`, which is the invariant that keeps the two fields from drifting;
the suppressed ones follow.

Suppression exists because unique is not the same as identifying. Where one column
is unique on all but a handful of rows, every wider column in the table completes
it, and the resulting combinations are the same fact restated: the column has
duplicates. Reporting them as keys buries the real key among filler and puts a
test in scaffolded dbt on a tuple nobody meant. So they are suppressed from
`candidate_keys`, kept in `key_evidence` with the reason, and stated once in
`data_quality` alongside the counts. A caller learns from `key_evidence` that a
probe ran and found only artifacts; that a probe never ran, or was narrowed by the
budget, is what the probe's own notes say, and the two are not the same answer.

`key_evidence` is a `profile` field. `explore map` reports the best-ranked key as
`candidate_key` and is budgeted per object, so the full ranking belongs to the
command whose subject is one relation in full.

**`explore map` returns the map, not a receipt for it.** Alongside the counts,
`data.objects` carries each top-ranked object's row count, detected grain,
best-ranked candidate key (the full ranking and the reason behind each key are
`explore profile`'s `key_evidence`, which this payload deliberately does not
carry), notable columns (each with the role that earned it a place: `grain`,
`key`, `join`, or a PII flag) and data-quality findings, and `data.edges`
carries the join edges in exactly the shape `explore relationships` returns them.
It is budgeted the way `explore diagram` is budgeted: at most 25 objects kept by
rank, 12 columns per object, 40 edges, and 5 data-quality findings per object.
Every cap binds in every mode, `elided_object_count` / `elided_column_count` /
`elided_edge_count` report what each one cut, and `notes` names the count, the cap
and the way to read the rest, so a truncated answer never reads as a complete one.
`notes` is always present, so an empty list is the positive statement "nothing was
elided".

Which objects are eligible differs from `explore diagram` on purpose, and the two
commands share only the *column* selection. Every profiled object is eligible here,
including one that joins to nothing, because an isolated lookup table carrying PII
flags and an empty-table warning is exactly a finding; the diagram drops those,
since a box with no edge draws nothing. So a map can report more objects than the
diagram of the same cache draws, and the counts in the envelope always reconcile
with the objects printed beside them.

`--detail` widens what is *eligible*: every column rather than the notable ones,
and objects that were inventoried but never profiled. It lifts none of the caps.
It is deliberately not spelled `--full`, which on this command decides how much
gets scanned and therefore what the run costs; `--detail` decides only how much of
what was found comes back, and spends nothing.

**No column value crosses this envelope.** The cache holds `min_value`,
`max_value` and a value domain for the columns that earned them, and this command
does not read them: `explore profile` is where a caller asks for a value domain,
deliberately and one object at a time. PII is category and confidence, as
everywhere.

`explore map` never caps silently: past 50 objects it profiles the top
`profile_top_n` (default 25) by rank and announces the cutoff in `notes`
alongside `skipped_count` (`--full` profiles everything). `data.skipped` names
what it skipped: `count`, `by_namespace` (records of `namespace` and `count`,
largest first, each namespace being everything before an identifier's last dot),
and `objects` in rank order, highest first, each list capped at 25 with the rest
counted in `elided_namespaces` and `elided`. It is always present, so `count: 0`
on a `--full` run is the positive statement that nothing was skipped, and it
changes neither which objects are profiled nor the estimate. Objects skipped on a
re-map carry their prior profiles forward (`carried_forward_count`), each
stamped with its own `profiled_at`. A selected object whose cached profile is
still fresh (same connector, schema unchanged, profiled within
`profile_freshness_hours`, default 24; `0` disables reuse) is reused without a
re-scan and reported as `cache_hit_count`, so it never enters the cost preflight
or the billed handshake. `--refresh` forces a full re-profile of every selected
object even when the cache is fresh, for a source that changed in a way the free
metadata check cannot see. `explore relationships` and the standalone
`explore profile <objects>` reuse fresh profiles the same way
(`cache_hit_count`), so a `profile` on a table `map` just wrote is served free;
all three accept `--refresh`.

Global flags (shared resolution path): `--connector`, `--path` (DuckDB),
`--scope`, `--project` and `--dataset` (BigQuery only), `--repo-root`,
`--confirm`, `--budget`, `--session-ceiling`, `--no-session-ceiling`, and
`--values-free`.

`--values-free` runs the command in values-free mode, where no value read from a
table appears in any result, refusal, error, note or warning: `explore profile`
withholds `min_value`, `max_value` and `value_domain`, `explore query` accepts only
aggregate-only statements, an error carries a code instead of a warehouse's words,
and a command not reviewed for the mode refuses. A host pins it for every command
with `DEX_RESULT_MODE=values_free` in the environment, and no flag turns it off;
`.dex/config.yml` never selects it. Every envelope the mode produces carries
`data.result_mode: "values_free"`. The rules, the command allowlist and the
residual risks are in [`values-free-mode.md`](values-free-mode.md).

`--session-ceiling <value>` and `--no-session-ceiling` are the two answers to the
one-time cumulative-ceiling ask below. Either one writes the answer into
`.dex/config.yml` and reports the amendment as an `update` diff; they are answers
to a question about the project, not per-command overrides, which is why they are
durable and why `--budget` is unaffected by both. The amendment edits the budget
lines in place and nothing else, so the file's comments, key order, and any key
this release does not read all survive it. Both answers are the person's; a
coding agent never supplies one on its own, and nothing it reads can.

A host caps the cumulative ceiling with `DEX_SESSION_CEILING_CAP=<magnitude>` in
the environment (or `DexEngine(session_ceiling_cap=...)` on the library). There
is no flag for it and `.dex/config.yml` never sets it. The ceiling in force is
the lower of the cap and `budget.session_ceiling`, and the cap alone where the
file sets none or recorded a decline, so neither answer above and no `dex config`
write can raise it; one that leaves the ceiling in force unchanged is still
written, with a warning naming the cap. A capped process is never asked the
one-time question, a refusal over the capped remainder names the cap as its
source, and a value that is not a positive number refuses every command
(`reason: configuration`). What it protects and what it does not:
[`cost-controls.md`](cost-controls.md#a-host-set-cap-on-the-daily-ceiling).

The `config` verbs take none of these. They read and write the committed file
before any engine is built, so a run override would be accepted and then do
nothing; each one is refused by name instead, pointing at `dex config set`.

`.dex/config.yml` is found by walking up from the `--repo-root` directory (default
the shell cwd) to the enclosing git root, the way git and dbt locate their project,
so a command run from a subdirectory resolves the project's config rather than the
current directory's. The walk anchors on the config file (a subdirectory holding
only a `.dex/` cache never shadows the real config higher up) and stops at the git
root (a stray `.dex/config.yml` above the repo cannot capture the session). The
`config` verbs find the file the same way, so they read and change the config the
next command will load. With no
config found anywhere and no explicit `--connector`/`--path`, the engine refuses
and names the fix rather than defaulting to DuckDB. A committed relative
`duckdb.path` resolves against the project root the config lives in, so the same
target opens from any subdirectory; a live `--path` stays relative to the shell cwd.
`dex demo`'s positional path differs because it names a file to write rather than
one to read: a relative one resolves against the `--repo-root` directory itself
(not the walked-up project root), so the demo writes exactly what
`cd <repo-root> && dex demo` would.

`--scope` is repeatable and narrows the source allowlist for one command. Each
connector reads it in its own namespace vocabulary: a `dataset` on BigQuery, a
`schema`, `database`, or `database.schema` on Snowflake, a `catalog` or
`catalog.schema` on Databricks, a `schema` on Postgres. It is never written back
to config, so `connect test --scope X` works before a connector block exists.

Two rules make it a cost control rather than a hint, both in
[`cost-controls.md`](cost-controls.md): it narrows and never widens, and it is
honored or named in an error, never dropped. `--project` and `--dataset` are
BigQuery vocabulary and error on any other connector; DuckDB has no namespace to
scope and refuses all three (its target is `--path`).

## The query firewall

`explore query` executes SQL the agent wrote; the engine generates nothing and
only refuses or bounds. A call may carry several statements, and the gate below
runs over each of them separately: batching buys call count and nothing else.
The gate, in order:

1. **Parse, don't trust.** A single read-only SELECT per statement, structurally
   checked. Writes, DDL, PRAGMA and DESCRIBE are refused (introspection goes
   through `inventory`/`profile`), and so is multi-statement input *inside one
   string*: `"select 1; select 2"` stays refused whether it arrives alone or
   alongside other arguments. Separate arguments are the supported way to ask
   several questions, because they keep the statement boundaries explicit
   instead of asking the parser to find them. A call to a function that reads
   outside the relations a statement names is refused by name wherever it sits,
   projection, FROM or subquery: server files (`pg_read_file`, DuckDB
   `read_csv`/`read_parquet`/`read_text`, `glob`), URLs and object stores
   (ClickHouse `url`, `s3`, `hdfs`), other databases (`remote`, `mysql`,
   `postgresql`, `dblink`, BigQuery `EXTERNAL_QUERY`), and SQL held in a string
   (DuckDB `query`, Trino `system.query`, Postgres `query_to_xml`). The reason is
   `unapproved_function`. No setting lifts it, `guards.approved_functions`
   included, because a setting that could would live in a file an agent may
   write.
2. **Resolve against the cache, then against the connection.** Every table and
   column must exist in `.dex/cache.json`, because profiling is what makes the
   PII policy computable and the firewall cannot judge a column whose flags it
   does not have. That holds for every table the statement reads, not only the
   ones it projects from: a subquery under WHERE, EXISTS, IN, HAVING, a join's
   ON or ORDER BY is resolved the same way, since a table nobody profiled is
   still an oracle over its values when it is only used as a filter. What
   follows from a cache miss is not a refusal, though: the
   object is looked up in the live inventory, and if the connection has it, it is
   profiled and the query then runs. This covers the case no amount of upfront
   exploration can, a relation built since the last inventory (a model `dbt run`
   just created), which is neither profiled nor inventoried.

   Three states trigger the profile: never profiled, present but inventory-only
   (no column detail), and profiled against a column signature the warehouse has
   since changed. Age does not: a cached profile whose columns still match is
   reused however old it is, so a probe never silently becomes a re-scan.

   The scan is disclosed, never silent. The result warns, naming what was
   profiled, and `data.profiled_on_demand` lists it for a machine reader. The
   profile is a full one, same detection, same `pii_overrides`, same cache write,
   so the flags governing the query are the flags a deliberate `explore profile`
   would have produced. On a metered connector it is priced rather than implied:
   a single handshake covers profiling those objects and running the statements,
   itemized per table alongside a `(the statement itself)` entry (or one
   `(statement N)` entry each when the call carries several), and the confirmed
   call does both. A call is resolved as one set, so two statements over the same
   cold table pay for one scan rather than two. If the guard then refuses a
   statement anyway, the profile is still saved and the refusal says so, so a
   corrected query does not pay for it twice.

   An object the connection does not have is still refused, and only the
   statements that named it. The message names the connection rather than the
   cache, because no amount of profiling puts an absent object into it. A
   statement already refused is never scanned for: the objects worth profiling
   are the ones a statement that can still run will read.
   `--no-auto-profile`, or `auto_profile: false` in
   `.dex/config.yml`, restores the strict prerequisite: the original refusals
   word for word, and no connection opened to produce them.

   `explore cluster` resolves its object the same way, with one difference in
   pricing that its shape forces. Its sample statement is built from the feature
   columns, which come out of the profile, so the profile is priced first and the
   sample passes a mid-command gate afterward: a budget too small for the sample
   returns `needs_confirmation` with the profile already saved, rather than
   refusing and discarding it.
3. **Classify the projection.** Output may carry values only from columns the
   PII policy clears, which is what the profiles gathered in step 2 are for:
   the firewall cannot judge a column whose flags it does not have. Which
   aggregates carry a value out, what a sub-threshold flag does, and how a human
   clears one are in [`pii-policy.md`](pii-policy.md). Filters, join conditions,
   GROUP BY and ORDER BY are unrestricted: values flow in, not out.
4. **Bound the result.** LIMIT is clamped (default 50 rows), long cells are cut
   (default 256 chars), the payload is byte-capped (default 16 KiB), at most 10
   statements ride in one call, and every cut is announced in `notes`. A watchdog
   interrupts queries that outlive their time budget (default 30s). All of them
   are configurable under `query:` in `.dex/config.yml`; `auto_profile` sits at
   the top level instead, because `explore cluster` honors it too. The byte cap
   is the one that follows the call rather than the statement, because it is the
   one protecting agent context: a call answering ten statements must not emit
   ten times what one statement is allowed.

   In values-free mode a statement is first held to the aggregate-only shape (one
   row of allowlisted aggregates, no column-keyed GROUP BY, no DISTINCT, LIMIT,
   ORDER BY or window), before anything is opened or profiled, and its result is
   held to one untruncated row of numbers afterwards: the mode refuses rather than
   cuts. See [`values-free-mode.md`](values-free-mode.md).
5. **Record.** Every decision, allowed, refused, or failed, is appended to
   `.dex/queries.jsonl` (SQL text and counts, never result values). An allowed
   query that projected sub-threshold flagged columns records those warnings
   under `pii_warnings`, so the audit trail keeps every such projection findable.
   A decision that profiled first records the objects under `profiled_on_demand`
   (and `profile_planned` on a `needs_confirmation`), so a scan is as findable in
   the ledger as a spend. One entry per statement whatever the call carried; a
   call carrying several adds `batch_index` and `batch_size` to each and gives
   them all one timestamp, so an auditor can see that six statements were one
   authorization event rather than six. In values-free mode a refused or failed
   entry's `reason` is the error's code, never its words.

Results are row-major (`columns`, `cells` as a list of lists, `row_count`,
`truncated`, `notes`), which is cheaper in tokens than records and keeps the
envelope sanitizer's list-of-dicts raw-row rule intact as a backstop.

A call carrying one statement puts that shape directly in `data`. A call carrying
several puts one entry per statement under `data.results`, each with the same
keys plus `index`, `status` (`ok`, `refused`, `failed`, `skipped`), `error`,
`reason`, `stage` (see the envelope rules below), and the source `line` when the
statement came from `--sql-file`;
`statement_count`, `ok_count` and `failed_count` sit beside them. A statement
that failed makes the envelope's own status `error` while every answer stays in
`data`, the same way `transform build` reports a run that failed partway: an `ok`
envelope carrying a guard refusal would be the weaker claim, and dropping the
answers to report the refusal would discard work already paid for.

`--across <ds,ds,...>` (comma-separated, repeatable) asks one question of mirrored
environments: dev, qa and prod copies of the same datasets. Every statement names
its dataset as `{ds}` and runs once per target, so this is two statements in one
call:
`dex explore query "select status, count(*) from {ds}.orders group by 1" --across dev_raw,prod_raw`.
They are ordinary statements to everything downstream: each is adjudicated by the
firewall against its own relation's profile and PII flags, and the whole call is
priced in one handshake and profiled once. Each entry in `data.results` adds
`target` and `template` (which of the caller's statements it came from), the
entries run template by template so the variants of one probe sit together, and
`data.across` lists the targets in the order given. A refusal names its target
(`statement 2 (target qa_raw) refused: ...`). A target must be a plain dataset
name (letters, digits, `_`, `-`, dot-separated for a qualified one), since it is
substituted into the SQL; a statement with no `{ds}`, a repeated target, and a
call whose statements times targets exceeds `query.max_statements` are refused
before anything runs. A call without `--across` reports neither key.

The caps follow the call rather than the statement where the difference matters.
`query.max_rows` and `query.max_cell_chars` bound each statement;
`query.max_payload_bytes` is the budget for the whole call, spent in statement
order, so several statements cannot emit several times what one is allowed; and
`query.max_statements` (default 10) bounds how many a call may carry at all.

## The envelope

Every command prints one object of this shape (`exmergo_dex_core.envelope`):

```json
{
  "status": "ok | not_implemented | error | needs_confirmation",
  "data": {},
  "cost": {
    "estimate": null,
    "ceiling": null,
    "paradigm": "bytes_scanned | compute_time | db_load | hosted | free_local | null",
    "estimate_quality": "exact | approximate | unknown | null",
    "unit": "bytes | seconds | null"
  },
  "warnings": [],
  "diffs": [],
  "errors": []
}
```

In values-free mode `data.result_mode` is `"values_free"` on every envelope, and
an error envelope adds `data.code`, a stable code that quotes no value (see
[`values-free-mode.md`](values-free-mode.md)). Outside the mode neither key is
present.

Where a host pinned a cap on the daily ceiling, every envelope carries
`data.session_ceiling_cap`: `{"value", "unit", "source"}`, with `source` one of
`environment` (`DEX_SESSION_CEILING_CAP`) or `host` (`DexEngine`), and `unit`
null where the command resolved no connector. Without a cap the key is absent.

Rules the envelope enforces, all of them Tier-2 eval targets:

- **Cost before spend.** `cost` is a preflight estimate, and any command that
  would spend returns `needs_confirmation` until it is confirmed and budgeted.
  The handshake in full is in [`cost-controls.md`](cost-controls.md).
- **An estimate says how much it is worth.** `estimate_quality` is `exact` on
  BigQuery, where a dry run is what the job will bill, and `approximate` on every
  connector that models a run instead. `unknown` means dex tried to price the
  work and could not, so only the ceiling and the server-side per-statement cap
  are still binding; `null` means nothing was priced at all. Those last two are
  different states, and a caller that collapses them admits an unpriced command
  believing it was priced. `unit` spells out what the magnitude counts, derived
  from the paradigm so no command can report bytes and label them seconds.
- **A priced phase the caller did not request is an offer, not a refusal.** When
  a command's free half is a complete answer in its own right, the envelope is
  `ok` and the price of the optional half sits in `data.offer`, carrying the
  same estimate, breakdown, and `--confirm --budget` hint a refusal would.
  `maintain check` and `maintain semantic` are the two. The spend gate is
  unchanged (nothing runs without the confirmed re-issue) and `cost.estimate`
  stays unset, so a reader can keep treating a populated `cost.estimate` on an
  `ok` as settled preflight for work that ran. `needs_confirmation` stays
  reserved for work the caller asked for and has not authorized, which is the
  only case where dex is genuinely blocked.
- **`data.spend` reports settled spend for every billed command**, including
  `transform build`, and always matches what the same command appended to the
  ledger. It carries the connector's unit (`bytes_billed` or `seconds_billed`)
  plus `session_spent_today`, which is what the next command's cumulative
  ceiling will start from. A failed build reports it too: dbt bills for the
  statements it ran before it stopped.
- **And it is the only place spend is reported.** No command puts a billed
  magnitude anywhere else in `data`, and every command that can bill carries the
  unit key whatever it settled at, zero included. Both halves are the same rule:
  a key that exists on one command and not another is worse than one that never
  exists, because at the top of `data` a missing key reads as a value, and the
  value it reads as is zero. `transform build` used to stamp `data.bytes_billed`
  as well, so a caller reading that key saw a build's spend and read a `maintain
  check` that had just scanned 0.89 GB as free. Under-reporting spend is the one
  envelope defect that breaks the cost-governance guarantee rather than annoying
  the caller, so where a figure is genuinely unavailable the key reports `null`
  and a note says why, rather than rounding an unknown down to zero. Key parity
  across `transform build`, `maintain check`, `explore query`, `explore map` and
  `explore profile` is a contract test.
- **ClickHouse's paradigm depends on its declared deployment.** Config-free
  callers retain the backward-compatible `db_load` default; an effective
  `clickhouse.deployment: cloud` uses `compute_time`. Cloud keeps seconds as the
  binding ceiling and ledger unit, and adds approximate compute-unit-hours from
  live per-replica memory plus optional USD from the configured price. Missing
  or partial capacity refuses before billed work.
- **Trino is guarded as `db_load` in cluster-seconds.** `trino.deployment:
  galaxy` is accepted by config validation, so the committed shape is final,
  but refused by `connect`, the dev-target preflight and `transform init`,
  with `compute_time` named as the paradigm it would need; a `self_hosted`
  declaration reaching a `*.galaxy.starburst.io` host is refused too.
- **Spend, the ledger, and the daily ceiling are governed by
  [`cost-controls.md`](cost-controls.md).** That file owns the
  `.dex/spend.jsonl` row shape, how reservations and settlements net, the
  cumulative `budget.session_ceiling` and its one-time ask, and what a ledger
  that cannot be read does to a billed command. This section states only what
  the envelope carries.
- **`cost.paradigm` names the connector the command ran against**, not what the
  command happened to cost. A free metadata command on BigQuery reports
  `bytes_scanned` with a null estimate, so a caller learns what a billed command
  will cost in before running one. `free_local` is DuckDB's own paradigm and a
  positive claim that the connector bills nothing; when no connector was
  resolved the field is `null` instead. A refusal carries the paradigm, the
  estimate, and the ceiling that bound it, so "was this refused over money?" is
  answerable from the structured fields without parsing the error prose.
- **A warehouse refusal names the door it came through.** A statement the
  warehouse itself refused reads `reason: execution_failure`, and on a connector
  that can tell where the refusal happened, `data.stage` says which. `plan`: the
  warehouse would not plan the statement (on BigQuery, the dry run or the job
  insert answered 400), so nothing ran, nothing was billed, and the same
  statement will be refused again until it changes. `run`: it planned, ran, and
  failed, so it may have billed, and the failure can depend on the data or on
  resources rather than on the SQL. BigQuery is the one connector that sets it
  today. The key is absent, never null, on every other error envelope: a
  refusal from a connector that cannot tell its doors apart, and every refusal
  that is not the warehouse's. A multi-statement `explore query` reports each
  statement's verdict in its own `data.results` entry, so `stage` sits there
  beside that entry's `reason`, and is null on a statement that answered, one a
  guard refused, and one whose connector named no stage, the same way `reason`
  and `error` are always present in that shape. The error text is the same
  whichever door it was, so a
  host that matched on it keeps working and one that branches on the field no
  longer has to.
- **`connection` names the target that answered.** Its connector, safe target
  coordinates, and resolution source are stamped once at the CLI boundary so
  individual command handlers cannot omit or reshape them. The sanitizer scans
  this block as well as `data`; credentials remain forbidden even if a future
  connector accidentally places one among its target coordinates.
- **Diffs, not silent writes.** Proposed changes appear in `diffs`; being there
  does not apply them. The user applies through their normal review and PR flow.
- **The config file speaks through `reason` and `warnings`.** A `.dex/config.yml`
  no command can load (it does not parse, or a value fails the schema) refuses
  every command except `config` with `reason: configuration`, naming each setting
  and its line rather than repeating the validator's own text. A key dex does not
  read, a key written twice, or a credential-like key does not refuse anything:
  each rides the envelope's `warnings`, so a typo in a setting is never a setting
  that silently does nothing. `dex config validate` reports all of them at once,
  and `dex config unset <key>` removes one.
- **Two engines on one `.dex/` are named, not refused.** The project's own pin of
  `exmergo-dex-core` is read from the nearest `uv.lock` (a registry entry) or
  `pyproject.toml` (an exact `==` requirement) at or above the dex root, up to the
  enclosing git repository. When it names a different release than the running
  engine, every envelope, the `config` verbs included, carries one warning naming
  both versions and the file. A dev segment and a local label are ignored, so two
  commits of one unreleased version agree. `cache.json` (`provenance.tool_version`)
  and `snapshot.json` (`tool_version`) record the release that wrote them, `null`
  where it is unknown, and never the `0.0.0` a build without `.git` used to stamp.
  The explore commands that fold a prior cache into their own, and every drift
  axis that reads the baseline, warn when that document came from another
  release. A stamp of `null` or `0.0.0` records no claim and is never compared.
- **No secrets, no uncleared values.** `data` is scanned before printing
  (`envelope.sanitize`): a secret-like key or a record-shaped raw-row payload is
  a hard failure, never a silent scrub. Result values appear only in `explore
  query`'s columnar payload, and only after the query firewall has proven they
  come from profiled, PII-cleared columns. `diffs` is not scanned the way `data`
  is, so a `config` diff withholds its own: any value under a credential-like
  name, including one on a context line, reads `<withheld>`. This is a
  release-blocking safety guarantee.

## Why this is the first artifact

Because every skill, test, and benchmark depends on it, the contract was locked
before the engine logic was built, and the subcommands fill in against this
fixed boundary. Exploration, authoring, and maintenance are live today; the Viz
preview is the last stub, pending the Viz integration.
