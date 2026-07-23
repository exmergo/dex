# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
The engine version is derived from the git tag and follows
[PEP 440](https://peps.python.org/pep-0440/); the plugin follows semver. A single
tag releases both in lockstep, so entries below are keyed by the engine version.

## [Unreleased]

### Fixed

- **`transform build`'s dev-namespace preflight no longer refuses or warns
  over a database/catalog/schema nothing in the project would ever write
  to** ([#110]). A project with per-layer `+schema:`/`+database:`/
  `+catalog:` config (or an equivalent `generate_schema_name` convention)
  resolves every model into its own namespace and never touches the
  connector-level `dev_dataset`/`dev_database`/`dev_catalog`/`dev_schema`
  fallback at all, yet every connector's check fired unconditionally on
  every build regardless, training users to skim past a line that, in a
  real missing-and-unwritable scenario, is the one that would explain the
  failure. Originally fixed for BigQuery's warning alone; a compiled
  manifest from a prior build already answers "does anything resolve into
  this namespace" for free, so the same check now also gates Snowflake's
  and Databricks's missing-database/catalog refusal (the more consequential
  case: those block the build outright, not just warn) and Postgres's and
  Redshift's missing-privilege refusal on `dev_schema`. Every check stays
  silent only when a manifest proves nothing targets the namespace, falling
  back to the previous unconditional behavior when no manifest exists yet
  (a project's first build).

## [1.3.0] - 2026-07-21

### Added

- **`explore semantic`: query the dbt semantic layer, locally or against dbt
  Cloud, behind one abstraction.** `explore semantic list` discovers metrics,
  dimensions, and entities; `explore semantic query` with a `--metric` and a
  `--group-by` runs a governed metric query and returns a capped, columnar
  result. Two backends answer the same commands, chosen ambiently by
  `.dex/config.yml` `semantic.backend` and overridable per command with `--local`
  / `--api`. `--local` renders the SQL with MetricFlow's `explain()` through a
  renderer-only client (MetricFlow never opens a connection or sees a credential)
  and executes it through dex's own connector, PII request-gate, SELECT-only
  assertion, and cost-before-spend handshake; it needs a dbt project and the
  `[semantic]` extra, while `list` is a pure `semantic_manifest.json` read-view
  that needs neither. `--api` queries a hosted dbt Cloud Semantic Layer over
  GraphQL with no local project required (the `[semantic-api]` extra plus a
  `DBT_SL_TOKEN`); because dbt Cloud owns the warehouse connection and executes
  server-side, dex's cost guard is structurally unavailable there, so every hosted
  result warns, explicitly, that spend is governed by the dbt Cloud environment,
  not by dex. No credential ever crosses the envelope on either backend.
  PII is gated before any query runs: locally, each grouped or filtered dimension
  resolves through the manifest to its physical column and that column's `.dex/`
  cache flag decides (honoring `pii_overrides`), so a dimension whose name reads
  clean is still refused when its column is flagged, and a profiled, cleared column
  is not re-blocked by a PII-shaped name; where the cache cannot speak, a name
  heuristic is the fail-closed floor. The local backend also pre-checks the
  rendered SQL's relations against the cached inventory and refuses, before the
  cost handshake, when the project was compiled against a namespace this
  connection does not have.
- - **`pii_overrides` gains an opt-in pattern form** (#106). Alongside the
  existing exact `column` entry, a `column_name` + `scope` entry clears a
  named column on every table whose fully-qualified identifier matches the
  `scope` glob, so one reviewed decision (for example, "this CDC export's
  `document_name` is a resource path, not a person name") no longer costs one
  config entry per table per environment on Firestore/Mongo/DynamoDB-style
  sources, where the same column exists by construction on every entity's
  table in every environment mirror. `column` and `column_name`/`scope` are
  mutually exclusive on one entry (enforced at load). The profile-time typo
  guard now covers pattern entries too: it warns when a `scope` matches
  profiled tables but none carries the named column, and stays silent when
  the scope matches no table yet, since new entities landing later under the
  same scope is the point of the pattern form. `blob_overrides` keeps its
  exact-only shape for now.
- **`transform init --layered-schemas`: per-layer schema routing out of the
  box.** The flag additionally scaffolds `models/intermediate/`, a
  `generate_schema_name` macro override, and a `dbt_project.yml` `models:`
  block with `+schema: staging|intermediate|marts`, so each layer builds into
  its own `<layer>_<target name>` schema (`staging_dev`, `intermediate_dev`,
  `marts_dev` on the dev target: sibling datasets on BigQuery, sibling schemas
  inside the dev database/catalog on Snowflake and Databricks, sibling schemas
  on Postgres and Redshift, schemas inside the target file on DuckDB). Models
  with no custom schema still land in `target.schema`. The macro also ships
  standalone as `transform macro generate_schema_name`, so an existing project
  can adopt the convention without re-initializing. The default scaffold is
  unchanged.
- **`transform init` now warns when a dev namespace already holds content.**
  A new free, metadata-only content preflight lists every namespace the new
  project would build into (the base dev namespace, plus each layer namespace
  under `--layered-schemas`) and warns, naming the namespace, the object
  count, and up to five object names, when one already contains tables or
  views, so a name collision surfaces at init (where the name is trivial to
  change) instead of as a confusing model clash mid-build. Advisory by
  design: init still succeeds, empty or absent namespaces stay silent, no
  reachable connection degrades to a single note (init remains
  credential-optional), and the probe rides each connector's free metadata
  path (BigQuery `tables.list`, Snowflake `SHOW` on cloud services, Databricks
  Unity Catalog REST, Postgres/Redshift catalog lookups, DuckDB catalog
  functions), so nothing is billed and no warehouse wakes. DuckDB's base
  namespace is exempt (the dev target is the source file); only its layer
  schemas are checked. Backing this, every adapter gains a
  `list_namespace_objects` metadata method alongside `missing_dev_namespaces`.

### Fixed

- **`explore profile` no longer flags non-string columns as EMAIL/NAME/PHONE,
  nor aggregate-count columns as PII.** PII classification is now type-aware: a
  category that cannot structurally live on a column's type is suppressed at
  classification time rather than flagged and then worked around. An integer
  `<x>_email_count` (the PII-safe derived replacement for a staging-only array
  of addresses) is no longer flagged `EMAIL` at 0.9, so the query firewall stops
  refusing value-carrying aggregates on it and its min/max are surfaced again.
  The gate is per-category impossibility, not blanket: `EMAIL`/`NAME`/`FREE_TEXT`
  are string-only, `PHONE` excludes only boolean and temporal (a phone-as-INT
  still flags), and `ADDRESS`/`GOVERNMENT_ID`/`FINANCIAL`/`LOCATION`/`DOB` keep
  flagging on numeric and temporal types where they legitimately belong (`zip`,
  `ssn`, `salary` as `INT`, `lat`/`lng` as `FLOAT`, `dob` as `DATE`). Separately,
  a non-string column whose name ends in an aggregate suffix (`_count`, `_cnt`,
  `_sum`, `_avg`, `_pct`, `_ratio`) is treated as a derived statistic and
  suppressed even for those categories, so `ssn_count` and `zip_count` no longer
  flag. `pii_overrides` still works and existing entries are untouched; they
  simply stop being necessary for this class of column (#112).
- **Snowflake integer and NUMBER columns now read as numeric everywhere the
  engine reasons about type.** Snowflake's `SHOW COLUMNS` surfaces every
  integer/NUMBER as the token `FIXED`, which matched none of the numeric type
  hints, so on Snowflake no integer column was recognized as numeric. `FIXED` is
  now a numeric hint. Beyond making the type-aware PII gate above effective on
  Snowflake, this is a visible pre-existing correction: Snowflake numeric columns
  now surface min/max in profiles (a numeric extreme is not sensitive) and become
  eligible features for `explore cluster`, matching how every other connector's
  integers have always been treated (#112).
- **`explore query` now allows `COUNTIF(cond)` over a PII-flagged column.**
  `COUNTIF`/`COUNT_IF` (BigQuery, Snowflake, DuckDB) releases exactly what
  `COUNT(*) FILTER (WHERE cond)` already released a row count, with the
  condition never crossing the envelope so the firewall now treats it as a
  measuring aggregate instead of refusing it as value-carrying. This closes a
  dialect gap: BigQuery has no `FILTER (WHERE ...)` clause, so `COUNTIF` was
  its only batched filtered-count spelling, and it was the one form the
  firewall still refused. The refusal message's example list and the probe
  playbook now name `COUNTIF` alongside `COUNT` (#105).
- **`explore profile` no longer scans blob-type columns by default.**
  `BYTES`/`BLOB`/`bytea`/`BINARY` columns, scalar or repeated, are excluded
  from the aggregate scan across every connector (DuckDB, BigQuery,
  Snowflake, Databricks, Postgres, Redshift): their profile can only ever be
  a null fraction and a distinct estimate, yet a columnar engine bills for a
  column's full stored bytes once it is referenced at all, so blob-heavy
  tables had these columns dominating scan cost for negligible signal.
  Excluded columns are named in the dataset's `data_quality` notes, the same
  convention `explore cluster` already uses for excluded keys. A new
  `blob_overrides` list in `.dex/config.yml` (mirroring `pii_overrides`)
  restores real stats for a specific column when they matter. Every
  connector's `profile_estimate` reflects the same exclusion, so the
  pre-execution cost estimate matches what the pruned scan actually runs
  (#108).
- **`explore query` now resolves CTE aliases across set operations.** `WITH`
  clause relations attached to `UNION`, `INTERSECT`, or `EXCEPT` roots are
  registered before either branch is inspected, including later CTEs that
  reference earlier ones. Multi-CTE probes no longer misdiagnose query-local
  aliases as tables missing from the `.dex` cache (#117).

## [1.2.2] - 2026-07-18

### Changed

- **`explore map` and `explore relationships` skip re-profiling an object whose
  cached profile is still fresh.** Before scanning a selected object, each
  command now checks `.dex/cache.json` for a same-connector profile of that
  exact object that was profiled within a freshness window and whose column
  signature (name, type, nullability) still matches the warehouse's free
  metadata; a match is reused wholesale instead of re-scanned, so it never
  enters the cost preflight or the billed handshake. Iterative workflows on a
  metered warehouse (map, tweak, map again) and `--verify` re-runs no longer
  re-pay the full profiling scan when nothing changed. The freshness check is
  fail-closed: a missing or unparseable `profiled_at`, a schema change, or a
  different connector re-profiles. The envelope gains a `cache_hit_count` field
  (distinct from the existing `carried_forward_count`, which covers
  below-rank-cutoff objects) and a note when reuse happened.
- **New `--refresh` flag on `explore map` / `explore relationships`** forces a
  full re-profile of every selected object even when the cache is fresh, for
  callers who know the source changed in a way the cheap metadata check cannot
  see.
- **New `profile_freshness_hours` config knob** (`DexConfig`, default `24.0`)
  sets how fresh a cached profile must be to be reused; `0` disables reuse
  (always re-profile). No cache schema change: `Dataset.profiled_at` and the
  stored column signatures already carry everything the check needs.
- Model validation's jinja stripping is parenthesis-aware: a jinja-only line
  inside parentheses (a macro rendering a whole SELECT, for example
  `from ( {{ unpivot_json_object(...) }} )`) is validated as a placeholder
  subquery instead of failing the SELECT-only parse; top-level jinja-only
  lines (a `{{ config(...) }}` header) vanish as before.

### Added

- **`transform plan` can author the two project-root config files** (#83). New
  edit kinds `project_yml` (`dbt_project.yml`) and `profiles_yml`
  (`profiles.yml`) bring project settings and connection targets into the same
  plan -> diff -> apply flow as models, schema, semantic, macro, and packages
  edits, so a project-wide config change is a reviewable, hash-pinned diff
  rather than a raw file write outside the guardrail. Each kind is pinned by
  name to the one root file it may target (and no other kind may reach those
  files), a `project_yml` edit must keep a `name` (and warns when it drops a
  `model-paths`/`macro-paths` entry that would orphan files), and both are
  gated by dbt's own parser at plan time. `profiles_yml` is secret-guarded: an
  edit is refused when it, or the file it would replace, inlines a literal
  credential, so no secret ever reaches the diff or agent context; reference
  secrets via `{{ env_var('NAME') }}`. As a side effect, the loader now carries
  root config files into its view, so edits to an existing `packages.yml` /
  `dependencies.yml` pin the real content hash instead of mis-registering as a
  create.
- **`explore query` can unnest JSON and array columns** (#78). The firewall's
  FROM clause now admits each connector's native unnest idiom (BigQuery
  `UNNEST`, Snowflake `LATERAL FLATTEN`, Databricks `LATERAL VIEW EXPLODE`,
  Postgres set-returning functions, Redshift PartiQL navigation and
  `UNPIVOT ... AT`, DuckDB `UNNEST`) when the unnested value derives from a
  column of a table the query already reads, either bare or through an
  allowlisted JSON/array function (`JSON_KEYS`, `JSON_EXTRACT_ARRAY`,
  `OBJECT_KEYS`, `jsonb_object_keys`, `jsonb_each`, and kin). Unnesting a
  subquery, another table, a literal, or a generator stays refused, and every
  column an unnest produces (values, keys, paths, offsets) inherits the source
  column's PII flags, so the reshape cannot launder a flagged value. This
  unblocks the headline schemaless-exploration probe, "which keys appear
  across every row of this JSON column".
- **A shipped dbt macro library, scaffolded, starting with
  `unpivot_json_object`** (#85). `transform macro` lists the macros dex
  ships; `transform macro <name>` proposes the macro file into the project's
  macro directory as a reviewable plan (dbt-parse-checked, applied with
  `transform apply`; re-running diffs the project's copy against the shipped
  version). `unpivot_json_object(relation, json_column, key_alias, value_alias,
  passthrough)` unpivots a dynamic-key JSON object column into one row per
  top-level key on every connector, key as a plain string, value in the
  warehouse's native semi-structured type, with BigQuery's two JSON gotchas
  (literal-only path arguments; `JSON_KEYS` recursing into nested objects by
  default) baked in. Plans gained the `macro_sql` edit kind: the editing
  surface now includes the project's macro paths, macro files are validated
  structurally and by dbt's parser, and a planned model that calls a shipped
  macro the project lacks warns with the scaffold command.

### Fixed

- **`.dex/config.yml` resolves from any subdirectory instead of silently
  defaulting to DuckDB.** Config was only ever looked for relative to the run
  directory, so a command issued from a subdirectory of a project (a scaffolded
  dbt project folder, say) found no config and fell back to a default whose
  connector is `duckdb`. The failure then surfaced far downstream as a phantom
  "config and profiles disagree about the connector" error naming a `duckdb` that
  appears in no file on disk. dex now walks up from the run directory to the
  enclosing git repository looking for the `.dex/config.yml` that owns the tree,
  the way git and dbt find their project roots, so the current directory no longer
  matters. The walk anchors on the config file (a subdirectory holding only a
  `.dex/` cache never shadows the real config higher up) and stops at the git root
  (a stray `.dex/config.yml` above the repo can never capture the session). When
  no config is found anywhere and no `--connector`/`--path` is given, dex refuses
  and names the fix rather than reading a wrong default. The skill wrapper that
  picks the install extra walks up the same way, so a subdirectory run installs
  the project's real connector, not the DuckDB on-ramp.
- **Redshift connections survive a Serverless cold start.** An idle Serverless
  workgroup resumes on first contact, and a slow resume can reset the startup
  handshake, so the first command to touch a cold workgroup failed hard while
  everything after it ran warm. The connect is now retried with backoff on the
  transient connection errors a resume produces (a wrong credential or database
  still fails immediately), across a window wide enough to cover the wake. A
  per-attempt connect timeout bounds a stalled handshake and is cleared once the
  connection is up so it never caps a later billed query's result read.
- **Relationship inference no longer floods `--verify` with generic id-column
  collisions** ([#77]). On warehouses where many unrelated tables share a
  generic id-shaped column name (the norm for Firestore/Mongo/DynamoDB-style
  CDC exports, e.g. every collection has its own `document_id`), name-based
  inference matched every such pair as a candidate join, spending real verify
  query cost confirming what was, essentially always, a naming convention
  rather than a relationship. A same-named-FK match is now withheld when its
  column name is held as a key by three or more unrelated datasets, and the
  withheld count and names are surfaced in `explore relationships`/`explore
  map`'s notes instead of silently inferring less.
- **`transform build` names dbt's real error on every connector, not just
  Snowflake** ([#76], a connector-parity follow-up to [#50]/[#55]). dbt wraps
  a failure's actual cause behind one or more generic, information-free
  headers: a per-node failure as "<Type> Error in <node> (<path>)", a
  whole-invocation fatal again in "Encountered an error:", a nested exception
  chain once more per level. For a per-node failure specifically, it also logs
  a bare progress line and a bare "Failure in <node> (<path>)" header before
  the message that actually names the cause. This shape is identical on every
  adapter (it comes from dbt_common, not a connector), but keeping only the
  first captured line let whichever of these uninformative lines happened to
  log first silently win the envelope's `errors[0]` slot, on Snowflake as much
  as BigQuery; #50/#55's own repro just never happened to hit it. The real
  cause line now rides alongside its header instead of being dropped, and
  dbt's own per-node/per-run "this is what actually failed" events are
  promoted ahead of a progress line or bare header the same way the #50 fix
  already promoted them ahead of a deprecation notice. Also fixed in the same
  pass: a stale `target/run_results.json` left over from a prior successful
  build, which a whole-invocation fatal never rewrites, is now cleared before
  each build so it can never be misreported as this invocation's node
  results; and ANSI color codes dbt bakes into its messages even under
  `--log-format json` no longer leak into the envelope.

## [1.2.1] - 2026-07-17

### Changed

- **The query firewall is confidence-aware.** A PII flag blocks projection at
  confidence 0.5 and above (`PII_BLOCK_CONFIDENCE`, a hard-coded engine
  constant, uniform across categories); a flag below the threshold projects
  with an envelope warning naming the column, category, and confidence, and
  the allowed entry in `.dex/queries.jsonl` records those warnings under
  `pii_warnings`. Every base confidence in the detector sits at or above the
  threshold, so nothing unblocks without value-shape evidence. Refusal
  messages now also point at the `pii_overrides` recovery path, and, on a
  cache written before value-shape profiling existed, suggest re-profiling.
  Min/max suppression and dbt `meta` stamping remain presence-based at any
  confidence.
- **Generic `name` flags are refined by value-shape evidence** (the standing
  over-flag reproduced on four datasets, most recently Snowflake TPC-H
  `R_NAME`/`N_NAME`/`P_NAME`). The profiling scan computes three in-engine
  shape statistics for generic `*_name` string columns (all-caps vocabulary
  fraction, given-plus-surname shape fraction, average token count) as regex
  predicates inside measuring aggregates, so only numeric fractions leave the
  engine. Evidence moves confidence in both directions and fails closed: a
  person-shaped distribution corroborates 0.6 to 0.75, a tiny closed all-caps
  vocabulary (at most 32 distinct values) or long multi-token labels de-rate
  0.6 to 0.3, and missing or ambiguous evidence changes nothing. The flag is
  never removed by evidence; detector recall is unchanged.
- **`.dex/cache.json` schema version is now 3** (column profiles gained the
  `pii_overridden` audit field, and flag confidence became load-bearing for
  the firewall). A v2 cache still loads; its stored flags keep blocking
  exactly as they did until a re-profile computes shape evidence.

### Added

- **`explore map`, `explore relationships`, and `explore profile` now emit
  periodic progress to stderr on long runs** ([#84]). Previously these commands
  produced no output until they completed or errored, so a slow profiling run
  (many objects, or `--verify` adding an overlap probe per inferred join) was
  indistinguishable from a hung one. A minimal `dex: profiled 40/90 objects`
  (and `dex: verified N/M joins` on `--verify`) line now goes to stderr as the
  slow loops advance, gated so fast runs stay completely silent. The stdout
  contract is untouched: progress goes only to stderr, never the JSON envelope.
- **`explore cluster <object>`: k-means segmentation over a bounded feature
  sample.** Discovers structure in a table without ever loading it into
  context. Cache-gated like `explore query`, so it auto-selects features from
  profiled numeric, non-PII, non-key columns (or takes an explicit
  `--features` list, where naming a PII column or a key opts it in deliberately
  and only its per-cluster mean, an aggregate, is reported). A key is never a
  feature: its mean is meaningless, and a fact table is mostly keys plus a few
  measures, so clustering on them just partitions surrogate ranges. Unique
  columns, columns that join out (per the joins `explore map` inferred), and
  columns named like a key are all excluded, and the notes name each one. The sample query scans only
  the feature columns and carries a dialect-aware sample clause (DuckDB
  `USING SAMPLE`, BigQuery/Postgres `TABLESAMPLE SYSTEM`, Snowflake `SAMPLE`,
  Databricks `TABLESAMPLE`, Redshift random top-N), so a metered warehouse
  reads a fraction and takes the same cost-before-spend handshake as the other
  scanning commands. Only aggregates cross the boundary: per-cluster sizes and
  fractions, centroids (feature means), inertia, and the silhouette score;
  with `-k` omitted the engine sweeps k and reports the silhouette it chose
  from. The sample is seeded where the dialect allows it (`cluster.sample_seed`,
  default 0; DuckDB `REPEATABLE`), because a re-drawn sample is a different
  dataset and can change the chosen k, not just the rounding. Where an engine
  has no seedable sample, nothing is invented: `sample_repeatable` is false and
  a note says the run cannot be compared to another. A cluster holding under 1%
  of the sample is called out as an outlier pocket rather than a segment, since
  it inflates the silhouette and a high score on it otherwise reads as a
  confident segmentation. scikit-learn rides behind a new `[cluster]` extra,
  lazy-imported so the light default install stays light and the explore skill
  wrapper adds it automatically for this subcommand.
- **`pii_overrides` in `.dex/config.yml`: a durable, reviewable way to clear a
  false-positive PII flag.** Each entry names a fully qualified column
  (`db.schema.table.column`, case-insensitive, no wildcards) with an optional
  reason. An overridden column's flag is suppressed at profile time (min/max
  return for safe types, no `contains_pii` in scaffolded dbt meta), the
  firewall honors the override immediately without a re-profile, drift-added
  columns in `maintain reconcile` honor it too, and the cache records which
  category the detector had matched (`pii_overridden`) as the audit trail.
  Profiling warns when an entry matches no column of a profiled table.

### Fixed

- **`explore map`, `explore relationships`, and `explore profile` now persist
  each object's profile as it completes on billed connectors** ([#75]).
  Previously the cache was written exactly once, at the very end of the command,
  after the whole profiling pass plus inference and ranking finished. When a run
  against a billed connector (BigQuery, Snowflake, Redshift, Postgres,
  Databricks) exhausted its budget partway through, the cost gate raised mid-pass
  and none of the profiling already paid for reached `.dex/cache.json` real
  spend, no cache. Each of the three commands now checkpoints every fully
  profiled object to the cache as it completes, so a run that dies at object 60
  of 90 leaves 60 objects' worth of raw profile behind, and reports how many of
  how many objects were saved. A fully successful run still overwrites the
  checkpoints with the authoritative composed cache (relationships, ranking,
  carry-forward), and the free DuckDB path is unchanged (its re-runs are free, so
  it never checkpoints).
- **`explore relationships` now folds same-lineage/replica duplicate edges
  before caching, matching `explore map`** ([#70]). `relationships` profiles
  the full inventory, so it is even more likely than `map` to pull a
  dev/replica schema into scope alongside its source; without folding, a
  cache last written by `relationships` could carry replica-duplicate edges
  that a `map` run would have folded away. The folded set now flows into both
  the envelope and the persisted cache, and a note reports how many edges were
  folded and how many objects mirror source lineage. Dev-schema matching is
  also fixed for two cases the original folding logic missed: a BigQuery-style
  qualified `dev_dataset` (`project.dataset`) is compared by its bare schema
  name, and schema names are compared case-insensitively so a lower-cased
  configured `dev_schema` still matches an upper-cased warehouse schema
  (Snowflake, Redshift).

## [1.2.0] - 2026-07-14

### Fixed

- **`explore profile` and `explore relationships` now persist their results
  to `.dex/cache.json`**, merging into any existing cache instead of
  discarding the scan they just paid for. Previously only `explore map` wrote
  the cache, so `explore query` on an already-profiled table demanded a
  second, redundant warehouse scan via `map`, and the query firewall's own
  refusal messages ("run `explore profile <table>` first") promised a path
  that did not work. The merge is keyed by identifier: refreshed datasets
  carry forward `map`'s rank score, untouched prior datasets keep their older
  `profiled_at` (and `profile` preserves prior relationships, while
  `relationships` replaces them with its authoritative full-set inference),
  `provenance.created_at` survives, and a prior cache built for a different
  connector is replaced wholesale with a loud note rather than poisoned by
  mixing. `relationships` also annotates candidate keys and grain before
  persisting, so its cached datasets match `map`'s shape. Known asymmetry:
  `relationships` does not fold same-lineage replica edges the way `map`
  does, mirroring the two commands' existing envelope behavior.

### Added

- **`--use-project`: explore can read the dbt project, on request.**
  Exploration still starts bare (default behavior is unchanged; a dbt project
  in the repo earns only a discovery note). With the flag, `explore
  relationships` and `explore map` report joins the project itself declares:
  every resolvable `relationships` test becomes a declared join at confidence
  1.0, resolved against the connection's inventory (manifest-first for exact
  physical names, with a name-based fallback when the project is not
  compiled). A declared join that matches nothing, or more than one object,
  is surfaced as a note instead of guessed. An inferred join that duplicates
  a declared one is folded into it and noted as independently confirmed.
- **Declared grain and declared-unique checks (under `--use-project`).**
  A semantic model's primary entity overrides the heuristic grain on the
  matching profiled dataset (disagreements are noted), and a profiled column
  that contradicts its declared `unique` test gets a data-quality note.
  Candidate keys stay measurement-only. `explore profile` takes the flag too.
- **Metric-aware ranking (under `--use-project`).** Models reachable from
  metric definitions feed the ranking hints alongside (never displacing) the
  configured `ranking_hints`, so metric-backing tables surface first.
  Declared joins also sharpen the existing connectivity signal.
- A stale compiled manifest (older than the model sources) is noted rather
  than trusted silently; a repo with no dbt project, several projects, or an
  unreadable one degrades to heuristics exactly as before.
- **Composite candidate-key detection in `explore profile`** ([#49]). When no
  single column proves unique, the profiler now tests a small ranked set of
  2-column combinations with exact distinct-combination counts, so fact tables
  like TPCH `LINEITEM` report their true grain (`L_ORDERKEY, L_LINENUMBER`)
  instead of "no candidate key detected; grain unknown". Pairs are pruned on a
  necessary condition (the product of the members' distinct counts must reach
  the row count), ranked id-shaped-first then smallest-product-first, and
  capped at three probes issued as one statement. Works on all connectors;
  on metered ones the probe spends only inside the already-confirmed budget and
  degrades to "grain unknown" with an explanatory note when the remaining
  budget cannot cover it. Proven composite keys flow into `candidate_keys`,
  `grain`, and downstream test scaffolding.
- **Composite grain drift in `maintain grain`.** A snapshot whose baseline
  carries a composite key now re-verifies the combination itself (estimated
  and gated like every other grain scan) and reports a combination-level
  `key_lost_uniqueness` finding; composite members are no longer checked one
  at a time, which would have fabricated findings on every run.

#### AWS Redshift

- **Amazon Redshift connector** (`[redshift]` extra), Serverless-first and
  provisioned-compatible: Postgres-catalog metadata (a `pg_class` census
  merged with `SVV_TABLE_INFO` size facts and `SVV_COLUMNS`, so empty tables
  the view omits still appear), the compute-time cost paradigm in seconds
  with an RPU-hour translation from the workgroup's base capacity (dollars
  when `redshift.rpu_price_usd` is set), the 60-second Serverless wake
  minimum floored into every estimate exactly once per command, and a
  per-statement server-side `statement_timeout` wound down to the remaining
  budget so a wrong heuristic cannot overrun the ceiling. Credential
  discovery spans both of Redshift's worlds: a pinned Serverless
  `redshift.workgroup` (or provisioned `cluster_identifier`) resolved through
  the AWS default credential chain into IAM temporary database credentials,
  the `REDSHIFT_*` environment, the committed non-secret config target
  (password via `REDSHIFT_PASSWORD`), or a dbt profile. `transform init`
  renders IAM or env-var-password dev profiles; the dev-target preflight asks
  the Postgres privilege question of the profile's user. Profiling uses
  `HLL(...)` approximate distincts (Redshift caps `APPROXIMATE
  COUNT(DISTINCT)` at three per statement, verified live) with exact
  escalation inside the confirmed budget; there is deliberately no
  sampled-profiling knob because Redshift has no TABLESAMPLE. Session
  read-only is attempted and reported honestly rather than assumed (verified
  live: Redshift accepts and enforces it), and inventory degrades with a
  named grant fix when an IAM-minted user cannot read `svv_table_info`. The
  five safety families are extended to the new connector against a stateful
  fake (`tests/fakes/redshift.py`), and `references/redshift.md` documents
  the cost story, including that Serverless bills metadata activity. The
  whole loop was verified live against a Redshift Serverless workgroup on
  both auth paths, including a keyless `method: iam` dbt build.

### Changed

- One shared read view in the engine's dbt project reader now feeds explore's
  declared joins, the semantic definitions, and `maintain snapshot`'s
  fingerprints (previously a separate parser); snapshot output is unchanged.

#### AWS Redshift

- The relationship-verification overlap probe now measures orphans with a
  LEFT JOIN against the DISTINCT parent keys instead of a `NOT EXISTS`
  projected into the SELECT list, which Redshift refuses outright (XX000:
  correlated subquery pattern not supported). Same aggregate-only result on
  every connector, same fanout safety, one dialect fewer surprises.

## [1.1.1] - 2026-07-12

### Added

- **`--scope`**, a portable, repeatable source-scope override that every
  warehouse connector reads in its own namespace vocabulary: a `dataset` on
  BigQuery, a `schema`, `database`, or `database.schema` on Snowflake, a
  `catalog`/`catalog.schema` on Databricks, a `schema` on Postgres. Nothing is
  written back to `.dex/config.yml`. A committed source allowlist is a cost
  boundary, so `--scope` may only narrow it, never widen it, and a scope that
  reaches outside is refused.
- **Source-scope validation on every warehouse connector.** BigQuery, Databricks,
  and Postgres now resolve each scope entry through their own free metadata path
  before anything is estimated, matching what Snowflake already did: a dataset,
  catalog, `catalog.schema`, or schema that names nothing is refused, and the
  message lists what does exist and names where the entry came from (the `--scope`
  or `--dataset` flag, or the allowlist in `.dex/config.yml`). `connect test`
  therefore fails for free on a bad scope.
- **A dev-target preflight on every warehouse connector.** The free check that
  runs before the cost gate now covers BigQuery, Databricks, and Postgres. What
  dbt cannot create for itself is refused; what it can create is not, and that
  lands differently per connector: dbt never creates a Databricks catalog, so a
  missing `dev_catalog` is refused with the `CREATE CATALOG` statement that fixes
  it; dbt *does* create its BigQuery dev dataset, so an absent one warns (naming
  the `bigquery.datasets.create` permission the build needs) while an unreachable
  dev project is refused; and dbt creates its Postgres dev schema only if the role
  may, so the privilege is what gets checked, asked of the role in the rendered
  profile rather than the one dex reads with, and refused with the `GRANT` that
  fixes it.
- **Snowflake scope resolution and validation.** Scopes now resolve against the
  account through free SHOW metadata before anything is estimated. A bare schema
  is qualified against the databases in scope; an ambiguous one asks for
  `database.schema`; one that names nothing is refused with the schemas that do
  exist listed. `connect test --scope <bad>` therefore fails for free.
- **A dev-target preflight before `transform build`.** It runs after the prod
  refusal and before the cost gate, and it is free, so a build that cannot
  succeed is refused before anyone is asked to weigh a budget. On Snowflake it
  refuses a missing `dev_database` and names the `CREATE DATABASE` statement that
  fixes it; dbt creates schemas but never databases, so the first build otherwise
  failed inside dbt's `list_schemas` macro with an opaque
  `002043: Object does not exist`. DuckDB's existing missing-file refusal moved
  into the same preflight unchanged.

### Fixed

- **The live Snowflake transform tests failed in CI rather than skipping.** Every
  test in that suite starts from `transform init`, which refuses a
  workload-identity connection because dbt-snowflake's profile carries no
  workload-identity provider field and so cannot authenticate that way. CI
  authenticates keylessly through GitHub OIDC, so two of the three tests asserted
  a successful init against a connection the engine refuses by design. The guard
  now covers the suite instead of a single test, and CI runs the dbt tests on a
  dedicated key-pair service user holding the same least-privilege role, so the
  coverage is restored rather than skipped. The refusal itself was correct and is
  unchanged.
- **`explore map --dataset <schema>` was accepted and silently ignored on
  Snowflake** (and, identically, on Databricks and Postgres). Scoping was
  governed solely by the config allowlist, so a nonexistent schema was accepted
  without error and the estimate spanned every table the allowlist permitted. A
  user could confirm a budget believing it bounded an eight-table schema while it
  in fact covered billion-row tables elsewhere. Scoping flags are now honored or
  named in an error, never dropped.
- **A `.dex/config.yml` edit to the dev target was inert after `transform
  init`.** `profiles.yml` was the sole source of truth thereafter, so retargeting
  `snowflake.dev_database` produced a green build against the old database.
  `transform build` now refuses when the two disagree, naming both values and
  both files. It never rewrites `profiles.yml`, which may legitimately be
  hand-edited.
- `transform build` surfaced a dbt deprecation warning (`[WARNING]
  PropertyMovedToConfigDeprecation`) as the failure cause instead of the real
  error. dbt 1.11 logs these notices before the actual failure on every
  normally-authored project, so the notice reliably won the `errors[0]` slot.
  `_collect_messages` now promotes dbt's own `MainEncounteredError` event (the
  structured summary of what actually failed) to the front and sinks
  `[WARNING]`-tagged lines to `warnings` instead. (#50)
- `semantic define`/`update`/`plan` reported `warnings: []` for a plan that
  parsed cleanly but whose YAML would go on to log dbt deprecation notices
  (e.g. `PropertyMovedToConfigDeprecation`) at `transform build`. `shadow_parse`
  only collected messages on a failed parse; it now collects them on a clean
  parse too, and the caller surfaces them as plan-time warnings instead of
  letting the author discover them for the first time at build (where they
  also poisoned the failure-error channel, #50). (#55)

### Changed

- **`--project` and `--dataset` now error on every connector except BigQuery**,
  where they remain as aliases of `--scope`. They were previously accepted and
  discarded, which is strictly worse than a refusal. `--scope` on DuckDB errors
  too: a DuckDB target is one file, selected with `--path`.
- **`--dataset` on BigQuery now narrows a committed `bigquery.datasets`
  allowlist rather than replacing it.** With no allowlist committed it still sets
  one, so the `connect test --project X --dataset Y` smoke test is unchanged.

## [1.1.0] - 2026-07-09

### Added

- **Databricks connector**, completing the planned cloud-warehouse set:
  explore, maintain, ad-hoc query, and dbt builds against Unity Catalog
  (`catalog.schema.table`), behind the `[databricks]` extra (which now
  carries dbt-databricks). Connections are discovered through the SDK's
  unified auth chain (`databricks auth login`, `DATABRICKS_*` env, or a dbt
  profile); only a coarse auth method is ever surfaced.
- The connector guards **warehouse-seconds** (DBUs and dollars alongside)
  with a deliberate client split: all metadata comes free from the Unity
  Catalog REST API, and the SQL session opens lazily on the first billed
  statement, so free commands never touch, or wake, the warehouse. Estimates
  start as an honestly labeled floor (Databricks has no dry-run and no free
  table sizes) and refine inside the confirmed budget via `DESCRIBE DETAIL`;
  every billed statement is capped server-side by `STATEMENT_TIMEOUT` wound
  down to the remaining budget, and actual seconds land in the
  `.dex/spend.jsonl` ledger.
- `transform init --connector databricks` renders a dbt-databricks `dev`
  profile: dev catalog.schema (refused when it overlaps a source scope), the
  pinned warehouse's HTTP path, one thread, and auth without a persisted
  secret (dbt's own OAuth flow for user connections, a `DATABRICKS_TOKEN`
  env reference otherwise).
- The Databricks safety-spine block (all five assertion families against a
  stateful fake Unity Catalog + DBAPI pair, including the lazy-open
  invariant), a live env-gated integration suite (`DEX_TEST_DATABRICKS_*`)
  reading the samples catalog, a scheduled `integration.yml` job
  authenticated by an OIDC federation policy (no stored keys), and
  `scripts/setup_databricks_ci.sh` automating the one-time provisioning
  (service principal, federation policy, dedicated 2X-Small serverless
  warehouse, scratch catalog, GitHub environment).

### Fixed

- `explore relationships` only recognized `_id`-style foreign key columns, so
  warehouses using a `_key` convention (dimensional surrogate keys, and
  TPC-H's own FK structure: `O_CUSTKEY`, `L_ORDERKEY`, `N_REGIONKEY`, ...)
  inferred zero joins. `_fk_stem` now recognizes `key` alongside `id`, and a
  new alias-stripping match handles TPC-H's convention of naming a foreign
  key after the child table's own alias rather than the parent's entity name
  (`L_ORDERKEY` on `LINEITEM` referring to `O_ORDERKEY` on `ORDERS`). (#45)

## [1.0.1] - 2026-07-06

### Fixed

- The `description` field in each skill's `SKILL.md` frontmatter
  (`explore`, `transform`, `maintain`) was an unquoted YAML plain scalar that
  itself contained a `: ` partway through the text (for example "DuckDB
  file: inventory..."), which a strict YAML parser reads as an ambiguous
  nested mapping and rejects. `npx skills install` uses such a parser, so it
  silently found zero valid skills and reported "No skills found" even though
  the repo installed and `/plugin install` worked. Quoted the field so the
  embedded colons are plain text.

## [1.0.0] - 2026-07-06

Release to the public

### Added

- **PostgreSQL connector**, the operational-database connector and the first
  on the db-load paradigm. Explore, transform, and maintain run against
  Postgres with the same guardrails as the billed connectors, adapted to what
  the paradigm actually protects (no dollars are billed; the guarded quantity
  is load on a production primary, in database-seconds):
  - Connection discovery, never prompting: a `pg_service.conf` entry pinned by
    `postgres.service`, `DATABASE_URL`, the `PG*` environment (resolved
    natively by libpq, including `~/.pgpass`), the committed non-secret
    `postgres.host`/`dbname` config target, or a dbt profile. Only a coarse
    auth method is surfaced; DSNs and passwords never cross the envelope.
  - Database-seconds budgets (`--budget`, `budget.ceiling`,
    `budget.session_ceiling`) through the same strict confirm handshake as the
    billed connectors. Query estimates come from the genuinely free planner
    preflight (`EXPLAIN (FORMAT JSON)`, so index-served queries are not quoted
    as full scans) and profile estimates from relation sizes, both honestly
    labeled `estimate_quality: "heuristic"`; the budget is hard-enforced
    regardless by a per-statement server-side `statement_timeout`. Actual
    wall-clock seconds land in the `.dex/spend.jsonl` ledger as
    `billed_seconds`. Sessions connect as `application_name = 'dex'`.
  - Read-only in depth: `default_transaction_read_only = on` on every session
    (autocommit, so no idle-in-transaction holds back vacuum), the SELECT-only
    guard in the postgres dialect through one execution door, an adapter that
    issues only catalog SELECTs / EXPLAIN / session SETs, and a documented
    least-privilege role shape.
  - Profiling that is deliberately light on the primary: one cheap single-pass
    aggregate batch (counts, nulls, safe min/max); distinct counts come free
    from `pg_stats.n_distinct` (never the value-carrying statistics columns),
    and near-unique keys escalate to exact `COUNT(DISTINCT)` inside the
    confirmed budget. The escalation scan also upgrades `reltuples` estimates
    to exact row counts, so uniqueness proofs and `maintain grain` verdicts
    never fabricate duplicates from a stale planner estimate. `json`/`jsonb`,
    arrays, `bytea`, and geometric types degrade to non-null counts; tables
    above `postgres.max_full_profile_bytes` profile from `TABLESAMPLE SYSTEM`.
  - `transform init --connector postgres`: a dbt-postgres dev profile from the
    discovered connection (password only as an `env_var('PGPASSWORD')`
    reference, never a value), writing to a dedicated `dev_schema` refused as
    a source, one thread. `transform build` injects the ceiling as
    `PGOPTIONS="-c statement_timeout=<ceiling>s"` (dbt has no dry-run; the
    per-statement cap is the binding cost control) and accounts per-node
    execution time into the ledger.
  - The `[postgres]` extra now carries `dbt-postgres`.
  - Testing per the established connector template: a stateful fake connection
    (catalog + pg_stats registry, size-derived EXPLAIN costs, simulated timing,
    psycopg's real `QueryCanceled` on timeout), safety-spine extensions across
    all five families for the db-load paradigm, and an env-gated live
    integration suite (`DEX_TEST_PG_DSN`) against the seeded database from
    `scripts/postgres_seed.sql`. No cloud setup script: CI runs the suite
    against a free, keyless `postgres:16` service container, and
    `scripts/setup_postgres_dev.sh` stands up the same seeded database locally
    in Docker.

### Fixed

- `explore map` replica folding now recognizes the Snowflake and Postgres dev
  schemas (`snowflake.dev_schema`, `postgres.dev_schema`); previously only
  BigQuery's `dev_dataset` fed the fold, so a mapped Snowflake dev schema
  could inflate one real foreign key into duplicate edges.

## [0.1.4] - 2026-07-06

### Added

- **Snowflake connector**, the second billed cloud connector and the first on
  the compute-time paradigm. Explore, transform, and maintain run against
  Snowflake with the same guardrails as BigQuery, adapted to the cost
  inversion (metadata is free via SHOW commands; scans bill warehouse time):
  - Connection discovery, never prompting: a `connections.toml` entry pinned
    by `snowflake.connection_name`, the default connection, `SNOWFLAKE_*`
    environment variables (including workload-identity tokens, the keyless CI
    path), or a dbt profile. Only a coarse auth method is surfaced.
  - Warehouse-seconds budgets (`--budget`, `budget.ceiling`,
    `budget.session_ceiling`) with the credit translation shown on every cost
    surface, and dollars when `snowflake.credit_price_usd` is configured.
    Estimates are an honestly labeled heuristic (`estimate_quality:
    "heuristic"`; Snowflake has no dry-run) floored by the 60-second resume
    minimum on a suspended warehouse; the budget is hard-enforced regardless
    by a per-statement server-side `STATEMENT_TIMEOUT_IN_SECONDS`. Actual
    wall-clock seconds land in the `.dex/spend.jsonl` ledger as
    `billed_seconds`, kept separate from byte entries so paradigms never sum
    together.
  - Strict warehouse pinning: billed statements run only on
    `snowflake.warehouse`; a connection-default warehouse is never spent on.
    Every session is tagged `QUERY_TAG = 'dex'`.
  - Free-path inventory and profiling estimation from SHOW metadata; batched
    aggregate profiling with semi-structured degradation (VARIANT, OBJECT,
    ARRAY, GEOGRAPHY) and opt-in `SAMPLE SYSTEM` above
    `snowflake.max_full_profile_bytes`.
  - `transform init --connector snowflake`: a dbt-snowflake dev profile from
    the discovered connection (key-pair as a path, SSO as externalbrowser, a
    password only as an `env_var` reference, never a value), writing to a
    dedicated `dev_database.dev_schema` refused as a source, one thread, on
    the pinned warehouse. `transform build` accounts per-node execution time
    into the ledger.
  - The `[snowflake]` extra now carries `dbt-snowflake` and requires
    `snowflake-connector-python>=3.17` (workload-identity support).
  - Testing per the established billed-connector template: a stateful fake
    connection with simulated timing and real connector error types,
    safety-spine extensions across all five families for the compute-time
    paradigm, an env-gated live integration suite (`DEX_TEST_SNOWFLAKE_*`)
    against `SNOWFLAKE_SAMPLE_DATA`, and a scheduled keyless CI job
    (Snowflake workload identity federation, GitHub OIDC). One-time
    provisioning automated by `scripts/setup_snowflake_ci.sh`.

## [0.1.3] - 2026-07-05

Hardening pass from the first billed-connector dogfooding sessions (BigQuery):
the full explore, transform, and maintain loop on a real warehouse.

### Added

- `packages_yml` edit kind: author the project-root `packages.yml` (or
  `dependencies.yml`) through the normal `transform plan`/`apply` contract, so
  declaring a dbt package dependency is a reviewable, hash-pinned diff like every
  other edit instead of a hand-written file outside the guardrail. The edit must
  carry a `packages:` or `dependencies:` list; writes stay confined to the dbt
  project (arbitrary project-root files are still refused).
- `connect test --project` and `--dataset` (repeatable) for BigQuery: convenience
  overrides of the config target, applied in memory only (never written to
  `.dex/config.yml`), so a first smoke test works before a `bigquery:` block
  exists. They mirror DuckDB's `--path`.

### Changed

- `explore map` folds same-lineage duplicate relationships when a dev/replica
  dataset is mapped alongside its source. A replica's models mirror source
  entities and keys, which otherwise inflated one real foreign key into source,
  replica, and cross-dataset lookalike edges; the canonical (source-schema) edge
  is kept, the duplicates are dropped, and the summary notes how many objects
  mirror source lineage. The replica schema is recognized from
  `bigquery.dev_dataset` or structurally (a matching entity and column set in a
  second schema).
- The query firewall's PII refusal now points at an unflagged column that
  plausibly carries the same readable value (for example `inventory_items.product_name`
  when `products.name` is flagged), computed from the cache. The flag itself is
  never weakened and no value is ever surfaced; only the guidance improves.

### Fixed

- dbt subprocess path doubling: with a relative `dbt_project_dir`, `--project-dir`
  and `--profiles-dir` resolved a second time against the already-pinned cwd
  (`project/project`), which broke `transform build` and the `semantic define`
  parse gate on a clean project. The engine now passes absolute dbt CLI paths, so
  a relative project dir no longer needs a hand-edit to an absolute path.
- BigQuery cost estimates now fold in the per-query billing floor (10 MiB per
  referenced table). The dry-run estimate summed raw scanned bytes and ignored
  the floor, so on small data (and fan-out commands like `maintain check`) it read
  far below what must be approved and produced a ladder of budget rejections. The
  surfaced estimate now reflects what BigQuery will bill, so the budget the agent
  proposes clears in one step.
- `maintain` no longer reports phantom dimension-cardinality drift from an
  approximate baseline. It compares an exact current count against the snapshot's
  distinct count, which for a low-cardinality categorical dimension is a
  HyperLogLog estimate; a delta within the sketch's error band is now suppressed
  as noise (the band scales with cardinality, so a genuine new category at low
  cardinality still fires, and an exact baseline still fires on any change).

- `transform init --connector snowflake` on a workload-identity connection
  now refuses with the working alternatives named (key-pair or SSO via
  `snowflake.connection_name`) instead of rendering a profile that references
  a `SNOWFLAKE_PASSWORD` that cannot exist. Stable dbt-snowflake does not
  support workload identity yet; the engine paths (explore, maintain, query)
  are unaffected. Surfaced by the first scheduled Snowflake integration run,
  where the whole suite authenticates keylessly.
- The live `connect test` assertion that no identity crosses the envelope now
  checks identity-shaped keys and credential values instead of a raw username
  substring, which false-positived when the CI username (`DEX_CI`) was a
  substring of the scratch database and warehouse names the envelope
  legitimately reports.

## [0.1.2] - 2026-07-04

### Added

- Maintain: the drift-detection and reconcile engine, closing the ETM loop. It
  compares current reality against the `.dex/snapshot.json` baseline on four
  axes and proposes the fix.
  - `maintain snapshot` captures the baseline: it pins the `.dex/` map (so the
    grain baseline is the exact-distinct verdicts `explore map` computed) plus
    per-layer fingerprints of the dbt project's definitions (file hashes,
    source declarations, semantic models and metrics with their referenced
    columns). Fingerprinting the definitions, not the compiled manifest, keeps
    the baseline stable across dbt versions. Without a cache it captures a
    metadata-only baseline and says the grain and cardinality axes have nothing
    to diff against.
  - `maintain schema` (structural: columns and tables added, dropped, retyped,
    renamed; nullability; dangling sources) and `maintain volume` (freshness:
    row counts that collapsed, emptied, or spiked) read metadata and are free
    on every connector.
  - `maintain grain` (lost key uniqueness and increased join fanout, from exact
    distinct counts and the verified overlap probes) and the categorical
    dimension-cardinality half of `maintain semantic` scan the warehouse, so on
    a billed connector they run the same `--confirm --budget` handshake as
    `explore profile`. `maintain semantic` also does the free half: definition
    changes against the baseline, dangling references, and impact analysis
    tracing warehouse drift through to the affected models and metrics.
  - `maintain check` sweeps every axis, ranked by blast radius (severity plus
    the count of impacted models and metrics). On a billed connector it is
    two-phase: the free axes complete immediately and their findings ride along
    in the `needs_confirmation` envelope with one combined estimate for the
    scanning axes.
  - `maintain reconcile` proposes the fixing edits as a stored plan of
    reviewable diffs, each tagged `mechanical` (a schema re-scaffold of a
    dex-generated staging model) or `advisory` (a decision surfaced, at most
    backed by a visibility test). Applied with `transform apply <plan-id>`, so
    the human-edit conflict handshake is inherited; reconcile itself writes
    nothing.
  - New-categorical-value detection is a cardinality delta only: no dimension
    value is ever stored in `.dex/` or surfaced in the envelope (naming a new
    value is left to a firewalled `explore query`).
- `.dex/drift.json`: a non-canonical cache of the last detection report, so
  `reconcile` reads what `check` found instead of re-scanning; axes merge across
  focused runs but are dropped when the baseline changes.

## [0.1.1] - 2026-07-04

The first cloud connector: the full explore and transform loop runs on BigQuery
with hard cost guards, alongside the existing DuckDB path.

### Added

- BigQuery adapter (`--connector bigquery`, behind the `[bigquery]` extra):
  free API-metadata inventory (never `INFORMATION_SCHEMA`), batched aggregate
  profiling with nested-type (`STRUCT`/`ARRAY`/`JSON`) handling, metadata-only
  degradation for partition-filter-required tables, and opt-in `TABLESAMPLE`
  block sampling for very large tables (`bigquery.max_full_profile_bytes`).
- Credential discovery for BigQuery: Application Default Credentials only
  (user, service account, impersonated, or federated), never a prompted or
  pasted key; the project resolves from `.dex/config.yml`, the environment,
  the ADC default, or a dbt profile, and every failure names the fix.
- Bytes-scanned cost guards: every billed command is estimated with free
  dry-runs and returns `needs_confirmation` until re-issued with
  `--confirm --budget <bytes>`; every job carries a server-side
  `maximum_bytes_billed` cap; billed bytes land in a `.dex/spend.jsonl` ledger
  (byte counts and statement hashes, never SQL text); and
  `budget.session_ceiling` bounds cumulative spend per UTC day against that
  ledger. Over-ceiling estimates are refused outright and confirmation cannot
  override them.
- `bigquery:` config block (`project`, `datasets` allowlist supporting
  qualified `project.dataset` entries such as public datasets, `location`,
  `dev_dataset`, `max_full_profile_bytes`).
- `transform init --connector bigquery`: renders a dev-only dbt profile with
  `method: oauth` (ADC, no secrets), pointed at a dedicated dev dataset that
  is refused when it collides with a source dataset; `transform build` on
  BigQuery requires `--confirm` with a `--budget`, inherits the profile's
  per-statement `maximum_bytes_billed` cap, and records billed bytes from
  dbt's run results into the spend ledger. The `[bigquery]` extra now carries
  `dbt-bigquery`.
- Live BigQuery integration suite (`tests/integration/`), gated on
  `DEX_TEST_BQ_*` environment variables and skipped otherwise, reading public
  datasets with per-query byte ceilings; a scheduled `integration.yml`
  workflow authenticates via Workload Identity Federation (no stored keys).
- Safety-spine coverage for the billed paradigm: SELECT-only in the BigQuery
  dialect (scripting, `MERGE`, `EXPORT DATA`, `CALL` refused), the
  unconfirmed-never-executes and over-ceiling-cannot-confirm guarantees, the
  server-side cap on every job, PII firewall checks for BigQuery
  value-carrying aggregates (`ANY_VALUE`, `ARRAY_AGG`, `STRING_AGG`,
  `TO_JSON_STRING`), secret-free generated profiles, and a sanitizer-checked
  capabilities payload (principal type only, never an identity).

### Changed

- Explore envelopes on billed connectors now stamp the preflight `cost` and
  report actual spend under `data.spend`; `connect test` reports the
  connector's cost paradigm and performs a real API round-trip (a stale
  credential no longer reports a healthy connection).
- The query firewall and `explore query` now parse in the active connector's
  SQL dialect instead of assuming DuckDB.
- Relationship verification probes are authored in portable SQL and transpiled
  per connector (BigQuery lacks `FILTER (WHERE ...)`).

## [0.1.0] - 2026-07-03


Hardening pass from two same-day dogfooding sessions across the full
explore/transform/semantic loop. The theme: stop layers from vouching for
something they did not fully check, and stop assuming a repo with nothing already
in it.

### Added

- `transform deps`: install or refresh dbt packages (repo-confined, no warehouse
  spend, no cost gate). `transform build` now also runs `dbt deps` automatically
  when the project declares packages but `dbt_packages/` is missing, so a project
  with dependencies builds on the first try.
- `semantic plan`: accepts a mix of new and existing names in one payload and
  classifies per name, reporting `defined` and `updated`, so one logical change no
  longer forces separate define and update calls.
- Authoritative validation for semantic plans: beyond MetricFlow's schemas, the
  engine resolves every metric input (ratio and derived inputs must reference
  metrics, not measures) and runs the emitted YAML through dbt's own parser against
  a throwaway copy of the project before the plan is stored. A plan that cannot
  parse is never stored. When dbt is unavailable the check degrades to a warning;
  `--no-parse` skips it.
- `transform plans`: list stored plans, pending and applied, newest first.

### Changed

- `explore map` no longer caps silently: past 50 objects it profiles the top
  `profile_top_n` (default 25) by rank and states the cutoff and `skipped_count` in
  the summary. On a re-map, objects outside this run's top set carry their prior
  profiles forward (`carried_forward_count`), each stamped with its own
  `profiled_at`, so coverage accumulates across runs.
- `transform apply` with no plan id applies the latest unapplied plan of any kind
  (semantic plans included), absorbing the one behavior `emit dbt` used to add.

### Removed

- The `emit` command group is gone. `emit dbt` was a redundant spelling of
  `transform apply` for semantic plans; its only distinct behavior (default to the
  latest unapplied plan) now lives on `transform apply`, so a stored semantic plan
  is applied the same way as any model plan. `emit osi` and the dormant OSI
  exporter (`exporters/`, the pinned `osi-schema.json`, and the OSI reference docs)
  are removed with it: dex reasons over the dbt project and authors into it
  directly, and does not project the model back out into other formats. This is a
  deliberate contract break, taken while pre-1.0; update any `emit dbt` call to
  `transform apply`. The base `jsonschema` dependency, which only the OSI validator
  used directly, is dropped.

### Fixed

- False "grain unknown" verdicts: approximate distinct counts could overshoot a
  genuinely unique column and hide a real key. Profiling now escalates near-unique
  columns to an exact `COUNT(DISTINCT)` (batched, read-only, bounded per table),
  and only an exact count is allowed to confirm a key or a table's grain.
- dbt subprocesses now pin their cwd to the project dir, so a relative `path:` in
  `profiles.yml` resolves inside the project instead of silently creating a stray
  empty database at the caller's shell cwd. A missing dev DuckDB database is an
  actionable refusal when the project reads from sources, a warning otherwise.
- Build failure envelopes surface the real cause: `errors[0]` carries the first
  actual dbt message, the rest land in `warnings`, deduplicated and per-entry
  capped, with a pointer to the full log when anything was trimmed (previously the
  cause was buried under kilobytes of duplicated tracebacks).

## [0.1.0a6] - 2026-07-03

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
