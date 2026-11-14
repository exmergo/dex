"""From the plan's comparison offer to a recorded verdict (#1049).

#856 built the comparison; the 1.17.1 benchmark found no agent that ran it. The
offer was in the plan payload and nothing downstream asked about it. These tests
cover the handoff around the comparator rather than the comparator itself: what
the offer says about static attribution, the next step it names, the verdict it
records on the plan, what apply and a ``--for-plan`` build report about it, and
the refusal to compare over a parent whose last build failed.

The fixture is the one #1049 specifies: an account/user refactor whose compiled
SQL calls a project macro, with name aggregation separate from user counting and
some users that have no display-name record. Extracting an intermediate with an
inner join to the names loses those users from the count; extracting it with a
left join, counting users and names separately, keeps the downstream result.
"""

from __future__ import annotations

import json
import shlex
from pathlib import Path
from types import SimpleNamespace

import pytest
from tests.transform.test_transform_compare import _engine

from exmergo_dex_core.transform import commands, refactor_compare
from exmergo_dex_core.transform.artifact_runtime import (
    artifact_paths,
    readable_artifact_dir,
)
from exmergo_dex_core.transform.native_semantic import plan_hint
from exmergo_dex_core.transform.plans import EditKind, PlanEdit

pytestmark = pytest.mark.live_open_adapter

_MACRO = "{% macro count_users(column) %}count(distinct {{ column }}){% endmacro %}\n"

_SUMMARY = """
select
    a.account_id,
    coalesce(u.user_count, 0) as user_count,
    coalesce(n.named_users, 0) as named_users
from {{ ref('accounts') }} a
left join (
    select account_id, {{ count_users('user_id') }} as user_count
    from {{ ref('users') }}
    group by account_id
) u on u.account_id = a.account_id
left join (
    select us.account_id, count(*) as named_users
    from {{ ref('users') }} us
    join {{ ref('display_names') }} d on d.user_id = us.user_id
    group by us.account_id
) n on n.account_id = a.account_id
"""

#: The trap: one intermediate that joins names to users with an inner join, so
#: users with no display name drop out of the user count too.
_INNER_INT = """
select
    us.account_id,
    {{ count_users('us.user_id') }} as user_count,
    count(d.user_id) as named_users
from {{ ref('users') }} us
inner join {{ ref('display_names') }} d on d.user_id = us.user_id
group by us.account_id
"""

#: The count-driven extraction: users drive, names are left-joined.
_LEFT_INT = _INNER_INT.replace("inner join", "left join")

_EXTRACTED_SUMMARY = """
select
    a.account_id,
    coalesce(i.user_count, 0) as user_count,
    coalesce(i.named_users, 0) as named_users
from {{ ref('accounts') }} a
left join {{ ref('int_account_users') }} i on i.account_id = a.account_id
"""

#: An intentional behavior change: named users only, which is not a refactor.
_INTENDED = _SUMMARY.replace(
    "coalesce(u.user_count, 0) as user_count",
    "coalesce(n.named_users, 0) as user_count",
)


@pytest.fixture
def accounts(tmp_path: Path) -> Path:
    pytest.importorskip("dbt.adapters.duckdb")
    root = tmp_path / "analytics"
    models = root / "models"
    models.mkdir(parents=True)
    (root / "macros").mkdir()
    (root / "dbt_project.yml").write_text(
        'name: p\nversion: "1.0.0"\nprofile: p\nmodel-paths: ["models"]\n'
        'macro-paths: ["macros"]\n',
        encoding="utf-8",
    )
    (root / "profiles.yml").write_text(
        "p:\n  target: dev\n  outputs:\n    dev:\n      type: duckdb\n"
        f"      path: {tmp_path / 'dev.duckdb'}\n",
        encoding="utf-8",
    )
    (root / "macros" / "count_users.sql").write_text(_MACRO, encoding="utf-8")
    (models / "accounts.sql").write_text(
        "select * from (values (1), (2), (3)) as t(account_id)", encoding="utf-8"
    )
    (models / "users.sql").write_text(
        "select * from (values ('u1', 1), ('u2', 1), ('u3', 2), ('u4', 2))"
        " as t(user_id, account_id)",
        encoding="utf-8",
    )
    (models / "display_names.sql").write_text(
        "select * from (values ('u1', 'Ann'), ('u3', 'Cy')) as t(user_id, name)",
        encoding="utf-8",
    )
    (models / "account_summary.sql").write_text(_SUMMARY, encoding="utf-8")
    _engine(root).build(target="dev")
    return root


