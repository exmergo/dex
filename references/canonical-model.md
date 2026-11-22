# The source of truth (the repository) and the `.dex/` cache

dex maintains no canonical model of its own. **The repository is the source of
truth**, on two axes: the transformation project, which is dbt, and the semantic
layer, which is dbt's own or a native format that owns its definitions in files
beside it. dex reads whichever are present, reasons over them together with
warehouse introspection, and writes changes back as reviewable diffs. The only
state dex keeps of its own lives under `.dex/`: derived observations alongside
configuration, a chosen baseline, and operational history.

## Why dbt, not a dex-native model

An earlier design made the source of truth a dex-invented semantic model, with
every other format a projection of it. That was an over-correction. The reasoning
was "the interchange formats are immature, so build our own," but the right
response to an immature standard is to anchor on the mature thing, not to invent
a third one: a representation dex invents has no spec, no tooling, and no users,
so it is the least mature option, not the most stable. And the premise was
largely wrong on the facts: **dbt's MetricFlow already expresses nearly all of
the richness** in question (entities; categorical and time dimensions with
granularity; measures; and metrics of kind simple, ratio, derived, and
cumulative, with time spines). The dex-native model was, in effect, a plan to
re-implement MetricFlow, worse. dbt is mature, versioned, adopted, and where AEs
already live, so it is the correct anchor for the transformation model.

The decisive practical win: with the repository as the source of truth there is
no parallel copy to reconcile, so the round-trip fidelity problem, the
per-element provenance and identity machinery, and "the human's file wins"
conflict resolution all disappear. Human edits are authoritative by construction.

That reasoning is about not inventing a model, and it does not argue against
reading a format somebody else specifies. A semantic layer that owns its own
definitions is read where it lives, on its own axis, with no translation into
dbt's vocabulary and no dbt project required. Those definitions are source of
truth in exactly the same sense the dbt project is: they are files in git, dex
holds no competing copy of them, and every change dex makes to them is a diff.
See [`semantic-layer.md`](semantic-layer.md).

## How dex reads and writes the dbt project

- **Read:** load the project primarily from the compiled `manifest.json` (dbt's
  own documented, versioned serialization of nodes, sources, tests, semantic
  models, metrics, and lineage), supplemented by the raw SQL and YAML for editing.
  This lives in `dbt_project.py`.
- **Reason:** over that view plus warehouse introspection and the `.dex/` cache.
- **Write:** edits go back into the dbt source files as reviewable diffs. dex
  never holds a competing copy.

## The `.dex/` cache (not canonical)

The exploration cache informs proposals and never owns model definitions.
Other files under `.dex/` hold decisions and history that cannot be recovered
by re-reading the current warehouse:

```
.dex/
  config.yml      non-secret config: connector + dbt target, budgets, ranking hints
  cache.json      exploration artifacts (DexCache): profiles, PII flags, relationships,
                  ranked candidate keys with the evidence behind each (key_evidence,
                  including the combinations suppressed as artifacts and why), grain,
                  rankings, data-quality observations. A column profile records whether
                  its distinct count is exact or approximate (distinct_count_exact); each
                  dataset carries the time it was profiled (profiled_at) so
                  carried-forward profiles stay attributable, and the file carries a
                  schema version, so a profile written before the artifact exclusions
                  existed is re-profiled rather than read as if they had applied.
                  provenance.tool_version is the engine release that last wrote it
                  (null where unknown), so a cache two releases share says which
                  one it came from
  snapshot.json   the maintain baseline: a frozen fingerprint of the warehouse schema, the
                  transformation project's state, the semantic layer's definitions with the
                  relationships and keys they declare, and declared grain assumptions. The
                  two project layers are fingerprinted independently, so a repository with a
                  semantic layer and no transformation project still gets a baseline. Written
                  by `maintain snapshot`; the drift detectors diff current reality against it,
                  and warn when the tool_version it records is another engine release.
  queries.jsonl   the `explore query` audit log: one line per firewall decision (allowed,
                  refused, or failed) with the SQL text and result counts, never result
                  values. A decision that profiled an object on demand first names it
                  under profiled_on_demand (profile_planned where the decision was
                  needs_confirmation), so a scan run on the caller's behalf is as
                  findable as a spend. A call carrying several statements writes one
                  line each, sharing a timestamp and carrying batch_index and
                  batch_size, so six statements read as one authorization event
                  rather than six. Doubles as product signal: probe shapes that
                  recur here are candidates for promotion to named commands.
```

