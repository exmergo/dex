"""The executable contract for values-free mode, shipped so a host can run it.

A host that relies on values-free mode (#591; see :mod:`.values_free`) relies on a
property of the release it pins, so it should be able to check that release
rather than take the changelog's word for it. This module is that check:

    from exmergo_dex_core.values_free_conformance import ValuesFreeContract

    class TestDexValuesFree(ValuesFreeContract):
        pass

That is the whole integration: pytest collects the inherited ``test_*`` methods and
runs them against a DuckDB warehouse the suite builds for itself. A host that
constructs its engine differently (its own store, its own config) overrides
:meth:`ValuesFreeContract.make_engine` and the suite runs against that.

What it does:

1. Builds a small DuckDB warehouse (:func:`build_sentinel_warehouse`) whose every
   text, date, timestamp and numeric value carries a distinctive sentinel
   (:data:`SENTINELS`), including PII-shaped and nullable columns and a column a
   cast will fail on, quoting the value it failed on.
2. Runs ``profile``, ``map``, ``relationships``, ``inventory`` and ``diagram`` in
   the mode, over a cold cache and over one a full-mode call already filled, and
   asserts no sentinel appears in the result, its envelope, the cache entry the
   call wrote, or the query ledger.
3. Runs every statement in :data:`ACCEPTED_QUERIES` and :data:`REFUSED_QUERIES`
   through ``query``, ``query_batch`` and ``query_across``: an accepted one returns
   numbers only, a refused one returns its expected code and no cell, and a
   warehouse error quotes nothing it read.
4. Checks the mode cannot leak across: a profile cached outside the mode is never
   served inside it with its values, and one cached inside it is never served
   outside it as complete.
5. Fails on any result field it does not know (:data:`KNOWN_FIELDS`), so a field a
   later release adds forces a decision here instead of leaking silently.

The accepted-query check asserts that cells are numbers, not that they are free of
sentinels: an aggregate over the sentinel rows is near their values by design.
That is the residual risk the mode names (an aggregate over one row is that row's
value), and a host bounds it with a cost ceiling and a query budget, not with this
suite.

This module imports pytest, so nothing in the package imports it. Install the
``[values-free-conformance]`` extra for pytest and the DuckDB client.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from . import envelope as env
from .config import DexConfig
from .demo.warehouse import DemoTable, generate_fixture_warehouse
from .engine import DexEngine
from .errors import WarehouseQueryError
from .results import Result, to_envelope
from .storage import MemoryStore
from .values_free import ResultMode, ValuesFreeCode, ValuesFreeError

__all__ = [
    "ACCEPTED_QUERIES",
    "KNOWN_FIELDS",
    "REFUSED_QUERIES",
    "SENTINELS",
    "RecordingStore",
    "ValuesFreeContract",
    "build_sentinel_warehouse",
    "find_sentinels",
]

# The table the fixture holds, and the one the corpora below read.
SENTINEL_TABLE = "customers"
# A second table, so relationships and the map have a join to infer.
SENTINEL_CHILD_TABLE = "orders"

_ROWS = 40
_FIRST_DAY = date(1911, 3, 17)
_FIRST_SEEN = datetime(1911, 3, 17, 13, 37, 42)

#: Substrings that appear in the fixture's values and nowhere a dex payload would
#: otherwise put them. Matched case-insensitively. Booleans carry no sentinel (a
#: boolean has nowhere to put one), and ids are plain on purpose, so a key column
#: profiles as a key.
SENTINELS: tuple[str, ...] = (
    "zq7x",  # every text value
    "1911-0",  # every date and timestamp (1911-03-17 onwards)
    "73195",  # DECIMAL balance
    "27182",  # DOUBLE score
    "94044",  # INTEGER visits
    "88000",  # the numeric-looking VARCHAR legacy_code
    "415-555",  # phone
)

#: Statements the mode accepts: every output column an aggregate, one row back.
ACCEPTED_QUERIES: tuple[str, ...] = (
    "select count(*) from customers",
    "select count(distinct email), avg(balance), sum(visits) from customers",
    "select count(*) filter (where tier = 'ZQ7X_GOLD') from customers",
    "select round(stddev(score), 2), variance(visits) from customers",
    "select count(*) / nullif(count(distinct tier), 0) from customers",
    "select bool_or(active), bool_and(active) from customers",
    "select count(referrer) * 1.0 / count(*) from customers",
    "select (select count(*) from orders) - count(*) from customers",
    "select count(*) from customers group by ()",
    "select count(*) from customers c join orders o "
    "on o.customer_id = c.id where c.tier = 'ZQ7X_SILVER'",
)

#: Statements the mode refuses, each with the code it must be refused with. Every
#: rule the shape check enforces is here, with DuckDB's spellings of the
#: aggregates that return a value from a row or a collection of them.
REFUSED_QUERIES: tuple[tuple[str, ValuesFreeCode], ...] = (
    ("select email from customers", ValuesFreeCode.BARE_COLUMN),
    ("select * from customers", ValuesFreeCode.BARE_COLUMN),
    ("select count(*) + visits from customers", ValuesFreeCode.BARE_COLUMN),
    (
        "select tier, count(*) from customers group by tier",
        ValuesFreeCode.GROUP_BY_COLUMN,
    ),
    (
        "select count(*) from customers group by tier",
        ValuesFreeCode.GROUP_BY_COLUMN,
    ),
    (
        "select count(*) from customers group by all",
        ValuesFreeCode.GROUP_BY_COLUMN,
    ),
    (
        "select count(*) from customers group by rollup (tier)",
        ValuesFreeCode.GROUP_BY_COLUMN,
    ),
    (
        "select distinct count(*) from customers",
        ValuesFreeCode.DISTINCT_PROJECTION,
    ),
    ("select count(*) from customers limit 1", ValuesFreeCode.LIMIT_OFFSET),
    ("select count(*) from customers offset 1", ValuesFreeCode.LIMIT_OFFSET),
    ("select count(*) from customers order by 1", ValuesFreeCode.ORDER_BY),
    (
        "select count(*) over () from customers",
        ValuesFreeCode.WINDOW_FUNCTION,
    ),
    (
        "select sum(visits) over (order by id) from customers",
        ValuesFreeCode.WINDOW_FUNCTION,
    ),
    (
        "select min(balance) from customers",
        ValuesFreeCode.AGGREGATE_NOT_ALLOWED,
    ),
    (
        "select max(signup_date) from customers",
        ValuesFreeCode.AGGREGATE_NOT_ALLOWED,
    ),
    (
        "select any_value(email) from customers",
        ValuesFreeCode.AGGREGATE_NOT_ALLOWED,
    ),
    (
        "select arg_max(email, visits) from customers",
        ValuesFreeCode.AGGREGATE_NOT_ALLOWED,
    ),
    ("select mode(tier) from customers", ValuesFreeCode.AGGREGATE_NOT_ALLOWED),
    (
        "select first(email) from customers",
        ValuesFreeCode.AGGREGATE_NOT_ALLOWED,
    ),
    ("select list(email) from customers", ValuesFreeCode.AGGREGATE_NOT_ALLOWED),
    (
        "select array_agg(email) from customers",
        ValuesFreeCode.AGGREGATE_NOT_ALLOWED,
    ),
    (
        "select string_agg(email, ',') from customers",
        ValuesFreeCode.AGGREGATE_NOT_ALLOWED,
    ),
    (
        "select median(visits) from customers",
        ValuesFreeCode.AGGREGATE_NOT_ALLOWED,
    ),
    (
        "select quantile_cont(visits, 0.5) from customers",
        ValuesFreeCode.AGGREGATE_NOT_ALLOWED,
    ),
    (
        "select approx_quantile(visits, 0.5) from customers",
        ValuesFreeCode.AGGREGATE_NOT_ALLOWED,
    ),
    (
        "select 'zq7x echoed', count(*) from customers",
        ValuesFreeCode.LITERAL_PROJECTION,
    ),
    (
        "select upper(email) from customers",
        ValuesFreeCode.FUNCTION_NOT_ALLOWED,
    ),
    (
        "select cast(count(*) as varchar) from customers",
        ValuesFreeCode.FUNCTION_NOT_ALLOWED,
    ),
    (
        "select (select email from customers limit 1) from customers",
        ValuesFreeCode.UNAGGREGATED_SUBQUERY,
    ),
    (
        "select (select max(email) from customers) + count(*) from customers",
        ValuesFreeCode.UNAGGREGATED_SUBQUERY,
    ),
    ("select 1 from customers", ValuesFreeCode.NO_AGGREGATE),
    (
        "select count(*) from customers union all select count(*) from orders",
        ValuesFreeCode.SET_OPERATION,
    ),
    # Passes the shape and fails on what came back: DuckDB's AVG over a
    # timestamp is a timestamp, which is a value rather than a number.
    (
        "select avg(cast(signup_date as timestamp)) from customers",
        ValuesFreeCode.NON_NUMERIC_RESULT,
    ),
)

#: A statement of the accepted shape the warehouse refuses on a value: the cast
#: fails on the one non-numeric ``legacy_code`` and DuckDB's message quotes it.
CAST_FAILURE_QUERY = "select sum(cast(legacy_code as integer)) from customers"


def _under(prefix: str, *paths: str) -> frozenset[str]:
    return frozenset({prefix, *(f"{prefix}{p}" for p in paths)})


_PII = ("", ".category", ".confidence")
_RELATIONSHIP = (
    "",
    "[]",
    "[].from_dataset",
    "[].from_columns",
    "[].from_columns[]",
    "[].to_dataset",
    "[].to_columns",
    "[].to_columns[]",
    "[].kind",
    "[].confidence",
    "[].verified",
    "[].orphan_fraction",
    "[].declared_by",
    "[].declaration_sources",
    "[].declaration_sources[]",
)
_CONFLICTS = (
    "",
    "[]",
    "[].from_dataset",
    "[].to_dataset",
    "[].declarations",
    "[].declarations[]",
    "[].declarations[].column_pairs",
    "[].declarations[].column_pairs[]",
    "[].declarations[].column_pairs[][]",
    "[].declarations[].source",
)
_JOIN_NOTES = (
    "",
    ".joins",
    ".joins[]",
    ".joins[].from_table",
    ".joins[].from_columns",
    ".joins[].from_columns[]",
    ".joins[].to_table",
    ".joins[].to_columns",
    ".joins[].to_columns[]",
    ".joins[].verified",
    ".joins[].orphan_fraction",
)
_STATEMENT = (
    "",
    "[]",
    "[].index",
    "[].status",
    "[].line",
    "[].shape",
    "[].columns",
    "[].columns[]",
    "[].types",
    "[].types[]",
    "[].cells",
    "[].cells[]",
    "[].cells[][]",
    "[].row_count",
    "[].truncated",
    "[].tables",
    "[].tables[]",
    *(f"[].query_notes{p}" for p in _JOIN_NOTES),
    "[].notes",
    "[].notes[]",
    "[].warnings",
    "[].warnings[]",
    "[].error",
    "[].reason",
    "[].stage",
    "[].code",
    "[].target",
    "[].template",
)
#: A model's declaration under ``--use-project`` (#498). Repository text, not a
#: table read, but a population filter's values and its free-text rule are column
#: values all the same, so in the mode ``population.rule`` and each filter's
#: ``includes``/``excludes`` are always null and named in ``withheld_fields``.
_DECLARATION = (
    "",
    ".model",
    ".source",
    ".grain",
    ".grain[]",
    ".grain_status",
    ".population",
    ".population.rule",
    ".population.filters",
    ".population.filters[]",
    ".population.filters[].column",
    ".population.filters[].includes",
    ".population.filters[].excludes",
    ".population.parent",
    ".population.parent.model",
    ".population.parent.columns",
    ".population.parent.columns[]",
    ".population.parent.parent_columns",
    ".population.parent.parent_columns[]",
    ".columns",
    ".columns[]",
    ".columns[].name",
    ".columns[].role",
    ".columns[].unit",
    ".columns[].additivity",
    ".columns[].references",
    ".elided_column_count",
    ".withheld_fields",
    ".withheld_fields[]",
)
# Every result in the mode carries these.
_COMMON = frozenset({"result_mode", "notes", "notes[]", "spend"})

#: Every key path a result in the mode may carry, per command. A path is the
#: envelope ``data`` key chain with list positions written ``[]``. Reviewed: none
#: of these holds a value read from a table (``cells`` holds the aggregates an
#: accepted statement computed, which the query tests hold to numbers). A path not
#: listed fails the suite, so a field a later release adds is a decision rather
#: than a silent leak. ``column_notes`` is deliberately absent: it annotates bare
#: projected columns, and the mode admits none.
KNOWN_FIELDS: dict[str, frozenset[str]] = {
    "profile": _COMMON
    | frozenset(
        {
            "profiled_count",
            "cache_hit_count",
            "cache_path",
            "updated_at",
        }
    )
    | _under(
        "datasets",
        "[]",
        "[].identifier",
        "[].object_type",
        "[].row_count",
        "[].byte_size",
        "[].candidate_keys",
        "[].candidate_keys[]",
        "[].candidate_keys[][]",
        "[].grain",
        "[].grain[]",
        "[].key_evidence",
        "[].key_evidence[]",
        "[].key_evidence[].columns",
        "[].key_evidence[].columns[]",
        "[].key_evidence[].status",
        "[].key_evidence[].reason",
        "[].composite_keys",
        "[].composite_keys[]",
        "[].composite_keys[][]",
        "[].rank_score",
        "[].data_quality",
        "[].data_quality[]",
        "[].findings",
        "[].findings[]",
        "[].findings[].column",
        "[].findings[].code",
        "[].findings[].severity",
        "[].findings[].detail",
        "[].profiled_at",
        "[].semantic_models",
        "[].semantic_models[]",
        *(f"[].declaration{p}" for p in _DECLARATION),
        "[].suppressed_fields",
        "[].suppressed_fields[]",
        "[].withheld_fields",
        "[].withheld_fields[]",
        "[].columns",
        "[].columns[]",
        "[].columns[].name",
        "[].columns[].data_type",
        "[].columns[].nullable",
        "[].columns[].null_fraction",
        "[].columns[].distinct_count",
        "[].columns[].distinct_count_exact",
        "[].columns[].is_unique",
        *(f"[].columns[].pii{p}" for p in _PII),
        "[].columns[].pii_overridden",
        "[].columns[].temporal_granularity",
        "[].columns[].temporal_span",
        "[].columns[].temporal_distinct_periods",
        "[].columns[].temporal_missing_periods",
        "[].columns[].temporal_largest_gap",
        "[].elided_column_count",
    ),
    "query": _COMMON
    | frozenset(
        {
            "shape",
            "columns",
            "columns[]",
            "types",
            "types[]",
            "cells",
            "cells[]",
            "cells[][]",
            "row_count",
            "truncated",
            "tables",
            "tables[]",
            "profiled_on_demand",
            "profiled_on_demand[]",
        }
    )
    | _under("query_notes", *_JOIN_NOTES[1:]),
    "query_batch": _COMMON
    | frozenset(
        {
            "shape",
            "statement_count",
            "ok_count",
            "failed_count",
            "profiled_on_demand",
            "profiled_on_demand[]",
            "across",
            "across[]",
        }
    )
    | _under("results", *_STATEMENT[1:]),
    "map": _COMMON
    | frozenset(
        {
            "cache_path",
            "object_count",
            "profiled_count",
            "cache_hit_count",
            "skipped_count",
            "carried_forward_count",
            "out_of_scope_carried_count",
            "dropped_count",
            "carried_relationship_count",
            "relationship_count",
            "pii_column_count",
            "data_quality_note_count",
            "elided_object_count",
            "elided_column_count",
            "elided_edge_count",
            "updated_at",
        }
    )
    | _under(
        "skipped",
        ".count",
        ".by_namespace",
        ".by_namespace[]",
        ".by_namespace[].namespace",
        ".by_namespace[].count",
        ".objects",
        ".objects[]",
        ".elided",
        ".elided_namespaces",
    )
    | _under("top_objects", "[]", "[].identifier", "[].rank_score")
    | _under(
        "objects",
        "[]",
        "[].identifier",
        "[].object_type",
        "[].row_count",
        "[].rank_score",
        "[].profiled_at",
        "[].grain",
        "[].grain[]",
        "[].candidate_key",
        "[].candidate_key[]",
        "[].semantic_models",
        "[].semantic_models[]",
        *(f"[].declaration{p}" for p in _DECLARATION),
        "[].columns",
        "[].columns[]",
        "[].columns[].name",
        "[].columns[].data_type",
        "[].columns[].role",
        *(f"[].columns[].pii{p}" for p in _PII),
        "[].elided_column_count",
        "[].pii_column_count",
        "[].data_quality",
        "[].data_quality[]",
        "[].elided_data_quality_count",
    )
    | _under("edges", *_RELATIONSHIP[1:])
    | _under("conflicts", *_CONFLICTS[1:]),
    "relationships": _COMMON
    | frozenset(
        {
            "declared_count",
            "semantic_join_count",
            "inferred_count",
            "profiled_count",
            "cache_hit_count",
            "carried_relationship_count",
            "cache_path",
            "updated_at",
        }
    )
    | _under("relationships", *_RELATIONSHIP[1:])
    | _under("conflicts", *_CONFLICTS[1:]),
    "inventory": _COMMON
    | frozenset({"object_count", "ranked", "elided_object_count"})
    | _under(
        "objects",
        "[]",
        "[].identifier",
        "[].object_type",
        "[].row_estimate",
        "[].column_count",
        "[].rank_score",
    ),
    "diagram": _COMMON
    | frozenset(
        {
            "mermaid",
            "entity_count",
            "edge_count",
            "elided_entity_count",
            "elided_column_count",
            "elided_edge_count",
            "full",
            "updated_at",
        }
    )
    | _under("entities", "[]", "[].entity", "[].identifier"),
}


class RecordingStore(MemoryStore):
    """A :class:`~.storage.MemoryStore` that also keeps every query-ledger row and
    every cache document written, so the suite can search them.

    ``ledger_rows`` leaves out each row's ``sql``: that is the statement the agent
    wrote, so any literal in it came from the agent rather than from a table, and
    the corpus deliberately writes sentinels into literals to prove a refusal does
    not echo them back.
    """

    def __init__(self) -> None:
        super().__init__()
        self.ledger_rows: list[dict] = []
        self.saved_caches: list[str] = []

    def append_query_log(self, entry: dict) -> None:
        super().append_query_log(entry)
        self.ledger_rows.append({k: v for k, v in entry.items() if k != "sql"})

    def save_cache(self, cache, *, now=None) -> str:
        self.saved_caches.append(cache.model_dump_json())
        return super().save_cache(cache, now=now)


def sentinel_tables() -> tuple[DemoTable, ...]:
    """The fixture's tables, as data, for a caller that writes them elsewhere."""

    people = [
        (
            i + 1,
            f"zq7x.person{i}@sentinel.test",
            f"+1-415-555-{7000 + i}",
            f"Zq7x Person{i}",
            ("ZQ7X_GOLD", "ZQ7X_SILVER", "ZQ7X_BRONZE")[i % 3],
            f"zq7x note {i}",
            _FIRST_DAY + timedelta(days=i),
            _FIRST_SEEN + timedelta(hours=i),
            Decimal("731950.0001") + i,
            27182.5 + i * 0.25,
            9_404_401 + i,
            i % 2 == 0,
            None if i % 2 else f"zq7x-ref-{i % 3}",
            "ZQ7X-NOT-A-NUMBER" if i == _ROWS - 1 else str(880_000 + i),
        )
        for i in range(_ROWS)
    ]
    visits = [(n + 1, (n % _ROWS) + 1, 9_404_401 + n) for n in range(_ROWS * 2)]
    return (
        DemoTable(
            name=SENTINEL_TABLE,
            columns=(
                ("id", "INTEGER"),
                ("email", "VARCHAR"),
                ("phone", "VARCHAR"),
                ("full_name", "VARCHAR"),
                ("tier", "VARCHAR"),
                ("note", "VARCHAR"),
                ("signup_date", "DATE"),
                ("last_seen", "TIMESTAMP"),
                ("balance", "DECIMAL(12, 4)"),
                ("score", "DOUBLE"),
                ("visits", "INTEGER"),
                ("active", "BOOLEAN"),
                ("referrer", "VARCHAR"),
                ("legacy_code", "VARCHAR"),
            ),
            rows=tuple(people),
            note="every value carries a values-free sentinel",
        ),
        DemoTable(
            name=SENTINEL_CHILD_TABLE,
            columns=(
                ("order_id", "INTEGER"),
                ("customer_id", "INTEGER"),
                ("visits", "INTEGER"),
            ),
            rows=tuple(visits),
            note="joins to the customers table on customer_id",
        ),
    )


