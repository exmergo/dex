# Running the lifecycle across more than one process

dex is designed for one process holding one project directory: you explore, you
plan, you apply, you build, and the whole thing shares a working tree and a
`.dex/` store. That is the right shape for an interactive user and for an agent
driving the command contract, and nothing here changes it.

Some applications split that lifecycle up. Planning runs where the model runs.
Application runs offline, in a disposable checkout with no network and no
credential. The build runs in a sandbox that holds only a dev warehouse
credential. Each boundary exists because something on the far side is not
trusted, so nothing may cross it except data the receiving side can verify for
itself.

That host has four questions the interactive path never has to answer:

1. What exactly does this plan change, and is the plan I am about to apply the
   plan that was authored?
2. What does this change depend on, and did resolution actually finish?
3. Does this change contain executable or authority-bearing content, as opposed
   to declaring something?
4. Did the build that just reported success actually validate this change?

This document is how to answer them without writing a second SQL parser, dbt
resolver, semantic classifier, or cost engine. A second implementation of any of
those will disagree with dex's, and the disagreements surface as wrong policy
decisions rather than as errors.

## One import

```python
from exmergo_dex_core.host import (
    BuildOutcome,
    DependencyPolicy,
    verify_plan_document,
    conformance_vectors,
)
```

`exmergo_dex_core.host` re-exports the contract. Every name in it is the same
object the engine uses internally, so a host and the engine cannot disagree about
what a plan digest is or about what makes a build validated. It is a door, not a
second implementation, and it deliberately contains no way to *run* anything: the
verbs stay on `DexEngine`, which owns the connection, the store, and the guards.

The offline half imports cleanly on a bare install with no connector extra, which
is what a disposable checkout with no warehouse client actually has.

## 1. A plan you can carry

`DexEngine.plan()` stores a plan under `.dex/plans/`. `export_plan()` turns it
into a document:

```python
with DexEngine.from_repo(repo) as engine:
    result = engine.plan("define a revenue metric", edits=edits)
    exported = engine.export_plan(result.plan_id)

document = exported.plan     # send this wherever
digest = exported.digest     # send this through a channel you trust
```

The document carries every edit's operation, kind, preimage hash, content hash,
content, project-relative path, and classification, plus a digest over the whole
plan. `transform export` is the same thing on the command line.

Applying it needs a repo root and nothing else: no plan store, no connector, no
cache, no dbt, no network.

```python
with DexEngine.from_repo(sandbox) as engine:
    applied = engine.apply_plan_document(document, expect_digest=digest)
```

```bash
dex transform apply --plan-file plan.json --expect-digest sha256:...
```

### What the digest is, and what it is not

The digest proves a document is internally consistent. Recompute every content
hash from the content carried, recompute the digest from those, and a byte
changed anywhere fails.

**It is not a signature, and it cannot be.** Anything that can rewrite the
content can rewrite the digest beside it. The host closes that gap by carrying
the digest across the boundary through a channel it trusts and passing it as
`expect_digest`. That is the one place authenticity can live, because only the
host knows which channel that is. A digest presented as tamper-proof is worse
than no digest, since it invites skipping the pinning that does the work.

Three refusals, and they are different:

| what happened | what you get |
|---|---|
| content changed, hashes untouched | `PlanDigestMismatchError` naming the path |
| content, hash, and digest all recomputed | verifies on its own terms; refused against `expect_digest` |
| the applying checkout edited the file first | a conflict, `needs_confirmation`, nothing written |

The third is propose-don't-impose reaching across a process boundary. A human
edit in the *applying* checkout is authoritative there exactly as it is in the
authoring one, and one conflict withholds the whole plan rather than the
conflicting edit.

### What is deliberately not in the digest

Not `created_at`, because a plan id is already content-addressed and two
identical changes authored an hour apart are the same change. Not
`engine_version`, because an engine upgrade must not invalidate a digest a host
pinned before it. Not the classification, because that is derived, and a later
engine that reads a hook it used to miss would otherwise change the digest of a
plan whose bytes never moved.

The same plan exported from two checkouts of one source state is byte-identical.

## 2. What the plan depends on

```python
grounding = engine.ground(plan_id).data()
```

```bash
dex transform ground <plan-id>
```

Repo-only and free on every connector: it reads the project's files and the
compiled artifacts and opens no connection.

The verdict leads the payload, because a long dependency list gets read from the
top and sometimes cut from the bottom, and the honesty must not be what is lost:

```json
{"completeness": "partial",
 "limits": ["packages are declared but not installed, so package contents were
             not scanned; run `transform deps` first"],
 "unresolved": [{"path": "models/marts/dyn.sql", "line": 2, "form": "ref_call",
                 "kind": "model", "reason": "dex could not resolve this
                 reference statically"}],
 "ambiguous": [], "freshness": {...}, "binding": {...},
 "relations": ["dev.main.stg_orders"], "dependencies": [...]}
```

