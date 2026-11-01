# ruff: noqa: S608 -- fixture SQL over test-owned relation names
"""Join coverage and declared parent populations against built data (#857).

Each case builds real DuckDB relations, writes the manifest dbt would have
written for the model reading them, and runs `maintain verify` end to end, so
the probe SQL, the policy read from the declaration and the envelope are all
exercised together. Every case asserts what was found *and* what the records
say, because an obligation that went unchecked must never read as clean.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from exmergo_dex_core.explore import relationships as rel_mod
from exmergo_dex_core.maintain import verify as verify_mod

from .test_verify import _write_artifacts

W = '"warehouse"."main"'


@pytest.fixture
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


def _node(name: str, sql: str, *, columns=None, dex=None) -> dict:
    node: dict = {
        "name": name,
        "resource_type": "model",
        "relation_name": f'{W}."{name}"',
        "compiled_code": sql,
        "columns": {
            column: {"name": column, "config": {"meta": {"dex": meta}}}
            for column, meta in (columns or {}).items()
        },
    }
    if dex is not None:
        node["config"] = {"meta": {"dex": dex}}
    return node


def _source_model(name: str) -> dict:
    """A project node standing for a staging model built as ``name``, so a
    ``references`` or ``population.parent`` can name it."""

    return {
        "name": name,
        "resource_type": "model",
        "relation_name": f'{W}."{name}"',
        "compiled_code": f'select * from {W}."raw_{name}"',
    }


def _build(repo, name: str, sql: str) -> None:
    repo.sql(f"CREATE OR REPLACE TABLE {name} AS {sql}")


def _subscriptions(repo, *, statuses: bool = False) -> None:
    if statuses:
        repo.sql(
            "CREATE TABLE subscriptions (subscription_id VARCHAR, "
            "created_at TIMESTAMP, status VARCHAR)",
            "INSERT INTO subscriptions VALUES "
            "('sub_elm', '2026-01-01', 'active'), "
            "('sub_oak', '2026-01-02', 'active'), "
            "('sub_pine', '2026-01-03', 'deleted')",
        )
        return
    repo.sql(
        "CREATE TABLE subscriptions (subscription_id VARCHAR, created_at TIMESTAMP)",
        "INSERT INTO subscriptions VALUES ('sub_elm', '2026-01-01'), "
        "('sub_oak', '2026-01-02'), ('sub_pine', '2026-01-03')",
    )


def _verify(repo, *args: str) -> dict:
    rc, payload = repo.dex("maintain", "verify", *args)
    assert rc == 0 and payload["status"] == "ok", payload
    return payload["data"]


def _codes(data: dict, model: str) -> dict[str, dict]:
    return {f["code"]: f for f in data["findings"] if f["identifier"] == model}


def _records(data: dict, model: str) -> list[dict]:
    return [r for r in data.get("join_coverage") or [] if r["model"] == model]


# --- the issue's reproduction ---------------------------------------------------

_WRONG = (
    "select s.subscription_id, s.created_at, a.activity_id "
    f"from {W}.activity a left join {W}.subscriptions s "
    "on a.subscription_id = s.subscription_id"
)


def test_disjoint_keys_are_a_zero_match_join_and_missing_parents(
    maintain_repo, _assume_the_project_compiles
):
    """The reproduction: a report driven from activity, left-joining
    subscriptions, whose contract is one row per subscription. Every activity
    key is unknown to subscriptions, so the join matches nothing, every
    subscription column is NULL, and all three subscriptions are missing."""

    _subscriptions(maintain_repo)
    maintain_repo.sql(
        "CREATE TABLE activity (activity_id INTEGER, subscription_id VARCHAR)",
        "INSERT INTO activity VALUES (1, 'x1'), (2, 'x2'), (3, 'x3'), (4, 'x4')",
    )
    _build(maintain_repo, "rpt_subscriptions", _WRONG)
    _write_artifacts(
        maintain_repo.project_dir,
        nodes={
            "model.maintain_test.subscriptions": _source_model("subscriptions"),
            "model.maintain_test.rpt_subscriptions": _node(
                "rpt_subscriptions",
                _WRONG,
                columns={
                    "subscription_id": {"role": "key"},
                    "created_at": {"role": "time", "null_rule": "never"},
                },
                dex={
                    "grain": ["subscription_id"],
                    "population": {
                        "rule": "Every subscription.",
                        "parent": {
                            "model": "subscriptions",
                            "key": {"subscription_id": "subscription_id"},
                        },
                    },
                },
            ),
        },
        results=[],
    )
    data = _verify(maintain_repo, "rpt_subscriptions")
    codes = _codes(data, "rpt_subscriptions")

    no_match = codes["join_no_match"]
    assert no_match["severity"] == "high"
    assert no_match["data"]["eligible_rows"] == 4
    assert no_match["data"]["matched_rows"] == 0
    assert no_match["data"]["eligible_keys"] == 4
    assert no_match["data"]["required_by"].startswith("created_at")

    all_null = codes["join_field_all_null"]
    assert all_null["column"] == "created_at"
    assert all_null["data"]["rows"] == 4

    missing = codes["parent_population_missing"]
    assert missing["severity"] == "high"
    assert missing["data"]["missing_parents"] == 3
    assert missing["data"]["eligible_parent_keys"] == 3
    assert missing["data"]["output_null_key_rows"] == 4
    assert missing["data"]["coverage"] == 0.0
    # The output carries no subscription key at all, so nothing is unexpected:
    # the unknown activity keys are never offered as the population.
    assert "parent_population_unexpected" not in codes

    (record,) = _records(data, "rpt_subscriptions")
    assert record["status"] == "contradicted"
    assert record["declared"]["required"] is True
    assert record["measured"]["unmatched_keys"] == 4
    assert record["derived_columns_all_null"] == ["created_at", "subscription_id"]
    (parent,) = data["parent_population"]
    assert parent["status"] == "contradicted"
    assert parent["declared"]["key"] == [
        {"column": "subscription_id", "parent_column": "subscription_id"}
    ]


def test_output_keys_drawn_from_the_children_are_unexpected_not_the_population(
    maintain_repo, _assume_the_project_compiles
):
    """The same report projecting the activity's own key: the four unknown
    keys are output entities the parent does not have, counted, never
    proposed as the population; the three subscriptions are still missing."""

    sql = (
        "select a.subscription_id, s.created_at "
        f"from {W}.activity a left join {W}.subscriptions s "
        "on a.subscription_id = s.subscription_id"
    )
    _subscriptions(maintain_repo)
    maintain_repo.sql(
        "CREATE TABLE activity (activity_id INTEGER, subscription_id VARCHAR)",
        "INSERT INTO activity VALUES (1, 'x1'), (2, 'x2'), (3, 'x3'), (4, 'x4')",
    )
    _build(maintain_repo, "rpt_subscriptions", sql)
    _write_artifacts(
        maintain_repo.project_dir,
        nodes={
            "model.maintain_test.subscriptions": _source_model("subscriptions"),
            "model.maintain_test.rpt_subscriptions": _node(
                "rpt_subscriptions",
                sql,
                dex={
                    "population": {
                        "parent": {
                            "model": "subscriptions",
                            "key": {"subscription_id": "subscription_id"},
                        }
                    }
                },
            ),
        },
        results=[],
    )
    data = _verify(maintain_repo, "rpt_subscriptions")
    codes = _codes(data, "rpt_subscriptions")
    unexpected = codes["parent_population_unexpected"]
    assert unexpected["data"]["unexpected_keys"] == 4
    assert "not a replacement population" in unexpected["detail"]
    assert codes["parent_population_missing"]["data"]["missing_parents"] == 3
    # No column the join supplies is declared required and the declaration
    # marks none optional, so zero matches is still reported, at high.
    assert codes["join_no_match"]["severity"] == "high"


def test_a_model_driven_from_the_parent_is_clean_and_says_so(
    maintain_repo, _assume_the_project_compiles
):
    """The fix: drive from subscriptions and left-join activity. A parent with
    no children survives, its unmatched rows are a LEFT join's optional
    relationship, and every subscription is present."""

    sql = (
        "select s.subscription_id, s.created_at, a.activity_id "
        f"from {W}.subscriptions s left join {W}.activity a "
        "on a.subscription_id = s.subscription_id"
    )
    _subscriptions(maintain_repo)
    maintain_repo.sql(
        "CREATE TABLE activity (activity_id INTEGER, subscription_id VARCHAR)",
        "INSERT INTO activity VALUES (1, 'sub_elm'), (2, 'sub_elm'), (3, 'sub_oak')",
    )
    _build(maintain_repo, "rpt_subscriptions", sql)
    _write_artifacts(
        maintain_repo.project_dir,
        nodes={
            "model.maintain_test.subscriptions": _source_model("subscriptions"),
            "model.maintain_test.rpt_subscriptions": _node(
                "rpt_subscriptions",
                sql,
                columns={"created_at": {"null_rule": "never"}},
                dex={
                    "population": {
                        "parent": {
                            "model": "subscriptions",
                            "key": {"subscription_id": "subscription_id"},
                        }
                    }
                },
            ),
        },
        results=[],
    )
    data = _verify(maintain_repo, "rpt_subscriptions")
    codes = _codes(data, "rpt_subscriptions")
    assert not [c for c in codes if c.startswith(("join_", "parent_"))], codes
    (record,) = _records(data, "rpt_subscriptions")
    assert record["status"] == "satisfied"
    assert record["optional_unmatched"] is True
    assert record["measured"]["eligible_rows"] == 3
    assert record["measured"]["matched_rows"] == 2
    assert record["coverage"] == {"rows": 0.6667, "keys": 0.6667}
    (parent,) = data["parent_population"]
    assert parent["status"] == "satisfied"
    assert parent["measured"]["missing_parents"] == 0
    assert parent["measured"]["coverage"] == 1.0


def test_duplicated_child_keys_report_rows_and_keys_apart(
    maintain_repo, _assume_the_project_compiles
):
    """Three rows of one known key and one row of an unknown key behind an
    inner join: three quarters of the rows match, half of the keys."""

    sql = (
        "select a.activity_id, s.created_at "
        f"from {W}.activity a inner join {W}.subscriptions s "
        "on a.subscription_id = s.subscription_id"
    )
    _subscriptions(maintain_repo)
    maintain_repo.sql(
        "CREATE TABLE activity (activity_id INTEGER, subscription_id VARCHAR)",
        "INSERT INTO activity VALUES (1, 'sub_elm'), (2, 'sub_elm'), "
        "(3, 'sub_elm'), (4, 'unknown')",
    )
    _build(maintain_repo, "fct_activity", sql)
    _write_artifacts(
        maintain_repo.project_dir,
        nodes={"model.maintain_test.fct_activity": _node("fct_activity", sql)},
        results=[],
    )
    data = _verify(maintain_repo, "fct_activity")
    orphans = _codes(data, "fct_activity")["join_orphans"]
    assert orphans["data"]["unmatched_rows"] == 1
    assert orphans["data"]["coverage_rows"] == 0.75
    assert orphans["data"]["coverage_keys"] == 0.5
    assert orphans["data"]["threshold_source"] == "default"
    (record,) = _records(data, "fct_activity")
    assert record["measured"]["eligible_keys"] == 2
    assert record["measured"]["matched_keys"] == 1


def test_a_required_relationship_is_judged_against_the_contract_not_a_default(
    maintain_repo, _assume_the_project_compiles
):
    """A foreign key declared with `references` makes every key's match an
    obligation: one unknown key in forty is below the default thresholds, and
    still contradicts the declaration."""

    _subscriptions(maintain_repo)
    maintain_repo.sql(
        "CREATE TABLE activity (activity_id INTEGER, subscription_id VARCHAR)",
        "INSERT INTO activity SELECT i, CASE WHEN i = 1 THEN 'unknown' "
        "ELSE 'sub_oak' END FROM range(1, 41) t(i)",
    )
    sql = (
        "select a.activity_id, a.subscription_id, s.created_at "
        f"from {W}.activity a left join {W}.subscriptions s "
        "on a.subscription_id = s.subscription_id"
    )
    _build(maintain_repo, "fct_activity", sql)
    _write_artifacts(
        maintain_repo.project_dir,
        nodes={
            "model.maintain_test.subscriptions": _source_model("subscriptions"),
            "model.maintain_test.fct_activity": _node(
                "fct_activity",
                sql,
                columns={
                    "subscription_id": {
                        "role": "foreign_key",
                        "references": "subscriptions.subscription_id",
                    }
                },
            ),
        },
        results=[],
    )
    data = _verify(maintain_repo, "fct_activity")
    orphans = _codes(data, "fct_activity")["join_orphans"]
    assert orphans["severity"] == "high"
    assert orphans["data"]["threshold_source"] == "contract"
    assert orphans["data"]["threshold"] == 0
    assert orphans["data"]["unmatched_rows"] == 1
    assert "foreign_key referencing subscriptions" in orphans["data"]["required_by"]


def test_an_intentional_parent_filter_excludes_parents_rather_than_missing_them(
    maintain_repo, _assume_the_project_compiles
):
    """The model drops deleted subscriptions and declares so: the deleted
    parent is counted as excluded by the declared filter, not as missing."""

    sql = (
        "select s.subscription_id, s.status "
        f"from {W}.subscriptions s where s.status <> 'deleted'"
    )
    _subscriptions(maintain_repo, statuses=True)
    _build(maintain_repo, "dim_subscriptions", sql)
    _write_artifacts(
        maintain_repo.project_dir,
        nodes={
            "model.maintain_test.subscriptions": _source_model("subscriptions"),
            "model.maintain_test.dim_subscriptions": _node(
                "dim_subscriptions",
                sql,
                dex={
                    "population": {
                        "filters": [{"column": "status", "excludes": ["deleted"]}],
                        "parent": {
                            "model": "subscriptions",
                            "key": {"subscription_id": "subscription_id"},
                        },
                    }
                },
            ),
        },
        results=[],
    )
    data = _verify(maintain_repo, "dim_subscriptions")
    codes = _codes(data, "dim_subscriptions")
    assert "parent_population_missing" not in codes, codes
    (parent,) = data["parent_population"]
    assert parent["status"] == "satisfied"
    assert parent["declared"]["filters_applied"] == ["status"]
    assert parent["measured"]["excluded_by_filters"] == 1
    assert parent["measured"]["eligible_parent_keys"] == 2


def test_an_undeclared_parent_filter_leaves_the_parents_missing(
    maintain_repo, _assume_the_project_compiles
):
    """The same model with no declared filter: the deleted subscription is a
    missing parent, since nothing says it is excluded."""

    sql = (
        f"select s.subscription_id from {W}.subscriptions s where s.status <> 'deleted'"
    )
    _subscriptions(maintain_repo, statuses=True)
    _build(maintain_repo, "dim_subscriptions", sql)
    _write_artifacts(
        maintain_repo.project_dir,
        nodes={
            "model.maintain_test.subscriptions": _source_model("subscriptions"),
            "model.maintain_test.dim_subscriptions": _node(
                "dim_subscriptions",
                sql,
                dex={
                    "population": {
                        "parent": {
                            "model": "subscriptions",
                            "key": {"subscription_id": "subscription_id"},
                        }
                    }
                },
            ),
        },
        results=[],
    )
    data = _verify(maintain_repo, "dim_subscriptions")
    missing = _codes(data, "dim_subscriptions")["parent_population_missing"]
    assert missing["data"]["missing_parents"] == 1
    assert missing["data"]["excluded_by_filters"] == 0


@pytest.mark.parametrize("never", [False, True])
def test_null_join_keys_follow_the_declared_policy(
    maintain_repo, _assume_the_project_compiles, never: bool
):
    """Two of four activity rows carry no key. By default they are excluded
    from the eligible population and reported apart; where the column carrying
    the key is declared `null_rule: never`, they count as unmatched."""

    sql = (
        "select a.activity_id, a.subscription_id, s.created_at "
        f"from {W}.activity a left join {W}.subscriptions s "
        "on a.subscription_id = s.subscription_id"
    )
    _subscriptions(maintain_repo)
    maintain_repo.sql(
        "CREATE TABLE activity (activity_id INTEGER, subscription_id VARCHAR)",
        "INSERT INTO activity VALUES (1, 'sub_elm'), (2, 'sub_oak'), "
        "(3, NULL), (4, NULL)",
    )
    _build(maintain_repo, "fct_activity", sql)
    columns = {"subscription_id": {"null_rule": "never"}} if never else {}
    _write_artifacts(
        maintain_repo.project_dir,
        nodes={
            "model.maintain_test.fct_activity": _node(
                "fct_activity", sql, columns=columns
            )
        },
        results=[],
    )
    data = _verify(maintain_repo, "fct_activity")
    (record,) = _records(data, "fct_activity")
    assert record["measured"]["null_key_rows"] == 2
    if never:
        assert record["declared"]["null_keys"] == "unmatched"
        assert record["measured"]["eligible_rows"] == 4
        assert record["coverage"]["rows"] == 0.5
    else:
        assert record["declared"]["null_keys"] == "excluded"
        assert record["measured"]["eligible_rows"] == 2
        assert record["coverage"]["rows"] == 1.0
        assert record["status"] == "satisfied"


def test_empty_inputs_have_no_coverage_rather_than_full_coverage(
    maintain_repo, _assume_the_project_compiles
):
    """Every input empty: the join and the parent population are both
    `not_applicable`, with coverage undefined, and nothing is reported."""

    maintain_repo.sql(
        "CREATE TABLE subscriptions (subscription_id VARCHAR, created_at TIMESTAMP)",
        "CREATE TABLE activity (activity_id INTEGER, subscription_id VARCHAR)",
    )
    _build(maintain_repo, "rpt_subscriptions", _WRONG)
    _write_artifacts(
        maintain_repo.project_dir,
        nodes={
            "model.maintain_test.subscriptions": _source_model("subscriptions"),
            "model.maintain_test.rpt_subscriptions": _node(
                "rpt_subscriptions",
                _WRONG,
                columns={"created_at": {"null_rule": "never"}},
                dex={
                    "population": {
                        "parent": {
                            "model": "subscriptions",
                            "key": {"subscription_id": "subscription_id"},
                        }
                    }
                },
            ),
        },
        results=[],
    )
    data = _verify(maintain_repo, "rpt_subscriptions")
    codes = _codes(data, "rpt_subscriptions")
    assert not [c for c in codes if c.startswith(("join_", "parent_"))], codes
    (record,) = _records(data, "rpt_subscriptions")
    assert record["status"] == "not_applicable"
    assert record["reason_code"] == "empty_input"
    assert record["coverage"] == {"rows": None, "keys": None}
    (parent,) = data["parent_population"]
    assert parent["status"] == "not_applicable"
    assert parent["measured"]["coverage"] is None


def test_unsupported_joins_are_unchecked_with_a_reason_each(
    maintain_repo, _assume_the_project_compiles
):
    """A range join, a column read through a UNION CTE (never mistaken for a
    warehouse table, #840) and SQL that does not parse each get their own
    `unchecked` record with a stable code, and no finding."""

    _subscriptions(maintain_repo)
    maintain_repo.sql(
        "CREATE TABLE activity (activity_id INTEGER, subscription_id VARCHAR, "
        "happened_at TIMESTAMP)",
        "INSERT INTO activity VALUES (1, 'sub_elm', '2026-02-01')",
    )
    ranged = (
        "select a.activity_id, s.subscription_id "
        f"from {W}.activity a join {W}.subscriptions s "
        "on a.happened_at >= s.created_at"
    )
    unioned = (
        "with combined as ("
        f"select subscription_id from {W}.activity union all "
        f"select subscription_id from {W}.subscriptions) "
        "select c.subscription_id, s.created_at from combined c "
        f"left join {W}.subscriptions s on c.subscription_id = s.subscription_id"
    )
    for name, sql in (("rpt_range", ranged), ("rpt_union", unioned)):
        _build(maintain_repo, name, sql)
    _build(maintain_repo, "rpt_macro", f"select * from {W}.activity")
    _write_artifacts(
        maintain_repo.project_dir,
        nodes={
            "model.maintain_test.rpt_range": _node("rpt_range", ranged),
            "model.maintain_test.rpt_union": _node("rpt_union", unioned),
            "model.maintain_test.rpt_macro": _node(
                "rpt_macro", "select {{ not compiled }} from (((("
            ),
        },
        results=[],
    )
    rc, payload = maintain_repo.dex("maintain", "verify")
    assert rc == 0 and payload["status"] == "ok", payload
    data = payload["data"]
    by_model = {
        r["model"]: r for r in data["join_coverage"] if r["model"].startswith("rpt_")
    }
    assert by_model["rpt_range"]["status"] == "unchecked"
    assert by_model["rpt_range"]["reason_code"] == "non_equality_join"
    assert by_model["rpt_union"]["reason_code"] == "set_operation_cte"
    assert by_model["rpt_macro"]["reason_code"] == "unparseable_sql"
    assert not [
        f
        for f in data["findings"]
        if f["identifier"] in by_model and f["axis"] == "join_contract"
    ]
    assert any("rpt_union" in w and "UNION" in w for w in payload["warnings"])


# --- the probe itself -------------------------------------------------------------


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
def test_the_coverage_probe_is_one_aggregate_select_on_every_dialect(dialect):
    from exmergo_dex_core.cache import Relationship, RelationshipKind
    from exmergo_dex_core.guards.sql_guard import assert_select_only

    relationships = [
        Relationship(
            from_dataset="db.s.orders",
            from_columns=["customer_id", "region"],
            to_dataset="db.s.customers",
            to_columns=["id", "region"],
            kind=RelationshipKind.DECLARED,
        ),
        Relationship(
            from_dataset="db.s.orders",
            from_columns=["product_id"],
            to_dataset="db.s.products",
            to_columns=["id"],
            kind=RelationshipKind.DECLARED,
        ),
    ]
    (statement,) = rel_mod.coverage_statements(relationships, dialect)
    assert assert_select_only(statement, dialect=dialect)


def test_the_coverage_probe_counts_rows_keys_and_null_keys():
    duckdb = pytest.importorskip("duckdb")
    from exmergo_dex_core.cache import Relationship, RelationshipKind

    conn = duckdb.connect()
    conn.execute("CREATE TABLE p (id VARCHAR)")
    conn.execute("INSERT INTO p VALUES ('a'), ('b'), (NULL)")
    conn.execute("CREATE TABLE c (pid VARCHAR)")
    conn.execute("INSERT INTO c VALUES ('a'), ('a'), ('z'), ('z'), ('y'), (NULL)")
    rel = Relationship(
        from_dataset="c",
        from_columns=["pid"],
        to_dataset="p",
        to_columns=["id"],
        kind=RelationshipKind.DECLARED,
    )
    row = conn.execute(rel_mod.coverage_probe_sql([rel])).fetchone()
    names = [d[0] for d in conn.description]
    got = dict(zip(names, row, strict=True))
    assert got == {
        "dex_cov_keys_0": 3,
        "dex_cov_matched_keys_0": 1,
        "dex_cov_rows_0": 6,
        "dex_cov_eligible_0": 5,
        "dex_cov_matched_0": 2,
    }


def test_compiled_sql_that_does_not_parse_is_a_typed_skip(tmp_path: Path):
    target = tmp_path / "target"
    target.mkdir()
    (target / "manifest.json").write_text(
        json.dumps(
            {
                "nodes": {
                    "model.p.m": {
                        "name": "m",
                        "resource_type": "model",
                        "compiled_code": "select {{ x }} from ((((",
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    plan = verify_mod.plan_join_contract(tmp_path, "duckdb")
    (skip,) = plan.skips["m"]
    assert skip.code == "unparseable_sql"


def test_a_build_scope_of_unique_ids_selects_the_same_models(tmp_path: Path):
    target = tmp_path / "target"
    target.mkdir()
    sql = "select a.x from a join b on a.id = b.id"
    (target / "manifest.json").write_text(
        json.dumps(
            {
                "nodes": {
                    "model.p.m": {
                        "name": "m",
                        "resource_type": "model",
                        "compiled_code": sql,
                    },
                    "model.p.n": {
                        "name": "n",
                        "resource_type": "model",
                        "compiled_code": sql,
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    plan = verify_mod.plan_join_contract(tmp_path, "duckdb", scope={"model.p.m"})
    assert list(plan.candidates) == ["m"]


def test_a_column_newly_null_against_the_cached_profile_is_reported():
    """No declared rule, but the last profile saw the column filled: a join
    that stopped matching is reported as newly NULL, at medium."""

    candidate = verify_mod.JoinCandidate(
        from_relation="db.main.activity",
        from_columns=("subscription_id",),
        to_relation="db.main.subscriptions",
        to_columns=("subscription_id",),
        side="left",
        label="left join subscriptions",
    )
    ctx = verify_mod.ModelJoinContext(
        relation="db.main.rpt",
        lineage={"created_at": ("db.main.subscriptions", "created_at")},
    )
    policy = verify_mod.join_policy(candidate, ctx)
    assert policy.derived == ("created_at",)
    record: dict = {}
    result = verify_mod.JoinContractResult()
    verify_mod._judge_derived_nulls(
        "rpt",
        candidate,
        policy,
        ctx,
        ("db.main.rpt", 5, {"created_at": 0}),
        record,
        result,
        {"db.main.rpt": ("2026-10-01T00:00:00Z", {"created_at": 0.1})},
    )
    (finding,) = result.findings
    assert finding.code == "join_field_newly_null"
    assert finding.severity == "medium"
    assert finding.data["baseline_null_fraction"] == 0.1
    assert record["null_baseline"] == "cache"

    record, result = {}, verify_mod.JoinContractResult()
    verify_mod._judge_derived_nulls(
        "rpt",
        candidate,
        policy,
        ctx,
        ("db.main.rpt", 5, {"created_at": 0}),
        record,
        result,
        None,
    )
    assert result.findings == []
    assert record == {
        "derived_columns_all_null": ["created_at"],
        "null_baseline": "none",
    }
