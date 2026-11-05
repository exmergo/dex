# ruff: noqa: S608 -- fixture SQL over test-owned relation names
"""Join coverage through a filtered CTE, measured over the rows the model
actually joins (#1085).

The fixture is synthetic: accounts whose history keeps every version with an
``is_active`` flag, and events whose history keeps a ``_is_current`` flag. The
model reads both through ``where`` CTEs, the shape a current-row selection
compiles to. Each case builds real DuckDB relations, writes the manifest dbt
would have written and runs ``maintain verify`` end to end, asserting the
aggregate evidence *and* the record, never a key value.
"""

from __future__ import annotations

import pytest
import sqlglot

from exmergo_dex_core import sql_shape
from exmergo_dex_core.explore import relationships as rel_mod
from exmergo_dex_core.maintain import verify as verify_mod

from .test_join_coverage import (
    W,
    _codes,
    _node,
    _records,
    _verify,
)
from .test_verify import _write_artifacts


@pytest.fixture(autouse=True)
def _assume_the_project_compiles(monkeypatch: pytest.MonkeyPatch):
    """These tests write the manifest dbt would have, so dbt's own parse of
    the fixture project is not what is under test."""

    monkeypatch.setattr(
        verify_mod,
        "shadow_parse",
        lambda *a, **k: {
            "available": True,
            "reason": None,
            "success": True,
            "messages": [],
        },
    )


_CURRENT = (
    "with active_accounts as ("
    f"select * from {W}.account_history where is_active), "
    "current_events as ("
    f"select * from {W}.event_history where coalesce(_is_current, true)) "
    "select e.event_id, a.account_name "
    "from current_events e {side} join active_accounts a "
    "on e.account_id = a.account_id"
)


def _sql(side: str = "left") -> str:
    return _CURRENT.replace("{side}", side)


def _history(repo, accounts: list[tuple], events: list[tuple]) -> None:
    """``accounts``: (account_id, account_name, is_active); ``events``:
    (event_id, account_id, _is_current)."""

    def values(rows):
        return (
            ", ".join(
                "(" + ", ".join("NULL" if v is None else repr(v) for v in row) + ")"
                for row in rows
            )
            .replace("True", "TRUE")
            .replace("False", "FALSE")
        )

    repo.sql(
        "CREATE TABLE account_history (account_id VARCHAR, account_name VARCHAR, "
        "is_active BOOLEAN)",
        "CREATE TABLE event_history (event_id INTEGER, account_id VARCHAR, "
        "_is_current BOOLEAN)",
    )
    if accounts:
        repo.sql(f"INSERT INTO account_history VALUES {values(accounts)}")
    if events:
        repo.sql(f"INSERT INTO event_history VALUES {values(events)}")


def _model(repo, sql: str, name: str = "rpt_events", **node) -> None:
    repo.sql(f"CREATE OR REPLACE TABLE {name} AS {sql}")
    _write_artifacts(
        repo.project_dir,
        nodes={f"model.maintain_test.{name}": _node(name, sql, **node)},
        results=[],
    )


# --- the shape: which filters are carried, which stay unchecked ----------------


def _joins(sql: str, dialect: str = "duckdb"):
    return verify_mod._model_joins(
        sqlglot.parse_one(sql, read=dialect), sql_shape, dialect
    )


def test_a_simple_active_filter_is_carried_onto_both_sides():
    (candidate,), skipped = _joins(_sql())
    assert skipped == []
    assert candidate.from_relation == "warehouse.main.event_history"
    assert candidate.from_filters == ('COALESCE("_is_current", TRUE)',)
    assert candidate.from_scopes == ("CTE `current_events`",)
    assert candidate.to_relation == "warehouse.main.account_history"
    assert candidate.to_filters == ('"is_active"',)
    assert candidate.scope_filters() == (
        '(COALESCE("_is_current", TRUE))',
        '("is_active")',
    )


