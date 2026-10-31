"""`transform compare`: one model before and after a stored plan (#856)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from exmergo_dex_core.cli import main
from exmergo_dex_core.config import DexConfig
from exmergo_dex_core.engine import DexEngine
from exmergo_dex_core.storage import FilesystemStore
from exmergo_dex_core.transform.plans import EditKind, PlanEdit
from exmergo_dex_core.transform.refactor_compare import nondeterministic_expression

pytestmark = pytest.mark.live_open_adapter

_ORIGINAL = """
select t.team_id, coalesce(c.event_count, 0) as event_count
from {{ ref('teams') }} t
left join {{ ref('event_counts') }} c on c.team_id = t.team_id
left join {{ ref('user_names') }} n on n.team_id = t.team_id
"""

#: The refactor from the issue: an inner join extracted as an intermediate. It
#: builds, keeps the team row, and turns the count into zero.
_INTERMEDIATE = """
select c.team_id, c.event_count
from {{ ref('event_counts') }} c
inner join {{ ref('user_names') }} n on n.team_id = c.team_id
"""
_EXTRACTED = """
select t.team_id, coalesce(i.event_count, 0) as event_count
from {{ ref('teams') }} t
left join {{ ref('int_counts') }} i on i.team_id = t.team_id
"""

_EQUIVALENT = """
with counted as (select team_id, event_count from {{ ref('event_counts') }})
select t.team_id, coalesce(c.event_count, 0) as event_count
from {{ ref('teams') }} t
left join counted c on c.team_id = t.team_id
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
    (models / "teams.sql").write_text("select 10 as team_id", encoding="utf-8")
    (models / "event_counts.sql").write_text(
        "select 10 as team_id, 1 as event_count", encoding="utf-8"
    )
    (models / "user_names.sql").write_text(
        "select 20 as team_id, 'someone' as user_name", encoding="utf-8"
    )
    (models / "team_activity.sql").write_text(_ORIGINAL, encoding="utf-8")
    _engine(root).build(target="dev")
    return root


def _plan(engine: DexEngine, project: Path, *edits: tuple[str, str]) -> str:
    planned = engine.plan(
        "refactor",
        edits=[
            PlanEdit(path=path, new_content=content, kind=EditKind.MODEL_SQL)
            for path, content in edits
        ],
    )
    return planned.plan_id


def test_the_issues_join_extraction_is_caught_through_a_same_plan_intermediate(
    project,
):
    engine = _engine(project)
    plan_id = _plan(
        engine,
        project,
        ("models/int_counts.sql", _INTERMEDIATE),
        ("models/team_activity.sql", _EXTRACTED),
    )
    result = engine.compare(plan_id, model="team_activity")
    assert result.status == "different", result.detail
    assert result.reason_code == "rows_differ"
    assert result.multiset == {
        "left_rows": 1,
        "right_rows": 1,
        "only_in_left": 1,
        "only_in_right": 1,
    }
    assert result.baseline_compiled != result.proposed_compiled
    assert result.plan_id == plan_id
    assert result.compared_at
    # int_counts was never built: it is inlined, and neither side reads it.
    assert not any("int_counts" in name for name in result.input_scope)
    assert any("event_counts" in name for name in result.input_scope)


def test_a_positive_refactor_is_equivalent_on_the_compared_inputs(project):
    engine = _engine(project)
    plan_id = _plan(engine, project, ("models/team_activity.sql", _EQUIVALENT))
    result = engine.compare(plan_id)
    assert result.status == "equivalent", result.detail
    assert result.reason_code == "equivalent_on_compared_inputs"
    assert result.model == "team_activity"


def test_a_rewritten_parent_is_inlined_on_both_sides(project):
    """The dev relation of a parent the plan edits holds the old rows, so reading
    it would compare the new model against the old parent."""

    engine = _engine(project)
    plan_id = _plan(
        engine,
        project,
        ("models/event_counts.sql", "select 10 as team_id, 2 as event_count"),
    )
    result = engine.compare(plan_id, model="team_activity")
    assert result.status == "different", result.detail
    assert result.multiset["only_in_right"] == 1


def test_a_rename_needs_a_mapping_and_declares_itself_with_one(project):
    renamed = _ORIGINAL.replace("as event_count", "as events")
    engine = _engine(project)
    plan_id = _plan(engine, project, ("models/team_activity.sql", renamed))
    assert engine.compare(plan_id).reason_code == "schemas_differ"
    mapped = engine.compare(plan_id, column_mapping={"event_count": "events"})
    assert mapped.status == "equivalent", mapped.detail


def test_a_key_gives_per_key_counts(project):
    engine = _engine(project)
    plan_id = _plan(
        engine,
        project,
        ("models/int_counts.sql", _INTERMEDIATE),
        ("models/team_activity.sql", _EXTRACTED),
    )
    result = engine.compare(plan_id, model="team_activity", key_columns=["team_id"])
    assert result.keyed == {"only_in_left": 0, "only_in_right": 0, "changed": 1}
    assert result.key_source == "request"


def test_a_baseline_that_will_not_compile_is_not_checked(project):
    (project / "models" / "team_activity.sql").write_text(
        "select * from {{ ref('no_such_model') }}", encoding="utf-8"
    )
    engine = _engine(project)
    plan_id = _plan(engine, project, ("models/team_activity.sql", _EQUIVALENT))
    result = engine.compare(plan_id)
    assert result.status == "not_checked"
    assert result.reason_code == "baseline_not_compilable"


