# ruff: noqa: S608 -- fixture SQL over test-owned relation names
"""`transform plan` says what an edit changes about each model it rewrites beside
the request, and which of those changes no assumption records (#1115).

Two failures motivate it: a filter meant for the aggregates that also lands on a
roster model, and a coverage repair that also changes what an empty window
counts. Both pass every other plan-time check. The unit tests below read the
analysis directly; the last ones drive the CLI over a small DuckDB project.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from exmergo_dex_core.cli import main
from exmergo_dex_core.transform import change_scope as cs
from exmergo_dex_core.transform.completion import BuildNode, summarize_completion

from .test_build_completion import _evidence, _uid

PATH = "models/marts/site_daily.sql"
PRIOR = (
    "with v as (\n"
    "  select visit_date, count(*) as visits, avg(duration) as avg_duration\n"
    "  from {{ ref('stg_visits') }} group by visit_date\n"
    ")\n"
    "select visit_date, visits, avg_duration, cast(site_id as integer) as site_id\n"
    "from v\n"
)


def _scope(authored: str, assumptions: list | None = None, prior: str = PRIOR):
    return cs.scope_for(PATH, prior, authored, "duckdb", assumptions or [])


def _kinds(scope) -> list[tuple[str, str | None, str | None]]:
    return [(c.kind, c.column, c.status) for c in scope.changes]


# --- outputs ---------------------------------------------------------------------


def test_count_star_to_count_column_in_a_cte_is_an_aggregate_change():
    scope = _scope(PRIOR.replace("count(*)", "count(visit_id)"))
    assert _kinds(scope) == [("aggregate_changed", "visits", "undeclared")]
    (change,) = scope.changes
    assert change.scope == "v"
    assert "count(visit_id)" in change.detail and "count(*)" in change.detail
    assert scope.population == "kept"
    assert scope.outputs == "changed"


def test_a_coalesce_removed_is_a_null_handling_change():
    prior = PRIOR.replace(
        "avg(duration) as avg_duration", "coalesce(avg(duration), 0) as avg_duration"
    )
    scope = _scope(PRIOR, prior=prior)
    assert _kinds(scope) == [("null_handling_changed", "avg_duration", "undeclared")]


def test_a_changed_cast_is_a_type_change():
    scope = _scope(PRIOR.replace("as integer", "as varchar"))
    assert _kinds(scope) == [("type_changed", "site_id", "undeclared")]


def test_a_dropped_column_is_judged_and_an_added_one_is_only_listed():
    authored = PRIOR.replace(
        ", cast(site_id as integer) as site_id", ", 1 as visit_rank"
    )
    scope = _scope(authored)
    assert sorted(_kinds(scope)) == [
        ("column_added", "visit_rank", None),
        ("column_removed", "site_id", "undeclared"),
    ]
    assert scope.undeclared == 1


def test_a_rewritten_formula_is_listed_without_a_verdict():
    scope = _scope(PRIOR.replace("avg(duration)", "avg(duration) / 60.0"))
    assert _kinds(scope) == [("expression_changed", "avg_duration", None)]
    assert scope.undeclared == 0


def test_reformatting_and_renaming_an_alias_are_silent():
    authored = (
        "WITH v AS (SELECT s.visit_date, COUNT(*) AS visits,\n"
        "  AVG(s.duration) AS avg_duration FROM {{ ref('stg_visits') }} AS s\n"
        "  GROUP BY visit_date)\n"
        "SELECT visit_date, visits, avg_duration, CAST(site_id AS INTEGER) AS site_id "
        "FROM v"
    )
    scope = _scope(authored)
    assert scope.changes == []
    assert (scope.population, scope.outputs) == ("kept", "kept")


@pytest.mark.parametrize(
    ("before", "after", "population"),
    [
        ("visit_date", "s.visit_date", "kept"),
        ("t.visit_date", "s.visit_date", "kept"),
        ("visit_date", "month", "changed"),
        ("visit_date", "visit_date, site_id", "changed"),
        ("visit_date, site_id", "visit_date", "changed"),
        ("visit_date, site_id", "s.visit_date, s.site_id", "kept"),
    ],
)
def test_group_by_qualifier_changes_do_not_change_population(
    before: str, after: str, population: str
):
    prior = f"select visit_date, count(*) as visits from visits t group by {before}"
    authored = f"select s.visit_date, count(*) as visits from visits s group by {after}"

    scope = cs.scope_for(PATH, prior, authored, "duckdb", [])

    assert scope.population == population
    assert scope.outputs == "kept"
    assert ("group_by_changed" in {change.kind for change in scope.changes}) is (
        population == "changed"
    )


def test_group_by_alias_rename_keeps_relation_identity():
    prior = "select s.visit_date from visits as s group by s.visit_date"
    authored = "select t.visit_date from visits as t group by t.visit_date"

    scope = cs.scope_for(PATH, prior, authored, "duckdb", [])

    assert scope.population == "kept"
    assert "group_by_changed" not in {change.kind for change in scope.changes}


def test_group_by_different_joined_relations_remains_a_change():
    prior = (
        "select a.id from customers as a "
        "join orders as b on a.id = b.customer_id group by a.id"
    )
    authored = (
        "select b.id from customers as a "
        "join orders as b on a.id = b.customer_id group by b.id"
    )

    scope = cs.scope_for(PATH, prior, authored, "duckdb", [])

    assert scope.population == "changed"
    assert "group_by_changed" in {change.kind for change in scope.changes}
    (change,) = [
        change for change in scope.changes if change.kind == "group_by_changed"
    ]
    assert "a.id" in change.detail and "b.id" in change.detail


def test_group_by_self_join_aliases_are_not_assumed_equivalent():
    prior = (
        "select a.id from customers as a "
        "join customers as b on a.parent_id = b.id group by a.id"
    )
    authored = (
        "select b.id from customers as a "
        "join customers as b on a.parent_id = b.id group by b.id"
    )

    scope = cs.scope_for(PATH, prior, authored, "duckdb", [])

    assert scope.population == "changed"
    assert "group_by_changed" in {change.kind for change in scope.changes}


def test_bare_group_by_column_in_join_is_not_assumed_unambiguous():
    prior = (
        "select a.id from customers as a "
        "join orders as b on a.id = b.customer_id group by id"
    )
    authored = (
        "select a.id from customers as a "
        "join orders as b on a.id = b.customer_id group by a.id"
    )

    scope = cs.scope_for(PATH, prior, authored, "duckdb", [])

    assert scope.population == "changed"
    assert "group_by_changed" in {change.kind for change in scope.changes}


@pytest.mark.parametrize(
    ("before", "after"),
    [
        (
            "catalog_a.schema_a.visits.visit_date",
            "catalog_b.schema_a.visits.visit_date",
        ),
        (
            "catalog_a.schema_a.visits.visit_date",
            "catalog_a.schema_b.visits.visit_date",
        ),
    ],
)
def test_group_by_keeps_catalog_and_database_qualifiers(
    before: str, after: str
):
    prior = f"select s.visit_date from visits as s group by {before}"
    authored = f"select s.visit_date from visits as s group by {after}"

    scope = cs.scope_for(PATH, prior, authored, "duckdb", [])

    assert scope.population == "changed"
    assert "group_by_changed" in {change.kind for change in scope.changes}


# --- population ------------------------------------------------------------------


def test_an_added_predicate_changes_the_population_and_names_its_columns():
    scope = _scope(PRIOR + "where visit_date >= '2024-01-01'\n")
    assert scope.population == "changed"
    (change,) = scope.changes
    assert (change.axis, change.kind, change.status) == (
        "population",
        "predicate_added",
        "undeclared",
    )
    assert change.columns == ["visit_date"]


def test_a_byte_identical_edit_is_not_reported():
    assert cs.analyze([(PATH, PRIOR, PRIOR)], [], "duckdb") is None


def test_a_broad_filter_report_says_which_populations_change_and_which_stay():
    roster = "select account_id, region, is_sandbox from {{ ref('stg_accounts') }}\n"
    summary = (
        "select region, count(*) as accounts from {{ ref('stg_accounts') }} "
        "group by region\n"
    )
    report = cs.analyze(
        [
            ("models/marts/dim_accounts.sql", roster, roster + "-- the directory\n"),
            (
                "models/marts/region_summary.sql",
                summary,
                summary.replace("group by", "where not is_sandbox group by"),
            ),
        ],
        [],
        "duckdb",
    )
    assert report is not None
    # The comment-only edit is byte-different but changes nothing it returns.
    assert report["population"] == {
        "changed": ["region_summary"],
        "kept": ["dim_accounts"],
        "unchecked": [],
    }
    assert report["undeclared"] == 1


# --- settling --------------------------------------------------------------------


def test_an_assumption_naming_the_column_declares_the_change():
    assumption = {
        "model": "site_daily",
        "decision": "What an empty day counts",
        "chosen": "zero visits",
        "evidence": "default",
        "column": "visits",
    }
    scope = _scope(
        PRIOR.replace("count(*)", "coalesce(count(visit_id), 0)"), [assumption]
    )
    assert {c.kind for c in scope.changes} == {
        "aggregate_changed",
        "null_handling_changed",
    }
    for change in scope.changes:
        assert change.status == "declared"
        assert change.assumption == {
            "decision": "What an empty day counts",
            "chosen": "zero visits",
            "evidence": "default",
            "column": "visits",
        }
        assert change.note and "default" in change.note
    assert scope.undeclared == 0


def test_an_assumption_on_another_column_does_not_declare_it():
    scope = _scope(
        PRIOR.replace("count(*)", "count(visit_id)"),
        [{"decision": "d", "chosen": "c", "evidence": "request", "column": "site_id"}],
    )
    assert scope.changes[0].status == "undeclared"


def test_a_population_change_is_declared_by_an_assumption_on_its_predicate_column():
    scope = _scope(
        PRIOR + "where not is_sandbox\n",
        [
            {
                "decision": "Sandbox sites",
                "chosen": "excluded",
                "evidence": "request",
                "column": "is_sandbox",
            }
        ],
    )
    (change,) = scope.changes
    assert change.status == "declared"


def test_a_qualified_assumption_column_matches_its_bare_name():
    scope = _scope(
        PRIOR.replace("count(*)", "count(visit_id)"),
        [
            {
                "decision": "d",
                "chosen": "c",
                "evidence": "contract",
                "column": "site_daily.visits",
            }
        ],
    )
    assert scope.changes[0].status == "declared"


def test_analyze_settles_against_the_model_the_assumption_belongs_to():
    report = cs.analyze(
        [(PATH, PRIOR, PRIOR.replace("count(*)", "count(visit_id)"))],
        [
            {"model": "other", "column": "visits", "evidence": "request"},
        ],
        "duckdb",
    )
    assert report["models"][0]["changes"][0]["status"] == "undeclared"


# --- what it cannot read ---------------------------------------------------------


def test_a_macro_leaves_the_model_unchecked_with_its_reason():
    authored = PRIOR.replace("count(*)", "{{ count_rows() }}")
    scope = _scope(authored)
    assert (scope.population, scope.outputs) == ("unchecked", "unchecked")
    assert scope.reason and "count_rows" in scope.reason
    assert cs.stored_form(
        {"models": [scope.model_dump(mode="json", exclude_none=True)]}
    ) == {"site_daily": [{"kind": "unread", "status": "unchecked"}]}


def test_a_star_leaves_removal_unread_but_still_compares_the_ctes():
    prior = PRIOR.replace(
        "select visit_date, visits, avg_duration, cast(site_id as integer) as site_id",
        "select *",
    )
    scope = _scope(prior.replace("count(*)", "count(visit_id)"), prior=prior)
    assert scope.outputs == "changed"
    assert scope.reason and "star" in scope.reason
    assert _kinds(scope) == [("aggregate_changed", "visits", "undeclared")]


def test_an_unread_star_with_nothing_else_changed_is_stored_unchecked():
    prior = PRIOR.replace(
        "select visit_date, visits, avg_duration, cast(site_id as integer) as site_id",
        "select *",
    )
    scope = _scope(prior + "-- reviewed\n", prior=prior)
    assert (scope.population, scope.outputs) == ("kept", "unchecked")
    assert cs.stored_form(
        {"models": [scope.model_dump(mode="json", exclude_none=True)]}
    ) == {"site_daily": [{"kind": "unread", "status": "unchecked"}]}


def test_a_star_with_a_changed_cte_is_still_stored_unread():
    prior = PRIOR.replace(
        "select visit_date, visits, avg_duration, cast(site_id as integer) as site_id",
        "select *",
    )
    scope = _scope(
        prior.replace("count(*)", "count(visit_id)"),
        [{"decision": "d", "chosen": "c", "evidence": "request", "column": "visits"}],
        prior=prior,
    )
    assert scope.outputs == "changed" and scope.outputs_unread is True
    assert cs.stored_form(
        {"models": [scope.model_dump(mode="json", exclude_none=True)]}
    ) == {
        "site_daily": [
            {"kind": "aggregate_changed", "column": "visits", "status": "declared"},
            {"kind": "unread", "status": "unchecked"},
        ]
    }


def test_what_the_caps_cut_is_stored_unchecked():
    columns = ", ".join(f"cast(c{i} as integer) as c{i}" for i in range(12))
    prior = "select " + columns + " from {{ ref('t') }}"
    edits = [
        (f"models/m{i:02d}.sql", prior, prior.replace("as integer", "as varchar"))
        for i in range(cs.MAX_MODELS + 2)
    ]
    report = cs.analyze(edits, [], "duckdb")
    assert report["elided"] == {"models": 2}
    assert report["beyond_cap"] == ["m25", "m26"]
    stored = cs.stored_form(report)
    assert stored["m00"][-1] == {"kind": "elided", "status": "unchecked"}
    assert stored["m26"] == [{"kind": "elided", "status": "unchecked"}]


def test_an_edit_inside_a_union_is_unchecked_not_kept():
    prior = (
        "with both as (\n"
        "  select account_id from {{ ref('a') }}\n"
        "  union all\n"
        "  select account_id from {{ ref('b') }}\n"
        ")\nselect account_id from both\n"
    )
    authored = prior.replace("{{ ref('b') }}", "{{ ref('b') }} where active")
    scope = _scope(authored, prior=prior)
    assert scope.population == "unchecked"
    assert scope.reason and "`both`" in scope.reason
    assert cs.stored_form(
        {"models": [scope.model_dump(mode="json", exclude_none=True)]}
    ) == {"site_daily": [{"kind": "unread", "status": "unchecked"}]}


def test_the_caps_count_what_they_cut():
    columns = ", ".join(f"cast(c{i} as integer) as c{i}" for i in range(12))
    prior = "select " + columns + " from {{ ref('t') }}"
    scope = _scope(prior.replace("as integer", "as varchar"), prior=prior)
    assert len(scope.changes) == cs.MAX_CHANGES_PER_MODEL
    assert scope.elided == 12 - cs.MAX_CHANGES_PER_MODEL
    assert scope.undeclared == 12


def test_the_warning_names_the_change_and_both_fixes():
    report = cs.analyze(
        [(PATH, PRIOR, PRIOR.replace("count(*)", "count(visit_id)"))], [], "duckdb"
    )
    (warning,) = cs.warnings(report)
    assert warning.startswith("'site_daily':")
    assert "count(visit_id)" in warning
    assert "assumption" in warning and "undo it" in warning


# --- completion ------------------------------------------------------------------


def test_a_for_plan_build_carries_undeclared_changes_as_obligations():
    report = "rpt_sites"
    nodes = [BuildNode(unique_id=_uid(report), name=report, status="success")]
    completion = summarize_completion(
        _evidence(nodes, required=[report]),
        None,
        change_scope={
            report: [
                {
                    "kind": "aggregate_changed",
                    "column": "visits",
                    "status": "undeclared",
                },
                {"kind": "type_changed", "column": "site_id", "status": "declared"},
                {"kind": "unread", "status": "unchecked"},
            ],
            "not_required": [{"kind": "column_removed", "status": "undeclared"}],
        },
    )
    assert completion.data()["obligations"]["change_scope"] == [
        {
            "model": report,
            "kind": "change_scope",
            "status": "undeclared",
            "column": "visits",
            "change": "aggregate_changed",
        },
        {
            "model": report,
            "kind": "change_scope",
            "status": "unchecked",
            "change": "unread",
        },
    ]


# --- the CLI ---------------------------------------------------------------------


ROSTER = "select account_id, region, is_sandbox from {{ source('raw', 'accounts') }}\n"
SUMMARY = (
    "select region, count(*) as accounts\n"
    "from {{ source('raw', 'accounts') }}\ngroup by region\n"
)


@pytest.fixture
def accounts_project(tmp_path: Path) -> tuple[Path, Path]:
    duckdb = pytest.importorskip("duckdb")
    warehouse = tmp_path / "accounts.duckdb"
    conn = duckdb.connect(str(warehouse))
    conn.execute(
        "CREATE TABLE accounts AS SELECT range AS account_id, "
        "CASE WHEN range % 2 = 0 THEN 'emea' ELSE 'amer' END AS region, "
        "range % 5 = 0 AS is_sandbox FROM range(20)"
    )
    conn.close()
    project = tmp_path / "analytics"
    (project / "models" / "marts").mkdir(parents=True)
    (project / "dbt_project.yml").write_text(
        'name: acct\nversion: "1.0.0"\nprofile: acct\nmodel-paths: ["models"]\n',
        encoding="utf-8",
    )
    (project / "profiles.yml").write_text(
        "acct:\n  target: dev\n  outputs:\n    dev:\n      type: duckdb\n"
        f"      path: {tmp_path / 'dev.duckdb'}\n",
        encoding="utf-8",
    )
    (project / "models" / "marts" / "sources.yml").write_text(
        "version: 2\nsources:\n  - name: raw\n    tables:\n      - name: accounts\n",
        encoding="utf-8",
    )
    (project / "models" / "marts" / "dim_accounts.sql").write_text(ROSTER)
    (project / "models" / "marts" / "region_summary.sql").write_text(SUMMARY)
    return tmp_path, warehouse


def _plan(repo: Path, warehouse: Path, body: dict, capsys) -> dict:
    payload = repo / "edits.json"
    payload.write_text(json.dumps(body), encoding="utf-8")
    rc = main(
        [
            "--repo-root",
            str(repo),
            "transform",
            "plan",
            "exclude sandbox accounts",
            "--edits-file",
            str(payload),
            "--connector",
            "duckdb",
            "--path",
            str(warehouse),
        ]
    )
    envelope = json.loads(capsys.readouterr().out)
    assert rc == 0, envelope
    return envelope


def _edit(name: str, content: str) -> dict:
    return {"path": f"models/marts/{name}.sql", "kind": "model_sql", "content": content}


def test_plan_reports_an_undeclared_roster_filter_and_stores_it(
    accounts_project, capsys
):
    repo, warehouse = accounts_project
    filtered = ROSTER.rstrip() + " where not is_sandbox\n"
    envelope = _plan(
        repo,
        warehouse,
        {
            "edits": [
                _edit("dim_accounts", filtered),
                _edit(
                    "region_summary",
                    SUMMARY.replace("group by", "where not is_sandbox\ngroup by"),
                ),
            ]
        },
        capsys,
    )
    scope = envelope["data"]["change_scope"]
    assert scope["population"]["changed"] == ["dim_accounts", "region_summary"]
    assert scope["undeclared"] == 2
    assert any(
        w.startswith("'dim_accounts': this plan changes more")
        for w in envelope["warnings"]
    )
    plan_id = envelope["data"]["plan_id"]
    stored = json.loads(
        (repo / ".dex" / "plans" / f"{plan_id}.json").read_text(encoding="utf-8")
    )
    assert stored["change_scope"]["dim_accounts"] == [
        {"kind": "predicate_added", "status": "undeclared"}
    ]


def test_plan_settles_a_declared_filter_and_relays_the_detail(accounts_project, capsys):
    repo, warehouse = accounts_project
    envelope = _plan(
        repo,
        warehouse,
        {
            "edits": [
                _edit(
                    "region_summary",
                    SUMMARY.replace("group by", "where not is_sandbox\ngroup by"),
                )
            ],
            "declarations": [
                {
                    "model": "region_summary",
                    "assumptions": [
                        {
                            "decision": "Sandbox accounts in the summary",
                            "chosen": "excluded",
                            "evidence": "request",
                            "column": "is_sandbox",
                            "detail": "the request names the region summary",
                        }
                    ],
                }
            ],
        },
        capsys,
    )
    data = envelope["data"]
    (model,) = data["change_scope"]["models"]
    assert model["changes"][0]["status"] == "declared"
    assert data["change_scope"]["undeclared"] == 0
    assert not any("this plan changes more" in w for w in envelope["warnings"])
    (decision,) = data["decisions"]
    assert decision["detail"] == "the request names the region summary"


def test_a_new_model_has_no_change_scope(accounts_project, capsys):
    repo, warehouse = accounts_project
    envelope = _plan(
        repo,
        warehouse,
        {
            "edits": [
                _edit(
                    "account_count",
                    "select count(*) as n from {{ ref('dim_accounts') }}\n",
                )
            ]
        },
        capsys,
    )
    assert "change_scope" not in envelope["data"]