def test_filters_accumulate_across_hops_and_through_renames():
    (candidate,), skipped = _joins(
        f"with a as (select id, coalesce(flag, false) as on_, k from {W}.t), "
        "b as (select * from a where on_ and k > 1), "
        "c as (select id as acc, on_, k from b where k < 9) "
        f"select * from {W}.o o join c on o.id = c.acc"
    )
    assert skipped == []
    assert candidate.to_relation == "warehouse.main.t"
    assert candidate.to_columns == ("id",)
    assert set(candidate.to_filters) == {
        'COALESCE("flag", FALSE)',
        '"k" > 1',
        '"k" < 9',
    }
    assert candidate.to_scopes == ("CTE `c`", "CTE `b`")


def test_an_inline_subquery_filter_is_carried_too():
    (candidate,), skipped = _joins(
        f"select * from {W}.o o join (select * from {W}.t where status = 'open') x "
        "on o.id = x.id"
    )
    assert skipped == []
    assert candidate.to_filters == ("\"status\" = 'open'",)
    assert candidate.to_scopes == ("a subquery",)


@pytest.mark.parametrize(
    ("body", "said"),
    [
        (f"select id, count(*) as n from {W}.t where x = 1 group by id", "GROUP BY"),
        (f"select distinct id from {W}.t where x = 1", "DISTINCT"),
        (f"select id from {W}.t where x = 1 limit 5", "LIMIT"),
        (f"select id from {W}.t where ts > current_date", "CurrentDate"),
        (f"select id from {W}.t where id in (select id from {W}.u)", "IN"),
        (f"select id from {W}.t where abs(x) = 1", "Abs"),
        (
            f"select t.id from {W}.t t join {W}.u u on t.id = u.id where u.ok",
            "join beside its filter",
        ),
    ],
)
def test_an_unsupported_reducer_stays_filtered_cte_with_its_shape(body, said):
    candidates, (skip,) = _joins(
        f"with b as ({body}) select * from {W}.o o join b on o.id = b.id"
    )
    # A join the CTE writes itself is its own candidate; the outer one is not.
    assert not [c for c in candidates if c.from_relation == "warehouse.main.o"]
    assert skip.code == "filtered_cte"
    assert said in skip.reason
    assert "CTE `b`" in skip.reason


def test_a_filter_on_a_window_column_is_an_unsupported_reducer():
    # The latest-version pattern: `row_number()` in one CTE, `rn = 1` in the
    # next. The filter reads a column the chain computes, so it is not carried.
    candidates, (skip,) = _joins(
        "with ranked as (select id, row_number() over (partition by id "
        f"order by ts desc) as rn from {W}.t), "
        "latest as (select * from ranked where rn = 1) "
        f"select * from {W}.o o join latest l on o.id = l.id"
    )
    assert candidates == []
    assert skip.code == "filtered_cte"
    assert "computes" in skip.reason


def test_a_filter_reading_another_relation_than_the_key_stays_unchecked():
    candidates, (skip,) = _joins(
        f"with j as (select t.id, u.flag from {W}.t t left join {W}.u u "
        "on t.id = u.id), b as (select * from j where flag) "
        f"select * from {W}.o o join b on o.id = b.id"
    )
    assert not [c for c in candidates if c.from_relation == "warehouse.main.o"]
    assert skip.code == "filtered_cte"
    assert "different relation" in skip.reason


def test_a_safe_cast_filter_is_not_carried_where_the_connector_has_none():
    sql = (
        f"with b as (select * from {W}.t where try_cast(code as int) = 1) "
        f"select * from {W}.o o join b on o.id = b.id"
    )
    assert _joins(sql)[1] == []
    _candidates, (skip,) = _joins(sql, "postgres")
    assert skip.code == "filtered_cte"


def test_the_same_join_through_another_filter_is_another_candidate():
    a = _joins(_sql())[0][0]
    b = _joins(_sql().replace("where is_active", "where not is_active"))[0][0]
    assert a != b


# --- measured over the effective input ------------------------------------------


