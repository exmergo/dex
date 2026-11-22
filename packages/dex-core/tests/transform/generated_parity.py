"""The parity harness: each generated test and ``maintain verify`` agree (#1450, #1451).

A declared fact Dex renders as a generated test has two implementations: the
test's SQL macro, which runs in the project's ``dbt build``, and the check
``maintain verify`` runs. This harness holds them to one answer.

**A row** names a generated test, the ``maintain verify`` finding codes it must
agree with, the declaration both fixture models carry, and two fixture models:
one that violates the declaration and one that meets it. For each row the
harness

1. declares both fixture models through ``transform plan`` with
   ``declarations.render_tests`` on, so the test is rendered (and its macro
   written) exactly as a customer's plan renders it;
2. applies the plan and builds both models and their tests with
   ``transform build --target dev``;
3. runs ``maintain verify --target dev`` on both models;
4. reports a disagreement wherever the violating model does not both fail the
   test and get the finding, or the clean model does not both pass the test
   and get no finding, and wherever the test's failure count differs from the
   number the finding states.

It drives everything through the CLI, the same commands a customer runs, so the
one runner serves DuckDB offline (``test_generated_parity.py``) and every
warehouse in ``integration.yml`` (each connector's ``*_transform.py`` calls
:func:`run_parity` with its own seeding). A row whose test does not support the
connector (``GeneratedTestKind.dialects``) must render nothing there and say so
under ``data.generated_tests``; the harness checks that instead (#1451).

Adding a generated test (#1453 onward) adds a :class:`ParityRow` to
:data:`ROWS`. A test with no ``maintain verify`` twin (#1461: ``dex_continuity``)
is a row with no codes and its own ``failures``: the violating fixture must fail
the test with exactly that count and the clean one pass, on every dialect, and
verify is not consulted for it. A dbt test a
declaration renders unconditionally (``relationships`` from a ``references``,
#1459) is a ``builtin`` row: no ``render_tests`` switch and no dialect list, the
rest alike.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from exmergo_dex_core import generated_tests as gt

#: Where the fixture models live in the project, apart from anything else.
FOLDER = "models/dex_parity"
#: Where the fixture snapshots live (#1458), under dbt's default snapshot path.
SNAPSHOT_FOLDER = "snapshots/dex_parity"


@dataclass(frozen=True)
class ParityRow:
    """One generated test held to ``maintain verify``.

    ``violating`` and ``clean`` render a SELECT over literal rows given the
    connector's spelling of a typed integer NULL, so one row serves every
    dialect. ``count`` reads, from the finding, the number the test's dbt
    ``failures`` must equal (``None`` where the finding states no count the
    test's ``fail_calc`` reproduces).
    """

    test: str
    codes: frozenset[str]
    declaration: Mapping[str, Any]
    violating: Callable[[str], str]
    clean: Callable[[str], str]
    count: Callable[[Mapping[str, Any]], int] | None = None
    #: Whether ``maintain verify``'s payload shows it measured the model at all,
    #: so a clean fixture's absent finding is a verdict and not a check that
    #: never ran (a refused statement, an unreadable relation). The payload is
    #: verify's ``data`` with its envelope's ``warnings`` under ``_warnings``.
    measured: Callable[[Mapping[str, Any], str], bool] | None = None
    #: The row's own name, where one test has several rows (#1453: an
    #: ``includes`` list and an ``excludes`` list); the test's name otherwise.
    name: str | None = None
    #: Undeclared models the row's fixtures read (#1454: the declared parent),
    #: by suffix: each is built as ``dex_parity_<slug>_<suffix>`` in the same
    #: selection, and the declaration names it so.
    fixtures: Mapping[str, Callable[[str], str]] = field(default_factory=dict)
    #: Arguments that pick this row's test out of several of one name on one
    #: model (#1454: a parent's ``missing`` and ``unexpected`` directions),
    #: matched against the rendered spec and the manifest's test kwargs.
    arguments: Mapping[str, Any] = field(default_factory=dict)
    #: The reason the plan renders no test for this row on any connector
    #: (#1455: a ``percent`` column is ``heuristic_only``). Such a row is judged
    #: as an unsupported dialect is: the plan lists both fixtures
    #: ``not_rendered`` with this reason, no test builds, and ``maintain
    #: verify`` alone answers.
    withheld: str | None = None
    #: A dbt test a declaration renders on every plan, on every dialect, with
    #: no ``declarations.render_tests`` switch (#1459: ``relationships`` from a
    #: ``references``), rather than one of Dex's generated kinds.
    builtin: bool = False
    #: Declarations for fixtures, by suffix (#1457: the children whose
    #: ``references`` imply the row's test on the parent fixtures). A fixture
    #: not named here stays undeclared.
    declared_fixtures: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    #: Where ``maintain verify``'s finding sits, by side, where it is not the
    #: model the test sits on (#1457: the test is on the parent, ``row_fanout``
    #: on the child that joins it): each side names a fixture suffix.
    finding_on: Mapping[str, str] = field(default_factory=dict)
    #: Whether the finding follows from the violation but not the reverse
    #: (#1457: a repeated parent key fans out a child, and a fan-out has other
    #: causes). The violating fixture must still fail the test and get the
    #: finding; the clean one need only pass the test.
    directional: bool = False
    #: dbt snapshots the row's fixtures read (#1458: the snapshot a declared
    #: history is verified against), by suffix: each renders the SQL between
    #: ``{% snapshot dex_parity_<slug>_<suffix> %}`` and ``{% endsnapshot %}``
    #: (its ``config()`` included) and is written under ``snapshots/``. It is
    #: built in the same selection; ``maintain verify`` is not pointed at it,
    #: since it judges models.
    snapshots: Mapping[str, Callable[[str], str]] = field(default_factory=dict)
    #: The failures the violating fixture's test must report, for a test with
    #: no ``maintain verify`` twin (#1461), whose ``codes`` are empty: the pair
    #: is then judged on dbt's answer alone.
    failures: int | None = None

    @property
    def label(self) -> str:
        return self.name or self.test

    @property
    def slug(self) -> str:
        return self.label.removeprefix("dex_")

    def model(self, side: str) -> str:
        return f"dex_parity_{self.slug}_{side}"

    def judged(self, side: str) -> str:
        """The model ``maintain verify``'s finding is read on for ``side``."""

        suffix = self.finding_on.get(side)
        return self.model(suffix) if suffix is not None else self.model(side)

    def picks(self, arguments: Mapping[str, Any] | None) -> bool:
        """Whether a test with ``arguments`` is this row's test."""

        arguments = arguments or {}
        return all(arguments.get(k) == v for k, v in self.arguments.items())


def _rows(*rows: tuple[str, str]) -> str:
    """A SELECT over ``rows`` as (order_id, line_number). The first row names
    the columns, so it never holds a NULL whose type the warehouse would guess."""

    (order_id, line_number), *rest = rows
    lines = [f"select {order_id} as order_id, {line_number} as line_number"]
    lines += [f"union all select {a}, {b}" for a, b in rest]
    return "\n".join(lines) + "\n"


def _grain_measured(verify: Mapping[str, Any], model: str) -> bool:
    return any(
        record.get("model") == model and record.get("status") in {"proven", "broken"}
        for record in verify.get("grain_coverage") or []
    )


#: #1452: a composite grain. Three rows share one grain value and two share
#: another through a NULL in a grain column, which both sides count as a value
#: (``SELECT DISTINCT`` and ``GROUP BY`` alike). The violating model has three
#: rows beyond the first of each value; a macro counting duplicate *groups*
#: would say two, so the count comparison tells the two apart.
UNIQUE_COMBINATION = ParityRow(
    test=gt.UNIQUE_COMBINATION,
    codes=frozenset({"grain_broken"}),
    declaration={
        "description": "Parity fixture: one row per order line.",
        "grain": ["order_id", "line_number"],
    },
    violating=lambda null: _rows(
        ("1", "1"),
        ("1", "1"),
        ("1", "1"),
        ("1", "2"),
        ("2", null),
        ("2", null),
        ("3", "1"),
    ),
    clean=lambda null: _rows(("1", "1"), ("1", "2"), ("2", null), ("3", "1")),
    count=lambda finding: int(finding["data"]["duplicate_count"]),
    measured=_grain_measured,
)


def _statuses(*rows: tuple[str, str]) -> str:
    """A SELECT over ``rows`` as (id, status). ``None`` writes a NULL status,
    spelled ``nullif('x', 'x')``: a text NULL every dialect types from the
    expression, with no cast to spell per warehouse. The first row names the
    columns, so it is never NULL."""

    def value(status: str | None) -> str:
        return "nullif('x', 'x')" if status is None else status

    (first_id, first), *rest = rows
    lines = [f"select {first_id} as id, {value(first)} as status"]
    lines += [f"union all select {i}, {value(v)}" for i, v in rest]
    return "\n".join(lines) + "\n"


def _population_measured(verify: Mapping[str, Any], model: str) -> bool:
    """``maintain verify`` ran the population check over ``model``: the class
    is not suppressed (it is, when no model's filter could be measured) and no
    warning names this model's population, or its whole declared-facts
    statement, as unmeasured (a column the relation lacks, a refused
    statement). Another model's measured filter keeps the class unsuppressed,
    so the per-model warnings are what tell this model apart."""

    if "population" in (verify.get("suppressed") or {}):
        return False
    unmeasured = (
        f"declared population on {model} ",
        f"declared facts on {model} could not be measured",
    )
    return not any(
        marker in str(line)
        for line in verify.get("_warnings") or []
        for marker in unmeasured
    )


def _contradicting_rows(finding: Mapping[str, Any]) -> int:
    return int(finding["data"]["contradicting_rows"])


def _population_row(name: str, filters: dict, violating: str, clean: str) -> ParityRow:
    return ParityRow(
        test=gt.POPULATION_FILTER,
        name=name,
        codes=frozenset({"population_contradicted"}),
        declaration={
            "description": "Parity fixture: one row per order.",
            "population": {"filters": [{"column": "status", **filters}]},
        },
        violating=lambda _null: violating,
        clean=lambda _null: clean,
        count=_contradicting_rows,
        measured=_population_measured,
    )


#: #1453: an ``includes`` list. One row holds a value outside it.
POPULATION_INCLUDES = _population_row(
    "dex_population_filter_includes",
    {"includes": ["paid", "shipped"]},
    _statuses(("1", "'paid'"), ("2", "'shipped'"), ("3", "'refunded'")),
    _statuses(("1", "'paid'"), ("2", "'shipped'")),
)

#: #1453: a NULL is outside an ``includes`` list, on both sides.
POPULATION_INCLUDES_NULL = _population_row(
    "dex_population_filter_includes_null",
    {"includes": ["paid", "shipped"]},
    _statuses(("1", "'paid'"), ("2", "'shipped'"), ("3", None)),
    _statuses(("1", "'paid'"), ("2", "'shipped'")),
)

#: #1453: an ``excludes`` list. Two rows hold the excluded value, so the count
#: is compared too, and a NULL that breaks no ``excludes`` list sits in both
#: fixtures: counted by either side, the clean fixture would fail. The list also
#: holds a quote and a backslash, which each warehouse writes in its own literal
#: syntax: a literal the warehouse cannot parse errors the test.
POPULATION_EXCLUDES = _population_row(
    "dex_population_filter_excludes",
    {"excludes": ["cancelled", "it's", "a\\b"]},
    _statuses(("1", "'paid'"), ("2", "'cancelled'"), ("3", "'cancelled'"), ("4", None)),
    _statuses(("1", "'paid'"), ("2", "'shipped'"), ("4", None)),
)

#: #1453: text values against a number column, compared as text on both sides
#: (the column cast to the warehouse's string type).
POPULATION_NUMERIC = _population_row(
    "dex_population_filter_numeric",
    {"excludes": ["7"]},
    _statuses(("1", "1"), ("2", "7"), ("3", "8")),
    _statuses(("1", "1"), ("2", "8")),
)


def _customers(null_int: str, *rows: tuple[str, str | None]) -> str:
    """A SELECT over ``rows`` as (customer_id, status), the first naming the
    columns. A ``None`` status is a text NULL, as :func:`_statuses` writes it;
    a ``None`` id is the connector's typed integer NULL."""

    def status(value: str | None) -> str:
        return "nullif('x', 'x')" if value is None else f"'{value}'"

    def ident(value: str | None) -> str:
        return null_int if value is None else value

    (first_id, first), *rest = rows
    lines = [f"select {ident(first_id)} as customer_id, {status(first)} as status"]
    lines += [f"union all select {ident(i)}, {status(v)}" for i, v in rest]
    return "\n".join(lines) + "\n"


def _parent_measured(verify: Mapping[str, Any], model: str) -> bool:
    """``maintain verify`` judged ``model``'s declared parent."""

    return any(
        record.get("model") == model
        and record.get("status") in {"satisfied", "contradicted"}
        for record in verify.get("parent_population") or []
    )


def _parent_row(
    name: str,
    direction: str,
    parent: Callable[[str], str],
    violating: Callable[[str], str],
    clean: Callable[[str], str],
    filters: list[dict] | None = None,
) -> ParityRow:
    """A ``dex_parent_population`` row: the parent is a fixture the row
    builds, and both the violating and the clean model declare it."""

    slug = name.removeprefix("dex_")
    population: dict[str, Any] = {
        "parent": {
            "model": f"dex_parity_{slug}_parent",
            "key": {"customer_id": "customer_id"},
        }
    }
    if filters:
        population["filters"] = filters
    code, measure = {
        "missing": ("parent_population_missing", "missing_parents"),
        "unexpected": ("parent_population_unexpected", "unexpected_keys"),
    }[direction]
    return ParityRow(
        test=gt.PARENT_POPULATION,
        name=name,
        codes=frozenset({code}),
        declaration={
            "description": "Parity fixture: one row per customer.",
            "population": population,
        },
        violating=violating,
        clean=clean,
        count=lambda finding: int(finding["data"][measure]),
        measured=_parent_measured,
        fixtures={"parent": parent},
        arguments={"direction": direction},
    )


def _parent(null: str) -> str:
    """The declared parent: 3 is churned, and 4's status is NULL."""

    return _customers(
        null, ("1", "active"), ("2", "active"), ("3", "churned"), ("4", None)
    )


#: #1454: a parent key the model lacks. Two are missing, so the count is
#: compared too.
PARENT_MISSING = _parent_row(
    "dex_parent_population_missing",
    "missing",
    _parent,
    lambda null: _customers(null, ("1", "active"), ("2", "active")),
    lambda null: _customers(
        null, ("1", "active"), ("2", "active"), ("3", "churned"), ("4", None)
    ),
)

#: #1454: keys the parent lacks; 5 twice is one distinct key, so the count is
#: two, and a NULL key in both fixtures is no key on either side.
PARENT_UNEXPECTED = _parent_row(
    "dex_parent_population_unexpected",
    "unexpected",
    _parent,
    lambda null: _customers(
        null,
        ("1", "active"),
        ("2", "active"),
        ("3", "churned"),
        ("4", None),
        ("5", "active"),
        ("5", "active"),
        ("6", "active"),
        (None, "active"),
    ),
    lambda null: _customers(
        null,
        ("1", "active"),
        ("2", "active"),
        ("3", "churned"),
        ("4", None),
        (None, "active"),
    ),
)

#: #1454: a declared ``excludes`` filter scopes the eligible parents. 3 is
#: churned and not owed; 4's NULL status is no excluded value, so 4 is owed.
PARENT_MISSING_FILTERED = _parent_row(
    "dex_parent_population_missing_filtered",
    "missing",
    _parent,
    lambda null: _customers(null, ("1", "active"), ("2", "active")),
    lambda null: _customers(null, ("1", "active"), ("2", "active"), ("4", None)),
    filters=[{"column": "status", "excludes": ["churned"]}],
)

#: #1454: a filter on a column the parent does not have is skipped on both
#: sides (maintain verify lowers its finding to medium; the test fails), so
#: the count is the parents the filter cannot rule out.
PARENT_MISSING_UNAPPLIED = _parent_row(
    "dex_parent_population_missing_unapplied",
    "missing",
    lambda _null: (
        "select 1 as customer_id, 'web' as channel\n"
        "union all select 2, 'web'\nunion all select 3, 'store'\n"
    ),
    lambda null: _customers(null, ("1", "active"), ("2", "active")),
    lambda null: _customers(null, ("1", "active"), ("2", "active"), ("3", "paid")),
    filters=[{"column": "status", "includes": ["active", "paid"]}],
)


def _shares(null_int: str, *values: str | None) -> str:
    """A SELECT over ``values`` as (id, share). A ``None`` share is the
    connector's typed NULL, never in the first row, which names the columns.
    Every value is written with a decimal point, so no warehouse has to find a
    supertype for an integer and a decimal in one UNION."""

    first, *rest = values
    assert first is not None
    lines = [f"select 1 as id, {first} as share"]
    lines += [
        f"union all select {i}, {null_int if v is None else v}"
        for i, v in enumerate(rest, start=2)
    ]
    return "\n".join(lines) + "\n"


def _unit_measured(verify: Mapping[str, Any], model: str) -> bool:
    """``maintain verify`` measured ``model``'s declared unit: the class is not
    suppressed and nothing it says names this model's unit, or its whole
    declared-facts statement, as unchecked (an all-NULL or text column, a
    column the relation lacks, a refused statement)."""

    if "unit" in (verify.get("suppressed") or {}):
        return False
    unmeasured = (
        f"declared unit of {model}.",
        f"declared unit on {model} ",
        f"declared facts on {model} could not be measured",
    )
    said = [*(verify.get("_warnings") or []), *(verify.get("notes") or [])]
    return not any(marker in str(line) for line in said for marker in unmeasured)


#: #1455: a column declared a fraction. Two values sit outside 0 to 1, one on
#: each side, so the count is compared too; the bounds 0 and 1 and a NULL sit in
#: both fixtures, and either side counting one would fail the clean fixture.
UNIT_FRACTION = ParityRow(
    test=gt.UNIT_RANGE,
    name="dex_unit_range_fraction",
    codes=frozenset({"unit_out_of_range"}),
    declaration={
        "description": "Parity fixture: one row per order.",
        "columns": {"share": {"unit": "fraction"}},
    },
    violating=lambda null: _shares(null, "0.5", "1.5", "-0.2", "0.0", "1.0", None),
    clean=lambda null: _shares(null, "0.5", "0.0", "1.0", None),
    count=lambda finding: int(finding["data"]["out_of_range_rows"]),
    measured=_unit_measured,
)

#: #1455: a column declared a percent renders no test (``heuristic_only``):
#: ``maintain verify`` flags one whose every value sits within 0 to 1 as a
#: question, which correct small percentages answer too. The plan says so on
#: both fixtures and ``maintain verify`` alone flags the 0-to-1 one.
UNIT_PERCENT = ParityRow(
    test=gt.UNIT_RANGE,
    name="dex_unit_range_percent",
    codes=frozenset({"unit_out_of_range"}),
    declaration={
        "description": "Parity fixture: one row per order.",
        "columns": {"share": {"unit": "percent"}},
    },
    violating=lambda null: _shares(null, "0.05", "0.12", "1.0", None),
    clean=lambda null: _shares(null, "5.0", "12.0", "100.0", None),
    measured=_unit_measured,
    withheld=gt.HEURISTIC_ONLY,
)


def _join_measured(verify: Mapping[str, Any], model: str) -> bool:
    """``maintain verify`` measured ``model``'s join for coverage."""

    return any(
        record.get("model") == model
        and record.get("status") in {"satisfied", "contradicted"}
        for record in verify.get("join_coverage") or []
    )


def _orders(null_int: str, *rows: tuple[str, str | None]) -> str:
    """A SELECT over ``rows`` as (order_id, customer_id), the first naming the
    columns; a ``None`` customer is the connector's typed integer NULL."""

    def ident(value: str | None) -> str:
        return null_int if value is None else value

    (first_id, first), *rest = rows
    lines = [f"select {first_id} as order_id, {ident(first)} as customer_id"]
    lines += [f"union all select {i}, {ident(c)}" for i, c in rest]
    return "\n".join(lines) + "\n"


def _left_join_orders(orders: str) -> Callable[[str], str]:
    """The fixture model: ``orders`` LEFT-joining the declared parent, so the
    join keeps an order whose customer the parent lacks."""

    def render(_null: str) -> str:
        return (
            "select o.order_id, o.customer_id, c.segment\n"
            f"from {{{{ ref('dex_parity_relationships_{orders}') }}}} o\n"
            "left join {{ ref('dex_parity_relationships_parent') }} c\n"
            "  on o.customer_id = c.customer_id\n"
        )

    return render


#: #1459: a `references` with no `role` on a LEFT-joined key renders dbt's
#: `relationships`, which fails on a non-null value the parent lacks, and
#: `maintain verify` holds the join to the same claim. Customer 9 is no
#: parent's, on two orders, so both count two rows; the clean model writes
#: those two references as NULL, which both accept.
RELATIONSHIPS_LEFT_JOIN = ParityRow(
    test="relationships",
    builtin=True,
    codes=frozenset({"join_orphans", "join_zero_overlap", "join_no_match"}),
    declaration={
        "description": "Parity fixture: one row per order.",
        "columns": {
            "customer_id": {"references": "dex_parity_relationships_parent.customer_id"}
        },
    },
    violating=_left_join_orders("orders_orphaned"),
    clean=_left_join_orders("orders_unreferenced"),
    count=lambda finding: int(finding["data"]["unmatched_rows"]),
    measured=_join_measured,
    fixtures={
        "parent": lambda _null: (
            "select 1 as customer_id, 'retail' as segment\n"
            "union all select 2, 'retail'\nunion all select 3, 'wholesale'\n"
        ),
        "orders_orphaned": lambda null: _orders(
            null,
            ("10", "1"),
            ("11", "2"),
            ("12", "3"),
            ("13", "9"),
            ("14", "9"),
            ("15", None),
        ),
        "orders_unreferenced": lambda null: _orders(
            null,
            ("10", "1"),
            ("11", "2"),
            ("12", "3"),
            ("13", None),
            ("14", None),
            ("15", None),
        ),
    },
)


def _join_coverage_parent(_null: str) -> str:
    return (
        "select 1 as customer_id, 'retail' as segment\n"
        "union all select 2, 'retail'\nunion all select 2, 'retail'\n"
        "union all select 3, 'wholesale'\n"
    )


def _joined(prefix: str, orders: str, side: str = "inner") -> Callable[[str], str]:
    """The fixture model: ``orders`` joined to the row's declared parent on
    ``customer_id``, an INNER join by default, which drops an order whose
    customer the parent lacks."""

    def render(_null: str) -> str:
        return (
            "select o.order_id, o.customer_id, c.segment\n"
            f"from {{{{ ref('dex_parity_{prefix}_{orders}') }}}} o\n"
            f"{side} join {{{{ ref('dex_parity_{prefix}_parent') }}}} c\n"
            "  on o.customer_id = c.customer_id\n"
        )

    return render


def _join_row(
    name: str,
    orphaned: tuple[tuple[str, str | None], ...],
    *,
    side: str = "inner",
    withheld: str | None = None,
) -> ParityRow:
    """A ``dex_join_coverage`` row (#1456): the violating fixture joins orders
    holding ``orphaned``, the clean one orders whose every non-null customer
    the parent has, a NULL among them, which both sides leave out (a missing
    reference). The parent holds customer 2 twice, which must multiply
    nothing."""

    slug = name.removeprefix("dex_")
    return ParityRow(
        test=gt.JOIN_COVERAGE,
        name=name,
        codes=frozenset({"join_orphans", "join_zero_overlap", "join_no_match"}),
        declaration={
            "description": "Parity fixture: one row per matched order.",
            "columns": {
                "customer_id": {"references": f"dex_parity_{slug}_parent.customer_id"}
            },
        },
        violating=_joined(slug, "orders_orphaned", side),
        clean=_joined(slug, "orders_matched", side),
        count=lambda finding: int(finding["data"]["unmatched_rows"]),
        measured=_join_measured,
        withheld=withheld,
        fixtures={
            "parent": _join_coverage_parent,
            "orders_orphaned": lambda null: _orders(null, *orphaned),
            "orders_matched": lambda null: _orders(
                null, ("10", "1"), ("11", "2"), ("12", "3"), ("13", None)
            ),
        },
    )


#: #1456: an INNER join a `references` requires drops the two orders of
#: customer 9, which no parent row holds; the output shows no orphan, so only a
#: test reading the join's inputs agrees with `join_orphans`. The NULL customer
#: is a missing reference on both sides.
JOIN_COVERAGE_INNER = _join_row(
    gt.JOIN_COVERAGE,
    (("10", "1"), ("11", "2"), ("12", "3"), ("13", "9"), ("14", "9"), ("15", None)),
)
#: #1456: nothing matches, `join_no_match`, and the test fails with every
#: eligible row.
JOIN_COVERAGE_NO_MATCH = _join_row(
    "dex_join_coverage_no_match", (("10", "7"), ("11", "8"), ("12", None))
)
#: #1456: a required LEFT join renders no test, since `relationships` counts
#: the same rows on its output; `maintain verify` alone answers for it here.
JOIN_COVERAGE_LEFT = _join_row(
    "dex_join_coverage_left",
    (("10", "1"), ("11", "9")),
    side="left",
    withheld=gt.COVERED_BY_BUILTIN,
)


def _statused_orders(null_int: str, *rows: tuple[str, str | None, str]) -> str:
    """``_orders`` with a ``status`` column."""

    def ident(value: str | None) -> str:
        return null_int if value is None else value

    (first_id, first, first_status), *rest = rows
    lines = [
        f"select {first_id} as order_id, {ident(first)} as customer_id, "
        f"'{first_status}' as status"
    ]
    lines += [f"union all select {i}, {ident(c)}, '{st}'" for i, c, st in rest]
    return "\n".join(lines) + "\n"


def _filtered(orders: str) -> Callable[[str], str]:
    """The fixture model reading its orders through a staging CTE's filter."""

    def render(_null: str) -> str:
        return (
            "with orders as (\n"  # noqa: S608 - a fixture model's SQL
            "    select * from "
            f"{{{{ ref('dex_parity_join_coverage_filtered_{orders}') }}}}\n"
            "    where status <> 'cancelled'\n"
            ")\n"
            "select orders.order_id, orders.customer_id, c.segment\n"
            "from orders\n"
            "join {{ ref('dex_parity_join_coverage_filtered_parent') }} c\n"
            "  on orders.customer_id = c.customer_id\n"
        )

    return render


#: #1456: the driving input is read through a staging CTE's filter. Customer 8
#: is no parent's, on a cancelled order the filter drops before the join, so
#: neither side counts it; customer 9, on a kept order, is the one orphan.
JOIN_COVERAGE_FILTERED = ParityRow(
    test=gt.JOIN_COVERAGE,
    name="dex_join_coverage_filtered",
    codes=JOIN_COVERAGE_INNER.codes,
    declaration={
        "description": "Parity fixture: one row per kept, matched order.",
        "columns": {
            "customer_id": {
                "references": "dex_parity_join_coverage_filtered_parent.customer_id"
            }
        },
    },
    violating=_filtered("orders_orphaned"),
    clean=_filtered("orders_matched"),
    count=lambda finding: int(finding["data"]["unmatched_rows"]),
    measured=_join_measured,
    fixtures={
        "parent": _join_coverage_parent,
        "orders_orphaned": lambda null: _statused_orders(
            null,
            ("10", "1", "paid"),
            ("11", "9", "paid"),
            ("12", "8", "cancelled"),
            ("13", None, "paid"),
        ),
        "orders_matched": lambda null: _statused_orders(
            null,
            ("10", "1", "paid"),
            ("11", "3", "paid"),
            ("12", "8", "cancelled"),
            ("13", None, "paid"),
        ),
    },
)


def _referenced_parent(null_int: str, repeated: bool) -> str:
    """A parent fixture as (customer_id, segment): customer 2 twice where
    ``repeated``, and two NULL keys in both, which are no key and repeat
    nothing."""

    lines = [
        "select 1 as customer_id, 'retail' as segment",
        "union all select 2, 'retail'",
    ]
    if repeated:
        lines.append("union all select 2, 'retail'")
    lines += [
        "union all select 3, 'wholesale'",
        f"union all select {null_int}, 'unknown'",
        f"union all select {null_int}, 'unknown'",
    ]
    return "\n".join(lines) + "\n"


def _referencing(side: str) -> Callable[[str], str]:
    """A child fixture LEFT-joining the orders to the ``side`` parent, so a
    repeated parent key gives an order two rows."""

    def render(_null: str) -> str:
        return (
            "select o.order_id, o.customer_id, c.segment\n"
            "from {{ ref('dex_parity_referenced_key_orders') }} o\n"
            f"left join {{{{ ref('dex_parity_referenced_key_{side}') }}}} c\n"
            "  on o.customer_id = c.customer_id\n"
        )

    return render


def _references_parent(side: str) -> dict[str, Any]:
    return {
        "description": "Parity fixture: one row per order, with its segment.",
        "columns": {
            "customer_id": {
                "references": f"dex_parity_referenced_key_{side}.customer_id"
            }
        },
    }


#: #1457: a `references` implies its parent column is unique. The test sits on
#: the parent fixtures; `row_fanout` on the child joining each. Customer 2 is
#: on the violating parent twice, so the order of customer 2 gets two rows (5
#: rows against 4 orders, +25%). Directional: fan-out has other causes, so the
#: clean arm asserts only that the test passes, and no count is compared (the
#: finding counts the child's rows, the test the parent's).
REFERENCED_KEY = ParityRow(
    test=gt.REFERENCED_KEY,
    codes=frozenset({"row_fanout"}),
    declaration={"description": "Parity fixture: one row per customer."},
    violating=lambda null: _referenced_parent(null, repeated=True),
    clean=lambda null: _referenced_parent(null, repeated=False),
    fixtures={
        # The orders read a relation: maintain verify does not compare a model
        # driven off a SELECT of literals (#988), which supplies anchors.
        "orders_raw": lambda null: _orders(
            null, ("10", "1"), ("11", "2"), ("12", "3"), ("13", None)
        ),
        "orders": lambda _null: (
            "select order_id, customer_id\n"
            "from {{ ref('dex_parity_referenced_key_orders_raw') }}\n"
        ),
        "child_violating": _referencing("violating"),
        "child_clean": _referencing("clean"),
    },
    declared_fixtures={
        "child_violating": _references_parent("violating"),
        "child_clean": _references_parent("clean"),
    },
    finding_on={"violating": "child_violating", "clean": "child_clean"},
    directional=True,
)


def _t(day: int) -> str:
    """A timestamp literal every connector reads the same way."""

    return f"cast('2024-01-0{day} 00:00:00' as timestamp)"


#: #1458: the versions a status-history snapshot recorded, one row per version
#: (``version_id`` is the snapshot's key, so one snapshot run records them all,
#: and the timestamp strategy copies ``changed_at`` into ``dbt_valid_from``,
#: which orders them). Host 1 goes basic, super, basic, super: three
#: transitions, the last into super on day 4. Host 2 is super twice: no
#: transition under a baseline first observation, first super on day 1. Host 3
#: is basic only.
_VERSIONS = [
    (1, 1, "basic", 1),
    (2, 1, "super", 2),
    (3, 1, "basic", 3),
    (4, 1, "super", 4),
    (5, 2, "super", 1),
    (6, 2, "super", 2),
    (7, 3, "basic", 1),
]


def _versions(_null: str) -> str:
    (vid, host, tier, day), *rest = _VERSIONS
    lines = [
        f"select {vid} as version_id, {host} as host_id, '{tier}' as tier, "
        f"{_t(day)} as changed_at"
    ]
    lines += [f"union all select {v}, {h}, '{t}', {_t(d)}" for v, h, t, d in rest]
    return "\n".join(lines) + "\n"


def _history_snapshot(prefix: str) -> Callable[[str], str]:
    def render(_null: str) -> str:
        return (
            "{{ config(unique_key='version_id', strategy='timestamp', "
            "updated_at='changed_at') }}\n"
            f"select * from {{{{ ref('{prefix}_versions') }}}}\n"
        )

    return render


def _per_host(
    prefix: str, column: str, values: Mapping[int, str]
) -> Callable[[str], str]:
    """A model with one row per host in the snapshot, ``column`` given per
    host by ``values`` (NULL for a host not listed)."""

    def render(_null: str) -> str:
        cases = " ".join(f"when {host} then {value}" for host, value in values.items())
        return (
            f"select h.host_id, case h.host_id {cases} end as {column}\n"  # noqa: S608
            f"from (select distinct host_id from {{{{ ref('{prefix}_snap') }}}}) h\n"
        )

    return render


def _temporal_measured(measure: str) -> Callable[[Mapping[str, Any], str], bool]:
    def measured(verify: Mapping[str, Any], model: str) -> bool:
        for record in verify.get("temporal_contracts") or []:
            if record.get("model") != model:
                continue
            for assertion in record.get("assertions") or []:
                if assertion.get("assertion") == measure:
                    return assertion.get("status") in {"satisfied", "contradicted"}
        return False

    return measured


def _temporal_row(
    name: str, measure: str, column: str, violating: dict, clean: dict
) -> ParityRow:
    prefix = f"dex_parity_{name.removeprefix('dex_')}"
    return ParityRow(
        test=gt.TEMPORAL_CONTRACT,
        name=name,
        codes=frozenset({_finding_code(measure)}),
        declaration={
            "description": "Parity fixture: one row per host.",
            "history": {
                "snapshot": f"{prefix}_snap",
                "entity": {"host_id": "host_id"},
                "status": {"column": "tier", "target": "super"},
                "initial_observation": "baseline",
                "null_status": "ignore",
                "population": "all_observed",
                # The timestamp strategy copies the source's time, so it is
                # business time; `capture` would be contradicted.
                "time": {"column": "dbt_valid_from", "provenance": "business"},
                "measures": {measure: column},
            },
        },
        violating=_per_host(prefix, column, violating),
        clean=_per_host(prefix, column, clean),
        count=lambda finding: int(finding["data"]["mismatched_entities"]),
        measured=_temporal_measured(measure),
        fixtures={"versions": _versions},
        snapshots={"snap": _history_snapshot(prefix)},
        arguments={"measure": measure},
    )


def _finding_code(measure: str) -> str:
    from exmergo_dex_core.maintain.temporal_contracts import _MEASURE_FINDINGS

    return _MEASURE_FINDINGS[measure]


#: #1458: transition counts. The violating model counts host 1's four versions
#: and host 2's invented prior state (a baseline first observation is no
#: transition): two entities wrong, which a macro returning a flag would not
#: count.
TEMPORAL_TRANSITIONS = _temporal_row(
    "dex_temporal_transitions",
    "transition_count",
    "transitions",
    violating={1: "4", 2: "1", 3: "0"},
    clean={1: "3", 2: "0", 3: "0"},
)

#: #1458: the latest transition into the target, a timestamp each warehouse
#: types its own way (the cast is where a dialect goes wrong). Host 1's last
#: transition into super is day 4; host 2 never transitioned, so NULL. The
#: violating model gives host 1 day 3 and host 2 day 1.
TEMPORAL_LATEST = _temporal_row(
    "dex_temporal_latest",
    "latest_target_transition_at",
    "latest_super_at",
    violating={1: _t(3), 2: _t(1)},
    clean={1: _t(4)},
)


def _freshness_measured(verify: Mapping[str, Any], model: str) -> bool:
    """``maintain verify`` measured ``model``'s declared freshness."""

    return any(
        record.get("model") == model
        and record.get("status") in {"satisfied", "contradicted"}
        for record in verify.get("freshness") or []
    )


def _loaded_at(expression: str, *, view: bool = False, empty: bool = False):
    """A model with one ``loaded_at`` row, ``expression`` in dbt's Jinja so each
    warehouse spells its own clock (``dbt.current_timestamp()``). A view reads
    the clock of the statement reading it, so a value computed from it sits at
    the same lag whenever the test and ``maintain verify`` read it."""

    def render(_null: str) -> str:
        head = "{{ config(materialized='view') }}\n" if view else ""
        tail = " where 1 = 0" if empty else ""
        return f"{head}select 1 as id, {expression} as loaded_at{tail}\n"

    return render


def _ago(seconds: int) -> str:
    return f"{{{{ dbt.dateadd('second', -{seconds}, dbt.current_timestamp()) }}}}"


_NOW = "{{ dbt.current_timestamp() }}"


def _freshness_row(name: str, violating, clean) -> ParityRow:
    return ParityRow(
        test=gt.FRESHNESS,
        name=name,
        codes=frozenset({"freshness_exceeded"}),
        declaration={
            "description": "Parity fixture: one row per load.",
            "columns": {
                "loaded_at": {
                    "role": "time",
                    "freshness": {"max_lag": "6h", "basis": "capture"},
                }
            },
        },
        violating=violating,
        clean=clean,
        count=lambda finding: int(finding["data"]["stale"]),
        measured=_freshness_measured,
    )


#: #1460: a load that stopped long ago fails both; one a few minutes old
#: passes both.
FRESHNESS_STALE = _freshness_row(
    "dex_freshness_stale",
    violating=_loaded_at("cast('2000-01-01 00:00:00' as timestamp)"),
    clean=_loaded_at(_ago(300)),
)

#: #1460: the boundary. Views over the warehouse's clock sit exactly at the
#: 6h limit (passes: the limit is inclusive) and one second past it (fails),
#: whenever each side reads them; a check flooring or rounding its two epoch
#: seconds differently, or reading the runner's clock, disagrees here.
FRESHNESS_AT_LIMIT = _freshness_row(
    "dex_freshness_at_limit",
    violating=_loaded_at(_ago(21601), view=True),
    clean=_loaded_at(_ago(21600), view=True),
)

#: #1460: no rows is not fresh. The empty model fails both; a fresh one beside
#: it passes both.
FRESHNESS_EMPTY = _freshness_row(
    "dex_freshness_empty",
    violating=_loaded_at(_NOW, empty=True),
    clean=_loaded_at(_NOW),
)


def _values(column: str, *rows: tuple[str, str]) -> str:
    """A SELECT over ``rows`` as (region, ``column``), each value a SQL literal."""

    (region, value), *rest = rows
    lines = [f"select '{region}' as region, {value} as {column}"]
    lines += [f"union all select '{r}', {v}" for r, v in rest]
    return "\n".join(lines) + "\n"


def _date(text: str) -> str:
    return f"cast('{text}' as date)"


def _ts(text: str) -> str:
    return f"cast('{text}' as timestamp)"


def _continuity_row(
    name: str,
    continuity: dict[str, Any],
    column: str,
    violating: tuple[tuple[str, str], ...],
    clean: tuple[tuple[str, str], ...],
    failures: int,
) -> ParityRow:
    return ParityRow(
        test=gt.CONTINUITY,
        name=name,
        codes=frozenset(),
        declaration={
            "description": "Parity fixture: one row per order.",
            "columns": {column: {"role": "time", "continuity": continuity}},
        },
        violating=lambda _null: _values(column, *violating),
        clean=lambda _null: _values(column, *clean),
        failures=failures,
    )


#: #1461: a date column searched by the day. The violating model misses the
#: 3rd and the 5th and 6th: two gaps (five buckets are empty), so a macro
#: counting empty buckets says 3. The clean model's data stops long before
#: today and starts after the 1st: an edge is no gap.
CONTINUITY_DAY = _continuity_row(
    "dex_continuity_day",
    {"grain": "day"},
    "ordered_on",
    violating=tuple(("a", _date(f"2024-03-0{d}")) for d in (1, 2, 4, 7)),
    clean=tuple(("a", _date(f"2024-03-0{d}")) for d in (2, 3, 4, 5, 6)),
    failures=2,
)

#: #1461: per key. In the violating model region b misses the 2nd, which
#: region a has, so the whole table has no gap and b has one. In the clean
#: model b starts on the 2nd: a key's own edge is no gap either.
CONTINUITY_BY = _continuity_row(
    "dex_continuity_by",
    {"grain": "day", "by": ["region"]},
    "ordered_at",
    violating=(
        ("a", _ts("2024-03-01 09:00:00")),
        ("a", _ts("2024-03-02 09:00:00")),
        ("a", _ts("2024-03-03 09:00:00")),
        ("b", _ts("2024-03-01 23:59:59")),
        ("b", _ts("2024-03-03 00:00:00")),
    ),
    clean=(
        ("a", _ts("2024-03-01 09:00:00")),
        ("a", _ts("2024-03-02 09:00:00")),
        ("a", _ts("2024-03-03 09:00:00")),
        ("b", _ts("2024-03-02 00:00:00")),
        ("b", _ts("2024-03-03 23:59:59")),
    ),
    failures=1,
)

#: #1461: the ISO week starts on Monday, on every dialect. Sunday 7 January
#: 2024 and Monday 15 January leave the ISO week of the 8th empty (a
#: Sunday-start week would see none); Monday 8 January and Sunday 21 January
#: sit in consecutive ISO weeks (a Sunday-start week would see one).
CONTINUITY_WEEK = _continuity_row(
    "dex_continuity_week",
    {"grain": "week"},
    "ordered_on",
    violating=(("a", _date("2024-01-07")), ("a", _date("2024-01-15"))),
    clean=(("a", _date("2024-01-08")), ("a", _date("2024-01-21"))),
    failures=1,
)

#: #1461: a calendar grain across a year boundary. December and February
#: leave January empty; the last hour of 2023 and the first of 2024 are two
#: consecutive months, in UTC.
CONTINUITY_MONTH = _continuity_row(
    "dex_continuity_month",
    {"grain": "month"},
    "ordered_at",
    violating=(
        ("a", _ts("2023-12-15 12:00:00")),
        ("a", _ts("2024-02-10 12:00:00")),
        ("a", _ts("2024-03-01 00:00:00")),
    ),
    clean=(
        ("a", _ts("2023-12-31 23:00:00")),
        ("a", _ts("2024-01-01 01:00:00")),
        ("a", _ts("2024-02-29 12:00:00")),
    ),
    failures=1,
)

#: #1461: a grain finer than a day, on a timestamp. 10:00 and 10:59:59 are one
#: hour, and 12:00 leaves 11 empty; 10:59:59, 11:00:00 and 12:30 are three
#: consecutive hours.
CONTINUITY_HOUR = _continuity_row(
    "dex_continuity_hour",
    {"grain": "hour"},
    "ordered_at",
    violating=(
        ("a", _ts("2024-03-01 10:00:00")),
        ("a", _ts("2024-03-01 10:59:59")),
        ("a", _ts("2024-03-01 12:00:00")),
    ),
    clean=(
        ("a", _ts("2024-03-01 10:59:59")),
        ("a", _ts("2024-03-01 11:00:00")),
        ("a", _ts("2024-03-01 12:30:00")),
    ),
    failures=1,
)


#: Every row the harness runs, in the order the generated tests shipped.
ROWS: tuple[ParityRow, ...] = (
    UNIQUE_COMBINATION,
    POPULATION_INCLUDES,
    POPULATION_INCLUDES_NULL,
    POPULATION_EXCLUDES,
    POPULATION_NUMERIC,
    PARENT_MISSING,
    PARENT_UNEXPECTED,
    PARENT_MISSING_FILTERED,
    PARENT_MISSING_UNAPPLIED,
    UNIT_FRACTION,
    UNIT_PERCENT,
    RELATIONSHIPS_LEFT_JOIN,
    JOIN_COVERAGE_INNER,
    JOIN_COVERAGE_NO_MATCH,
    JOIN_COVERAGE_FILTERED,
    JOIN_COVERAGE_LEFT,
    REFERENCED_KEY,
    TEMPORAL_TRANSITIONS,
    TEMPORAL_LATEST,
    FRESHNESS_STALE,
    FRESHNESS_AT_LIMIT,
    FRESHNESS_EMPTY,
    CONTINUITY_DAY,
    CONTINUITY_BY,
    CONTINUITY_WEEK,
    CONTINUITY_MONTH,
    CONTINUITY_HOUR,
)

#: The finding codes no row may raise on a clean model, whichever row it is.
ALL_CODES: frozenset[str] = frozenset().union(*(row.codes for row in ROWS))


@dataclass
class ParityReport:
    """What one connector's run established. ``disagreements`` is empty when
    every row agreed; ``unsupported`` names the rows whose test declares the
    connector unsupported and rendered nothing there, as it must, and
    ``withheld`` the rows whose fact renders no test on any connector (#1455),
    each held to ``maintain verify`` alone."""

    connector: str | None
    disagreements: list[str] = field(default_factory=list)
    agreed: list[str] = field(default_factory=list)
    unsupported: list[str] = field(default_factory=list)
    withheld: list[str] = field(default_factory=list)
    models: list[str] = field(default_factory=list)
    #: The fixture snapshots built (#1458): tables, where ``models`` are the
    #: connector's default materialization, so a caller drops them apart.
    snapshots: list[str] = field(default_factory=list)
    envelopes: dict[str, Any] = field(default_factory=dict)
    #: Per command: seconds on the wall clock and the envelope's ``cost``, so a
    #: billed job can say what the harness spent (#1451).
    spend: dict[str, dict[str, Any]] = field(default_factory=dict)

    def record(self, step: str, envelope: Mapping[str, Any], seconds: float) -> None:
        self.envelopes[step] = envelope
        self.spend[step] = {
            "seconds": round(seconds, 1),
            "status": envelope.get("status"),
            "cost": envelope.get("cost"),
        }


Runner = Callable[[list[str]], tuple[int, dict]]


def payload(
    rows: tuple[ParityRow, ...] = ROWS,
    *,
    null_int: str = "cast(null as integer)",
    extra_edits: list[dict] | None = None,
) -> dict:
    """The edits payload declaring both fixture models of every row."""

    edits: list[dict] = list(extra_edits or [])
    declarations: list[dict] = []
    for row in rows:
        for suffix, render in row.snapshots.items():
            name = row.model(suffix)
            edits.append(
                {
                    "path": f"{SNAPSHOT_FOLDER}/{name}.sql",
                    "kind": "snapshot_sql",
                    "content": (
                        f"{{% snapshot {name} %}}\n{render(null_int)}"
                        "{% endsnapshot %}\n"
                    ),
                }
            )
        for suffix, render in row.fixtures.items():
            edits.append(
                {
                    "path": f"{FOLDER}/{row.model(suffix)}.sql",
                    "kind": "model_sql",
                    "content": render(null_int),
                }
            )
            if suffix in row.declared_fixtures:
                declarations.append(
                    {"model": row.model(suffix), **row.declared_fixtures[suffix]}
                )
        for side, render in (("violating", row.violating), ("clean", row.clean)):
            name = row.model(side)
            edits.append(
                {
                    "path": f"{FOLDER}/{name}.sql",
                    "kind": "model_sql",
                    "content": render(null_int),
                }
            )
            declarations.append({"model": name, **row.declaration})
    return {"edits": edits, "declarations": declarations}


def switch_on(repo: Path, rows: tuple[ParityRow, ...] = ROWS) -> None:
    """Turn on exactly the rows' generated tests in ``.dex/config.yml``."""

    config_path = repo / ".dex" / "config.yml"
    config: dict[str, Any] = {}
    if config_path.is_file():
        config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    config.setdefault("declarations", {})["render_tests"] = sorted(
        {row.test for row in rows if not row.builtin}
    )
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")


def _paid(budget: float | None) -> list[str]:
    return [] if budget is None else ["--confirm", "--budget", str(budget)]


def run_parity(
    run: Runner,
    repo: Path,
    project: Path | None = None,
    *,
    connector: str | None,
    budget: float | None = None,
    rows: tuple[ParityRow, ...] = ROWS,
    null_int: str = "cast(null as integer)",
    extra_edits: list[dict] | None = None,
) -> ParityReport:
    """Run every row on the connector ``repo``'s ``.dex/config.yml`` names.

    ``project`` is the dbt project directory (its ``target/`` holds the run
    results the test's failure count is read from), by default the one
    directory under ``repo`` holding a ``dbt_project.yml``. ``budget`` is the
    connector's magnitude for ``--confirm --budget`` on a metered connector,
    ``None`` on a free one. ``extra_edits`` ride in the same plan: a planted
    per-dialect macro, which is how the harness is shown to catch a broken
    macro.
    """

    report = ParityReport(connector=connector)

    def step(name: str, argv: list[str]) -> tuple[int, dict]:
        started = time.monotonic()
        rc, envelope = run(argv)
        report.record(name, envelope, time.monotonic() - started)
        return rc, envelope

    if project is None:
        [project] = [p.parent for p in repo.glob("*/dbt_project.yml")]
    root = ["--repo-root", str(repo)]
    switch_on(repo, rows)
    edits_file = repo / "dex_parity_edits.json"
    edits_file.write_text(
        json.dumps(payload(rows, null_int=null_int, extra_edits=extra_edits)),
        encoding="utf-8",
    )
    rc, planned = step(
        "plan",
        [
            *root,
            "transform",
            "plan",
            "parity fixtures",
            "--edits-file",
            str(edits_file),
        ],
    )
    if rc != 0 or planned["status"] != "ok":
        report.disagreements.append(f"the plan failed: {planned.get('errors')}")
        return report
    listed = (planned["data"].get("generated_tests") or {}).get("tests") or []

    live: list[ParityRow] = []
    for row in rows:
        if row.builtin:
            # dbt's own test, rendered on every dialect: the build's manifest
            # is where it is found, as for any other row.
            live.append(row)
            continue
        kind = next(k for k in gt.kinds() if k.name == row.test)
        models = {row.model("violating"), row.model("clean")}
        claims = _claims(row, kind)
        mine = [
            t
            for t in listed
            if t["test"] == row.test
            and t["model"] in models
            and (claims is None or t["claim"] in claims)
        ]
        if row.withheld is None and gt.supports(kind, connector):
            if sorted(t["status"] for t in mine) != ["added", "added"]:
                report.disagreements.append(
                    f"{row.label}: the plan did not render the test on both "
                    f"fixtures: {mine}"
                )
                continue
            live.append(row)
            continue
        # #1451: a dialect the test does not support renders nothing there;
        # #1455: nor does a fact only maintain verify judges, on any dialect.
        reason = gt.DIALECT_UNSUPPORTED if row.withheld is None else row.withheld
        statuses = {(t["status"], t.get("reason")) for t in mine}
        # A withheld row shares its kind with rows that render it, so the
        # diffs cannot tell; the manifest check in _judge_unsupported does.
        rendered = row.withheld is None and any(
            row.test in d.get("diff", "") for d in planned.get("diffs", [])
        )
        if statuses != {("not_rendered", reason)} or len(mine) != 2 or rendered:
            report.disagreements.append(
                f"{row.label}: the plan should render nothing ({reason}), yet it "
                f"says {mine}"
            )
        elif row.withheld is not None:
            report.withheld.append(row.label)
        else:
            report.unsupported.append(row.label)
        report.models.extend(sorted(models))

    rc, applied = step("apply", [*root, "transform", "apply"])
    if rc != 0:
        report.disagreements.append(f"apply failed: {applied.get('errors')}")
        return report
    selected = [
        row.model(side)
        for row in rows
        for side in (*row.fixtures, "violating", "clean")
    ]
    report.models = sorted(set(report.models) | set(selected))
    snapshots = [row.model(suffix) for row in rows for suffix in row.snapshots]
    report.snapshots = sorted(set(snapshots))

    _rc, built = step(
        "build",
        [
            *root,
            "transform",
            "build",
            "--target",
            "dev",
            "--select",
            " ".join([*snapshots, *selected]),
            *_paid(budget),
        ],
    )
    results, tests = _artifacts(project)
    model_status = {
        n["name"]: n["status"] for n in (built.get("data") or {}).get("nodes", [])
    }
    # A row whose test sits on a parent fixture (#1457) fails it there, and
    # dbt build skips every model downstream of a failed test. The whole
    # selection is built again with its tests left out (dbt's
    # DBT_INDIRECT_SELECTION=empty), so every model builds and the manifest
    # holds every model's compiled SQL for verify to read; a narrower
    # selection would leave the other rows' models uncompiled there. The test
    # results are the first pass's, read above. A snapshot is not built again:
    # its first pass stands, and a second would MERGE into it (which Trino's
    # memory catalog cannot).
    snapshot_status = {name: model_status.get(name) for name in snapshots}
    skipped = [
        name for name in dict.fromkeys(selected) if model_status.get(name) == "skipped"
    ]
    if skipped:
        previous = os.environ.get("DBT_INDIRECT_SELECTION")
        os.environ["DBT_INDIRECT_SELECTION"] = "empty"
        try:
            _rc, rebuilt = step(
                "build_models",
                [
                    *root,
                    "transform",
                    "build",
                    "--target",
                    "dev",
                    "--select",
                    " ".join(selected),
                    *_paid(budget),
                ],
            )
        finally:
            if previous is None:
                os.environ.pop("DBT_INDIRECT_SELECTION", None)
            else:
                os.environ["DBT_INDIRECT_SELECTION"] = previous
        # Every model is judged by the rebuild, which built them all: a model
        # the first pass built and this one failed is not success.
        model_status = {
            n["name"]: n["status"] for n in (rebuilt.get("data") or {}).get("nodes", [])
        }
        built = rebuilt
    model_status.update(snapshot_status)
    for name in dict.fromkeys([*snapshots, *selected]):
        if model_status.get(name) != "success":
            report.disagreements.append(
                f"{name} did not build ({model_status.get(name)}): "
                f"{built.get('errors')}"
            )
    if not live and not report.unsupported and not report.withheld:
        return report

    _rc, verified = step(
        "verify",
        [*root, "maintain", "verify", "--target", "dev", *selected, *_paid(budget)],
    )
    verify_data = {
        **(verified.get("data") or {}),
        "_warnings": verified.get("warnings") or [],
    }
    findings = verify_data.get("findings") or []

    for row in live:
        _judge(row, results, tests, findings, report, verify_data)
    for row in rows:
        if row.label in report.unsupported or row.label in report.withheld:
            _judge_unsupported(row, tests, findings, report, verify_data)
    return report


def _judge(
    row: ParityRow,
    results: Mapping[str, Mapping[str, Any]],
    tests: list[tuple[str, str, dict[str, Any], str]],
    findings: list[Mapping[str, Any]],
    report: ParityReport,
    verify: Mapping[str, Any],
) -> None:
    for side, should_fail in (("violating", True), ("clean", False)):
        model = row.model(side)
        uid = _test_uid(tests, model, row)
        result = results.get(uid or "", {})
        status = result.get("status")
        where = f"{row.label} on {model}"
        if row.failures is not None:
            # No maintain verify twin (#1461): dbt's answer is the verdict.
            failures = int(result.get("failures") or 0)
            if should_fail and (status != "fail" or failures != row.failures):
                report.disagreements.append(
                    f"{where}: the test should fail on the violating fixture with "
                    f"{row.failures} failure(s), dbt says {status!r} with {failures}"
                )
            elif not should_fail and status != "pass":
                report.disagreements.append(
                    f"{where}: the test should pass on the clean fixture, dbt "
                    f"says {status!r}"
                )
            continue
        judged = row.judged(side)
        found = [
            f for f in findings if f["identifier"] == judged and f["code"] in row.codes
        ]
        test_failed = status == "fail"
        test_passed = status == "pass"
        if should_fail:
            if not test_failed:
                report.disagreements.append(
                    f"{where}: the test should fail on the violating fixture, "
                    f"dbt says {status!r}"
                )
            if not found:
                report.disagreements.append(
                    f"{where}: maintain verify raised none of "
                    f"{sorted(row.codes)} on "
                    + ("the violating fixture" if judged == model else judged)
                )
            if test_failed and found and row.count is not None:
                expected = row.count(found[0])
                if int(result.get("failures") or 0) != expected:
                    report.disagreements.append(
                        f"{where}: dbt counts {result.get('failures')} failure(s), "
                        f"maintain verify counts {expected}"
                    )
        elif row.directional:
            # The finding follows from the violation, not the reverse: a clean
            # key says nothing about verify's finding, which has other causes.
            if not test_passed:
                report.disagreements.append(
                    f"{where}: the test should pass on the clean fixture, dbt "
                    f"says {status!r}"
                )
        else:
            if row.measured is not None and not row.measured(verify, model):
                report.disagreements.append(
                    f"{where}: maintain verify did not measure the clean fixture, "
                    "so its absent finding is no verdict"
                )
            if not test_passed:
                report.disagreements.append(
                    f"{where}: the test should pass on the clean fixture, dbt "
                    f"says {status!r}"
                )
            if found:
                report.disagreements.append(
                    f"{where}: maintain verify raised {[f['code'] for f in found]} "
                    "on the clean fixture"
                )
    if not any(
        d.startswith((f"{row.label} ", f"{row.label}:")) for d in report.disagreements
    ):
        report.agreed.append(row.label)


def _judge_unsupported(
    row: ParityRow,
    tests: list[tuple[str, str, dict[str, Any], str]],
    findings: list[Mapping[str, Any]],
    report: ParityReport,
    verify: Mapping[str, Any],
) -> None:
    """On an unsupported dialect no test exists, and ``maintain verify`` alone
    answers: still the finding on the violating fixture and none on the clean."""

    for side, owed in (("violating", True), ("clean", False)):
        model = row.model(side)
        if _test_uid(tests, model, row) is not None:
            report.disagreements.append(
                f"{row.label}: a test built on {model} where the dialect is unsupported"
            )
        found = any(
            f["identifier"] == model and f["code"] in row.codes for f in findings
        )
        if not owed and row.measured is not None and not row.measured(verify, model):
            report.disagreements.append(
                f"{row.label}: maintain verify did not measure {model}"
            )
        if found is not owed:
            report.disagreements.append(
                f"{row.label}: maintain verify {'missed' if owed else 'raised'} "
                f"{sorted(row.codes)} on {model}"
            )


def _claims(row: ParityRow, kind: gt.GeneratedTestKind) -> set[str] | None:
    """The claims of ``row``'s own test on its two fixtures, where its
    ``arguments`` pick one of several instances of the kind on one model, so
    the plan's ``data.generated_tests`` rows (which name a claim, not the
    arguments) are told apart. ``None`` where every instance is the row's."""

    if not row.arguments:
        return None
    from exmergo_dex_core.transform.claims import claim_id
    from exmergo_dex_core.transform.declarations import parse_declarations

    out: set[str] = set()
    for side in ("violating", "clean"):
        model = row.model(side)
        [declaration] = parse_declarations([{"model": model, **row.declaration}])
        if kind.reads_history:
            # The fixture reads its snapshot through ref() (#1458).
            instances = kind.instances(  # type: ignore[call-arg]
                declaration, gt.HistoryReading(lineage="descends")
            )
        else:
            instances = kind.instances(declaration)
        for instance in instances:
            if row.picks(instance.arguments):
                out.add(
                    claim_id(
                        model, instance.field, instance.columns, dict(instance.params)
                    )
                )
    return out


def _test_uid(
    tests: list[tuple[str, str, dict[str, Any], str]], model: str, row: ParityRow
) -> str | None:
    """The unique id of ``row``'s test on ``model``, or ``None``."""

    for attached, name, kwargs, uid in tests:
        if attached == model and name == row.test and row.picks(kwargs):
            return uid
    return None


def _artifacts(
    project: Path,
) -> tuple[dict[str, dict[str, Any]], list[tuple[str, str, dict[str, Any], str]]]:
    """The newest run results under the project's target path, by unique id,
    and each generic test as (attached model, test name, kwargs, unique id)."""

    candidates = sorted(
        (project / "target").rglob("run_results.json"),
        key=lambda path: path.stat().st_mtime,
    )
    if not candidates:
        return {}, []
    folder = candidates[-1].parent
    results = {
        r["unique_id"]: r
        for r in json.loads(candidates[-1].read_text(encoding="utf-8"))["results"]
    }
    tests: list[tuple[str, str, dict[str, Any], str]] = []
    manifest = folder / "manifest.json"
    if manifest.is_file():
        for uid, node in json.loads(manifest.read_text(encoding="utf-8"))[
            "nodes"
        ].items():
            meta = node.get("test_metadata") or {}
            attached = node.get("attached_node") or ""
            if node.get("resource_type") == "test" and meta.get("name"):
                tests.append(
                    (
                        attached.rsplit(".", 1)[-1],
                        str(meta["name"]),
                        dict(meta.get("kwargs") or {}),
                        uid,
                    )
                )
    return results, tests
