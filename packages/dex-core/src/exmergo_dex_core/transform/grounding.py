"""What a plan depends on, and how much of that dex actually established.

A host that applies a change offline and validates it in a sandbox has to decide
whether the change is grounded before either happens: which relations it reads,
which project nodes it touches, which semantic definitions it reaches, and
whether the answer is complete. The engine already resolves each of those
separately. ``transform references`` indexes the project, the semantic catalog
resolves a metric to the model behind it, and the exploration cache knows the
warehouse. None of them is bound to a plan, and none of them says whether
resolution finished.

That last part is the contract. The difference between "this plan depends on
nothing" and "resolution did not finish" is invisible in an empty list, and a
host reading the first when it should have read the second admits a change whose
dependencies nobody checked. So :class:`Grounding` reports the verdict before the
findings: ``completeness``, then what could not be resolved, then what more than
one thing could have meant, then what nothing defines, then the dependencies
themselves.

**A name defined nowhere is a finding, not a gap.** ``unresolved``,
``ambiguous`` and ``missing`` are one count taken three ways: a reference dex could
not name, a name with more than one definition, and a name with none. Only the
first is about whether dex finished reading, so only it (with ``limits``) lowers
``completeness``. ``ref('no_such_model')`` is a finished answer that says the build
has nothing to run, and folding it into the verdict would make ``partial`` mean
both "dex did not finish" and "dex finished and found a broken name".

**Staleness is reported beside completeness, not folded into it.** A fully
resolved graph read out of a manifest older than the model sources is complete
and stale at once, and collapsing the two would make a caller choose which fact
to lose. ``freshness`` carries the timestamps so the caller judges rather than
inherits dex's threshold.

**The result is bound to what produced it.** ``binding`` fingerprints the plan,
the source tree, the effective configuration, the package inputs, and the engine
version, so a host can tell that the grounding it admitted and the build it later
ran describe the same state. Nothing here is a security control on its own; it is
what makes a later mismatch visible instead of silent.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from .. import __version__
from ..edits import EditOp
from .plans import PlanEdit

#: How a dependency was established. A caller weighing evidence needs to know
#: whether dex read dbt's own compiled artifact or scanned the source itself,
#: because the first is what dbt will do and the second is dex's reading of what
#: dbt will do.
ORIGINS = ("manifest", "semantic_manifest", "source_scan")

#: Where the definition a dependency resolves to lives once the plan lands, in
#: the order dbt prefers them: the plan's own files and the rest of the project
#: before an installed package.
DEFINITIONS = ("plan", "project", "package", "missing")

#: What ``ref()`` reaches. The reference index kinds a ``ref()`` by this
#: project's own nodes and calls anything else a model, so a ref to a package's
#: seed arrives as a model. The lookup spans all three and the dependency takes
#: the kind of what it finds, which is also the kind the manifest files it under.
_REF_KINDS = ("model", "seed", "snapshot")

#: Kinds dbt resolves only against what the project and its packages declare, so
#: zero definitions fails the parse. Every other kind can be satisfied by
#: something a static read cannot see: dbt's own and the adapter's macros,
#: ``--vars`` and a var's default, a dimension MetricFlow supplies itself.
_CONCLUSIVE_KINDS = frozenset({*_REF_KINDS, "source", "metric", "measure"})

#: Kinds with any definition to look up. Columns have none: a column is defined
#: by whatever the warehouse returns, which no file says.
_LOOKUP_KINDS = frozenset({*_CONCLUSIVE_KINDS, "macro", "var", "entity", "dimension"})

#: What a plan can be found to depend on. The first eleven are the vocabulary
#: `transform references` already answers in, so the two can be compared
#: directly; the last two are the physical and semantic ends the reference index
#: does not reach on its own.
DEPENDENCY_KINDS = (
    "model",
    "source",
    "seed",
    "snapshot",
    "macro",
    "var",
    "column",
    "metric",
    "entity",
    "dimension",
    "measure",
    "semantic_model",
    "relation",
)


class GroundedDependency(BaseModel):
    """One thing the plan reads, and the evidence that it resolves.

    ``defined`` and ``relation`` answer different questions and do not imply each
    other. ``defined`` says whether the name exists once the plan lands, and
    where: ``plan`` when a file this plan writes defines it (a rewrite counts, so
    it does not mean "new"), ``project`` for a project file the plan leaves alone,
    ``package`` when only an installed package does (``package`` then names it),
    and ``missing`` when nothing does. It is absent where dex makes no claim: a
    column, a macro or var the project does not declare, a ``ref()`` that names
    another project, a name only the compiled semantic catalog knows, or a name
    dex could not rule out because part of the project went unread.

    ``resolved_to`` is what the name turned out to name, where that differs from
    the name itself: a measure resolves to its semantic model, a semantic model
    to the transformation model it sits on. ``relation`` is the physical address
    where dex could reach one, and it is absent rather than guessed: an ephemeral
    model compiles to no relation at all, and inventing one would send a caller
    looking for a table that does not exist.
    """

    kind: str
    name: str
    defined: str | None = None
    resolved_to: str | None = None
    relation: str | None = None
    origin: str = "source_scan"
    package: str | None = None
    used_in: list[str] = Field(default_factory=list)


class UnresolvedReference(BaseModel):
    """A reference dex read and could not name.

    ``{{ ref(var('x')) }}`` is the canonical one: there is a dependency there,
    dex knows it exists, and no static reading names it. Reporting it is the
    whole difference between an incomplete answer and a wrong one.
    """

    path: str
    line: int
    form: str
    kind: str
    reason: str


class AmbiguousReference(BaseModel):
    """A name more than one thing in scope defines.

    An installed package shipping a model this project also defines is the case
    that matters: dbt resolves the project's copy, and a caller who does not know
    the package still ships one is surprised the day somebody deletes the local
    file.
    """

    name: str
    kind: str
    candidates: list[str] = Field(default_factory=list)


class MissingReference(BaseModel):
    """A name the plan reads that nothing defines once the plan lands.

    The zero-definition counterpart of :class:`AmbiguousReference`, and as sure as
    it: one is only made when dex read everything that could have defined the
    name. ``used_in`` is where the plan reads it, which is where the fix goes.
    """

    kind: str
    name: str
    used_in: list[str] = Field(default_factory=list)


class GroundingBinding(BaseModel):
    """What this grounding was computed from.

    Five fingerprints because five things can move independently and each moves
    the answer: the plan itself, the files it was resolved against, the
    configuration in force, the packages contributing names, and the engine doing
    the resolving. A digest is ``None`` where the input does not exist, which is
    a different state from a digest over nothing.
    """

    plan_digest: str | None = None
    source_digest: str | None = None
    config_digest: str | None = None
    packages_digest: str | None = None
    engine_version: str = __version__


class Grounding(BaseModel):
    """A plan's dependencies, and how much of the answer is finished."""

    # "complete" means dex finished reading: every reference in the plan was
    # named statically (nothing in `unresolved`) and every name was looked up
    # and followed as far as dex goes (nothing in `limits`). It is not a claim
    # that every named reference exists. `ref(var('x'))` is unresolved and
    # lowers this to "partial"; `ref('no_such_model')` is named, looked up, and
    # found nowhere, which is reported in `missing` and does not move this field,
    # exactly as a name with two definitions is reported in `ambiguous`. A
    # missing metric or measure is reported the same way. So a host admitting a
    # change reads `completeness == "complete"` and `missing == []` together.
    # `missing` is never a guess: where part of the project went unread (a
    # declared package not installed, a YAML file that did not parse), a name
    # with no definition is left out of it and a limit says why, which is what
    # makes the answer "partial".
    completeness: str = "unresolved"
    dependencies: list[GroundedDependency] = Field(default_factory=list)
    relations: list[str] = Field(default_factory=list)
    unresolved: list[UnresolvedReference] = Field(default_factory=list)
    ambiguous: list[AmbiguousReference] = Field(default_factory=list)
    missing: list[MissingReference] = Field(default_factory=list)
    limits: list[str] = Field(default_factory=list)
    freshness: dict[str, Any] = Field(default_factory=dict)
    binding: GroundingBinding = Field(default_factory=GroundingBinding)

    def __getitem__(self, key: str) -> Any:
        if key in type(self).model_fields:
            return getattr(self, key)
        raise KeyError(key)

    def __contains__(self, key: object) -> bool:
        return key in type(self).model_fields

    def get(self, key: str, default: Any = None) -> Any:
        if key in type(self).model_fields:
            return getattr(self, key)
        return default

    def data(self) -> dict[str, Any]:
        # The verdict leads. A long dependency list is the ordinary case, so it
        # will be read from the top and sometimes cut from the bottom, and the
        # honesty is what must not be what gets lost.
        return {
            "completeness": self.completeness,
            "limits": self.limits,
            "unresolved": [u.model_dump(mode="json") for u in self.unresolved],
            "ambiguous": [a.model_dump(mode="json") for a in self.ambiguous],
            "missing": [m.model_dump(mode="json") for m in self.missing],
            "freshness": self.freshness,
            "binding": self.binding.model_dump(mode="json"),
            "relations": self.relations,
            "dependencies": [
                d.model_dump(mode="json", exclude_none=True) for d in self.dependencies
            ],
        }