def test_an_unknown_macro_is_not_checked(project):
    engine = _engine(project)
    plan_id = _plan(
        engine,
        project,
        ("models/team_activity.sql", "select {{ no_such_macro() }} as team_id"),
    )
    result = engine.compare(plan_id)
    assert result.reason_code == "proposed_not_compilable"


def test_a_nondeterministic_model_is_not_checked(project):
    engine = _engine(project)
    plan_id = _plan(
        engine,
        project,
        (
            "models/team_activity.sql",
            _EQUIVALENT + "\nwhere random() > 0",
        ),
    )
    result = engine.compare(plan_id)
    assert result.status == "not_checked"
    assert result.reason_code == "nondeterministic_expression"


def test_an_edit_since_planning_is_not_checked(project):
    engine = _engine(project)
    plan_id = _plan(engine, project, ("models/team_activity.sql", _EQUIVALENT))
    (project / "models" / "team_activity.sql").write_text(
        _ORIGINAL + "\n-- touched\n", encoding="utf-8"
    )
    result = engine.compare(plan_id)
    assert result.reason_code == "plan_stale"
    assert "team_activity" in result.detail


def test_an_applied_plan_is_not_checked(project):
    engine = _engine(project)
    plan_id = _plan(engine, project, ("models/team_activity.sql", _EQUIVALENT))
    engine.apply(plan_id)
    result = engine.compare(plan_id)
    assert result.reason_code == "plan_applied"


def test_a_parent_nobody_built_is_not_checked(project, tmp_path):
    (tmp_path / "dev.duckdb").unlink()
    import duckdb

    duckdb.connect(str(tmp_path / "dev.duckdb")).close()
    engine = _engine(project)
    plan_id = _plan(engine, project, ("models/team_activity.sql", _EQUIVALENT))
    result = engine.compare(plan_id)
    assert result.status == "not_checked"
    assert result.reason_code == "inputs_unsynchronized", result.detail


def test_several_rewritten_models_need_a_choice(project):
    engine = _engine(project)
    plan_id = _plan(
        engine,
        project,
        ("models/team_activity.sql", _EQUIVALENT),
        ("models/teams.sql", "select 10 as team_id -- same"),
    )
    with pytest.raises(ValueError, match="--model"):
        engine.compare(plan_id)


def test_comparing_builds_nothing_and_leaves_the_tree_alone(project, tmp_path):
    import duckdb

    before = (project / "models" / "team_activity.sql").read_text(encoding="utf-8")
    engine = _engine(project)
    plan_id = _plan(
        engine,
        project,
        ("models/int_counts.sql", _INTERMEDIATE),
        ("models/team_activity.sql", _EXTRACTED),
    )
    engine.compare(plan_id, model="team_activity")
    assert (project / "models" / "team_activity.sql").read_text("utf-8") == before
    assert not (project / "models" / "int_counts.sql").exists()
    con = duckdb.connect(str(tmp_path / "dev.duckdb"), read_only=True)
    tables = {row[0] for row in con.execute("show tables").fetchall()}
    con.close()
    assert "int_counts" not in tables


def test_the_plan_offers_the_comparison_and_runs_nothing(project):
    engine = _engine(project)
    planned = engine.plan(
        "refactor",
        edits=[
            PlanEdit(
                path="models/team_activity.sql",
                new_content=_EQUIVALENT,
                kind=EditKind.MODEL_SQL,
            )
        ],
    )
    offer = planned.data()["refactor_comparison_offer"]
    assert offer["models"] == ["team_activity"]
    assert offer["command"].startswith(f"transform compare {planned.plan_id}")


def test_a_new_model_gets_no_offer(project):
    engine = _engine(project)
    planned = engine.plan(
        "new",
        edits=[
            PlanEdit(
                path="models/brand_new.sql",
                new_content="select 1 as id",
                kind=EditKind.MODEL_SQL,
            )
        ],
    )
    assert "refactor_comparison_offer" not in planned.data()


def test_the_cli_returns_one_envelope_with_no_rows(project, tmp_path, capsys):
    (tmp_path / ".dex").mkdir(exist_ok=True)
    (tmp_path / ".dex" / "config.yml").write_text(
        f"connector: duckdb\ndbt_target: dev\ndbt_project_dir: {project.name}\n"
        f"path: {tmp_path / 'dev.duckdb'}\n",
        encoding="utf-8",
    )
    engine = _engine(project)
    plan_id = _plan(engine, project, ("models/team_activity.sql", _EQUIVALENT))
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
        ]
    )
    out = capsys.readouterr().out
    envelope = json.loads(out)
    assert envelope["status"] == "ok", envelope
    assert envelope["data"]["status"] == "equivalent"
    assert "someone" not in out


@pytest.mark.parametrize(
    ("sql", "found"),
    [
        ("select random() as r", "rand"),
        ("select uuid() as r", "uuid"),
        ("select now() as r", "current_timestamp"),
        ("select current_date as r", "current_date"),
        ("select 1 as a from t tablesample (10 percent)", "sample"),
        ("select 1 as a", None),
    ],
)
def test_nondeterminism_is_read_from_the_statement(sql, found):
    result = nondeterministic_expression(sql, "duckdb")
    if found is None:
        assert result is None
    else:
        assert result is not None