def _plan(engine, *edits: tuple[str, str]):
    return engine.plan(
        "extract the account user counts into an intermediate",
        edits=[
            PlanEdit(path=path, new_content=content, kind=EditKind.MODEL_SQL)
            for path, content in edits
        ],
    )


def _extraction(engine, intermediate: str):
    return _plan(
        engine,
        ("models/int_account_users.sql", intermediate),
        ("models/account_summary.sql", _EXTRACTED_SUMMARY),
    )


def test_the_offer_says_static_attribution_could_not_establish_the_refactor(
    accounts,
):
    planned = _extraction(_engine(accounts), _INNER_INT)
    offer = planned.data()["refactor_comparison_offer"]
    assert offer["plan_id"] == planned.plan_id
    assert offer["models"] == ["account_summary"]
    (entry,) = offer["per_model"]
    assert entry["model"] == "account_summary"
    # The rewrite calls a project macro, which only dbt can render.
    assert entry["static_attribution"] == "unattributed"
    assert entry["command"] == (
        f"transform compare {planned.plan_id} --model account_summary"
    )
    assert offer["recommended_before_apply"] is True
    # Still the released fields, so a consumer reading them keeps working.
    assert offer["command"].startswith(f"transform compare {planned.plan_id}")
    # The offer leads the large fields, as it did.
    keys = list(planned.data())
    assert keys.index("refactor_comparison_offer") < keys.index("declarations")


def test_the_next_step_names_the_comparison_before_apply(accounts):
    planned = _extraction(_engine(accounts), _INNER_INT)
    hint = plan_hint(planned)["next"]
    compare_at = hint.index(f"transform compare {planned.plan_id}")
    assert compare_at < hint.index(f"transform apply {planned.plan_id}")


def test_the_inner_join_extraction_is_different_and_recorded(accounts):
    engine = _engine(accounts)
    planned = _extraction(engine, _INNER_INT)
    result = engine.compare(planned.plan_id, model="account_summary")
    assert result.status == "different", result.detail
    assert result.reason_code == "rows_differ"

    stored = engine.store.load_plan(planned.plan_id)
    assert stored.refactor_models == {"account_summary": "unattributed"}
    recorded = stored.comparisons["account_summary"]
    assert recorded["status"] == "different"
    assert recorded["reason_code"] == "rows_differ"
    assert recorded["compared_at"] == result.compared_at

    # Apply still applies: an intentional change needs no equivalence, and only
    # the caller knows which kind this was. It says what the record holds.
    applied = engine.apply(planned.plan_id)
    assert applied.written
    assert applied.refactor_comparison == [
        {
            "model": "account_summary",
            "static_attribution": "unattributed",
            "baseline_ready": True,
            "status": "different",
            "reason_code": "rows_differ",
            "compared_at": result.compared_at,
            "inputs_built_by": recorded["inputs_built_by"],
        }
    ]
    # The fixture's own build made the inputs the comparison read (#1114).
    assert recorded["inputs_built_by"]["cold"] is False
    assert recorded["inputs_built_by"]["invocation_id"]
    assert any("did not establish the results were kept" in n for n in applied.notes)
    assert not any("comparison" in w for w in applied.warnings)


def test_the_left_join_extraction_is_equivalent_and_apply_says_so(accounts):
    engine = _engine(accounts)
    planned = _extraction(engine, _LEFT_INT)
    result = engine.compare(planned.plan_id, model="account_summary")
    assert result.status == "equivalent", result.detail
    assert result.input_scope
    applied = engine.apply(planned.plan_id)
    assert applied.refactor_comparison[0]["status"] == "equivalent"
    assert applied.notes == []


