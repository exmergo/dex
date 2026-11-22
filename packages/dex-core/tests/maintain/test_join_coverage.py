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


# --- #1459: a `references` is the claim, on a LEFT join and whatever the role ---


def _join_findings(data: dict, model: str) -> list[str]:
    return [
        f["code"]
        for f in data["findings"]
        if f["identifier"] == model and f["axis"] == "join_contract"
    ]


_LEFT_ACTIVITY = (
    "select a.activity_id, a.subscription_id, s.created_at "
    f"from {W}.activity a left join {W}.subscriptions s "
    "on a.subscription_id = s.subscription_id"
)


def _left_activity(repo, orphan: str | None, columns: dict) -> dict:
    """Forty activity rows LEFT-joining subscriptions, the first keyed
    ``orphan`` (a value no subscription has, or NULL), with ``columns``
    declared on the model."""

    _subscriptions(repo)
    first = "NULL" if orphan is None else f"'{orphan}'"
    repo.sql(
        "CREATE TABLE activity (activity_id INTEGER, subscription_id VARCHAR)",
        f"INSERT INTO activity SELECT i, CASE WHEN i = 1 THEN {first} "
        "ELSE 'sub_oak' END FROM range(1, 41) t(i)",
    )
    _build(repo, "fct_activity", _LEFT_ACTIVITY)
    _write_artifacts(
        repo.project_dir,
        nodes={
            "model.maintain_test.subscriptions": _source_model("subscriptions"),
            "model.maintain_test.fct_activity": _node(
                "fct_activity", _LEFT_ACTIVITY, columns=columns
            ),
        },
        results=[],
    )
    return _verify(repo, "fct_activity")


@pytest.mark.parametrize(
    "declared",
    [
        pytest.param({}, id="no-role"),
        pytest.param({"role": "key"}, id="key"),
        pytest.param({"role": "dimension"}, id="dimension"),
    ],
)
def test_a_references_with_no_foreign_key_role_requires_the_left_join(
    maintain_repo, _assume_the_project_compiles, declared: dict
):
    """The issue: a `references` renders a `relationships` test whatever the
    column's role, and that test fails on one non-null orphan. `maintain
    verify` now reads the same claim: the LEFT join is required, and the one
    orphan in forty contradicts it at the contract threshold, naming the
    declaration it rests on."""

    data = _left_activity(
        maintain_repo,
        "unknown",
        {
            "subscription_id": {
                **declared,
                "references": "subscriptions.subscription_id",
            }
        },
    )
    orphans = _codes(data, "fct_activity")["join_orphans"]
    assert orphans["severity"] == "high"
    assert orphans["data"]["threshold_source"] == "contract"
    assert orphans["data"]["threshold"] == 0
    assert orphans["data"]["unmatched_rows"] == 1
    assert orphans["data"]["required_by"] == (
        "subscription_id declares references subscriptions.subscription_id"
    )
    (record,) = _records(data, "fct_activity")
    assert record["status"] == "contradicted"
    assert record["declared"]["required"] is True
    assert "optional_unmatched" not in record


def test_a_missing_reference_written_as_null_satisfies_the_left_join(
    maintain_repo, _assume_the_project_compiles
):
    """The same model with the orphan's key written as NULL: a NULL is a
    missing reference, which `relationships` skips and the coverage probe
    counts apart, so the required join is satisfied."""

    data = _left_activity(
        maintain_repo,
        None,
        {"subscription_id": {"references": "subscriptions.subscription_id"}},
    )
    assert not _join_findings(data, "fct_activity")
    (record,) = _records(data, "fct_activity")
    assert record["status"] == "satisfied"
    assert record["declared"]["required"] is True
    assert record["measured"]["null_key_rows"] == 1
    assert record["measured"]["eligible_rows"] == 39
    assert record["measured"]["unmatched_rows"] == 0


@pytest.mark.parametrize(
    "columns",
    [
        pytest.param({}, id="nothing-declared"),
        pytest.param({"subscription_id": {"role": "foreign_key"}}, id="no-references"),
        pytest.param({"subscription_id": {"role": "dimension"}}, id="dimension"),
    ],
)
def test_a_left_join_whose_key_declares_no_references_is_unchanged(
    maintain_repo, _assume_the_project_compiles, columns: dict
):
    """With no `references` on the key, the LEFT join keeps today's reading:
    its unmatched rows are the optional relationship it writes."""

    data = _left_activity(maintain_repo, "unknown", columns)
    assert not _join_findings(data, "fct_activity")
    (record,) = _records(data, "fct_activity")
    assert record["status"] == "satisfied"
    assert record["declared"]["required"] is False
    assert record["optional_unmatched"] is True
    assert record["threshold_source"] == "default"


