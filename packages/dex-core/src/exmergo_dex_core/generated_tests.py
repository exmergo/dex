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
    two claims apart without decoding them (#1454).
    """

    column: str | None
    field: str
    columns: tuple[str, ...]
    params: Mapping[str, Any] = field(default_factory=dict)
    arguments: Mapping[str, Any] = field(default_factory=dict)
    variant: str | None = None


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
    )
)