def test_an_intentional_change_applies_uncompared_with_a_note(accounts):
    engine = _engine(accounts)
    planned = _plan(engine, ("models/account_summary.sql", _INTENDED))
    assert planned.refactor_comparison_offer is not None
    applied = engine.apply(planned.plan_id)
    assert applied.written
    assert applied.refactor_comparison[0]["status"] == "not_compared"
    (note,) = applied.notes
    assert "account_summary" in note
    assert "unverified" in note


def test_a_parent_whose_last_build_failed_blocks_the_baseline(accounts):
    """The parent's dev relation predates its current SQL: both sides would read
    it, and the verdict would hold for inputs the project no longer produces."""

    engine = _engine(accounts)
    planned = _extraction(engine, _LEFT_INT)
    run_results = artifact_paths(readable_artifact_dir(accounts)).run_results
    document = json.loads(run_results.read_text(encoding="utf-8"))
    for row in document["results"]:
        if row["unique_id"] == "model.p.users":
            row["status"] = "error"
    run_results.write_text(json.dumps(document), encoding="utf-8")

    result = engine.compare(planned.plan_id, model="account_summary")
    assert result.status == "not_checked"
    assert result.reason_code == "inputs_unsynchronized"
    assert "users (error)" in result.detail
    assert "transform build" in result.detail
    # Recorded, so apply reports that equivalence was not established.
    stored = engine.store.load_plan(planned.plan_id)
    assert stored.comparisons["account_summary"]["status"] == "not_checked"


def test_a_pending_or_applied_verdict_is_not_recorded(accounts):
    engine = _engine(accounts)
    planned = _extraction(engine, _LEFT_INT)
    engine.apply(planned.plan_id)
    assert engine.compare(planned.plan_id).reason_code == "plan_applied"
    assert engine.store.load_plan(planned.plan_id).comparisons == {}


def test_a_for_plan_build_carries_the_recorded_verdict(accounts):
    engine = _engine(accounts)
    planned = _extraction(engine, _LEFT_INT)
    engine.compare(planned.plan_id, model="account_summary")
    engine.apply(planned.plan_id)
    built = _engine(accounts).build(target="dev", for_plan=planned.plan_id)
    completion = built.data()["completion"]
    assert completion["change"]["plan_id"] == planned.plan_id
    assert completion["deliverables"]["coverage"] == "known"
    assert sorted(completion["deliverables"]["built"]["names"]) == [
        "account_summary",
        "int_account_users",
    ]
    assert completion["refactor_comparison"] == [
        {
            "model": "account_summary",
            "status": "equivalent",
            "static_attribution": "unattributed",
            "reason_code": "equivalent_on_compared_inputs",
            "compared_at": engine.store.load_plan(planned.plan_id).comparisons[
                "account_summary"
            ]["compared_at"],
            "inputs_built_by": engine.store.load_plan(planned.plan_id).comparisons[
                "account_summary"
            ]["inputs_built_by"],
            "baseline_ready": True,
        }
    ]


def test_a_downstream_comparison_counts_for_the_plan(accounts):
    """#1049 compares the downstream contract: a plan that rewrites only an
    intermediate, compared at the mart, was compared."""

    from exmergo_dex_core.transform.commands import (
        _comparison_notes,
        refactor_comparison_states,
    )

    engine = _engine(accounts)
    planned = _extraction(engine, _LEFT_INT)
    stored = engine.store.load_plan(planned.plan_id)
    stored.refactor_models = {"int_account_users": "unattributed"}
    stored.comparisons = {
        "account_summary": {"status": "equivalent", "reason_code": "x"}
    }
    states = refactor_comparison_states(stored)
    assert [s["model"] for s in states] == ["account_summary", "int_account_users"]
    assert _comparison_notes(states) == []


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ([], "none"),
        ([{"kind": "unattributable", "attributed": False}], "unattributed"),
        # Named and not counted: opt-in on a billed connector.
        ([{"kind": "where_added", "attributed": False}], "unmeasured"),
        ([{"kind": "where_added", "attributed": True, "rows": None}], "unmeasured"),
        (
            [{"kind": "where_added", "attributed": True, "rows": 3, "delta": 0}],
            "attributed",
        ),
    ],
)
def test_static_attribution_tells_unisolable_from_unmeasured(changes, expected):
    from exmergo_dex_core.transform.commands import _static_attribution

    assert _static_attribution([{"changes": changes}]) == expected