**"No dependencies" and "resolution did not finish" are different answers.** An
empty `dependencies` with `completeness: complete` and `limits: []` means the
plan reads nothing. An empty one with `completeness: unresolved` means dex could
not see. A host that reads the first when it should have read the second admits a
change nobody checked, which is why `completeness` is computed from the limits
rather than asserted.

**Staleness sits beside completeness, not inside it.** A fully resolved graph
read out of a manifest older than the model sources is complete and stale at
once, and collapsing the two makes a caller choose which fact to lose.
`freshness` carries the two timestamps so the caller judges rather than
inheriting a threshold.

`binding` fingerprints the plan, the source tree, the effective configuration,
the package inputs, and the engine version. None of them is a security control on
its own; together they are what makes a later mismatch visible instead of silent.

A metric resolves through its measures to its semantic model and on to the
relation that model sits on, which is how "which table is behind this change"
gets answered without a second resolver.

## 3. What the edits contain

```python
classification = engine.classify(plan_id)
```

```bash
dex transform classify <plan-id>
dex transform classify --edits-file edits.json   # before anything is planned
```

The verdict is computed from content and operation only. The declared `kind` and
the filename are supplied by whoever authored the edit and describe where the
file goes; a host deciding whether a change may be applied offline is asking what
is in it. A caller passing a kind that contradicts its own content gets the
content's answer.

| class | what it means |
|---|---|
| `declarative` | statements of fact the build reads |
| `executable` | content that runs, or makes something else run |
| `authority_bearing` | content that confers rights rather than running |
| `data` | values, not instructions |
| `unknown` | dex could not read it |

Two rules matter more than the taxonomy. **Unparseable content is `unknown`,
never `declarative`**: a classifier that fell back to "declarative and safe" on
anything it could not read would be most confident exactly where it understood
least. And **a signal is reported even when it did not decide the class**, so a
document carrying both a hook and a grant classifies as executable with the grant
still listed, because a host applying its own policy needs the evidence rather
than the verdict alone.

The case worth having: two YAML files in one plan, both authored as
`kind: schema_yml`, one of them carrying a `post-hook`. Identical kind, different
verdicts, and the signal names the exact YAML path.

## 4. What the build established

```python
result = engine.build(target="dev", for_plan_document=document)
result.evidence["outcome"]
```

```bash
dex transform build --target dev --for-plan-file plan.json --confirm --budget N
```

`BuildResult.success` is unchanged and still means dbt's process outcome, which
is the right meaning for a command line and the wrong one for a host: a build
whose selection matched no nodes exits zero, and so does a build of a model the
change never touched. `evidence.outcome` is what tells them apart.

| outcome | what happened |
|---|---|
| `validated` | everything required ran and passed, against current artifacts |
| `empty_selection` | the selection matched nothing |
| `unrelated` | nodes ran, and none of them was required by this change |
| `partial` | some required nodes ran and some did not |
| `failed` | a node errored, or a test returned rows |
| `skipped` | every node was skipped, usually behind a failed parent |
| `stale` | it passed, and the tree it validated is not the one the plan describes |
| `not_run` | dbt never reached node execution |

`coverage` is how a semantic or schema change becomes checkable at all: a model
edit requires its own node, a `schema.yml` requires the nodes it documents, and a
semantic YAML requires the dbt model its semantic model sits on. It is absent
rather than empty when no plan was named, because an empty coverage block reads
as "nothing required was built", which is the opposite of "nobody asked".

`selection.complete` is `null` for a selector using dbt's graph operators or
method selectors. dex does not evaluate dbt's selector language, and a second
implementation of it would disagree with dbt's.

`stale` deserves a note. The obvious reading is "the compiled artifacts predate
the sources", and that is reported as `stale_artifacts`, but it is close to
unreachable from a build that just ran, because a successful dbt build rewrites
its own manifest. The reachable one is `plan_drift`: a file the plan wrote no
longer holds what the plan wrote, so the build validated a tree the plan does not
describe. It fires on builds that otherwise look perfect.

## 5. What the sandbox may install, and what the provider will enforce

```python
engine.build(target="dev", dependencies=DependencyPolicy.REFUSE)
```

```bash
dex transform build --target dev --no-install-deps
```

The default installs missing packages post-gate, exactly as every build did
before this existed, so nothing an interactive user sees changes. `REFUSE` names
the declared-but-uninstalled packages and stops, before any subprocess and before
any pricing, which keeps the refusal free and stops a sandbox with no network
paying for a dry run of a build that cannot happen.

```
this build may not install dependencies, and these declared dbt packages are not
installed: dbt-labs/dbt_utils@1.3.0, calogica/dbt_expectations@0.10.4. Run
`transform deps` where the network is available, or bake dbt_packages/ into the
environment before the build
```

