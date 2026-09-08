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
one thing could have meant, then the dependencies themselves.

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
ORIGINS = ("manifest", "semantic_manifest", "source_scan", "cache")

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

    ``resolved_to`` is what the name turned out to name, where that differs from
    the name itself: a measure resolves to its semantic model, a semantic model
    to the transformation model it sits on. ``relation`` is the physical address
    where dex could reach one, and it is absent rather than guessed: an ephemeral
    model compiles to no relation at all, and inventing one would send a caller
    looking for a table that does not exist.
    """

    kind: str
    name: str
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

    completeness: str = "unresolved"
    dependencies: list[GroundedDependency] = Field(default_factory=list)
    relations: list[str] = Field(default_factory=list)
    unresolved: list[UnresolvedReference] = Field(default_factory=list)
    ambiguous: list[AmbiguousReference] = Field(default_factory=list)
    limits: list[str] = Field(default_factory=list)
    freshness: dict[str, Any] = Field(default_factory=dict)
    binding: GroundingBinding = Field(default_factory=GroundingBinding)

    def data(self) -> dict[str, Any]:
        # The verdict leads. A long dependency list is the ordinary case, so it
        # will be read from the top and sometimes cut from the bottom, and the
        # honesty is what must not be what gets lost.
        return {
            "completeness": self.completeness,
            "limits": self.limits,
            "unresolved": [u.model_dump(mode="json") for u in self.unresolved],
            "ambiguous": [a.model_dump(mode="json") for a in self.ambiguous],
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
    installed = project / "dbt_packages"
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
) -> tuple[list[GroundedDependency], list[str]]:
    """Resolve semantic names to the models and relations behind them.

    A metric names its input measures, a measure names its semantic model, and a
    semantic model names both the transformation model it sits on and the relation
    that model builds. Following that chain is what answers "which table is behind
    this metric", and it is a name join through the compiled artifacts rather than
    a second resolver.
    """

    dependencies: list[GroundedDependency] = []
    unknown: list[str] = []
    models = {m.name: m for m in getattr(catalog, "semantic_models", [])}
    measures = {m.name: m for m in getattr(catalog, "measures", [])}
    metrics = {m.name: m for m in getattr(catalog, "metrics", [])}

    def add_model(model_name: str, via: str) -> None:
        info = models.get(model_name)
        if info is None:
            unknown.append(f"semantic model '{model_name}' is not in the catalog")
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
            unknown.append(f"metric '{name}' is not in the semantic catalog")
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
            unknown.append(f"measure '{name}' is not in the semantic catalog")
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
    return dependencies, unknown


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
    """

    from ..dbt_project import SourceFile, manifest_freshness
    from ..edits import content_hash
    from ..references import ReferenceIndex

    edited = {edit.path for edit in edits}
    deleted = {edit.path for edit in edits if edit.op is EditOp.DELETE}
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
    resolved, indeterminate = index.references_from(edited - deleted)

    relations = _manifest_relations(after.manifest)
    seen: dict[tuple[str, str], GroundedDependency] = {}
    semantic_names: dict[str, set[str]] = {}
    for reference in resolved:
        if reference.name is None:
            continue
        if reference.kind in {"metric", "measure", "entity", "dimension"}:
            semantic_names.setdefault(reference.kind, set()).add(reference.name)
        key = (reference.kind, reference.name)
        existing = seen.get(key)
        if existing is None:
            relation = relations.get(key)
            seen[key] = GroundedDependency(
                kind=reference.kind,
                name=reference.name,
                relation=relation,
                origin="manifest" if relation else "source_scan",
                package=reference.package,
                used_in=[reference.path],
            )
        elif reference.path not in existing.used_in:
            existing.used_in.append(reference.path)

    dependencies = [seen[key] for key in sorted(seen)]

    if catalog is not None:
        semantic, unknown = _semantic_dependencies(catalog, semantic_names)
        dependencies.extend(semantic)
        limits.extend(unknown)
    elif semantic_names:
        limits.append(
            catalog_error
            or "the semantic catalog could not be read, so semantic references "
            "in this plan were not resolved to the models behind them"
        )

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
        limits=limits,
        freshness=manifest_freshness(after),
        binding=GroundingBinding(
            plan_digest=plan_digest,
            source_digest=source_digest(view),
            config_digest=config_digest(config),
            packages_digest=packages_digest(project),
        ),
    )