def test_a_references_to_another_relation_does_not_require_this_join(
    maintain_repo, _assume_the_project_compiles
):
    """A `references` names one parent: a join into another relation is not
    the one it holds to the parent, so it keeps the LEFT join's reading."""

    data = _left_activity(
        maintain_repo,
        "unknown",
        {"subscription_id": {"references": "dim_subscriptions.subscription_id"}},
    )
    (record,) = _records(data, "fct_activity")
    assert record["declared"]["required"] is False
    assert record["status"] == "satisfied"


def _versioned(version: int, latest: int | None = 2) -> dict:
    node = {
        "name": "subscriptions",
        "resource_type": "model",
        "version": version,
        "relation_name": f'{W}."subscriptions_v{version}"',
        "compiled_code": f'select * from {W}."raw_subscriptions"',
    }
    if latest is not None:
        node["latest_version"] = latest
    return node


@pytest.mark.parametrize(
    ("joined", "reference", "latest", "required"),
    [
        pytest.param(1, "subscriptions.v1.subscription_id", 2, True, id="label"),
        pytest.param(2, "subscriptions.v1.subscription_id", 2, False, id="other"),
        pytest.param(2, "subscriptions.subscription_id", 2, True, id="bare-latest"),
        pytest.param(1, "subscriptions.subscription_id", 2, False, id="bare-old"),
        pytest.param(1, "subscriptions.subscription_id", 1, True, id="latest-1"),
        pytest.param(2, "subscriptions.subscription_id", None, True, id="no-latest"),
    ],
)
def test_a_versioned_reference_names_the_version_ref_resolves_to(
    maintain_repo, _assume_the_project_compiles, joined, reference, latest, required
):
    """`subscriptions.v1.subscription_id` renders `ref('subscriptions', v=1)`,
    and a bare `subscriptions.subscription_id` the version an unversioned
    `ref()` resolves to (its `latest_version`, else the highest): the join is
    required only where it reads that version's relation."""

    for v in (1, 2):
        maintain_repo.sql(
            f"CREATE TABLE subscriptions_v{v} (subscription_id VARCHAR)",
            f"INSERT INTO subscriptions_v{v} VALUES ('sub_elm'), ('sub_oak')",
        )
    maintain_repo.sql(
        "CREATE TABLE activity (activity_id INTEGER, subscription_id VARCHAR)",
        "INSERT INTO activity SELECT i, CASE WHEN i = 1 THEN 'unknown' "
        "ELSE 'sub_oak' END FROM range(1, 41) t(i)",
    )
    sql = (
        "select a.activity_id, a.subscription_id "
        f"from {W}.activity a left join {W}.subscriptions_v{joined} s "
        "on a.subscription_id = s.subscription_id"
    )
    _build(maintain_repo, "fct_activity", sql)
    _write_artifacts(
        maintain_repo.project_dir,
        nodes={
            "model.maintain_test.subscriptions.v1": _versioned(1, latest),
            "model.maintain_test.subscriptions.v2": _versioned(2, latest),
            "model.maintain_test.fct_activity": _node(
                "fct_activity",
                sql,
                columns={"subscription_id": {"references": reference}},
            ),
        },
        results=[],
    )
    data = _verify(maintain_repo, "fct_activity")
    (record,) = _records(data, "fct_activity")
    assert record["declared"]["required"] is required
    assert ("join_orphans" in _codes(data, "fct_activity")) is required


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


# --- a key compared through a conversion (#861) -----------------------------------


def _products(repo) -> None:
    repo.sql(
        "CREATE TABLE raw_products (product_id VARCHAR, product_name VARCHAR)",
        "INSERT INTO raw_products VALUES ('7', 'anvil'), ('007', 'anvil, boxed'), "
        "('12', 'rope'), ('org_cedar', 'plank'), (NULL, 'unlabelled')",
    )
    repo.sql(
        "CREATE TABLE product_prices (product_id INTEGER, price DOUBLE)",
        "INSERT INTO product_prices VALUES (7, 10.0), (12, 3.5), (99, 1.0)",
    )


_CAST_JOIN = (
    "select p.product_id, p.product_name, l.price "
    f"from {W}.raw_products p left join {W}.product_prices l "
    "on try_cast(p.product_id as integer) = l.product_id"
)