def build_sentinel_warehouse(path: str | Path) -> Path:
    """Create the sentinel DuckDB warehouse at ``path`` (which must not exist)."""

    return generate_fixture_warehouse(path, sentinel_tables())


def find_sentinels(text: str) -> list[str]:
    """The sentinels ``text`` contains, case-insensitively."""

    lowered = text.lower()
    return [s for s in SENTINELS if s in lowered]


def _dump(value: Any) -> str:
    if isinstance(value, Result):
        return json.dumps(
            {
                "result": value.model_dump(mode="json"),
                "envelope": to_envelope(value).model_dump(mode="json"),
            },
            default=str,
        )
    if isinstance(value, BaseException):
        return json.dumps(
            {
                "message": str(value),
                "envelope": env.error_for(value).model_dump(mode="json"),
            },
            default=str,
        )
    return json.dumps(value, default=str)


def _paths(value: Any, prefix: str = "") -> set[str]:
    found: set[str] = set()
    if isinstance(value, dict):
        for key, sub in value.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            found.add(path)
            found |= _paths(sub, path)
    elif isinstance(value, list):
        path = f"{prefix}[]"
        for sub in value:
            found.add(path)
            found |= _paths(sub, path)
    return found


def _unknown_paths(command: str, data: dict) -> list[str]:
    return sorted(_paths(data) - KNOWN_FIELDS[command])


