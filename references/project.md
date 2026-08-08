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
| 3 | `EditableProject` | `write_edits()` | `transform` and `reconcile` may write back |

`tier_of(project)` reports the highest tier an object satisfies, checked with
`isinstance` rather than declared, so a format cannot claim a tier it has not
implemented.

**Tier 1 is where the value is concentrated, and it is easy to underestimate.**
Declared keys and declared joins reach dex through `definitions()` and through no
other channel: not through the layers, not through a content hash. A format that
implements only tier 1 has already delivered the part of a project that changes what
`explore` concludes.

**Tier 3 is the one to decline on purpose.** A project reduced from a running graph
cannot receive an edit: its source of truth is the code that produced the graph, so
writing into the reduction would edit an artifact that is regenerated on the next
run. Declining the tier is the honest answer, and it is why the tiers exist rather
than a `writeback: no` flag. A flag is a claim the engine has to trust, while a tier
is checkable.

That distinction has teeth, and it is what `maintain reconcile` reads. A format that
does not implement `EditableProject` gets every finding back as an advisory
proposal, with no edits and no stored plan, and a warning naming the format and the
tier it declined. The findings themselves are still surfaced: declining the write
tier removes dex's authority to author an edit, not your need to see the drift.

That used to hold by accident. Reconcile's two mechanical write paths gate on the
`models/staging/stg_<table>.*` scaffold convention and fail closed, so a generated
tree was safe as long as its own directory naming happened not to collide, and a
format whose layers used that vocabulary would have been written into. The
convention checks are still there as a second line; what changed is that the tier is
asked first, so the guarantee rests on what your format declared rather than on what
it happened to be called.

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
Use `MaintainProjectContract` instead if you reach tier 2, and mix
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
| shipped | `dbt` | dex's own formats, and never shadowable by anything installed |
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

`ProjectContext` has three fields, and the point of the shape is that a format
ignores the ones it does not have.

- **`repo_root`**, the directory dex was pointed at, or `None` when there is no
  repository in the picture.
- **`project_dir`**, where within that repository the project was pinned, relative
  to `repo_root`. `None` when nothing pinned one.
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