def test_blocked_parents_match_a_versioned_model_by_its_file_stem(dbt_project_dir):
    """A versioned model shares its name across versions; a plan edit and
    `--model` call it by its file stem."""

    from tests.dbt_artifacts import dex_target

    from exmergo_dex_core.transform.refactor_compare import _blocked_parents

    target = dex_target(dbt_project_dir)
    target.mkdir(parents=True, exist_ok=True)
    nodes = {
        "model.p.dim_customers.v2": {
            "name": "dim_customers",
            "version": 2,
            "original_file_path": "models/dim_customers_v2.sql",
            "depends_on": {"nodes": ["model.p.stg_customers", "model.p.int_rewritten"]},
        },
        "model.p.stg_customers": {"name": "stg_customers", "depends_on": {"nodes": []}},
        "model.p.int_rewritten": {
            "name": "int_rewritten",
            "original_file_path": "models/int_rewritten.sql",
            "depends_on": {"nodes": []},
        },
    }
    (target / "manifest.json").write_text(json.dumps({"nodes": nodes}))
    (target / "run_results.json").write_text(
        json.dumps(
            {
                "results": [
                    {"unique_id": "model.p.stg_customers", "status": "error"},
                    {"unique_id": "model.p.int_rewritten", "status": "error"},
                ]
            }
        )
    )
    # The plan rewrites int_rewritten, so it is inlined and not judged.
    assert _blocked_parents(dbt_project_dir, "dim_customers_v2", {"int_rewritten"}) == [
        ("stg_customers", "error", None)
    ]


def test_the_offered_command_quotes_a_model_name_the_shell_would_read(monkeypatch):
    """The stem comes from a file name in the project, and the command is pasted."""

    monkeypatch.setattr(
        commands, "_record_refactor_models", lambda engine, plan_id, per_model: None
    )
    monkeypatch.setattr(
        refactor_compare, "edited_existing_models", lambda edits, project: ["a;touch b"]
    )
    result = SimpleNamespace(plan_id="p1", row_attribution=[])
    engine = SimpleNamespace(project_dir=Path)
    commands._offer_refactor_comparison(engine, result, [])
    offer = result.refactor_comparison_offer
    assert offer["per_model"][0]["command"] == (
        "transform compare p1 --model 'a;touch b'"
    )
    assert "--model 'a;touch b' " in offer["command"]
    assert shlex.split(offer["per_model"][0]["command"])[-1] == "a;touch b"


# --- #1114: a baseline repaired by a cold build, and an offer that knows ---


def _artifacts(
    directory: Path, manifest: dict, results: list, when: str, which: str = "build"
) -> None:
    """Write a manifest and the run results of one build into ``directory``."""

    directory.mkdir(parents=True, exist_ok=True)
    (directory / "manifest.json").write_text(json.dumps(manifest))
    (directory / "run_results.json").write_text(
        json.dumps(
            {
                "metadata": {"generated_at": when, "invocation_id": f"inv-{when}"},
                "args": {"which": which},
                "results": results,
            }
        )
    )


_PACKAGE_MANIFEST = {
    "metadata": {"project_name": "p"},
    "nodes": {
        "model.p.summary": {
            "name": "summary",
            "package_name": "p",
            "depends_on": {"nodes": ["model.roster.members"]},
        },
        "model.roster.members": {
            "name": "members",
            "package_name": "roster",
            "depends_on": {"nodes": []},
        },
    },
}

#: The cold build's graph: the project's override of `members`, the package's
#: own model disabled and so absent.
_OVERRIDE_MANIFEST = {
    "metadata": {"project_name": "p"},
    "nodes": {
        "model.p.summary": {
            "name": "summary",
            "package_name": "p",
            "depends_on": {"nodes": ["model.p.members"]},
        },
        "model.p.members": {
            "name": "members",
            "package_name": "p",
            "depends_on": {"nodes": []},
        },
    },
}


