"""Cube's own drift fingerprint: the maintain snapshot channel.

Builds the neutral `SemanticLayerSnapshot` from the validated set, reusing the
catalog's resolution (:class:`.catalog.Resolution`) so whether a field links to
a physical column is decided once, whichever channel asks.

- Each cube is one semantic model: its dimensions and aggregating measures, each
  mapped to the column behind it or None, and its primary key.
- Each view is one semantic model with no relation, its members mapped to
  nothing: a view's columns are its cubes', and a dropped column is reported
  against the cube that reads it.
- Each measure, of a cube or of a view, is one metric named the way the catalog
  names it (``cube.member``), so a removed or rewritten one is its own finding.
- Each join is one relationship with every ordered pair and its arity, oriented
  child to parent.

**What the content hash covers.** The element as written, keys normalized to
snake_case, so respelling ``sqlTable`` as ``sql_table`` is not a change and
``meta`` is excluded: it is free-form and carries nothing a definition means.

**Compiled-only definitions** (a ``.js`` model, a Jinja-templated file, a cube
or view with ``extends``) are only Cube's compiler's to read, so nothing
structural is fingerprinted out of them. Each is recorded by its content instead
(:class:`..maintain.snapshot.OpaqueDef`): a whole file by its bytes, an
``extends`` element by its own parsed body, so editing a sibling cube in the
same file does not mark it changed. A change is then reported as a change
(``compiled_only_changed``), and the fingerprint's notes say on every read that
these were compared by content only, so a layer holding one never reads clean.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from ..maintain.snapshot import (
    MetricDef,
    OpaqueDef,
    RelationshipDef,
    SemanticLayerSnapshot,
    SemanticModelDef,
)
from . import catalog as catalog_mod
from .model import Cube, View
from .validator import canonical_key

if TYPE_CHECKING:
    from .loader import LoadResult

__all__ = ["semantic_layer"]


def semantic_layer(
    loaded: LoadResult,
    resolved: catalog_mod.Resolution,
    repo_root: Path | str | None = None,
) -> SemanticLayerSnapshot:
    """Fingerprint the semantic layer from the validated set.

    ``repo_root`` is where the compiled-only files are read from to hash them;
    without one they are named in the notes and not recorded.
    """

    notes = loaded.notes()
    opaque = _opaque(loaded, repo_root) if repo_root is not None else []
    if loaded.compiled_only:
        names = ", ".join(o.name for o in opaque) or ", ".join(
            c.file for c in loaded.compiled_only
        )
        notes.append(
            f"compiled-only Cube definitions ({names}) are compared by their "
            "content alone, never structurally: a change inside one is reported "
            "as changed, and what changed is not read"
        )
    if loaded.empty:
        return SemanticLayerSnapshot(
            **_provenance(),
            opaque_definitions=opaque,
            opaque_captured=repo_root is not None,
            notes=notes,
        )

    relations = {
        cube.name: relation
        for cube in loaded.cubes
        if (relation := resolved.relation(cube.name))
    }
    models: list[SemanticModelDef] = []
    metrics: list[MetricDef] = []
    relationships: list[RelationshipDef] = []
    for cube in loaded.cubes:
        if cube.dimensions or cube.measures or cube.joins:
            models.append(_cube_def(cube, resolved))
        metrics.extend(
            MetricDef(
                name=catalog_mod.member_token(cube.name, measure.name),
                path=cube.file,
                content_sha256=_hash(measure.raw),
            )
            for measure in cube.measures
        )
        for join in cube.joins:
            declared, note = catalog_mod.declared_relationship(
                cube, join, resolved, relations
            )
            if note:
                notes.append(note)
            if declared is not None:
                relationships.append(_relationship_def(cube, join, declared))
    for view in loaded.views:
        models.append(_view_def(view))
        metrics.extend(
            MetricDef(
                name=catalog_mod.member_token(view.name, exposed.name),
                path=view.file,
                content_sha256=_hash(
                    {"source": exposed.source, "definition": exposed.member.raw}
                ),
            )
            for exposed in view.members_of("measure")
        )
    return SemanticLayerSnapshot(
        semantic_models=models,
        metrics=metrics,
        relationships=relationships,
        relationships_and_keys_captured=True,
        relationship_cardinality_captured=True,
        opaque_definitions=opaque,
        opaque_captured=repo_root is not None,
        **_provenance(),
        notes=notes,
    )


_ELEMENT = re.compile(r"^(cubes|views)\['(.*)'\]$")


def _opaque(loaded: LoadResult, repo_root: Path | str) -> list[OpaqueDef]:
    """Every compiled-only definition, hashed by its content."""

    found: list[OpaqueDef] = []
    for item in loaded.compiled_only:
        try:
            raw = (Path(repo_root) / item.file).read_bytes()
        except OSError:
            continue
        name = item.file
        digest = hashlib.sha256(raw).hexdigest()
        match = _ELEMENT.match(item.element or "")
        if match:
            name = f"{item.file}: {item.element}"
            digest = _hash(_element(raw, match.group(1), match.group(2)))
        found.append(
            OpaqueDef(
                name=name, path=item.file, reason=item.reason, content_sha256=digest
            )
        )
    return found


def _element(raw: bytes, section: str, name: str) -> Any:
    """One element's parsed body out of a file, or the whole file's bytes as
    text where it cannot be found, which still detects a change."""

    try:
        body = yaml.safe_load(raw)
    except yaml.YAMLError:
        return raw.decode("utf-8", "replace")
    listed = body.get(section) if isinstance(body, dict) else None
    for entry in listed if isinstance(listed, list) else []:
        if isinstance(entry, dict) and str(entry.get("name")) == name:
            return entry
    return raw.decode("utf-8", "replace")


def _provenance() -> dict[str, Any]:
    """Which reader produced the hashes. Cube versions no model format, so the
    validator release is the pin."""

    from .dependency import CUBE_VALIDATOR_RELEASE

    return {"vendor": "cube", "vendor_release": CUBE_VALIDATOR_RELEASE}


def _cube_def(cube: Cube, resolved: catalog_mod.Resolution) -> SemanticModelDef:
    relation = resolved.relation(cube.name)
    dimensions: dict[str, str | None] = {}
    categorical: dict[str, str] = {}
    for member in cube.dimensions:
        column = resolved.column(cube.name, member.name)
        dimensions[member.name] = column
        if column and member.type != "time":
            categorical[member.name] = column
    measures = {
        member.name: resolved.column(cube.name, member.name)
        for member in cube.measures
        if catalog_mod._aggregating(member)
    }
    key = catalog_mod.declared_key_columns(cube, resolved)
    return SemanticModelDef(
        name=cube.name,
        path=cube.file,
        content_sha256=_hash(cube.raw),
        relation=relation,
        dimensions=dimensions,
        categorical_dimensions=categorical,
        measures=measures,
        keys=[key] if key else [],
    )


def _view_def(view: View) -> SemanticModelDef:
    return SemanticModelDef(
        name=view.name,
        path=view.file,
        content_sha256=_hash(view.raw),
        dimensions={m.name: None for m in view.members_of("dimension")},
    )


def _relationship_def(cube: Cube, join: Any, declared: Any) -> RelationshipDef:
    """One join, named by its endpoints and pairs, the identity drift diffs by."""

    name = declared.name or (
        f"{declared.model}->{declared.to_model}:"
        + ",".join(f"{frm}={to}" for frm, to in declared.column_pairs)
    )
    return RelationshipDef(
        name=name,
        path=cube.file,
        content_sha256=_hash({"declared_on": cube.name, **join.raw}),
        model=declared.model,
        to_model=declared.to_model,
        column_pairs=declared.column_pairs,
        # `declared_relationship` already turned a one_to_many join round so the
        # row reads child to parent; the arity turns with it.
        cardinality=(
            "many_to_one" if join.relationship == "one_to_many" else join.relationship
        ),
    )


def _hash(definition: Any) -> str:
    def strip(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                canonical_key(str(k)): strip(v)
                for k, v in value.items()
                if canonical_key(str(k)) != "meta"
            }
        if isinstance(value, list):
            return [strip(v) for v in value]
        return value

    text = json.dumps(strip(definition), sort_keys=True, default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
