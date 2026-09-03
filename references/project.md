# Projects: where dex reads the source of truth

A **project** is what declares intent: which models exist, what grain each is
unique at, which columns join to which, what the metrics mean. dex reads it and
reasons over it; the warehouse is what dex measures against it.

Today one format ships. `DbtProject` reads a dbt project: model SQL, `schema.yml`,
the semantic manifest, and the compiled `manifest.json` when there is one. [`dbt-project.md`](dbt-project.md) covers what dex reads out of it and what it
writes back.

This document is about the seam underneath that: what a *second* format has to
satisfy, and what dex promises to do with it.

## Why you would write your own

One reason, and it is a real one: your models are not a dbt project.

A host that builds its transformation graph in something else (an orchestrator's
asset graph, SQLMesh, a semantic layer that owns its own definitions) still has
everything dex needs to be useful. It knows which tables it builds, at what grain,
and how they relate. What it does not have is a `dbt_project.yml`, and generating a
fake one to satisfy dex means maintaining a translation nobody reads and dex cannot
check.

The seam exists so that translation can be code you own instead.

## The three tiers

The contract is three nested protocols in `adapters/project.py`. Implement the one
that matches what your format can actually answer, and stop there: a narrower
implementation is complete rather than partial.

| tier | protocol | adds | what it buys |
|---|---|---|---|
| 1 | `ExploreProject` | `definitions()` | declared keys, joins and metric models reach `explore` |
| 2 | `MaintainProject` | `transform_layer()`, `semantic_layer()` | the format can be a drift baseline |
| 3 | `EditableProject` | `write_edits(edits, project_dir, *, confirmed=False)` | `transform` and `reconcile` may write back |

`tier_of(project)` reports the highest tier an object satisfies, checked with
`isinstance` rather than declared, so a format cannot claim a tier it has not
implemented.

Two protocols sit beside the tiers rather than in them.

