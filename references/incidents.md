# Test failures as incidents

A failing dbt test is one data point. The on-call question is larger: which tests
have been failing, since when, and whether they are still failing. dex answers it
by folding the outcomes each source records into **incidents**, one per failure
of one check, from the first observed failure to the latest.

dex reads more than one source of test outcomes: the `run_results.json` dbt
writes for each invocation, and, where the project installs the Elementary
package, the history of test results the package uploads to the warehouse. Every
source is turned into the same observations, and **one rule** turns observations
into incidents, so two sources can never open or close an incident by different
rules.

The rule lives in the engine, in `exmergo_dex_core.maintain.incidents`, as one
pure function, `fold_incidents`. It opens no connection, reads no file, runs no
dbt, and keeps nothing between calls: each call derives the incidents from the
history the sources hold at that moment.

## Three states, never two

Every interval of a check's history is one of three things:

- **observed good**: a source recorded the test passing;
- **observed bad**: a source recorded it failing (an error or a warning counts);
- **unobserved**: nothing recorded an outcome. dbt did not run, the test was
  skipped or not selected, the package's hooks did not run, test-result uploads
  were off, or retention removed the rows.

Nothing reads an unobserved interval as good. A run that did not check a test
looks the same as a run in which it passed unless something says otherwise, and
reading the gap as a pass would place the start of a failure in the wrong place.

## What an incident is about

An incident is keyed by a **subject** and a **signal**.

- The **subject** is the claim id in `config.meta.dex.claim` where dex rendered
  the test from a declaration, otherwise the test's dbt `unique_id`. The kind of
  key is part of the subject, so a claim id and a `unique_id` never collide.
  Because a claim id survives a test's rename, a renamed test checking the same
  claim stays one incident. See "The claim each test checks" in the transform
  skill's declarations reference.
- The **signal** is what failed beyond the subject: the column and the metric of
  a test that writes one row per column, such as an Elementary anomaly test, and
  nothing otherwise. It never holds the test's name.

## The lifecycle

| Event | Effect |
| --- | --- |
| An observed failure for a subject and signal | Opens an incident, or extends the open one. The source that observed it is the incident's opening source. |
| `N` consecutive observed passes from the opening source | Closes it. `N` is set per subject and signal and defaults to 2, so a test that flaps stays one incident. `N` counts runs, not rows: see below. |
| An unobserved interval | Neither opens nor closes an incident, and does not break a run of passes: `N` counts observed passes. |
| The check stops running | Ends it as `no_longer_observed`, never as `closed`. |
| An observation from another source that disagrees | Attached as evidence. It never counts toward `N` and never closes the incident. |

Observations are ordered by invocation, not by clock, because sources stamp times
with different clocks. Within one invocation a failure is read before a pass, so a
source that passes in the same run another fails is evidence, not the opener.

### One outcome per source per run

A source has one outcome for a subject and signal in each run. Where it holds
more than one row for that run (two tests carrying one claim, or an adapter that
did not deduplicate), a failure wins over a pass, as on the coverage timeline.
So `N` counts runs: two passing rows in one run are one pass, and a run that
failed is never also counted as a pass. Every row is still kept on the
incident, so its tests and its latest severity are as the sources wrote them.

### One invocation order for every source

A run id names one invocation, so it has one place in invocation order whichever
source holds it, and that place holds no other run. Across the observations, the
runs each `SourceHistory` holds and the timelines, `fold_incidents` refuses with
a `FoldInputError`:

- `run_sequence_conflict`: one run id that carries two `sequence` values;
- `sequence_run_conflict`: one `sequence` that carries two run ids, which is
  what an adapter numbering its own runs from 1 most often produces.

Reading such input would order one source's outcomes against another's wrongly,
so the adapters have to number every source's runs from one series.

### Which source opens a tie

Where more than one source fails in the run that opens an incident, the source
named first in `sources` opens it. The order of `sources` is the only precedence:
source names are never compared. A tie that involves any source `sources` does not
name is refused, even where another tied source is named,, with `code` `opener_tie_undeclared`, rather than settled by an
order nobody declared.