def test_a_join_through_a_safe_cast_is_measured_with_its_failures_apart(
    maintain_repo, _assume_the_project_compiles
):
    """`try_cast(p.product_id as integer) = l.product_id` used to be unchecked
    as a non-equality join. It is a key equality through a conversion: the
    probe applies the same cast, and the identifier that does not convert is
    eligible, unmatched, and counted apart from a NULL key."""

    _products(maintain_repo)
    _build(maintain_repo, "dim_products", _CAST_JOIN)
    _write_artifacts(
        maintain_repo.project_dir,
        nodes={"model.maintain_test.dim_products": _node("dim_products", _CAST_JOIN)},
        results=[],
    )
    data = _verify(maintain_repo, "dim_products")
    (record,) = _records(data, "dim_products")
    assert record["status"] != "unchecked", record
    assert record["key_conversion"] == {
        "from": ["TRY_CAST(product_id AS INT)"],
        "to": ["product_id"],
    }
    measured = record["measured"]
    assert measured["rows"] == 5
    assert measured["null_key_rows"] == 1
    assert measured["eligible_rows"] == 4
    assert measured["matched_rows"] == 3  # 7, 007 and 12
    assert measured["conversion_failed_rows"] == 1  # org_cedar
    assert measured["conversion_failed_keys"] == 1
    assert measured["eligible_keys"] == 4
    assert measured["matched_keys"] == 3


def test_a_hard_cast_is_measured_as_its_safe_form_instead_of_failing(
    maintain_repo, _assume_the_project_compiles
):
    """A hard `cast` that the data cannot satisfy would fail the probe outright;
    measured as `try_cast`, its failures are counts rather than an error."""

    _products(maintain_repo)
    sql = _CAST_JOIN.replace("try_cast(", "cast(")
    _build(
        maintain_repo,
        "dim_products",
        _CAST_JOIN,  # the built relation; the hard cast would not build
    )
    _write_artifacts(
        maintain_repo.project_dir,
        nodes={"model.maintain_test.dim_products": _node("dim_products", sql)},
        results=[],
    )
    data = _verify(maintain_repo, "dim_products")
    (record,) = _records(data, "dim_products")
    assert record["measured"]["conversion_failed_rows"] == 1
    assert record["key_conversion"]["from"] == ["TRY_CAST(product_id AS INT)"]


def test_a_key_rewritten_by_an_unmeasured_expression_says_so(
    maintain_repo, _assume_the_project_compiles
):
    _products(maintain_repo)
    sql = (
        "select p.product_id, l.price "
        f"from {W}.raw_products p left join {W}.product_prices l "
        "on cast(regexp_replace(p.product_id, '^0+', '') as integer) = l.product_id"
    )
    _build(maintain_repo, "dim_products", _CAST_JOIN)
    _write_artifacts(
        maintain_repo.project_dir,
        nodes={"model.maintain_test.dim_products": _node("dim_products", sql)},
        results=[],
    )
    data = _verify(maintain_repo, "dim_products")
    (record,) = _records(data, "dim_products")
    assert record["status"] == "unchecked"
    assert record["reason_code"] == "unsupported_key_expression"


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
def test_a_converted_key_probe_is_one_aggregate_select_on_every_dialect(dialect):
    from exmergo_dex_core.cache import Relationship, RelationshipKind
    from exmergo_dex_core.guards.sql_guard import assert_select_only

    rel = Relationship(
        from_dataset="db.s.products",
        from_columns=["product_id"],
        to_dataset="db.s.prices",
        to_columns=["product_id"],
        kind=RelationshipKind.DECLARED,
    )
    exprs = {id(rel): (("TRY_CAST(LOWER({}) AS INT)",), (None,))}
    if dialect == "postgres":
        # No cast there returns NULL instead of failing: a cast to a type the
        # guard does not cover is refused, not run (#1075).
        from exmergo_dex_core.sql_shape import NoSafeCastError

        dated = {id(rel): (("TRY_CAST({} AS DATE)",), (None,))}
        with pytest.raises(NoSafeCastError):
            rel_mod.coverage_statements([rel], dialect, key_exprs=dated)
    (statement,) = rel_mod.coverage_statements([rel], dialect, key_exprs=exprs)
    assert assert_select_only(statement, dialect=dialect)
    assert "dex_cov_failed_0" in statement.lower()
    # The safe form survives the dialect: sqlglot renders a TryCast it did not
    # parse as a failing CAST on Snowflake and ClickHouse, and Postgres has no
    # safe cast, so an integer cast is guarded by its shape and range there.
    safe = {
        "bigquery": "SAFE_CAST(",
        "clickhouse": "accurateCastOrNull(",
        "postgres": "BETWEEN -2147483648 AND 2147483647",
    }
    assert safe.get(dialect, "TRY_CAST(") in statement
    if dialect == "snowflake":
        assert "TRY_CAST(CAST(" in statement


