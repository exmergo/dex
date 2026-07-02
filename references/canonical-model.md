# The source of truth (dbt) and the `.dex/` cache

dex maintains no canonical model of its own. **The dbt project is the source of
truth.** dex reads it, reasons over it together with warehouse introspection, and
writes changes back into it as reviewable diffs. The only state dex keeps of its
own is a non-canonical cache under `.dex/`.

## Why dbt, not a dex-native model

An earlier design made the source of truth a dex-invented semantic model, with dbt
and OSI as projections. That was an over-correction. The reasoning was "OSI is
immature, so build our own," but the right response to an immature standard is to
anchor on the mature thing, not to invent a third one: a representation dex
invents has no spec, no tooling, and no users, so it is the least mature option,
not the most stable. And the premise was largely wrong on the facts: **dbt's
MetricFlow already expresses nearly all of the richness** in question (entities;
categorical and time dimensions with granularity; measures; and metrics of kind
simple, ratio, derived, and cumulative, with time spines). The dex-native model
was, in effect, a plan to re-implement MetricFlow, worse. dbt is mature, versioned,
adopted, and where AEs already live, so it is the correct anchor.

The decisive practical win: with dbt as the source of truth there is no parallel
copy to reconcile, so the round-trip fidelity problem, the per-element provenance
and identity machinery, and "human dbt wins" conflict resolution all disappear.
Human edits are authoritative by construction.

## How dex reads and writes the dbt project

- **Read:** load the project primarily from the compiled `manifest.json` (dbt's
  own documented, versioned serialization of nodes, sources, tests, semantic
  models, metrics, and lineage), supplemented by the raw SQL and YAML for editing.
  This lives in `dbt_project.py`.
- **Reason:** over that view plus warehouse introspection and the `.dex/` cache.
- **Write:** edits go back into the dbt source files as reviewable diffs. dex
  never holds a competing copy.

## The `.dex/` cache (not canonical)

`.dex/` holds only what the dbt project has no home for, and it is a cache that
informs proposals, never the source of truth:

```
.dex/
  config.yml      non-secret config: connector + dbt target, budgets, ranking hints
  cache.json      exploration artifacts (DexCache): profiles, PII flags, relationships,
                  candidate keys, grain, rankings, data-quality observations
  snapshot.json   the maintain baseline: a frozen fingerprint of the warehouse schema, the
                  dbt manifest state, and declared grain/semantic assumptions. Written by
                  `maintain snapshot`; the drift detectors diff current reality against it.
  queries.jsonl   the `explore query` audit log: one line per firewall decision (allowed,
                  refused, or failed) with the SQL text and result counts, never result
                  values. Doubles as product signal: probe shapes that recur here are
                  candidates for promotion to named commands.
```

Delete `.dex/` and nothing canonical is lost: dex re-derives the cache from the dbt
project and the warehouse. The cache types live in `cache.py`; secrets never live
here. PII is recorded as `(column, category, confidence)` with no example values.

## The extension seam (more formats later)

Supporting more model formats over time does not require a neutral internal model.
It requires a thin `ProjectAdapter` protocol (`adapters/project.py`) with one
implementation today, `DbtProject`, plus exporters that read a project view.
Future sources (SQLMesh, Cube) become new adapter implementations; future targets
(OSI and others) become exporters. The interface is thin and dbt is its only
implementation in v1; dex does not build a rich neutral model behind it.

## OSI and other outputs

Not emitted in v1. OSI is a dormant exporter (`exporters/osi.py`): the pinned-schema
validator is live and tested so the mechanism is ready, but the engine produces no
OSI until the format matures. See `osi-map-schema.md`.

## The round-trip rule (reconcile, simplified)

1. Re-read the dbt project and warehouse on every transform or maintain run.
2. Human edits are already authoritative (dbt is canonical); there is no internal
   copy to reconcile against, only the `.dex/` snapshot used to detect change.
3. Diff the current dbt project and warehouse against the snapshot; propose edits.
4. On any ambiguous or overwriting change, surface a diff and ask. Never silently
   overwrite.
