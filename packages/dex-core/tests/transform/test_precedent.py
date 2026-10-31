"""Precedent and the nonexistent-literal warning in the declaration draft (#494).

What a project's other models already do with a column is evidence for the
model being planned: these tests pin how it is read (column identity through
aliases and CTEs, conflicting groups kept apart, readers dex could not read
named rather than counted) and when a compared literal is warned about (only
against a complete, exact, non-PII domain).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from exmergo_dex_core.cache import (
    ColumnProfile,
    Dataset,
    DexCache,
    PIICategory,
    PIIFlag,
    ValueCount,
    ValueDomain,
)
from exmergo_dex_core.dbt_project import load
from exmergo_dex_core.references import ReferenceIndex
from exmergo_dex_core.transform.drafting import draft
from exmergo_dex_core.transform.precedent import MAX_GROUPS, usage_of

pytest.importorskip("sqlglot")

EXCLUDE = (
    "select * from {{ ref('stg_orders') }} "
    "where status not in ('cancelled', 'returned')\n"
)


def _domain(*values: str, elided: int = 0) -> ValueDomain:
    return ValueDomain(
        values=[ValueCount(value=v, count=10) for v in values], elided=elided
    )


def _cache(**status: object) -> DexCache:
    fields = {"value_domain": _domain("shipped", "cancelled", "returned")}
    fields.update(status)
    return DexCache(
        datasets=[
            Dataset(
                identifier="warehouse.main.stg_orders",
                columns=[
                    ColumnProfile(name="order_id", data_type="integer"),
                    ColumnProfile(name="status", data_type="varchar", **fields),
                    ColumnProfile(name="amount", data_type="double"),
                    ColumnProfile(
                        name="quantity",
                        data_type="integer",
                        value_domain=ValueDomain(values=[ValueCount(value=1, count=5)]),
                    ),
                ],
            ),
            Dataset(
                identifier="warehouse.main.stg_returns",
                columns=[
                    ColumnProfile(name="return_id", data_type="integer"),
                    ColumnProfile(name="status", data_type="varchar"),
                ],
            ),
        ]
    )


def _project(tmp_path: Path, models: dict[str, str]) -> tuple[dict[str, str], Path]:
    (tmp_path / "dbt_project.yml").write_text(
        "name: shop\nversion: '1.0'\nprofile: shop\nmodel-paths: ['models']\n",
        encoding="utf-8",
    )
    files = {}
    for name, sql in models.items():
        path = tmp_path / "models" / f"{name}.sql"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(sql, encoding="utf-8")
        files[f"models/{name}.sql"] = sql
    return files, tmp_path


def _draft(tmp_path: Path, planned: str, siblings: dict[str, str], cache=None):
    files, root = _project(tmp_path, {**siblings, "fct": planned})
    index = ReferenceIndex(load(root), scan_packages=False)
    drafts, warnings = draft(
        ["fct"],
        files,
        ["models"],
        cache if cache is not None else _cache(),
        dialect="duckdb",
        index=lambda: index,
    )
    return drafts["fct"]["drafted"], warnings


def _status(drafted: dict) -> dict:
    return next(p for p in drafted["precedent"] if p["column"] == "stg_orders.status")


# --- precedent ------------------------------------------------------------------------


def test_conflicting_precedent_is_grouped_and_counted_never_reduced_to_a_rule(
    tmp_path,
):
    siblings = {f"m{i}": EXCLUDE for i in range(4)}
    siblings["keeps_all"] = "select order_id, status from {{ ref('stg_orders') }}\n"
    drafted, warnings = _draft(
        tmp_path,
        "select order_id from {{ ref('stg_orders') }} where status = 'shipped'\n",
        siblings,
    )

    status = _status(drafted)
    assert status["this_model"] == [
        {"usage": "where", "expression": "status = 'shipped'"}
    ]
    assert status["readers"] == 5
    assert status["completeness"] == "complete"
    assert status["limits"] == []
    [excluding, keeping] = status["groups"]
    assert excluding == {
        "usage": "where",
        "expression": "NOT status IN ('cancelled', 'returned')",
        "count": 4,
        "models": ["m0", "m1", "m2", "m3"],
        "elided_models": 0,
    }
    assert keeping == {
        "usage": "none",
        "count": 1,
        "models": ["keeps_all"],
        "elided_models": 0,
    }
    assert any(
        "drafted.precedent shows how the project's other models treat "
        "stg_orders.status (5)" in w
        for w in warnings
    )


def test_a_column_is_the_same_column_only_through_its_lineage(tmp_path):
    siblings = {
        # An alias and a CTE that renames the relation: both read stg_orders.status.
        "aliased": "select o.order_id from {{ ref('stg_orders') }} o "
        "where o.status <> 'cancelled'\n",
        "via_cte": "with orders as (select * from {{ ref('stg_orders') }})\n"
        "select order_id from orders where status != 'cancelled'\n",
        # Same column name on an unrelated relation is not precedent.
        "returns": "select return_id from {{ ref('stg_returns') }} "
        "where status <> 'cancelled'\n",
    }
    drafted, _ = _draft(
        tmp_path,
        "select order_id from {{ ref('stg_orders') }} where status = 'shipped'\n",
        siblings,
    )

    [group] = _status(drafted)["groups"]
    assert group["expression"] == "status <> 'cancelled'"
    assert group["models"] == ["aliased", "via_cte"]


def test_case_branches_and_aggregations_are_their_own_groups(tmp_path):
    siblings = {
        "bucketed": "select case when status = 'shipped' then 1 else 0 end as done "
        "from {{ ref('stg_orders') }}\n",
        "summed": "select sum(amount) as revenue from {{ ref('stg_orders') }}\n",
        "averaged": "select avg(o.amount) as aov from {{ ref('stg_orders') }} o\n",
    }
    drafted, _ = _draft(
        tmp_path,
        "select sum(amount) as revenue from {{ ref('stg_orders') }} "
        "where status = 'shipped'\n",
        siblings,
    )

    status = _status(drafted)
    assert {(g["usage"], g.get("expression")) for g in status["groups"]} == {
        ("case", "status = 'shipped'"),
        ("none", None),
    }
    amount = next(p for p in drafted["precedent"] if p["column"] == "stg_orders.amount")
    assert amount["this_model"] == [
        {"usage": "aggregation", "expression": "SUM(amount)"}
    ]
    assert {(g["usage"], g.get("expression")) for g in amount["groups"]} == {
        ("aggregation", "SUM(amount)"),
        ("aggregation", "AVG(amount)"),
        ("none", None),
    }


def test_a_reader_dex_cannot_read_makes_the_answer_incomplete_and_is_not_counted(
    tmp_path,
):
    siblings = {
        "macro": "select {{ dbt_utils.star(ref('stg_orders')) }} "
        "from {{ ref('stg_orders') }} where status <> 'cancelled'\n",
        "dynamic": "select * from {{ ref(var('orders_model')) }}\n",
        "plain": EXCLUDE,
    }
    drafted, _ = _draft(
        tmp_path,
        "select order_id from {{ ref('stg_orders') }} where status = 'shipped'\n",
        siblings,
    )

    status = _status(drafted)
    assert status["completeness"] == "incomplete"
    assert status["readers"] == 1
    assert [g["models"] for g in status["groups"]] == [["plain"]]
    assert any(
        limit.startswith("'macro' reads stg_orders") for limit in status["limits"]
    )
    assert any(
        "could not be resolved statically" in limit for limit in status["limits"]
    )


def test_groups_are_capped_with_the_rest_counted_and_full_lifts_the_cap(tmp_path):
    siblings = {
        f"m{i:02d}": f"select * from {{{{ ref('stg_orders') }}}} where status = 's{i}'"  # noqa: S608
        for i in range(MAX_GROUPS + 2)
    }
    files, root = _project(tmp_path, siblings)
    index = ReferenceIndex(load(root), scan_packages=False)
    target = [("stg_orders", "status")]

    capped = usage_of(
        target, files, ["models"], _cache(), dialect="duckdb", vars=None, index=index
    )
    full = usage_of(
        target,
        files,
        ["models"],
        _cache(),
        dialect="duckdb",
        vars=None,
        index=index,
        full=True,
    )

    assert len(capped[("stg_orders", "status")]["groups"]) == MAX_GROUPS
    assert capped[("stg_orders", "status")]["elided_groups"] == 2
    assert len(full[("stg_orders", "status")]["groups"]) == MAX_GROUPS + 2
    assert full[("stg_orders", "status")]["elided_groups"] == 0


def test_no_index_drafts_no_precedent(tmp_path):
    files, _ = _project(tmp_path, {"fct": EXCLUDE})
    drafts, _ = draft(["fct"], files, ["models"], _cache(), dialect="duckdb")
    assert "precedent" not in drafts["fct"]["drafted"]


# --- the literal check ----------------------------------------------------------------


def _literal_warnings(tmp_path: Path, where: str, cache=None) -> list[str]:
    _, warnings = _draft(
        tmp_path,
        f"select order_id from {{{{ ref('stg_orders') }}}} where {where}",  # noqa: S608
        {},
        cache,
    )
    return [w for w in warnings if "profiled values" in w]


def test_a_literal_the_complete_domain_never_holds_is_warned_about(tmp_path):
    [warning] = _literal_warnings(tmp_path, "status = 'refunded'")
    assert warning.startswith(
        "'fct' compares stg_orders.status to 'refunded', which none of its 3 "
        "profiled values is"
    )
    assert "expected to appear later" in warning


def test_a_miscased_literal_names_the_value_it_was_probably_meant_to_be(tmp_path):
    [warning] = _literal_warnings(tmp_path, "status not in ('Cancelled ', 'returned')")
    assert "'Cancelled '" in warning
    assert "'cancelled' matches it ignoring case and whitespace" in warning


_LEFT_JOIN_FILTER = (
    "select c.customer_id, count(o.order_id) as n\n"
    "from {{ ref('stg_customers') }} as c\n"
    "left join {{ ref('stg_orders') }} as o\n"
    "    on o.customer_id = c.customer_id and o.status <> 'Cancelled'\n"
    "group by 1\n"
)


def test_a_literal_in_a_join_condition_is_checked_like_a_where(tmp_path):
    """Under a LEFT JOIN that must keep every parent, the child filter lives in
    the ON clause; a misspelled literal there drops nothing and keeps everything,
    so it is the one place the check matters most."""

    drafted, warnings = _draft(tmp_path, _LEFT_JOIN_FILTER, {})
    [warning] = [w for w in warnings if "profiled values" in w]
    assert "stg_orders.status to 'Cancelled'" in warning
    assert "'cancelled' matches it ignoring case and whitespace" in warning
    # The key equality beside it compares two columns and is not a usage.
    assert [u["usage"] for u in _status(drafted)["this_model"]] == ["join"]


def test_a_join_condition_filter_in_a_sibling_is_precedent(tmp_path):
    sibling = _LEFT_JOIN_FILTER.replace("'Cancelled'", "'cancelled'")
    drafted, _ = _draft(tmp_path, EXCLUDE, {"clv": sibling})
    usages = {group["usage"] for group in _status(drafted)["groups"]}
    assert "join" in usages


@pytest.mark.parametrize(
    ("where", "cache"),
    [
        ("status in ('shipped', 'cancelled')", None),
        # A capped domain cannot prove absence.
        ("status = 'refunded'", _cache(value_domain=_domain("shipped", elided=4))),
        # Compares something other than the column.
        ("lower(status) = 'refunded'", None),
        # A string against a number column is the warehouse's cast to judge.
        ("quantity = '7'", None),
    ],
)
def test_the_literal_check_stays_silent_where_it_could_not_prove_absence(
    tmp_path, where, cache
):
    assert _literal_warnings(tmp_path, where, cache) == []


def test_a_number_the_domain_never_holds_is_warned_about(tmp_path):
    [warning] = _literal_warnings(tmp_path, "quantity = 7")
    assert "stg_orders.quantity to 7" in warning


def test_a_pii_column_is_never_checked_and_no_value_of_it_leaks(tmp_path):
    cache = _cache(pii=PIIFlag(category=PIICategory.EMAIL, confidence=0.3))
    _, warnings = _draft(
        tmp_path,
        "select order_id from {{ ref('stg_orders') }} where status = 'Shipped'\n",
        {},
        cache,
    )
    assert not any("profiled values" in w for w in warnings)
    assert not any("'shipped'" in w for w in warnings)


# --- transform references --usage -----------------------------------------------------


def _references(argv: list[str], capsys) -> tuple[int, dict]:
    import json

    from exmergo_dex_core.cli import main

    rc = main(argv)
    out = capsys.readouterr().out
    assert out.count("\n") == 1, "exactly one envelope line"
    return rc, json.loads(out)


def test_references_usage_answers_before_any_sql_is_written(
    tmp_path, capsys, monkeypatch
):
    import exmergo_dex_core.connect as connect

    def refuse(*_args, **_kwargs):
        raise AssertionError("--usage opened a warehouse connection")

    monkeypatch.setattr(connect, "open_adapter", refuse)
    _project(
        tmp_path,
        {
            "stg_orders": "select * from {{ source('raw', 'orders') }}\n",
            "m0": EXCLUDE,
            "m1": EXCLUDE,
            "keeps_all": "select order_id, status from {{ ref('stg_orders') }}\n",
        },
    )

    rc, envelope = _references(
        [
            "--repo-root",
            str(tmp_path),
            "transform",
            "references",
            "stg_orders.status",
            "--usage",
        ],
        capsys,
    )

    assert rc == 0, envelope
    [target] = [t for t in envelope["data"]["targets"] if "usage" in t]
    usage = target["usage"]
    assert usage["readers"] == 3
    assert [(g["usage"], g["count"]) for g in usage["groups"]] == [
        ("where", 2),
        ("none", 1),
    ]


def test_references_usage_refuses_a_bare_column_name(tmp_path, capsys):
    _project(tmp_path, {"m0": EXCLUDE})

    rc, envelope = _references(
        ["--repo-root", str(tmp_path), "transform", "references", "status", "--usage"],
        capsys,
    )

    assert rc != 0
    assert envelope["status"] == "error"
    assert "model.column" in envelope["errors"][0]