def test_postgres_holds_back_only_the_casts_it_cannot_guard():
    """On Postgres an integer key cast is measured through its guard, and a
    cast to any other type is reported unchecked rather than run (#1075)."""

    from types import SimpleNamespace

    def pair(template):
        return ("m", SimpleNamespace(key_exprs=lambda: ((template,), (None,))), None)

    integer = pair("TRY_CAST(LOWER({}) AS BIGINT)")
    dated = pair("TRY_CAST({} AS DATE)")
    plain = pair(None)
    keep, unsafe = verify_mod._split_unsafe([integer, dated, plain], "postgres")
    assert keep == [integer, plain]
    assert unsafe == [dated]
    assert verify_mod._split_unsafe([dated], "duckdb") == ([dated], [])


# --- a probe that fails on a value says so without the value (#1070) -------------

_SENTINEL = "zq_sentinel_7"
_MASKED = "Could not convert string <value withheld> to INT32"


def _mixed_keys(repo) -> None:
    repo.sql(
        "CREATE TABLE raw_events (event_id INTEGER, account_ref VARCHAR)",
        f"INSERT INTO raw_events VALUES (1, '7'), (2, '12'), (3, '{_SENTINEL}')",
    )
    repo.sql(
        "CREATE TABLE raw_accounts (account_id INTEGER, region VARCHAR)",
        "INSERT INTO raw_accounts VALUES (7, 'north'), (12, 'south')",
    )


def test_a_plain_join_that_casts_implicitly_does_not_quote_the_key(
    maintain_repo, _assume_the_project_compiles
):
    """No cast is written: DuckDB compares the VARCHAR key to the INTEGER key by
    casting it, and the probe fails on the key that does not convert. The
    record keeps the warehouse's words and loses the key."""

    _mixed_keys(maintain_repo)
    _build(
        maintain_repo,
        "fct_events",
        f"select event_id, account_ref from {W}.raw_events",
    )
    sql = (
        "select e.event_id, e.account_ref, a.region "
        f"from {W}.raw_events e left join {W}.raw_accounts a "
        "on e.account_ref = a.account_id"
    )
    _write_artifacts(
        maintain_repo.project_dir,
        nodes={"model.maintain_test.fct_events": _node("fct_events", sql)},
        results=[],
    )
    rc, payload = maintain_repo.dex("maintain", "verify", "fct_events")
    assert rc == 0, payload
    text = json.dumps(payload)
    assert _SENTINEL not in text
    (record,) = _records(payload["data"], "fct_events")
    assert record["status"] == "unchecked"
    assert _MASKED in record["reason"]


def test_every_declared_check_over_a_failing_view_names_the_failure_only(
    maintain_repo, _assume_the_project_compiles
):
    """A view whose SQL casts the sentinel fails every statement that reads it:
    the row count, the declared grain, the declared population and parent. Each
    check goes unmeasured with the warehouse's words, never the value."""

    _mixed_keys(maintain_repo)
    maintain_repo.sql(
        "CREATE VIEW fct_accounts AS SELECT CAST(account_ref AS INTEGER) "
        f"AS account_id, 'north' AS region FROM {W}.raw_events"
    )
    _build(maintain_repo, "accounts", f"select * from {W}.raw_accounts")
    nodes = {
        "model.maintain_test.accounts": _node(
            "accounts", f"select * from {W}.raw_accounts"
        ),
        "model.maintain_test.fct_accounts": _node(
            "fct_accounts",
            f"select cast(account_ref as integer) as account_id from {W}.raw_events",
            columns={"account_id": {"role": "key"}},
            dex={
                "grain": ["account_id"],
                "population": {
                    "filters": [{"column": "region", "excludes": ["south"]}],
                    "parent": {
                        "model": "accounts",
                        "key": {"account_id": "account_id"},
                    },
                },
            },
        ),
    }
    _write_artifacts(maintain_repo.project_dir, nodes=nodes, results=[])
    rc, payload = maintain_repo.dex("maintain", "verify", "fct_accounts")
    assert rc == 0, payload
    assert _SENTINEL not in json.dumps(payload)
    said = json.dumps(
        {
            "warnings": payload["warnings"],
            "suppressed": payload["data"].get("suppressed"),
            "parent_population": payload["data"].get("parent_population"),
        }
    )
    assert _MASKED in said


# --- an INNER join used as a filter (#1162) ------------------------------------

_WITH_HANDLE = (
    "select m.member_id, h.handle "
    f"from {W}.members m inner join {W}.handles h on m.member_id = h.member_id"
)


def _members_and_handles(repo) -> None:
    """The issue's session: 33 members, 13 of whom have no handle."""

    repo.sql(
        "CREATE TABLE members (member_id INTEGER, name VARCHAR)",
        "INSERT INTO members SELECT i, 'm' || i FROM range(1, 34) t(i)",
        "CREATE TABLE handles (member_id INTEGER, handle VARCHAR)",
        "INSERT INTO handles SELECT i, 'h' || i FROM range(1, 21) t(i)",
    )