def test_a_package_parent_that_failed_is_blocked_and_named_with_its_package(
    dbt_project_dir,
):
    """A package's failed model is blocked and named with its package, and the
    advice names the cold build."""

    from tests.dbt_artifacts import dex_target

    from exmergo_dex_core.transform.refactor_compare import (
        _blocked_parents,
        repair_first,
    )

    _artifacts(
        dex_target(dbt_project_dir),
        _PACKAGE_MANIFEST,
        [
            {"unique_id": "model.roster.members", "status": "error"},
            {"unique_id": "model.p.summary", "status": "skipped"},
        ],
        "2026-11-05T09:00:00Z",
    )
    blocked = _blocked_parents(dbt_project_dir, "summary", set())
    assert blocked == [("members", "error", "roster")]
    advice = repair_first(blocked)
    assert "its own plan" in advice
    assert "--cold-deps" in advice
    assert "roster" in advice
    assert "dbt_packages/" in advice


def test_a_cold_build_that_repaired_the_parent_unblocks_the_comparison(
    dbt_project_dir,
):
    """The cold build writes `cold/`; before #1114 compare read only the
    ordinary build's results and kept naming the failure it repaired."""

    from tests.dbt_artifacts import dex_target

    from exmergo_dex_core.transform.refactor_compare import (
        _blocked_parents,
        inputs_built_by,
    )

    target = dex_target(dbt_project_dir)
    _artifacts(
        target,
        _PACKAGE_MANIFEST,
        [{"unique_id": "model.roster.members", "status": "error"}],
        "2026-11-05T09:00:00Z",
    )
    _artifacts(
        target / "cold",
        _OVERRIDE_MANIFEST,
        [
            {"unique_id": "model.p.members", "status": "success"},
            {"unique_id": "model.p.summary", "status": "success"},
        ],
        "2026-11-05T09:05:00Z",
    )
    assert _blocked_parents(dbt_project_dir, "summary", set()) == []
    assert inputs_built_by(dbt_project_dir, "summary") == {
        "invocation_id": "inv-2026-11-05T09:05:00Z",
        "generated_at": "2026-11-05T09:05:00Z",
        "cold": True,
    }


def test_a_later_ordinary_failure_blocks_again(dbt_project_dir):
    """An ordinary build newer than the cold one wins, so its failure blocks again."""

    from tests.dbt_artifacts import dex_target

    from exmergo_dex_core.transform.refactor_compare import _blocked_parents

    target = dex_target(dbt_project_dir)
    _artifacts(
        target / "cold",
        _OVERRIDE_MANIFEST,
        [{"unique_id": "model.p.members", "status": "success"}],
        "2026-11-05T09:00:00Z",
    )
    _artifacts(
        target,
        _OVERRIDE_MANIFEST,
        [{"unique_id": "model.p.members", "status": "error"}],
        "2026-11-05T09:05:00Z",
    )
    assert _blocked_parents(dbt_project_dir, "summary", set()) == [
        ("members", "error", None)
    ]


@pytest.mark.parametrize("status", ["partial success", "no-op", "unknown"])
def test_a_parent_on_an_unrecognized_status_blocks_the_baseline(
    dbt_project_dir, status
):
    """#1105: only `success` rebuilt a parent's dev relation. A status dex does
    not recognize leaves it as an earlier run wrote it, as `skipped` does."""

    from tests.dbt_artifacts import dex_target

    from exmergo_dex_core.transform.refactor_compare import _blocked_parents

    _artifacts(
        dex_target(dbt_project_dir),
        _OVERRIDE_MANIFEST,
        [
            {"unique_id": "model.p.members", "status": status},
            {"unique_id": "model.p.summary", "status": "success"},
        ],
        "2026-11-05T09:00:00Z",
    )
    assert _blocked_parents(dbt_project_dir, "summary", set()) == [
        ("members", status, None)
    ]


