"""``maintain coverage``'s declaration section: what checks each declared fact (#1283).

A model's declaration (``config.meta.dex`` on the model and its columns) states
facts about its rows. Some are checked on every ``dbt build`` because a dbt test
asserts them (Dex renders ``not_null`` for ``null_rule: never``, ``unique`` and
``not_null`` for a single-column grain and ``relationships`` for a
``references``); some only by ``maintain verify`` (and ``transform build
--verify``); some by nothing. This section names, per declared fact, which.

``dex_verify`` means ``maintain verify`` *can* judge the fact: the section reads
the parsed manifest only, opens no connection and says nothing about the last
result. Each record names the verify finding codes that would report the fact
contradicted.

A native semantic layer's declarations (Ossie, SLayer, Cube, Metric Views,
Semantic Views) are read from the channels that already carry them (#1346): its
keys and joins from the fingerprint ``maintain grain`` re-verifies, and its
``dex`` metadata from the layer's declared models, the ones ``explore profile
--use-project`` reads. Each record names its ``source``: ``dbt``, or the
vendor. A native layer runs no dbt test, and ``maintain verify`` reads
declarations from the dbt manifest only, so ``maintain grain``'s codes are the
only check a native fact can have:

- a key the layer declares: ``declared_grain_not_unique``. A primary key is a
  ``grain`` record; a uniqueness claim that is not a grain (an Ossie
  ``unique_keys`` entry, a Metric View's ``rely.at_most_one_match``) a ``key``.
- a join the layer declares: ``references`` with ``declared_join_orphans``, and
  ``join_cardinality_exceeded`` where it is declared ``many_to_one`` or
  ``one_to_one``. A ``many_to_many`` join bounds nothing and has neither.
- what its ``dex`` metadata states (roles, references, population, unit,
  additivity): nothing, unless the same fact is one of the above. A declared
  model's grain is the primary key the layer declares, so it is that key's
  record.

A fact a dbt model and a native layer both declare on the same relation is
listed once per source and counted once in the summary, with what checks it
taken from both.

Never a value: a population filter's ``includes``/``excludes`` and a rule's
prose stay out of the payload in every mode, so the section is the same in
values-free mode.

How each fact kind is judged (dbt test; maintain verify codes):

- grain, one column: ``unique`` on it; ``grain_broken``.
- grain, composite: ``unique_combination_of_columns`` over the same set;
  ``grain_broken``.
- key role: ``unique`` on it; ``grain_broken`` only where it is the
  single-column grain.
- foreign_key role with no ``references``: ``relationships`` on it; nothing
  in verify.
- references: ``relationships`` on it to the named model; ``join_no_match``
  and ``join_orphans`` only where the model reads the referenced model, since
  verify judges a reference through a join its SQL writes.
- null_rule ``never``: ``not_null`` on it; nothing in verify (dbt's test is
  the check). Any other null_rule allows NULLs and is ``prose``.
- population.filters: no dbt test is read as one; ``population_contradicted``.
- population.parent: none; ``parent_population_missing`` and
  ``parent_population_unexpected``.
- population.rule with no filter or parent: ``prose``.
- unit ``fraction`` or ``percent``: ``unit_out_of_range``; any other unit:
  nothing.
- additivity ``additive``: ``additivity_contradicted``; any other: nothing
  (``transform plan`` flags a SUM over a semi- or non-additive column, verify
  does not).
- history: ``temporal_*``.
- freshness (#1460): ``freshness_exceeded``, and dbt's ``dex_freshness`` where
  the column carries it.
"""

from __future__ import annotations

from collections import defaultdict
from typing import TYPE_CHECKING, Any

from .. import dbt_tests
from ..guards.pii_meta import entry_meta

if TYPE_CHECKING:
    from ..project_definitions import ModelDeclaration
    from .snapshot import SemanticLayer

#: Fact records listed before the cut; the summary counts every one.
FACT_CAP = 50

CHECKERS = ("dbt_test", "dex_verify", "both", "none", "prose")

KINDS = (
    "grain",
    "key",
    "foreign_key",
    "references",
    "null_rule",
    "population_filter",
    "population_parent",
    "population_rule",
    "unit",
    "additivity",
    "history",
    "freshness",
)