def _handles_mart(repo, *, dex=None, columns=None, sql: str = _WITH_HANDLE) -> dict:
    _members_and_handles(repo)
    _build(repo, "mrt_members_with_handle", sql)
    _write_artifacts(
        repo.project_dir,
        nodes={
            "model.maintain_test.handles": _source_model("handles"),
            "model.maintain_test.mrt_members_with_handle": _node(
                "mrt_members_with_handle", sql, columns=columns, dex=dex
            ),
        },
        results=[],
    )
    return _verify(repo, "mrt_members_with_handle")


_HANDLE_ASSUMPTION = {
    "decision": "Which members count",
    "chosen": "only members with a handle; the inner join drops the rest",
    "evidence": "request",
    "column": "member_id",
}


def test_an_inner_join_the_declaration_names_as_its_filter_is_satisfied(
    maintain_repo, _assume_the_project_compiles
):
    """An assumption whose `column` names the driving key says the join is the
    model's filter: the record is satisfied with its counts kept, names the
    assumption, and no join finding is raised."""

    data = _handles_mart(maintain_repo, dex={"assumptions": [_HANDLE_ASSUMPTION]})
    codes = _codes(data, "mrt_members_with_handle")
    assert not [c for c in codes if c.startswith("join_")], codes
    (record,) = _records(data, "mrt_members_with_handle")
    assert record["status"] == "satisfied"
    assert "finding" not in record
    assert record["measured"]["eligible_rows"] == 33
    assert record["measured"]["unmatched_rows"] == 13
    assert record["coverage"]["rows"] == 0.6061
    assert record["threshold_source"] == "default"
    assert record["unmatched_accounted_for"] == {
        "by": "assumption",
        "column": "member_id",
        "decision": "Which members count",
        "chosen": "only members with a handle; the inner join drops the rest",
        "evidence": "request",
        "withheld_finding": "join_orphans",
    }


def test_a_population_filter_on_the_driving_key_accounts_for_the_join(
    maintain_repo, _assume_the_project_compiles
):
    data = _handles_mart(
        maintain_repo,
        dex={
            "population": {
                "rule": "Members with a handle.",
                "filters": [{"column": "MEMBER_ID"}],
            }
        },
    )
    assert "join_orphans" not in _codes(data, "mrt_members_with_handle")
    (record,) = _records(data, "mrt_members_with_handle")
    assert record["status"] == "satisfied"
    assert record["measured"]["unmatched_rows"] == 13
    assert record["unmatched_accounted_for"] == {
        "by": "population_filter",
        "column": "member_id",
        "withheld_finding": "join_orphans",
    }


def test_an_inner_join_the_declaration_says_nothing_about_stays_contradicted(
    maintain_repo, _assume_the_project_compiles
):
    """No declaration, or an assumption on another column, leaves the default
    reading: the join is `join_orphans` and the record is contradicted."""

    for dex in (
        None,
        {"assumptions": [{**_HANDLE_ASSUMPTION, "column": "handle"}]},
        {"population": {"filters": [{"column": "handle", "excludes": ["x"]}]}},
    ):
        data = _handles_mart(maintain_repo, dex=dex)
        orphans = _codes(data, "mrt_members_with_handle")["join_orphans"]
        assert orphans["data"]["unmatched_rows"] == 13
        (record,) = _records(data, "mrt_members_with_handle")
        assert record["status"] == "contradicted"
        assert record["finding"] == "join_orphans"
        assert "unmatched_accounted_for" not in record
        maintain_repo.sql("DROP TABLE members", "DROP TABLE handles")


def test_a_left_join_ignores_an_assumption_on_its_key(
    maintain_repo, _assume_the_project_compiles
):
    """LEFT-join handling is unchanged: unmatched rows are the optional
    relationship it writes, and an assumption naming the key does not turn a
    zero-overlap finding into a satisfied record."""

    sql = (
        "select m.member_id, h.handle "
        f"from {W}.members m left join {W}.handles h on m.member_id = h.member_id"
    )
    _members_and_handles(maintain_repo)
    maintain_repo.sql("DELETE FROM handles WHERE member_id > 2")
    _build(maintain_repo, "mrt_members", sql)
    _write_artifacts(
        maintain_repo.project_dir,
        nodes={
            "model.maintain_test.mrt_members": _node(
                "mrt_members", sql, dex={"assumptions": [_HANDLE_ASSUMPTION]}
            )
        },
        results=[],
    )
    data = _verify(maintain_repo, "mrt_members")
    overlap = _codes(data, "mrt_members")["join_zero_overlap"]
    assert overlap["severity"] == "medium"
    (record,) = _records(data, "mrt_members")
    assert record["status"] == "contradicted"
    assert "unmatched_accounted_for" not in record


