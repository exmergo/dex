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
| `N` consecutive observed passes from the opening source | Closes it. `N` is set per subject and signal and defaults to 2, so a test that flaps stays one incident. |
| An unobserved interval | Neither opens nor closes an incident, and does not break a run of passes: `N` counts observed passes. |
| The check stops running | Ends it as `no_longer_observed`, never as `closed`. |
| An observation from another source that disagrees | Attached as evidence. It never counts toward `N` and never closes the incident. |

Observations are ordered by invocation, not by clock, because sources stamp times
with different clocks. Within one invocation a failure is read before a pass, so a
source that passes in the same run another fails is evidence, not the opener.

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
| `coverage` | the opening source's runs from the first failure to the close (or its latest run), as segments each `observed_good`, `observed_bad` or `unobserved` |
| `retention_bound` | per source, the oldest point it still holds, as read at call time, or `unknown` |
| `state` | `open`, `closed` or `no_longer_observed` |
| `closed_by` | on a closed incident, the pass that closed it |
| `ended_reason` | on a `no_longer_observed` incident, `removed` or `stale` |
| `schedule` | on an incident open or ended at call time, the schedule staleness was judged against: `basis` (`derived` or `unknown`), `series` (`subject` or `source`), `median_gap_seconds`, `points`, and `allowance_seconds`, how long without an observation reads as stale |

## No value crosses into an incident

An incident carries ids, statuses, counts and times. It never carries a sampled
row, a failing key, a result's message or a compiled query's output. The
observation every source adapter builds declares only those fields and refuses
any other, and a severity is a short token (`warn`, `ERROR`) rather than free
text, so a value cannot ride along in it.