_CHECKED_UNITS = frozenset({"fraction", "percent"})
_TEMPORAL_CODES = [
    "temporal_transition_count_mismatch",
    "temporal_latest_transition_mismatch",
    "temporal_first_attainment_mismatch",
    "temporal_population_missing",
    "temporal_population_unexpected",
    "temporal_provenance_contradicted",
]


def _ratio(numerator: int, denominator: int, of: str) -> dict[str, Any]:
    return {"numerator": numerator, "denominator": denominator, "of": of}


def _label(unique_id: str, node: dict[str, Any]) -> str:
    from ..dbt_project import manifest_node_label

    return manifest_node_label(unique_id, node)


def _checked_by(tests: list[str], verify: list[str]) -> str:
    if tests and verify:
        return "both"
    if tests:
        return "dbt_test"
    if verify:
        return "dex_verify"
    return "none"


class _Tests:
    """The enabled generic tests on each model, read once."""

    def __init__(self, manifest: dict[str, Any], labels: dict[str, str]) -> None:
        # (model uid) -> list of (test name, column lowered, kwargs, reads)
        self.by_model: dict[str, list[tuple[str, str | None, dict, list[str]]]] = (
            defaultdict(list)
        )
        for test in (manifest.get("nodes") or {}).values():
            if not isinstance(test, dict) or test.get("resource_type") != "test":
                continue
            if not dbt_tests.test_enabled(test):
                continue
            if dbt_tests.data_test_kind(test) != "generic":
                continue
            name = dbt_tests.generic_test_name(test)
            meta = test.get("test_metadata") or {}
            raw = meta.get("kwargs")
            kwargs: dict[str, Any] = raw if isinstance(raw, dict) else {}
            column = test.get("column_name") or kwargs.get("column_name")
            column_key = _bare(str(column)) if column else None
            reads = dbt_tests.test_reads(test)
            for owner in dbt_tests.generic_test_targets(test):
                self.by_model[owner].append((name, column_key, kwargs, reads))
        self.labels = labels
        #: Each model's package, so a package-qualified `ref` resolves.
        self.parents: dict[str, dict] = {
            uid: node
            for uid, node in (manifest.get("nodes") or {}).items()
            if isinstance(node, dict)
            and node.get("resource_type") in {"model", "seed", "snapshot"}
        }
        self.packages: dict[str, str | None] = {
            uid: node.get("package_name") for uid, node in self.parents.items()
        }

    def on_column(self, model: str, column: str, test: str) -> bool:
        key = _bare(column)
        return any(
            _base(name) == test and col == key
            for name, col, _kwargs, _reads in self.by_model.get(model, [])
        )

    def freshness(self, model: str, column: str, declared: dict) -> bool:
        """Whether a ``dex_freshness`` on ``column`` holds the declared lag
        (#1460): a test left at an older, looser lag permits data the
        declaration forbids, so it does not check it."""

        from ..semantic_declarations import lag_seconds

        try:
            seconds = lag_seconds(str(declared.get("max_lag")))
        except ValueError:
            return False
        key = _bare(column)
        for name, col, kwargs, _reads in self.by_model.get(model, []):
            if _base(name) != "dex_freshness" or col != key:
                continue
            limit = kwargs.get("max_lag_seconds")
            try:
                if limit is not None and int(limit) == seconds:
                    return True
            except (TypeError, ValueError):
                continue
        return False

    def combination(self, model: str, columns: list[str]) -> str | None:
        wanted = {_bare(c) for c in columns}
        for name, _col, kwargs, _reads in self.by_model.get(model, []):
            if _base(name) != "unique_combination_of_columns":
                continue
            combo = kwargs.get("combination_of_columns")
            if isinstance(combo, list) and {_bare(str(c)) for c in combo} == wanted:
                return name
        return None

    def _resolve(self, name: str, *, near: str) -> str | None:
        """The manifest unique id a declared ``references`` parent names: a
        model, seed or snapshot, of ``near``'s own package first, then the only
        one of that name anywhere; an unversioned name of a versioned model is
        its latest version, as dbt resolves an unversioned ``ref``."""

        key = name.lower()
        found = []
        for uid, node in self.parents.items():
            label = (self.labels.get(uid) or str(node.get("name") or "")).lower()
            if label == key:
                found.append(uid)
            elif str(node.get("name") or "").lower() == key:
                version, latest = node.get("version"), node.get("latest_version")
                if version is None or latest is None or str(version) == str(latest):
                    found.append(uid)
        own = [
            uid for uid in found if self.packages.get(uid) == self.packages.get(near)
        ]
        if len(own) == 1:
            return own[0]
        return found[0] if len(found) == 1 else None

    def relationship(
        self,
        model: str,
        column: str,
        parent: str | None,
        field: str | None = None,
    ) -> bool:
        """Whether a ``relationships`` test on ``model.column`` checks the
        declared reference: to ``parent`` (by the relation it reads, the model
        itself included for a self-reference) and, where both name one, on
        the declared ``field``."""

        key = _bare(column)
        for name, col, kwargs, reads in self.by_model.get(model, []):
            if _base(name) != "relationships" or col != key:
                continue
            tested_field = kwargs.get("field")
            if (
                field is not None
                and tested_field is not None
                and _bare(str(tested_field)) != _bare(field)
            ):
                continue
            if parent is None:
                return True
            # dbt resolved the test's `to` into its dependencies: the target is
            # what the test reads besides the child, and the child itself only
            # when it reads nothing else (a self-reference).
            targets = [uid for uid in reads if uid != model] or [model]
            wanted = self._resolve(parent, near=model)
            if wanted is not None:
                if wanted in targets:
                    return True
                continue
        return False