class ValuesFreeContract:
    """The values-free guarantees, as tests a host's suite inherits.

    Override :meth:`make_engine` to build the engine the way the host does. It
    must use the ``store`` it is handed, and the warehouse at ``warehouse``.
    """

    def make_engine(
        self, warehouse: Path, store: MemoryStore, result_mode: str
    ) -> DexEngine:
        return DexEngine(
            connector="duckdb",
            path=str(warehouse),
            config=DexConfig(connector="duckdb"),
            store=store,
            result_mode=result_mode,
        )

    # --- fixtures -------------------------------------------------------------

    @pytest.fixture
    def warehouse(self, tmp_path: Path) -> Path:
        return build_sentinel_warehouse(tmp_path / "sentinel.duckdb")

    @pytest.fixture
    def store(self) -> RecordingStore:
        return RecordingStore()

    @pytest.fixture
    def engine(self, warehouse: Path, store: RecordingStore) -> DexEngine:
        return self.make_engine(warehouse, store, ResultMode.VALUES_FREE.value)

    @pytest.fixture
    def full_engine(self, warehouse: Path, store: RecordingStore) -> DexEngine:
        return self.make_engine(warehouse, store, ResultMode.FULL.value)

    def assert_no_sentinel(self, *things: Any) -> None:
        for thing in things:
            text = thing if isinstance(thing, str) else _dump(thing)
            leaked = find_sentinels(text)
            assert not leaked, f"values-free output carried {leaked}: {text[:2000]}"

    # --- the fixture itself ---------------------------------------------------

    def test_the_fixture_does_carry_the_sentinels(self, full_engine) -> None:
        """Without this, a fixture that lost its sentinels would pass every check
        below by searching for something that was never there."""

        result = full_engine.profile(SENTINEL_TABLE)
        leaked = find_sentinels(_dump(result))
        assert {"zq7x", "1911-0", "73195", "94044"} <= set(leaked)

    # --- profile and the commands that read profiles --------------------------

    def test_a_profile_over_a_cold_cache_carries_no_value(self, engine, store) -> None:
        result = engine.profile(SENTINEL_TABLE, SENTINEL_CHILD_TABLE)

        assert result.result_mode is ResultMode.VALUES_FREE
        assert result.profiled_count == 2
        self.assert_no_sentinel(result, *store.saved_caches, store.ledger_rows)
        for dataset in result.datasets:
            assert dataset.values_withheld
        payload = to_envelope(result).data
        assert payload["result_mode"] == "values_free"
        for dataset in payload["datasets"]:
            assert dataset["withheld_fields"] == [
                "min_value",
                "max_value",
                "value_domain",
            ]
            for column in dataset["columns"]:
                assert not {"min_value", "max_value", "value_domain"} & set(column)

    def test_a_profile_over_a_full_cache_carries_no_value(
        self, engine, full_engine, store
    ) -> None:
        full_engine.profile(SENTINEL_TABLE, SENTINEL_CHILD_TABLE)
        assert find_sentinels("".join(store.saved_caches)), "the full run wrote values"

        result = engine.profile(SENTINEL_TABLE, SENTINEL_CHILD_TABLE)

        assert result.cache_hit_count == 2
        self.assert_no_sentinel(result, store.ledger_rows)
        # Served projected, and the full entries normal callers read are left
        # exactly as the full run wrote them: the mode replaces nothing.
        cache = store.load_cache()
        assert cache is not None
        assert not any(d.values_withheld for d in cache.datasets)

    def test_the_columns_shown_do_not_depend_on_the_cache(
        self, warehouse, tmp_path
    ) -> None:
        cold = self.make_engine(warehouse, RecordingStore(), "values_free")
        warm_store = RecordingStore()
        self.make_engine(warehouse, warm_store, "full").profile(SENTINEL_TABLE)
        warm = self.make_engine(warehouse, warm_store, "values_free")

        def shown(engine: DexEngine) -> list:
            data = to_envelope(engine.profile(SENTINEL_TABLE)).data
            return [c["name"] for c in data["datasets"][0]["columns"]]

        assert shown(cold) == shown(warm)

    def test_a_values_free_profile_is_never_served_as_full(
        self, engine, full_engine
    ) -> None:
        engine.profile(SENTINEL_TABLE)

        result = full_engine.profile(SENTINEL_TABLE)

        assert result.profiled_count == 1, "a withheld entry was served as complete"
        assert not result.datasets[0].values_withheld

    def test_map_relationships_inventory_and_diagram_carry_no_value(
        self, engine, store
    ) -> None:
        mapped = engine.map(full=True, verify=True)
        related = engine.relationships(verify=True)
        listed = engine.inventory(rank=True)
        drawn = engine.diagram(full=True)

        for result in (mapped, related, listed, drawn):
            assert result.result_mode is ResultMode.VALUES_FREE
        assert mapped.cache is not None
        assert all(d.values_withheld for d in mapped.cache.datasets if d.columns)
        self.assert_no_sentinel(
            mapped, related, listed, drawn, *store.saved_caches, store.ledger_rows
        )

    # --- query ----------------------------------------------------------------

    @pytest.mark.parametrize("sql", ACCEPTED_QUERIES)
    def test_an_accepted_query_returns_numbers_only(self, engine, sql) -> None:
        engine.profile(SENTINEL_TABLE, SENTINEL_CHILD_TABLE)

        result = engine.query(sql)

        assert result.result_mode is ResultMode.VALUES_FREE
        assert result.row_count == 1
        assert not result.truncated
        for value in result.cells[0]:
            assert value is None or isinstance(value, (int, float, Decimal)), value

    @pytest.mark.parametrize(("sql", "code"), REFUSED_QUERIES)
    def test_a_refused_query_returns_its_code_and_no_cell(
        self, engine, store, sql, code
    ) -> None:
        engine.profile(SENTINEL_TABLE, SENTINEL_CHILD_TABLE)

        with pytest.raises(ValuesFreeError) as refused:
            engine.query(sql)

        assert refused.value.code is code
        self.assert_no_sentinel(refused.value, store.ledger_rows)

    def test_a_batch_refuses_per_statement_and_carries_no_refused_cell(
        self, engine, store
    ) -> None:
        engine.profile(SENTINEL_TABLE, SENTINEL_CHILD_TABLE)
        refused = [sql for sql, _code in REFUSED_QUERIES]

        result = engine.query_batch(*ACCEPTED_QUERIES, *refused, CAST_FAILURE_QUERY)

        assert result.result_mode is ResultMode.VALUES_FREE
        statuses = [r.status for r in result.results]
        assert statuses[: len(ACCEPTED_QUERIES)] == ["ok"] * len(ACCEPTED_QUERIES)
        for statement, (_sql, code) in zip(
            result.results[len(ACCEPTED_QUERIES) :], REFUSED_QUERIES, strict=False
        ):
            assert statement.status == "refused"
            assert statement.code == code.value
            assert statement.cells == []
        last = result.results[-1]
        assert last.status == "failed"
        assert last.code == ValuesFreeCode.WAREHOUSE_REFUSED.value
        assert last.cells == []
        # Accepted cells are aggregates over the sentinel rows and so near their
        # values by design; everything else in the batch must carry none.
        self.assert_no_sentinel(
            *[r.model_dump_json() for r in result.results if r.status != "ok"],
            result.warnings,
            result.notes,
            store.ledger_rows,
        )

    def test_a_query_across_targets_holds_to_the_mode(self, engine) -> None:
        engine.profile(SENTINEL_TABLE)

        result = engine.query_across(
            "select count(*) from {ds}.customers",
            "select email from {ds}.customers",
            targets=["main"],
        )

        assert [r.status for r in result.results] == ["ok", "refused"]
        assert result.results[1].code == ValuesFreeCode.BARE_COLUMN.value

    def test_a_warehouse_error_quotes_no_value(self, engine, store) -> None:
        engine.profile(SENTINEL_TABLE)

        with pytest.raises(WarehouseQueryError) as failed:
            engine.query(CAST_FAILURE_QUERY)

        self.assert_no_sentinel(failed.value, store.ledger_rows)
        assert failed.value.__cause__ is None
        envelope = env.error_for(failed.value)
        assert envelope.reason is env.Reason.EXECUTION_FAILURE
        assert envelope.data["code"] == ValuesFreeCode.WAREHOUSE_REFUSED.value

    def test_the_same_cast_error_does_quote_the_value_outside_the_mode(
        self, full_engine
    ) -> None:
        """The control: without it, a DuckDB release that stopped quoting the value
        would pass the test above while proving nothing."""

        full_engine.profile(SENTINEL_TABLE)

        with pytest.raises(WarehouseQueryError) as failed:
            full_engine.query(CAST_FAILURE_QUERY)

        assert find_sentinels(str(failed.value))

    # --- the mode itself ------------------------------------------------------

    def test_a_pinned_engine_refuses_a_call_asking_for_full_results(
        self, engine
    ) -> None:
        with pytest.raises(ValuesFreeError) as refused:
            engine.profile(SENTINEL_TABLE, result_mode="full")

        assert refused.value.code is ValuesFreeCode.RESULT_MODE_PINNED

    def test_a_full_engine_lets_one_call_opt_in(self, full_engine) -> None:
        result = full_engine.profile(SENTINEL_TABLE, result_mode="values_free")

        assert result.result_mode is ResultMode.VALUES_FREE
        self.assert_no_sentinel(result)

    def test_a_command_not_reviewed_for_the_mode_refuses(self, engine) -> None:
        engine.profile(SENTINEL_TABLE)
        for call in (
            lambda: engine.cluster(SENTINEL_TABLE),
            lambda: engine.semantic_values("tier"),
            lambda: engine.check(),
            lambda: engine.verify(),
            lambda: engine.plans(),
        ):
            with pytest.raises(ValuesFreeError) as refused:
                call()
            assert refused.value.code is ValuesFreeCode.COMMAND_NOT_VALUES_FREE

    def test_the_repository_config_cannot_select_the_mode(
        self, warehouse, tmp_path
    ) -> None:
        root = tmp_path / "repo"
        (root / ".dex").mkdir(parents=True)
        (root / ".dex" / "config.yml").write_text(
            f"connector: duckdb\nduckdb:\n  path: {warehouse}\nresult_mode: full\n",
            encoding="utf-8",
        )

        pinned = DexEngine.from_repo(root, result_mode="values_free")
        unpinned = DexEngine.from_repo(root)

        assert pinned.result_mode is ResultMode.VALUES_FREE
        assert unpinned.result_mode is ResultMode.FULL
        assert any("result_mode" in w for w in unpinned.config.load_warnings)

    # --- the tripwire ---------------------------------------------------------

    def test_every_field_is_known(self, engine) -> None:
        outputs = {
            "profile": engine.profile(SENTINEL_TABLE, SENTINEL_CHILD_TABLE),
            "map": engine.map(full=True, verify=True),
            "relationships": engine.relationships(verify=True),
            "inventory": engine.inventory(rank=True),
            "diagram": engine.diagram(full=True),
            "query": engine.query(
                "select count(*), count(c.email) from customers c "
                "join orders o on o.customer_id = c.id"
            ),
            "query_batch": engine.query_batch(
                "select count(*) from customers",
                "select email from customers",
                CAST_FAILURE_QUERY,
            ),
        }

        unknown = {
            command: _unknown_paths(command, to_envelope(result).data)
            for command, result in outputs.items()
        }

        assert unknown == {command: [] for command in outputs}, (
            "a result field values-free mode was never reviewed for; decide whether "
            "it can carry a value and add it to KNOWN_FIELDS"
        )
