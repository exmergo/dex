"""Which entity a model is about, drafted when nothing declares it (#1228).

A ``population.parent`` (#857) names the model whose rows decide which rows a
model holds, and every population check is built on it: the plan-time driver
check (#1029), the parent-population count on the built relation (#857, #1102),
and its package form (#1155). Few models declare one. An aggregate over a child
table is the shape that goes wrong most often (orphan child keys kept, parents
with no children dropped or kept as NULL), and it is exactly the model nobody
declared a parent for.

This module drafts one. The model's key is its grain: the declared grain, else a
uniqueness test on the model, else the grain the SQL proves (a ``GROUP BY``
settles it with no cache). Candidate parents are ranked by evidence, strongest
first:

1. ``unique_key``: a model, seed or snapshot whose tested, declared or profiled
   unique key is the model's grain, column for column;
2. ``relationship``: a ``relationships`` test or declared ``references`` on a
   grain column, on this model or on the column the grain is carried from, or
   a verified join with no orphans out of that column;
3. ``description``: the noun after "one row per" in the model's description
   names the candidate, which carries the grain columns;
4. ``driving``: the relation that drives the SQL, when the grain is carried from
   it unchanged.

A candidate is a node ``ref()`` reaches: a source is the raw table under a
staging model, not an entity a mart is drafted against (#1155). The model
itself, anything downstream of it, and a time spine or a model generated in SQL
(#988), which supply anchors rather than entities, never qualify.

The answer is a *draft*. It is shown to the author with its evidence, and
measured by ``transform build --verify``, but never written into the project and
never treated as intent: a declared parent always wins, and a draft that turns
out wrong only asks a question. Read from the repository and the cache alone; it
opens no connection.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..cache import DexCache
    from ..project_definitions import ProjectDefinitions
    from ..references import ReferenceIndex
    from .drafting import _Reading

#: Evidence tiers, strongest first.
TIERS = ("unique_key", "relationship", "description", "driving")

#: How many runners-up a draft lists beside its winner.
MAX_ALTERNATIVES = 3

#: The pre-1.9 name dbt required of the time spine (#988).
_LEGACY_SPINE = "metricflow_time_spine"

_ROW_PER = re.compile(
    r"\b(?:one|a single|1|each)\s+row\s+(?:per|for each|for every)\s+"
    r"(?:distinct\s+|unique\s+)?([A-Za-z][A-Za-z0-9_ -]*)",
    re.IGNORECASE,
)
_STOP = re.compile(
    r"\s+(?:and|with|in|from|that|which|who|where|per|of|on|by|for|across|over)\b.*",
    re.IGNORECASE,
)


@dataclass
class _Candidate:
    model: str
    key: dict[str, str]
    tiers: dict[str, str] = field(default_factory=dict)

    def best(self) -> int:
        return min(TIERS.index(tier) for tier in self.tiers)


@dataclass
class Draft:
    """A drafted parent, or why none was drafted.

    ``model`` is ``None`` where no candidate qualified; ``withheld`` then says
    why. ``key`` maps the model's output column to the parent's column.
    """

    model: str | None = None
    key: dict[str, str] = field(default_factory=dict)
    evidence: str | None = None
    supported_by: list[str] = field(default_factory=list)
    detail: str | None = None
    alternatives: list[dict[str, Any]] = field(default_factory=list)
    driving: list[str] = field(default_factory=list)
    drives_from_parent: bool | None = None
    withheld: str | None = None

    def as_dict(self) -> dict[str, Any]:
        if self.model is None:
            return {"value": None, "withheld": self.withheld}
        out: dict[str, Any] = {
            "value": {"model": self.model, "key": dict(self.key)},
            "evidence": self.evidence,
            "supported_by": list(self.supported_by),
            "detail": self.detail,
            "drafted": True,
        }
        if self.alternatives:
            out["alternatives"] = list(self.alternatives)
        if self.driving:
            out["driving_relation"] = ", ".join(self.driving)
        if self.drives_from_parent is not None:
            out["drives_from_parent"] = self.drives_from_parent
        return out


@dataclass
class KeyFacts:
    """What the project states about keys, read once per draft run.

    ``unique`` maps a lowered node name to the column sets declared or tested
    unique on it; ``foreign`` maps ``(lowered model, lowered column)`` to the
    ``(parent, parent column)`` pairs a ``relationships`` test states.
    """

    unique: dict[str, list[tuple[frozenset[str], str]]] = field(default_factory=dict)
    foreign: dict[tuple[str, str], list[tuple[str, str]]] = field(default_factory=dict)
    spines: set[str] = field(default_factory=set)

    def add_unique(self, model: str, columns: Iterable[str], source: str) -> None:
        key = frozenset(c.lower() for c in columns if c)
        if not key:
            return
        entries = self.unique.setdefault(model.lower(), [])
        if all(existing != key for existing, _ in entries):
            entries.append((key, source))

    def known_keys(self) -> dict[str, list[tuple[list[str], str]]]:
        """Each node's stated unique keys with their sources, in the shape
        :func:`.drafting.read` takes, so a grain carried from a tested or
        declared key is read without a cache."""

        return {
            name: [(sorted(key), source) for key, source in keys]
            for name, keys in self.unique.items()
        }

    def add_foreign(self, model: str, column: str, parent: str, field_: str) -> None:
        pairs = self.foreign.setdefault((model.lower(), column.lower()), [])
        if (parent, field_) not in pairs:
            pairs.append((parent, field_))


def key_facts(*definitions: ProjectDefinitions | None) -> KeyFacts:
    """The keys and joins every ``definitions`` states, merged.

    A plan passes two: the project's YAML as the plan leaves it, and the
    compiled manifest (which alone reads an installed package's tests).
    """

    facts = KeyFacts()
    for defs in definitions:
        if defs is None:
            continue
        for model, grain in defs.declared_grains.items():
            facts.add_unique(_base(model), grain, "its declared grain")
        for composite in defs.declared_composite_keys:
            if composite.claim in ("primary", "combination"):
                facts.add_unique(
                    _base(composite.model),
                    composite.columns,
                    "a unique_combination_of_columns test"
                    if composite.claim == "combination"
                    else "a primary key",
                )
        for key in defs.declared_keys:
            if key.unique or key.primary:
                facts.add_unique(
                    _base(key.model),
                    [key.column],
                    "a primary key" if key.primary else "a unique test",
                )
        for model, column in defs.primary_entities.items():
            facts.add_unique(_base(model), [column], "its primary entity")
        for fk in defs.foreign_keys:
            facts.add_foreign(
                _base(fk.model), fk.column, _base(fk.to_model), fk.to_column
            )
    return facts


def facts_for(view: Any, manifest: Mapping[str, Any] | None = None) -> KeyFacts:
    """The key facts of a project view: its YAML as it reads (a plan passes the
    view its edits leave), and the compiled manifest where one is, which alone
    holds an installed package's tests. Never raises."""

    from ..dbt_project import _declared_from_manifest, _declared_from_yaml
    from ..project_definitions import ProjectDefinitions

    readings: list[ProjectDefinitions] = []
    try:
        from_yaml = ProjectDefinitions(present=True)
        _declared_from_yaml(view, from_yaml)
        readings.append(from_yaml)
    except Exception:  # noqa: S110 - a key fact is evidence, never a failure
        pass
    nodes = (manifest or {}).get("nodes")
    if isinstance(nodes, dict) and nodes:
        try:
            compiled = ProjectDefinitions(present=True)
            _declared_from_manifest(dict(manifest or {}), compiled)
            readings.append(compiled)
        except Exception:  # noqa: S110
            pass
    facts = key_facts(*readings)
    files = {path: source.content for path, source in view.files.items()}
    facts.spines = spine_names(files)
    return facts


def spine_names(files: Mapping[str, str]) -> set[str]:
    """Models a ``time_spine:`` config marks, read from the YAML, plus the
    name dbt required of the spine before that config existed (#988)."""

    import yaml

    found = {_LEGACY_SPINE}
    for path, content in files.items():
        if not path.endswith((".yml", ".yaml")) or "time_spine" not in content:
            continue
        try:
            parsed = yaml.safe_load(content)
        except yaml.YAMLError:
            continue
        if not isinstance(parsed, dict):
            continue
        for entry in parsed.get("models") or []:
            if (
                isinstance(entry, dict)
                and isinstance(entry.get("name"), str)
                and entry.get("time_spine")
            ):
                found.add(entry["name"].lower())
    return found


def _base(name: str) -> str:
    """A version label's model name (``dim_teams.v2`` is ``dim_teams``)."""

    head, _, tail = name.rpartition(".")
    return head if head and re.fullmatch(r"v\d+", tail) else name


def draft_parent(
    model: str,
    reading: _Reading,
    *,
    declared: Mapping[str, Any],
    files: Mapping[str, str],
    model_paths: list[str],
    cache: DexCache | None,
    facts: KeyFacts,
    index: ReferenceIndex | None,
    packaged: Callable[[str], str | None] | None = None,
    dialect: str | None = None,
    vars: Mapping[str, Any] | None = None,
) -> Draft | None:
    """The drafted parent of ``model``, or ``None`` where it declares one.

    ``reading`` is the model's SQL as :func:`.drafting.read` reads it, and
    ``declared`` its declaration as :func:`.drafting.read_declared` returns it.
    """

    population = declared.get("population")
    if isinstance(population, dict) and isinstance(population.get("parent"), dict):
        return None

    grain, grain_from = _grain(model, reading, declared, facts)
    if not grain:
        return Draft(
            withheld=(
                "the model's grain is not known (no declared grain, no uniqueness "
                "test on it, and its SQL proves none), so there is no key to "
                "measure a parent by"
            ),
            driving=list(reading.driving),
        )
    lowered = [g.lower() for g in grain]

    from .declarations import model_sql_path
    from .drafting import _dataset_for_model, _project_name, proven_keys

    downstream: set[str] = set()
    if index is not None:
        downstream = {n.lower() for n in index.descendants_of(model)}
    downstream.add(model.lower())

    def known(name: str) -> str | None:
        """``name`` spelled the way the project or a package defines it, or
        ``None`` where nothing ``ref()`` reaches defines it."""

        spelled = _project_name(files, model_paths, name)
        if model_sql_path(files, model_paths, spelled) is not None:
            return spelled
        if _seed_or_snapshot(files, model_paths, spelled):
            return spelled
        if index is not None and index.package_node_kind(spelled) is not None:
            return spelled
        if packaged is not None and packaged(spelled) is not None:
            return spelled
        return None

    candidates: dict[str, _Candidate] = {}

    def offer(name: str, key: dict[str, str], tier: str, why: str) -> None:
        spelled = known(name)
        if spelled is None or spelled.lower() in downstream:
            return
        if spelled.lower() in facts.spines:
            return
        found = candidates.setdefault(spelled.lower(), _Candidate(spelled, dict(key)))
        found.tiers.setdefault(tier, why)

    # 1. A node whose unique key is the grain, by column name.
    wanted = frozenset(lowered)
    for name, keys in facts.unique.items():
        for key, source in keys:
            covered = _covered(key, wanted, name, reading)
            if covered is not None:
                offer(
                    name,
                    {g: g for g in grain if g.lower() in key},
                    "unique_key",
                    f"{', '.join(sorted(key))} is unique on {name} ({source})"
                    + covered,
                )
    if cache is not None:
        for name in _node_names(files, model_paths, facts, index):
            if name.lower() in candidates or name.lower() in downstream:
                continue
            dataset = _dataset_for_model(cache, name, files, model_paths)
            if dataset is None:
                continue
            for key in proven_keys(dataset):
                if frozenset(c.lower() for c in key) == wanted:
                    offer(
                        name,
                        {
                            g: next(c for c in key if c.lower() == g.lower())
                            for g in grain
                        },
                        "unique_key",
                        f"{', '.join(grain)} is a proven unique key of "
                        f"{dataset.identifier} in the exploration cache",
                    )
                    break

    # 2. A relationships test, declared reference or clean verified join.
    for parent, key, why in _related(
        model, grain, reading, declared, facts, cache, files, model_paths, packaged
    ):
        offer(parent, key, "relationship", why)

    # 3. The description's head noun.
    noun = head_noun(declared.get("description"))
    if noun:
        for name in _node_names(files, model_paths, facts, index):
            if not _names_noun(name, noun):
                continue
            if name.lower() in candidates:
                candidates[name.lower()].tiers.setdefault(
                    "description", f"the description says one row per {noun}"
                )
                continue
            columns = _columns_of(name, files, model_paths, cache)
            if columns is not None and wanted <= columns:
                offer(
                    name,
                    {g: g for g in grain},
                    "description",
                    f"the description says one row per {noun}, and {name} carries "
                    f"{', '.join(grain)}",
                )

    # 4. The driving relation, when the grain is carried from it unchanged.
    grouped = (reading.grain_evidence or "").startswith("the model groups by")
    if len(reading.driving) == 1 and not grouped:
        driver = reading.driving[0]
        origins = [reading.origins.get(g) for g in lowered]
        if all(o is not None and o.relation == driver for o in origins):
            offer(
                driver,
                {g: o.column for g, o in zip(grain, origins, strict=True) if o},
                "driving",
                f"the model is driven from {driver} and carries "
                f"{', '.join(grain)} from it unchanged",
            )

    # A spine or a model generated in SQL supplies anchors, not entities.
    for name in list(candidates):
        if _generated(candidates[name].model, files, model_paths, dialect, vars):
            del candidates[name]

    if not candidates:
        return Draft(
            withheld=(
                f"no model, seed or snapshot outside this model's own lineage is "
                f"keyed by {', '.join(grain)} ({grain_from}), and its driving "
                "relation is not one ref() reaches"
            ),
            driving=list(reading.driving),
        )

    ancestors = (
        {n.lower() for n in index.ancestors_of(model)} if index is not None else set()
    )

    def upstream_of(candidate: _Candidate) -> int:
        """How many other candidates descend from this one."""
        if index is None:
            return 0
        below = {n.lower() for n in index.descendants_of(candidate.model)}
        return sum(
            1
            for other in candidates
            if other != candidate.model.lower() and other in below
        )

    ranked = sorted(
        candidates.values(),
        key=lambda c: (
            c.best(),
            -len(c.tiers),
            0 if c.model.lower() in ancestors else 1,
            -upstream_of(c),
            c.model.lower(),
        ),
    )
    winner = ranked[0]
    tiers = sorted(winner.tiers, key=TIERS.index)
    drivers = {d.lower() for d in reading.driving}
    drives = None
    if reading.driving:
        drives = winner.model.lower() in drivers or (
            index is not None
            and any(
                winner.model.lower() in {n.lower() for n in index.ancestors_of(d)}
                for d in reading.driving
            )
        )
    return Draft(
        model=winner.model,
        key=winner.key,
        evidence=tiers[0],
        supported_by=tiers,
        detail="; ".join(winner.tiers[t] for t in tiers) + f" (grain: {grain_from})",
        alternatives=[
            {
                "model": other.model,
                "evidence": sorted(other.tiers, key=TIERS.index)[0],
            }
            for other in ranked[1 : 1 + MAX_ALTERNATIVES]
        ],
        driving=list(reading.driving),
        drives_from_parent=drives,
    )


def _covered(
    key: frozenset[str], wanted: frozenset[str], name: str, reading: _Reading
) -> str | None:
    """Whether a node's unique ``key`` identifies the model's grain: equal to
    it, or a part of it where every other grain column is carried from that
    same node (``group by account_id, account_name``, the name read off the
    accounts). Returns the note to append to the evidence, or ``None``."""

    if key == wanted:
        return ""
    if not key or not key < wanted:
        return None
    extra = sorted(wanted - key)
    for column in extra:
        origin = reading.origins.get(column)
        if origin is None or origin.relation.rsplit(".", 1)[-1].lower() != name.lower():
            return None
    return f", and {', '.join(extra)} in the grain is carried from {name} itself"


def head_noun(description: Any) -> str | None:
    """The entity a description says one row stands for, lowered and in the
    singular: "One row per support account, with ..." is ``account``."""

    if not isinstance(description, str):
        return None
    match = _ROW_PER.search(description)
    if match is None:
        return None
    phrase = _STOP.sub("", match.group(1)).strip(" -_").lower()
    if not phrase:
        return None
    word = re.split(r"[\s_-]+", phrase)[-1]
    return _singular(word) or None


def _singular(word: str) -> str:
    if word.endswith("ies") and len(word) > 4:
        return word[:-3] + "y"
    if word.endswith(("ses", "xes", "zes", "ches", "shes")):
        return word[:-2]
    if word.endswith("s") and not word.endswith("ss") and len(word) > 3:
        return word[:-1]
    return word


def _names_noun(name: str, noun: str) -> bool:
    tokens = [t for t in re.split(r"[^a-z0-9]+", name.lower()) if t]
    return any(_singular(token) == noun for token in tokens)


def _grain(
    model: str,
    reading: _Reading,
    declared: Mapping[str, Any],
    facts: KeyFacts,
) -> tuple[list[str], str]:
    grain = declared.get("grain")
    if isinstance(grain, list) and grain and all(isinstance(g, str) for g in grain):
        return list(grain), "declared"
    tested = facts.unique.get(model.lower()) or []
    if tested:
        key, source = tested[0]
        produced = {p.lower(): p for p in reading.produced or []}
        return [produced.get(c, c) for c in sorted(key)], source
    if reading.grain:
        return list(reading.grain), reading.grain_evidence or "drafted from the SQL"
    return [], ""


def _related(
    model: str,
    grain: list[str],
    reading: _Reading,
    declared: Mapping[str, Any],
    facts: KeyFacts,
    cache: DexCache | None,
    files: Mapping[str, str],
    model_paths: list[str],
    packaged: Callable[[str], str | None] | None,
) -> list[tuple[str, dict[str, str], str]]:
    """``(parent, key, why)`` for each parent every grain column joins to."""

    from .drafting import _dataset_for_model, _verified_reference

    per_column: list[dict[str, tuple[str, str, str]]] = []
    stated_columns = declared.get("columns")
    if not isinstance(stated_columns, dict):
        stated_columns = {}
    for column in grain:
        found: dict[str, tuple[str, str, str]] = {}
        stated = stated_columns.get(column) or {}
        reference = stated.get("references") if isinstance(stated, dict) else None
        if isinstance(reference, str) and "." in reference:
            parent, _, field_ = reference.rpartition(".")
            _add(
                found,
                parent,
                field_,
                f"{model}.{column} declares references {reference}",
            )
        for parent, field_ in facts.foreign.get((model.lower(), column.lower()), []):
            _add(
                found,
                parent,
                field_,
                f"a relationships test on {model}.{column} names {parent}.{field_}",
            )
        origin = _origin(reading, column)
        if origin is not None:
            source = origin.relation.rsplit(".", 1)[-1]
            for parent, field_ in facts.foreign.get(
                (source.lower(), origin.column.lower()), []
            ):
                _add(
                    found,
                    parent,
                    field_,
                    f"{column} is carried from {source}.{origin.column}, whose "
                    f"relationships test names {parent}.{field_}",
                )
            dataset = (
                _dataset_for_model(cache, origin.relation, files, model_paths)
                if cache is not None
                else None
            )
            if cache is not None and dataset is not None:
                verified = _verified_reference(
                    cache,
                    dataset,
                    origin.column,
                    files,
                    model_paths,
                    model=model,
                    packaged=packaged,
                )
                if verified is not None and verified[0] is not None:
                    parent, _, field_ = verified[0].rpartition(".")
                    _add(
                        found,
                        parent,
                        field_,
                        f"{column} is carried from a column where {verified[1]}",
                    )
        per_column.append(found)

    if not per_column or not all(per_column):
        return []
    shared = set(per_column[0])
    for found in per_column[1:]:
        shared &= set(found)
    out = []
    for parent_key in sorted(shared):
        parent = per_column[0][parent_key][0]
        key = {
            column: found[parent_key][1]
            for column, found in zip(grain, per_column, strict=True)
        }
        why = "; ".join(found[parent_key][2] for found in per_column)
        out.append((parent, key, why))
    return out


def _origin(reading: _Reading, column: str) -> Any:
    """Where an output column is carried from: its traced lineage, or, where a
    ``*`` over an unprofiled relation hid it, the same name on the single
    relation that drives the model. The second is a reading by name, good
    enough to look up a stated join and never a claim on its own."""

    from .drafting import _Origin

    traced = reading.origins.get(column.lower())
    if traced is not None:
        return traced
    if len(reading.driving) == 1 and reading.produced is None:
        return _Origin(reading.driving[0], column)
    if len(reading.driving) == 1 and not reading.relations[1:]:
        return _Origin(reading.driving[0], column)
    return None


def _add(
    found: dict[str, tuple[str, str, str]], parent: str, field_: str, why: str
) -> None:
    found.setdefault(_base(parent).lower(), (_base(parent), field_, why))


def _node_names(
    files: Mapping[str, str],
    model_paths: list[str],
    facts: KeyFacts,
    index: ReferenceIndex | None,
) -> list[str]:
    """Every model, seed and snapshot name the project or its packages define,
    as far as the files and the index say."""

    roots = [PurePosixPath(p) for p in model_paths]
    names: dict[str, str] = {}
    for path in files:
        candidate = PurePosixPath(path)
        if candidate.suffix == ".csv" or (
            candidate.suffix == ".sql" and any(r in candidate.parents for r in roots)
        ):
            names.setdefault(candidate.stem.lower(), candidate.stem)
    for name in facts.unique:
        names.setdefault(name, name)
    if index is not None:
        for name in index.defined_nodes():
            names.setdefault(name.lower(), name)
    return sorted(names.values())


def _seed_or_snapshot(
    files: Mapping[str, str], model_paths: list[str], name: str
) -> bool:
    from .drafting import _is_seed_or_snapshot

    return _is_seed_or_snapshot(files, model_paths, name)


def _columns_of(
    name: str,
    files: Mapping[str, str],
    model_paths: list[str],
    cache: DexCache | None,
) -> frozenset[str] | None:
    from .drafting import _dataset_for_model

    if cache is None:
        return None
    dataset = _dataset_for_model(cache, name, files, model_paths)
    if dataset is None or not dataset.columns:
        return None
    return frozenset(c.name.lower() for c in dataset.columns)


def _generated(
    name: str,
    files: Mapping[str, str],
    model_paths: list[str],
    dialect: str | None,
    vars: Mapping[str, Any] | None,
) -> bool:
    """Whether a project model manufactures its rows (a date spine, a VALUES
    list) and names no warehouse relation, so it is no entity (#988)."""

    from .declarations import model_sql_path
    from .drafting import read

    path = model_sql_path(files, model_paths, name)
    if path is None or dialect is None:
        return False
    try:
        return read(files[path], None, dialect=dialect, vars=vars).generated_sql
    except Exception:
        return False


#: How many models one build drafts and measures a parent for. A build of a
#: whole project would otherwise price a statement per model; the plan's own
#: models come first, and the rest are counted in a note.
MAX_INFERRED = 25


@dataclass
class BuildDrafts:
    """The parents drafted for one build's models (#1228).

    ``parents`` maps a lowered model label to the rule the declared-fact sweep
    measures. ``withheld`` maps a model the build was for (``focus``) to why
    nothing was drafted for it, so it gets an ``unchecked`` record rather than
    silence; a model outside the focus with no draft is only counted.
    """

    parents: dict[str, Any] = field(default_factory=dict)
    withheld: dict[str, str] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


def draft_for_build(
    project_dir: Any,
    labels: Iterable[str],
    *,
    cache: DexCache | None,
    dialect: str,
    focus: Iterable[str] | None = None,
) -> BuildDrafts:
    """Draft a parent for each built model that declares none, reading the
    project tree as the build left it and the stored cache. Free: no statement
    runs here, and a model that cannot be read is withheld with the reason.
    """

    from ..dbt_project import load
    from ..maintain.declared_facts import SETTLING_TIERS, ParentRule
    from ..references import ReferenceIndex
    from .declarations import model_sql_path
    from .drafting import read, read_declared

    out = BuildDrafts()
    wanted = list(dict.fromkeys(labels))
    focused = {f.lower() for f in focus} if focus is not None else None
    if focused is not None:
        wanted = [label for label in wanted if label.lower() in focused]
    if not wanted:
        return out
    try:
        view = load(project_dir)
    except Exception as exc:
        if focused is None:
            return out
        out.notes.append(
            f"no parent was drafted for this build's models: the project could not "
            f"be read ({type(exc).__name__})"
        )
        return out
    files = {path: source.content for path, source in view.files.items()}
    facts = facts_for(view, view.manifest)
    built: list[ReferenceIndex] = []

    def index() -> ReferenceIndex | None:
        if not built:
            try:
                built.append(ReferenceIndex(view))
            except Exception:
                return None
        return built[0]

    elided = 0
    no_draft = 0
    for label in wanted:
        if len(out.parents) >= MAX_INFERRED:
            elided += 1
            continue
        path = model_sql_path(files, view.model_paths, label)
        if path is None or not path.endswith(".sql"):
            if focused is not None:
                out.withheld[label] = "its SQL file could not be found or is not SQL"
            continue
        declared = read_declared(files, label)
        try:
            reading = read(
                files[path],
                cache,
                dialect=dialect,
                vars=view.vars,
                files=files,
                model_paths=view.model_paths,
                known_keys=facts.known_keys(),
            )
            draft = draft_parent(
                label,
                reading,
                declared=declared,
                files=files,
                model_paths=view.model_paths,
                cache=cache,
                facts=facts,
                index=index(),
                dialect=dialect,
                vars=view.vars,
            )
        except Exception as exc:
            if focused is not None:
                out.withheld[label] = f"drafting failed ({type(exc).__name__})"
            continue
        if draft is None:
            continue  # it declares a parent, which the declared check measures
        if draft.model is None:
            no_draft += 1
            if focused is not None:
                out.withheld[label] = draft.withheld or "no parent could be drafted"
            continue
        keys = {o.lower() for o in draft.key}
        empty = [c for c in reading.aggregated if c.lower() not in keys] or [
            c for c in reading.outer_joined if c.lower() not in keys
        ]
        out.parents[label.lower()] = ParentRule(
            model=draft.model,
            relation=None,
            key=tuple(draft.key.items()),
            drafted=True,
            evidence=draft.evidence,
            supported_by=tuple(draft.supported_by),
            detail=draft.detail,
            alternatives=tuple(a["model"] for a in draft.alternatives),
            empty_columns=tuple(empty),
            settled_by=_settling(declared, keys, SETTLING_TIERS),
        )
    if elided:
        out.notes.append(
            f"a parent was drafted and measured for the first {MAX_INFERRED} "
            f"models; {elided} more were not, so narrow --select or build with "
            "--for-plan to measure the ones a change is about"
        )
    if no_draft and focused is None:
        out.notes.append(
            f"{no_draft} built model(s) have no drafted parent (no grain, or no "
            "entity outside their own lineage keyed by it), so their population "
            "was not measured against one"
        )
    return out


def _settling(
    declared: Mapping[str, Any], keys: set[str], tiers: Iterable[str]
) -> tuple[tuple[str, str], ...] | None:
    """The first assumption that names a key column at a tier that settles a
    population, as ``(field, value)`` pairs, or ``None``."""

    allowed = set(tiers)
    for assumption in declared.get("assumptions") or []:
        if not isinstance(assumption, dict):
            continue
        column = assumption.get("column")
        if not isinstance(column, str):
            continue
        if column.strip().lower().rsplit(".", 1)[-1] not in keys:
            continue
        if assumption.get("evidence") not in allowed:
            continue
        return tuple(
            (k, str(assumption[k]))
            for k in ("decision", "chosen", "evidence", "column")
            if assumption.get(k) is not None
        )
    return None