def _bare(name: str) -> str:
    return name.strip().strip('"`[]').lower()


def _base(test_name: str) -> str:
    """``dbt_utils.unique_combination_of_columns`` -> its name without namespace."""

    return test_name.rsplit(".", 1)[-1]


def declarations(
    manifest: dict[str, Any],
    *,
    selected: set[str],
    values_free: bool = False,
    full: bool = False,
    semantic: SemanticLayer | None = None,
    native_models: dict[str, ModelDeclaration] | None = None,
    selector: list[str] | None = None,
) -> dict[str, Any]:
    """Per declared fact on the ``selected`` models, what checks it.

    ``selected`` holds manifest unique ids of in-scope enabled project models.
    ``values_free`` changes nothing today: the section carries no value in
    either mode. It is taken so the caller states the mode it runs under.
    ``semantic`` is a native semantic vendor's fingerprint and ``native_models``
    its declared models, ``None`` for a project whose semantic layer is dbt's.
    ``selector`` is the model names a scope gave, which narrows the native
    records by model name the way ``selected`` narrows the manifest's.
    """

    del values_free  # the section is values-free in every mode
    nodes: dict[str, dict] = manifest.get("nodes") or {}
    labels = {
        uid: _label(uid, node)
        for uid, node in nodes.items()
        if isinstance(node, dict) and node.get("resource_type") == "model"
    }
    tests = _Tests(manifest, labels)

    records: list[dict[str, Any]] = []
    declared_models = 0
    in_scope = [uid for uid in selected if uid in labels]
    for uid in sorted(in_scope, key=lambda u: labels[u]):
        model_facts = _model_facts(uid, nodes[uid], labels[uid], tests, nodes)
        if model_facts:
            declared_models += 1
            records.extend(model_facts)
    wanted = {name.lower() for name in selector} if selector else None
    native = [
        record
        for record in _native_facts(semantic, native_models or {})
        if wanted is None or record["model"].lower() in wanted
    ]
    records.extend(native)
    known = {
        _parts(node.get("relation_name"))
        for node in [*nodes.values(), *(manifest.get("sources") or {}).values()]
        if isinstance(node, dict)
    }
    if semantic is not None:
        known |= {_parts(sm.relation) for sm in semantic.semantic_models}
    known |= {_parts(d.relation) for d in (native_models or {}).values()}
    _assign_ids(records, {rel for rel in known if rel is not None})
    facts = _one_per_source(records)

    # A fact two sources declare on one relation is one fact: listed per
    # source, counted once, checked by whatever checks either record.
    # Two records of one source share an `_id` only when they differ in
    # cardinality: two declarations, so they never fill one group.
    by_id: dict[tuple, list[list[dict[str, Any]]]] = defaultdict(list)
    for fact in facts:
        slots = by_id[fact["_id"]]
        slot = next(
            (g for g in slots if all(f["source"] != fact["source"] for f in g)), None
        )
        if slot is None:
            slots.append([fact])
        else:
            slot.append(fact)
    groups = {
        (key, n): group for key, slots in by_id.items() for n, group in enumerate(slots)
    }
    by_kind: dict[str, dict[str, int]] = {}
    by_checker = dict.fromkeys(CHECKERS, 0)
    shared = 0
    for group in groups.values():
        checker = _group_checker(group)
        bucket = by_kind.setdefault(
            group[0]["fact"], {"count": 0, **dict.fromkeys(CHECKERS, 0)}
        )
        bucket["count"] += 1
        bucket[checker] += 1
        by_checker[checker] += 1
        if len(group) > 1:
            shared += 1
            sources = [f["source"] for f in group]
            for fact in group:
                fact["also_declared_by"] = [s for s in sources if s != fact["source"]]
    by_source: dict[str, int] = {}
    for fact in facts:
        by_source[fact["source"]] = by_source.get(fact["source"], 0) + 1
        for internal in _INTERNAL:
            fact.pop(internal, None)
    checked = by_checker["dbt_test"] + by_checker["dex_verify"] + by_checker["both"]
    checkable = len(groups) - by_checker["prose"]

    order = {"none": 0, "dex_verify": 1, "dbt_test": 2, "both": 3, "prose": 4}
    facts.sort(
        key=lambda f: (
            order[f["checked_by"]],
            f["model"],
            KINDS.index(f["fact"]),
            f.get("column") or "",
            f["source"],
        )
    )
    elided = 0
    notes: list[str] = []
    if not full and len(facts) > FACT_CAP:
        elided = len(facts) - FACT_CAP
        facts = facts[:FACT_CAP]
        notes.append(
            f"{elided} declared fact(s) are not listed: the list is capped at "
            f"{FACT_CAP}, unchecked ones first; the summary counts every one. "
            "Pass --full, or name models, to list them"
        )
    if by_checker["dex_verify"] or by_checker["both"]:
        notes.append(
            "`dex_verify` means `maintain verify` (and `transform build --verify`) "
            "can judge the fact, or `maintain grain` for a native semantic "
            "layer's keys and joins; this section reads no warehouse and says "
            "nothing about the last result"
        )
    if native:
        vendors = ", ".join(sorted({f["source"] for f in native}))
        notes.append(
            f"facts from the {vendors} semantic layer carry its name as `source`: "
            "`maintain grain` can judge its keys and joins, and nothing checks "
            "what its `dex` metadata states, since `maintain verify` reads "
            "declarations from the dbt manifest only"
        )
    if shared:
        notes.append(
            f"{shared} declared fact(s) are stated by more than one source on the "
            "same relation: each is listed per source, with `also_declared_by`, "
            "and counted once"
        )
    if by_checker["prose"]:
        notes.append(
            "a `prose` fact is one nothing can check: a population stated only as "
            "a `rule`, or a `null_rule` that allows NULLs. Declare "
            "`population.filters` or `population.parent` to make a population "
            "checkable"
        )

    models_of = "selected enabled project models"
    return {
        "summary": {
            "models_declared": _ratio(declared_models, len(in_scope), models_of),
            "models_undeclared": len(in_scope) - declared_models,
            "facts": {
                "count": sum(b["count"] for b in by_kind.values()),
                "checked": _ratio(
                    checked,
                    checkable,
                    "checkable declared facts on selected enabled project models "
                    + ("and the native semantic layer " if native else "")
                    + "(prose facts excluded)",
                ),
                "by_kind": {k: by_kind[k] for k in KINDS if k in by_kind},
                "by_checker": by_checker,
                "by_source": by_source,
            },
        },
        "facts": facts,
        "elided": elided,
        "notes": notes,
    }