def test_a_compile_is_not_a_rebuild(dbt_project_dir):
    """A build is priced with a compile, which writes `success` for every node
    it compiled; that says nothing about the relation."""

    from tests.dbt_artifacts import dex_target

    from exmergo_dex_core.transform.refactor_compare import _blocked_parents

    target = dex_target(dbt_project_dir)
    _artifacts(
        target,
        _PACKAGE_MANIFEST,
        [{"unique_id": "model.roster.members", "status": "error"}],
        "2026-11-05T09:00:00Z",
    )
    _artifacts(
        target / "cold",
        _PACKAGE_MANIFEST,
        [{"unique_id": "model.roster.members", "status": "success"}],
        "2026-11-05T09:05:00Z",
        which="compile",
    )
    assert _blocked_parents(dbt_project_dir, "summary", set()) == [
        ("members", "error", "roster")
    ]


def _fail(project: Path, unique_id: str) -> None:
    """Mark ``unique_id`` as errored in the last build's run results."""

    run_results = artifact_paths(readable_artifact_dir(project)).run_results
    document = json.loads(run_results.read_text(encoding="utf-8"))
    for row in document["results"]:
        if row["unique_id"] == unique_id:
            row["status"] = "error"
    run_results.write_text(json.dumps(document), encoding="utf-8")


def test_the_offer_says_the_baseline_does_not_build_and_how_to_repair_it(accounts):
    """The offer reports the blocked input and leads ``data.next`` with the repair."""

    engine = _engine(accounts)
    _fail(accounts, "model.p.users")
    planned = _extraction(engine, _LEFT_INT)
    offer = planned.data()["refactor_comparison_offer"]
    assert offer["baseline_ready"] is False
    (entry,) = offer["per_model"]
    assert entry["blocked_inputs"] == [
        {"model": "users", "status": "error", "owner": "project"}
    ]
    assert "its own plan" in offer["why"]
    assert "unapplied" in offer["why"]
    assert "mixed_with_repair" not in offer
    hint = plan_hint(planned)["next"]
    assert hint.startswith("the baseline does not build (users)")
    # The comparison is still named, after the repair.
    assert f"transform compare {planned.plan_id}" in hint


def test_a_ready_baseline_says_so(accounts):
    """With nothing blocked the offer says the baseline is ready and names no input."""

    planned = _extraction(_engine(accounts), _LEFT_INT)
    offer = planned.data()["refactor_comparison_offer"]
    assert offer["baseline_ready"] is True
    assert "blocked_inputs" not in offer["per_model"][0]


def test_a_repair_bundled_with_the_refactor_is_named_and_warned(accounts):
    """The 1.17.3 failure: one plan carrying the repair and the extraction has
    nothing built to compare against, ever."""

    engine = _engine(accounts)
    _fail(accounts, "model.p.users")
    planned = _plan(
        engine,
        (
            "models/users.sql",
            "select * from (values ('u1', 1), ('u2', 1), ('u3', 2), ('u4', 2))"
            " as t(user_id, account_id)\n",
        ),
        ("models/int_account_users.sql", _LEFT_INT),
        ("models/account_summary.sql", _EXTRACTED_SUMMARY),
    )
    offer = planned.data()["refactor_comparison_offer"]
    assert offer["mixed_with_repair"] == ["users"]
    (warning,) = [w for w in planned.warnings if "both repairs" in w]
    assert "its own plan" in warning
    assert plan_hint(planned)["next"].startswith("this plan carries a repair of users")


def test_a_comparison_after_the_repair_records_what_built_its_inputs(accounts):
    """Blocked, repaired and built: the comparison is equivalent and records the
    build it read."""

    engine = _engine(accounts)
    planned = _extraction(engine, _LEFT_INT)
    _fail(accounts, "model.p.users")
    blocked = engine.compare(planned.plan_id, model="account_summary")
    assert blocked.reason_code == "inputs_unsynchronized"
    assert "its own plan" in blocked.detail
    assert "unapplied" in blocked.detail
    # The repair, built: the comparison now reads it and says so.
    _engine(accounts).build(target="dev")
    result = _engine(accounts).compare(planned.plan_id, model="account_summary")
    assert result.status == "equivalent", result.detail
    built = result.inputs_built_by
    assert built["cold"] is False and built["invocation_id"]
    recorded = engine.store.load_plan(planned.plan_id).comparisons["account_summary"]
    assert recorded["inputs_built_by"] == built
    assert result.data()["inputs_built_by"] == built
