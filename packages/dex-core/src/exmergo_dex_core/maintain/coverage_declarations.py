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

Read from the dbt manifest alone. Declarations a native semantic layer carries
(Ossie, SLayer, Cube, Metric Views, Semantic Views) are not read in this
release.

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
"""

from __future__ import annotations

import re
from collections import defaultdict
from typing import Any

from .. import dbt_tests
from ..guards.pii_meta import entry_meta

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

    def on_column(self, model: str, column: str, test: str) -> bool:
        key = _bare(column)
        return any(
            _base(name) == test and col == key
            for name, col, _kwargs, _reads in self.by_model.get(model, [])
        )

    def combination(self, model: str, columns: list[str]) -> str | None:
        wanted = {_bare(c) for c in columns}
        for name, _col, kwargs, _reads in self.by_model.get(model, []):
            if _base(name) != "unique_combination_of_columns":
                continue
            combo = kwargs.get("combination_of_columns")
            if isinstance(combo, list) and {_bare(str(c)) for c in combo} == wanted:
                return name
        return None

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
            wanted = parent.lower()
            target = _ref_target(kwargs.get("to"))
            if target is not None:
                if target.lower() == wanted.split(".", 1)[0]:
                    return True
                continue
            # No readable `to`: the target is what the test reads besides the
            # child, and the child itself only when it reads nothing else.
            targets = [uid for uid in reads if uid != model] or [model]
            if any(
                self.labels.get(uid, "").lower() == wanted
                or self.labels.get(uid, "").split(".", 1)[0].lower() == wanted
                for uid in targets
            ):
                return True
        return False


_REF = re.compile(
    r"""^\s*(?:\{\{\s*)?ref\(\s*['"]([^'"]+)['"]\s*(?:,\s*['"]([^'"]+)['"]\s*)?\)"""
)


def _ref_target(to: Any) -> str | None:
    """The model a ``relationships`` test's ``to`` names, from ``ref('x')`` or
    ``ref('package', 'x')``; ``None`` for a source or anything else."""

    match = _REF.match(str(to)) if isinstance(to, str) else None
    if match is None:
        return None
    return match.group(2) or match.group(1)


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
) -> dict[str, Any]:
    """Per declared fact on the ``selected`` models, what checks it.

    ``selected`` holds manifest unique ids of in-scope enabled project models.
    ``values_free`` changes nothing today: the section carries no value in
    either mode. It is taken so the caller states the mode it runs under.
    """

    del values_free  # the section is values-free in every mode
    nodes: dict[str, dict] = manifest.get("nodes") or {}
    labels = {
        uid: _label(uid, node)
        for uid, node in nodes.items()
        if isinstance(node, dict) and node.get("resource_type") == "model"
    }
    tests = _Tests(manifest, labels)

    facts: list[dict[str, Any]] = []
    declared_models = 0
    in_scope = [uid for uid in selected if uid in labels]
    for uid in sorted(in_scope, key=lambda u: labels[u]):
        model_facts = _model_facts(uid, nodes[uid], labels[uid], tests, nodes)
        if model_facts:
            declared_models += 1
            facts.extend(model_facts)

    by_kind: dict[str, dict[str, int]] = {}
    by_checker = dict.fromkeys(CHECKERS, 0)
    for fact in facts:
        bucket = by_kind.setdefault(
            fact["fact"], {"count": 0, **dict.fromkeys(CHECKERS, 0)}
        )
        bucket["count"] += 1
        bucket[fact["checked_by"]] += 1
        by_checker[fact["checked_by"]] += 1
    checked = by_checker["dbt_test"] + by_checker["dex_verify"] + by_checker["both"]
    checkable = len(facts) - by_checker["prose"]

    order = {"none": 0, "dex_verify": 1, "dbt_test": 2, "both": 3, "prose": 4}
    facts.sort(
        key=lambda f: (
            order[f["checked_by"]],
            f["model"],
            KINDS.index(f["fact"]),
            f.get("column") or "",
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
            "can judge the fact; this section reads no warehouse and says nothing "
            "about the last result"
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
                    "(prose facts excluded)",
                ),
                "by_kind": {k: by_kind[k] for k in KINDS if k in by_kind},
                "by_checker": by_checker,
            },
        },
        "facts": facts,
        "elided": elided,
        "notes": notes,
    }


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

    def add(kind: str, column: str | None, found: list[str], verify: list[str]):
        record: dict[str, Any] = {"model": label, "fact": kind}
        if column is not None:
            record["column"] = column
        record["checked_by"] = _checked_by(found, verify)
        record["tests"] = found
        record["verify"] = verify
        facts.append(record)

    def prose(kind: str, column: str | None) -> None:
        record: dict[str, Any] = {"model": label, "fact": kind}
        if column is not None:
            record["column"] = column
        record.update(checked_by="prose", tests=[], verify=[])
        facts.append(record)

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
            add(
                "references",
                name,
                found,
                ["join_no_match", "join_orphans"] if joins else [],
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
                add("null_rule", name, found, [])
            else:
                prose("null_rule", name)
        unit = dex.get("unit")
        if isinstance(unit, str) and unit.strip():
            checked = unit.strip().lower() in _CHECKED_UNITS
            add("unit", name, [], ["unit_out_of_range"] if checked else [])
        additivity = dex.get("additivity")
        if isinstance(additivity, str) and additivity.strip():
            add(
                "additivity",
                name,
                [],
                ["additivity_contradicted"] if additivity == "additive" else [],
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
        for column_name in filter_columns:
            add("population_filter", column_name, [], ["population_contradicted"])
        from ..project_definitions import read_population_parent

        stated = read_population_parent(population.get("parent"))
        if stated is not None:
            record_column = ", ".join(stated.key)
            add(
                "population_parent",
                record_column,
                [],
                ["parent_population_missing", "parent_population_unexpected"],
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
        add("history", None, [], list(_TEMPORAL_CODES))
    return facts