_INTERNAL = ("_rel", "_target", "_cols", "_card", "_what", "_id", "_sid")


def _part(part: str) -> str:
    """One identifier: a quoted one keeps its case, an unquoted one is lowered.

    Folding rules differ by warehouse (Snowflake upper-cases, DuckDB and
    Postgres lower-case), so a quoted identifier never matches an unquoted one
    spelled in another case: two relations may then count twice, never one
    relation's check be credited to another.
    """

    part = part.strip()
    if len(part) > 1 and (part[0], part[-1]) in {('"', '"'), ("`", "`"), ("[", "]")}:
        return part[1:-1]
    return part.lower()


def _parts(relation: str | None) -> tuple[str, ...] | None:
    """A relation's dotted parts, unquoted, unquoted ones lowered."""

    if not relation:
        return None
    parts = tuple(p for p in (_part(part) for part in _split_dots(str(relation))) if p)
    return parts or None


def _split_dots(relation: str) -> list[str]:
    """A relation split at the dots outside quotes, so a quoted identifier
    that holds a dot stays one part."""

    closers = {'"': '"', "`": "`", "[": "]"}
    parts: list[str] = []
    current: list[str] = []
    closer: str | None = None
    for char in relation:
        if closer is None and char in closers:
            closer = closers[char]
        elif char == closer:
            closer = None
        elif closer is None and char == ".":
            parts.append("".join(current))
            current = []
            continue
        current.append(char)
    parts.append("".join(current))
    return parts


