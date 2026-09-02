"""House conventions read out of a project's own models, checked at plan time.

Every other plan-time check measures an authored edit against something written
down: a declared column list, a path family, a ``ref()`` that has to resolve.
This one measures it against a convention nobody wrote down, inferred from what
the model's own siblings do, and that difference sets the whole design.

A convention dex infers wrongly is worse than no check at all, because the
caller has no declaration to point at when disagreeing. So the bar for speaking
is deliberately high: the precedent has to be several siblings deep, unanimous,
and the fix has to already be available in the project. Where any of that is
missing this says nothing rather than hedging, and it never refuses a plan. Each
check here is separately disableable, because a style judgment the house
disagrees with should cost one line of config to switch off.

This module is imported lazily from :mod:`.plans`, so reaching into
``explore.relationships`` for the foreign-key naming vocabulary costs nothing on
a command that never plans.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

from ..dbt_project import DbtProjectView, node_name, path_family
from ..explore.relationships import _LAYER_PREFIX, entity_of, fk_stem, is_id_shaped
from .plans import EditKind, EditOp, select_columns

if TYPE_CHECKING:
    from pathlib import Path

    from .plans import PlanEdit

# How many siblings have to resolve a key before their agreement reads as a
# convention rather than as three authors happening to do the same thing.
# "Several", in the terms of the request, and the same threshold
# ``explore.relationships`` already uses to call a repeated name a convention.
MIN_PRECEDENT = 3

# How many precedents, columns and parents the warning names before it stops
# listing. The point is to make the convention checkable in one read, not to
# enumerate it.
_MAX_NAMED = 3


def unresolved_key_warnings(
    edits: list[PlanEdit], view: DbtProjectView, project: Path
) -> list[str]:
    """Warn when an authored model passes a raw foreign key through into a
    folder whose siblings all resolve theirs to a descriptive attribute.

    A dimension exposing ``supplier_id`` where every sibling dimension exposes
    ``supplier_name`` is a convention violation nothing catches until review,
    and plan time is the moment it is cheapest to act on.

    Four things have to hold together before this speaks, and every one of them
    exists to keep it quiet rather than to catch more:

    - **The authored SELECT list resolves statically.** A ``select *`` produces
      silence, not a hedge. The declared-column comparison reports its own skip
      because a declaration exists and went unchecked; here nothing was
      promised, so there is nothing to report.
    - **The siblings agree, unanimously.** Siblings are the models sharing this
      one's folder *and* its layer prefix (``dim_``, ``fct_``, ...), widening to
      that prefix project-wide when the folder is too small to hold a
      precedent. At least :data:`MIN_PRECEDENT` of them resolve a key of the
      same id-suffix shape and **none** passes one through. A single
      counter-example ends it, which is what keeps a folder mixing facts and
      dimensions silent: a fact table carries raw keys legitimately, and its
      presence is the counter-example.
    - **The key is not the model's own.** ``dim_suppliers.supplier_id`` is that
      model's identity, not a foreign key it declined to resolve.
    - **The project holds a parent to resolve against**: a model named for the
      same entity that produces something other than keys. Read from the
      project's own models rather than from the exploration cache, because the
      fix is a ``ref()``, and a parent dex cannot name is a warning the caller
      cannot act on.

    Only models this plan authors are judged. One already in the project that
    breaks the convention is not this plan's doing and not this plan's warning.
    """

    authored = _authored_models(edits, view, project)
    if not authored:
        return []

    # The project as it will stand once this plan applies: current files, this
    # plan's upserts overlaid, its deletes removed. The declared-column
    # comparison overlays for the same reason. Two dimensions authored together
    # are each other's precedent, and a sibling this plan removes is none.
    universe = _model_universe(edits, view, project)
    columns = {path: select_columns(content) for path, content in universe.items()}
    parents = _parent_candidates(universe, columns)

    warnings: list[str] = []
    for path in sorted(authored):
        produced = columns.get(path)
        if not produced:
            continue
        model = node_name(path)
        siblings = _siblings(path, universe)

        unresolved: list[_Key] = []
        resolvable: list[str] = []
        precedent: set[str] = set()
        for key in _foreign_keys(produced, model=model):
            if _resolved(key, produced):
                continue
            parent = _parent_of(key, parents)
            if parent is None or parent == path:
                continue
            resolvers, dissent = _precedent(key.suffix, siblings, columns)
            if dissent or len(resolvers) < MIN_PRECEDENT:
                continue
            unresolved.append(key)
            resolvable.append(parent)
            precedent.update(node_name(p) for p in resolvers)
        if not unresolved:
            continue

        warnings.append(
            f"{path}: {model} exposes {_listed(k.column for k in unresolved)} "
            "with no resolved counterpart, while every readable sibling under "
            f"{PurePosixPath(path).parent} resolves keys of this shape "
            f"({_listed(sorted(precedent))}) and the project has "
            f"{_listed(node_name(p) for p in resolvable)} to "
            "resolve against. Set conventions.resolved_keys: false in "
            ".dex/config.yml to turn this check off"
        )
    return warnings


@dataclass(frozen=True)
class _Key:
    """One id-shaped column, split the way the convention reads it.

    ``suffix`` is carried separately because the convention is read per shape: a
    house that resolves every ``*_key`` has said nothing about how it treats a
    ``*_id``, and reading one as precedent for the other is exactly the
    confident guess this module exists not to make.
    """

    column: str
    stem: str
    suffix: str


def _foreign_keys(produced: set[str], *, model: str) -> list[_Key]:
    """The id-shaped columns of one model that name some *other* entity.

    A key naming the model's own entity is its identity rather than a foreign
    key, and a bare ``id``/``key`` names no entity at all, so neither has a stem.

    Ownership is a prefix test rather than an equality one, which reads
    ``dim_suppliers_eu.supplier_id`` and ``dim_customers.cust_id`` as the
    models' own keys. It is a loose test and loose in one direction only: it
    can silence a key that really was foreign (``dim_order_lines.order_id``),
    never speak about one that was the model's own. Silence is the side to err
    on for a style judgment, and it is the reverse test that would be
    dangerous, since ``order_status_id`` in ``dim_orders`` is exactly the kind
    of key worth resolving.

    It is asked of both spellings of the model's name for the same reason. The
    shared singularizer is a heuristic, and on a word like ``enterprises`` it
    lands on ``enterpri``, which no longer prefixes the ``enterprise`` its own
    key is named for. Comparing the plural too costs nothing and is what keeps
    ``dim_enterprises.enterprise_id`` from being read as a key the model
    declined to resolve.
    """

    keys: list[_Key] = []
    for column in sorted(produced):
        stem = fk_stem(column)
        if stem is None:
            continue
        suffix = column[len(stem) :].lstrip("_").lower()
        if suffix and not owns(model, stem):
            keys.append(_Key(column, stem, suffix))
    return keys


def owns(model: str, stem: str) -> bool:
    """Whether a model is the one a key named for ``stem`` belongs to.

    One predicate, asked twice: of the authored model, to tell its own key from
    a foreign one, and of every other model, to find the parent a foreign key
    could resolve against. They have to be the same question, or a key can be
    foreign to its own model and still find no parent anywhere.

    Both sides are read as the layer-stripped word *and* as its singular,
    because the shared singularizer is a heuristic that disagrees with itself
    across a plural boundary: it takes ``enterprises`` to ``enterpris`` and
    leaves ``enterprise`` alone, so an equality test on singulars misses the
    pair entirely.
    """

    return any(
        name.startswith(named) for name in _names_of(model) for named in _names_of(stem)
    )


def _names_of(name: str) -> tuple[str, str]:
    """A name as the layer-stripped word it is and as its singular."""

    return _LAYER_PREFIX.sub("", name).lower(), entity_of(name)


def _resolved(key: _Key, produced: set[str]) -> bool:
    """Whether the model also carries a descriptive attribute for this key:
    ``supplier_id`` beside ``supplier_name``, ``supplier_company``, or a bare
    ``supplier``. The counterpart has to be something other than another key,
    since ``supplier_id`` beside ``supplier_key`` resolves nothing."""

    stem = key.stem.lower()
    return any(
        (column == stem or column.startswith(f"{stem}_")) and not is_id_shaped(column)
        for column in produced
    )


def _parent_of(key: _Key, candidates: list[str]) -> str | None:
    """The model a raw key could resolve against, or ``None``."""

    return next((p for p in candidates if owns(node_name(p), key.stem)), None)


def _precedent(
    suffix: str, siblings: list[str], columns: dict[str, set[str] | None]
) -> tuple[list[str], bool]:
    """The siblings resolving a key of this shape, and whether any breaks ranks.

    A sibling whose SELECT list does not resolve statically is neither: it is
    dropped rather than assumed to comply, so a folder of ``select *`` models
    simply never reaches the threshold.
    """

    resolvers: list[str] = []
    dissent = False
    for path in siblings:
        produced = columns.get(path)
        if not produced:
            continue
        keys = [
            k
            for k in _foreign_keys(produced, model=node_name(path))
            if k.suffix == suffix
        ]
        if any(not _resolved(key, produced) for key in keys):
            dissent = True
        elif keys:
            resolvers.append(path)
    return resolvers, dissent


def _siblings(path: str, universe: dict[str, str]) -> list[str]:
    """The models whose precedent governs this one.

    The folder first, since a project that separates its dimensions has already
    said where its convention lives. A folder too small to hold a precedent
    widens to the layer prefix project-wide, which is the same convention read
    at the layer instead: a shop with one flat ``models/`` directory still has a
    ``dim_`` layer, whether or not it has a ``dim/`` folder.
    """

    folder = PurePosixPath(path).parent
    layer = _layer(path)
    peers = [other for other in universe if other != path and _layer(other) == layer]
    in_folder = [p for p in peers if PurePosixPath(p).parent == folder]
    return in_folder if len(in_folder) >= MIN_PRECEDENT else peers


def _layer(path: str) -> str:
    """The warehouse-layer prefix a model file is named with, or ``""``.
    Compared rather than interpreted: what matters is that two models were named
    with the same one, not which one it is."""

    match = _LAYER_PREFIX.match(node_name(path))
    return match.group(0).lower() if match else ""


def _parent_candidates(
    universe: dict[str, str], columns: dict[str, set[str] | None]
) -> list[str]:
    """The ``ref()``-able models that could resolve a key named for them.

    A model of nothing but ids has no attribute to resolve to and is not a
    parent. Ordered shortest path first, so where several models could answer
    for one entity a project's canonical dimension wins over a variant beside
    it, and the answer is the same on every run.
    """

    return [
        path
        for path in sorted(universe, key=lambda p: (len(p), p))
        if (produced := columns.get(path))
        and not all(is_id_shaped(column) for column in produced)
    ]


def _authored_models(
    edits: list[PlanEdit], view: DbtProjectView, project: Path
) -> set[str]:
    return {
        edit.path
        for edit in edits
        if edit.kind is EditKind.MODEL_SQL
        and edit.op is EditOp.UPSERT
        and edit.new_content is not None
        and _is_model_sql(edit.path, view, project)
    }


def _model_universe(
    edits: list[PlanEdit], view: DbtProjectView, project: Path
) -> dict[str, str]:
    universe = {
        path: source.content
        for path, source in view.files.items()
        if _is_model_sql(path, view, project)
    }
    for edit in edits:
        if edit.kind is not EditKind.MODEL_SQL or not _is_model_sql(
            edit.path, view, project
        ):
            continue
        if edit.op is EditOp.DELETE:
            universe.pop(edit.path, None)
        elif edit.new_content is not None:
            universe[edit.path] = edit.new_content
    return universe


def _is_model_sql(path: str, view: DbtProjectView, project: Path) -> bool:
    return path.endswith(".sql") and path_family(project, path, view) == "model"


def _listed(names: object) -> str:
    unique = sorted(dict.fromkeys(names))  # type: ignore[arg-type]
    shown = ", ".join(unique[:_MAX_NAMED])
    extra = len(unique) - _MAX_NAMED
    return f"{shown} and {extra} more" if extra > 0 else shown
