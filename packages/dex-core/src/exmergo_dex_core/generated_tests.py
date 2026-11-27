"""The tests Dex generates from declarations, beyond the three built-ins (#1449).

A declaration already renders dbt's ``unique``, ``not_null`` and
``relationships`` (the built-ins, always on). A **generated test** checks a
declared fact those cannot (a composite grain, a population filter, a parent's
population, ...), each a generic test of Dex's own. Generated tests run in the
customer's production runs, which Dex does not run and whose cost the customer
pays, so two rules hold for every one of them:

- **They are opt-in.** ``declarations.render_tests`` in ``.dex/config.yml``
  turns them on, and is off by default. ``true`` turns on every kind this
  release renders; a list turns on the kinds it names, by test name or by
  cadence, so a project can start with the cheap ones.
- **Each carries a cadence tag**, ``dex_every_run`` or ``dex_scheduled``,
  written as a dbt tag so an orchestrator selects them with ``dbt build
  --select tag:dex_scheduled`` or leaves them out with ``--exclude``. The tag is
  fixed per kind, never chosen per project: a scheduled test is one that scans
  whole tables (an anti-join, a gap search).

This module is the catalog: each kind, its cadence, and which rows of a
declaration it checks. Each generated-test issue (#1452 onward) registers its
kind here with :func:`register`. It imports nothing heavier than the standard
library, because ``config validate`` reads it to check the names a list gives.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

#: The cadence tag of a test cheap enough to run on every production run.
EVERY_RUN = "dex_every_run"
#: The cadence tag of a test that scans whole tables, run on its own schedule.
SCHEDULED = "dex_scheduled"
#: Every cadence tag, in the order the docs give them.
CADENCES: tuple[str, ...] = (EVERY_RUN, SCHEDULED)

#: The setting key, named in messages so a reader can find it.
SETTING = "declarations.render_tests"

#: The tests a declaration renders whatever the switch says. Never tagged.
BUILT_INS: frozenset[str] = frozenset({"unique", "not_null", "relationships"})


@dataclass(frozen=True)
class Instance:
    """One test a kind renders for one declaration.

    ``column`` is the column the test sits under, or ``None`` for a model-level
    test (``data_tests`` on the model entry). ``field``, ``columns`` and
    ``params`` are the claim it checks (``transform.claims.claim_id``), so two
    instances of one kind on one model (two population filters) get two claims.
    A string in ``params`` is a name and is folded to lower case, as dbt
    resolves names; a value from the data (a filter's values), whose case is
    part of the fact, goes in a list, which is hashed as written.
    ``arguments`` are the generic test's arguments, written under
    ``arguments:`` as dbt 1.10 and later read them. ``variant`` names, in a
    word, which of a kind's tests on one model this is (a filter's
    ``includes`` or ``excludes`` list, a parent's ``missing`` or
    ``unexpected`` direction), so a reader of ``data.generated_tests`` can tell
    two claims apart without decoding them (#1454). ``withheld`` is the reason
    an instance never renders, whatever the switch says: the fact is one only
    ``maintain verify`` can judge, as a question, so a test that failed a run on
    it would fail correct data (#1455: a ``percent`` column). It is still listed
    under ``data.generated_tests``, ``not_rendered`` with that reason.
    """

    column: str | None
    field: str
    columns: tuple[str, ...]
    params: Mapping[str, Any] = field(default_factory=dict)
    arguments: Mapping[str, Any] = field(default_factory=dict)
    variant: str | None = None
    withheld: str | None = None


@dataclass(frozen=True)
class GeneratedTestKind:
    """A generated test: its name (the generic test's), its cadence, and the
    instances a declaration gives it."""

    name: str
    cadence: str
    #: The instances one declaration gives this kind; empty where it declares
    #: nothing this kind checks.
    instances: Callable[[Any], Iterable[Instance]]
    #: The shipped macro asset (``transform/assets/macros/<macro>.sql``) that
    #: defines the generic test, written into the project's macro paths by the
    #: plan that first renders the test where the project defines none.
    macro: str | None = None
    #: The connectors the test's macro is proved on (#1451): every connector
    #: ``maintain verify`` runs the matching check on. A connector not listed
    #: renders no test, and the plan reports it ``dialect_unsupported``, rather
    #: than rendering one whose answer nothing has held to ``maintain verify``'s.
    #: ``None`` means every connector.
    dialects: frozenset[str] | None = None
    #: Whether :attr:`instances` also reads the model's joins (#1456): it is
    #: then called as ``instances(declaration, joins)``, ``joins`` being what
    #: the plan read of the model's SQL
    #: (:class:`~.transform.join_reading.ModelJoins`), or ``None`` where the
    #: caller read nothing, which gives no instance at all.
    reads_joins: bool = False
    #: Whether the kind checks a fact about a model that another model's
    #: declaration implies (#1457: a ``references`` implies its parent column
    #: is unique). Such a kind gives nothing for a model's own declaration: it
    #: is called as ``instances(parent_declaration, references)``, ``references``
    #: being the :class:`ParentReferences` the project's declarations point at
    #: the parent, and its tests render into the parent's YAML entry.
    on_parent: bool = False
    #: Whether :attr:`instances` also reads what the plan read of a declared
    #: ``history`` block's snapshot (#1458): it is then called as
    #: ``instances(declaration, reading)``, ``reading`` being a
    #: :class:`HistoryReading`, or ``None`` where the caller read nothing,
    #: which gives no instance at all.
    reads_history: bool = False
    #: Whether each test carries a dbt ``name`` built from its claim,
    #: ``<kind>_<claim>`` (#1458), rather than the name dbt builds from its
    #: arguments: two claims whose arguments stringify alike (a target of ``1``
    #: and of ``"1"``) would otherwise share a name, which dbt refuses, and a
    #: value from the data would sit in the node name.
    named_by_claim: bool = False
    #: Whether the test's SQL reads the model it sits on (#1462). Every kind
    #: does but :data:`JOIN_COVERAGE`, which reads the join's two inputs.
    reads_model: bool = True
    #: The arguments that name another relation the test's SQL reads, each a
    #: ``ref()`` or ``source()`` call written as text (or ``None`` where the
    #: plan could not name it), so a plan can say which tables a test reads
    #: and price it from their production sizes (#1462).
    relation_arguments: tuple[str, ...] = ()


@dataclass(frozen=True)
class HistoryReading:
    """What a plan read, statically, of a declared ``history`` block's snapshot
    (#1458): the facts ``maintain verify`` reads from the manifest instead.

    ``lineage`` is ``descends`` where the model's ``ref()`` ancestors hold the
    snapshot, ``not_ancestor`` where they provably do not (``other_snapshots``
    names the snapshots they do hold), and ``unknown`` where something on the
    way could hide it (``why`` says what). ``hard_deletes`` and ``valid_from``
    are the snapshot's config (``valid_from`` is ``dbt_valid_from`` unless
    ``snapshot_meta_column_names`` renames it); ``config_unread`` names a
    config key written as an expression the plan cannot read."""

    lineage: str
    why: str | None = None
    other_snapshots: tuple[str, ...] = ()
    hard_deletes: str | None = None
    valid_from: str = "dbt_valid_from"
    config_unread: str | None = None


@dataclass(frozen=True)
class ParentReferences:
    """What the project's declarations say about one parent (#1457).

    ``columns`` maps each of the parent's columns a ``references`` names (as
    the first declaration spelled it) to the ``child.column`` pairs naming it,
    sorted. ``unique_tested`` are the parent's columns, folded to lower case,
    that already list dbt's ``unique`` test, which asserts the same fact."""

    columns: Mapping[str, tuple[str, ...]]
    unique_tested: frozenset[str] = frozenset()


_KINDS: dict[str, GeneratedTestKind] = {}


def register(kind: GeneratedTestKind) -> GeneratedTestKind:
    """Add ``kind`` to the catalog. Refuses a second kind of one name, a name
    that is a cadence or a built-in, and a cadence that is not one of
    :data:`CADENCES`."""

    if kind.cadence not in CADENCES:
        raise ValueError(
            f"generated test '{kind.name}' has cadence '{kind.cadence}'; a cadence "
            f"is one of {', '.join(CADENCES)}"
        )
    if kind.name in CADENCES or kind.name in BUILT_INS:
        raise ValueError(f"'{kind.name}' cannot name a generated test")
    if kind.name in _KINDS:
        raise ValueError(f"generated test '{kind.name}' is already registered")
    _KINDS[kind.name] = kind
    return kind


def unregister(name: str) -> None:
    """Take ``name`` out of the catalog (a test's fixture kind)."""

    _KINDS.pop(name, None)


def kinds() -> tuple[GeneratedTestKind, ...]:
    """Every kind this release renders, in the order they were registered."""

    return tuple(_KINDS.values())


def unknown_entries(setting: Any) -> list[str]:
    """The entries of a ``render_tests`` list that name neither a kind this
    release renders nor a cadence. They turn nothing on; the config reports them
    without refusing, so a file written for a later release still loads."""

    if not isinstance(setting, list):
        return []
    return [
        entry
        for entry in setting
        if isinstance(entry, str) and entry not in _KINDS and entry not in CADENCES
    ]


def enabled(kind: GeneratedTestKind, setting: bool | list[str]) -> bool:
    """Whether ``setting`` turns ``kind`` on: ``true`` turns on every kind, and
    a list the kinds it names and the kinds of each cadence it names."""

    if isinstance(setting, bool):
        return setting
    return kind.name in setting or kind.cadence in setting


def supports(kind: GeneratedTestKind, dialect: str | None) -> bool:
    """Whether ``kind`` renders on ``dialect`` (a connector name). An unknown
    dialect (``None``: no connector configured) is not refused here: the plan
    cannot tell which warehouse will run the test, so it renders as before."""

    return kind.dialects is None or dialect is None or dialect in kind.dialects


#: The reason a kind turned on renders nothing on the configured connector.
DIALECT_UNSUPPORTED = "dialect_unsupported"
#: The reason a test renders nothing because the model's SQL provably does not
#: output a column it reads (a filter on a column it aggregates away, a grain or
#: parent key column it drops), so the test could only error.
COLUMN_NOT_OUTPUT = "column_not_output"
#: The reason a declared fact renders no test because ``maintain verify``'s
#: check of it is a heuristic, not arithmetic (#1455: a ``percent`` column whose
#: values all sit within 0 to 1 may be a fraction stored as a percent, or a
#: column of genuinely small percentages), so it stays with ``maintain verify``.
HEURISTIC_ONLY = "heuristic_only"


#: The reasons a required join renders no ``dex_join_coverage`` (#1456). The
#: plan could not read the model's joins from its SQL (Jinja other than
#: ``ref()``/``source()``/``config()``, or SQL that does not parse), so it cannot
#: tell which join a ``references`` makes required; ``maintain verify``, which
#: reads the compiled SQL, still judges it.
JOIN_UNREAD = "join_unread"
#: The required join is a LEFT join to the whole parent: it keeps every
#: unmatched row, so the
#: ``relationships`` (or ``not_null``) test the declaration already renders
#: counts the same rows on the model's output. Also the reason a referenced
#: parent column renders no ``dex_referenced_key`` (#1457): the parent's
#: one-column grain, or a ``unique`` test already listed on the column, asserts
#: it.
COVERED_BY_BUILTIN = "covered_by_builtin"
#: The required join is a shape this release does not render (a key compared
#: through a conversion, a grouped side, a RIGHT or FULL join, a side that is
#: no ``ref()`` or ``source()``, a filter that does not carry into the dialect).
JOIN_SHAPE_UNSUPPORTED = "join_shape_unsupported"

#: The reasons a referenced parent column renders no ``dex_referenced_key``
#: (#1457). The parent has no YAML entry under ``models:`` (a model nobody
#: documented, a seed, a snapshot), and Dex does not create one to hold a test.
PARENT_UNDECLARED = "parent_undeclared"
#: The parent is an installed package's model, or a project override of one its
#: package documents, whose YAML entry is the package's: Dex never edits it.
PARENT_IN_PACKAGE = "parent_in_package"

#: The reasons a declared ``history`` measure renders no
#: ``dex_temporal_contract`` (#1458), each the reason ``maintain verify`` leaves
#: the same measure unchecked with. The declaration leaves out a policy the
#: measure's value depends on, and none is assumed.
POLICY_MISSING = "policy_missing"
#: The model's ``ref()`` lineage does not reach the declared snapshot.
SNAPSHOT_NOT_ANCESTOR = "snapshot_not_ancestor"
#: Whether the model reaches the declared snapshot could not be read.
LINEAGE_UNAVAILABLE = "lineage_unavailable"
#: The snapshot writes hard-delete records, versions that are no observation.
HARD_DELETE_RECORDS = "hard_delete_records"
#: The plan's own reason, with no ``maintain verify`` twin (verify reads the
#: compiled config): the snapshot's ``hard_deletes`` or
#: ``snapshot_meta_column_names`` is written as an expression the plan cannot
#: read, so it cannot tell whether the shape is supported or which column
#: orders the versions.
SNAPSHOT_CONFIG_UNREAD = "snapshot_config_unread"


def off_reason(setting: bool | list[str]) -> str:
    """Why a kind ``setting`` leaves off is off: the switch is off, or the list
    does not name it."""

    return "render_tests_off" if setting is False else "kind_not_enabled"


def is_generated(test: Any) -> bool:
    """Whether a parsed test entry carries a cadence tag, the mark of a test
    Dex generated (a built-in never carries one)."""

    if not isinstance(test, Mapping) or len(test) != 1:
        return False
    body = next(iter(test.values()))
    if not isinstance(body, Mapping):
        return False
    tags: Any = None
    config = body.get("config")
    if isinstance(config, Mapping):
        tags = config.get("tags")
    if tags is None:
        tags = body.get("tags")
    if isinstance(tags, str):
        tags = [tags]
    return isinstance(tags, list) and any(tag in CADENCES for tag in tags)


# --- the kinds this release renders ---------------------------------------------

#: Every connector Dex builds on. ``maintain verify``'s grain check is one
#: aggregate statement it runs on each of them, so a test agreeing with it is
#: owed on each (#1451).
ALL_CONNECTORS: frozenset[str] = frozenset(
    {
        "bigquery",
        "clickhouse",
        "databricks",
        "duckdb",
        "postgres",
        "redshift",
        "snowflake",
        "trino",
    }
)

UNIQUE_COMBINATION = "dex_unique_combination"


def _composite_grain(declaration: Any) -> list[Instance]:
    """A declared ``grain`` of two or more columns (#1452). A one-column grain
    already renders dbt's ``unique`` and ``not_null``."""

    grain = [str(column) for column in (getattr(declaration, "grain", None) or [])]
    if len(grain) < 2:
        return []
    return [
        Instance(
            column=None,
            field="grain",
            columns=tuple(grain),
            params={"check": UNIQUE_COMBINATION},
            arguments={"columns": grain},
        )
    ]


register(
    GeneratedTestKind(
        UNIQUE_COMBINATION,
        EVERY_RUN,
        _composite_grain,
        macro=UNIQUE_COMBINATION,
        dialects=ALL_CONNECTORS,
    )
)

POPULATION_FILTER = "dex_population_filter"


def _population_filters(declaration: Any) -> list[Instance]:
    """One test per declared ``population.filters`` list (#1453), under the
    filter's column. A filter stating both ``includes`` and ``excludes`` is two
    claims, checked apart, as ``maintain verify`` reads it. A list's values are
    the declaration's own committed text, so they go into the claim as written:
    ``'Paid'`` and ``'paid'`` are two different facts."""

    population = getattr(declaration, "population", None)
    out: list[Instance] = []
    for entry in getattr(population, "filters", None) or []:
        column = str(entry.column)
        for mode in ("includes", "excludes"):
            values = list(getattr(entry, mode, None) or [])
            if not values:
                continue
            out.append(
                Instance(
                    column=column,
                    field="population",
                    columns=(column,),
                    params={"check": POPULATION_FILTER, mode: values},
                    arguments={mode: values},
                    variant=mode,
                )
            )
    return out


register(
    GeneratedTestKind(
        POPULATION_FILTER,
        EVERY_RUN,
        _population_filters,
        macro=POPULATION_FILTER,
        dialects=ALL_CONNECTORS,
    )
)

PARENT_POPULATION = "dex_parent_population"
#: The two claims a declared ``population.parent`` makes, each its own test.
PARENT_DIRECTIONS: tuple[str, ...] = ("missing", "unexpected")


def _parent_population(declaration: Any) -> list[Instance]:
    """Two model-level tests per declared ``population.parent`` (#1454), one
    per claim it makes: no eligible parent key is ``missing`` from the model,
    and no output key is ``unexpected`` (absent from the parent). Two tests
    rather than one summed count, so each failure count is the count of one
    ``maintain verify`` finding and each direction keeps its own history.

    The declaration's ``population.filters`` ride along: ``maintain verify``
    scopes the eligible parent keys with each filter whose column the parent
    has, and the test does the same."""

    population = getattr(declaration, "population", None)
    parent = getattr(population, "parent", None)
    if parent is None:
        return []
    from .model_versions import ref_call

    key = {str(out): str(column) for out, column in parent.key.items()}
    filters: list[dict[str, Any]] = []
    for entry in getattr(population, "filters", None) or []:
        rule: dict[str, Any] = {"column": str(entry.column)}
        for mode in ("includes", "excludes"):
            values = list(getattr(entry, mode, None) or [])
            if values:
                rule[mode] = values
        if len(rule) > 1:
            filters.append(rule)
    out: list[Instance] = []
    for direction in PARENT_DIRECTIONS:
        # Fresh containers per test: a mapping two tests share would be
        # written as a YAML anchor and alias, which no reviewer expects.
        arguments: dict[str, Any] = {
            "parent": ref_call(str(parent.model)),
            "key": dict(key),
            "direction": direction,
        }
        params: dict[str, Any] = {
            "check": PARENT_POPULATION,
            "direction": direction,
            "parent": str(parent.model),
            # The parent columns, in key order: names, folded as claim_id
            # folds a string (a list is hashed as written).
            "parent_key": [column.lower() for column in key.values()],
        }
        if filters and direction == "missing":
            # Only the missing direction reads the filters: an unexpected key
            # is one the whole parent lacks, as maintain verify counts it.
            arguments["filters"] = [dict(rule) for rule in filters]
            params["filters"] = [dict(rule) for rule in filters]
        out.append(
            Instance(
                column=None,
                field="population",
                columns=tuple(key),
                params=params,
                arguments=arguments,
                variant=direction,
            )
        )
    return out


register(
    GeneratedTestKind(
        PARENT_POPULATION,
        SCHEDULED,
        _parent_population,
        macro=PARENT_POPULATION,
        dialects=ALL_CONNECTORS,
        relation_arguments=("parent",),
    )
)

UNIT_RANGE = "dex_unit_range"
#: The declared units ``maintain verify`` checks, folded as it folds them.
RANGE_UNITS: tuple[str, ...] = ("fraction", "percent")


def _declared_units(declaration: Any) -> list[Instance]:
    """One column test per column declared ``unit: fraction`` (#1455): it fails
    on a non-NULL value below 0 or above 1, as ``maintain verify``'s
    ``unit_out_of_range`` does. A column declared ``percent`` is listed and
    withheld (:data:`HEURISTIC_ONLY`): verify flags a percent whose every value
    sits within 0 to 1, which correct small percentages do too."""

    out: list[Instance] = []
    for name, column in (getattr(declaration, "columns", None) or {}).items():
        unit = getattr(column, "unit", None)
        if not isinstance(unit, str) or unit.strip().lower() not in RANGE_UNITS:
            continue
        folded = unit.strip().lower()
        out.append(
            Instance(
                column=str(name),
                field="unit",
                columns=(str(name),),
                params={"check": UNIT_RANGE, "unit": folded},
                variant=folded,
                withheld=HEURISTIC_ONLY if folded == "percent" else None,
            )
        )
    return out


register(
    GeneratedTestKind(
        UNIT_RANGE,
        EVERY_RUN,
        _declared_units,
        macro=UNIT_RANGE,
        dialects=ALL_CONNECTORS,
    )
)

JOIN_COVERAGE = "dex_join_coverage"


def _join_coverage(declaration: Any, joins: Any) -> list[Instance]:
    """One model-level test per INNER join the declaration requires (#1456):
    every eligible row of the join's driving input finds a parent row, as
    ``maintain verify``'s join contract holds a required join at threshold
    zero. An INNER join drops its unmatched rows, so the model's output (and
    the ``relationships`` test on it) cannot show them: the test reads the
    join's two inputs, through the filters the model applies to each, as the
    plan read them from the model's SQL.

    A required LEFT join to the whole parent is listed and withheld
    (:data:`COVERED_BY_BUILTIN`); one to a filtered parent renders, since
    ``relationships`` reads the whole parent. A
    shape this release does not render is withheld
    (:data:`JOIN_SHAPE_UNSUPPORTED`), and where the plan could not read the
    joins at all each declared ``references`` is listed withheld
    (:data:`JOIN_UNREAD`). ``joins`` ``None`` (no reading asked for) gives
    nothing."""

    if joins is None:
        return []
    if not getattr(joins, "read", False):
        out: list[Instance] = []
        for name, column in (getattr(declaration, "columns", None) or {}).items():
            target = getattr(column, "references", None)
            if not isinstance(target, str) or not target.strip():
                continue
            out.append(
                Instance(
                    column=None,
                    field="references",
                    columns=(str(name),),
                    params={"check": JOIN_COVERAGE, "references": target.strip()},
                    withheld=JOIN_UNREAD,
                )
            )
        return out
    out = []
    for join in joins.required(declaration):
        arguments: dict[str, Any] = {
            "child": join.child,
            "child_columns": list(join.child_columns),
            "parent": join.parent,
            "parent_columns": list(join.parent_columns),
        }
        if join.child_where:
            arguments["child_where"] = join.child_where
        if join.parent_where:
            arguments["parent_where"] = join.parent_where
        if join.null_keys != "excluded":
            arguments["null_keys"] = join.null_keys
        params: dict[str, Any] = {
            "check": JOIN_COVERAGE,
            "child": join.child or "",
            "child_columns": [c.lower() for c in join.child_columns],
            "parent": join.parent or "",
            "parent_columns": [c.lower() for c in join.parent_columns],
            # The NULL policy decides the count, so a declaration that starts
            # or stops counting NULL keys is a different claim.
            "null_keys": join.null_keys,
        }
        for key in ("child_where", "parent_where"):
            if arguments.get(key):
                params[key] = [arguments[key]]
        withheld = None if join.status == "render" else join.status
        out.append(
            Instance(
                column=None,
                field="references",
                columns=tuple(join.driving) or tuple(join.child_columns),
                params=params,
                arguments=arguments if withheld is None else {},
                withheld=withheld,
            )
        )
    return out


register(
    GeneratedTestKind(
        JOIN_COVERAGE,
        SCHEDULED,
        _join_coverage,
        macro=JOIN_COVERAGE,
        dialects=ALL_CONNECTORS,
        reads_joins=True,
        reads_model=False,
        relation_arguments=("child", "parent"),
    )
)

REFERENCED_KEY = "dex_referenced_key"


def _referenced_keys(declaration: Any, references: Any) -> list[Instance]:
    """One column test per parent column a ``references`` names (#1457): a
    join to a key that is not unique multiplies rows, so the column the join
    reads is held unique on the parent, the model the claim is about. Where the
    parent declares that column as its whole grain, or the column already lists
    dbt's ``unique``, that test asserts the same fact, and this one is listed
    and withheld (:data:`COVERED_BY_BUILTIN`).

    ``references`` is a :class:`ParentReferences`, or ``None`` (nothing points
    at the model), which gives nothing."""

    if not isinstance(references, ParentReferences):
        return []
    grain = [str(c).lower() for c in (getattr(declaration, "grain", None) or [])]
    out: list[Instance] = []
    for column in references.columns:
        folded = column.lower()
        covered = grain == [folded] or folded in references.unique_tested
        out.append(
            Instance(
                column=column,
                field="referenced_key",
                columns=(column,),
                params={"check": REFERENCED_KEY},
                withheld=COVERED_BY_BUILTIN if covered else None,
            )
        )
    return out


register(
    GeneratedTestKind(
        REFERENCED_KEY,
        EVERY_RUN,
        _referenced_keys,
        macro=REFERENCED_KEY,
        dialects=ALL_CONNECTORS,
        on_parent=True,
    )
)

TEMPORAL_CONTRACT = "dex_temporal_contract"
#: The measures a ``history`` block can declare, in the order verify reports.
TEMPORAL_MEASURES: tuple[str, ...] = (
    "transition_count",
    "latest_target_transition_at",
    "first_target_at",
)
#: The policies each measure's expected value depends on (#1027). ``time``
#: means ``time.provenance``. ``maintain verify`` reads the same table, so the
#: measures it leaves ``policy_missing`` are the ones that render no test.
TEMPORAL_POLICY_OWED: Mapping[str, tuple[str, ...]] = {
    "transition_count": ("initial_observation", "null_status"),
    "latest_target_transition_at": ("initial_observation", "null_status", "time"),
    "first_target_at": ("time",),
}


def policy_owed(history: Any, measure: str) -> list[str]:
    """The policies ``history`` leaves out that ``measure`` depends on, as
    ``maintain verify`` names them (``time.provenance`` for the time)."""

    owed: list[str] = []
    for name in TEMPORAL_POLICY_OWED[measure]:
        if name == "time":
            time = getattr(history, "time", None)
            if time is None or getattr(time, "provenance", None) is None:
                owed.append("time.provenance")
        elif getattr(history, name, None) is None:
            owed.append(name)
    return owed


def _enum(value: Any) -> str | None:
    return None if value is None else str(getattr(value, "value", value))


def _temporal_contracts(declaration: Any, reading: Any) -> list[Instance]:
    """One model-level test per measure a declared ``history`` names (#1458):
    it fails with the number of entities whose column disagrees with what the
    snapshot's versions and the declared policy give, ``maintain verify``'s
    ``mismatched_entities`` for that measure's finding. One test per measure,
    so each failure count is one finding's count.

    A measure ``maintain verify`` would leave unchecked is listed and withheld
    with verify's reason, checked in verify's order: the snapshot is not an
    ancestor, the lineage cannot be read, the snapshot writes hard-delete
    records, then a policy the measure depends on is missing. ``reading``
    ``None`` (nothing read) gives nothing."""

    history = getattr(declaration, "history", None)
    if history is None or not isinstance(reading, HistoryReading):
        return []
    from .model_versions import ref_call

    measures = getattr(history, "measures", None)
    time = getattr(history, "time", None)
    entity = {str(out): str(snap) for out, snap in history.entity.items()}
    out: list[Instance] = []
    for measure in TEMPORAL_MEASURES:
        column = getattr(measures, measure, None)
        if column is None:
            continue
        withheld: str | None = None
        if reading.lineage == "not_ancestor":
            withheld = SNAPSHOT_NOT_ANCESTOR
        elif reading.lineage != "descends":
            withheld = LINEAGE_UNAVAILABLE
        elif reading.hard_deletes == "new_record":
            withheld = HARD_DELETE_RECORDS
        elif reading.config_unread is not None:
            withheld = SNAPSHOT_CONFIG_UNREAD
        elif policy_owed(history, measure):
            withheld = POLICY_MISSING
        policies = {
            "initial_observation": _enum(history.initial_observation),
            "null_status": _enum(history.null_status),
            "time_column": str(time.column) if time is not None else None,
            "provenance": _enum(getattr(time, "provenance", None)),
        }
        params: dict[str, Any] = {
            "check": TEMPORAL_CONTRACT,
            "measure": measure,
            "snapshot": str(history.snapshot),
            # Names, folded as claim_id folds a string (a list is hashed as
            # written).
            "entity": [[o.lower(), s.lower()] for o, s in entity.items()],
            "status_column": str(history.status.column),
            # A value from the data, whose case is part of the fact.
            "target": [history.status.target],
            "order_column": reading.valid_from,
            **{k: v for k, v in policies.items() if v is not None},
        }
        arguments: dict[str, Any] = {}
        if withheld is None:
            arguments = {
                "snapshot": ref_call(str(history.snapshot)),
                "entity": dict(entity),
                "status_column": str(history.status.column),
                "target": history.status.target,
                "measure": measure,
                "measure_column": str(column),
                **{k: v for k, v in policies.items() if v is not None},
            }
            if reading.valid_from.lower() != "dbt_valid_from":
                arguments["order_column"] = reading.valid_from
        out.append(
            Instance(
                column=None,
                field="history",
                columns=(*entity, str(column)),
                params=params,
                arguments=arguments,
                variant=measure,
                withheld=withheld,
            )
        )
    return out


register(
    GeneratedTestKind(
        TEMPORAL_CONTRACT,
        SCHEDULED,
        _temporal_contracts,
        macro=TEMPORAL_CONTRACT,
        dialects=ALL_CONNECTORS,
        reads_history=True,
        named_by_claim=True,
        relation_arguments=("snapshot",),
    )
)

FRESHNESS = "dex_freshness"


def _declared_freshness(declaration: Any) -> list[Instance]:
    """One column test per column declaring ``freshness`` (#1460): it fails when
    the column's newest value trails the warehouse's clock by more than
    ``max_lag``, or when the column holds no value at all, as ``maintain
    verify``'s ``freshness_exceeded`` does. The lag goes into the claim in
    seconds, so ``360m`` and ``6h`` are one fact; the basis goes in too, since a
    business time and a capture time are different claims, but not into the
    test's arguments, because the test fails alike on both."""

    out: list[Instance] = []
    for name, column in (getattr(declaration, "columns", None) or {}).items():
        freshness = getattr(column, "freshness", None)
        # A deadline alone (#1486) gives no dex_freshness: a lag is not a
        # deadline, and the deadline is verify's and the vendor monitor's.
        if freshness is None or freshness.max_lag_seconds is None:
            continue
        seconds = int(freshness.max_lag_seconds)
        basis = getattr(freshness.basis, "value", freshness.basis)
        out.append(
            Instance(
                column=str(name),
                field="freshness",
                columns=(str(name),),
                params={
                    "check": FRESHNESS,
                    "max_lag_seconds": seconds,
                    "basis": str(basis),
                },
                arguments={"max_lag_seconds": seconds},
                variant=str(basis),
            )
        )
    return out


register(
    GeneratedTestKind(
        FRESHNESS,
        EVERY_RUN,
        _declared_freshness,
        macro=FRESHNESS,
        dialects=ALL_CONNECTORS,
    )
)

CONTINUITY = "dex_continuity"


def _declared_continuity(declaration: Any) -> list[Instance]:
    """One column test per column declaring ``continuity`` (#1461): it fails with
    the number of gaps in the column, a gap being a run of buckets at the
    declared grain holding no row between two buckets that hold rows, for the
    whole model or per value of the ``by`` columns. An edge is never a gap, so
    the answer does not depend on today's date. The ``by`` columns go into the
    test's columns, so a model that does not output one renders nothing
    (:data:`COLUMN_NOT_OUTPUT`). Scheduled: the search reads every row."""

    out: list[Instance] = []
    for name, column in (getattr(declaration, "columns", None) or {}).items():
        continuity = getattr(column, "continuity", None)
        if continuity is None:
            continue
        grain = str(getattr(continuity.grain, "value", continuity.grain))
        by = [str(key) for key in continuity.by]
        arguments: dict[str, Any] = {"grain": grain}
        if by:
            arguments["by"] = list(by)
        out.append(
            Instance(
                column=str(name),
                field="continuity",
                columns=(str(name), *sorted(by, key=str.lower)),
                params={
                    "check": CONTINUITY,
                    "grain": grain,
                    # Names, folded as claim_id folds a string (a list is
                    # hashed as written), and sorted: the keys partition the
                    # search, so `[a, b]` and `[b, a]` are one claim.
                    "by": sorted(key.lower() for key in by),
                },
                arguments=arguments,
                variant=grain,
            )
        )
    return out


register(
    GeneratedTestKind(
        CONTINUITY,
        SCHEDULED,
        _declared_continuity,
        macro=CONTINUITY,
        dialects=ALL_CONNECTORS,
    )
)