def test_a_required_inner_join_is_judged_against_the_contract_regardless(
    maintain_repo, _assume_the_project_compiles
):
    """A driving key declared a `foreign_key` referencing the joined model
    requires every key to match (`threshold_source: contract`), and an
    assumption on the same column does not loosen that."""

    data = _handles_mart(
        maintain_repo,
        dex={"assumptions": [_HANDLE_ASSUMPTION]},
        columns={
            "member_id": {"role": "foreign_key", "references": "handles.member_id"}
        },
    )
    orphans = _codes(data, "mrt_members_with_handle")["join_orphans"]
    assert orphans["severity"] == "high"
    assert orphans["data"]["threshold_source"] == "contract"
    (record,) = _records(data, "mrt_members_with_handle")
    assert record["status"] == "contradicted"
    assert "unmatched_accounted_for" not in record


def test_an_inner_join_matching_nothing_is_still_no_match(
    maintain_repo, _assume_the_project_compiles
):
    """A filter that keeps no row is not what the declaration describes: the
    keys share no value, so `join_no_match` stands."""

    _members_and_handles(maintain_repo)
    maintain_repo.sql("UPDATE handles SET member_id = member_id + 100")
    sql = _WITH_HANDLE
    _build(maintain_repo, "mrt_members_with_handle", sql)
    _write_artifacts(
        maintain_repo.project_dir,
        nodes={
            "model.maintain_test.mrt_members_with_handle": _node(
                "mrt_members_with_handle",
                sql,
                dex={"assumptions": [_HANDLE_ASSUMPTION]},
            )
        },
        results=[],
    )
    data = _verify(maintain_repo, "mrt_members_with_handle")
    assert "join_no_match" in _codes(data, "mrt_members_with_handle")
    (record,) = _records(data, "mrt_members_with_handle")
    assert record["status"] == "contradicted"
    assert "unmatched_accounted_for" not in record


def test_the_build_completion_reads_the_accounted_join_as_asserted(
    maintain_repo, _assume_the_project_compiles
):
    """The issue's acceptance, end to end into `data.completion`: with the
    assumption the mart is asserted and absent from `failed_checks`; without
    it, `failed_checks` names the check kind, the finding and the join."""

    from exmergo_dex_core.transform.completion import summarize_completion
    from exmergo_dex_core.transform.evidence import (
        BuildEvidence,
        BuildNode,
        BuildSelection,
    )

    mart = "mrt_members_with_handle"
    evidence = BuildEvidence(
        outcome="validated",
        nodes=[BuildNode(unique_id=f"model.t.{mart}", name=mart, status="success")],
        selection=BuildSelection(matched=[mart]),
    )

    def completion(data: dict):
        verification = {
            "ran": True,
            "findings": data["findings"],
            "join_coverage": data["join_coverage"],
        }
        return summarize_completion(evidence, verification).assurance

    declared = completion(
        _handles_mart(maintain_repo, dex={"assumptions": [_HANDLE_ASSUMPTION]})
    )
    assert declared.asserted.names == [mart]
    assert declared.failed_checks.count == 0
    assert declared.failed_checks.checks == []

    maintain_repo.sql("DROP TABLE members", "DROP TABLE handles")
    undeclared = completion(_handles_mart(maintain_repo))
    assert undeclared.failed_checks.names == [mart]
    (check,) = undeclared.failed_checks.checks
    assert check.kind == "join_coverage"
    assert check.finding == "join_orphans"
    assert check.join == (f"inner join {W}.handles AS h on m.member_id = h.member_id")


# --- key columns read from SQL, spelled the way the relation stores them (#1366) --