The choice matters, because only the opening source's passes close an incident.
Name first the source that holds the longer run history of a check: a source
that holds only the invocation it read cannot supply `N` passes, so an incident
it opens stays open until the check stops running.

### When a check has stopped running

At call time, an open incident ends as `no_longer_observed` when either holds:

- **removed**: its subject is not among the checks the project currently has (the
  test was removed or disabled);
- **stale**: its opening source has not observed it for longer than the expected
  schedule allows (the test was deselected, or the source stopped uploading).

The expected schedule is **derived** from the history, never assumed: the median
gap between the subject's own observations, or, where it has fewer than three,
between the runs its source holds. A check is stale after three expected gaps
with no observation, and never in under an hour: a burst of builds run by hand
seconds apart is not a schedule. With fewer than three dated points the schedule is
`unknown`, and nothing is ended for staleness. Every incident still open or ended
at call time reports the schedule it was judged against, and which series it was
derived from.

A gap that later resumed is history, not an ending: an unobserved interval
between two failures leaves one incident.

### When it started

`first_observed` is the incident's first observed failure only when an observed
pass precedes it. Where no source holds a pass before the first failure, the
failure may have begun before the oldest point any source still holds, so
`first_observed` is `unknown`, never the run that happened to be read first. A
single `run_results.json` holds one invocation, so every incident read from it
alone starts `unknown`.

## Fields

| Field | What it holds |
| --- | --- |
| `subject` | `kind` (`claim` or `unique_id`) and `key` |
| `signal` | `column` and `metric`, each `null` for a test that writes one row |
| `tests` | the test `unique_id` values seen, more than one where a claimed test was renamed |
| `first_observed` | the run, time and source of the first observed failure, or `unknown` |
| `last_observed` | the run, time and source of the latest observed failure |
| `observations` | each observation the incident rests on: `run`, `at`, `commit`, `source`, `outcome`, `severity`, `test`, and `evidence` where it came from another source and disagreed |
| `severity` | the latest failure's severity, in the source's own words, never mapped onto a dex scale |
| `opening_source` | the source whose observation opened the incident |
| `coverage` | the opening source's timeline from the first failure to the close (or its latest run): `heartbeat`, the run series it was laid on, and `segments`, each `observed_good`, `observed_bad` or `unobserved`, an unobserved one with its `reason` and a `missed` segment with the runs the schedule expected in it |
| `onset` | where the check broke: `after` the last observed-good run before the first failure (or `unknown`), `through` that failure, each with its `run`, `sequence`, `at`, `commit` and `commit_basis`, the unobserved runs, missed runs and reasons between them, and `commits`, the commit range read from the repository's history (`null` where no history was passed) |
| `retention_bound` | per source, the oldest point it still holds, as read at call time, or `unknown` |
| `state` | `open`, `closed` or `no_longer_observed` |
| `closed_by` | on a closed incident, the pass that closed it |
| `ended_reason` | on a `no_longer_observed` incident, `removed` or `stale` |
| `schedule` | on an incident open or ended at call time, the schedule staleness was judged against: `basis` (`derived` or `unknown`), `series` (`subject` or `source`), `median_gap_seconds`, `points`, and `allowance_seconds`, how long without an observation reads as stale |

## The coverage timeline

An incident's coverage, and the history a diagnosis reads, are laid on one
source's **timeline**: its runs in invocation order, with every interval that
recorded no outcome marked unobserved, and why. The timeline lives in
`exmergo_dex_core.maintain.timeline` and is pure logic too: the adapters supply
the runs and the observations.

### The heartbeat

A timeline is laid on a **heartbeat**, a series that shows a run happened even
when it recorded nothing for a test:

| Basis | When |
| --- | --- |
| `dbt_invocations` | Elementary uploads its invocations table, one row per run. The default. |
| `orchestrator` | The invocation upload is off (`disable_dbt_invocation_autoupload`), and the orchestrator's run history is available. |
| `snapshots` | Neither, and a `maintain snapshot` series is. Coarser: one point per snapshot, not per run. |
| `observations` | None of them: only the runs the observations name, which cannot show a run that recorded nothing. |

Only invocations that run tests (`build`, `test`) are heartbeat points: a
`dbt compile` writes an invocation row too, and checks nothing.

The timeline names its basis, the invocation-upload setting as the package's
configuration gives it (`on`, `off` or `unknown`), and why it fell back
(`invocation_upload_off`, or `no_invocations` where the table held no rows).

### Why an interval is unobserved

| Reason | What it means |
| --- | --- |
| `missed_run` | The heartbeat has a gap longer than the expected schedule allows: dbt did not run, or ran with the package's hooks off, which writes no invocation row either. |
| `snapshot_missed` | On the snapshot series, a snapshot the schedule expected is missing. |
| `hooks_off` | The run happened (the orchestrator lists it), and the package's hooks did not run. |
| `uploads_off` | The run happened, and the hooks ran with the test-results upload off. |
| `retention` | Before the oldest point the source still holds. |
| `not_checked` | The run recorded outcomes, and none for this test: it was deselected, skipped, or did not exist yet. |

dbt's own test materialization is not among them. Where the package's
materialization is not in use, Elementary still uploads a row for every test dbt
ran, built from the test's definition, so the test is observed; only its sampled
rows and failed-row counts are missing.

### Gaps against the schedule

The expected gap between heartbeat points is **declared** by the caller, or
**derived** as the median gap between them (three dated points or more), or
`unknown`, in which case no gap is read as missed. A gap more than one and a half
expected gaps long holds at least one missed run, and the timeline estimates how
many. With a derived schedule a gap must also exceed an hour, for the same reason
the staleness allowance does: builds run by hand seconds apart are not a schedule.
A declared schedule has no floor.

### Order and commits

Runs are ordered by invocation, never by clock. Each run carries its commit and
where that commit came from, in this order:

1. `git_sha`: the commit the run's invocation records (in Elementary's
   invocations table). Elementary fills it only from `DBT_GIT_SHA`,
   `DBT_CLOUD_GIT_SHA` or `GITHUB_SHA`, so it is empty under most other
   orchestrators.