A declaration dex cannot check by name (a git URL names a repository, and dbt
installs under the package's own name) is reported separately rather than named
on a guess. `REFUSE` checks every declaration even when `dbt_packages/` is
non-empty, and stops on missing or unverifiable packages. This checks presence;
it does not verify lockfile versions or installed package contents.

```bash
dex transform preflight
```

reports what the *warehouse* will enforce on the statements the build runs, read
from the project's rendered `profiles.yml` rather than from what dex would have
written:

```json
{"connector": "bigquery", "binding": true, "estimate_quality": "exact",
 "controls": [{"name": "maximum_bytes_billed", "binds": "statement",
   "source": "profile", "unit": "bytes", "value": 500000000,
   "detail": "written into profiles.yml when the project was initialized, from
              the ceiling configured then; a later --budget does not move it"}],
 "unsupported": ["no server-side wall-clock cap on a build statement: ..."]}
```

`binding` is the headline, and it is `false` more often than expected. A project
initialized before a ceiling was committed has a dev target that looks entirely
healthy and no provider-side cap at all, and this is the command that says so.
**A target named `dev` is not by itself evidence of anything.**

On DuckDB `binding` is always `false`, and the report says why: there is no
provider. The read-only open is a real guarantee and it bounds what dex reads,
not what a dbt build writes to the dev target.

Free and connectionless on every connector.

### Named statement refusals

`NotSelectOnlyError` carries a `reason`. sqlglot funnels `CALL`, `EXEC`, and
`EXECUTE IMMEDIATE` into one catch-all node, so a message quoting the node class
tells a host nothing it can branch on:

`multi_statement`, `write`, `ddl`, `stored_procedure_or_call`, `dynamic_sql`,
`unapproved_function`, `not_a_query`.

`guards.approved_functions` in `.dex/config.yml` turns on an allowlist over the
functions a compiled model calls. It is empty by default and the emptiness is the
design: on by default would refuse working builds against every warehouse with
local functions the engine has never heard of. `explore query` never consults it.

With a non-empty allowlist, builds compile the requested selection and check
every selected SQL node before running `dbt build`, including on DuckDB. Billed
builds also check before handing compiled statements to the provider's estimator.
An unapproved function is a named refusal; compilation failure or missing
compiled SQL stops the guarded build instead of falling back to unchecked work.
Compilation results are cleared before the build so they cannot become build
evidence. An empty allowlist preserves the existing execution path.

This check covers the compiled SQL, not Jinja execution, hooks, or materialization
macros. dbt compilation and the subsequent build still execute repository code
and must run in the host's intended sandbox. The check does not freeze the
repository or guarantee that nondeterministic templates render identically twice.

## 6. Cost, on both outcomes

`cost` carries `estimate_quality` and `unit` alongside the magnitude:

| value | meaning |
|---|---|
| `exact` | BigQuery: a dry run is what the job will bill |
| `approximate` | a model of the run (Snowflake, Databricks, Redshift, Postgres, ClickHouse) |
| `unknown` | pricing was attempted and produced no number |
| absent | nothing was priced |

The last two are different states and a host that collapses them admits an
unpriced build believing it was priced.

`spend` carries `settled` and `unknown_settlement` on success and on failure. dbt
runs a build's statements, and some adapters report no billing figure at all, so
a build that ran and billed something it cannot name reports
`unknown_settlement: true` rather than zero. Nothing is appended to the ledger
then, because there is no figure to append.

## Conformance vectors

```python
from exmergo_dex_core.host import conformance_vectors

for vector in conformance_vectors():
    vector["contract"]     # portable-plan | classification | grounding | build-evidence
    vector["payload"]      # what a host will be handed
    vector["expect"]       # the verdict it should reach
```

Twenty-one fixtures ship in the wheel beside the reader: a valid plan and the
same plan exported from a second checkout, tampered content, a repinned digest, a
future schema version, a delete; a declarative, executable, authority-bearing, and
unknown classification; a complete, unresolved, and ambiguous grounding; and one
per build outcome.

They are data rather than assertions, deliberately. The shape a host has to agree
with is the payload, and a shipped test would only tell it whether dex agrees with
dex. Run your own reader over them and check the verdict. The engine's own suite
replays every one, so a vector that stops describing what the engine does fails
before it reaches anybody.

This complements, rather than replaces, the executable contract classes shipped
for the storage, project, and semantic seams. Those are for someone implementing a
protocol; these are for someone consuming a payload.

## What stays where it is

Every existing public method keeps its signature and its result shape, and
nothing here changes what an interactive user or an agent driving the command
contract sees. Every new behavior is opt-in: no `for_plan`, no coverage; the
default dependency policy still installs; the function allowlist is off until a
project writes one.

The engine does not implement admission policy. It reports what a plan contains,
what it depends on, what the provider will enforce, and what a build established.
Deciding what to do with that is the host's job, and run identity, freshness
windows, and who is allowed to approve what are the host's to supply.