Do not delete `.dex/` to resolve a cache conflict: that also discards configuration,
the baseline and spend history. Dex can re-derive exploration observations from
the repository and the warehouse, but cannot reconstruct those decisions or past
events. The cache types live in `cache.py`; secrets never live here. PII is
recorded as `(column, category, confidence)` with no example values.

### Merging branches that both refreshed Dex

For the default filesystem backend, use this classification when resolving a
merge. A regenerable output is only current after its producer succeeds against
the merged inputs; selecting either branch's copy alone does not refresh it.

| Path under `.dex/` | Role and resolution |
| --- | --- |
| `config.yml` | Authored configuration. Merge targets, limits, ranking hints and PII overrides intentionally; no refresh command reconstructs them. |
| `cache.json` | Derived exploration observations. Regenerate with `dex explore map --refresh`; use `--full` when the intended refresh must cover objects below the rank cutoff. Keep the scheduled job's connector, scope, project and inclusion flags. A narrow run carries older observations outside its scope, so it cannot refresh the whole cache. |
| `drift.json` | Latest derived drift report. Regenerate with `dex maintain check` after rebuilding and mapping, against the agreed snapshot. |
| `review/refresh/*.json` | Command envelopes saved by a project's refresh job, not a separate engine store. Re-run that job and capture each command's output at its original path, for example `transform build --verify`, `explore map` and `maintain check`. |
| `snapshot.json` | The team's chosen comparison baseline. Preserve the agreed baseline; create a new one with `dex maintain snapshot` only when deliberately accepting a new reference. Rebaselining to resolve a conflict hides the changes `maintain check` should report. |
| `plans/*.json` | Proposed edits and review history. Preserve needed plans; deliberately run `dex transform plan` again when the merged source invalidates a proposal. A new plan does not recreate its old review history. |
| `mutations/*.json` | Latest mutation-test evidence per model. Re-run the same `dex transform test --mutate` invocation against the merged project if this evidence is required. |
| `queries.jsonl`, `spend.jsonl` | Append-only decisions and billing history. Preserve recorded events; neither mapping nor drift detection recreates them. Discarding spend history also loses the ledger used by budget checks. |
| `cache.lock`, `spend.lock` | Local runtime lock files. Keep them out of Git; do not remove or replace a lock while a process may hold it. |
| `dex.db`, `dex.db-journal` | SQLite backend state, which includes history and baselines as well as the cache. Keep it local; do not treat the database as a disposable JSON output or resolve it with a text merge. See [storage](storage.md). |

A safe refresh merge follows this order:

1. Wait for local refresh writers to finish. Merge the dbt and semantic source
   files, ingestion inputs and `.dex/config.yml`, and preserve the agreed
   snapshot and operational history.