`PlacingProject` is one, and reaching `reconcile`'s write path means implementing it
as well as tier 3. Its three methods are `load()`, `edit_path()` and
`editing_surface()`, and they are described under
[Where an edit lands](#where-an-edit-lands-and-what-you-own) below.

`SemanticCatalogProject` is the other, one method, `semantic_catalog()`, and it is
what `explore semantic list --local` reads. It is described under
[Reading the semantic layer twice](#reading-the-semantic-layer-twice-for-two-different-questions).

Both are beside rather than on a tier because these protocols are
`runtime_checkable`: a method added to a tier would demote every format that has not
implemented it yet, so `tier_of` would start answering 2 where it answered 3 and the
write path would close for exactly the implementers who were already passing. A
capability a format may legitimately decline belongs in its own protocol, where
declining it is an answer rather than a regression.

That is also why `load()` is there rather than on tier 3, which is where you would
look for it first. Nothing outside the placement path calls it: both callers reach it
only for a format that places, so asking it of tier 3 would demote formats that never
needed it in order to state a requirement that is not theirs.

**Tier 1 is where the value is concentrated, and it is easy to underestimate.**
Declared keys and declared joins reach dex through `definitions()` and through no
other channel: not through the layers, not through a content hash. A format that
implements only tier 1 has already delivered the part of a project that changes what
`explore` concludes.

**Tier 3 is the one to decline on purpose.** The clearest case is a project reduced
from a running graph, where the reduction is not the source of truth: the code that
produced the graph is, so writing into the reduction would edit an artifact that is
regenerated on the next run. Declining the tier is the honest answer, and it is why
the tiers exist rather than a `writeback: no` flag. A flag is a claim the engine has
to trust, while a tier is checkable.

**Ask which artifact the edit lands in, not where the project came from.** Those two
questions have different answers more often than the graph example suggests. A format
can reduce a graph for its model list and still read its declared keys, joins and
semantics from hand-authored files that nothing regenerates, which is a common shape:
an asset graph carries neither column names nor join keys, so a format over one has
to get them from somewhere, and that somewhere is usually a file a person wrote. Those
files are a real source of truth and they are the shape `reconcile` already proposes
edits to. A format holding one may reach tier 3 for that channel while still refusing
to author a model, and deciding from "we are graph-derived" alone would decline a tier
it could honestly serve.

That distinction has teeth, and it is what `maintain reconcile` reads. A format that
does not implement `EditableProject` gets every finding back as an advisory
proposal, with no edits and no stored plan, and a warning naming the format and the
tier it declined. The findings themselves are still surfaced: declining the write
tier removes dex's authority to author an edit, not your need to see the drift.

That used to hold by accident. Reconcile's two mechanical write paths gated on the
`models/staging/stg_<table>.*` scaffold convention and failed closed, so a generated
tree was safe as long as its own directory naming happened not to collide, and a
format whose layers used that vocabulary would have been written into. The guarantee
now rests on what your format declared rather than on what it happened to be called:
the tier is asked first, and the paths themselves come from the format rather than
from that convention.

**If you do implement tier 3, two parameters carry the whole safety story.**

`project_dir` is where the edits go, and it comes from the caller rather than from
however your project was built. dex is applying a stored plan, and a plan records the
directory it was pinned against relative to the repository root, which is what keeps
it valid when the repository moves. A project built from engine configuration need
not point at the same place. Resolving the directory yourself means writing into
whichever project the engine happened to be configured for while hash-checking
against that project's files, and the disagreement is silent. If your format is not
keyed by a directory, ignore the parameter, exactly as you ignore `repo_root` on
`ProjectContext`.

`confirmed` is the human-edit conflict handshake, and it is most of the reason this
tier is separable at all. Re-hash every target against the content the edit was
planned against. A target whose hash moved is a conflict, because a human edited it
while the plan sat in review. With `confirmed=False` a conflict refuses the whole
apply and writes nothing, and the divergence comes back as diffs for someone to read.
With `confirmed=True` that someone has looked and said to go ahead. A write path that
cannot receive `confirmed` silently overwrites the work, which is the failure this
seam exists to prevent, so `EditableProjectContract` asserts the behavior rather than
trusting it: no check on the shape of your class can see it.

**What you return has to say what happened.** `transform apply` reads `written` to
decide whether the plan is now applied and `conflicts` to decide whether to show a
human the divergence and ask. A result answering neither fails in both directions at
once: a plan recorded as applied that wrote nothing, or a conflict that never reaches
the person it was raised for. Return `dbt_project.ApplyResult`, or anything exposing
those two.

## Reading the semantic layer twice, for two different questions

Beside tier 2 sits one optional protocol, `SemanticCatalogProject`, and the reason
it is a second channel rather than a third member of the tier is worth stating
before you implement either.

```python
@runtime_checkable
class SemanticCatalogProject(Protocol):
    def semantic_catalog(self) -> SemanticCatalogView: ...
```

`semantic_layer()` is a **fingerprint**. Its job is to make a change detectable, so
it reduces the layer to a content hash per definition plus the physical column
behind each field, and it hashes what the author wrote rather than what a compiler
produced, which is what keeps a stored baseline stable across a tool upgrade. It
deliberately throws away everything a reader wants: element types, the project's own
labels and descriptions, a measure's aggregation, a metric's composition, and the
token a query actually groups by.

`semantic_catalog()` is the **read view** `explore semantic list` returns, and every
field it carries is a field the fingerprint is right to drop. Widening the
fingerprint to serve it would push presentation metadata into a persisted baseline
and cost it the stability it exists for, so the two are separate reductions of one
layer and your format performs both.

Four things to get right, all of them things dex itself got wrong first:

**An entity is not one record.** `EntityInfo.roles` carries one entry per
`(entity, semantic model)` declaration, with that model's own `type`, `expr` and
`description`. An entity is `primary` in the one model that keys it and `foreign` in
every model that joins to it, the join key differs per model for the same entity, and
each declaration is where a project documents that model's join. The single
top-level `type` is derived, primary wherever any declaration is primary. Returning
one record per entity means picking a value, and whichever you pick is iteration
order rather than a fact about the layer.

**`DimensionInfo.name` is a query token, not a display name.** A caller pastes it
into `--group-by`. If your layer requires a qualified path, return the path;
`definition` and `semantic_model` are where the declaration behind it goes.
`SemanticCatalogContract` asserts that every dimension a metric claims to be
groupable by appears as a dimension row, because the two have to be one vocabulary
or neither can be acted on.

**Resolve the layer to the warehouse, and never invent the resolution.**
`SemanticModelInfo.relation` is the physical relation the model sits on, and it is
the only place a relation appears in the catalog: a dimension, an entity
declaration and a measure each carry `column` and each already names its
`semantic_model`, so an element's address is its column plus its model's relation.
That is what connects your layer to the objects `explore map` and
`explore profile` describe, and what lets a caller answer "which table is behind
this metric". Leave `column` unset wherever the reference is a computed expression
rather than a plain column, because the PII gate resolves a dimension to a column
and reads that column's evidence: a guessed column makes it screen the wrong one
and report the verdict as authoritative. A backend that structurally cannot know
the relation declares that gap instead, which is a different statement from a
format that could resolve it and did not.

**Say what one dimension row is.** `dimension_scope` is `declarations` (one row per
declared dimension) or `queryable_paths` (one row per groupable token, so a dimension
reached through a join appears once per path). Both are honest and they produce very
different counts for one layer, which is unreadable to a caller who is not told
which they hold.

`physical_columns` maps every dimension and entity token, bare and qualified, to the
`(relation, column)` behind it. It is never serialized: the PII request-gate reads it
to resolve a token to a profiled column, which is why the resolution belongs to the
format rather than to a query backend. Leave a token out where the reference is a
computed expression, on the same principle as the `None` columns on
`SemanticModelDef`: a guessed column makes the gate over-claim.

Raise `ProjectError` where the layer cannot be read *yet*, as opposed to being
empty. An uncompiled project and a project that declares no metrics are different
answers and only one is fixed by running a command, and the caller turns the raise
into a refusal naming that command. Declining the protocol outright is also a
complete answer: a format with no semantic layer implements nothing here, and
`explore semantic list --local` refuses by name rather than returning an empty
catalog that reads as a layer with nothing in it.

## Where an edit lands, and what you own

Tier 3 says your format *can* receive an edit. It does not say where the edit goes,
and reconcile decides that before it consults you. `PlacingProject` is where you
answer, and reaching the write path means implementing all three of its methods.

```python
from exmergo_dex_core.adapters.project import ProjectView
from exmergo_dex_core.transform.plans import EditKind


class MyProject:  # ... tier 3 methods as above
    def load(self) -> ProjectView:
        return MyView(root=self.root, files=self.declarations())

    def edit_path(self, kind: EditKind, model: str) -> str | None:
        if kind is EditKind.SCHEMA_YML:
            return f"declarations/{model}.yml"
        return None  # no authored staging model in this format

    def editing_surface(self) -> list[str]:
        return ["declarations"]
```

The three describe one keyspace, and none of them is answerable alone. A key is a key
into a view; a surface is a region of the same space; and a view nobody places into is
a read dex has no use for on this path. A format holding two of the three places
nothing at all, and dex says which one is missing rather than leaving it to surface as
`AttributeError` from inside a command the tier check already let through.

`load()` reads the keyspace, once per command, and returns a `ProjectView`:

| member | type | what breaks without it |
|---|---|---|
| `root` | `str` | the plan has no directory to record its edits as pinned against |
| `files` | `Mapping[str, SourceFileView]` | a placed path resolves to nothing |
| `files[k].content` | `str` | no diff can be built for a human to review |
| `files[k].sha256` | `str` | every existing file pins as a create, so a one-line change renders as a whole-file overwrite and the next apply conflicts on a file nobody touched |

`files` is keyed exactly the way `edit_path` keys and `editing_surface` prefixes,
which is the whole point of the type. `root` is what the plan records as the directory
it was pinned against, relative to the repository root where that subtraction is
possible and verbatim where it is not, so a format keyed by something other than a
directory returns whatever identifies its root and the plan carries that. The hash need
only be consistent with what your own writer re-checks: it is your keyspace, and dex
compares your value against your value.

`ProjectView` and `SourceFileView` are declared protocols and neither is
`runtime_checkable`. Nothing calls `isinstance` against them, because an isinstance
check on a data protocol only asks whether the attribute names exist, which reads as a
type check and is not one. `PlacingProjectContract` makes that check instead, with a
message naming the missing member and what it costs.

`edit_path(kind, model)` says where an edit of that kind for that table lives. Two
things about it are easy to get wrong. `model` is the warehouse table the finding is
about, not a model name in your vocabulary: the `stg_` prefix is dbt's own scaffold
convention and dex does not apply it here. And the return is a key into whatever your
`load()` returns rather than a filesystem path, because your format already owns that
keyspace.

**Answering `None` for a kind is a complete answer, not a degenerate one.** The kinds
are not equally receivable and you are expected to differ across them. `SCHEMA_YML`
is a mutation of a file you already have. `MODEL_SQL` carries dbt SQL that dex
generates alongside the path, so placement alone cannot open that channel: a format
that places a staging model elsewhere gets the proposal as advice rather than having
dbt written into its tree. One `None` and one path is the shape this exists for.

The kind list grows over time (`MACRO_SQL`, `SNAPSHOT_SQL`, `SEED_CSV`, `TEST_SQL`
and `ANALYSIS_SQL` are all dbt-shaped artifacts a non-dbt format may have no
equivalent of), and `None` stays the right answer for every one your format does
not place. The shipped dbt format
answers `None` for them too: reconcile proposes staging models and their
`schema.yml` and nothing else, so a path for the rest would be invented rather
than known.

`editing_surface()` declares the region those paths must stay inside, and it is a
different question with a different caller. `transform plan` validates edits an agent
authored, where there is no `(kind, model)` pair to ask `edit_path` about and no
prior answer to compare against, only a path and the question of whether it is inside
what you admit to owning. Prefixes are matched by path segment, so `declarations`
admits `declarations/orders.yml` and does not admit `declarations_backup/orders.yml`.
Absolute paths and paths climbing out through `..` are refused ahead of this and are
not yours to permit. An empty list is coherent: it refuses every edit rather than
admitting all of them, which is the same statement declining tier 3 makes.

This is containment, which is a safety property and stays mandatory. What the seam
moved is who declares the surface, not whether there is one.

**Honor it in your writer too, and expect dex to re-check it.** Containment is
checked when a plan is stored, and re-checked against this declaration before dex
hands a stored plan to `write_edits`, for the same reason the hashes are re-checked:
a plan is a stored artifact that sits through a human review, so what it was
validated against then is not what it is being written into. That refusal is a hard
one, and `confirmed` is not a way past it. Nobody accepts a write outside the surface
the format itself declared. Your own writer still has to check, because `write_edits`
is a public method of your format and dex is not its only caller, and the conformance
suite asserts the case a prefix comparison gets wrong.

**Declare what your writer accepts, not less.** The shipped dbt format lists every
one of its authored path families *and* the four root manifests it authors by
name, because a
declaration narrower than the writer is not a modest one: it refuses the project
config, the profiles and the package manifests at apply, every one of which is a path
dex authors through a plan.

**One thing dex still spells its own way.** The `unique` test edit finds your model
inside the placed file by the file's own name (`declarations/orders.yml` means a
model named `orders`). If you pack several models into one file, no entry matches and
you get a warning instead of an edit, which is a refusal to guess rather than a wrong
write.

**That edit is checked against your declarations before it is proposed.** If
`definitions()` reports a composite grain covering the column, no column-level
`unique` is proposed on it and the warning names the combination, because the edit
would assert something your project explicitly does not claim: dbt runs a
column-level `unique` and a `unique_combination_of_columns` independently, so the
new test fails every build from then on and can only go green by changing the
declared grain, while a format that resolves the two as dbt's semantics imply
discards it and the plan applies having changed nothing.

This is the practical reason `declared_composite_keys` is worth populating properly,
and why it is a separate field from `declared_keys` rather than several entries in
it. It is not decoration on a diagram. `maintain grain` re-verifies the combinations
that arrive there, so a format that leaves the field empty loses grain verification
on exactly the tables whose grain is composite, and gets offered `unique` tests on
their member columns. Splitting one grain across several entries is worse than
leaving it out, because each entry then claims a column is unique on its own, which
is a stronger claim than the one you made and the one that gets acted on. The
conformance suite checks both shapes.

## The one rule that is not visible in the signatures

**`definitions()` must not raise.** Not on a project that is absent, not on an
ambiguous choice between two, not on a source that will not parse.

Exploration runs against warehouses with no project at all, so absence is an
ordinary state rather than an error, and a format that raises turns a normal
condition into an outage in the middle of a command that had nothing to do with the
project. The empty result is the correct answer; a note saying why is how the
operator tells "nothing declared" apart from "your source has a typo in it".

This is the assertion a second implementation is most likely to get wrong, and a
contract that only checked shapes would not catch it. It is also not hypothetical:
the shipped dbt format violated it until recently, because a `dbt_project.yml` that
is not valid YAML surfaced as `yaml.YAMLError` while only `DbtProjectError` was
caught. The conformance suite below is what found that.

Tier 2 is different, deliberately. `transform_layer()` and `semantic_layer()`
presume a project that loads, because `maintain` already treats an unreadable
project as a handled state and carries a note saying so. Returning empty layers
instead would be worse than raising: an empty layer compared against an empty layer
reads as "no drift" rather than "this could not be checked".

## Writing one

```python
from exmergo_dex_core.dbt_project import ProjectDefinitions


class MyProject:
    name = "my_format"

    def __init__(self, graph):
        self._graph = graph

    def definitions(self) -> ProjectDefinitions:
        defs = ProjectDefinitions(present=True)
        defs.built_relation_names = sorted(self._graph.models)
        # ...declared keys, joins, metric models
        return defs
```

`ProjectDefinitions` is the engine's existing model, not a new neutral one. That is
deliberate: it is already what `definitions()` returns and already what every
consumer reads, so a second format reuses the vocabulary rather than introducing a
parallel one that has to be mapped at every call site.

Where your format's shape and this model's shape disagree, say so in `notes` rather
than inventing a value. A fabricated field is indistinguishable from a measured one
by the time a finding is shown to a human.

That rule holds at tier 2 too, and two things make it possible to follow there.

**The three `path` fields are optional.** `SourceTable`, `SemanticModelDef` and `MetricDef`
each carry a `path`, and each accepts `None`. They are provenance and nothing else: the
file an analyst would open, carried on the `dangling_source` and `definition_changed`
findings, never opened by dex. A format whose sources are declared in configuration, or
whose metrics are objects in a graph, leaves them unset, and the finding omits the key
rather than reporting a null. Both findings still identify their subject by name, so
nothing is lost but a shortcut. Supplying a plausible-looking path instead would attach
a file that is not there to a high-severity finding, which is the same failure the
`None` on `SemanticModelDef`'s column mappings exists to avoid.

**`TransformLayer` and `SemanticLayer` carry `notes`.** Same meaning as on
`ProjectDefinitions`: what your format could not supply. A layer that is faithful but
narrower than a dbt project's has somewhere in the return value to say so, and the
`maintain` commands fold those notes into their warnings, at detection time as well as
when the baseline is pinned. Worth using for anything a reader would otherwise
misread: a `file_count` of zero beside a dozen models is a shape nobody can interpret
without one line of explanation.

Notes are informational, deliberately. No detector reads them, they take no part in any
comparison (so a changed note is never drift), and dex will not branch on one. Anything
dex has to *decide* from belongs in a tier, which is checkable, rather than in prose the
engine would have to trust. That is the same reason the tiers exist instead of a
`writeback` flag.

## Proving it works

```
pip install "exmergo-dex-core[project-conformance]"
```

```python
from exmergo_dex_core.adapters.conformance import ExploreProjectContract


class TestMyProject(ExploreProjectContract):
    def make_project(self):
        return MyProject(nothing_declared())
```

pytest collects the inherited assertions and runs the contract against your format.
Use `MaintainProjectContract` instead if you reach tier 2, `EditableProjectContract`
if you reach tier 3, mix `DeclaringProjectContract` and `SemanticProjectContract`
beside it for the content your format declares, mix `PlacingProjectContract` and
`SemanticCatalogContract` beside it for each protocol you implement beside the
tiers, and mix
`ProjectFactoryContract` in front of it if dex will build your format from a name
rather than be handed an instance. Construction is a separate contract, so a format
that passes the behavioral suite can still be unreachable from configuration; "the
suite is green" should mean correct **and** constructable.

Tier 2's assertions are thin, because the layers' contents are your format's business.
The one worth knowing about is that your layers **survive a JSON round trip inside a
`Snapshot`**, since that is what reaching tier 2 buys: a store serializes the baseline
and a later command loads it back to diff against. The check is deliberately a real
serialization rather than a copy, because that is where a value your format chose can be
accepted in Python and rejected on the way back, and the failure would otherwise surface
on the run *after* the one that caused it.

Two hooks are worth overriding beyond `make_project`:

- **`make_unreadable_project()`** returns a project whose source your format cannot
  parse. It defaults to `None`, which skips the never-raises assertions with a
  message saying so. That is a compromise, because a format reduced from an
  in-memory object may genuinely have no unparseable state. If yours does have one, override
  it: those are the assertions worth the most.
- **`DeclaringProjectContract`**, mixed in beside your tier contract, checks that a
  declared key and a declared join actually arrive. It is separate because the two
  contracts answer different questions: whether dex can safely call your format, and
  whether reading it was worth doing.

  It carries two further hooks that default to skipping, and both are worth
  supplying if your format can reach the state:

  - **`a_project_declaring_a_composite_key()`** returns `(project, model, columns)`.
    `declared_composite_keys` is a separate field from `declared_keys` and nothing
    else in the suite reaches it. **Declare more than two columns**: a format that
    handles a composite grain by special-casing the pair passes a two-column fixture
    and fails a four-column one, so a pair cannot tell you what you came to find out.
    A truncated composite key is the expensive failure here, because it does not read
    as a missing declaration. It reads as a narrower grain that is simply wrong.
  - **`a_project_declaring_a_join_with_differently_named_sides()`** returns the same
    shape as `a_project_declaring_a_join`, with `column != to_column` (the contract
    checks that and refuses a mirrored fixture). If both ends of your join fixture are
    spelled the same, an implementation that reads the source column and copies it
    onto the target satisfies the plain join assertion exactly, and the defect ships.
    A key whose two ends are named differently is the ordinary case, and a format that
    mirrors joins on a column the target may not have, surfacing as a wrong answer
    rather than as a failure.

- **`SemanticProjectContract`**, mixed in beside `MaintainProjectContract` if your
  format declares semantics at all. Its one hook,
  `a_project_declaring_a_semantic_model()`, returns `(project, name, dimensions,
  measures)` where the two mappings are `field name -> the warehouse column behind
  it`.

  It asserts tier 2 first, against the project the hook returns. `semantic_layer`
  is a tier-2 member, so a format mixing this in beside `ExploreProjectContract`
  would otherwise fail with `AttributeError: no attribute 'semantic_layer'`, which
  names the missing attribute rather than the tier it belongs to.

  This is the gap most worth closing, because the tier contract cannot see it.
  `MaintainProjectContract` checks that an *empty* project yields an empty semantic
  layer and never looks at a populated one, so a format that reads every field name
  and drops the column behind each passes the tier suite completely. `SemanticModelDef`
  keys every field to a column and the drift detector skips any field whose column is
  `None`, correctly, since it cannot resolve what it was not given. A layer mapped
  entirely to `None` therefore validates, serializes, and compares clean forever: the
  check does not fail, it never runs, and a dropped warehouse column that should raise
  `dangling_reference` at high severity raises nothing. Map a field to `None` when you
  genuinely have no bare column for it, and include one such field if you can produce
  one, because `categorical_dimensions` takes `str` rather than `str | None` and a
  field that is categorical *and* unresolved has to be left out of that mapping rather
  than given an invented column.

Tier 3 adds one more hook, and unlike the two above it is **not optional**.
`an_edit_against_a_changed_target()` returns a project, a directory, an edit set
pinned to content that has since moved, and a callable reading what the target holds
now. It raises rather than skipping because the excuse that justifies skipping
`make_unreadable_project` does not apply here. A format may genuinely have no
unparseable state, but a format that reached tier 3 writes into a source of truth a
human can also edit, so the case where the human got there first exists for all of
them.

Four assertions are built from it. An unconfirmed write must leave the target alone
and a confirmed one must go through, and those two only mean something as a pair,
since a `write_edits` that never writes satisfies the first on its own. **That pair
rules out a writer that never writes; it does not rule out a writer that writes too
much**, which is what the other two are for:

- **A conflict refuses the apply, not the conflicting edit within it.** The edit set
  is the unit. A writer that lands the clean edits and holds back the conflicting one
  leaves the project matching neither the proposal nor what the human had, while the
  apply reports itself refused, so nothing records which half arrived. A single-edit
  set cannot see this, which is why the assertion stages two.
- **A create pinned to no prior content is refused when the target now exists.**
  `old_content_hash=None` is a claim that the file was absent at plan time. If one is
  there now, the claim is false and honoring it costs the human who created it during
  review the whole file rather than the lines that diverged. A writer reading `None`
  as "nothing to compare, go ahead" passes every other assertion, because in the
  staged conflict the pinned hash is a real one.

One optional hook is worth supplying beside them. **`a_clean_edit(project)`** returns
an edit that is not in conflict, pinned truthfully against the project the conflict
hook just staged, and a callable reading its target. Without it the all-or-nothing
assertion can only ask what `write_edits` *reported*, so a writer that lands half an
edit set and reports nothing written still passes; with it, the target is read
directly and the claim is checked against the project. Returning `None` falls back to
an edit derived from the staged conflict's own path, which is inside your surface by
construction and needs nothing from you.

`PlacingProjectContract`, mixed in beside it, has one hook that is likewise not
optional. `placeable_model()` returns a warehouse table your format would place an
edit for, spelled as the warehouse spells it rather than as your format names the
model derived from it, because that is how reconcile's findings arrive and it is the
thing `edit_path` is most often gotten wrong on. The assertions are cheap and mostly
about your three answers agreeing: at least one kind places somewhere, every path
`edit_path` returns is inside `editing_surface()`, and the surface itself stays
within the project. That middle one is a reason the contract exists, since a
placement outside your own declared surface builds a proposal the plan store then
refuses, and from the outside that reads as dex declining rather than as the format
contradicting itself.

Two more come from `load()` being part of the same keyspace. The view has to carry
`root` and `files`, and the entry behind a path your own hook staged an edit against
has to carry `content` and `sha256`, each of which fails silently in its own way
when it is absent. And `write_edits` has to refuse a path outside the surface you
declared: what is asserted is the case a prefix comparison gets wrong, a sibling that
merely starts with the same characters, since a format matching by string prefix
admits the whole neighborhood of every region it owns while passing everything else.
Refusing by raising and refusing by writing nothing both count.

Placement presupposes tier 3, and the contract checks that first, the way
`SemanticProjectContract` checks tier 2. Mix it in beside `EditableProjectContract`:
the assertions here write through your format, and placing an edit a format cannot
receive describes a path that stops halfway.

The suite needs only pytest: no dialect engine, no connector. A packaging test keeps
it that way.

Formats contributed here run the same suite: see
`packages/dex-core/tests/adapters/test_project_parity.py`, which is deliberately the
same few lines a third party writes.

## Constructing one

The tiers describe what dex may ask of a project it already holds. Getting it to
hold one is a separate contract, and it is separate on purpose: keeping it off the
tiers is what preserves the property that makes this seam cheap, that a class with
the right methods is a project, with no base class and no registration step.

There are two ways in, and a host normally needs only one.

**A host that runs dex in its own process hands over an instance.**

```python
engine = DexEngine.from_repo(repo_root, project_format=MyProject(graph))
```

That always wins over anything named in configuration. A caller holding an object
has already made the decision configuration exists to make for the callers who are
not.

**Everyone else names a format in `.dex/config.yml`**, which is the only door open
to a host that reaches dex as a subprocess:

```yaml
project:
  format: mypkg.projects:my_project
  options:
    graph: orders
```

`format` is an open registry rather than a closed set:

| Name | Example | For |
|---|---|---|
| shipped | `dbt`, `ossie` | dex's own formats, and never shadowable by anything installed |
| dotted path | `mypkg.projects:my_project` | a factory reachable by import, with no packaging work |
| entry point | `acme` | a name an installed distribution registered under `exmergo_dex_core.projects` |

A format published as its own package registers itself:

```toml
# in your own pyproject.toml
[project.entry-points."exmergo_dex_core.projects"]
acme = "dex_acme_format:acme_format"
```

Install it beside dex and `format: acme` resolves. A shipped name always wins over a
registration, so installing a package can never silently redirect which models an
existing repo is reasoned about, and nothing in the output would have said so.

`--project-format` overrides the configured name for one run. It leaves `options`
behind when it names a different format, because options are not namespaced by
format and one format's coordinates are not another's.

### What a factory is

Anything callable that takes a `ProjectContext` and returns a project: a function, a
class whose `__init__` takes one, or a classmethod like `DbtProject.from_context`.

`ProjectContext` is a set of nullable slots, and the point of the shape is that a
format ignores the ones it does not have.

- **`repo_root`**, the directory dex was pointed at, or `None` when there is no
  repository in the picture.
- **`project_dir`**, where within that repository the project was pinned, relative
  to `repo_root`. `None` when nothing pinned one.
- **`connector`**, the name of the warehouse dex resolved for this run, or `None`
  when nothing named one. A **name**, never a live adapter and never a credential,
  so reading it opens no connection and costs nothing. It is here because a format
  may have to read an authored expression or relation name the way the active
  warehouse would, and identifier arity, quoting, and unquoted-case folding are
  the connector's rules: a format that guesses them links a declaration to the
  wrong column or to none. dbt reads its own target and ignores this.
- **`options`**, your format's own coordinates, passed through verbatim. dex does
  not interpret them, so the keys are yours to define and yours to validate.

**Refuse an option you cannot honor.** A silently dropped setting is
indistinguishable from a working one right up until dex is reading a different
project than the configuration named, and that surfaces as drift nobody can
reproduce, arbitrarily far from the line that caused it. The shipped dbt format
refuses unknown options by naming them, and the conformance suite checks that yours
does too.

**Construction has to be cheap.** dex builds a project per command rather than
holding one, because a project is an artifact a previous command may have just
rewritten and a stale read is a wrong drift report. Open a connection, fetch a
graph, or parse anything large lazily on first use, not in the factory. An instance
a host hands in is held instead, because its freshness is the host's to know.

**No secret ever reaches a `ProjectContext`**, because `.dex/config.yml` is
committed. Read the credential from the environment at construction, or skip this
contract and hand the engine a project you built yourself.

### Two alternatives that were rejected

Recorded so they are not re-argued.

**A constructor argument alone.** It is the smaller change and it is what storage
shipped first, and storage had to add the other half afterwards for exactly the
reason that applies here: a host talking to dex as a subprocess has no object to
pass, so the seam would have been unreachable for the deployment shape that asked
for it.

**Keying the context on a directory.** `project_dir` alone reads naturally against
the only format that exists, and that is what makes it the wrong choice: a project
reduced from a running graph has no directory, a hosted one has service coordinates,
and a contract shaped around dbt would have left both unbuildable. The nullable
slots plus verbatim `options` are what let a format that is keyed by nothing at all
be named the same way.

**Declaring `load()` on tier 3.** It is where a reader looks for it, and it is the
protocol the method's callers are typed against, so it was the obvious home. It is
the wrong one twice over. These protocols are `runtime_checkable`, so a method added
to tier 3 demotes every format that has not implemented it, which is the argument
that put `PlacingProject` beside the tiers in the first place; and the requirement is
not tier 3's to state, because nothing outside the placement path calls `load()`. A
format that receives edits and does not place them never needed a view, and would
have been demoted to say otherwise. Beside the two methods that share its keyspace,
it demotes only formats that were already going to fail, and it fails them into the
advisory degradation the gate was written to give.