2. `orchestrator`: the commit the orchestrator records for the same run, where
   it records one. The adapter that reads the orchestrator's run history matches
   its run to the invocation and supplies the commit. Orchestrators that record
   one per run include dbt Cloud (each run's `git_sha`), GitHub Actions (each
   workflow run's `head_sha`), GitLab CI (each pipeline's `sha`) and Airflow 3
   with a Git Dag bundle (each Dag run's bundle version, the commit it ran).
   Dagster+ exposes the commit to the run as `DAGSTER_CLOUD_GIT_SHA`, which a
   project can pass on as `DBT_GIT_SHA`, so it reaches `git_sha` instead.
3. `unknown`: neither records one.

A commit is a hexadecimal object name and nothing else. One spelled any other
way (a branch name, free text) is read as `unknown`, so a commit can carry no
value from the data and is never read by `git` as an option.

### The commits a break spans

Runs and commits can disagree. A commit can be deployed days after it was made, a
rollback runs an older commit after a newer one, and two branches can deploy in
turn. A commit range read off run order alone can then name commits that never
ran in it. So, given the repository's history, a break also carries `commits`:
the commits read from that history, never from the runs in between. Without a
history to read, `commits` is `null`.

The range ends at the first failing run's commit. It starts at every earlier
passing commit that is an ancestor of it, as `git bisect` would take them: the
range is the commits the failing commit has that none of those passing commits
has. A passing run whose commit descends from the failing one (a rollback, or a
branch deployed in turn) bounds nothing, so it is left out rather than read as
the start of the range, and `run_order` says `disagrees`. Deployed in the order
A, C, B, where B introduced the failure and C fixed it, the range is A to B, and
C is not in it; deployed in the order A, B, C it is the same range, with
`run_order: agrees`.

| `status` | What it means |
| --- | --- |
| `ordered` | the range is read from history: `after` (the passing commits that bound it), `commits` (newest first, at most 50) and `count` |
| `same_commit` | the failing commit itself passed in an earlier run: no commit is in the range, so the change is not in the code |
| `not_ordered` | the last passing run's commit and the failing one are on diverged branches (`reason: diverged`): which side changed is not known, so no range is given |
| `unknown` | with a `reason`: `commit_unknown` or `commit_not_in_history` (with the `side`, `after` or `through`, that lacks one), `no_prior_pass`, `no_good_ancestor` (every earlier pass descends from the failure), `history_shallow` (a shallow clone cut the history the answer needs), or `history_unreadable` |

Each end names its run and where its commit came from (`git_sha`,
`orchestrator` or `unknown`), and `last_good_run` is always the last passing run
in invocation order, whether or not it bounds the range. `unplaced_good_runs`
counts earlier passes whose commit is unknown or not in the history: each could
only narrow the range. A shallow clone holds only part of the history: a commit
older than its depth reads `commit_not_in_history`, and where the clone's cut could
hide an ancestry (a commit that is not an ancestor in the clone may be one in the
repository) or shorten the range, it reads `history_shallow` rather than
`not_ordered` or a short range.

The history is read through `exmergo_dex_core.maintain.commit_history`:
`GitHistory` asks `git` in a local checkout, read-only and one call at a time
with no shell, keeping its answers for the life of the reader, and `MappedHistory` holds a commit graph in memory for a host with
no checkout.

### A subject's history

A subject's **history** is its series of observed outcomes on the timeline,
collapsed into segments. Its latest observed pass is its **last verified** run: a
deselected test, a missed run or a run with uploads off leaves it where it was,
and `latest` says the state of the timeline's latest run for that test, so a test
the last build did not select reads "verified at run 1, unobserved since".

Each time the history goes from good to bad it holds a **break**: after the last
observed-good run, up to and including the first observed-bad run. An unobserved
interval between the two widens the range rather than splitting it, and the
break counts the unobserved runs, the missed runs and their reasons. Where no
observed pass precedes the failure, the break is after `unknown`. An incident's
`onset` is the break at its first failure.

## Sources

Each source has an adapter that turns what it recorded into observations and a
run history (a `SourceHistory`: its runs and the oldest point it still holds).
An adapter opens and closes no incident: `fold_incidents` does that, by the one
rule above, whichever source the observations came from.

### dbt run results

`exmergo_dex_core.maintain.run_results_incidents` reads the test results in the
`run_results.json` dbt writes, under the source name `dbt_run_results`. It needs
no warehouse connection and no Elementary package, so it works on every dbt
project. `read_run_results(project_dir, manifest)` finds the file and reads it;
`observe_run_results(document, manifest)` maps a document already parsed.

- **Which run.** The newest `run_results.json` a building dbt command left
  (`build`, `test`, `run`, `seed`, `snapshot`) that holds a test result, in
  the project's target path or dex's isolated build directory, each with the
  `cold/` directory beside it. A later `dbt run` or `dbt seed` holds no test
  result, so it observes nothing; it is passed over, with a note, rather than
  hiding the failures of the build before it. A `compile`, `parse` or `ls`
  writes the file too, where `success` means compiled, so it is never read.
  The runs in those directories are separate invocations, often against
  separate targets, and are not stitched into one history.
- **One invocation, so no known start.** The file holds one invocation, which
  is the source's whole history and its retention bound. Every interval before
  it is unobserved, and every incident read from it alone has `first_observed:
  unknown`. The run read is never reported as the start of a failure.
- **A run the project did not log is refused.** When the dbt logs beside the
  file do not name its invocation, the file came from somewhere else (copied
  artifacts, another checkout), and nothing is read from it. With no log to
  check, it is read, and a note says so.
- **Only current tests.** A result is observed only for a test (or unit test)
  the current manifest still has, enabled. A result for one since removed or
  disabled is counted and dropped. A test the run did not reach has no
  observation, so the timeline reads that run as one that did not check it.
- **Statuses.** `pass` is observed good. `fail`, `error` (`runtime error` on
  older dbt releases) and `warn` are observed bad. `skipped` is unobserved,
  never good. A status outside those is counted and gives no observation.
- **Subject and severity.** The subject is the test's claim id where its
  manifest node carries one in `config.meta.dex.claim`, else its `unique_id`;
  `manifest_claims` in `exmergo_dex_core.transform.claims` reads the claims from
  a manifest. The signal is always empty, since dbt writes one result per test.
  A warning carries severity `warn`; a failure or an error carries the token
  the test's `config.severity` sets, where it is one.
- **No commit.** `run_results.json` records none, so every run's commit is
  `unknown`.

Both take a keyword `sequence`, the run's place in invocation order (1 by
default). Folded beside another source that observed the same invocation
(Elementary's `dbt_invocations` carries dbt's `invocation_id`), the caller
passes the place that source gave it, since the fold refuses one run at two
places.