class _FoldingWarehouse:
    """A warehouse that folds an unquoted name (upper on Snowflake, lower on
    Postgres and Redshift) and resolves a quoted one exactly, the way those
    connectors do, so a probe quoting a name no relation holds is refused with
    Snowflake's 000904. The rows live in DuckDB, which matches names regardless
    of case, so each stored name is renamed to a physical one there and the
    probe runs once every name has resolved."""

    def __init__(self, dialect: str, tables: dict[str, dict[str, list]]):
        duckdb = pytest.importorskip("duckdb")
        self.dialect = dialect
        self.tables = tables
        self.metadata_reads: list[str] = []
        self.physical: dict[str, str] = {}
        self.conn = duckdb.connect()
        self.conn.execute("ATTACH ':memory:' AS db")
        self.conn.execute("CREATE SCHEMA db.s")
        for identifier, columns in tables.items():
            names = list(columns)
            for name in names:
                self.physical.setdefault(name, f"col_{len(self.physical)}")
            ddl = ", ".join(f"{self.physical[n]} VARCHAR" for n in names)
            self.conn.execute(f"CREATE TABLE {identifier} ({ddl})")
            for row in zip(*columns.values(), strict=True):
                marks = ", ".join("?" for _ in names)
                self.conn.execute(f"INSERT INTO {identifier} VALUES ({marks})", row)

    def _fold(self, name: str) -> str:
        return name.upper() if self.dialect == "snowflake" else name.lower()

    def table_metadata(self, identifier: str):
        from exmergo_dex_core.adapters.base import ColumnMeta, ObjectMeta

        self.metadata_reads.append(identifier)
        columns = self.tables[identifier]
        return (
            ObjectMeta(identifier, "table", "s", identifier, None, None, len(columns)),
            [
                ColumnMeta(name, "VARCHAR", True, ordinal)
                for ordinal, name in enumerate(columns, start=1)
            ],
        )

    def run_query(self, sql: str, *, max_rows: int, timeout_seconds: float):
        import sqlglot
        from sqlglot import exp

        from exmergo_dex_core.adapters.base import QueryResult
        from exmergo_dex_core.errors import WarehouseQueryError

        tree = sqlglot.parse_one(sql, read=self.dialect)
        aliases = {self._fold(a.alias) for a in tree.find_all(exp.Alias)}
        for column in list(tree.find_all(exp.Column)):
            ident = column.this
            name = ident.name if ident.quoted else self._fold(ident.name)
            if name in self.physical:
                column.set("this", exp.to_identifier(self.physical[name]))
            elif name not in aliases:
                raise WarehouseQueryError(
                    f"000904 (42000): SQL compilation error: invalid identifier "
                    f"'\"{ident.name}\"'"
                )
        cursor = self.conn.execute(tree.sql(dialect="duckdb"))
        names = [d[0] for d in cursor.description]
        row = list(cursor.fetchone())
        return QueryResult(names, ["BIGINT"] * len(names), [row], truncated=False)


def _coverage(adapter: _FoldingWarehouse, from_col: str, to_col: str):
    candidate = verify_mod.JoinCandidate(
        from_relation="db.s.orders",
        from_columns=(from_col,),
        to_relation="db.s.customers",
        to_columns=(to_col,),
        side="left",
        label="left join customers",
    )
    return verify_mod.join_coverage(
        adapter,
        verify_mod.JoinContractPlan(candidates={"m": [candidate]}),
        {},
        list(adapter.tables),
        handshake=lambda _estimate, _count: None,
    )


@pytest.mark.parametrize(
    ("dialect", "written"),
    [
        ("snowflake", "customer_id"),
        ("snowflake", "CUSTOMER_ID"),
        ("snowflake", "Customer_Id"),
        ("postgres", "CUSTOMER_ID"),
        ("postgres", "customer_id"),
        ("redshift", "CUSTOMER_ID"),
        ("redshift", "customer_id"),
    ],
)
def test_a_key_written_in_either_case_is_probed_as_the_relation_stores_it(
    dialect, written
):
    # An unquoted key the warehouse folded (upper on Snowflake, lower on
    # Postgres and Redshift) is probed under the folded name, so a join that
    # matches nothing reports join_no_match rather than probe_refused (#1366).
    stored = written.upper() if dialect == "snowflake" else written.lower()
    adapter = _FoldingWarehouse(
        dialect,
        {
            "db.s.orders": {stored: ["a", "b"]},
            "db.s.customers": {stored: ["x", "y"]},
        },
    )
    result = _coverage(adapter, written, written)
    (record,) = result.records
    assert record["status"] == "contradicted", record
    assert [f.code for f in result.findings] == ["join_no_match"]
    # A name already spelled the way the connector folds it reads no metadata.
    assert bool(adapter.metadata_reads) == (written != stored)


def test_a_quoted_mixed_case_key_keeps_its_exact_spelling():
    # A column the SQL quoted keeps its case: the relation holds "CustId" and,
    # beside it, the CUSTID an unquoted custid would fold to; the probe reads
    # the one the SQL named (#1366).
    adapter = _FoldingWarehouse(
        "snowflake",
        {
            "db.s.orders": {"CustId": ["a", "b"], "CUSTID": ["x", "y"]},
            "db.s.customers": {"CustId": ["a", "b"]},
        },
    )
    result = _coverage(adapter, "CustId", "CustId")
    (record,) = result.records
    assert record["status"] == "satisfied", record
    assert result.findings == []


