"""`transform compare --expect`: an intended row change held to the rows (#1157).

The comparator half, on DuckDB: each clause (none, any, a predicate, an IN
lookup), changed columns allowed and not, the grain, the row-level fallback
with no key, the refusals, and the key sample, which is the one output that
carries values and goes through the firewall like any read.
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import pytest

from exmergo_dex_core.adapters.duckdb import DuckDBAdapter
from exmergo_dex_core.cache import ColumnProfile, Dataset, DexCache, PIIFlag
from exmergo_dex_core.config import QueryLimits
from exmergo_dex_core.transform.expected_change import (
    Expectation,
    ExpectationError,
    lookup_relations,
    parse_expectation,
    resolve_refs,
)
from exmergo_dex_core.transform.multiset_compare import compare_refactors

_EMAIL = "hidden-key@example.com"


def _dataset(name: str, *columns: tuple[str, str], pii: str | None = None) -> Dataset:
    """A cached profile of ``main.<name>``, with ``pii`` flagged as an email."""

    return Dataset(
        identifier=f"main.{name}",
        columns=[
            ColumnProfile(
                name=column,
                data_type=kind,
                pii=PIIFlag(category="email", confidence=0.95)
                if column == pii
                else None,
            )
            for column, kind in columns
        ],
    )


@pytest.fixture
def warehouse(tmp_path: Path):
    """A DuckDB warehouse with the four tables and their cached profiles."""

    path = tmp_path / "expect.duckdb"
    connection = duckdb.connect(str(path))
    connection.execute("create table daily (day date, visits integer)")
    connection.execute(
        "insert into daily values ('2026-01-01', 3), ('2026-01-02', 0), "
        "('2026-01-03', 5), ('2026-01-04', 0)"
    )
    connection.execute("create table workspaces (id integer, archived boolean)")
    connection.execute("insert into workspaces values (1, false), (2, true)")
    connection.execute("create table members (id integer, workspace_id integer)")
    connection.execute("insert into members values (1, 1), (2, 2), (3, 1)")
    connection.execute("create table usage (token_count integer, calls integer)")
    connection.execute("insert into usage values (1, 5), (2, 6)")
    connection.execute("create table people (email varchar, score integer)")
    connection.execute(
        "insert into people values (?, 1), ('ada@example.com', 2)", [_EMAIL]
    )
    connection.close()
    adapter = DuckDBAdapter(path)
    cache = DexCache(
        datasets=[
            _dataset("daily", ("day", "DATE"), ("visits", "INTEGER")),
            _dataset("workspaces", ("id", "INTEGER"), ("archived", "BOOLEAN")),
            _dataset("members", ("id", "INTEGER"), ("workspace_id", "INTEGER")),
            _dataset("usage", ("token_count", "INTEGER"), ("calls", "INTEGER")),
            _dataset("people", ("email", "VARCHAR"), ("score", "INTEGER"), pii="email"),
        ]
    )
    try:
        yield adapter, cache
    finally:
        adapter.close()


def _compare(warehouse, left: str, right: str, expect, keys=None, **kwargs):
    adapter, cache = warehouse
    return compare_refactors(
        left,
        right,
        adapter=adapter,
        cache=cache,
        limits=QueryLimits(),
        command="transform compare",
        key_columns=keys,
        expectation=parse_expectation(expect),
        **kwargs,
    )


SPARSE = "select day, visits from daily where visits > 0"
FILLED = "select day, visits from daily"


def test_a_backfill_that_adds_only_the_stated_rows_is_as_expected(warehouse):
    result = _compare(
        warehouse, SPARSE, FILLED, {"added": {"where": "visits = 0"}}, ["day"]
    )
    report = result.expectation.data()
    # The plain comparison still says the rows differ; the verdict is the report's.
    assert result.status == "different"
    assert report["verdict"] == "as_expected"
    assert report["unit"] == "keys"
    assert report["added"] == {"total": 2, "matching": 2, "unexpected": 0}
    assert report["grain"] == {
        "key_columns": ["day"],
        "holds": True,
        "non_unique_keys": 0,
    }
    assert "unexpected_keys" not in report


def test_an_over_broad_backfill_breaks_the_grain(warehouse):
    """The ADE-bench case: zero rows for combinations that already had data."""

    broad = FILLED + " union all select day, 0 from daily where visits > 0"  # noqa: S608
    report = _compare(
        warehouse, SPARSE, broad, {"added": {"where": "visits = 0"}}, ["day"]
    ).expectation.data()
    assert report["verdict"] == "unexpected"
    assert "grain_not_held" in report["findings"]
    assert report["grain"]["holds"] is False
    assert report["grain"]["non_unique_keys"] == 2


def test_a_backfill_that_adds_a_row_outside_the_predicate_is_named(warehouse):
    broad = f"{FILLED} union all select date '2026-02-01', 9"
    report = _compare(
        warehouse, SPARSE, broad, {"added": {"where": "visits = 0"}}, ["day"]
    ).expectation.data()
    assert report["findings"] == ["unexpected_added"]
    assert report["added"] == {"total": 3, "matching": 2, "unexpected": 1}
    assert report["unexpected_keys"] == {"added": [{"day": "2026-02-01"}]}


ALL_MEMBERS = "select id, workspace_id from members"
ARCHIVED = "workspace_id in (select id from workspaces where archived)"


def test_an_exclusion_through_a_lookup_is_as_expected(warehouse):
    kept = ALL_MEMBERS + (  # noqa: S608
        " where workspace_id not in (select id from workspaces where archived)"
    )
    report = _compare(
        warehouse, ALL_MEMBERS, kept, {"removed": {"where": ARCHIVED}}, ["id"]
    ).expectation.data()
    assert report["verdict"] == "as_expected"
    assert report["removed"] == {"total": 1, "matching": 1, "unexpected": 0}


def test_an_exclusion_that_drops_more_than_it_states_names_the_keys(warehouse):
    over = f"{ALL_MEMBERS} where id = 3"
    report = _compare(
        warehouse, ALL_MEMBERS, over, {"removed": {"where": ARCHIVED}}, ["id"]
    ).expectation.data()
    assert report["findings"] == ["unexpected_removed"]
    assert report["removed"] == {"total": 2, "matching": 1, "unexpected": 1}
    assert report["unexpected_keys"] == {"removed": [{"id": 1}]}


SHIFTED = "select id, workspace_id + 1 as workspace_id from members"


def test_a_changed_column_must_be_named(warehouse):
    report = _compare(warehouse, ALL_MEMBERS, SHIFTED, {}, ["id"]).expectation.data()
    assert report["findings"] == ["unexpected_changed_column"]
    assert report["changed"] == {"total": 3, "matching": 0, "unexpected": 3}
    assert report["columns"] == [
        {"column": "workspace_id", "keys_changed": 3, "allowed": False}
    ]


def test_a_named_changed_column_is_as_expected(warehouse):
    report = _compare(
        warehouse, ALL_MEMBERS, SHIFTED, {"changed_columns": ["workspace_id"]}, ["id"]
    ).expectation.data()
    assert report["verdict"] == "as_expected"
    assert report["columns"][0]["allowed"] is True


def test_a_null_that_becomes_a_value_is_a_change(warehouse):
    left = "select id, cast(null as integer) as workspace_id from members"
    report = _compare(warehouse, left, ALL_MEMBERS, {}, ["id"]).expectation.data()
    assert report["changed"]["total"] == 3


def test_a_predicate_that_is_null_does_not_match(warehouse):
    report = _compare(
        warehouse,
        SPARSE,
        "select day, cast(null as integer) as visits from daily where visits = 0 "  # noqa: S608
        "union all " + SPARSE,
        {"added": {"where": "visits = 0"}},
        ["day"],
    ).expectation.data()
    assert report["added"] == {"total": 2, "matching": 0, "unexpected": 2}


def test_no_key_judges_rows(warehouse):
    report = _compare(
        warehouse, ALL_MEMBERS, f"{ALL_MEMBERS} where id < 3", {"removed": "any"}
    ).expectation.data()
    assert report["verdict"] == "as_expected"
    assert report["unit"] == "rows"
    assert report["grain"]["reason"] == "no_key"
    assert report["samples_withheld"] == "no_key"


def test_a_baseline_key_that_is_not_unique_falls_back_to_rows(warehouse):
    report = _compare(
        warehouse,
        "select 1 as id, workspace_id from members",
        "select 1 as id, workspace_id from members where workspace_id = 1",
        {"removed": "any"},
        ["id"],
    ).expectation.data()
    assert report["unit"] == "rows"
    assert report["grain"]["reason"] == "key_not_unique_on_baseline"


def test_equal_results_are_as_expected_under_any_expectation(warehouse):
    result = _compare(warehouse, ALL_MEMBERS, ALL_MEMBERS, {}, ["id"])
    assert result.status == "equivalent"
    assert result.expectation.verdict == "as_expected"


def test_a_schema_change_is_unexpected(warehouse):
    result = _compare(
        warehouse, ALL_MEMBERS, "select id from members", {"removed": "any"}
    )
    assert result.reason_code == "schemas_differ"
    assert result.expectation.findings == ("schema_changed",)


@pytest.mark.parametrize(
    ("expect", "keys", "message"),
    [
        ({"removed": {"where": "sum(id) > 1"}}, ["id"], "aggregates"),
        ({"removed": {"where": "nope = 1"}}, ["id"], "not an output column"),
        ({"changed_columns": ["workspace_id"]}, None, "needs a key"),
        ({"changed_columns": ["id"]}, ["id"], "key column"),
        (
            {"removed": {"where": "id in (select id from workspaces group by id)"}},
            ["id"],
            "no join, grouping",
        ),
    ],
)
def test_an_expectation_that_does_not_fit_is_not_checked(
    warehouse, expect, keys, message
):
    result = _compare(warehouse, ALL_MEMBERS, ALL_MEMBERS, expect, keys)
    assert result.status == "not_checked"
    assert result.reason_code == "invalid_expectation"
    assert message in result.detail
    assert result.expectation is None


def test_a_pii_key_is_withheld_from_the_sample_and_the_counts_stay(warehouse):
    report = _compare(
        warehouse,
        "select email, score from people",
        "select email, score from people where score > 1",
        {},
        ["email"],
    ).expectation
    assert report.findings == ("unexpected_removed",)
    assert report.removed.total == 1
    assert report.unexpected_keys is None
    assert report.samples_withheld == "firewall_refused"
    assert _EMAIL not in repr(report)


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ("{", "not JSON"),
        ([], "JSON object"),
        ({"added": 3}, "added is"),
        ({"addded": "none"}, "not addded"),
        ({"changed_columns": "x"}, "list of output column"),
        ({"key": "changed"}, "key is"),
    ],
)
def test_a_malformed_expectation_is_refused(raw, message):
    """Malformed JSON or a wrong field is refused, naming the field."""

    with pytest.raises(ExpectationError, match=message):
        parse_expectation(raw)


def test_the_issue_spelling_and_defaults_parse():
    stated = parse_expectation({"expected_change": {"added": {"where": "x = 1"}}})
    assert stated == Expectation(added="x = 1")
    assert stated.data()["removed"] == "none"
    assert (
        stated.digest() == parse_expectation('{"added": {"where": "x = 1"}}').digest()
    )


def test_ref_resolves_to_the_relation_the_comparison_reads():
    """``ref('<model>')`` resolves to the relation either side reads, or is refused."""

    stated = parse_expectation(
        {"removed": {"where": "w in (select id from ref('stg_workspaces') where a)"}}
    )
    resolved = resolve_refs(stated, ['"dev"."main"."stg_workspaces"'], "duckdb")
    assert '"dev"."main"."stg_workspaces"' in resolved.removed
    assert lookup_relations(resolved, "duckdb") == ['"dev"."main"."stg_workspaces"']
    with pytest.raises(ExpectationError, match="is not one of"):
        resolve_refs(stated, ["main.other"], "duckdb")


def test_a_key_named_like_a_credential_withholds_the_sample(warehouse):
    """The envelope refuses a key that reads as a credential's name, so a sample
    keyed by `token_count` would fail the whole command: it is withheld."""

    report = _compare(
        warehouse,
        "select token_count, calls from usage",
        "select token_count, calls from usage where calls > 5",
        {},
        ["token_count"],
    ).expectation
    assert report.findings == ("unexpected_removed",)
    assert report.removed.total == 1
    assert report.unexpected_keys is None
    assert report.samples_withheld == "key_name_withheld"


def test_rows_say_the_key_clause_was_not_judged(warehouse):
    """With no key, the default ``key: unchanged`` is noted as not judged."""

    report = _compare(
        warehouse,
        ALL_MEMBERS,
        f"{ALL_MEMBERS} where id < 3",
        {"removed": "any"},
    ).expectation.data()
    assert report["verdict"] == "as_expected"
    assert report["grain"]["holds"] is None
    assert any("was not judged" in note for note in report["notes"])
    stated_any = _compare(
        warehouse,
        ALL_MEMBERS,
        f"{ALL_MEMBERS} where id < 3",
        {"removed": "any", "key": "any"},
    ).expectation.data()
    assert "notes" not in stated_any