`read_run_results` returns a `status` of `read`, `missing` (`no_run_results`:
no building run on disk; `no_test_results`: none of them ran a test) or
`refused` (`unlogged_invocation`, or
`no_invocation_id` for a file that names no invocation, since a run id is never
invented), with the run it found, the counts of what it dropped, and notes
naming each. From a result row it reads `unique_id` and `status` and nothing
else: never `message`, `compiled_code`, `failures`, `adapter_response` or
`relation_name`.

### Elementary's tables

`exmergo_dex_core.maintain.elementary_incidents` reads `dbt_invocations` and
`elementary_test_results`, under the source name `elementary`, where the project
installs the Elementary package. It holds both tables to the release's recorded
layout before writing a statement, sends every statement through the query
firewall, the source allowlist and the cost handshake, and reads ids, status and
severity tokens, an anomaly test's column and metric, each run's times and
commit, and counts. It maps dbt's statuses as the run-results adapter does.
Unlike `run_results.json`, the tables hold many invocations, so an incident read
from them has a known start wherever an observed pass precedes its first
failure. `plan_read(..., last=n)` reads only the newest `n` invocation rows (by
`created_at`, ties by id), so a window can be a run count rather than a time.

### `dex maintain incidents`

The command (`DexEngine.incidents`) reads both sources and folds them together,
Elementary first: the fold's tie-break between two sources failing in the run
that opens an incident is the order they are named in, and Elementary's tables
hold the longer run history.

- **A bounded read.** Elementary's tables are read over the newest 20
  invocations, or `--invocations <n>` (at most 2000). A run count rather than a
  number of days, so the price does not depend on the schedule. Every statement
  is priced before any runs, with the profiles the firewall needs on a first
  read, as one handshake.