def _digest(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def source_digest(view: Any) -> str:
    """A fingerprint of the project's editable source tree.

    Built from the per-file hashes the view already carries, so this costs a sort
    rather than a second read of the project.
    """

    return _digest(
        "\n".join(f"{path}:{view.files[path].sha256}" for path in sorted(view.files))
    )


def config_digest(config: Any) -> str | None:
    """A fingerprint of the configuration in force, or ``None`` where there is none.

    Over the serialized config rather than the file, because a config assembled in
    memory by a host is exactly as load-bearing as one read from disk and has no
    file to hash.
    """

    if config is None:
        return None
    try:
        payload = config.model_dump(mode="json")
    except AttributeError:
        return None
    return _digest(json.dumps(payload, sort_keys=True, separators=(",", ":")))


def packages_digest(project: Path) -> str | None:
    """A fingerprint of the package inputs: what is declared, pinned, and installed.

    All three, because they disagree in ways that matter. A declaration without a
    lockfile resolves differently on two days; a lockfile without an install
    resolves to nothing at all; and an install that no longer matches either is
    what a stale container looks like. ``None`` means the project declares no
    packages, which is different from declaring some and installing none.
    """

    parts: list[str] = []
    declared = False
    for name in ("packages.yml", "dependencies.yml", "package-lock.yml"):
        candidate = project / name
        if candidate.is_file():
            content = candidate.read_text(encoding="utf-8")
            parts.append(f"{name}:{_digest(content)}")
            if name != "package-lock.yml":
                declared = True
        else:
            parts.append(f"{name}:absent")
    from .build import packages_dir as _packages_dir

    installed = _packages_dir(project)
    if installed.is_dir():
        for child in sorted(p for p in installed.iterdir() if p.is_dir()):
            manifest = child / "dbt_project.yml"
            version = ""
            if manifest.is_file():
                try:
                    parsed = yaml.safe_load(manifest.read_text(encoding="utf-8"))
                except yaml.YAMLError:
                    parsed = None
                if isinstance(parsed, dict):
                    version = str(parsed.get("version") or "")
            parts.append(f"installed:{child.name}:{version}")
    if not declared and not parts[3:]:
        return None
    return _digest("\n".join(parts))


def _manifest_relations(manifest: dict[str, Any] | None) -> dict[tuple[str, str], str]:
    """``(kind, referable name) -> physical relation`` from a compiled manifest.

    Guarded throughout: a hand-rolled or truncated manifest has to fall through
    quietly rather than raise, because grounding a plan against a project whose
    artifacts are odd is exactly when a caller most needs the partial answer.
    """

    from ..dbt_project import strip_relation_quoting

    relations: dict[tuple[str, str], str] = {}
    if not isinstance(manifest, dict):
        return relations
    for uid, node in (manifest.get("nodes") or {}).items():
        del uid
        if not isinstance(node, dict):
            continue
        kind = node.get("resource_type")
        name = node.get("name")
        relation = node.get("relation_name")
        if (
            kind in {"model", "seed", "snapshot"}
            and isinstance(name, str)
            and isinstance(relation, str)
            and relation
        ):
            relations[(kind, name)] = strip_relation_quoting(relation)
    for uid, node in (manifest.get("sources") or {}).items():
        del uid
        if not isinstance(node, dict):
            continue
        source_name, table = node.get("source_name"), node.get("name")
        relation = node.get("relation_name")
        if (
            isinstance(source_name, str)
            and isinstance(table, str)
            and isinstance(relation, str)
            and relation
        ):
            relations[("source", f"{source_name}.{table}")] = strip_relation_quoting(
                relation
            )
    return relations


def _semantic_dependencies(
    catalog: Any, names: dict[str, set[str]]
) -> tuple[list[GroundedDependency], set[tuple[str, str]]]:
    """Resolve semantic names to the models and relations behind them.

    A metric names its input measures, a measure names its semantic model, and a
    semantic model names both the transformation model it sits on and the relation
    that model builds. Following that chain is what answers "which table is behind
    this metric", and it is a name join through the compiled artifacts rather than
    a second resolver.

    Returns what the catalog resolved and the ``(kind, name)`` pairs it does not
    carry. Whether an absent name is a stale catalog or a name nothing defines is
    the caller's call, because only the caller knows what the project declares.
    """

    dependencies: list[GroundedDependency] = []
    absent: set[tuple[str, str]] = set()
    models = {m.name: m for m in getattr(catalog, "semantic_models", [])}
    measures = {m.name: m for m in getattr(catalog, "measures", [])}
    metrics = {m.name: m for m in getattr(catalog, "metrics", [])}

    def add_model(model_name: str, via: str) -> None:
        info = models.get(model_name)
        if info is None:
            absent.add(("semantic_model", model_name))
            return
        dependencies.append(
            GroundedDependency(
                kind="semantic_model",
                name=model_name,
                resolved_to=info.model_ref,
                relation=info.relation,
                origin="semantic_manifest",
                used_in=[via],
            )
        )

    for name in sorted(names.get("metric", set())):
        metric = metrics.get(name)
        if metric is None:
            absent.add(("metric", name))
            continue
        dependencies.append(
            GroundedDependency(
                kind="metric",
                name=name,
                resolved_to=", ".join(metric.input_measures or []) or None,
                origin="semantic_manifest",
            )
        )
        for model_name in metric.semantic_models or []:
            add_model(model_name, f"metric:{name}")
    for name in sorted(names.get("measure", set())):
        measure = measures.get(name)
        if measure is None:
            absent.add(("measure", name))
            continue
        dependencies.append(
            GroundedDependency(
                kind="measure",
                name=name,
                resolved_to=measure.semantic_model,
                origin="semantic_manifest",
            )
        )
        if measure.semantic_model:
            add_model(measure.semantic_model, f"measure:{name}")
    return dependencies, absent


def _unread_definitions(index: Any, project: Path, written: set[str]) -> list[str]:
    """What dex could not read that might define a name, as reasons.

    Empty means a name with no definition really has none. Each reason is a place
    dbt would look that dex did not: a declared package that is not installed, one
    whose install directory the declaration does not name (a git or tarball
    package), one that failed to load, a YAML file that does not parse, or a
    package set this plan changes, which nothing has installed yet.
    """

    from .build import missing_packages, unverifiable_packages

    reasons: list[str] = []
    changed = sorted(written & {"packages.yml", "dependencies.yml"})
    if changed:
        reasons.append(
            f"this plan changes {', '.join(changed)}, and the packages it declares "
            "are not installed until `transform deps` runs"
        )
    absent = missing_packages(project)
    if absent:
        reasons.append(
            "these declared packages are not installed: "
            + ", ".join(p.describe() for p in absent)
        )
    unknown = unverifiable_packages(project)
    if unknown:
        reasons.append(
            "dex cannot check whether these declared packages are installed: "
            + ", ".join(p.describe() for p in unknown)
        )
    if index.unread:
        reasons.append("these could not be read: " + ", ".join(index.unread))
    return reasons


def ground_plan(
    view: Any,
    edits: list[PlanEdit],
    *,
    project: Path,
    config: Any = None,
    catalog: Any = None,
    catalog_error: str | None = None,
    plan_digest: str | None = None,
) -> Grounding:
    """Everything the plan's edits depend on, with the verdict on the answer.

    ``view`` is the project as it stands; the plan's own edits are overlaid in
    memory first, exactly as the delete guard does, because a plan's dependencies
    are the dependencies of the project *after* it lands. Nothing is written and
    no connection is opened.

    Each name is looked up where dbt would look for it: the project after the
    plan, then its installed packages. The overlaid project is the authority on
    whether a name exists, because it is what dbt will parse. The compiled
    artifacts are older than the plan, so they supply where a name is built and
    what a metric reaches, and count as evidence that a name exists, but never as
    evidence that one does not.
    """

    from ..dbt_project import SourceFile, manifest_freshness
    from ..edits import content_hash
    from ..references import ReferenceIndex

    edited = {edit.path for edit in edits}
    deleted = {edit.path for edit in edits if edit.op is EditOp.DELETE}
    written = edited - deleted
    surviving = {
        path: source.content
        for path, source in view.files.items()
        if path not in deleted
    }
    for edit in edits:
        if edit.op is EditOp.UPSERT and edit.new_content is not None:
            surviving[edit.path] = edit.new_content
    after = view.model_copy(
        update={
            "files": {
                path: SourceFile(
                    path=path, content=content, sha256=content_hash(content)
                )
                for path, content in surviving.items()
            }
        }
    )

    index = ReferenceIndex(after)
    limits = list(index.limits)
    resolved, indeterminate = index.references_from(written)

    settled: dict[tuple[str, str], tuple[str | None, str, str | None]] = {}

    def settle(kind: str, name: str) -> tuple[str | None, str, str | None]:
        """``(defined, kind, package)`` for one name, in dbt's order of preference."""

        if (kind, name) not in settled:
            kinds = (
                _REF_KINDS
                if kind in _REF_KINDS
                else (kind,)
                if kind in _LOOKUP_KINDS
                else ()
            )
            found = index.declarations_of(name, kinds) if kinds else []
            local = [d for d in found if d.package is None]
            chosen = (
                next((d for d in local if d.path in written), None)
                or next(iter(local), None)
                or next(iter(found), None)
            )
            if chosen is None:
                settled[(kind, name)] = (None, kind, None)
            elif chosen.package is not None:
                settled[(kind, name)] = ("package", chosen.kind, chosen.package)
            else:
                where = "plan" if chosen.path in written else "project"
                settled[(kind, name)] = (where, chosen.kind, None)
        return settled[(kind, name)]

    relations = _manifest_relations(after.manifest)
    seen: dict[tuple[str, str], GroundedDependency] = {}
    # A name reached only through `ref('other', 'm')` is never called missing:
    # dbt looks for it in another project when `other` is one, and dex reads one.
    unqualified: set[tuple[str, str]] = set()
    for reference in resolved:
        if reference.name is None:
            continue
        defined, kind, package = settle(reference.kind, reference.name)
        key = (kind, reference.name)
        if reference.qualifier is None:
            unqualified.add(key)
        existing = seen.get(key)
        if existing is None:
            relation = relations.get(key)
            seen[key] = GroundedDependency(
                kind=kind,
                name=reference.name,
                defined=defined,
                relation=relation,
                origin="manifest" if relation else "source_scan",
                package=package,
                used_in=[reference.path],
            )
        elif reference.path not in existing.used_in:
            existing.used_in.append(reference.path)

    # The plan's own metrics and measures need no catalog: what they reach is in
    # the plan. Everything else is followed through it, including names nothing
    # in the project defines, because the catalog knowing one is evidence the
    # name exists somewhere dex does not read.
    semantic = {
        key
        for key, dependency in seen.items()
        if key[0] in ("metric", "measure") and dependency.defined != "plan"
    }
    in_catalog: set[tuple[str, str]] = set()
    if catalog is not None and semantic:
        names: dict[str, set[str]] = {}
        for kind, name in semantic:
            names.setdefault(kind, set()).add(name)
        followed, absent = _semantic_dependencies(catalog, names)
        in_catalog = semantic - absent
        for dependency in followed:
            key = (dependency.kind, dependency.name)
            existing = seen.get(key)
            if existing is None:
                seen[key] = dependency
                continue
            existing.resolved_to = existing.resolved_to or dependency.resolved_to
            existing.relation = existing.relation or dependency.relation
            existing.origin = dependency.origin
            existing.used_in.extend(
                via for via in dependency.used_in if via not in existing.used_in
            )
        for kind, name in sorted(absent):
            if kind == "semantic_model":
                limits.append(f"semantic model '{name}' is not in the catalog")
            elif seen[(kind, name)].defined is not None:
                limits.append(
                    f"{kind} '{name}' is defined in the source but not in the "
                    "compiled semantic catalog, so the models behind it were not "
                    "resolved; recompiling the project refreshes the catalog"
                )
    elif any(seen[key].defined is not None for key in semantic):
        limits.append(
            catalog_error
            or "the semantic catalog could not be read, so semantic references "
            "in this plan were not resolved to the models behind them"
        )

    candidates = sorted(
        key
        for key, dependency in seen.items()
        if dependency.defined is None
        and key[0] in _CONCLUSIVE_KINDS
        and key in unqualified
        and key not in in_catalog
    )
    unread = _unread_definitions(index, project, written) if candidates else []
    if unread:
        named = ", ".join(f"{kind} '{name}'" for kind, name in candidates)
        limits.append(
            f"nothing dex read defines {named}, but {'; '.join(unread)}, so dex "
            "does not report " + ("it" if len(candidates) == 1 else "them") + " missing"
        )
    else:
        for key in candidates:
            seen[key].defined = "missing"
    missing = [
        MissingReference(kind=kind, name=name, used_in=list(seen[(kind, name)].used_in))
        for kind, name in candidates
        if seen[(kind, name)].defined == "missing"
    ]

    dependencies = [seen[key] for key in sorted(seen)]

    ambiguous: list[AmbiguousReference] = []
    for dependency in dependencies:
        if dependency.kind not in {"model", "seed", "snapshot", "macro"}:
            continue
        definitions = index.definitions_of(dependency.name, dependency.kind)
        if len(definitions) > 1:
            ambiguous.append(
                AmbiguousReference(
                    name=dependency.name,
                    kind=dependency.kind,
                    candidates=sorted(
                        f"{d.package or 'this project'}:{d.path}" for d in definitions
                    ),
                )
            )

    unresolved = [
        UnresolvedReference(
            path=reference.path,
            line=reference.line,
            form=reference.form,
            kind=reference.kind,
            reason=reference.note or "dex could not resolve this reference statically",
        )
        for reference in indeterminate
    ]

    if unresolved:
        completeness = "partial" if dependencies else "unresolved"
    elif limits:
        completeness = "partial"
    else:
        completeness = "complete"

    return Grounding(
        completeness=completeness,
        dependencies=dependencies,
        relations=sorted({d.relation for d in dependencies if d.relation}),
        unresolved=unresolved,
        ambiguous=ambiguous,
        missing=missing,
        limits=limits,
        freshness=manifest_freshness(after),
        binding=GroundingBinding(
            plan_digest=plan_digest,
            source_digest=source_digest(view),
            config_digest=config_digest(config),
            packages_digest=packages_digest(project),
        ),
    )
