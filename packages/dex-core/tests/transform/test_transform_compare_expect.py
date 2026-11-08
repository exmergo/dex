"""`transform compare --expect` on a real dbt project (#1157).

Three repairs that are meant to change rows (a backfill, an exclusion, late
arrivals), each with an over-broad variant of the kind ADE-bench agents
shipped uncompared. The correct repair is ``as_expected``; the over-broad one
is ``unexpected`` and says why. The verdict is recorded on the plan and read
back as the build's ``obligations.comparison``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from exmergo_dex_core.cli import main
from exmergo_dex_core.config import DexConfig
from exmergo_dex_core.engine import DexEngine
from exmergo_dex_core.storage import FilesystemStore
from exmergo_dex_core.transform.plans import EditKind, PlanEdit

pytestmark = pytest.mark.live_open_adapter

_VISITS = """
select * from (values
  (date '2026-03-01', 'clinic_a'), (date '2026-03-01', 'clinic_b'),
  (date '2026-03-03', 'clinic_a'), (date '2026-03-05', 'clinic_b')
) as v(visited_on, clinic)
"""
_DAYS = """
select * from (values (date '2026-03-01'), (date '2026-03-02'), (date '2026-03-03'),
  (date '2026-03-04'), (date '2026-03-05')) as d(day)
"""
_DAILY = """
select visited_on as visit_date, count(*) as visits
from {{ ref('stg_visits') }}
group by visited_on
"""
_DAILY_FILLED = """
with days as (select day as visit_date from {{ ref('stg_days') }}),
counted as (
  select visited_on, count(*) as visits from {{ ref('stg_visits') }} group by visited_on
)
select days.visit_date, coalesce(counted.visits, 0) as visits
from days left join counted on counted.visited_on = days.visit_date
"""
#: The ADE-bench over-reach: every day crossed with every clinic, zero-filled.
_DAILY_CROSSED = """
with days as (select day as visit_date from {{ ref('stg_days') }}),
clinics as (select distinct clinic from {{ ref('stg_visits') }}),
counted as (
  select visited_on, clinic, count(*) as visits
  from {{ ref('stg_visits') }} group by visited_on, clinic
)
select days.visit_date, coalesce(counted.visits, 0) as visits
from days cross join clinics
left join counted on counted.visited_on = days.visit_date
  and counted.clinic = clinics.clinic
"""

_WORKSPACES = """
select * from (values (1, false), (2, true), (3, false)) as w(workspace_id, archived)
"""
_MEMBERS = """
select * from (values (10, 1), (11, 2), (12, 2), (13, 3), (14, 9))
  as m(membership_id, workspace_id)
"""
_MEMBERSHIPS = "select membership_id, workspace_id from {{ ref('stg_members') }}"
_MEMBERSHIPS_LIVE = """
select membership_id, workspace_id from {{ ref('stg_members') }}
where workspace_id not in (
  select workspace_id from {{ ref('stg_workspaces') }} where archived
)
"""
#: The inner join drops membership 14, whose workspace is not in the list.
_MEMBERSHIPS_JOINED = """
select m.membership_id, m.workspace_id
from {{ ref('stg_members') }} m
join {{ ref('stg_workspaces') }} w on w.workspace_id = m.workspace_id
where not w.archived
"""

_SCANS = """
select * from (values (1, 0, 'ok'), (2, 3, 'ok'), (3, 0, 'ok'), (4, 5, 'ok'))
  as s(scan_id, days_late, status)
"""
_RECENT = """
select scan_id, days_late, status from {{ ref('stg_scans') }} where days_late <= 1
"""
_ALL_SCANS = "select scan_id, days_late, status from {{ ref('stg_scans') }}"
#: Picks up the late scans, and also rewrites every scan's status.
_ALL_SCANS_RELABELLED = """
select scan_id, days_late,
  case when days_late > 1 then 'late' else 'on_time' end as status