def test_keys_matching_only_inactive_history_are_not_matches(
    maintain_repo,
):
    """Every event key exists in account history, but only in inactive rows.
    Probed against the whole table this join is fully covered; over the rows
    the model joins it matches nothing."""

    _history(
        maintain_repo,
        accounts=[
            ("acc_a", "Alder", False),
            ("acc_b", "Birch", False),
            ("acc_c", "Cedar", True),
        ],
        events=[(1, "acc_a", True), (2, "acc_b", None), (3, "acc_a", True)],
    )
    _model(maintain_repo, _sql())
    data = _verify(maintain_repo, "rpt_events")

    (record,) = _records(data, "rpt_events")
    assert record["status"] == "contradicted"
    assert record["finding"] == "join_no_match"
    assert record["measured"]["eligible_rows"] == 3
    assert record["measured"]["matched_rows"] == 0
    assert record["measured"]["eligible_keys"] == 2
    assert record["effective_input"] == {
        "from": {
            "relation": "warehouse.main.event_history",
            "through": ["CTE `current_events`"],
            "filters": ['COALESCE("_is_current", TRUE)'],
        },
        "to": {
            "relation": "warehouse.main.account_history",
            "through": ["CTE `active_accounts`"],
            "filters": ['"is_active"'],
        },
    }
    finding = _codes(data, "rpt_events")["join_no_match"]
    assert "rows where" in finding["detail"]
    assert finding["data"]["effective_input"] == record["effective_input"]
    assert not any("filtered_cte" in w for w in data.get("warnings") or [])


def test_a_partial_relationship_counts_the_filtered_rows_and_keys(
    maintain_repo,
):
    """One active match, one inactive-only key, one unknown key, and a
    non-current event that would match: the denominators are the current
    events alone, and the inner join's orphans are reported against them."""

    _history(
        maintain_repo,
        accounts=[
            ("acc_a", "Alder", True),
            ("acc_a", "Alder (old)", False),
            ("acc_b", "Birch", False),
        ],
        events=[
            (1, "acc_a", True),
            (2, "acc_a", True),
            (3, "acc_b", True),
            (4, "acc_z", True),
            (5, "acc_a", False),
        ],
    )
    _model(maintain_repo, _sql("inner"))
    data = _verify(maintain_repo, "rpt_events")

    (record,) = _records(data, "rpt_events")
    assert record["measured"]["rows"] == 4
    assert record["measured"]["eligible_rows"] == 4
    assert record["measured"]["matched_rows"] == 2
    assert record["measured"]["eligible_keys"] == 3
    assert record["measured"]["matched_keys"] == 1
    assert record["coverage"] == {"rows": 0.5, "keys": 0.3333}
    assert record["status"] == "contradicted"
    assert record["finding"] == "join_orphans"


def test_a_healthy_filtered_join_is_satisfied(
    maintain_repo,
):
    _history(
        maintain_repo,
        accounts=[("acc_a", "Alder", True), ("acc_b", "Birch", True)],
        events=[(1, "acc_a", True), (2, "acc_b", True), (3, "acc_q", False)],
    )
    _model(maintain_repo, _sql("inner"))
    (record,) = _records(_verify(maintain_repo, "rpt_events"), "rpt_events")
    assert record["status"] == "satisfied"
    assert record["measured"]["eligible_rows"] == 2


def test_no_current_rows_is_not_applicable_not_full_coverage(
    maintain_repo,
):
    _history(
        maintain_repo,
        accounts=[("acc_a", "Alder", True)],
        events=[(1, "acc_a", False), (2, "acc_a", False)],
    )
    _model(maintain_repo, _sql())
    data = _verify(maintain_repo, "rpt_events")
    (record,) = _records(data, "rpt_events")
    assert record["status"] == "not_applicable"
    assert record["reason_code"] == "empty_input"
    assert record["coverage"] == {"rows": None, "keys": None}
    assert "rows where" in record["reason"]
    assert not [f for f in data["findings"] if f["axis"] == "join_contract"]


def test_null_keys_are_counted_within_the_filter_and_follow_the_policy(
    maintain_repo,
):
    _history(
        maintain_repo,
        accounts=[("acc_a", "Alder", True)],
        events=[
            (1, "acc_a", True),
            (2, None, True),
            (3, None, False),
        ],
    )
    _model(maintain_repo, _sql())
    (record,) = _records(_verify(maintain_repo, "rpt_events"), "rpt_events")
    # The non-current NULL row is outside the population, the current one is
    # counted apart and, with no declaration, excluded.
    assert record["measured"]["rows"] == 2
    assert record["measured"]["null_key_rows"] == 1
    assert record["measured"]["eligible_rows"] == 1
    assert record["declared"]["null_keys"] == "excluded"
    assert record["status"] == "satisfied"


