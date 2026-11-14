"""An extraction that empties its intermediate, among inherited findings (#1230).

The fixture the issue specifies, after the asana005 run on 1.17.5: a project
that already carries several high-severity findings on models the change never
touches, and a refactor that extracts an intermediate whose inner join matches
nothing, so it builds empty. The agent applied it uncompared while the
baseline was ready, and the build's new ``join_no_match`` sat among the
inherited ones and was dismissed with them.

What must hold: the introduced finding leads ``data.verification.findings``
and ``data.verification.introduced``, the inherited ones are ``existing``, and
the outcome reads ``built_unverified``, naming both the uncompared rewrite and
the introduced finding, rather than ``validated``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from tests.transform.test_transform_compare import _engine

from exmergo_dex_core.transform.artifact_runtime import readable_artifact_dir
from exmergo_dex_core.transform.finding_attribution import HISTORY
from exmergo_dex_core.transform.plans import EditKind, PlanEdit

pytestmark = pytest.mark.live_open_adapter

_SUMMARY = """
select
    a.account_id,
    coalesce(u.user_count, 0) as user_count
from {{ ref('accounts') }} a
left join (
    select us.account_id, count(*) as user_count
    from {{ ref('users') }} us
    inner join {{ ref('memberships') }} m on m.user_id = us.user_id
    group by us.account_id
) u on u.account_id = a.account_id
"""

#: The trap: the intermediate joins memberships on the account id, a key that
#: shares no value with the user ids, so the inner join keeps nothing.
_EMPTY_INT = """
select us.account_id, count(*) as user_count
from {{ ref('users') }} us
inner join {{ ref('memberships') }} m on m.user_id = us.account_id
group by us.account_id
"""

_EXTRACTED = """
select
    a.account_id,
    coalesce(i.user_count, 0) as user_count