from {{ ref('stg_scans') }}
"""


def _engine(project: Path) -> DexEngine:
    return DexEngine(
        connector="duckdb",
        path=str(project.parent / "dev.duckdb"),
        repo_root=str(project.parent),
        store=FilesystemStore(project.parent),
        config=DexConfig(
            connector="duckdb", dbt_target="dev", dbt_project_dir=project.name
        ),
    )


@pytest.fixture
def project(tmp_path: Path) -> Path:
    pytest.importorskip("dbt.adapters.duckdb")
    root = tmp_path / "analytics"
    models = root / "models"
    models.mkdir(parents=True)
    (root / "dbt_project.yml").write_text(
        'name: p\nversion: "1.0.0"\nprofile: p\nmodel-paths: ["models"]\n',
        encoding="utf-8",
    )
    (root / "profiles.yml").write_text(
        "p:\n  target: dev\n  outputs:\n    dev:\n      type: duckdb\n"
        f"      path: {tmp_path / 'dev.duckdb'}\n",
        encoding="utf-8",
    )
    for name, sql in {
        "stg_visits": _VISITS,
        "stg_days": _DAYS,
        "daily_visits": _DAILY,
        "stg_workspaces": _WORKSPACES,
        "stg_members": _MEMBERS,
        "memberships": _MEMBERSHIPS,
        "stg_scans": _SCANS,
        "scans": _RECENT,
    }.items():
        (models / f"{name}.sql").write_text(sql, encoding="utf-8")
    _engine(root).build(target="dev")
    return root


def _plan(engine: DexEngine, model: str, sql: str) -> str:
    return engine.plan(
        "repair",
        edits=[
            PlanEdit(
                path=f"models/{model}.sql", new_content=sql, kind=EditKind.MODEL_SQL
            )
        ],
    ).plan_id


BACKFILL = {"added": {"where": "visits = 0"}}
EXCLUSION = {
    "removed": {
        "where": "workspace_id in (select workspace_id from ref('stg_workspaces') "
        "where archived)"
    }
}
LATE = {"added": {"where": "days_late > 1"}}


@pytest.mark.parametrize(
    ("model", "sql", "expect", "key"),
    [
        ("daily_visits", _DAILY_FILLED, BACKFILL, "visit_date"),
        ("memberships", _MEMBERSHIPS_LIVE, EXCLUSION, "membership_id"),
        ("scans", _ALL_SCANS, LATE, "scan_id"),
    ],
    ids=["backfill", "exclusion", "late_arrivals"],
)
def test_the_intended_repair_is_as_expected(project, model, sql, expect, key):
    engine = _engine(project)
    plan_id = _plan(engine, model, sql)
    result = engine.compare(plan_id, key_columns=[key], expectation=expect)
    assert result.status == "as_expected", (result.detail, result.expectation)
    assert result.reason_code == "matches_expected_change"
    assert result.expectation["findings"] == []
    recorded = engine.store.load_plan(plan_id).comparisons[model]
    assert recorded["status"] == "as_expected"
    assert recorded["expectation"] == result.expectation_digest


@pytest.mark.parametrize(
    ("model", "sql", "expect", "key", "findings", "sampled"),
    [
        (
            "daily_visits",
            _DAILY_CROSSED,
            BACKFILL,
            "visit_date",
            ["unexpected_changed_column", "grain_not_held"],
            "changed",
        ),
        (
            "memberships",
            _MEMBERSHIPS_JOINED,
            EXCLUSION,
            "membership_id",
            ["unexpected_removed"],
            "removed",
        ),
        (
            "scans",
            _ALL_SCANS_RELABELLED,
            LATE,
            "scan_id",
            ["unexpected_changed_column"],
            "changed",
        ),
    ],
    ids=["backfill", "exclusion", "late_arrivals"],
)
def test_the_over_broad_repair_is_unexpected(
    project, model, sql, expect, key, findings, sampled
):
    engine = _engine(project)
    plan_id = _plan(engine, model, sql)
    result = engine.compare(plan_id, key_columns=[key], expectation=expect)
    assert result.status == "unexpected", (result.detail, result.expectation)
    assert result.reason_code == "unexpected_change"
    assert result.expectation["findings"] == findings
    assert result.expectation["unexpected_keys"][sampled]


def test_the_exclusion_names_the_membership_it_should_not_have_dropped(project):
    engine = _engine(project)
    plan_id = _plan(engine, "memberships", _MEMBERSHIPS_JOINED)
    result = engine.compare(
        plan_id, key_columns=["membership_id"], expectation=EXCLUSION
    )
    assert result.expectation["removed"] == {"total": 3, "matching": 2, "unexpected": 1}
    assert result.expectation["unexpected_keys"] == {"removed": [{"membership_id": 14}]}
    # The statement is echoed as written, so its digest is the same on any
    # target, whichever relation the ref() resolved to.
    assert "ref('stg_workspaces')" in result.expectation["stated"]["removed"]["where"]


def test_the_recorded_verdict_settles_the_builds_comparison_obligation(project):
    engine = _engine(project)
    plan_id = _plan(engine, "scans", _ALL_SCANS)
    engine.compare(plan_id, key_columns=["scan_id"], expectation=LATE)
    engine.apply(plan_id)
    built = _engine(project).build(target="dev", for_plan=plan_id)
    obligations = built.data()["completion"]["obligations"]
    assert obligations["comparison"] == [
        {"model": "scans", "kind": "comparison", "status": "satisfied"}
    ]


def test_an_intended_change_applied_uncompared_is_an_open_obligation(project):
    engine = _engine(project)
    plan_id = _plan(engine, "scans", _ALL_SCANS)
    engine.apply(plan_id)
    built = _engine(project).build(target="dev", for_plan=plan_id)
    assert built.data()["completion"]["obligations"]["comparison"] == [
        {
            "model": "scans",
            "kind": "comparison",
            "status": "unchecked",
            "reason_code": "not_compared",
        }
    ]


def test_the_cli_reads_an_expect_file(project, tmp_path, capsys):
    (tmp_path / ".dex").mkdir(exist_ok=True)
    (tmp_path / ".dex" / "config.yml").write_text(
        f"connector: duckdb\ndbt_target: dev\ndbt_project_dir: {project.name}\n"
        f"path: {tmp_path / 'dev.duckdb'}\n",
        encoding="utf-8",
    )
    engine = _engine(project)
    plan_id = _plan(engine, "daily_visits", _DAILY_FILLED)
    expect = tmp_path / "expect.json"
    expect.write_text(json.dumps(BACKFILL), encoding="utf-8")
    capsys.readouterr()
    main(
        [
            "--repo-root",
            str(tmp_path),
            "--path",
            str(tmp_path / "dev.duckdb"),
            "transform",
            "compare",
            plan_id,
            "--key",
            "visit_date",
            "--expect-file",
            str(expect),
        ]
    )
    envelope = json.loads(capsys.readouterr().out)
    assert envelope["status"] == "ok", envelope
    assert envelope["data"]["status"] == "as_expected"
    assert envelope["data"]["expectation"]["added"]["matching"] == 2


def test_the_cli_refuses_a_malformed_expectation_before_compiling(
    project, tmp_path, capsys
):
    (tmp_path / ".dex").mkdir(exist_ok=True)
    (tmp_path / ".dex" / "config.yml").write_text(
        f"connector: duckdb\ndbt_target: dev\ndbt_project_dir: {project.name}\n"
        f"path: {tmp_path / 'dev.duckdb'}\n",
        encoding="utf-8",
    )
    engine = _engine(project)
    plan_id = _plan(engine, "daily_visits", _DAILY_FILLED)
    capsys.readouterr()
    main(
        [
            "--repo-root",
            str(tmp_path),
            "transform",
            "compare",
            plan_id,
            "--expect",
            '{"added": "sometimes", "nope": 1}',
        ]
    )
    envelope = json.loads(capsys.readouterr().out)
    assert envelope["status"] == "error"
    assert "not nope" in envelope["errors"][0]
