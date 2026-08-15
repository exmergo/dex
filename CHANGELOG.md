# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
The engine version is derived from the git tag and follows
[PEP 440](https://peps.python.org/pep-0440/); the plugin follows semver. A single
tag releases both in lockstep, so entries below are keyed by the engine version.

## [Unreleased]

### Fixed

- **`maintain check` carries each axis's findings in the command envelope**
  ([#279]). The top-level `data.findings` ranking could report drift while the
  adjacent per-axis result did not carry those findings, making an axis look
  clean to callers that grouped the report by axis. Each `data.axes` entry now
  includes its `run_at`, `scope`, `finding_count`, and `findings`. The merged
  top-level list remains ranked by severity, and a contract test proves that it
  contains exactly the same findings as the per-axis lists.
  
- **`explore profile` reports temporal continuity for date and timestamp
  columns** ([#206]). A temporal column reported a null fraction, a distinct
  count, and a min/max, and nothing said whether the range was continuous: a
  table covering 3,000 days with 2,900 distinct days looked identical to one
  covering all 3,000. Two real failure modes share exactly this signature and
  neither shows up in the row count: a half-failed load missing a
  day/partition (the total moves by a fraction of a percent, no drift
  threshold fires, but every daily aggregate for that period is wrong), and a
  date-spine defect (a rollup built from observed dates instead of a
  calendar, so zero-activity periods vanish instead of appearing as zero).

  Each eligible column now reports its detected granularity (day by default,
  month or hour where the data is clearly at that grain), `span` (periods
  between min and max, inclusive, at that grain), `distinct_periods`,
  `missing_periods` (`span - distinct_periods`), and `largest_gap` (the
  widest run of consecutive missing periods). The statistic is neutral: a
  genuinely sparse column (an event timestamp on a rare event) reports its
  numbers without being characterized as broken; interpretation belongs to
  whoever reads it, explicitly including a future drift-sweep detector this
  only lands the raw statistics for (issue #226, not built here).

  `largest_gap` rides the same aggregate batch that already fetches
  min/max/distinct, as a correlated scalar subquery (`LAG() OVER (ORDER BY
  period)`, the same "subquery inside the flat SELECT" shape the
  composite-key probe already uses) rather than a new row-returning adapter
  capability: zero extra round trip, one bounded scalar per column. Hour
  granularity is skipped for a bare calendar date (nothing to truncate to an
  hour, and BigQuery's `DATE_TRUNC` does not even accept that unit).
  Implemented across every connector (DuckDB, BigQuery, Snowflake,
  Databricks, Redshift, Postgres); gated on no PII flag at all, at any
  confidence, the same rule #204's declared-type checks already use.
  
## [1.6.5] - 2026-08-14

### Added

- **`dex demo` generates a seeded local warehouse, so a first run needs no
  credentials** ([#301]). The packaging has described a "zero-credential DuckDB
  on-ramp" since the extras were laid out, and it delivers one: a base install
  pulls no cloud client stack. The on-ramp just had no content. To see dex do
  anything at all, a stranger had to supply a warehouse, discover credentials,
  and accept a cost estimate against real data, which is the highest-friction
  possible starting point and, for a read that touches a metered connector, the
  one a cautious person is least willing to take on faith. The first run was
  doing double duty as an evaluation.

  `dex demo` builds a small e-commerce DuckDB warehouse (7 tables, 29,512 rows)
  in the directory you are standing in, plus a `.dex/config.yml` beside it, so
  everything after it runs with no flags. One command, no credentials, no cloud
  account, no network. It also lands in `dex --help`, which is where #296
  measured first contact actually happening.

  **It is seeded to be realistically broken**, because a first run that reports
  a clean bill of health teaches nothing. `order_item_id` lost its uniqueness to
  a batch loaded twice, so grain has a verdict to give; `products.sku` mixes
  numeric and md5-shaped ids from a merged catalogue, so a cast to a number
  would silently drop a tenth of the rows; `web_events.customer_id` shares the
  CRM's column name and type but none of its values, so inference proposes the
  join and `--verify` collapses it at 100% orphans rather than shipping a join
  that returns all-NULL parents and looks like it worked; `returns` is the table
  an interrupted load left empty; `orders.placed_at` is a VARCHAR holding
  timestamps and `web_events.occurred_at` a BIGINT holding epoch milliseconds,
  the two shapes #204 detects. `customers.email` and `full_name` are personal
  data the query firewall refuses to project, and `warehouse_locations.city` and
  its coordinates are PII false positives on a building, which are a designed
  behavior and cheapest to meet on data nobody minds. Five minutes in, a new
  user has seen dex refuse something and report a finding they did not know to
  look for.

  **Deterministic, because the documentation quotes it.** One pinned seed, a
  random stream restricted to primitives stable across CPython releases, and no
  wall clock anywhere: every date is measured back from a fixed anchor, so the
  file does not change overnight. A test pins a sha256 over every generated
  cell, so an edit that would move a count printed in a README fails CI instead
  of shipping documentation that disagrees with what the user sees. For a tool
  whose claim is precision, that disagreement is worse than having no quickstart.

  **Create-only, and structurally off the connector write path.** An existing
  file at the target is a refusal with no `--confirm` that can talk past it,
  because a confirmable overwrite would put a real warehouse one typo away from
  being replaced and naming another path costs nothing. No parent directory is
  ever created. A `.dex/config.yml` at or above the target is left untouched
  with a warning, and the printed commands switch to the explicit `--path`
  form, so a demo run inside someone's project cannot shadow their config with a
  second one. `--path` itself is refused rather than honored or ignored: it
  names the warehouse dex *reads* everywhere else, and this is the one command
  that writes one. The generator sits in its own module, imports `duckdb`
  directly, and reaches neither the adapter nor the SQL guard, so
  `test_read_only_duckdb_refuses_writes` keeps no branch it could have taken;
  two new safety-spine tests hold that mechanically rather than by argument, one
  scanning the package for every `duckdb.connect(` and requiring
  `read_only=True` outside the generator, the other opening a freshly generated
  file through the adapter and confirming a write is still refused.

  Rows are staged through a CSV and bulk-loaded with `COPY` rather than
  inserted as bound values, which is not a micro-optimization: DuckDB's
  per-value binding measures about 1.7k rows/s on 1.5.5 against 90k on 1.5.4,
  so an insert-based load made this command take anywhere from one second to
  eighteen depending on which release the user happened to resolve, with the
  slow number being the one a fresh install gets today. `COPY` reads at ~680k
  rows/s on both, so the command lands at ~0.2s regardless. `COPY` into an
  existing table uses that table's declared types, so nothing restates a
  schema, the staging file lives in the system temp directory rather than
  beside the target, and the loaded data is asserted cell-for-cell identical to
  the generated rows.

  Deliberately not done: no `.duckdb` committed to git (the storage format has
  broken backward compatibility before, so a stale file would fail on the first
  command a stranger runs, and binary blobs do not delta-compress, so every
  regeneration would be permanent history weight); no fixture shipped as data in
  the wheel, which the skills would then fetch per version per environment; no
  second repository to clone, which is exactly the friction this removes; and no
  download on first use, since corporate proxies are common in the environments
  this is meant to reassure and the first run is the one place that cannot
  afford to fail. A dbt project is out of scope: `transform init` already
  bootstraps one, and `dex demo` can chain into it when the demo needs to cover
  authoring or drift.

  `generate_demo_warehouse` is exported from the package, and
  `examples/quickstart.py` now builds its warehouse with it, so the library
  example and the CLI on-ramp show the same data and the packaging suite
  verifies the generator against a freshly built wheel.


- **`transform test --scaffold <model>` derives a dbt unit test from a
  model's own inputs** ([#215]). Writing a unit test by hand means restating
  every input's column set with correctly typed values before the assertion
  that is the actual point of the test even starts, and that restatement is
  mechanical: the model's own `ref()`/`source()` calls name the inputs, the
  model's own SQL names which of each input's columns it reads, and the
  exploration cache already knows their types. The scaffold now derives all
  three and emits a `unit_tests:` skeleton, planned like any other
  schema.yml edit: a `given` block per input, holding only the columns that
  input's data actually feeds into the model, not every column it has.

  Two things this deliberately never does. It never invents the expected
  output: the `expect:` block is an empty stub, on purpose, that fails until
  a human fills it in, because a fabricated expectation would pass by
  construction, which is worse than no test. And it never guesses a
  column's type: every value in a `given` row is typed from the exploration
  cache, and an input the cache does not know yet is a refusal naming it
  (`explore map` first), not a placeholder. A `select *`/`t.*` over a single
  resolvable source is expanded against the cache instead of refused, since
  that is the ordinary shape of a staging model's own source read; over more
  than one joined source, or an unqualified column with more than one in
  scope, it is refused rather than guessed at, same as an unsupported query
  shape. dbt's own parser gates the plan before it is ever stored, the same
  layering `transform macro` already uses.

- **A run directory holding exactly one `*.duckdb` file, and no config
  anywhere, is used instead of refused** ([#199]). The first two commands a
  new user tries against a bare DuckDB file both refused: no `.dex/config.yml`
  found, and `--connector duckdb` alone has no path either. Neither refusal
  was wrong on its own terms (dex must never invent a connection target), but
  one real file sitting in the directory the command was run from is not a
  phantom target; it is the single most likely thing meant, and it is the
  first thirty seconds of the zero-credential on-ramp.

  The exception stays as narrow as the rule it sits inside: only when nothing
  else named a connector at all (no config, no `--connector`, no `--path`) is
  the run directory (never recursive, never a walk up) checked for `*.duckdb`
  files. Exactly one is used, and the choice always warns, naming the file and
  the `--path`/`duckdb.path` that would make it explicit; two or more still
  refuses, now naming every candidate instead of leaving the caller to guess
  why; zero keeps today's refusal, unchanged; and a config, even one naming a
  different file, or an explicit `--connector`/`--path`, is never
  second-guessed, since something already made the honest choice this
  exception exists only to stand in for.

- **`explore profile` flags a candidate-key column that mixes value shapes**
  ([#205]). A string id column carrying two different value schemes (numeric
  ids alongside opaque hashes, or two id schemes left over from a partial
  migration or a merged upstream) profiled identically to a homogeneous one:
  nothing distinguished it. The failure mode is specific and severe, not
  cosmetic: a downstream cast to a number, or a numeric comparison, silently
  drops exactly the rows from the group it can't parse. Row counts fall by a
  few percent, every test still passes, and the loss is invisible until
  someone reconciles a total.

  A candidate-key column (single-column or a proven composite member) now
  reports its value-shape partition (numeric, UUID, fixed-length hex, or an
  unclassified remainder) when two or more shapes each hold a meaningful
  share, naming the fractions, the hex length (recognizing md5/sha1/sha256
  by their length) when it's fixed, and the consequence: casting to a number
  or comparing numerically will silently drop the non-numeric group(s). A
  homogeneous key (all one shape) or a non-key free-text column produces no
  note, matching the issue's acceptance criteria exactly.

  Computed the same way #204's declared-type checks are: fractions inside
  the already-scanned aggregate batch, at zero extra cost, gated on no PII
  flag at all, at any confidence. The hex bucket explicitly excludes
  anything the numeric pattern already claimed (a pure-digit string is valid
  hex-charset input too), which is what keeps `numeric_string_fraction`
  directly reusable unchanged and keeps the two buckets from double-counting
  the same value. Implemented across every connector (DuckDB, BigQuery,
  Snowflake, Databricks, Redshift, Postgres).

  Scoped to the issue's candidate-key eligibility branch only: the
  relationship-membership branch ("or one participating in a detected
  relationship") is only decidable after every table in a batch is profiled
  and cross-compared, in a step that runs in a different order in `map` vs.
  `relationships` and not at all in a bare `explore profile`, so it needs
  new persisted state and a new cross-command annotation pass. Filed as a
  follow-up rather than folded in here.

- **`CacheUnreadableError`**, exported from the package root and from
  `exmergo_dex_core.storage`. The sibling of `CacheRequiredError` that
  `BaselineUnreadableError` is of `NoBaselineError`: both remediate the same way,
  so the status is identical, but "nothing has been explored here" and "what was
  explored will not parse" are different facts about a deployment and only one of
  them suggests something went wrong. A host can page on the second without
  matching on prose.

  It carries no `schema_version`, unlike the baseline's, and the asymmetry is the
  one already reasoned out in `maintain/snapshot.py`: the cache's version drives a
  `<` comparison that *degrades*, where the baseline's is a membership test that
  *refuses*. A degrading version leaves no refusal for the attribute to carry.

### Fixed

- **A confirmation handshake is emitted only where spend is possible**
  ([#197], subsumes [#136]). `transform build --target dev` against DuckDB
  asked the caller to confirm spending nothing: the estimate was always
  zero, the paradigm was `free_local`, and nothing was billable, yet an
  unconfirmed run still stopped for `needs_confirmation`. A confirmation
  prompt is a scarce attention budget, human or automated, and asking for
  one where there is nothing to confirm trains a caller to click through the
  next one too, which is the one that gates real spend.

  `FREE_LOCAL` no longer reaches the confirmation or ceiling-required checks
  in the cost guard's own preflight, at every point that could raise them
  (the module-level gate `transform build` calls directly, and the stateful
  gate every other billed command shares); an unconfirmed run now proceeds
  and the envelope carries a warning naming why nothing was asked. Passing
  `--confirm` anyway is harmless and adds no note, since nothing was
  actually skipped that is worth remarking on. Over-ceiling still blocks
  regardless of paradigm: an estimate that contradicts an explicitly
  configured ceiling is the caller's own contradiction to resolve, not a
  spend question, so that check keeps running unconditionally, exactly as
  before. No metered paradigm is affected.

  `maintain semantic`/`maintain check` already returned their free findings
  as `ok` on DuckDB by construction (no adapter attaches a cost gate to a
  free connector, so the two-phase confirmation those commands build never
  triggers there); [#136]'s own scenario, a mixed free/billed result on a
  *billed* connector reading as `needs_confirmation` even with real findings
  already attached, is a distinct, larger change to the envelope's own
  pending-confirmation handling and is not made by this fix. Filed as a
  follow-up rather than folded in here.

- **A corrupt exploration cache no longer reports as a bad request** ([#249]).
  `load_cache` raises on a document it cannot parse, and pydantic's
  `ValidationError` subclasses `ValueError`, so an unreadable cache fell through
  to the CLI catch-all and was classified as `reason: request`. That tells an
  operator they typed something wrong, and tells a host to retry with different
  arguments, when the fix is a command nobody has run. It is the same defect
  `_require_baseline` fixed for the baseline in 1.6.3, on the load the storage
  contract had already flagged: *"this load has no such wrapper yet [...] raise a
  `ValueError` so the load is classifiable when it gets one."* All thirteen
  engine call sites, across `explore`, `maintain` and `transform`, now go through
  `readable_cache`, including the one `transform test --scaffold` added above:
  it types every value in a `given` row from the cache, so an unreadable one
  reached the catch-all there too.

  `explore/semantic/local.py` is deliberately **not** routed through it. Its bare
  `except Exception: return None` is documented as intentional, because a metric
  query is governed by dimension name before any SQL exists and a repo that never
  ran `explore map` can still query metrics. Routing it would make
  `explore semantic query` refuse where it currently degrades.

- **An unreadable drift report is rebuilt instead of refused** ([#249]). Same
  root cause, deliberately opposite remedy, and the one the note beside
  `DRIFT_SCHEMA_VERSION` pre-committed to: a baseline is *vouched for* and
  nothing else reproduces it, while a drift report is *derived* and
  `maintain check` regenerates it from the baseline on demand. `_stored_drift`
  treats a document that will not parse as absent, so `_record_axes` rebuilds it
  exactly as it already did for a report measured against a different baseline,
  and `reconcile` raises the `NoBaselineError` naming `maintain check` that it
  already raised for a missing one. No new error class, because neither caller
  needed one.

- **A declared join is measured, and the measurement does not revise the
  declaration** ([#163]). A relationship the project declares could not be
  verified at any budget, by any flag, from any caller: verify selected its
  probes by `kind`, and the skip happened *upstream* of `--verify`, so asking for
  verification spent nothing extra and covered nothing extra. `fanout_pairs` was
  permanently empty for a project that declares its joins, and `maintain grain`
  returned a result indistinguishable from a clean join graph. The same `kind`
  gate had since spread: the catastrophic orphan-rate finding added in 1.6.4
  ([#207]) was unreachable for exactly the cooperative case it was written for,
  because inference finds no edges where the project already declares them.

  The split this turns on is that a declaration is a claim *about the data*, and
  the overlap SQL does not care how the relationship was learned. **Measurement**
  (`verified`, `orphan_fraction`) now applies to both kinds. **Confidence
  arithmetic stays inferred-only**: demoting a declared 1.0 on a measured 0.2
  orphan rate would report a data defect as though dex had grown less sure of an
  edge the project stated. The disagreement surfaces through `orphan_findings`
  instead, with its own wording, because "the project and the warehouse disagree"
  is a different and more actionable claim than a shared name that turned out not
  to be a shared key.

  Two things this needed beyond lifting the filter. `declared_relationships()`
  was called *after* the verify handshake, so lifting the filter alone would have
  changed nothing on `explore relationships` or `explore map`; both now verify the
  merged set, which also means no measurement can be discarded by the merge rule
  that prefers a declared edge over the same inferred one, since at merge time
  nothing has been measured yet. And `probe_candidates` is now the single
  definition of what verify runs on, shared by `verify_relationships`,
  `probe_statements` and `_verify_estimate`, which previously agreed only by each
  hard-coding the same filter. Pricing N probes and issuing N+M under-reports
  spend *before* it happens, which is the one thing the cost preflight exists to
  prevent.

  Cost and scope. `--verify` now costs one additional probe per declared edge on
  a billed connector, covered by the existing handshake precisely because the
  estimate and the run select through the same function. The declared channel
  only exists under `--use-project`, so this is invisible in any fixture that
  maps without the flag. Composite keys stay excluded, explicitly rather than by
  omission: `_overlap_probe_sql` joins on the first column of each side, which
  answers about a *different* relationship and would report its orphan count as
  the join's, so composites stay unverified until the probe itself spans a key.
  And the fix is **not retroactive**, since `grain_plan` reads the baseline
  snapshot and existing snapshots hold declared joins at `verified: false`;
  fanout drift on a declared join needs a fresh `maintain snapshot` taken after
  the join was declared.

### Changed

- **`readable_cache` classifies rather than requires**, which is why there is no
  `_require_cache` mirroring `_require_baseline`. Every `load_snapshot` caller
  needs a baseline, so that helper can refuse on `None`. Absence is *legal* at
  most cache call sites: `explore profile`, `explore relationships` and
  `explore map` read a prior cache only to merge pre-run state and a first run
  has none, `maintain snapshot` falls back to a metadata capture, and
  `_baseline_warnings` merely skips a warning. `None` is returned unchanged and
  every caller keeps the absence policy it already had.

- The refusal names the cost. `maintain snapshot` is free on every connector, so
  the baseline's remedy can say "just re-run it"; `explore map` re-profiles the
  warehouse and **bills**. An operator choosing between investigating a corrupt
  document and replacing it needs that said before they run it.

## [1.6.4] - 2026-08-13

### Added

- **A verified join with a catastrophic orphan rate is now a finding, not
  only a demoted confidence** ([#207]). `explore relationships --verify`
  already measured an overlap probe per inferred join and stored the
  result as `orphan_fraction`, but when two "matching" columns shared zero
  actual values the only consequence was a lower confidence number buried
  in a list of edges: nothing in `notes`, `warnings`, or a dataset's
  `data_quality` said "these two columns are named alike and are not the
  same key." That is the exact failure a reader skimming several edges
  misses, and the cost is specific: a model joins on the same-named
  column, every parent-side attribute comes back `NULL`, and it looks like
  it worked.

  A verified inferred join at or above a 90% orphan rate now produces a
  finding naming both sides and the measured fraction, in `notes` (so a
  caller reading only the summary still sees it) and mirrored onto the
  child dataset's `data_quality` (so it survives into the cache for
  anything reading profiles later, not only this one command's output).
  90% sits well above the confidence-demotion tier (which already starts
  at 20%), so this fires only for the catastrophic case, not every
  weakened guess; a join that was never verified reports nothing, since
  nothing was measured. No change to inference or to the confidence
  arithmetic itself, purely surfacing what was already computed.
- **A project format can say where its edits land, and which paths it owns**
  ([#257], [#258]). `maintain reconcile` read the project seam's write tier and
  then narrowed again on `isinstance(editable, DbtProject)`, so a format that
  implemented tier 3 in full and passed the shipped conformance suite got exactly
  what a format declining the tier got. Underneath that, the paths reconcile
  proposed were literals (`models/staging/stg_<table>.sql` and its `.yml`) built
  before the format was consulted, so a second format was handed edits naming
  files it does not have. The two are one seam: those paths are keys into the
  view the format's own `load()` returned, so a format that answers where an edit
  goes supplies both halves.

  `PlacingProject` is that seam, beside `EditableProject` rather than on it: the
  tiers are `runtime_checkable`, so a method added to tier 3 would demote every
  format that has not implemented it yet, closing the write path for exactly the
  implementers who were already passing. `edit_path(kind, model)` answers where
  an edit of a kind lands and may answer `None` to decline that kind, which is
  the honest answer for a format whose models are reduced from a running graph
  (no authored staging model) but whose declared keys are hand-written files
  (a `unique` test lands fine). `editing_surface()` declares the region those
  paths must stay inside. The write gate now asks for the capability instead of
  the class.

  dbt reaches identical behaviour by the new route: `edit_path` returns the
  scaffold convention reconcile hard-coded, and `editing_surface` returns the
  project's configured model and macro paths, which is what containment checked
  directly before.

- **`transform plan` warns when a model's authored columns diverge from its
  declared schema.yml contract** ([#214]). A model's schema.yml entry is the
  closest thing a dbt project has to a column contract, and plan time is the
  cheapest moment to check it, but nothing did: a model authored with a
  different column set than its declaration passed silently. `transform plan`
  now compares the authored SELECT list against the declared columns for
  every model actually being planned, and warns in both directions: a
  declared column the SELECT does not produce, and a produced column the
  declaration does not name.

  The check never refuses. The declaration is often the side that is stale,
  and the caller is often deliberately changing the model, so a warning in
  the same envelope as the diff is what matters, not a block. A model with no
  declared columns produces no warning, since there is no contract to check
  against. Where the SELECT list cannot be resolved statically (a bare
  `select *`, a qualified `t.*`, or a macro standing in for a whole column
  with no alias), the warning says so instead of guessing; an aliased macro
  call is still resolved by its own alias. Declared columns are read with
  this same plan's own schema.yml edits overlaid on the project, so a model
  and its documentation edited together in one plan are compared against
  each other, not against a stale on-disk file.

### Changed

- **Containment validates an edit against the surface its own format declared**
  ([#257], [#258]). `transform plan` validated every edit path against dbt's
  `model_paths` whatever format produced it, so a second format placing a
  declaration sidecar was refused at plan time even with the two gates above
  resolved. Containment stays a safety property and stays mandatory; what moved
  is who declares the surface. A format that declares none is unaffected and
  validates against dbt's as before, and dbt's own path is unchanged, including
  the root manifests allowed by name and the checks that a macro and a model each
  live where dbt will find them. Escapes (absolute paths, `..`) are refused ahead
  of the surface and are not a format's to permit.

  The file an edit is pinned against now comes from the same view as the surface
  it is checked in. Those two disagreeing is not a refusal but a quiet defect:
  an existing file hashes as absent, the reviewable diff renders a one-line
  change as a whole-file create, and the apply that follows reports a conflict on
  a file nobody edited.

  That holds for agent-authored edits too, which is the whole write surface
  outside reconcile: `transform plan`, `transform macro`, and every
  `semantic define|update|plan` share one call, and it asked the engine for dbt's
  directory unconditionally. A format declaring a surface now supplies the
  directory from its own view, so a repository with no `dbt_project.yml` can
  reach those commands at all, and one with a dbt project elsewhere in the tree
  no longer pins an edit against a file the apply will not write. dbt is on the
  same path either way: its view loads the directory the engine would have named.

- **A plan is applied through the format it was planned against** ([#257]).
  `transform apply` wrote every plan with dbt's writer, which resolves each edit
  under the dbt project and re-hashes what it finds on disk, so a plan a second
  format could now store would still have been refused one stage later and the
  `write_edits` that format implemented to reach tier 3 would never have been
  called. Plans planned against dbt are written by dbt exactly as before.

  `EditableProject.write_edits` now documents that its return has to report
  `written` and `conflicts`, and `EditableProjectContract` asserts it. The apply
  path reads both to tell a refusal from a write, and a result answering neither
  fails in both directions at once: a plan recorded as applied that wrote
  nothing, or a conflict that never reaches the person it was raised for. This is
  a widening of the tier-3 contract, and a format returning
  `dbt_project.ApplyResult` (or anything exposing those two) already satisfies
  it.

- **Reconcile matches the model a format declares, not dbt's spelling of it**
  ([#258]). The `stg_` convention leaked through file contents as well as
  through paths: the `unique` test edit looked for a model named `stg_<table>`
  inside the YAML, so a format that placed its sidecar correctly and named its
  model `orders` was missed one line before the edit was built, silently. The
  model is now read from the file the format chose, which is the same string for
  dbt, whose scaffold path is `models/staging/stg_<table>.yml`. A file declaring
  no matching column entry now says so in `warnings` instead of skipping in
  silence, which was indistinguishable from dex deciding the test was already
  there.

### Fixed

- **The PII firewall decides again without a warehouse connection.** Since 1.6.3,
  `explore query` opened a connection before the guard ran: the object-gap probe
  added with the auto-profiling work took an already-open adapter, and the
  acquisition ahead of it was unguarded. Every query naming a relation therefore
  needed a reachable warehouse before the firewall could refuse anything, so a
  caller holding a profiled cache and no connector got a connector error where
  1.6.2 returned a refusal. That reaches further than an inconvenience: the guard
  reads cached PII flags and needs no warehouse to decide, so gating it on a
  connection turns a policy decision into a connectivity one and closes the
  firewall in exactly the offline environments that cannot bill for a mistake.

  The acquisition is now as tolerant as the use one level below it, where an
  unreadable column signature already "settles nothing" and falls through. A
  failed open settles nothing either. Drift detection is unchanged wherever a
  connection exists, and nothing is swallowed: a statement that passes the guard
  reaches the same opener afterwards and raises there, which is where a caller
  about to run SQL wants to hear it.

- **`explore cluster` refuses from the cache again without a connection.** The
  same probe was added to `cluster` in the same change, in front of the two
  things that command decides from the cache alone: that there is no cache at
  all, and that the named object is not in it. Both refusals sat below the
  acquisition, so both became connector errors. `auto_profile` defaults to
  `true`, which made this the ordinary path rather than an opt-in one, and
  `--no-auto-profile` the only remaining way to reach either refusal offline.
  The same fall-through applies, with the same limit: an object that *is*
  profiled still needs a connection to build its sample, so it reaches the
  opener at the bottom of `cluster` and raises there.

### Fixed

- **A BigQuery profile estimate reserves for an escalation query only where the
  probe could actually issue one, and says how much of itself is reserve**
  ([#299]). Since 1.6.0 the estimate held three per-table 10 MB floors rather
  than two: the value-domain probe added in that release
  ([#203]) took a reserve beside the near-unique and composite ones. The reserve
  scales with object count rather than data size, so on a warehouse of many
  small tables the release moved a 12-object `explore map` estimate by 125.8 MB
  in one step and started refusing a nightly refresh that had run for months.

  Three of the reserves were provably unspendable rather than merely unlikely,
  and are now dropped. A view has no row count (BigQuery reports none, and the
  profiling aggregate's own `COUNT(*)` is read per batch and never written back
  to the object), and all three probes return early without one, so a view held
  three floors no run could ever spend. Nested and repeated columns get no
  approximate distinct in the aggregate batch, and every probe's eligibility
  starts from one, so a table of nothing else can no more escalate than a blob
  column already excluded from the scan; the composite reserve was already
  conditioned on having two columns, but counted columns that can never join a
  pair. And a value domain needs at least one distinct value within a tenth of
  the non-null rows, so no column of a table below ten rows can qualify. The
  thresholds that imply that floor moved next to `ValueDomainSample` so the
  estimator and the probe cannot drift apart. Nothing else was narrowed: the
  reserve is dropped only where a probe's own guard already rules the query out,
  because an estimate that reserves for a query that cannot run is merely loose,
  while one that skips a query that can is the defect [#107] closed.

  That leaves the common case, a warehouse of small flat tables, reserving
  exactly as much as before, which is the second half of this. The confirm
  handshake and the over-ceiling refusal now split the estimate into measured
  dry-run scan and held reserve, in prose and in `reserved_bytes` /
  `reserved_queries`. A refusal previously said only "raise the budget or narrow
  the work" about a number that could be three quarters reserve, leaving the
  operator to reconstruct the split from `.dex/spend.jsonl` afterwards to find
  out whether the estimate grew because the warehouse did or because dex added a
  probe. Over-ceiling refusals reach the connector's own description of the
  estimate for the first time; before this only the confirmation payload did.

## [1.6.3] - 2026-08-10

### Changed

- **`explore query` accepts more than one statement per call** ([#265]). The
  positional was singular while `explore profile`'s beside it was already
  variadic, so N questions cost N invocations, each a fresh process re-resolving
  the connector and reloading the `.dex/` cache. That is the wrong shape for the
  common case, an agent asking a chain of small questions, whose alternative is
  an unfirewalled SQL client that answers six of them in one turn.

  `dex explore query "select ..." "select ..."` now runs both, and
  `--sql-file <path>` reads a larger batch from a file (one statement per line,
  or semicolon-separated; boundaries come from the tokenizer, so a `;` inside a
  string literal or a comment is text rather than a split, and a file whose lines
  cannot be told apart as statements is refused naming the line). A single
  statement returns the envelope it always did, byte for byte, because the two
  doors are one runner rather than two implementations. Two or more return
  `data.results`, one entry per statement carrying the familiar `columns` /
  `types` / `cells` / `row_count` / `truncated` shape plus its own `status`, so a
  refusal on the third statement does not discard the first two. The envelope's
  own status is `error` when any statement failed, with every successful result
  still present, which is how `transform build` already reports a run that failed
  partway.

  The guard does not move. Each statement is parsed, adjudicated, and ledgered on
  its own, statements are never joined into one string, and several statements in
  one argument stay refused exactly as before. On a metered connector the batch is
  priced as a batch: one estimate itemized per statement and per object, one
  `--confirm`, and the objects a whole call needs profiled are scanned once rather
  than once per statement. The per-statement gate and the server-side cap still
  bind on the way out, and the ledger keeps one line per statement, adding
  `batch_index` and `batch_size` so an auditor can see that six statements were
  one authorization event.

  Two bounds come with it, because a call that returns N results is a call that
  can flood agent context, which is what the caps exist to prevent.
  `query.max_payload_bytes` is now the budget for the whole call rather than for
  one statement, spent in statement order with what one leaves unspent available
  to the next (unchanged at 16 KB, and unchanged in effect for a single
  statement), and a new `query.max_statements` (default 10) refuses an oversized
  batch. `query.max_rows` and `query.max_cell_chars` still bound each statement.

- **`explore query` and `explore cluster` profile the object they name instead
  of refusing** ([#209]). The firewall resolves table and column references
  against the `.dex/` cache, because a taint rule over PII flags is only
  computable when the flags exist. That requirement stays. What followed from it
  did not survive contact with use: a query naming an uncached table was sent
  away to run a command whose exact argument dex was already holding, turning a
  one-step question into a three-step ritual, while the alternative in the
  caller's hand (a raw SQL client) has no steps and no firewall at all.

  An object the connection has but the cache cannot adjudicate is now profiled,
  and the statement then runs. Three states qualify: never profiled, inventoried
  without column detail, and profiled against a column signature the warehouse
  has since changed. Age deliberately does not, so a probe never turns into a
  billed re-scan because a day passed.

  The trigger is a live inventory lookup, not just a cache miss, which is what
  makes the dominant real case work. Measured on an agentic dbt benchmark
  (Claude Sonnet 5, 2,356 models), the agent adopted `dex explore` readily and
  still went to a raw `duckdb` client for about half its probes; 11 of 14 and 20
  of 29 of those raw probes targeted relations it had just built with `dbt run`.
  Such a relation is neither profiled nor inventoried, because it is newer than
  the inventory, so keying only on "inventoried but not profiled" would have
  refused the majority of ad-hoc querying.

  Nothing about the guarantee is relaxed. The profile is a full one, same
  detection, same `pii_overrides`, same cache write, so the flags governing the
  query are the flags a deliberate `explore profile` would have produced; the
  spine asserts a cache written this way is indistinguishable from one written
  the deliberate way. On a metered connector the scan is priced, not implied:
  one handshake covers profiling and the statement together, itemized per table,
  and an unconfirmed or over-ceiling call executes nothing. Every result that
  profiled says so in a warning and in `data.profiled_on_demand`, and the query
  ledger records it. Where the guard refuses the query anyway, the profile is
  still saved and the refusal says so, so a corrected query does not pay twice.

  An object the connection does not have is still refused, now with a message
  that says that rather than naming a cache no amount of profiling would fix.
  `--no-auto-profile`, or `auto_profile: false` in `.dex/config.yml`, restores
  the strict prerequisite word for word, and on that path no connection is
  opened to produce the refusal.

  `explore cluster` differs in one place, forced by its shape: its sample
  statement is built from feature columns that come out of the profile, so it
  cannot be priced up front. The profile is priced first and the sample passes
  the mid-command gate, which asks for a larger budget rather than refusing;
  the re-run reuses the saved profile and pays for the sample alone.

  This also closes the `explore query` half of the problem [#134] fixed for
  `explore semantic query --local`, and the two now share their relation
  extraction and their foreign-versus-missing verdict.

- **The three skill descriptions trigger on ticket-shaped prompts, not only on
  conversational questions** ([#267]). The trigger evals were written as
  first-person questions ("what's in my duckdb", "define a revenue metric on top
  of fct_orders"), and one of them encoded a routing rule that does not survive
  the change of register: `explore` listed "Build a staging model for the orders
  table." as a negative. That is a compound request, since you cannot write a
  staging model for a table whose columns and grain you have not inspected, so
  every description tuned against that set learned to route `explore` out of dbt
  authoring, which is most of what a real ticket asks for.

  `explore` now triggers on an unmet precondition rather than on a list of
  phrasings: you are about to write or fix SQL against tables whose columns,
  types, grain or join keys you have not verified in this session. That covers
  dbt authoring and bug-fix work, and it applies mid-task, not only at the start.
  Its sibling disclaimer is scoped to deliverable ownership: explore does not
  author the model, but it is not excluded from the task that ends in one.
  `transform` names bug-fix tickets as in scope and states its value over
  hand-editing the file, which is the real alternative. `maintain` adds
  symptom-first phrasing (a test that started failing with nobody having touched
  the model), which is how drift presents to whoever notices it. The eval sets
  gain ticket-shaped positives and their negatives are re-sorted to genuine
  near-misses: a ticket that already pins the full schema inline, or work local
  to files already in hand. Every pre-existing positive is retained.

  Measured with the `claude` CLI, Sonnet 5, xhigh effort, the skills installed as
  project skills, 3 runs per query: ticket-shaped triggering goes from 7/36 (19%)
  to 15/36 (42%), Fisher one-sided p = 0.036, consistent in direction across two
  replicates. Conversational prompts stay at 100% and negatives stay at 0% in
  both. Full-length benchmark tickets are unaffected, 0% before and after: those
  prompts suppress skill invocation whatever the description says, measured
  across 12 description variants, 5 skill names, explicit instruction, a project
  instruction file, and `append_system_prompt`. No `SKILL.md` body and no engine
  behavior changed.

### Added

- **`transform plan` attributes an edit's row-population change to the change
  that caused it** ([#266]). Planning already validated an edit against the real
  schema: column names, refs, materialization, deletions. It never compared the
  edit's *behaviour* to the behaviour of the model being replaced, so a change to
  which rows enter a model passed silently. The model compiles, the columns are
  right, every value is plausible, and the row population is different from what
  it was. On a fix or refactor ticket that is the failure mode, because the
  caller was asked to change some behaviour and not other behaviour, and nothing
  distinguished the two.

  Measured on an agentic dbt benchmark. A model carried a pre-existing
  `where status != 'CANCELLED'` that the ticket did not mention and the reference
  solution keeps. Profiling correctly reported `status` as unnormalised, 21
  distinct values including `CANCELLED`, `cancelled` and `C`, next to a
  clean-looking `is_cancelled` boolean, so the caller substituted
  `where not is_cancelled`. That is defensible engineering and wrong in this
  warehouse, where the two columns are mutually inconsistent: 158 rows match the
  string, 26 the boolean, 19 both. The two changes the ticket asked for netted
  -1 row. The one predicate swap nobody asked for accounted for +8, and the task
  failed on the row count alone with every other assertion passing. Three further
  runs of the same ticket reached the same wrong count by a different route,
  widening the customer universe with an added `LEFT JOIN`.

  A bare "the row count changed" warning does not help here, because that ticket
  *requires* two row-population changes. Only per-change attribution separates a
  requested change from a silent side effect, so that is what plan now reports
  under `data.row_attribution`: each `WHERE`, `HAVING` and `QUALIFY` predicate,
  each join added, removed or retyped, each swapped driving relation, and each
  `DISTINCT` or `GROUP BY` change, named and measured by applying it alone to the
  prior model and counting. Column expressions, aliases, casts and ordering
  produce nothing, by construction rather than by filtering, because they cannot
  change which rows enter a model and reporting them would bury the signal under
  an ordinary refactor. The whole-edit net is measured on the authored model and
  reported alongside, never summed from the parts, and when the two disagree
  `interacts` says so rather than letting a reader total the column and trust a
  number the warehouse would not produce.

  A warning, never a refusal. Changing the filter is sometimes exactly the job.
  What matters is that it is visible and quantified while the caller can still
  act on it, which is before `transform apply`.

  Naming a change is static and opens no connection, so it always runs.
  Measuring one is a `COUNT` aggregate over relations the model already reads,
  which is free on DuckDB and spend everywhere else, so the split is explicit:
  counting runs unasked on a free connector, and on BigQuery, Snowflake,
  Databricks, Redshift and Postgres it runs only under `--attribute-rows`,
  priced through the ordinary estimate and `--confirm --budget` handshake. A
  metered `transform plan` that does not ask for it behaves exactly as it did
  before, down to the envelope and down to not opening a connection.
  `--no-attribute-rows` turns counting off anywhere.

  Nothing about the guarantees is relaxed. The plan is built and stored before
  any of this runs, so a failure to attribute can never prevent a plan from
  existing, and planning still writes nothing into the dbt project. Every
  statement is one `SELECT COUNT(*)` cleared by the query firewall, so the
  model's parents must be profiled and the PII policy applies; a count projects
  no column, so a model filtering on a flagged column is still attributable and
  no value crosses the envelope. Nothing is materialized, no relation is created,
  and neither version of the model is built. A change that cannot be isolated or
  measured reports `attributed: false` and names why: macro-generated SQL, a
  jinja statement block, a CTE renamed so no prior scope can be paired with it, a
  relation the cache cannot place, or a counterfactual that is not valid alone
  because it depends on something else in the same edit. Authoring a model that
  does not exist yet produces no findings and opens nothing. The two caps that
  bound the work, six changes per model and five models per plan, are stated in a
  warning whenever they bind, because a capped run that said nothing about the
  cap would read as full coverage.

- **The conformance suite reaches the content a format declares, not just its
  shape** ([#259]). Three assertions, each covering a way a format can pass the
  existing suite while being wrong in a way that costs a real check.

  `DeclaringProjectContract` gains two optional hooks.
  `a_project_declaring_a_composite_key()` reaches `declared_composite_keys`, which
  is a separate field from `declared_keys` and which nothing in the suite touched
  before; it asks for more than two columns, because a format that special-cases the
  pair passes a two-column fixture and fails a four-column one. A truncated
  composite key does not read as a missing declaration, it reads as a narrower grain
  that is simply wrong, which is what makes it worth its own assertion.
  `a_project_declaring_a_join_with_differently_named_sides()` covers the case
  `test_a_declared_join_carries_both_sides` structurally cannot: if a fixture names
  both ends of a join the same, an implementation that copies the source column onto
  the target satisfies it exactly. The contract refuses a mirrored fixture here.

  `SemanticProjectContract` is new, and it closes the largest hole.
  `MaintainProjectContract` asserts that an empty project yields an empty semantic
  layer and never looks at a populated one, so a format that reads every dimension
  and measure name and drops the physical column behind each passed the whole suite.
  `SemanticModelDef` keys every field to a column and the drift detector skips any
  field whose column is `None`, so a layer mapped entirely to `None` validates,
  serializes, and compares clean forever: the check does not fail, it never runs, and
  a dropped warehouse column that should raise `dangling_reference` at high severity
  raises nothing. The absence is indistinguishable from agreement.

  `SemanticProjectContract` asserts tier 2 before anything else, against the project
  its own hook returns. `semantic_layer` is a tier-2 member, so a format mixing the
  contract in beside `ExploreProjectContract` would otherwise fail with
  `AttributeError: no attribute 'semantic_layer'`, naming the missing attribute
  rather than the tier it belongs to. Checking the hook's project rather than
  `make_project()` keeps the mixin dependent only on the one fixture it declares.

  All three are opt-in, and the two on `DeclaringProjectContract` skip with a message
  naming what goes unchecked rather than silently: an existing implementer's green
  suite must not turn red on an upgrade, and a format may genuinely have no
  multi-column grain to express. `DbtProject` implements all three in
  `tests/adapters/test_project_parity.py`, so the shipped format is held to them too.

### Fixed

- **A dev-target refusal no longer mixes path separators mid-sentence on
  Windows.** The message names the config file beside the profile it disagrees
  with, and the first half is built from a literal (`.dex/config.yml`) while the
  second took the platform separator, so a Windows reader got
  `.dex/config.yml and analytics\profiles.yml`. Both halves are repo-relative
  labels a reader matches against paths written in configuration rather than
  paths anything opens, so both now use forward slashes. The assertion that
  covers it was passing on CI and failing on a Windows checkout, which is the
  other half of the same bug.

## [1.6.2] - 2026-08-09

### Added

- **`explore profile` flags a declared type that contradicts the column's
  content** ([#204]). Profiling reported a column's declared type and
  nothing about whether the content agreed with it, so a raw source table
  (mostly string-typed columns, the landing-zone/CDC norm) went through
  silently even when a `VARCHAR` column held nothing but epoch timestamps:
  `data_quality` came back empty, and the caller only found out by hand,
  the one place the PII policy and query firewall do not apply.

  Three shapes are now detected, each measured as a fraction inside the
  aggregate scan the engine already runs, at zero extra cost: a string
  column whose values are dates/timestamps in a fixed format (ISO, or
  `%m/%d/%Y` / `%d/%m/%Y` with or without a time part), an integer-or-string
  column whose values are unix epochs (seconds vs. milliseconds, each with
  an implied calendar-date range), and a string column whose values are all
  numeric (and whether every value fits an integer). Where the day/month
  order of a slash-separated date genuinely can't be told apart from the
  data (neither component ever exceeds 12), the note says so rather than
  guessing; where one component does exceed 12 somewhere, that's a logical
  proof of the order, not a coin flip, and the note names the exact format.

  A column is eligible only with no PII flag at all, at any confidence,
  the same rule `is_min_max_safe` and the value-domain check already use.
  Only fractions, format/unit names, and (for epoch) a calendar date
  translated from an aggregate MIN/MAX ever leave the engine, never the
  underlying value. Implemented across every connector (DuckDB, BigQuery,
  Snowflake, Databricks, Redshift, Postgres), reusing the same
  fraction-inside-a-measuring-aggregate mechanism `explore profile` already
  uses for PII value-shape evidence.

### Fixed

- Expired BigQuery credentials now report a retryable prerequisite instead of
  an internal error.
  
### Changed

- `explore semantic query` accepts metrics positionally after the explicit `query`
  mode ([#253]), matching the other `explore` verbs. The repeatable `--metric`
  flag remains supported for existing callers, and both forms can be combined.

- **Declining tier 3 is a question about the artifact an edit lands in, not about
  where the project came from.** `EditableProject`, `references/project.md` and
  `CONTRIBUTING.md` all stated the decline as an absolute: a project reduced from a
  running graph cannot receive an edit. That is right about the reduction and wrong
  as a general rule, and the two come apart more often than the graph example
  suggests. An asset graph carries neither column names nor join keys, so a format
  over one reads its declared keys, joins and semantics from somewhere else, and that
  somewhere is usually a hand-authored file that nothing regenerates. Those files are
  a real source of truth and they are the shape `reconcile` already proposes edits
  to, so a format holding one may reach tier 3 for that channel while still refusing
  to author a model. Deciding from "this project is graph-derived" alone declines a
  tier the format could honestly serve. No behavior changes; the guidance does.
  
- `references/canonical-model.md`'s extension-seam section described a single
  `ProjectAdapter` protocol with `load()` and `write_edits()` as the live seam. That
  protocol is superseded, unreferenced, and satisfies no tier, so a format written
  against it is refused as "not a project", and this is the document a reader reaches
  before writing one. It now describes the three tiers and points at
  `references/project.md`.

## [1.6.1] - 2026-08-09

### Fixed

- **The tier-3 write path can carry the two things that make it safe** ([#241],
  [#242]). `EditableProject.write_edits` declared one argument while the behavior it
  governs depends on two more. `confirmed` had nowhere to go, so anything routed
  through the tier would refuse every apply that met a conflict and could never
  override one, and an implementation written against the published signature had no
  way to receive it at all. That is the parameter the human-edit conflict handshake
  turns on: every target is re-hashed against the content it was planned against, and
  a file whose hash moved is someone's work, not a stale line to overwrite. The
  project directory was resolved from engine configuration rather than from the plan
  being applied, and a plan records its own directory relative to the repository root
  precisely because that is what survives the repository moving, so the two can name
  different projects with nothing to say so: the edits would land in whichever
  project the engine happened to be configured for, hash-checked against that
  project's files.

  The member is now `write_edits(edits, project_dir, *, confirmed=False)`.
  `DbtProject` passes both through, and a directory the caller names wins over the
  one the instance was built with. Passing none keeps the configured pin, which is
  what a caller holding engine configuration and no plan has always had.

  No shipped path changes behavior, because nothing in the distribution called the
  tier: `transform apply` calls the module-level `dbt_project.write_edits`, which
  always resolved both correctly, and `reconcile` reads the tier only to decide
  whether it may author an edit at all. That is the argument for fixing the signature
  now rather than after someone builds on it. `runtime_checkable` checks only that a
  method exists, so a third-party implementation of the one-argument shape keeps
  satisfying `isinstance` and fails at the call.

- **The conformance suite reaches tier 3** ([#241]). `EditableProjectContract` is the
  class the suite did not have, and it asserts the behavioral half no shape check can
  see: an unconfirmed write leaves a target someone else changed alone, and a
  confirmed one goes through. The two are a pair on purpose, since a `write_edits`
  that never writes satisfies the first by itself. Its one hook raises rather than
  defaulting to a skip, unlike `make_unreadable_project`, because the excuse does not
  carry over. A format may genuinely have no unparseable state, but a format that
  reached tier 3 writes into a source of truth a human can also edit, so the case
  where the human got there first exists for every one of them.
- **A corrupt drift baseline is a prerequisite failure, not a bad request**
  ([#243]). `load_snapshot` raises on a document it cannot parse, and pydantic's
  `ValidationError` subclasses `ValueError`, so the refusal fell through to the
  CLI catch-all and was classified as a *request* error. The operator was told
  they had made a bad request when the fix is `maintain snapshot`, which is
  exactly the retry-versus-stop distinction `PrerequisiteError` exists to carry.
  The engine now catches `ValueError` out of the load, so a third-party backend
  that deserializes its own rows is covered too, and `references/storage.md` and
  the `Store` protocol both name that as the contract rather than leaving it to
  be inferred from what the shipped backends happen to raise.

- **A baseline from an unknown schema version is detected at all** ([#243]).
  `Snapshot.schema_version` was stamped on every write and read by nothing, so a
  document from a schema this engine does not understand was handed to the
  detectors as though it were current. That is the worse of the two states,
  because it is not an error: it is a drift report measured against a shape the
  engine misread. `SUPPORTED_SNAPSHOT_SCHEMA_VERSIONS` is the set that gives the
  field a meaning, and the policy is refuse rather than migrate, written down
  next to the set: a baseline is cheap to regenerate, a migration function per
  version has to be right about a document nobody has read in a while, and a
  wrongly migrated baseline is worse than an absent one because drift measured
  against it looks like a result.

  A set rather than the `<` comparison the query firewall uses on
  `CACHE_SCHEMA_VERSION`, because the two documents differ: an old cache has a
  degraded reading and the firewall adds a hint, while a baseline has no
  degraded reading, only a wrong one. The check runs on a parsed `Snapshot`, so
  it names a version for a document today's model still validates and refuses a
  future one that also changed shape as a parse failure instead, with the same
  remedy and no version named. Both messages say so. `DRIFT_SCHEMA_VERSION` is
  deliberately left without the same gate, and the reasoning now sits next to
  the constant.

- **`explore semantic query --local` no longer refuses a relation it has simply
  never profiled** ([#134]). The relation pre-check resolved the rendered metric
  SQL against `.dex/cache.json` and refused anything it could not find there. That
  file records what has been *profiled*, which is a different question from what
  exists, so a model `transform build` had created minutes earlier was refused
  until `explore profile` was run on it: the build-then-validate loop cost a
  profiling pass in the middle, and the refusal blamed a compiled-namespace
  mismatch it had never measured, sending the reader to `dbt parse` for a cache
  miss.

  The authority is now the connection itself. The cache stays as a free fast path
  and can only clear a relation, never condemn one; anything it cannot resolve is
  resolved against the live inventory, the same listing `explore profile` already
  resolves its arguments against, and that listing is only read when the cache
  came up short. A repo that never ran `explore map` gains the guard rather than
  skipping it, since the check no longer needs a cache to have something to check
  against.

  A refusal is now scoped to what the listing can settle, and says which of two
  things went wrong. A relation in a database this connection does not carry is
  the compiled-elsewhere mismatch and is refused as one. A relation missing from a
  schema the listing did cover is a model not built into this target, and says
  that instead. A relation in an unlisted schema of a connected database is not
  refused at all: the dataset allowlist is narrower than the dbt project, dex never
  looked there, and the old check would have refused a legitimate query on the
  strength of a question it never asked.

  Querying a relation with no profile does cost something real, just not access:
  the PII request-gate loses its value evidence and falls back to the name
  heuristic. That is now disclosed on the result, naming the relations and
  `explore profile`, rather than the screening quietly weakening. The obvious
  shortcut, writing the manifest's relations into the cache, is deliberately not
  taken: a cached column with no PII flag reads as "profiled and cleared", so
  registering unprofiled columns would switch the name heuristic off on exactly
  the fresh relation that most needs it.

  Two smaller defects fell out of the same rewrite. CTE names in the rendered SQL
  were treated as physical relations, so a MetricFlow subquery alias that failed to
  resolve was refused as a foreign table. And the SELECT-only assertion now runs
  before the pre-check rather than after, because the pre-check can introspect the
  connection and a statement should be proven read-only before anything else
  touches it.

- **The hosted backend discloses name-only PII screening too** ([#134]). Fixing the
  local side first made the asymmetry visible: local now says when its PII evidence
  was missing, while the hosted gate fell back to the name heuristic silently in two
  cases. A dimension the layer carries no `config.meta` for was screened by name
  with nothing said, and a dimension-metadata call that failed outright degraded
  *every* ref to the heuristic while `_meta_lookup` swallowed the error into a
  lookup that answered None for everything. Both are now reported on the result, and
  reported separately, because a layer that answered and knows nothing wants
  `meta: {pii: true}` on the dimension in the dbt project while a call that never
  answered wants retrying. The gate itself is unchanged; what changes is that the
  weaker screening stops passing for the stronger one.

### Added

- **`BaselineUnreadableError`**, exported from the package root ([#243]). A
  `PrerequisiteError`, distinct from `NoBaselineError` because "you never took a
  baseline" and "the baseline you have is unreadable" are different facts about
  a deployment and only one of them suggests something went wrong, so a host can
  page on the second without paging on the first. It carries `schema_version`:
  the stored version when the document parsed and this engine does not read that
  version, and `None` when it did not parse at all. Those are different
  operational events (a deployment rolled forward or back, versus an integrity
  problem) and the attribute is what separates them without matching on message
  prose.

### Changed

- **A refusal naming `maintain snapshot` now says what running it costs**
  ([#243]). The command is free on every connector and cannot really fail, which
  is what makes "just re-snapshot" sound costless. A replacement pins current
  state as known-good, so drift accumulated since the last readable baseline is
  absorbed and never reported on any axis. A baseline newer than the engine
  reading it is also told to upgrade dex first: dex advises committing
  `.dex/snapshot.json` like a lockfile, so that state is usually a colleague on
  a newer dex, and replacing the baseline resolves it by overwriting one they
  can still read.

- **`explore semantic query` accepts comma-separated name lists** ([#135]).
  `--group-by a,b` failed, on the local backend as an unresolvable MetricFlow
  format and on the hosted backend as an invalid semantic-layer name, and only the
  repeated flag worked. A comma-joined list is the natural first guess, a common
  CLI convention, and already what `explore profile` and
  `explore cluster --features` accept. `--metric`, `--group-by`, and `--order-by`
  now take either spelling and mix them freely; `--where` deliberately does not
  split, because a Jinja filter clause carries commas of its own. Normalization
  happens on the query object rather than in the CLI, so both backends and a
  library caller building the query directly get it from one place. The
  empty-metric guard moved to after normalization for the same reason:
  `--metric ,` is now refused identically on both backends, where before local had
  its own guard and hosted would have asked dbt Cloud for no metrics at all.

## [1.6.0] - 2026-08-08

### Added

- **A project format can be named in configuration, and `maintain` reads it**
  ([#171]). The project seam had three tiers, a conformance suite and one
  implementation, and no way to reach any of it: there was no configuration key
  naming a format and no constructor argument to inject one, so a second format
  was readable in principle and unreachable in practice. `.dex/config.yml` now
  takes a `project:` block with `format:` and `options:`, resolved through the
  same open registry `cache.backend` uses (a shipped name, a dotted
  `mypkg.projects:my_project` path, or a name registered under the
  `exmergo_dex_core.projects` entry-point group), with shipped names always
  winning so installing a package can never silently redirect which models a repo
  is reasoned about. `--project-format` overrides it for one run and leaves
  `options` behind when it names a different format, because options are not
  namespaced by format.

  Both doors, deliberately, because the two halves serve different hosts.
  `DexEngine(project_format=...)` is for a host running dex in its own process
  and holding a graph it built; it always wins over configuration. A name in a
  committed file is the only door open to a host that reaches dex as a
  subprocess, which is the deployment shape that asked for this, and shipping
  only the constructor argument would have repeated the gap storage had to close
  afterwards.

  Construction is a separate contract (`ProjectContext`, `ProjectFactory`) rather
  than members on the tiers, which is what keeps a class with the right methods a
  project, with no base class and no registration step. `ProjectContext` carries
  a nullable `repo_root`, a nullable `project_dir`, and verbatim `options`: a dbt
  project is keyed by a directory, one reduced from a running graph by nothing at
  all, and a hosted one by service coordinates, so a context shaped around the
  only format that exists would have left the other two unbuildable. `options`
  are the format's to define and the format's to validate, and refusing one you
  cannot honor is part of the contract, because a silently dropped setting is
  indistinguishable from a working one until dex is reading a different project
  than the configuration named.

  dex builds a project per command and never holds one it built. A project is an
  artifact `transform apply` and `transform build` rewrite, so an instance held
  across commands would serve a later command the project as it was before the
  write, and a drift report computed against a stale project is wrong rather than
  merely slow. An instance a host hands in is held, because its freshness is the
  host's to know. Construction therefore has to be cheap, which is stated on the
  contract.

  `maintain snapshot`, `schema`, `semantic` and `check` now read their two
  snapshot layers through `MaintainProject` instead of loading a dbt project
  directly, so a format that is not a dbt project can be a drift baseline through
  a supported path rather than by a host hand-building a `Snapshot`. `reconcile`
  stays outside tier 2 on the record: it wants the project's file surface rather
  than a layer, and widening tier 2 to carry a bag of file paths and file contents
  would make it a tier no format without files could reach.

  `DbtProject` memoizes its loaded view for the life of the instance, because the
  two layer accessors each need it and three of the four commands need both, so
  routing without the memo would have cost a second full walk of the model and
  macro trees plus a second parse of a routinely multi-megabyte `manifest.json`.

- **`maintain reconcile` asks the project whether it may be written to** ([#171]).
  A format that does not implement `EditableProject` gets every finding back as an
  advisory proposal, with no edits, no diffs and no stored plan, and a warning
  naming the format and the tier it declined. The findings are still surfaced:
  declining the write tier removes dex's authority to author an edit, not an
  operator's need to see the drift.

  This was previously true by accident. Reconcile's two mechanical write paths
  gate on the `models/staging/stg_<table>.*` scaffold convention and fail closed,
  so a generated tree was safe exactly as long as its own directory naming
  happened not to collide, and a format whose layers used that vocabulary would
  have been written into. The consumer who built the second format pinned that
  invariant with a test in their own repository, which is the wrong side of the
  boundary for an invariant this one owes. The convention checks stay as a second
  line: the declaration replaces the coincidence, not the check that made the
  coincidence survivable. Asserted in the safety spine, paired with the dbt format
  taking the mechanical path through identical fixtures so a regression that broke
  reconcile outright cannot pass by doing nothing.

  A second gate sits behind the first for the case that does not exist yet: a
  format that implements the write tier but is not one dex can author edits for
  (they are dbt artifacts, a staging model and its schema YAML) also degrades to
  advisory, with its own message, rather than reaching a dbt-shaped code path.

- **`ProjectError`, the format-neutral refusal** ([#171]). `maintain`'s layer
  reads catch it, because the format on the other side of the seam is whatever
  configuration named and catching the dbt format's own error there would let a
  second format's failure through to a traceback. `DbtProjectError` is now a
  subclass, so every existing catch and every existing consumer keeps working.

- **`ProjectFactoryContract` in the shipped conformance suite** ([#171]). The
  construction half, mixed in front of the tier contract, so "the suite is green"
  means a format is correct **and** constructable: it checks that a factory builds
  the tier it declares and that an option the format cannot honor is refused by
  name rather than accepted and dropped. `[project-conformance]` still needs only
  pytest, now held by a packaging test that installs the wheel and asserts no
  dialect engine came with it, which both `pyproject.toml` and
  `references/project.md` promised and nothing checked. A second packaging test
  builds an out-of-tree format against the published wheel alone and runs the
  shipped contract against it, and a third registers one under the entry-point
  group across two installed wheels, which is the only honest way to test a group
  that exists only as installed-distribution metadata.

- **`explore profile` reports the value domain of a low-cardinality non-PII
  column** ([#203]). `ColumnProfile.min_value`/`max_value` are suppressed for
  every string column, PII or not, because a string extreme is itself a raw
  value. On a raw source table (the norm: mostly string-typed columns), a
  caller learned `env_tier` had 4 distinct values and had no way to learn
  what they were, short of a raw SQL client, exactly the one place the PII
  policy and query firewall do not apply.

  A column now reports its `value_domain` (distinct values by frequency,
  capped, with an `elided` count when the cap binds) when it carries no PII
  flag at all, at any confidence, its distinct count clears both an absolute
  cap (25) and a row-relative fraction (10% of non-null rows, so a tiny
  table's near-key column does not qualify on the absolute count alone), and
  it is not a candidate key (single-column or a proven composite member). A
  flagged column reports no domain at any confidence or cardinality, full
  stop; detection, confidence, and the block threshold are unchanged.

  Eligibility is decided from the approximate pre-scan distinct count (cost
  control: an obviously-too-wide column is never even queried), but the
  reported domain is built from the exact count the new probe itself
  returns: when the approximation under-estimated and the true count is
  still within the fraction bar, the result is capped with an honest
  `elided` count rather than dropped; only a true count that breaks the
  fraction bar (a near-key the approximation missed) is dropped entirely.

  New adapter capability (`value_domain_counts`), implemented across every
  connector (DuckDB, BigQuery, Snowflake, Databricks, Redshift, Postgres),
  matching the existing `exact_distinct_counts`/`distinct_combination_counts`
  precedent. Every metered connector spends it only inside the
  already-confirmed budget, floored like every other optional escalation,
  and degrades to reporting no domain (plus a table note) rather than
  self-escalating when the remaining budget can't cover it. The reported
  order is re-sorted by the engine rather than trusted from the adapter, since
  not every dialect's array-aggregate reliably preserves an inner subquery's
  `ORDER BY` once collected into one value. Redshift's implementation reads
  back multiple tagged rows instead of one struct-shaped row (its
  semi-structured `SUPER` type is treated as degraded elsewhere in that
  adapter already), trading the column's native value type for a text cast
  that lets heterogeneous columns batch into one `UNION ALL` statement.
- **CLI**: resolve unambiguous bare subcommands and suggest alternatives when the name is ambiguous ([#236]).

- **transform init**: allow initializing into the current directory ([#235]).

- **`notes` on both maintain snapshot layers** ([#193]). `TransformLayer` and
  `SemanticLayer` now carry `notes: list[str]`, matching the one
  `ProjectDefinitions` has carried all along, and the four `maintain` commands
  fold them into their warnings. Without it a project format producing a layer
  that is faithful but narrower than a dbt project's had nowhere in the return
  value to say so, and kept those caveats beside the bridge instead, where no
  caller reading the snapshot ever saw them. That gap is what made the optional
  `path` below only half a fix: a reader seeing no provenance still could not
  tell "dex failed to record it" from "this format has no files", and the reason
  is a property of the format rather than of the finding. A `file_count` of zero
  beside a dozen models is the same problem one layer up.

  Notes are collected from every layer a command read, baseline and current, then
  deduplicated. Both sides, because the two provenance payloads read different
  ones: `dangling_source` compares against the baseline's declared sources while
  `definition_changed` reads the current project, so surfacing one side would
  leave the other axis's limits unexplained. In practice the same format produces
  both and says the same thing, which is what the deduplication is for. They are
  reported at detection time as well as when a baseline is pinned, because a host
  re-runs detection far more often than it re-pins.

  Informational by design, and the boundary is worth stating because the next ask
  will test it: no detector reads a note, notes take no part in any comparison so
  a changed note is never drift, and dex does not branch on one. Anything dex has
  to *decide* from belongs in a tier, which is checkable, rather than in prose the
  engine would have to trust. That is the same reason the tiers exist instead of a
  `writeback` flag.

- **The project conformance contract asserts that a format's layers can be a
  baseline** ([#193]). `MaintainProjectContract` gains one assertion: the two
  layers survive a JSON round trip inside a `Snapshot`. That is precisely what
  reaching tier 2 buys, since a store serializes the baseline and a later command
  loads it back to diff against, and nothing checked it. The check is a real
  serialization rather than a copy because that is where a value a format chose
  can be accepted in Python and rejected on the way back, and the failure would
  otherwise surface on the run after the one that caused it. No test in this
  repository had ever round-tripped a populated layer through JSON: the storage
  conformance fixture builds its snapshot with both layers unset, and the
  in-memory backend copies rather than serializing. The contract still needs only
  pytest. In-repo it now also runs against a pathless format, because the shipped
  dbt format always has paths and would never exercise the freedom below.

### Fixed

- **A malformed `dbt_project.yml` is now a refusal that names the file, not a
  traceback** ([#171]). `load` parsed the project file with `yaml.safe_load` and
  let the parser error escape, and `yaml.YAMLError` descends from `Exception`
  rather than `ValueError`, so it went straight past every caller's handler: all
  four `maintain` detection commands caught what looked like a complete pair and
  did not catch this. `definitions()` had been patched to compensate at its own
  call site, which fixed the explore path and left the rest. The wrap now happens
  in `load` itself, so `write_edits` (which loads before it writes) is covered by
  the same fix, and `definitions()`'s local workaround is gone.

- **The project seam finds a project in a subdirectory** ([#171]).
  `DbtProject.load()` resolved to `project_dir or repo_root`, running no discovery,
  while `definitions()` on the same object searched the repo root and its immediate
  children. So the two tiers of one project disagreed about which project they were
  reading in every repo that keeps its dbt project one directory down: tier 1 found
  it and tier 2 raised "no dbt_project.yml". Nothing had hit it because `load()` had
  no production caller until `maintain` was routed through the tier. The result is
  also resolved to an absolute path now, because the view's root reaches the plan
  store, which records a directory relative to the repo root and re-resolves it at
  apply time.

- **A `key_lost_uniqueness` finding with no scaffolded YAML to extend now says so**
  ([#171]). `reconcile` emitted the proposal and skipped the test edit silently, so
  a reader saw a proposal with no edit and no way to tell whether dex had declined
  or failed. The sibling path for the model SQL had always emitted a reason.

- **`transform plan` accepts a line-broken top-level dbt `ref()`** ([#195]).
  Placeholder-only Jinja lines remain intact after the model's first `SELECT` or
  `WITH`, while standalone config headers before the query are still removed.

- **transform dev-target preflight compares resolved DuckDB paths** ([#234]).
  Relative and absolute spellings of the same warehouse file (e.g. `warehouse.duckdb`
  vs `./warehouse.duckdb`) are no longer treated as configuration drift.

- **A project format with no files no longer has to invent provenance**
  ([#193]). `SourceTable`, `SemanticModelDef` and `MetricDef` each required a
  `path` with no default, so a format whose sources are declared in configuration
  and whose semantic models and metrics are objects in a running graph had nothing
  to supply and shipped the empty string. All three are now `str | None`.

  The cost of the required field was never a broken read. Every consumer of those
  three values is a provenance string in a finding payload, `declared_in` on
  `dangling_source` and `path` on `definition_changed`, and dex opens neither. It
  was that a `high` severity finding handed an analyst a file to go and check
  which, for a pathless format, would not be there. That is the argument
  `SemanticModelDef`'s own docstring already makes one field over, where a computed
  expression maps to `None` because guessing columns out of expressions would turn
  every refactor into a false dangling-ref finding.

  The empty string was worse than a fabricated path in one specific way, and this
  is why the change matters beyond the format that reported it: `""` was an
  undocumented sentinel. A format author reading `path: str` had no way to tell
  that the empty string was the sanctioned answer rather than a bug, so the next
  implementer picks `"n/a"`, or a synthetic path that reads exactly like a real
  one. `str | None = None` puts the answer in the signature, which is the same move
  as a declinable tier over a capability flag: the declaration and the enforcement
  become the same object.

  Both payloads now **omit** the key rather than reporting a null. A null-valued
  provenance key forces every reader to branch, and both branches fall back to the
  identifier the finding already carries, while an absent key lets a
  `.get(default)` do that on its own. Omission also cannot regress an existing
  consumer, because the shipped dbt format always has a path and never produces
  one; a null would be a new value shape in a key that is already there.

  `SNAPSHOT_SCHEMA_VERSION` is deliberately unchanged. Widening a field is
  backward compatible on read, so every existing baseline loads untouched, and the
  only break is in the downgrade direction: an older engine reading a snapshot
  that contains a null path. Such a snapshot can only have been written by a format
  that older engine could not have served anyway.

- **A crowded low-cardinality dimension no longer pushes the true grain out
  of the composite-key probe** ([#168]). `_probe_composite_keys` ranks
  candidate 2-column keys by smallest distinct-count product and probes only
  the top few, because each probe is a real two-column `DISTINCT` scan. When
  one dimension column happened to pair cheaply with several other columns,
  every one of those pairs ranked ahead of the true grain purely because they
  shared that same attractive anchor, even though they were really the same
  hypothesis ("does this dimension have *a* partner?") tried with different
  filler. The true grain, which can legitimately score worse on raw product,
  never got probed, and a wrong (or no) grain was recorded instead, so
  `maintain grain` went on monitoring the wrong composite indefinitely.

  The cap (`_COMPOSITE_PAIR_CAP`) is raised from 3 to 5 for headroom, and a
  pair that shares a column with an already-kept pair is now dropped as a
  near-duplicate when its product is within `_COMPOSITE_REDUNDANCY_RATIO`
  (3.0x) of the kept pair's: the same idea tried with interchangeable
  filler, not a genuinely different candidate. A pair whose product diverges
  meaningfully still gets its own slot even if it reuses a column, which is
  what lets the true grain through. Each additional slot is still a real
  scan, but the batch stays one statement, and a metered adapter that cannot
  cover the wider batch within the confirmed budget already degrades to
  "grain unknown" via `distinct_combination_counts`'s existing contract, so
  this does not bypass cost guards.

## [1.5.2] - 2026-08-05

### Added

- **`explore diagram`: the cached map as a Mermaid ER diagram** ([#189]).
  Serializes what `explore map` already learned (objects, keys, grain, joins,
  PII flags) into an `erDiagram` under `data.mermaid`, with an `entities` legend
  mapping each entity name back to its fully-qualified identifier. It is a pure
  function of the exploration cache, so it opens no connection, needs no
  credential, and cannot spend: free on every connector, and re-runnable while
  shaping a diagram. Also exported as `render_er_mermaid` for a Python host that
  already holds a cache.

  **The rule this establishes, because a renderer invites the next one:** dex
  may serialize structure it has already computed into a text format, and dex
  never renders data values into a visual encoding. A schema graph is structure;
  a chart of null fractions is a picture of the data, which is a different
  product's job. `references/methodology.md` states it, and it is what admits
  `erDiagram` while ruling out `pie` and `xychart`.

  The cardinality rules are the substance. Mermaid demands a glyph on every edge
  and a relationship record carries no cardinality, so a naive renderer invents
  one, and a diagram is believed more readily than the JSON behind it. The
  parent side claims "exactly one" only when the parent key is proven **and** the
  join was declared or measured with no orphans; it degrades to "zero or one"
  when uniqueness is proven but nothing measured the overlap, and to "zero or
  many" when uniqueness was never established. Declared joins draw solid,
  inferred dotted, and the label carries the kind, the confidence, and the
  orphan fraction, so an unverified guess says so on its face.

  Selection follows the rest of explore: the default draws profiled objects that
  participate in a join with their grain, key, join, and PII-flagged columns,
  `--full` widens to every eligible object and column, an entity cap binds in
  both modes, and every elision is counted in `notes`. No column value reaches a
  diagram (the cached min and max are never read) and PII renders as category and
  confidence, asserted in the safety spine because a diagram is the most
  shareable artifact dex produces. Rendering is deterministic, so a regenerated
  file diffs cleanly. dex writes no file: the text comes back in the envelope and
  where it lands is the caller's decision.

  **No Mermaid dependency, and an advisory `mermaid-syntax` CI job instead.** The
  renderer is stdlib plus dex's own cache types. Every Python Mermaid package was
  considered and none fits: the string builders replace none of the actual work
  (the cardinality derivation, the key and PII marks), the image renderers POST
  the diagram to a third-party host, which is disqualifying for a tool whose
  premise is that your data does not leave the machine, and the one parser binding
  requires Node installed anyway while sitting at 0.0.4 on a deprecated Mermaid
  API. But the compatibility contract is real (it is with a parser in someone
  else's tool), and the repo rule is that such a contract gets a test running
  outside this repository, so CI now renders a fixture corpus from the engine and
  parses each case with the genuine `mermaid` package from npm. The corpus is
  generated rather than committed so it cannot drift from what dex emits, and the
  dump fails if it stops covering every cardinality glyph the policy can produce.

  That job immediately corrected two assumptions in the first draft's own
  comments: a dotted entity name and a `NUMERIC(10,2)` type both parse fine in
  Mermaid 11, while angle-bracket types, spaces in a type name, leading digits, and
  embedded quotes genuinely break the attribute grammar. The identifier
  normalization is unchanged (short entity names are a legibility choice and the
  sanitizer answers the four real failures); the comments explaining it are now
  accurate rather than plausible.

  The entity legend crosses the boundary as a list of records rather than an
  object keyed by entity name. Keying it by name would have handed the warehouse
  the power to fail the boundary check, since the envelope sanitizer matches key
  names against secret-like substrings and a table called `access_tokens` is an
  ordinary thing to own. A spine assertion now states that as a rule for every
  future payload, not just this one.

- **The project seam is tiered, and load-bearing** ([#171]). `ProjectAdapter` had
  exactly one reference in the distribution, its own `class` statement, and
  `DbtProject` was never constructed anywhere, tests included, while every
  project-reading consumer went to `dbt_project` directly. It is now three nested
  protocols in `adapters/project.py`:
  `ExploreProject` (`definitions()`), `MaintainProject` (adding
  `transform_layer()` and `semantic_layer()`), and `EditableProject` (adding
  `write_edits()`), with `tier_of()` reporting the highest tier an object
  satisfies. `DbtProject` implements all three by delegating to the functions
  that already existed, and explore's one project read now goes through it, so
  the seam carries a real read path instead of describing one.

  Tier 2 is two methods rather than one returning a union, because
  `maintain.snapshot` already exposes exactly those two and `maintain.commands`
  already calls them; a union would have to be unpacked at every call site.
  Declining `EditableProject` is a supported answer: a project reduced from a
  running graph cannot receive an edit, and a tier is checkable where a
  `writeback: no` flag would be a claim the engine has to trust.

- **A conformance contract for project formats** ([#171]).
  `exmergo_dex_core.adapters.conformance` ships the seam's rules as assertions, so
  a format outside this distribution runs them in its own suite, the way
  `storage.conformance` already does for backends. Install
  `exmergo-dex-core[project-conformance]`, which needs only pytest: nothing in the
  contract reaches the dialect engine, and a packaging test keeps it that way.
  `references/project.md` covers the tiers and what the construction contract
  still leaves open.

### Fixed

- **`definitions()` no longer raises on a project file that is not valid YAML**
  ([#171]). Its docstring promises that no project, an ambiguous choice, or an
  unreadable project yields the empty view and never an exception, and explore
  depends on that: exploration runs against warehouses with no project at all. But
  `load` parses `dbt_project.yml` with `yaml.safe_load`, so a malformed project
  file arrived as `yaml.YAMLError` while only `DbtProjectError` was caught, and
  `explore --use-project` raised out of a read that is meant to degrade. The three
  profile readers in the same module already pair the two exceptions; this is that
  pairing on the read path. Found by the first run of the new conformance suite
  against the shipped format.

## [1.5.1] - 2026-08-03

### Fixed

- **`budget.session_ceiling` now binds under concurrency** ([#159]). The cost
  gate read the day's spend once, when it was built, and every ceiling decision
  for the life of that gate derived from that one reading. Two billed commands
  overlapping in time were therefore admitted against the same headroom. Nothing
  looked wrong afterwards: every ledger entry was well-formed and true, and only
  the aggregate was over budget.

  Re-verified live at 1.5.0 on BigQuery before this was scoped. A sequential
  pair refused exactly as designed; the same pair issued concurrently both ran,
  and the ledger finished **15.3% over** a seeded `session_ceiling`. Two further
  findings came out of that run. Inspecting the executed jobs showed the
  **server-side backstop was raced too**: each command's first statement carried
  the whole daily ceiling as its `maximum_bytes_billed`, so the warehouse-side
  bound on the pair was twice the ceiling. And `data.spend.session_spent_today`
  **under-reported**, because each command added its own spend to a stale
  reading, so two concurrent commands reporting 25.2 MB and 21.0 MB had in fact
  spent 46.1 MB between them.

  The fix is not a fresher read, which is the shape the defect first suggests: a
  read followed by a decision leaves a window in which two commands read the
  same number and both decide yes, however recent the number is. An admitted
  command now **books its estimate against the day's headroom before it runs**
  and releases the unspent part when it settles, both inside a lock the store
  provides. A refusal books nothing, so the unconfirmed handshake that begins
  every billed command stays a pure read. `transform build` reserves too, which
  matters most there: dbt executes the statements, so nothing reached the ledger
  until the run finished and a build's headroom looked free for as long as it
  ran.

  The server-side cap is bounded by the booking as well as by the ceiling, which
  is what closes the warehouse-side half. Two commands admitted together now
  carry caps that sum to at most the ceiling, verified on the live jobs, where
  before both carried the whole of it. A command whose estimate drifts past what
  it booked is re-admitted against a live reading rather than spending into
  headroom another command holds, so drift is still allowed to use budget that
  is genuinely free and no longer able to use budget that is not.

  Ledger entries gain an `entry` kind (`reservation`, `settlement`, `release`)
  and a `reservation_id`. A release is negative, so **a day that has finished
  sums to actual spend exactly as before**, and no consumer summing settled
  spend sees a different number; a day with commands still running sums to
  actual spend plus the headroom they hold, which is the number a concurrent
  command has to see. `spend_since` is unchanged, so every storage backend
  written before this keeps working untouched.

  A process killed outright cannot release, and its reservation then stands
  until the UTC rollover. That is deliberate and it is the safe direction for a
  spend guard. Every softer exit releases: the settlement funnel, the engine
  rebuilding a gate, and engine shutdown, which the CLI calls from a `finally`.

### Added

- **`SpendLock`, an optional storage capability** ([#159]). A store that
  implements `spend_lock` lets dex make the spend admission atomic. Both shipped
  backends implement it, so the CLI and the default library path are safe by
  default rather than safe by documentation: the filesystem backend takes an
  advisory lock on `.dex/spend.lock` (plus a per-path in-process lock, since a
  POSIX lock belongs to the open file description and would not keep two threads
  apart), and the memory backend takes a re-entrant lock.

  It sits beside the tiers rather than inside them, and a backend without one
  still runs: dex reserves regardless, which narrows the race from the duration
  of a warehouse query to the microseconds around the ledger read, and **every
  billed command warns that the cumulative ceiling is advisory on that
  backend**. A warning rather than a refusal, matching the call already made for
  a `session_ceiling` nobody set.

  This reverses a decision recorded on the protocol, which said it would not
  grow a member for atomicity because an optional member no implementer can rely
  on is worse than a stated bound. That holds for a member dex would use
  silently; it does not hold for one dex detects and reports, because a caller
  is then never told a ceiling bound when it did not.

  `SpendLockContract` ships in the conformance suite alongside the tier
  contracts. It checks that the lock excludes, that it is re-entrant for one
  holder, and that it is scoped to the ledger rather than to the backend, the
  last being the failure a single-tenant test cannot see: a global mutex passes
  the first two and serializes every tenant in a deployment.

## [1.5.0] - 2026-08-01

### Added

- **A machine-readable `reason` on every error envelope, alongside `status`**
  ([#170]). #155 gave every deliberate refusal a typed exception a library
  consumer can catch, but a host driving the CLI still only ever saw
  `status: error` and a prose string: `OverCeilingError` and a BigQuery
  connection fault arrived in the identical shape. A host mapping the
  envelope onto its own automation had to pattern-match error text to tell a
  policy refusal from a transient fault, and got it wrong in the field: a
  `connection refused` blip scored the same as a real policy violation and
  was treated as a permanent stop rather than something to retry.

  `reason` is one of seven values (`guard`, `prerequisite`, `connection`,
  `configuration`, `request`, `execution_failure`, `internal`), derived
  automatically from the caught exception's class, never invented per call
  site: `guard` covers a cost/PII/firewall/safety policy declining
  (`OverCeilingError`, `QueryRefusedError`, a production-target refusal);
  `prerequisite` covers a named setup command that needs to run first, then
  retry (`CacheRequiredError`, `NoBaselineError`, a missing optional extra);
  `connection` covers an unreachable warehouse; `configuration` covers the
  engine or repo being wired without something it needs; `request` covers
  bad input to this specific call; `execution_failure` covers an operation
  that ran (a `dbt build`) and failed partway; `internal` is reserved for
  whatever is not a deliberate dex refusal at all. `reason` is `null` outside
  `status: error`, same discipline as `cost.paradigm`'s null default (#162):
  an envelope that was not a refusal makes no claim about why.

  The prose messages are unchanged and stay the primary explanation for a
  human; `reason` is an addition for a host that wants to branch on retry
  semantics without regex-matching a string this project should stay free to
  reword.

  Classification itself is deliberately two-tiered: a small, always-available
  set of families resolves without importing anything connector-specific,
  and only a broader set (which pulls in sqlglot, scikit-learn, and the dbt
  reader transitively) is attempted after. Reason: classifying a missing-extra
  refusal is exactly the moment those imports are least likely to succeed,
  and a `ModuleNotFoundError` while classifying an error must not crash the
  process reporting it. A packaging test against a real isolated wheel
  install with no connector extra pins this end to end.

- **A project can declare a composite grain via `unique_combination_of_columns`**
  ([#169]). `DeclaredKey.column` was a single string, so a project could only
  ever declare that ONE column is unique, never that a table's real key is the
  COMBINATION of several. This wasn't merely insufficient: on a genuinely
  composite-grained table, declaring one member column (the only thing the old
  format allowed) made dex hold it as a permanent contradiction, and a
  semantic model's declared primary entity could unconditionally override a
  correctly-measured composite grain with a wrong single column. Measured
  evidence from the report: 13 declared keys across a reporting layer moved 0
  of 12 elected grains; the channel only ever reached tables that already had
  a correctly-measured single-column grain.

  A new `DeclaredCompositeKey` reads dbt's own `unique_combination_of_columns`
  test (both the dbt-core 1.9+ built-in and the `dbt_utils` macro compile to
  the same stripped-namespace test name) from the compiled manifest and from
  raw schema YAML's model-level `tests:`/`data_tests:` block, with no new
  dex-specific schema. A resolved, column-existence-checked declared composite
  now wins over a measured or heuristic grain, *unless* the current grain is
  already a measurement-proven single column, in which case the proven single
  stays and a note records that a composite was also declared. Because the
  declared composite lands in `Dataset.grain` as a multi-column list, it flows
  through `maintain grain`'s existing combination-check path automatically,
  never the single-column path, so it cannot reproduce the false
  `key_lost_uniqueness` failure mode the old single-column declaration could.

- **A billed command with no cumulative ceiling now warns** ([#165]).
  `budget.ceiling` is refused when missing, because nothing runs unbudgeted, but
  `budget.session_ceiling` was simply absent and silent: `effective_ceiling()`
  returns the tighter of the two bounds and `None` only when *neither* is set,
  so a config with `ceiling` and no `session_ceiling` ran every billed command
  with no daily cap and nothing said so. From outside, an unset cumulative cap
  and one that bound look identical.

  The warning rides the confirm handshake (where a caller is choosing a budget)
  and the settled result (where they are looking at what it cost). It stays a
  warning rather than a refusal deliberately: refusing would break every project
  that never set one.

  It also names the compounding half. Config is read from
  `<repo_root>/.dex/config.yml` and does not inherit, so a second repo root
  starts with no daily cap and a `budget:` block written in one root is invisible
  to the other. Config inheritance is **not** implemented here; the warning
  makes its absence visible from inside the root that lacks the ceiling.

### Fixed

- **A partially-profiled baseline no longer reports every unprofiled column as
  newly added** ([#150], [#161]). `maintain snapshot` pinned the exploration
  cache on a presence test where a validity test was needed, and both remaining
  ways a *present* cache can be invalid flowed through it.

  **Too thin.** Past 50 objects `explore map` profiles the top 25 by rank and
  enters the rest as metadata alone. The baseline pinned that without inspecting
  completeness, and because an object with no cached columns had an empty column
  set, the next `maintain check` reported every column of every unprofiled object
  as `column_added`. Measured on one warehouse: 204 objects cached, 46 with
  column detail, **1,548 false `column_added`**, which made a fresh baseline four
  times noisier than the week-stale one it replaced.

  The fix is the encoding, not a filter: **an empty column list in a baseline
  means *unknown*, not *empty*.** Every warehouse object has columns, so holding
  none for one of them records an absence of evidence, never evidence of absence.
  The schema axis now compares no columns for such an object, and says which
  objects and how many rather than going quiet, since an axis that could not
  compare must not look like one that compared and found nothing. Table-level
  findings are unaffected, because identity needs no profile. `maintain snapshot`
  warns at pin time and reports `column_detail_count` beside `dataset_count`, so
  a host automating the accept can gate on coverage structurally.

  **Too old.** The warehouse side was pinned from any usable cache with no
  freshness test, and the staleness warning that would have flagged it compared
  *write times*, so re-pinning made the baseline the newer file and the warning
  vanished while its contents were still that same old cache. The signal
  disappeared exactly when it was most needed, right after an operator was told
  their accept succeeded. Freshness is now judged on the capture time recorded in
  the baseline, so a re-pin cannot silence it, and the threshold is the existing
  `profile_freshness_hours` rather than a new setting.

  Also fixed: dex's own staleness warning recommended `maintain snapshot`, which
  on a warehouse past the rank cutoff is the path that produces the thin
  baseline. It now names `explore map --full` too. The cheap path and the correct
  path were opposites, and the guidance pointed at the cheap one.

- **`transform build` now reports `data.spend`** ([#166]). `stamp_spend` had no
  call site in `transform/`, so `data.spend` was present for explore and
  maintain and absent for builds while the ledger received the entry either way.
  Any consumer summing settled spend from envelopes counted every build as free.

  The fix is not the missing `stamp_spend` call it looks like: a build settles
  outside the cost gate entirely, because dbt executes the statements and
  `record_billed` never fires, so the gate's own total is zero for the run. The
  spend is now assembled where the billed figure already reaches the ledger, in
  the same shape and under the same keys every other command uses
  (`bytes_billed` or `seconds_billed`, plus `session_spent_today`). A failed
  build reports it too, since dbt bills for the statements it ran before it
  stopped, and that number is what sizes the re-run.
- **`explore map` no longer resurrects a relation deleted from the warehouse**
  ([#149]). Carry-forward unions back any prior cached profile this run "did
  not examine," correct when that means outside a narrower `--scope`/
  `--dataset` (issue #111), but the same test also fired when an object was
  simply dropped between two maps: the prior profile came back with its
  original `profiled_at`, `maintain snapshot` pinned the ghost as a real
  dataset, and the next `maintain check` reported it as a high-severity
  `table_dropped` finding for something the operator deliberately removed.
  With `orphan_relation` (#146) shipped, this actively worked against dex's
  own remediation loop: classify an orphan, drop it, re-map, and the re-map
  resurrected exactly what was just cleaned up.

  Neither the cache nor any adapter records what scope built a prior run, so
  "not examined" could not tell "out of scope" from "gone" apart. The fix
  derives the schema/dataset namespaces this run's inventory actually
  observed; an unexamined prior identifier is carried forward only when its
  own namespace was never observed (genuinely out of scope, so #111 still
  holds), and dropped, not carried, when the namespace was observed but the
  object itself is missing from it. `explore map`'s envelope now reports a
  `dropped_count` and a warning naming what was dropped. `explore
  relationships` shares the same carry-forward function and gets the fix for
  free; `explore profile` is unaffected, since neither does a full inventory
  scan wide enough to know what "observed" means.

  Known gap: if an entire schema is emptied out (every object in it dropped,
  not just a few), it still looks identical to "never in scope" and
  resurrects; narrower than the reported repro (a handful of relations gone
  from otherwise-live schemas) and left for a future fix.
- **A refusal no longer reports a metered connector as free** ([#162]). An
  over-ceiling refusal on BigQuery returned an envelope whose `cost.paradigm`
  said `free_local` while the error prose beside it correctly said
  `(bytes_scanned)`. `free_local` is a positive claim that the connector bills
  nothing, so a host branching on the structured field to ask "was this refusal
  about money?" was told no, and a host that trusted the structured field over
  the prose (the right instinct) got the wrong answer.

  **This is a visible change to the envelope contract**, in three parts:

  - `cost.paradigm` is now nullable, and `null` is what a command reports when
    nothing selected a connector. It is no longer possible for an envelope to
    claim `free_local` by omission.
  - `cost.paradigm` names **the connector the command ran against**, not what
    the command happened to cost. A repo-only command such as `transform plans`
    in a BigQuery-configured project now reports `bytes_scanned` where it used
    to report `free_local`. To ask whether a command actually spent anything,
    read `data.spend` and `cost.estimate`, not the paradigm.
  - A refusal now carries the whole cost block. An over-ceiling refusal, a
    confirmed-but-unbudgeted refusal, and a mid-run budget exhaustion all report
    the estimate and the ceiling that bound them alongside the paradigm, so the
    two numbers the error prose names are readable as structured fields.

  `free_local` consequently means DuckDB and nothing else.

## [1.4.3] - 2026-07-28

### Added

- **A storage backend dex does not ship can be selected from configuration**
  ([#157]). `.dex/config.yml` gains a `cache` block, and it is the schema change
  worth reading first:

  ```yaml
  cache:
    backend: mypkg.stores:my_store
    options:
      tenant: acme
  ```

  `backend` defaults to `filesystem`, so a repo that configures nothing behaves
  exactly as it did before the setting existed, and `options` reaches the selected
  backend's factory verbatim. `--cache-backend` overrides the name for one run,
  the way `--connector` overrides the configured connector. **Credentials never
  belong in `options`**: this file is committed, so a backend needing one reads it
  at runtime the way `connect.py` does.

  It is an **open registry, not a closed enum**, which is the part that would have
  been expensive to get wrong: a closed set of shipped names would make out-of-tree
  backends library-only permanently, and opening it later would be a config-schema
  change with a deprecation attached. Three kinds of name resolve, in order: a
  shipped name (`filesystem`), a dotted `mypkg.stores:my_store` path, or a name an
  installed distribution registered under the `exmergo_dex_core.stores`
  entry-point group. A shipped name always wins over a registration, so installing
  a package can never silently move where an existing repo's state lands.

  Before this, a contributor could implement a backend, watch the shipped
  conformance suite go green, and then discover the only way to use it was to stop
  using the CLI. `DexEngine.from_repo` now builds whatever the configuration names
  instead of hardcoding the filesystem backend; an explicitly passed `store=` still
  wins over both, since a caller holding an instance has already made the decision.

  **`memory` is deliberately not selectable.** Each CLI command runs as its own
  process, so a `MemoryStore` would drop the cache between `explore map` and
  `explore query`, and the second command would refuse with "run `explore map`
  first" having just run it. That reads as a broken tool rather than a chosen
  backend, so it refuses by explaining the process boundary. It remains the default
  for a library caller, where one process holds the engine.

  Every failure refuses with a `ConfigurationError` naming the fix: an unknown name
  lists what exists and both open forms, a dotted path that will not import points
  at the environment, a name resolving to something uncallable says so, and a
  factory that builds something which is not a store names the members it lacks.
  The tier check is on the constructed store rather than on the factory, because a
  callable protocol can only verify that `__call__` exists.

- **A storage backend can now say how it is constructed, not just how it behaves**
  ([#156]). #144 made the seam implementable: the tiered `Store` protocol, the
  shipped conformance suite, `py.typed`. What it left undecided was what happens
  between "something names a backend" and "the engine holds a store", and the
  shapes disagree: `FilesystemStore` is built from a repo root, `MemoryStore` from
  nothing, and a backend serving several end users from a tenant id with no
  repository anywhere in the picture. There is no single call that satisfies all
  three, so the contract had to be chosen.

  Construction is now its own small contract, deliberately separate from `Store`.
  A `StoreContext` carries a `repo_root` (`None` when there is no repository) and
  an `options` mapping of the backend's own non-secret coordinates, passed through
  verbatim; a `StoreFactory` is anything callable that turns one into a store. A
  plain function, a class whose `__init__` takes the context, and a classmethod
  all qualify, so a backend is constructable in whatever shape it already has.
  `FilesystemStore.from_context` and `MemoryStore.from_context` are the shipped
  reference implementations, one path-shaped and one that needs nothing.

  **`Store` stays purely structural.** Putting a construction obligation on the
  protocol would have cost the property that makes this seam cheap to implement,
  that a class with the right methods is a store, with no base class to inherit
  and no registration step. A host that builds its own store and passes it to the
  engine is untouched by any of this.

  **Construction is checkable, so the conformance suite's promise stays whole.**
  `StoreFactoryContract` composes with the contract for your tier and routes
  `make_store` through your own factory, so every behavioral and isolation
  assertion then runs against stores built the way dex builds them. "The suite is
  green" therefore still means a backend is correct *and* constructable, rather
  than quietly meaning only the first. The packaging test that installs the wheel
  in an environment with no access to this source tree now builds its backend
  through a factory and a context carrying no repo root, because a construction
  contract that only works from inside this repo has not been tested.

  **No secret ever reaches a `StoreContext`.** `.dex/config.yml` is committed, so
  a password, key, token, or connection string among the options would be a
  credential in version control. A backend reads its credential from the
  environment at construction, exactly as the connection and semantic-layer seams
  already do, or a host with per-request credentials skips the contract and hands
  the engine a store it built itself.

  Both shipped backends refuse an option they would otherwise have ignored, and
  `FilesystemStore.from_context` refuses a context with no repo root rather than
  falling back to the working directory, which would write one project's
  exploration cache into wherever the process happened to start. Accepted-and-
  ignored is worse than rejected: the caller believes a setting took effect and
  nothing in the output says otherwise.

  `references/storage.md` documents the contract, both shapes a factory can take,
  and the two rejected alternatives: always passing a `repo_root`, which cannot
  construct a tenant-keyed backend at all, and requiring a `from_config`
  classmethod, which puts the obligation back on the protocol.
  
- **`maintain check` accepts the same object scope as its focused detectors**
  ([#115]). The paid grain and cardinality axes always estimated and billed for
  every configured dataset, with no way to narrow them; a session focused on one
  environment's marts saw its estimate dominated by dozens of irrelevant raw
  tables, so the whole paid sweep was declined and the layer under active change
  got no grain coverage at all. `maintain check <objects>` now resolves the same
  scope `maintain schema`/`volume`/`grain`/`semantic` already accept, narrowing
  every axis, including the two paid ones, to what was actually asked for.


### Fixed

- **`explore cluster` reported `dropped_null_rows: 0` while its own sample SQL
  was silently dropping rows** ([#160]). `build_sample_sql` puts an
  `IS NOT NULL` predicate for every feature column into the sample query
  itself, so rows with any null feature are filtered by the warehouse and
  never reach Python; the reported count only ever saw rows that arrived, so it
  could count a non-numeric coercion failure but never a null, structurally.
  One production run silently clustered 92% of a table (the missing 8% shared
  a single null feature column) with no note or warning. `explore cluster` now
  runs a companion count query, over the same table and the same sample scope
  as the fetch, that measures exactly what the null filter excludes; a nonzero
  count gets a notes entry attributing it to the responsible feature column(s)
  (`"visits: 20"`, not a bare number) and flags that `total_rows` is
  cache-derived, a different moment than this live count. The clustering
  engine's own count (rows that arrived but failed float coercion, which is
  what the old field actually ever measured) is renamed
  `dropped_non_numeric_rows` so the two are never conflated again.

  **Cost note**: on a billed connector, `explore cluster`'s estimate roughly
  doubles, since two queries now price into it instead of one; each still
  scans only the feature columns over the same sample scope, so the added cost
  is the same order of magnitude as the sample fetch itself, not a full-table
  scan.
- **Low-cardinality enumerations (weekday names, month names, status codes) no
  longer keep a blocking name-only PII flag** ([#167]). A string column matching
  the generic `*_name` pattern (`day_name`, `month_name`) starts at 0.6
  confidence, above the query firewall's 0.5 blocking threshold; value-shape
  profiling can already de-rate a name-only flag when the values are visibly an
  all-caps reference vocabulary or long multi-token labels, but a closed set of
  single-token Title Case values (`Monday`, `January`) matched neither rule, so
  a conventional date dimension's weekday/month columns stayed blocked on every
  fresh re-profile. Cardinality is now its own corroborating signal: a column
  whose distinct count is small both in absolute terms and as a fraction of
  non-null rows de-rates the same way the existing shape rules do. The fraction
  half is the guard on the guard, verbatim from the report: a genuinely small
  table of distinct people has a low absolute distinct count but a *high*
  fraction (most rows are their own distinct value), so it is not cleared by
  this rule, and a person-shaped distribution still corroborates as a real name
  before cardinality is ever considered. The flag itself is never removed,
  consistent with every other shape rule; only where it lands relative to the
  blocking threshold.

- **`maintain snapshot` told every host to commit a file it may not have**
  ([#157]). The hint was a fixed string: "commit `.dex/snapshot.json` like a
  lockfile". A backend that keeps the baseline as a row or a document has no such
  file, so the advice named something that does not exist, while the
  `snapshot_path` beside it correctly reported that backend's own locator. The
  half that holds everywhere, re-pinning after each known-good build, is now what
  a backend dex does not ship gets; the git half is added only when the baseline
  really is a file in the repo. Nothing changes for the filesystem backend.
  
- **`maintain semantic`'s paid cardinality scan now actually narrows on scope,
  not just its reported findings** ([#115]). `cardinality_plan` built
  its scan over every semantic model's categorical dimensions regardless of the
  requested object scope; only the findings returned to the caller were filtered
  afterward, so a scoped run still paid for the unscoped one. The scope (an
  identifier, column, dimension, or semantic model name, the same vocabulary the
  reported findings already matched against) now filters before estimation and
  execution, so a narrower run is priced and billed for less.

- **The shipped conformance suite could not be run by two of the backends it was
  written for** ([#174]). Both defects were in how the suite is packaged and how it
  drives a backend, not in the storage contract, so nothing changes about what a
  store owes its callers. Both were reported by an implementer running a full-tier,
  tenant-keyed backend against 1.4.2 from outside this repository, and both surfaced
  as assertion failures attributed to that backend rather than as anything naming
  the suite.

  `[storage-conformance]` shipped only pytest, while the ten plan assertions build a
  `TransformPlan`, which reaches the SQL guard and so the dialect engine. Anyone
  implementing the full `Store` tier got ten failures on a clean install. The extra
  now self-references `[sql]`, the same treatment every connector extra already had.
  The explore tier still installs and runs with no dialect engine, and a packaging
  test now holds that line rather than leaving it to `a_plan`'s lazy import alone.

  The suite also drove all 34 assertions through one key and never reset, so a
  backend where two instances built from one key see the same state, which is the
  defining property of every durable backend, inherited the previous assertion's
  writes and failed nine of them. Every assertion now gets its own key, unique to
  the run, so a durable backend passes unmodified with no reset hook and no fixture
  of its own. A backend that resets on every call is unaffected: it receives
  different strings and nothing else changes. `make_store` now documents what the
  suite guarantees about key reuse and, just as importantly, what it does not
  require, since the old contract was silent in both directions.

  Neither gap was visible in-repo, because every backend the contract had ever run
  against reset itself. There is now an in-repo backend that does not, and the
  out-of-tree packaging test exercises the full tier instead of only the explore
  tier.

## [1.4.2] - 2026-07-28

### Changed

- **Every refusal dex raises deliberately is catchable as one type** ([#144]).
  The CLI renders refusals into an error envelope behind a catch-all, so it never
  needed a hierarchy; a library consumer cannot do that, and catching `Exception`
  at an API boundary swallows real bugs alongside deliberate refusals. All of
  them now descend from `DexError`, with `ConfigurationError` for an engine built
  without something it needs and `RequestError` for a call that named something
  unusable. `NoConnectorSelectedError`, `RepoRootRequiredError`, and
  `StoreRequiredError` replace the bare `ValueError`s that the public API raised
  for the three refusals a host hits most.

  **Nothing breaks for an existing consumer.** Both families also inherit
  `ValueError`, which is what those refusals raised before they were typed, so
  code written against the previous API keeps catching what it caught.

  Two families name the distinctions a host actually branches on.
  **`PrerequisiteError`** covers refusals where the engine is fine, the call is
  fine, and some state does not exist yet that a named command creates:
  `CacheRequiredError` and `NoBaselineError`. It is the one family a caller can
  resolve automatically, by running the command the message names and retrying.
  **`ConnectorError`** covers a warehouse connection that could not be
  established, so a host writes one `except` instead of importing a name per
  connector. `CredentialDiscoveryError` is deliberately outside it: a credential
  that was never configured will not appear on a retry.

  **Eleven refusals that were typed but not importable are now exported**:
  `PrerequisiteError`, `CacheRequiredError`, `NoBaselineError`, `ConnectorError`,
  `CredentialDiscoveryError`, `ScopeError`, `ClusterError`,
  `ClusterDependencyError`, `DialectDependencyError`, `PlanError`, and
  `PlanNotFoundError`. Being a distinct class is not enough on its own: with no
  importable name, a consumer branches by matching on message prose, and prose is
  not an interface anyone owes stability on, so rewording an error silently
  changes which branch a caller takes. `PlanNotFoundError` is the sharpest case,
  because `Store.load_plan` is documented as raising it and a third-party backend
  could not satisfy that contract without importing from a private module. A test
  now asserts every refusal reachable from the public API is importable, with an
  explicit internal set, so the next one added forces a decision.

  `SemanticBackendError` and `SemanticQueryRefusedError` are now exported from the
  package root. They are the documented catch for the hosted semantic-layer path
  and previously required importing from `exmergo_dex_core.explore.semantic`, a
  module layout that was never public surface.

### Added

- **The storage seam is a public extension point: a shipped conformance suite, a
  tiered protocol, and a typed package** ([#144]). `.dex/` state has lived behind
  a `Store` protocol since the storage seam landed, but the seam was only usable
  by someone willing to read the engine's source and infer several contracts that
  existed nowhere but its tests. It is now implementable from what is published.

  The protocol is three nested tiers, so a backend implements what its host
  actually uses instead of stubbing what it does not. `ExploreStore` is six
  members (the exploration cache, the two ledgers, locators) and covers a host
  that explores, profiles, and queries; `MaintainStore` adds the reconcile
  baseline and the drift report; `Store` adds the five transform plan members and
  is unchanged in shape, so existing annotations and backends keep working. The
  transform surface is the one place the widest tier is required, and it now
  refuses an explore-only store by naming the tier and the missing members rather
  than failing on a missing attribute several frames down.

  `exmergo_dex_core.storage.conformance` ships the contract as an executable
  suite. A backend outside this distribution installs `[storage-conformance]`, subclasses
  the class matching its tier, supplies a `make_store(key)` factory, and runs
  every assertion dex holds its own backends to, isolation across keys included.
  A packaging test proves this end to end: it builds the wheel, installs it in an
  environment with no access to this source tree, writes a backend there against
  the published protocol alone, and runs the shipped suite at it.

  The package now ships a `py.typed` marker. The seam is enforced entirely by
  structural typing, so without it a downstream type checker treated the whole
  package as untyped and verified nothing about a candidate backend.

  Six behavioral contracts that previously existed only inside the test tree are
  written onto the protocol members they govern: documents do not alias the
  caller's object in either direction, `save_cache` stamps the caller's object as
  well as the stored copy, documents round-trip as pydantic models, a corrupt
  document raises while a corrupt ledger line is skipped, a stored
  `schema_version` is not the store's to police, and the ledger read-then-write is
  not atomic at the protocol level (the session ceiling binds exactly under
  serialized commands, and the overshoot under concurrency is bounded by the sum
  of the concurrent estimates). `save_cache` is now explicitly a whole-document
  atomic write: cache membership decides what a query may name, so a backend with
  a document size cap chunks internally rather than getting a per-dataset seam
  that would let a reader observe half a cache.

  A seventh contract, reported from the field: **the spend ledger is scoped to the
  store instance**, so store granularity is ceiling granularity. One principal
  spanning two stores has two independent session ceilings and nothing bounds
  their sum. The existing docstring was specific about `field` and `connector`
  keeping paradigms apart, which read as the complete set of scoping rules, and
  the store axis was the one it omitted. It errs permissive and nothing warns, so
  a host federating state per user should key stores exactly as it keys
  principals.

  Every assertion in the shipped contract now carries a message naming the rule it
  enforces. In-repo a bare comparison was fine because a failure sent you to the
  source; as the artifact an outside implementer debugs against, the assertion
  text is the documentation. The no-aliasing and clock-stamping rules matter most
  there, since both fail silently and late and present as data corruption rather
  than as a protocol violation.

  New reference page `references/storage.md` covers the tiers, those contracts,
  which calls need nothing on the filesystem, and how a backend is selected.

### Fixed

- **`explore query` docs now say row-major, matching what `cells` actually
  returns** ([#152]). The `explore` skill and command contract described
  `query` results as columnar, but `cells` is a list of rows; a square-ish
  result set could be silently misread with no error. Docs updated to say
  row-major across `explore query` and `explore semantic query`; no behavior
  change.

## [1.4.1] - 2026-07-26

### Added

- **`maintain check` classifies orphaned warehouse relations** ([#146]). A model
  that is renamed or removed leaves its old relation behind in the warehouse, since
  dbt never drops it, so the leftover table was either invisible to drift detection
  or misreported as an unrelated finding. A new `orphan_relation` finding fires when
  a relation was model-backed or source-backed at the last snapshot, no longer is,
  and is still present in the warehouse. `maintain reconcile` proposes the matching
  `DROP TABLE` as an advisory statement for a human to run, never executing it, and
  `explore map --use-project` down-ranks and badges orphans so they stop surfacing
  as top objects. Detection and advice only: the drop itself stays manual.

### Changed

- **The hosted semantic layer is a genuinely standalone install: no warehouse
  client, no dbt-core, no SQL parser.** `[semantic-api]` used to require a dialect
  engine it could never use, because dbt Cloud renders and executes the query
  server-side and dex never sees a statement to validate. The coupling was one
  float: the PII block threshold lived in the query firewall, so reading it pulled
  in a SQL parser. The threshold now lives with the guards' shared policy, where
  every surface that gates on PII can read it without paying for a parser, and the
  `explore semantic` handlers moved out of the explore command module (which
  imports the firewall for the commands that do parse SQL) into the semantic
  package. `DexEngine.semantic_list` and `semantic_query` route there too, so a
  host serving metric queries with a `SemanticSource` and nothing on the filesystem
  needs only this extra, which is what the documentation already promised.

  A command that does parse SQL, run on an install that named no connector, now
  refuses and names the extra to install. There is deliberately no weaker fallback
  check: a query dex cannot parse is a query it cannot promise is read-only, so
  refusing is the only safe answer. `explore semantic list --local`, documented as
  a read-view needing no extra, works on a bare install for the first time.

### Fixed

- **Extras no longer under-declare what the code imports, and the sqlglot bound no
  longer lags what the code needs.** sqlglot was pinned inside each of the six
  connector extras, so any install that picked no connector failed on an import the
  metadata never promised: `[semantic-api]` could not import the hosted dbt Cloud
  backend at all, and `[cluster]` alone and a bare install failed the same way. The
  CLI caught the `ModuleNotFoundError` and reported it inside an error envelope, so
  it read as a broken environment rather than a packaging bug. Compounding it, the
  declared floor was `>=25` while the firewall's unnest allowlist referenced
  expression classes that only exist from 28.6 (`exp.JSONKeys`), so an environment
  whose resolver settled on an older sqlglot satisfied the metadata and then failed
  on an `AttributeError` at import.

  sqlglot is now declared exactly once, in a `[sql]` extra that every extra
  reaching a warehouse self-references, and bounded at both ends: `>=28.6,<31`.
  There is a ceiling because the firewall matches expression classes by name at
  module scope and sqlglot majors have already broken this code twice (the `Union`
  to `SetOperation` and `from` to `from_` renames both needed accommodating), so an
  open bound let a future release break every new install on something no test here
  could anticipate. Two guards keep the pair honest: a packaging test installs the
  wheel at exactly the declared floor and imports the guards, and a new advisory
  `sqlglot-canary` CI job runs the guards and the safety spine against the newest
  sqlglot on every push, so a breaking release surfaces on our side first and
  raising the ceiling is a deliberate act taken on evidence.

- **`[all]` now installs every optional capability, not just every connector.**
  The extra covered the six connectors and stopped there, so an install documented
  as everything silently omitted both semantic-layer backends (`[semantic]`,
  `[semantic-api]`) and clustering (`[cluster]`), and a user who asked for all of
  it still got failures telling them to install an extra they had already named. It
  self-references the other extras, so their requirement lists stay defined once,
  and a packaging test now asserts the reference list covers every declared extra
  except `dev` (contributor tooling, not a capability), then installs it and imports
  every client, dbt adapter, and semantic backend to prove they co-resolve. `[all]`
  is correspondingly the heaviest install available; the light default and the
  `[duckdb]` on-ramp are unchanged.

- **Windows path separators no longer break model-name parsing** ([#146]). The
  project file index keyed its entries with OS-native separators, while every
  consumer of those keys splits on `/`: the transform layer's model-name parsing,
  scaffolded model paths, and the relation names the orphan classification above
  depends on. On Windows they all quietly found nothing rather than failing loudly.
  Keys are now posix-separated regardless of platform.

## [1.4.0] - 2026-07-26

### Added

- **A public Python API: `from exmergo_dex_core import DexEngine`** ([#138]). The
  engine had no library contract, so consuming it from another Python project
  meant fabricating argparse namespaces or shelling out and parsing stdout JSON,
  and doing either materialized a `.dex/` directory in the host project as an
  unrequested side effect. `DexEngine` owns a connection, a configuration, and a
  store, and exposes one method per subcommand:

  ```python
  from exmergo_dex_core import DexEngine

  with DexEngine(connector="duckdb", path="shop.duckdb") as eng:
      mapped = eng.map()
      rows = eng.query("select status, count(*) from orders group by status")
  ```

  Methods return domain objects and result records carrying the counts, notes,
  warnings, and diffs that explain them; the stdout envelope stays at the CLI
  boundary. The default store keeps state in the process, so importing the
  package writes nothing; `DexEngine.from_repo(repo_root)` opts in to a project on
  disk. A `DexConfig` can be passed directly, with no `.dex/config.yml` present
  anywhere, and an engine given one reads no config file at all, so a process
  serving more than one principal cannot inherit a stray config from its
  filesystem. Confirmation and budget refusals become exceptions
  (`ConfirmationRequiredError` carrying the estimate and the payload needed to
  re-issue, `OverCeilingError`, `CeilingRequiredError`), except for the
  two-phase commands, where the ask rides back on the result alongside the work
  already paid for. `DexConfig`, `DexCache`, `Dataset`, `Snapshot`, `Store`,
  `MemoryStore`, and `FilesystemStore` are exported alongside it.

  The CLI is now the API's first consumer rather than a parallel
  implementation: it parses arguments, builds an engine, and wraps the result.
  User-visible CLI behavior is unchanged.

- **A host can supply the warehouse connection: `ConnectionSource`** ([#142]).
  Credentials were resolved only from process-ambient state (Application Default
  Credentials, `~/.databrickscfg`, `connections.toml`, the `PG*` environment, the
  AWS credential chain). That is exactly right for a CLI one person runs, and it
  is why dex needs no credential configuration of its own, but ambient is
  process-wide: a container serving several end users could only reach the
  warehouse under one shared identity, so per-end-user access control was not
  expressible whatever it did with the store or the config. `DexEngine` now takes
  a `connection=`, and the connector's own discovery is skipped:

  ```python
  from exmergo_dex_core import ConnectionSource, DexEngine

  with DexEngine(
      connector="snowflake",
      config=cfg,
      store=store,
      connection=ConnectionSource(connect=lambda: user_conn),
  ) as eng:
      eng.inventory()
  ```

  It carries a zero-argument factory rather than a live connection, so a free
  metadata command never opens a billed session; on Databricks it carries the
  Unity Catalog client alongside it, because metadata and billed SQL are two
  different doors there. On BigQuery it carries a credential, whose principal
  class dex derives from the credential itself rather than taking the host's word
  for it, so `capabilities()` stays truthful about what the process is connected
  as. Supported on all five warehouse connectors and refused on DuckDB, whose
  target is a local file dex opens read-only itself.

  The host owns authentication and therefore identity. It does not own the
  guards: dex still builds the cost gate from the injected store, so the
  per-command ceiling and the cumulative session ceiling bind on an injected
  connection exactly as on a discovered one, scope still narrows inward only, and
  reads stay read-only. dex also closes nothing it reached through the source,
  since the caller that opened a connection is the one still holding it. No
  credential is stored, cached, or refreshed anywhere, and `.dex/` stays
  secret-free. CLI behavior is unchanged: it passes no connection and every
  connector discovers credentials exactly as before.

- **The hosted semantic layer's token is injectable too: `SemanticSource`**
  ([#142]). The dbt Cloud Semantic Layer is a sixth credential path, and it was
  the one the connection seam above did not cover: the token came from
  `DBT_SL_TOKEN`, then `~/.dbt/dbt_cloud.yml`, and the backend read the process
  environment directly, so it was not reachable through any argument a caller
  could pass. Being process-global made it worse than merely absent, since the
  only way to vary it per request was to mutate the environment and race one
  user's token against another's.

  ```python
  from exmergo_dex_core import DexConfig, DexEngine, SemanticSource

  config = DexConfig(
      semantic={"backend": "dbt_cloud", "host": host, "environment_id": env_id}
  )
  with DexEngine(
      config=config, semantic_source=SemanticSource(token=lambda: token_for(user))
  ) as eng:
      catalog = eng.semantic_list()
  ```

  It carries a callable rather than a string, so a rotating token is re-read
  without rebuilding the engine, and it runs once per semantic command rather than
  once per HTTP call, because one metric query can poll dbt Cloud many times while
  it runs. The non-secret coordinates stay in the `semantic` config block, which
  is committed and already injectable; the token never goes there. When a source is
  supplied nothing ambient is consulted, including the environment's coordinates,
  so a single stray `DBT_SL_HOST` cannot redirect every tenant's metric query at
  once. This makes the hosted metric surface reachable with nothing on the
  filesystem: no dbt project, no store, no connector, no credential file. Supplying
  the token buys identity and not policy: the PII dimension gate still refuses the
  same dimensions, and the hosted path's cost posture is unchanged, since dbt Cloud
  owns that warehouse connection and executes server-side. A source passed to the
  local backend is refused rather than ignored, because it has no meaning there.

- **A runnable library example, and tests that install the package to run it**
  ([#138]). `packages/dex-core/examples/quickstart.py` walks the whole flow in
  one file: map a warehouse, read the inferred joins off the returned cache,
  see a column flagged as PII, ask a question, and watch the query firewall
  refuse one that would have projected the PII. It creates its own throwaway
  DuckDB file, so it runs anywhere. A new packaging suite builds a wheel,
  installs it into an isolated environment, and runs that example against it,
  which is the first coverage of what a consumer actually gets rather than what
  the source tree happens to provide: a bare install imports, no connector
  client is pulled in at import, the `dex` console script still prints exactly
  one envelope, and the documented example cannot rot.

### Changed

- **`.dex/` state now lives behind an injectable storage seam** ([#137]). All
  persistence used to run through two concrete, filesystem-bound classes that
  every call site constructed for itself from a repo root, so there was no way to
  substitute a non-filesystem backend. There is now a `Store` protocol in a new
  `exmergo_dex_core.storage` package, with two implementations: `FilesystemStore`
  (plain files under `.dex/`, byte for byte what shipped before, and still what
  the CLI uses) and `MemoryStore` (in-process, retains everything for the process
  lifetime, writes nothing to disk). The backend is chosen once, at the CLI entry
  point, and passed to every command. This is internal plumbing: the command
  surface, every envelope field, and all user-visible behavior are unchanged. The
  dbt project remains the source of truth and stays a git-reviewable filesystem
  artifact regardless of backend; only the non-canonical scratch cache moves.
  Internally, `DexStore` and `PlanStore` are replaced by the single
  `FilesystemStore`, and the envelope's `cache_path`, `snapshot_path`,
  `drift_path`, and `plan_path` now carry a backend-supplied locator string
  (an absolute path, as before, on the filesystem backend).

- **Cache and baseline refusals no longer name a file** ([#138]). Messages like
  "no .dex/cache.json in this repo" were correct only for the filesystem
  backend, which was the only one a user could reach until the API landed. They
  now name the state and keep the actionable half ("no exploration cache yet;
  run `explore map` first"), so the same refusal reads correctly whatever backs
  the store. The commands that report where something was written still report a
  real location, supplied by the backend.

### Fixed

- **A project-less deployment gets a real refusal from `explore semantic`**
  ([#142]). The local backend is the default, so a caller with no dbt project on
  disk landed there without asking to, and the failure surfaced as a bare
  `ValueError` about a missing repo root, from a frame that names neither the
  backend that needed the project nor the alternative. `resolve_backend` documents
  that it raises `SemanticBackendError` and nothing else; it now does, and the
  message names the choice: set `semantic.backend: dbt_cloud`, which needs no
  project and no local credential.

- **The `transform` dev-target preflight reaches billed connectors again**
  ([#137]). The storage seam gave `open_adapter` a required store on metered
  connectors, but the two preflight call sites in `transform/dev_target.py` still
  opened without one. Every free existence, privilege, and content probe on
  BigQuery, Snowflake, Databricks, Postgres, and Redshift therefore degraded to
  "could not preflight the dev database", so `transform build` no longer refused
  a missing Snowflake database or Databricks catalog, and `transform init` no
  longer warned about a dev namespace that already held objects. Both call sites
  now receive the store the command already holds. The degradation was silent by
  design, because a connection dex cannot open must never break a build dbt could
  have run, so there are now tests that fail if either caller stops passing it.

- **The `transform` dev-target preflight no longer re-reads config from disk**
  ([#142]). The same two call sites passed no configuration either, so they
  resolved one by walking up from the working directory even when the caller had
  supplied a `DexConfig` outright. An engine given an explicit config is promised
  that no file is read, precisely so a process serving several principals cannot
  inherit a stray `.dex/config.yml` from its filesystem, and the preflight was
  the one place that promise did not hold. Both sites now probe against the
  configuration and the connection the command itself is using, so the check and
  the build it precedes cannot disagree about the target or the principal.

- **`explore profile` now honors the same fresh-profile reuse as `explore map`
  and `explore relationships`** ([#128]). Profiling a table whose cached profile
  is still fresh (same connector, schema unchanged, profiled within
  `profile_freshness_hours`) is served from the `.dex/` cache for free instead
  of re-scanned, so a `profile` run right after a `map` no longer pays a second
  time. The envelope gains `cache_hit_count` and `profiled_count`, and a
  fresh-cached object never enters the cost preflight or the billed handshake.
  `explore profile` also gains the `--refresh` flag its sibling commands already
  had, to force a full re-profile when the source changed in a way the free
  metadata check cannot see.

## [1.3.1] - 2026-07-21

### Changed

- **`transform build` now surfaces an upfront cost estimate on every billed
  connector.** Previously a build asked for a `--budget` with no number to base
  it on, framed as a BigQuery limitation ("dbt has no dry-run"). dex now runs a
  free `dbt compile` and prices each compiled node (models, snapshots, and tests)
  through the connector's own execution-free estimator, the same one `explore`
  uses: a real dry-run on BigQuery, an `EXPLAIN` planner cost on Postgres, and an
  honestly-labeled size heuristic on Snowflake, Databricks, and Redshift. The
  summed estimate rides the `needs_confirmation` handshake in the connector's
  unit (bytes, warehouse-seconds, or database-seconds) with the same
  `estimate_quality` label. On a cold dev target, nodes whose inputs are not
  built yet cannot be priced and are noted, so the total is an honest partial
  floor; when dex cannot open its own connection the estimate degrades to none
  with a note, and the ceiling plus the server-side per-statement cap still bind.

### Added

- **`transform`: first-class, guarded model deletes.** Plan edits now carry an
  `op` of `upsert` (create or update, the default) or `delete`, so removing a dbt
  model is a reviewable diff inside the plan rather than a manual `rm` outside the
  tool. A delete is pinned to the file's hash like any other edit, so an
  unconfirmed delete against a file a human edited after planning returns
  `needs_confirmation` instead of silently removing it. Deletes are guarded as a
  unit: a plan is refused, naming the offenders, if any file that survives it
  still `ref()`s a deleted model, so the post-deletion project is proven free of
  dangling references before the plan is stored; when dbt is available, the same
  post-deletion tree is confirmed by dbt's own parser. This makes a rename or
  reclassification a single atomic plan (delete the old model, create the new one,
  update every referrer), closing the audit gap where a removal escaped the
  reviewable-diff guarantee. The delete path is file-level and connector-agnostic:
  it never issues DDL, so it only ever removes a file from the repo, never a
  relation from the warehouse.

### Fixed

- **`explore profile`'s BigQuery cost estimate now accounts for the per-query
  billing floor on every query a profile can issue, not just the aggregate
  scan** ([#107]). BigQuery bills at least 10 MB per query; the estimate
  already floored each aggregate batch, but a profile can also issue up to two
  more queries per table (an exact-distinct-count escalation for a
  near-unique column, a composite-key probe), priced only after the aggregate
  batch's own approximate results come back. Those were invisible to the
  upfront estimate entirely, so a batch of small tables billed up to 1.7x the
  quoted number. The estimate now reserves a floor for both possible
  escalations per table (skipped only for a table provably empty at estimate
  time), turning the number into a ceiling actual spend will not exceed
  rather than one it silently blows past. The two escalation queries'
  own internal budget accounting is now floored the same way, and BigQuery's
  confirmation handshake now names the floor directly in its hint.
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
- **A scoped `explore map` no longer drops out-of-scope datasets from the
  cache** ([#111]). `explore map --scope <dataset>` (or `--dataset`) rebuilt
  `.dex/cache.json` from only this run's inventory, so a scope narrower than
  what built the existing cache silently discarded every other dataset's
  profile, with `carried_forward_count: 0` and no warning: a subsequent
  `explore query` against a dropped-but-still-real table then refused with
  "not in the .dex cache". A prior dataset entirely absent from this run's
  inventory is now carried forward untouched, and its own count
  (`out_of_scope_carried_count`) and a note say so explicitly. `explore
  relationships` and `explore map` also both used to replace the cache's
  relationships wholesale from this run's inference alone; a relationship
  with an endpoint outside what this run examined is now carried forward the
  same way (`carried_relationship_count`), since neither declared-resolution
  nor inference had visibility into that endpoint to regenerate or supersede
  it. Re-running with the same scope as before was already unaffected by
  this; only a scope *change* between runs triggered the loss.

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