def test_an_unsupported_reducer_is_unchecked_end_to_end(
    maintain_repo,
):
    _history(
        maintain_repo,
        accounts=[("acc_a", "Alder", True)],
        events=[(1, "acc_a", True)],
    )
    sql = (
        "with firsts as (select distinct account_id, first_value(event_id) over "
        "(partition by account_id order by event_id) as first_event "
        f"from {W}.event_history where _is_current) "
        f"select a.account_id, f.first_event from {W}.account_history a "
        "left join firsts f on a.account_id = f.account_id"
    )
    _model(maintain_repo, sql)
    data = _verify(maintain_repo, "rpt_events")
    (record,) = _records(data, "rpt_events")
    assert record["status"] == "unchecked"
    assert record["reason_code"] == "filtered_cte"
    assert "DISTINCT" in record["reason"]
    assert not [f for f in data["findings"] if f["axis"] == "join_contract"]


# --- the probe ------------------------------------------------------------------


def _filtered_relationship():
    from exmergo_dex_core.cache import Relationship, RelationshipKind

    return Relationship(
        from_dataset="db.s.event_history",
        from_columns=["account_id"],
        to_dataset="db.s.account_history",
        to_columns=["account_id"],
        kind=RelationshipKind.DECLARED,
    )


@pytest.mark.parametrize(
    "dialect",
    [
        "duckdb",
        "bigquery",
        "snowflake",
        "databricks",
        "redshift",
        "postgres",
        "clickhouse",
        "trino",
    ],
)
def test_the_filtered_probe_is_one_aggregate_select_on_every_dialect(dialect):
    from exmergo_dex_core.guards.sql_guard import assert_select_only

    rel = _filtered_relationship()
    (statement,) = rel_mod.coverage_statements(
        [rel],
        dialect,
        scope_filters={
            id(rel): ('(COALESCE("_is_current", TRUE))', "(\"status\" IN ('a', 'b'))")
        },
    )
    assert assert_select_only(statement, dialect=dialect)
    assert statement.upper().count("WHERE") >= 3


def test_one_child_read_through_two_filters_is_two_populations():
    duckdb = pytest.importorskip("duckdb")
    from exmergo_dex_core.cache import Relationship, RelationshipKind

    conn = duckdb.connect()
    conn.execute("CREATE TABLE p (id VARCHAR)")
    conn.execute("INSERT INTO p VALUES ('a')")
    conn.execute("CREATE TABLE c (pid VARCHAR, cur BOOLEAN)")
    conn.execute("INSERT INTO c VALUES ('a', TRUE), ('b', TRUE), ('a', FALSE)")
    edges = [
        Relationship(
            from_dataset="c",
            from_columns=["pid"],
            to_dataset="p",
            to_columns=["id"],
            kind=RelationshipKind.DECLARED,
        )
        for _ in range(2)
    ]
    sql = rel_mod.coverage_probe_sql(
        edges, scope_filters={id(edges[0]): ('("cur")', None)}
    )
    row = dict(
        zip(
            [d[0] for d in conn.execute(sql).description],
            conn.execute(sql).fetchone(),
            strict=True,
        )
    )
    assert (row["dex_cov_rows_0"], row["dex_cov_matched_0"]) == (2, 1)
    assert (row["dex_cov_rows_1"], row["dex_cov_matched_1"]) == (3, 2)


def test_a_declared_never_null_column_read_through_the_filter_makes_it_required(
    maintain_repo,
):
    """The column the join supplies is read through the filtered CTE too. It is
    still that physical column, so `null_rule: never` makes the LEFT join
    required, and an unmatched current row contradicts the contract."""

    _history(
        maintain_repo,
        accounts=[("acc_a", "Alder", True), ("acc_b", "Birch", False)],
        events=[(1, "acc_a", True), (2, "acc_b", True)],
    )
    _model(
        maintain_repo,
        _sql(),
        columns={"account_name": {"role": "attribute", "null_rule": "never"}},
    )
    (record,) = _records(_verify(maintain_repo, "rpt_events"), "rpt_events")
    assert record["declared"]["required"] is True
    assert record["declared"]["derived_columns"] == ["account_name"]
    assert record["threshold_source"] == "contract"
    assert record["status"] == "contradicted"