def _target_of(relation: str | None, name: str | None) -> tuple[str, ...] | None:
    """What a join targets: its relation in full where known, else the name it
    gives, tagged so a bare name never matches a relation."""

    parts = _parts(relation)
    if parts:
        return parts
    return ("?name", _bare(name)) if name else None


def _lower(value: Any) -> str:
    return str(value).strip().lower()


def _filter(includes: Any, excludes: Any) -> tuple:
    """A population filter's values, for identity only: never output."""

    def values(raw: Any) -> tuple[tuple[str, str], ...]:
        # Typed, so `true` and "True" stay two predicates.
        if not isinstance(raw, list):
            return ()
        return tuple(sorted((type(v).__name__, repr(v)) for v in raw))

    return (values(includes), values(excludes))


def _parent_key(parent: Any) -> tuple:
    """A population parent's key pairs, for identity only. The parent itself
    is the record's target, matched by relation like a join's."""

    key = parent.key if isinstance(parent.key, dict) else {}
    return tuple(sorted((_bare(k), _bare(str(v))) for k, v in key.items()))


def _record(
    source: str,
    model: str,
    kind: str,
    column: str | None,
    relation: str | None,
    tests: list[str],
    verify: list[str] | None,
    *,
    target: tuple[str, ...] | None = None,
    pairs: list[tuple[str, str | None]] | None = None,
    cardinality: str | None = None,
    what: Any = None,
) -> dict[str, Any]:
    """One fact record; ``verify`` ``None`` marks a ``prose`` fact.

    The internal keys say which fact it is, so a fact two sources state can be
    found: the relation in full, a join's target and its ordered column pairs,
    what the fact asserts (``what``: a unit, an additivity, a filter's values,
    a parent and its key; a fresh object where two statements can never be
    shown equal, so they never merge), and a join's declared cardinality (two
    joins that differ only there are two declarations of one source).
    :func:`_assign_ids` reads them, and they are dropped before output, so no
    value ever leaves through them.
    """

    record: dict[str, Any] = {"model": model, "fact": kind}
    if column is not None:
        record["column"] = column
    record["source"] = source
    if verify is None:
        record.update(checked_by="prose", tests=[], verify=[])
    else:
        record["checked_by"] = _checked_by(tests, verify)
        record["tests"] = tests
        record["verify"] = verify
    record["_rel"] = _parts(relation)
    record["_target"] = target
    if pairs is not None:
        record["_cols"] = tuple((_bare(a), _bare(b) if b else None) for a, b in pairs)
    else:
        record["_cols"] = frozenset(
            _bare(c) for c in (column or "").split(",") if c.strip()
        )
    record["_card"] = cardinality
    record["_what"] = what
    return record