def test_a_key_the_relation_does_not_hold_is_still_refused_with_the_reason():
    adapter = _FoldingWarehouse(
        "snowflake",
        {
            "db.s.orders": {"CUSTOMER_ID": ["a"]},
            "db.s.customers": {"CUSTOMER_ID": ["a"]},
        },
    )
    result = _coverage(adapter, "client_id", "customer_id")
    (record,) = result.records
    assert record["status"] == "unchecked"
    assert record["reason_code"] == "probe_refused"


def test_a_respelled_key_is_reported_as_the_sql_wrote_it():
    # The probe reads CUSTOMER_ID, but the finding, its data and its detail name
    # the customer_id the model wrote, as the record beside it does (#1366).
    adapter = _FoldingWarehouse(
        "snowflake",
        {
            "db.s.orders": {"CUSTOMER_ID": ["a", "b"]},
            "db.s.customers": {"CUSTOMER_ID": ["x", "y"]},
        },
    )
    result = _coverage(adapter, "customer_id", "customer_id")
    (finding,) = result.findings
    (record,) = result.records
    assert finding.code == "join_no_match"
    assert finding.column == "customer_id"
    assert finding.data["from_columns"] == record["from_columns"] == ["customer_id"]
    assert finding.data["to_columns"] == record["to_columns"] == ["customer_id"]
    assert "(db.s.orders.customer_id) to (db.s.customers.customer_id)" in (
        finding.detail
    )


@pytest.mark.parametrize("dialect", ["databricks", "trino"])
def test_a_key_that_already_resolved_keeps_its_finding_fingerprint(dialect):
    # Databricks and Trino resolve an upper-case key against the lower-case
    # column regardless of quoting, so the probe already worked before #1366;
    # respelling it for the probe must not rename the finding, or a baseline
    # taken before would report it as introduced.
    from exmergo_dex_core.transform.finding_attribution import fingerprint

    adapter = _FoldingWarehouse(
        dialect,
        {
            "db.s.orders": {"customer_id": ["a", "b"]},
            "db.s.customers": {"customer_id": ["x", "y"]},
        },
    )
    result = _coverage(adapter, "CUSTOMER_ID", "CUSTOMER_ID")
    (finding,) = result.findings
    assert finding.column == "CUSTOMER_ID"
    assert finding.data["from_columns"] == ["CUSTOMER_ID"]
    assert fingerprint(finding) == (
        "join_contract|join_no_match|m|CUSTOMER_ID|left join customers"
    )


def test_an_unreadable_relation_is_a_note_and_keeps_the_sql_spelling():
    class _Unreadable(_FoldingWarehouse):
        def table_metadata(self, identifier: str):
            raise RuntimeError("no metadata")

    adapter = _Unreadable(
        "snowflake",
        {
            "db.s.orders": {"CUSTOMER_ID": ["a"]},
            "db.s.customers": {"CUSTOMER_ID": ["a"]},
        },
    )
    result = _coverage(adapter, "customer_id", "customer_id")
    (record,) = result.records
    assert record["reason_code"] == "probe_refused"
    assert (
        "m: the join keys on db.s.orders were probed as the SQL spells them: "
        "the relation's columns could not be read (RuntimeError)"
    ) in result.notes


@pytest.mark.parametrize("dialect", ["snowflake", "postgres"])
def test_a_filtered_sides_columns_are_probed_as_the_relation_stores_them(dialect):
    # A side read through a filtering CTE carries its filter as DuckDB SQL over
    # quoted names (#1085); on Snowflake "status" named no column of a relation
    # that stores STATUS, so the probe was refused and the join went unmeasured.
    # The filter is respelled as the keys are (#1456).
    fold = str.upper if dialect == "snowflake" else str.lower
    adapter = _FoldingWarehouse(
        dialect,
        {
            "db.s.orders": {
                fold("customer_id"): ["a", "b", "z"],
                fold("status"): ["paid", "paid", "cancelled"],
            },
            "db.s.customers": {fold("customer_id"): ["a", "x"]},
        },
    )
    candidate = verify_mod.JoinCandidate(
        from_relation="db.s.orders",
        from_columns=("customer_id",),
        to_relation="db.s.customers",
        to_columns=("customer_id",),
        side="inner",
        label="inner join customers",
        from_filters=("\"status\" <> 'cancelled'",),
    )
    result = verify_mod.join_coverage(
        adapter,
        verify_mod.JoinContractPlan(candidates={"m": [candidate]}),
        {},
        list(adapter.tables),
        handshake=lambda _estimate, _count: None,
    )
    (record,) = result.records
    # Two kept orders, one matched: the cancelled order's z is never counted.
    assert record["measured"]["eligible_rows"] == 2, record
    assert record["measured"]["matched_rows"] == 1, record