- **One order.** dbt's run is placed where Elementary placed the same
  invocation, or after the window when it started after the window's newest
  run (dbt's `invocation_started_at` against Elementary's `run_started_at`,
  two starts; `generated_at` is the run's end). It is then named under
  `window.after_window`, outside the count, which never exceeds the run count. A run that
  is neither has no place in the order and is left out of the fold, with
  `folded: false`. A run placed in Elementary's window sits in Elementary's
  order, so the run-results note that no failure has a known start is left
  out there. Where the run itself opened incidents with no known start (a
  test Elementary holds no result for), a note in its place counts those
  incidents alone, since others in the same fold can be dated. Every other
  placement keeps the note as written.
- **Every source answers.** `data.sources` reports each source `read`, `missing`
  or `refused` with a reason code: no package (`not_installed`, with the
  `dex init` that offers it), a package whose uploads are off (`not_uploading`),
  a schema outside the source allowlist (`outside_allowlist`, with the
  `dex config set` that adds it; nothing is read past the allowlist), a
  connection or catalog that could not be read (`connection_unavailable`,
  `catalog_unavailable`), a read that could not be priced (`unpriced`: it is
  never asked for at a lower price than it would spend), a statement the
  warehouse refused mid-read (`warehouse_refused`, its words masked, the price
  kept), or the reader's own
  refusals. Each source answers alone, so dbt's run results are still read when
  Elementary's tables are not. A confirmation, budget or ceiling refusal is the
  command's, not a source's. An empty `incidents` list means no failure only
  where a source reads `read`.
- **Read alone, dbt's run results never close an incident.** The next build
  overwrites the file, so a fixed failure drops out of it rather than reading as
  two passes.

### `dex maintain diagnose`

The command (`DexEngine.diagnose`, #1475) reads the incidents as
`maintain incidents` does, with the repository's history handed to the fold,
and takes each open incident to a cause, or to none.

- **Bisect.** The range is the incident's onset: after the last observed-good
  run, up to and including the first observed-bad one. A run that recorded
  nothing between them widens the range back to the last observed pass; it never
  splits it, so it is never read as good.
- **Bound.** The committed `maintain snapshot` series is the versions of
  `.dex/snapshot.json` in the repository's history. The newest snapshot captured
  at or before the last observed pass is `snapshots.before`; the oldest at or
  after the first failure is `snapshots.after`. A snapshot is placed by when
  its warehouse state was measured: `cache_updated_at` where it was pinned
  from the exploration cache, the earlier snapshot's time where a
  `--project-only` snapshot carried the block forward. On a project's first day there is
  no series (`series: none`), and the whole break is a window no recorded state
  of the warehouse covers (`unseen`, `no_snapshot_series`): a check then
  confirms a cause as present now, not when inside the break it entered. With a
  series, the unseen window is the stretch between the bounding snapshots.
- **Confirm with a check Dex already has.** Each candidate names the check that
  answered it and the finding code. This release checks the cause kind
  `dex demo` seeds: a uniqueness test failing because the loader delivered a key
  twice. It is held, on each source relation the tested model reads that carries
  the tested columns, by the grain axis of `maintain check`:
  `key_lost_uniqueness` against `snapshots.before` where its measurement proved
  the key unique (the volume axis's row counts ride beside it as evidence), and
  otherwise the declared-claim check, which needs no baseline: the failing
  test's own claim held to the source (`declared_grain_not_unique`), and only
  where one source the model reads carries the key: with several, today's data
  cannot tell a duplicated delivery from a source at another grain
  (`several_sources`, not checked). A source the bounding snapshot measured
  not unique while the test passed is ruled out
  (`not_unique_at_before_snapshot`); a relation nothing counted is
  `not_checked` (`uncounted`), never ruled out. The project
  is ruled out where the failing commit itself passed earlier (`onset.commits`
  reads `same_commit`). Seed, package and schedule causes are listed
  `not_checked` until their checks are read.
- **No cause is an answer.** `verdict` is `cause`, `several` (each named, none
  preferred) or `no_cause`. With no confirmed candidate the answer is
  `ruled_out` and `unseen`; the most likely candidate is never named as the
  cause.
- **The repair is `transform`'s.** A confirmed cause carries the
  `dex transform plan` step that would repair it under `next_steps`. Nothing
  here writes it.
- **Priced.** The incident read prices its own statements first; the checks
  are then priced together as one handshake before any probe runs, within
  what the per-command ceiling has left after the read. Checks that do not
  fit are offered (`scan_offered`, `price.checks.offered`), not asked for
  again, and `data.spend` sums both phases.

## No value crosses into an incident

An incident, a timeline and a history carry ids, statuses, counts, times and
commits. It never carries a sampled
row, a failing key, a result's message or a compiled query's output. The
observation every source adapter builds declares only those fields and refuses
any other, and a severity is a short token (`warn`, `ERROR`) rather than free
text, so a value cannot ride along in it.