def _assign_ids(records: list[dict[str, Any]], known: set[tuple[str, ...]]) -> None:
    """Each record's fact identity (``_id``) and its per-source identity
    (``_sid``, which adds the cardinality).

    A relation is matched in full. One that names fewer parts (a semantic layer
    writing ``schema.table`` where dbt writes ``db.schema.table``) is matched to
    a fuller one only when exactly one relation ``known`` (every relation the
    manifest and the layer name, declared or not) ends with it, so two
    databases' ``analytics.orders`` stay two relations. A join's target is
    resolved the same way.
    """

    known = known | {r["_rel"] for r in records if r["_rel"] is not None}
    known |= {
        r["_target"]
        for r in records
        if r["_target"] is not None and r["_target"][0] != "?name"
    }

    def resolve(rel: tuple[str, ...] | None) -> tuple[str, ...] | None:
        if rel is None or rel[0] == "?name":
            return rel
        fuller = [o for o in known if len(o) > len(rel) and o[-len(rel) :] == rel]
        return fuller[0] if len(fuller) == 1 else rel

    for record in records:
        rel = resolve(record["_rel"])
        where: Any = (
            rel if rel is not None else f"{record['source']}:{record['model'].lower()}"
        )
        record["_id"] = (
            where,
            record["fact"],
            resolve(record["_target"]),
            record["_cols"],
            record["_what"],
        )
        record["_sid"] = (record["_id"], record["source"], record["_card"])