2. Resolve only the conflicting derived outputs (`cache.json`, `drift.json`,
   and the job's `review/refresh/*.json`) by taking one complete side as a
   temporary starting point. Do not hand-merge profile fields or apply this
   choice to the whole `.dex/` directory.
3. Run the project's scheduled refresh pipeline over the merged inputs: ingest,
   build on its configured **dev** target, map, then check. Use the same flags,
   budget and confirmation rules as that job. Force fresh profiles with
   `explore map --refresh`, and cover every namespace and object the committed
   cache is meant to describe (`--full` bypasses the rank cutoff, not scope or
   source allowlists). Regenerate the captured review envelopes too.
4. Inspect the command statuses, refused or skipped work, cache scope and drift
   findings. A failed build or partial map is not a complete refresh. Review and
   stage the regenerated outputs only after they describe the merged warehouse;
   do not take a new snapshot merely to make the findings disappear.

For projects with scheduled refreshes, prefer keeping `cache.json`, `drift.json`
and refresh envelopes as CI artifacts or local outputs rather than committing
them on every run. If the team needs versioned observations, commit them only
after that regeneration step. The [README's commit guidance](../README.md#what-to-commit-in-dex)
still applies to values in caches and snapshots. Avoid a blanket `.gitattributes`
`merge=ours` rule: it can silently accept stale observations and discard
configuration or history, and it does not run the refresh pipeline.

The layout above is the CLI's choice, not the engine's. Where this state lands is
a backend behind the `Store` protocol, injected at the entry point: the CLI picks
the filesystem so subcommands stay stateless across processes and the session
budget carries from one command to the next, while an in-process library caller
gets an in-memory store by default and writes nothing at all. A host can
implement the protocol over its own session store or database. The repository is
unaffected by any of this: it is the source of truth and stays a git-reviewable
filesystem artifact, which is why `repo_root` remains separate from the store.

Credentials are in neither place. dex discovers them at connection time and never
stores, caches, or refreshes one, in any backend. A host serving more than one end
user can supply them itself, the warehouse connection and a hosted semantic layer
token alike, so each request reaches its target as its own principal instead of as
the process; that moves who the caller is, and nothing else. The cost gate is still
built by the engine from the store, the source allowlist still only narrows, and no
credential becomes persistent state.

## The extension seam (more formats later)

Supporting more model formats over time does not require a neutral internal model.
It requires a seam thin enough that a class with the right methods is a project,
with no base class to inherit and no registration step. That seam is three nested
protocols in `adapters/project.py`, and a format implements the tiers it can serve:

| tier | protocol | adds |
|---|---|---|
| 1 | `ExploreProject` | `definitions()` |
| 2 | `MaintainProject` | `transform_layer()`, `semantic_layer()` |
| 3 | `EditableProject` | `write_edits()` |

`DbtProject` is the one implementation dex ships. Future sources (SQLMesh, Cube, an
orchestrated asset graph) become new implementations of the same protocols without
touching the engine that reasons over a project. A semantic layer that builds
nothing is not one of them: it owns no model graph, no compilation, and no
targets, so it sits on a separate seam described in
[`semantic-layer.md`](semantic-layer.md) and satisfies none of the tiers above. Nothing behind the seam is a rich
neutral model: `definitions()` returns the engine's existing `ProjectDefinitions`
rather than a parallel vocabulary that would have to be mapped at every call site,
and dex does not project the dbt model back out into other formats. dex reasons
over the project and authors into it directly; there is no one-way exporter path.

Tiers rather than a capability flag, because a flag is a claim the engine has to
trust while `isinstance(project, EditableProject)` is checkable. A format that
cannot receive an edit declines tier 3 and cannot accidentally claim otherwise.

[`project.md`](project.md) is the contract itself: what each tier owes its callers,
the one rule that is not visible in the signatures, how a format is named in
configuration, and the conformance suite that proves an implementation works.
Read that before writing a format; this section only says where the seam sits.

> An earlier version of this section named a single `ProjectAdapter` protocol with
> `load()` and `write_edits()`. That protocol still exists, unreferenced, because it
> has been public since v1, but it is superseded and satisfies no tier. Anything
> written against it would be refused as "not a project".

## The round-trip rule (reconcile, simplified)

1. Re-read the dbt project and warehouse on every transform or maintain run.
2. Human edits are already authoritative (the repository is canonical); there is
   no internal copy to reconcile against, only the `.dex/` snapshot used to
   detect change.
3. Diff the current dbt project and warehouse against the snapshot; propose edits.
4. On any ambiguous or overwriting change, surface a diff and ask. Never silently
   overwrite.
