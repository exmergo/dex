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

A test Dex rendered for a composite grain, a fraction, a population filter
or a parent population carries the claim id of the fact it checks (#1448),
and is matched to that fact by the claim; a test with no claim (hand-written,
or rendered before #1448) is matched by kind, column and parameters: the same
columns, the same filter values, the same parent and key, both directions of
a parent population. A composite-grain, unit, population-filter or
parent-population fact with tests carries ``matched_by``: ``claim`` or
``kind_and_column``; other facts list their tests without it, a
single-column grain checked by ``unique``, or a ``references`` fact credited
by ``dex_join_coverage`` or ``dex_referenced_key``. A test carrying any
other claim earns no credit for the fact, not even where its kind and column
match: a stale claim names a fact the declaration no longer states, a
different filter or a different parent, so it checks something else (#1550).
``dex_join_coverage`` is the exception: its claim names the join the model's
SQL writes, which this section cannot read, so it is matched by its key pairs
and the parent its `parent` argument names, never by claim.

Never a value: a population filter's ``includes``/``excludes`` and a rule's
prose stay out of the payload in every mode, so the section is the same in
values-free mode.

How each fact kind is judged (dbt test; maintain verify codes):

- grain, one column: ``unique`` on it; ``grain_broken``.
- grain, composite: ``unique_combination_of_columns`` over the same set, or
  ``dex_unique_combination`` over the same set; ``grain_broken``.
- key role: ``unique`` on it; ``grain_broken`` only where it is the
  single-column grain.
- foreign_key role with no ``references``: ``relationships`` on it; nothing
  in verify.
- references: ``relationships`` on it to the named model, ``dex_join_coverage``
  over the join's key pairs, or ``dex_referenced_key`` on the named parent
  column; ``join_no_match`` and ``join_orphans`` only where the model reads
  the referenced model, since verify judges a reference through a join its
  SQL writes.
- null_rule ``never``: ``not_null`` on it; nothing in verify (dbt's test is
  the check). Any other null_rule allows NULLs and is ``prose``.
- population.filters: ``dex_population_filter`` per list with the declared
  values; ``population_contradicted``.
- population.parent: ``dex_parent_population`` in both directions with the
  declared parent, key and filters; ``parent_population_missing`` and
  ``parent_population_unexpected``.
- population.rule with no filter or parent: ``prose``.
- unit ``fraction`` or ``percent``: ``unit_out_of_range``; ``dex_unit_range``
  on it where the unit is ``fraction`` (the test checks 0 to 1; a percent is
  0 to 100, so it never checks a percent). Any other unit: nothing.
- additivity ``additive``: ``additivity_contradicted``; any other: nothing
  (``transform plan`` flags a SUM over a semi- or non-additive column, verify
  does not).
- history: ``temporal_*``.
- freshness (#1460): ``freshness_exceeded``, and dbt's ``dex_freshness`` where
  the column carries it.
- continuity (#1461): dbt's ``dex_continuity`` where the column carries one at
  the declared grain and ``by``; nothing in verify.
"""

from __future__ import annotations

from collections import defaultdict
from typing import TYPE_CHECKING, Any

from .. import dbt_tests
from ..guards.pii_meta import entry_meta
from ..transform.claims import claim_id, claim_in

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
    "continuity",
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
        # (model uid) -> list of (test name, column lowered, kwargs, reads, claim)
        self.by_model: dict[
            str, list[tuple[str, str | None, dict, list[str], str | None]]
        ] = defaultdict(list)
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
            claim = claim_in(test)
            for owner in dbt_tests.generic_test_targets(test):
                self.by_model[owner].append((name, column_key, kwargs, reads, claim))
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

    @staticmethod
    def _match(
        entries: list[tuple[str, str | None, dict, list[str], str | None]],
        claims: list[str],
    ) -> tuple[list[str], str | None]:
        """The tests among ``entries`` that check a fact, and how they match.

        ``entries`` are the tests of one kind already matched by column and
        parameters. A test carrying the fact's current claim checks it by
        ``claim``; one carrying no claim checks it by ``kind_and_column``; one
        carrying any other claim is stale and earns no credit for the fact
        (#1550), not even where its kind and column match.
        """

        by_claim = list(
            dict.fromkeys(
                name
                for name, _col, _kwargs, _reads, claim in entries
                if claim is not None and claim in claims
            )
        )
        if by_claim:
            return by_claim, "claim"
        by_kind = list(
            dict.fromkeys(
                name for name, _col, _kwargs, _reads, claim in entries if claim is None
            )
        )
        if by_kind:
            return by_kind, "kind_and_column"
        return [], None

    def on_column(self, model: str, column: str, test: str) -> bool:
        key = _bare(column)
        return any(
            _base(name) == test and col == key
            for name, col, _kwargs, _reads, _claim in self.by_model.get(model, [])
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
        for name, col, kwargs, _reads, _claim in self.by_model.get(model, []):
            if _base(name) != "dex_freshness" or col != key:
                continue
            limit = kwargs.get("max_lag_seconds")
            try:
                if limit is not None and int(limit) == seconds:
                    return True
            except (TypeError, ValueError):
                continue
        return False

    def continuity(self, model: str, column: str, declared: dict) -> bool:
        """Whether a ``dex_continuity`` on ``column`` searches the declared grain
        and ``by`` keys (#1461): a test left at another grain or other keys
        searches other buckets, so it does not check the declaration."""

        grain = str(declared.get("grain") or "day").lower()
        raw = declared.get("by") or []
        # The keys partition the search, so their order does not matter.
        by = sorted(_bare(str(k)) for k in (raw if isinstance(raw, list) else [raw]))
        key = _bare(column)
        for name, col, kwargs, _reads, _claim in self.by_model.get(model, []):
            if _base(name) != "dex_continuity" or col != key:
                continue
            listed = kwargs.get("by") or []
            listed = listed if isinstance(listed, list) else [listed]
            if (
                str(kwargs.get("grain") or "day").lower() == grain
                and sorted(_bare(str(k)) for k in listed) == by
            ):
                return True
        return False

    def unique_combination(
        self, model: str, columns: list[str], claims: list[str]
    ) -> tuple[list[str], str | None]:
        """Whether a combination test checks a composite grain (#1550): dbt's
        ``unique_combination_of_columns`` or Dex's ``dex_unique_combination``
        over the same set of columns, by claim where it carries the grain's,
        by kind and column where it carries none."""

        wanted = {_bare(c) for c in columns}
        entries = []
        for entry in self.by_model.get(model, []):
            name, _col, kwargs, _reads, _claim = entry
            if _base(name) not in {
                "unique_combination_of_columns",
                "dex_unique_combination",
            }:
                continue
            combo = kwargs.get("combination_of_columns") or kwargs.get("columns")
            if isinstance(combo, list) and {_bare(str(c)) for c in combo} == wanted:
                entries.append(entry)
        return self._match(entries, claims)

    def unit_range(
        self, model: str, column: str, claims: list[str]
    ) -> tuple[list[str], str | None]:
        """Whether a ``dex_unit_range`` on ``column`` checks the fact (#1550):
        the test checks 0 to 1, so the caller only asks it of a fraction."""

        key = _bare(column)
        entries = [
            entry
            for entry in self.by_model.get(model, [])
            if _base(entry[0]) == "dex_unit_range" and entry[1] == key
        ]
        return self._match(entries, claims)

    def population_filter(
        self,
        model: str,
        column: str,
        mode: str,
        values: list,
        claims: list[str],
    ) -> tuple[list[str], str | None]:
        """Whether a ``dex_population_filter`` checks one list of a declared
        population filter (#1550): on the filter's column, with the declared
        values in that mode. A test carrying an ``excludes`` list checks the
        excludes list alone, so it never checks an ``includes`` list."""

        key = _bare(column)
        other = "excludes" if mode == "includes" else "includes"
        entries = []
        for entry in self.by_model.get(model, []):
            name, col, kwargs, _reads, _claim = entry
            if _base(name) != "dex_population_filter" or col != key:
                continue
            # Typed and order-insensitive, the same form the fact's
            # identity uses, so `True` and `1` stay two predicates and a
            # reordered list still matches. The other-list guard applies
            # only to `includes`: a test carrying an `excludes` list checks
            # that list alone, whatever `includes` it carries.
            if _filter(kwargs.get(mode), None)[0] == _filter(list(values), None)[
                0
            ] and (mode == "excludes" or not kwargs.get(other)):
                entries.append(entry)
        return self._match(entries, claims)

    def parent_population(
        self,
        model: str,
        direction: str,
        parent: str,
        key: list[tuple[str, str]],
        filters: list[dict[str, Any]] | None,
        claims: list[str],
    ) -> tuple[list[str], str | None]:
        """Whether a ``dex_parent_population`` checks one direction of a
        declared parent population (#1550): the direction, the declared parent
        its ``parent`` argument names, the declared key pairs, and, for
        ``missing``, the declared filters. The ``unexpected`` direction reads
        every parent key, so a filter it carries is not read."""

        wanted_key = {(_bare(k), _bare(v)) for k, v in key}
        wanted = self._resolve(parent, near=model)
        entries = []
        for entry in self.by_model.get(model, []):
            name, _col, kwargs, _reads, _claim = entry
            if _base(name) != "dex_parent_population":
                continue
            if kwargs.get("direction") != direction:
                continue
            listed = kwargs.get("key")
            if not isinstance(listed, dict):
                continue
            if {
                (_bare(str(k)), _bare(str(v))) for k, v in listed.items()
            } != wanted_key:
                continue
            if wanted is None:
                continue
            # The parent the test's `parent` argument names, not merely
            # one of the relations it reads: a test naming another parent
            # while reading this one checks the other parent.
            if self._resolve_test_parent(kwargs.get("parent"), near=model) != wanted:
                continue
            if direction == "missing" and not _same_filters(
                kwargs.get("filters"), filters
            ):
                continue
            entries.append(entry)
        return self._match(entries, claims)

    def join_coverage(
        self, model: str, column: str, field: str | None, parent: str | None
    ) -> bool:
        """Whether a ``dex_join_coverage`` checks a declared ``references``
        (#1550): the test reads the join's two inputs, and the declared
        column and field are one of its key pairs, to the declared parent.
        The parent it joins to is the test's ``parent`` argument, not merely
        one of the relations it reads: a test whose driving input is the
        declared parent and whose parent is another model with the same key
        name checks another join. The filters the model applies before
        joining are the model's SQL, which this section cannot read, so they
        are not part of the match, and neither is the test's claim, which
        names that same join."""

        key = _bare(column)
        wanted_field = _bare(field) if field else None
        wanted = self._resolve(parent, near=model) if parent else None
        if wanted is None:
            return False
        for name, _col, kwargs, _reads, _claim in self.by_model.get(model, []):
            if _base(name) != "dex_join_coverage":
                continue
            children = kwargs.get("child_columns") or []
            parents = kwargs.get("parent_columns") or []
            if (
                not isinstance(children, list)
                or not isinstance(parents, list)
                or len(children) != len(parents)
            ):
                continue
            pairs = {
                (_bare(str(c)), _bare(str(p)))
                for c, p in zip(children, parents, strict=True)
            }
            if (key, wanted_field) not in pairs:
                continue
            if self._resolve_test_parent(kwargs.get("parent"), near=model) != wanted:
                continue
            return True
        return False

    def referenced_key(
        self, parent_uid: str | None, field: str | None, claims: list[str]
    ) -> tuple[list[str], str | None]:
        """Whether a ``dex_referenced_key`` on the parent checks a declared
        ``references`` (#1550): on the referenced field, by claim where it
        carries the parent's, by kind and column where it carries none. The
        test sits on the parent, the model the claim is about, not the child
        whose declaration states the fact."""

        if parent_uid is None or not field:
            return [], None
        key = _bare(field)
        entries = [
            entry
            for entry in self.by_model.get(parent_uid, [])
            if _base(entry[0]) == "dex_referenced_key" and entry[1] == key
        ]
        return self._match(entries, claims)

    def _resolve(
        self, name: str, *, near: str, package: str | None = None
    ) -> str | None:
        """The manifest unique id a declared ``references`` parent names: a
        model, seed or snapshot, of ``near``'s own package first, then the only
        one of that name anywhere; an unversioned name of a versioned model is
        its latest version, as dbt resolves an unversioned ``ref``. Where
        ``package`` names one, only a node of that package counts: a
        package-qualified ``ref`` names that package's model, never a
        same-named one of the project's own package."""

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
        if package is not None:
            in_package = [uid for uid in found if self.packages.get(uid) == package]
            return in_package[0] if len(in_package) == 1 else None
        own = [
            uid for uid in found if self.packages.get(uid) == self.packages.get(near)
        ]
        if len(own) == 1:
            return own[0]
        return found[0] if len(found) == 1 else None

    def _resolve_test_parent(self, parent: Any, *, near: str) -> str | None:
        """The manifest unique id a join test's ``parent`` argument names.

        The argument is the ``ref()``/``source()`` call the join reads the
        parent through, written as text, or a relation written directly. A
        ``ref()`` names its model by its last positional argument, and where
        it names two, the first is the package the model must live in, so
        ``ref('pkg', 'model')`` is that package's model, never a same-named
        one of the project's own package; a ``v=`` keyword never changes
        which one (a versioned ref makes the join unreadable, so it never
        reaches a test). A ``source()`` names no model, so it resolves
        to nothing: the declared parent is always a model."""

        if not isinstance(parent, str) or not parent.strip():
            return None
        value = parent.strip()
        if value.startswith("ref(") and value.endswith(")"):
            args = [
                part.strip()
                for part in value[4:-1].split(",")
                if part.strip() and "=" not in part
            ]
            names = [
                part[1:-1]
                for part in args
                if len(part) >= 2 and part[0] == part[-1] and part[0] in "\"'`"
            ]
            if not names:
                return None
            if len(names) >= 2:
                return self._resolve(names[-1], near=near, package=names[-2])
            return self._resolve(names[-1], near=near)
        if value.startswith("source("):
            return None
        # A bare name, or a relation written directly: its last part.
        return self._resolve(value.rsplit(".", 1)[-1].strip("\"'`[]"), near=near)

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
        for name, col, kwargs, reads, _claim in self.by_model.get(model, []):
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
        # Typed, so `true` and "True" stay two predicates. A falsy scalar
        # states no fact, like an absent mode; a truthy non-list is
        # malformed, so it never equals an absent mode: a test carrying
        # one must not match a declaration that states no such mode.
        if not isinstance(raw, list):
            return () if not raw else (("malformed", repr(raw)),)
        return tuple(sorted((type(v).__name__, repr(v)) for v in raw))

    return (values(includes), values(excludes))


def _same_filters(left: Any, right: Any) -> bool:
    """Whether two population filter rule lists state the same predicates.

    Typed and order-insensitive, the same form ``_filter`` uses for a
    fact's identity, so ``True`` and ``1`` stay two predicates and rules
    in another order still match. A side that is not a list of rules, or
    a rule that is not a mapping, matches nothing but an identical side.
    """

    if left is None or right is None:
        return left is None and right is None
    if not isinstance(left, list) or not isinstance(right, list):
        return False

    def rules(raw: list) -> list[tuple[str, tuple, tuple]] | None:
        out: list[tuple[str, tuple, tuple]] = []
        for rule in raw:
            if not isinstance(rule, dict):
                return None
            out.append(
                (
                    _bare(str(rule.get("column") or "")),
                    _filter(rule.get("includes"), rule.get("excludes"))[0],
                    _filter(rule.get("includes"), rule.get("excludes"))[1],
                )
            )
        return sorted(out, key=repr)

    a, b = rules(left), rules(right)
    return a is not None and b is not None and a == b


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
        kind: str,
        column: str | None,
        found: list[str],
        verify: list[str],
        matched_by: str | None = None,
        **identity,
    ):
        facts.append(
            _record("dbt", label, kind, column, relation, found, verify, **identity)
        )
        if matched_by is not None:
            facts[-1]["matched_by"] = matched_by

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

    # The claim ids each rendered test of this declaration carries (#1448), by
    # the model's label or, for a declaration on every version, its bare name.
    names = list(dict.fromkeys([label, str(node.get("name") or label)]))

    def ids(field: str, columns: list[str], params: dict[str, Any]) -> list[str]:
        return [claim_id(name, field, columns, params) for name in names]

    if grain_cols:
        if len(grain_cols) == 1:
            found = ["unique"] if tests.on_column(uid, grain_cols[0], "unique") else []
            add("grain", grain_cols[0], found, ["grain_broken"])
        else:
            found, how = tests.unique_combination(
                uid,
                grain_cols,
                ids("grain", grain_cols, {"check": "dex_unique_combination"}),
            )
            add("grain", ", ".join(grain_cols), found, ["grain_broken"], how)

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
            if tests.join_coverage(uid, name, field or None, parent):
                found.append("dex_join_coverage")
            target_uid = tests._resolve(parent, near=uid)
            if target_uid is not None and field:
                # The referenced-key test sits on the parent, under the
                # parent's claim for the field (#1457, #1550).
                parent_node = nodes.get(target_uid) or {}
                parent_names = list(
                    dict.fromkeys(
                        [
                            _label(target_uid, parent_node),
                            str(parent_node.get("name") or ""),
                        ]
                    )
                )
                ref_claims = [
                    claim_id(
                        parent_name,
                        "referenced_key",
                        [field],
                        {"check": "dex_referenced_key"},
                    )
                    for parent_name in parent_names
                    if parent_name
                ]
                ref_names, _how = tests.referenced_key(target_uid, field, ref_claims)
                found.extend(ref_names)
            joins = parent.lower() in reads or parent.split(".", 1)[0].lower() in reads
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
            unit_found: list[str] = []
            unit_how: str | None = None
            if checked and unit.strip().lower() == "fraction":
                # `dex_unit_range` checks 0 to 1: it checks a fraction, never
                # a percent (0 to 100), whose check stays a question verify
                # asks (#1455, #1550).
                unit_found, unit_how = tests.unit_range(
                    uid,
                    name,
                    ids(
                        "unit",
                        [name],
                        {"check": "dex_unit_range", "unit": "fraction"},
                    ),
                )
            add(
                "unit",
                name,
                unit_found,
                ["unit_out_of_range"] if checked else [],
                unit_how,
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
        continuity = dex.get("continuity")
        if isinstance(continuity, dict):
            found = (
                ["dex_continuity"] if tests.continuity(uid, name, continuity) else []
            )
            keys = continuity.get("by") or []
            keys = keys if isinstance(keys, list) else [keys]
            what = str(continuity.get("grain") or "day")
            if keys:
                what += " by " + ", ".join(str(k) for k in keys)
            add("continuity", name, found, [], what=what)

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
                # Every list the entry states needs its own test: one
                # checking `includes` says nothing about `excludes` (#1550).
                hows: list[str | None] = []
                for mode in ("includes", "excludes"):
                    values = entry.get(mode)
                    if values is None:
                        continue
                    if not isinstance(values, list):
                        # A malformed mode: no test can check it, and the
                        # report must not raise on it (#1550).
                        hows.append(None)
                        continue
                    if not values:
                        continue
                    _names, mode_how = tests.population_filter(
                        uid,
                        entry["column"],
                        mode,
                        list(values),
                        ids(
                            "population",
                            [entry["column"]],
                            {
                                "check": "dex_population_filter",
                                mode: list(values),
                            },
                        ),
                    )
                    hows.append(mode_how)
                if hows and all(h is not None for h in hows):
                    found = ["dex_population_filter"]
                    how = (
                        "claim"
                        if all(h == "claim" for h in hows)
                        else "kind_and_column"
                    )
                else:
                    found = []
                    how = None
                add(
                    "population_filter",
                    entry["column"],
                    found,
                    ["population_contradicted"],
                    how,
                    what=_filter(entry.get("includes"), entry.get("excludes")),
                )
        from ..project_definitions import read_population_parent

        stated = read_population_parent(population.get("parent"))
        if stated is not None:
            record_column = ", ".join(stated.key)
            # The filters the `missing` test carries, as the renderer builds
            # them (#1454): every declared filter with a list, in order.
            filter_rules: list[dict[str, Any]] = []
            for f_entry in filters if isinstance(filters, list) else []:
                if not (
                    isinstance(f_entry, dict) and isinstance(f_entry.get("column"), str)
                ):
                    continue
                filter_rule: dict[str, Any] = {"column": f_entry["column"]}
                for mode in ("includes", "excludes"):
                    values = f_entry.get(mode)
                    # A malformed mode is dropped, never raised on, and a
                    # rule left with no mode states no predicate (#1550).
                    if isinstance(values, list) and values:
                        filter_rule[mode] = list(values)
                if len(filter_rule) > 1:
                    filter_rules.append(filter_rule)
            key_cols = list(stated.key)
            # Both directions need their own test: one checking `missing`
            # says nothing about `unexpected` (#1550).
            hows = []
            for direction in ("missing", "unexpected"):
                params: dict[str, Any] = {
                    "check": "dex_parent_population",
                    "direction": direction,
                    "parent": stated.model,
                    # The parent columns, in key order, folded as the claim
                    # folds a string.
                    "parent_key": [str(v).lower() for v in stated.key.values()],
                }
                if direction == "missing" and filter_rules:
                    params["filters"] = [dict(rule) for rule in filter_rules]
                _names, dir_how = tests.parent_population(
                    uid,
                    direction,
                    stated.model,
                    list(stated.key.items()),
                    # None where the declaration states no filter: the test
                    # then carries no `filters` argument either.
                    [dict(rule) for rule in filter_rules]
                    if direction == "missing" and filter_rules
                    else None,
                    ids("population", key_cols, params),
                )
                hows.append(dir_how)
            if all(h is not None for h in hows):
                found = ["dex_parent_population"]
                how = "claim" if all(h == "claim" for h in hows) else "kind_and_column"
            else:
                found = []
                how = None
            add(
                "population_parent",
                record_column,
                found,
                ["parent_population_missing", "parent_population_unexpected"],
                how,
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
