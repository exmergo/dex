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

document = exported.plan  # send this wherever
digest = exported.digest  # send this through a channel you trust
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
 "ambiguous": [],
 "missing": [{"kind": "model", "name": "stg_order", "used_in":
              ["models/marts/orders.sql"]}],
 "freshness": {...}, "binding": {...},
 "relations": ["dev.main.stg_orders"], "dependencies": [...]}
```

**"No dependencies" and "resolution did not finish" are different answers.** An
empty `dependencies` with `completeness: complete` and `limits: []` means the
plan reads nothing. An empty one with `completeness: unresolved` means dex could
not see. A host that reads the first when it should have read the second admits a
change nobody checked, which is why `completeness` is computed from the limits
rather than asserted.

**Three findings, and only one of them is about dex.** `unresolved` is a
reference dex read and could not name, such as `ref(var('x'))`. `ambiguous` is a
name more than one thing defines. `missing` is a name nothing defines once the
plan lands: `ref('stg_order')` for a model called `stg_orders`, a source table
nobody declared, a metric reading a measure that does not exist. `unresolved` and
`limits` lower `completeness`, because they mean dex did not finish. `ambiguous`
and `missing` do not, because dex finished and found a problem with the plan. So
a host admitting a change requires `completeness: complete` *and* `missing: []`.

`missing` is never a guess. Where part of the project went unread (a declared
package that is not installed, a package set the plan itself changes, a YAML file
that does not parse), a name with no definition is left out of `missing` and a
limit names it and says why, which makes the answer `partial`. A two-argument
`ref('core', 'x')` is never reported missing either, because it can name another
dbt project that dex does not read.

Each dependency says where its definition lives once the plan lands, in
`defined`: `plan` when a file the plan writes defines it (a rewrite counts),
`project` for a file the plan leaves alone, `package` when only an installed
package does (`package` then names it), or `missing`. That is independent of
`relation`, which is where dex could see the name built. Without a compiled
manifest every `relation` is absent, and `defined` still answers. It is absent
where dex makes no claim: columns, macros and vars the project does not declare
(dbt's own macros and `--vars` are invisible to a static read), and names only the
compiled semantic catalog knows.

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
gets answered without a second resolver. A semantic model the plan adds needs no
compiled catalog: its own entities, dimensions and measures are `defined: plan`,
and what they sit on is a dependency of the plan like any other.

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

Some files decide which code a later build runs, or as whom, without a hook in
sight, and they are classified by what they hold rather than by their name:

| content | class | signal |
|---|---|---|
| a `packages:` entry (`packages.yml`, `dependencies.yml`) | `executable` | `dependency_hub` (detail `package@version`), `dependency_git`, `dependency_local`, `dependency_tarball`, `dependency_private` |
| top-level `dispatch:` or `flags:` (`dbt_project.yml`) | `authority_bearing` | `dispatch`, `flags` |
| a profile with `outputs:` (`profiles.yml`) | `authority_bearing` | `profile`, naming the profile |
| a guard, budget, limit, clearance, connector or semantic key (`.dex/config.yml`) | `authority_bearing` | `dex_config`, naming the key |
| `env_var('NAME')` anywhere in jinja | `authority_bearing` | `env_var`, naming the variable |
| `env_var(...)` with a computed name | `unknown` | `env_var_computed` |

`dbt deps` fetches whatever a dependency names and the next build runs its
macros, so a hub package pinned to an exact version is still `executable`; the
signal's detail is there so a host can choose to admit one. An `env_var()` read
puts a value from the dbt child's environment, which is the agent's, into
whatever the file renders into: `target/manifest.json`, docs, and with
`persist_docs` the warehouse's own comments.

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

### Which door a warehouse refusal came through

A statement the warehouse itself refused reads `reason: execution_failure` on
every connector. BigQuery can refuse one at two doors, and they mean different
things for whatever the host does next, so the error envelope names the door in
`data.stage`:

| `data.stage` | what happened | what it means for a retry |
|---|---|---|
| `plan` | BigQuery would not plan it: the dry run, or the job insert, answered 400 | nothing ran and nothing was billed; the same statement is refused again until it changes |
| `run` | the job was created, ran, and failed at `job.result()` | it may have billed, and the failure can depend on the data or on resources rather than on the SQL |
| absent | the refusal was not the warehouse's, or came from a connector that cannot tell its doors apart | `reason` is the whole answer |

Branch on the field, not on the text. Before it existed the only difference
between the two was the request line google-api-core puts in front of a
job-insert error (`400 POST https://bigquery.googleapis.com/...`), which depends
on how a client library renders an HTTP error and on dex's trim of the server's
words, and neither is a contract. In process, the same value is
`WarehouseQueryError.stage`, a `StatementStage` importable from
`exmergo_dex_core`. A multi-statement `explore query` carries it per statement,
beside each entry's `reason`.

### Classifying a refusal caught in process

A host that calls `DexEngine` holds the exception rather than an envelope, and
needs the `reason` the CLI would have printed for it. `reason_for` is the
function every CLI error envelope is built through, so the two cannot disagree:

```python
from exmergo_dex_core import ConfirmationRequiredError, DexError
from exmergo_dex_core.host import Reason, reason_for

try:
    result = engine.build()
except ConfirmationRequiredError:
    ...  # not a refusal: the CLI answers `needs_confirmation`, with no `reason`
except DexError as exc:
    if reason_for(exc) is Reason.PREREQUISITE:
        ...  # run the named setup command, then retry
```

Catch `ConfirmationRequiredError` first. It is a `CostGuardError`, so
`reason_for` reads it as `guard`, but it asks for a confirmation rather than
refusing, and the CLI's envelope for it carries no `reason`.

`Reason` is the envelope's bucket. `RefusalReason`, above, is the narrower answer
`NotSelectOnlyError` carries about one statement, and a warehouse refusal's door
is `WarehouseQueryError.stage`. The full table loads wherever the `sql` extra is
installed, which every warehouse connector extra brings. Without it, a class
listed only in the table's second tier reads its nearest listed ancestor, so
`CostGuardError` reads `request` rather than `guard`.

## 6. Cost, on both outcomes

`cost` carries `estimate_quality` and `unit` alongside the magnitude:

| value | meaning |
|---|---|
| `exact` | BigQuery: a dry run is what the job will bill |
| `approximate` | a model of the run (Snowflake, Databricks, Redshift, Postgres, ClickHouse, Trino) |
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
    vector["contract"]  # portable-plan | classification | grounding | build-evidence
    vector["payload"]  # what a host will be handed
    vector["expect"]  # the verdict it should reach
```

Twenty-three fixtures ship in the wheel beside the reader: a valid plan and the
same plan exported from a second checkout, tampered content, a repinned digest, a
future schema version, a delete; a declarative, executable, authority-bearing,
unknown, and filtered-metric classification; a complete, unresolved, ambiguous,
and missing grounding; and one per build outcome. A grounding vector carries the
project it was grounded against in `payload.project`, as relative paths and their
content.

They are data rather than assertions, deliberately. The shape a host has to agree
with is the payload, and a shipped test would only tell it whether dex agrees with
dex. Run your own reader over them and check the verdict. The engine's own suite
replays every plan, classification and grounding vector, so one that stops
describing what the engine does fails before it reaches anybody. A build vector
records a dbt run that cannot be repeated offline, so the suite checks that it
says what it claims rather than recomputing it.

This complements, rather than replaces, the executable contract classes shipped
for the storage, project, and semantic seams. Those are for someone implementing a
protocol; these are for someone consuming a payload.

## Requiring values-free results

A host whose model may see no value read from a table runs every call in
values-free mode, and the place to require it is where the host builds the
engine, never the repository: `DexEngine(result_mode="values_free")` pins the
engine, so a call asking for `"full"` is refused and every method not reviewed for
the mode refuses before it opens anything. A host that drives the CLI instead sets
`DEX_RESULT_MODE=values_free` in the environment of every process it launches; no
flag turns it off. Assert on the result rather than trusting the setting:
`result.result_mode` on the library, `data.result_mode` on every envelope. In the
mode an error carries `data.code` (`.code` on a `ValuesFreeError`,
`.values_free_code` on any other refusal) in place of a warehouse's words, so a
host branches on the code.

The guarantee ships as an executable contract,
`exmergo_dex_core.values_free_conformance.ValuesFreeContract`, installed with the
`[values-free-conformance]` extra. Subclass it in your suite, override
`make_engine` if you build engines differently, and run it against the release you
pin. What the mode keeps, withholds and refuses, and the residual risks it does
not close, are in [`values-free-mode.md`](values-free-mode.md).

## Pinning the daily spend ceiling

A host that confirms spend on an agent's behalf, or lets an agent confirm inside
committed limits, pins the daily ceiling where it builds the engine:
`DexEngine(session_ceiling_cap=<magnitude>)`, or `DEX_SESSION_CEILING_CAP` in the
environment of every CLI process it launches. The ceiling in force is the lower
of the cap and the repository's `budget.session_ceiling`, so nothing the agent
writes to `.dex/config.yml`, no answer flag, and no constructor argument raises
spend above it. Assert on the result rather than trusting the setting:
`result.session_ceiling_cap` on the library, `data.session_ceiling_cap` on every
envelope, each carrying the value, the unit and the source. It holds where the
agent cannot reach the constructor or the environment; against a coding agent
with an unrestricted shell it needs the agent host's permissions as well, as
[`cost-controls.md`](cost-controls.md#what-it-protects-and-what-it-does-not)
sets out.

## What stays where it is

Every existing public method keeps its signature and its result shape, and
nothing here changes what an interactive user or an agent driving the command
contract sees, except that a warehouse refusal's error envelope gains
`data.stage`, which is additive. Every other new behavior is opt-in: no
`for_plan`, no coverage; the default dependency policy still installs; the
function allowlist is off until a project writes one.

The engine does not implement admission policy. It reports what a plan contains,
what it depends on, what the provider will enforce, and what a build established.
Deciding what to do with that is the host's job, and run identity, freshness
windows, and who is allowed to approve what are the host's to supply.