def _one_per_source(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One record per fact and source: a native layer can state one key both
    as its primary key and in its ``dex`` grain, and that is one fact."""

    kept: dict[tuple, dict[str, Any]] = {}
    for record in records:
        key = record["_sid"]
        first = kept.get(key)
        if first is None:
            kept[key] = record
            continue
        for field in ("tests", "verify"):
            first[field] = [
                *first[field],
                *[x for x in record[field] if x not in first[field]],
            ]
        if first["checked_by"] == "prose" and record["checked_by"] != "prose":
            first["checked_by"] = record["checked_by"]
        elif first["checked_by"] != "prose":
            first["checked_by"] = _checked_by(first["tests"], first["verify"])
    return list(kept.values())


def _group_checker(group: list[dict[str, Any]]) -> str:
    if all(fact["checked_by"] == "prose" for fact in group):
        return "prose"
    return _checked_by(
        [t for fact in group for t in fact["tests"]],
        [v for fact in group for v in fact["verify"]],
    )


def _native_facts(
    semantic: SemanticLayer | None, models: dict[str, ModelDeclaration]
) -> list[dict[str, Any]]:
    """A native semantic layer's declared facts: its keys and joins as
    ``maintain grain`` reads them, then what its ``dex`` metadata states."""

    facts: list[dict[str, Any]] = []
    relations: dict[str, str | None] = {}
    joins: dict[tuple, str | None] = {}
    if semantic is not None and semantic.relationships_and_keys_captured:
        source = semantic.vendor or "semantic"
        for sm in semantic.semantic_models:
            relations[sm.name] = sm.relation
            claims = (
                sm.key_claims
                if sm.key_claims is not None and len(sm.key_claims) == len(sm.keys)
                else None
            )
            for position, combo in enumerate(sm.keys):
                # The fallback `maintain grain` reads for a fingerprint that
                # recorded no claims (#1293).
                if claims is not None:
                    claim = claims[position]
                else:
                    claim = "unique" if semantic.vendor == "databricks" else "primary"
                facts.append(
                    _record(
                        source,
                        sm.name,
                        "grain" if claim == "primary" else "key",
                        ", ".join(combo),
                        sm.relation,
                        [],
                        ["declared_grain_not_unique"],
                    )
                )
        cardinality = semantic.relationship_cardinality_captured
        for rel in semantic.relationships:
            if not rel.column_pairs:
                continue
            verify: list[str] = []
            if not (cardinality and rel.cardinality == "many_to_many"):
                verify.append("declared_join_orphans")
            if cardinality and rel.cardinality in ("many_to_one", "one_to_one"):
                verify.append("join_cardinality_exceeded")
            target = _target_of(relations.get(rel.to_model), rel.to_model)
            facts.append(
                _record(
                    source,
                    rel.model,
                    "references",
                    ", ".join(frm for frm, _to in rel.column_pairs),
                    relations.get(rel.model),
                    [],
                    verify,
                    target=target,
                    pairs=list(rel.column_pairs),
                    cardinality=rel.cardinality,
                )
            )
            pairs = tuple((_bare(a), _bare(b)) for a, b in rel.column_pairs)
            joins[(source, _bare(rel.model), target, pairs)] = rel.cardinality
    # A declared model names its relation too, for a parent or a target the
    # fingerprint does not hold.
    named = {n: d.relation for n, d in models.items() if d.relation}
    relations = {**named, **{k: v for k, v in relations.items() if v}}
    for name in sorted(models):
        facts.extend(_declared_model_facts(models[name], relations, joins))
    return facts


def _declared_model_facts(
    declaration: ModelDeclaration,
    relations: dict[str, str | None],
    joins: dict[tuple, str | None],
) -> list[dict[str, Any]]:
    """What a native layer's ``dex`` metadata states about one model. Nothing
    judges it on that layer; a fact the layer's keys or joins also carry is
    merged with them by the caller. A ``references`` the layer also declares as
    a join (same model, target and columns) takes that join's cardinality, so
    the two are one declaration."""

    source, model, relation = (
        declaration.source,
        declaration.model,
        declaration.relation,
    )
    facts: list[dict[str, Any]] = []

    def add(kind: str, column: str | None, prose: bool = False, **identity) -> None:
        facts.append(
            _record(
                source,
                model,
                kind,
                column,
                relation,
                [],
                None if prose else [],
                **identity,
            )
        )

    if declaration.grain:
        add("grain", ", ".join(declaration.grain))
    for name, meaning in declaration.columns.items():
        if meaning.references and "." in meaning.references:
            parent, _, field = meaning.references.rpartition(".")
            target = _target_of(relations.get(parent), parent)
            pairs = ((_bare(name), _bare(field)),)
            add(
                "references",
                name,
                target=target,
                pairs=list(pairs),
                cardinality=joins.get((source, _bare(model), target, pairs)),
            )
        elif meaning.role == "foreign_key":
            add("foreign_key", name)
        if meaning.role == "key":
            add("key", name)
        if meaning.unit:
            add("unit", name, what=_lower(meaning.unit))
        if meaning.additivity:
            add("additivity", name, what=_lower(meaning.additivity))
    population = declaration.population
    if population is not None:
        filter_columns = [
            f.column for f in population.filters if f.includes or f.excludes
        ]
        for f in population.filters:
            if f.includes or f.excludes:
                add("population_filter", f.column, what=_filter(f.includes, f.excludes))
        if population.parent is not None:
            add(
                "population_parent",
                ", ".join(population.parent.key),
                target=_target_of(
                    relations.get(population.parent.model), population.parent.model
                ),
                what=_parent_key(population.parent),
            )
        if (
            population.rule
            and population.rule.strip()
            and not filter_columns
            and population.parent is None
        ):
            add("population_rule", None, prose=True, what=object())
    return facts


def _model_facts(
    uid: str,
    node: dict[str, Any],
    label: str,
    tests: _Tests,
    nodes: dict[str, dict],
) -> list[dict[str, Any]]:
    model_dex = entry_meta(node).get("dex")
    model_dex = model_dex if isinstance(model_dex, dict) else {}
    facts: list[dict[str, Any]] = []
    relation = node.get("relation_name")

    def add(
        kind: str, column: str | None, found: list[str], verify: list[str], **identity
    ):
        facts.append(
            _record("dbt", label, kind, column, relation, found, verify, **identity)
        )

    def prose(kind: str, column: str | None) -> None:
        facts.append(
            _record("dbt", label, kind, column, relation, [], None, what=object())
        )

    grain = model_dex.get("grain")
    grain_cols = (
        [g for g in grain if isinstance(g, str) and g]
        if isinstance(grain, list)
        else []
    )
    single_grain = _bare(grain_cols[0]) if len(grain_cols) == 1 else None
    if grain_cols:
        if len(grain_cols) == 1:
            found = ["unique"] if tests.on_column(uid, grain_cols[0], "unique") else []
            add("grain", grain_cols[0], found, ["grain_broken"])
        else:
            combo = tests.combination(uid, grain_cols)
            add(
                "grain",
                ", ".join(grain_cols),
                [combo] if combo else [],
                ["grain_broken"],
            )

    # The model reads these, so a declared reference to one of them is a join
    # verify's join contract can measure.
    reads = {
        _label(r, nodes[r]).lower()
        for r in (node.get("depends_on") or {}).get("nodes") or []
        if isinstance(nodes.get(r), dict)
    }
    reads |= {name.split(".", 1)[0] for name in reads}

    columns = node.get("columns")
    for key, column in columns.items() if isinstance(columns, dict) else []:
        if not isinstance(column, dict):
            continue
        name = column.get("name") if isinstance(column.get("name"), str) else key
        if not isinstance(name, str) or not name:
            continue
        dex = entry_meta(column).get("dex")
        if not isinstance(dex, dict):
            continue
        role = dex.get("role")
        references = dex.get("references")
        if isinstance(references, str) and "." in references:
            parent, _, field = references.rpartition(".")
            found = (
                ["relationships"]
                if tests.relationship(uid, name, parent, field or None)
                else []
            )
            joins = parent.lower() in reads or parent.split(".", 1)[0].lower() in reads
            target_uid = tests._resolve(parent, near=uid)
            target_node = nodes.get(target_uid) if target_uid else None
            add(
                "references",
                name,
                found,
                ["join_no_match", "join_orphans"] if joins else [],
                target=_target_of(
                    (target_node or {}).get("relation_name"), parent.split(".")[-1]
                ),
                pairs=[(name, field or None)],
            )
        elif role == "foreign_key":
            found = ["relationships"] if tests.relationship(uid, name, None) else []
            add("foreign_key", name, found, [])
        if role == "key":
            found = ["unique"] if tests.on_column(uid, name, "unique") else []
            verify = ["grain_broken"] if _bare(name) == single_grain else []
            add("key", name, found, verify)
        null_rule = dex.get("null_rule")
        if isinstance(null_rule, str) and null_rule.strip():
            if null_rule.strip().lower() == "never":
                found = ["not_null"] if tests.on_column(uid, name, "not_null") else []
                add("null_rule", name, found, [], what="never")
            else:
                prose("null_rule", name)
        unit = dex.get("unit")
        if isinstance(unit, str) and unit.strip():
            checked = unit.strip().lower() in _CHECKED_UNITS
            add(
                "unit",
                name,
                [],
                ["unit_out_of_range"] if checked else [],
                what=_lower(unit),
            )
        additivity = dex.get("additivity")
        if isinstance(additivity, str) and additivity.strip():
            add(
                "additivity",
                name,
                [],
                ["additivity_contradicted"] if additivity == "additive" else [],
                what=_lower(additivity),
            )
        freshness = dex.get("freshness")
        if isinstance(freshness, dict) and freshness.get("max_lag"):
            found = ["dex_freshness"] if tests.freshness(uid, name, freshness) else []
            add(
                "freshness",
                name,
                found,
                ["freshness_exceeded"],
                what=f"{freshness.get('max_lag')} {freshness.get('basis')}",
            )

    population = model_dex.get("population")
    if isinstance(population, dict):
        filters = population.get("filters")
        filter_columns = [
            entry["column"]
            for entry in (filters if isinstance(filters, list) else [])
            if isinstance(entry, dict)
            and isinstance(entry.get("column"), str)
            and (entry.get("includes") or entry.get("excludes"))
        ]
        for entry in filters if isinstance(filters, list) else []:
            if (
                isinstance(entry, dict)
                and entry.get("column") in filter_columns
                and (entry.get("includes") or entry.get("excludes"))
            ):
                add(
                    "population_filter",
                    entry["column"],
                    [],
                    ["population_contradicted"],
                    what=_filter(entry.get("includes"), entry.get("excludes")),
                )
        from ..project_definitions import read_population_parent

        stated = read_population_parent(population.get("parent"))
        if stated is not None:
            record_column = ", ".join(stated.key)
            add(
                "population_parent",
                record_column,
                [],
                ["parent_population_missing", "parent_population_unexpected"],
                target=_target_of(
                    (nodes.get(tests._resolve(stated.model, near=uid) or "") or {}).get(
                        "relation_name"
                    ),
                    stated.model,
                ),
                what=_parent_key(stated),
            )
        rule = population.get("rule")
        if (
            isinstance(rule, str)
            and rule.strip()
            and not filter_columns
            and stated is None
        ):
            prose("population_rule", None)

    if isinstance(model_dex.get("history"), dict):
        add("history", None, [], list(_TEMPORAL_CODES), what=object())
    return facts