from {{ ref('accounts') }} a
left join {{ ref('int_account_users') }} i on i.account_id = a.account_id
"""

#: An inherited defect: a legacy model whose join matches nothing.
_LEGACY = """
select l.legacy_id, r.label
from {{ ref('legacy_ids') }} l
left join {{ ref('labels') }} r on r.label_id = l.legacy_id
"""


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
    (models / "accounts.sql").write_text(
        "select * from (values (1), (2), (3)) as t(account_id)", encoding="utf-8"
    )
    (models / "users.sql").write_text(
        "select * from (values (101, 1), (102, 1), (103, 2), (104, 3))"
        " as t(user_id, account_id)",
        encoding="utf-8",
    )
    (models / "memberships.sql").write_text(
        "select * from (values (101), (102), (103), (104)) as t(user_id)",
        encoding="utf-8",
    )
    (models / "labels.sql").write_text(
        "select * from (values (900, 'x'), (901, 'y')) as t(label_id, label)",
        encoding="utf-8",
    )
    for index in range(5):
        (models / f"legacy_{index}.sql").write_text(_LEGACY, encoding="utf-8")
    (models / "legacy_ids.sql").write_text(
        "select * from (values (1), (2)) as t(legacy_id)", encoding="utf-8"
    )
    (models / "account_summary.sql").write_text(_SUMMARY, encoding="utf-8")
    return root


def _built(project: Path, **kwargs):
    """One build on its own engine, closed after, so dbt can lock the file."""

    with _engine(project) as engine:
        return engine.build(**kwargs)


def _codes(findings: list[dict]) -> list[tuple[str, str, str]]:
    return [(f["code"], f["identifier"], f.get("attribution")) for f in findings]


def test_an_emptied_intermediate_leads_and_the_build_is_not_validated(project):
    engine = _engine(project)
    # The build before the plan, with --verify: the baseline its findings are
    # judged against. The legacy models' joins already match nothing.
    before = engine.build(target="dev", verify=True)
    engine.close()
    inherited = [
        f for f in before.verification["findings"] if f["code"] == "join_no_match"
    ]
    assert {f["identifier"] for f in inherited} == {f"legacy_{i}" for i in range(5)}
    assert all(f["severity"] == "high" for f in inherited)
    assert (readable_artifact_dir(project) / HISTORY).is_file()

    planned = engine.plan(
        "extract the account user counts into an intermediate",
        edits=[
            PlanEdit(
                path="models/int_account_users.sql",
                new_content=_EMPTY_INT,
                kind=EditKind.MODEL_SQL,
            ),
            PlanEdit(
                path="models/account_summary.sql",
                new_content=_EXTRACTED,
                kind=EditKind.MODEL_SQL,
            ),
        ],
    )
    offer = planned.refactor_comparison_offer
    assert offer is not None and offer["baseline_ready"] is True

    # Applied uncompared, as in the run. The mart's population is not what
    # this fixture is about, so any population change is accepted explicitly.
    applied = _engine(project).apply(
        planned.plan_id, accept_population_change="extraction under test"
    )
    assert applied.written

    built = _built(project, target="dev", verify=True, for_plan=planned.plan_id)
    data = built.data()
    verification = data["verification"]

    # The introduced findings lead, ahead of every inherited one: the empty
    # intermediate's own join, and the mart's join to it, which now matches
    # nothing either (judged against the baseline, which swept the mart clean).
    findings = verification["findings"]
    classes = [f["attribution"] for f in findings]
    assert classes == sorted(
        classes, key=["introduced", "unattributed", "existing"].index
    )
    introduced = {
        (f["code"], f["identifier"], f["attribution_basis"])
        for f in findings
        if f["attribution"] == "introduced"
    }
    assert ("join_no_match", "int_account_users", "new_model") in introduced
    assert ("join_no_match", "account_summary", "baseline") in introduced
    assert findings[0]["attribution"] == "introduced"
    assert {r["identifier"] for r in verification["introduced"]} >= {
        "int_account_users",
        "account_summary",
    }
    for finding in verification["findings"]:
        if finding["identifier"].startswith("legacy_"):
            assert finding["attribution"] == "existing", finding
    attribution = verification["attribution"]
    assert attribution["baseline"]["invocation_id"]
    assert attribution["counts"]["existing"] >= 5

    # The outcome does not read as success, and says why.
    completion = data["completion"]
    assert data["outcome"] == "built_unverified"
    assert completion["outcome"] == "built_unverified"
    assert data["evidence"]["outcome"] == "validated"
    reasons = {(u["model"], u["reason"]) for u in completion["unverified"]}
    assert ("int_account_users", "introduced_high_finding") in reasons
    assert ("account_summary", "comparison_not_run") in reasons
    assert "int_account_users" in {
        r["identifier"] for r in completion["verification"]["introduced"]
    }
    assert completion["verification"]["top"][0]["attribution"] == "introduced"


def test_a_compared_and_correct_extraction_validates(project):
    engine = _engine(project)
    engine.build(target="dev", verify=True)
    engine.close()
    planned = engine.plan(
        "extract the account user counts into an intermediate",
        edits=[
            PlanEdit(
                path="models/int_account_users.sql",
                new_content=_EMPTY_INT.replace(
                    "m.user_id = us.account_id", "m.user_id = us.user_id"
                ),
                kind=EditKind.MODEL_SQL,
            ),
            PlanEdit(
                path="models/account_summary.sql",
                new_content=_EXTRACTED,
                kind=EditKind.MODEL_SQL,
            ),
        ],
    )
    compared = _engine(project).compare(planned.plan_id, model="account_summary")
    assert compared.status == "equivalent", compared.detail
    _engine(project).apply(
        planned.plan_id, accept_population_change="extraction under test"
    )
    built = _built(project, target="dev", verify=True, for_plan=planned.plan_id)
    data = built.data()
    assert data["verification"]["introduced"] == []
    assert data["outcome"] == "validated", data["completion"].get("unverified")
