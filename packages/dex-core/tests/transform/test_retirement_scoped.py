"""A scoped retirement on real dbt and DuckDB (#1086).

The project of ``test_retirement_build`` plus two macros that have nothing to do
with the flag: one the column scan cannot parse, and one whose var read names
its variable at run time. Complete discovery cannot be promised there, so the
full retirement refuses; a contract scoped to its authored outputs keeps every
output check and leaves discovery unchecked through the build.

Expected schemas are written by hand from the fixture and read back with DuckDB
directly.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from .test_retirement_build import (
    FLAG,
    PERMANENT_SQL,
    RETIRE,
    RETIRE_SQL,
    _build_verify,
    _columns,
    _records,
    _rows,
    _run,
    _write,
)
from .test_retirement_build import repo as repo

pytestmark = pytest.mark.live_open_adapter

UNIONED = (
    "{% macro unioned(names) %}\n"
    "  {%- for name in names %} union all select * from {{ ref(name) }}"
    "{% endfor -%}\n{% endmacro %}\n"
)
ENABLED = (
    "{% macro enabled(names) %}\n"
    "  {%- for name in names -%}\n"
    "    {%- if var('use_' ~ name, true) %} union all select * from "
    "{{ ref(name) }} {% endif -%}\n"
    "  {%- endfor -%}\n{% endmacro %}\n"
)
SCOPED = {**RETIRE, "scope": "authored"}


@pytest.fixture
def jinja_repo(repo: Path) -> Path:
    _write(repo / "shop/macros/unioned.sql", UNIONED)
    _write(repo / "shop/macros/enabled.sql", ENABLED)
    return repo


def _payload(repo: Path, sql: dict, contract: dict, extra: list | None = None) -> str:
    payload = repo / "payload.json"
    payload.write_text(
        json.dumps(
            {
                "edits": [
                    {"path": f"models/{n}.sql", "kind": "model_sql", "content": c}
                    for n, c in sql.items()
                ]
                + list(extra or []),
                "retirement": contract,
            }
        ),
        encoding="utf-8",
    )
    return str(payload)


def _remove(repo: Path, capsys, sql: dict, contract: dict) -> dict:
    return _run(
        repo,
        capsys,
        "transform",
        "remove",
        "var",
        FLAG,
        "--edits-file",
        _payload(repo, sql, contract),
    )


def _verification(built: dict) -> dict[str, dict]:
    return {r["model"]: r for r in built["data"]["verification"]["retirement"]}


def test_the_full_retirement_refuses_and_names_the_scoped_route(jinja_repo, capsys):
    refused = _remove(jinja_repo, capsys, RETIRE_SQL, RETIRE)
    assert refused["status"] == "error"
    message = " ".join(map(str, refused["errors"]))
    assert "macros/unioned.sql" in message
    assert '"scope": "authored"' in message


def test_a_correct_scoped_retirement_is_checked_end_to_end(jinja_repo, capsys):
    planned = _remove(jinja_repo, capsys, RETIRE_SQL, SCOPED)
    assert planned["status"] == "ok", planned
    retirement = planned["data"]["retirement"]
    # The authored outputs are judged and pass ...
    assert retirement["verdict"] == "satisfied", retirement
    # ... and global discovery is reported apart, never as complete.
    discovery = retirement["discovery"]
    assert discovery["status"] == "unchecked"
    assert discovery["reason_code"] == "discovery_incomplete"
    assert any(x.startswith("macros/unioned.sql") for x in discovery["limits"])
    assert discovery["dynamic_reads"] == ["macros/enabled.sql:3"]
    assert retirement["contract"]["scope"] == "authored"
    assert "scoped to the outputs it names" in planned["warnings"][0]
    # The package reader is out of scope: reported, never edited.
    assert retirement["package_scope"]["readers"][0]["package"] == "tiny"
    plan_id = planned["data"]["plan_id"]

    compared = _run(
        jinja_repo, capsys, "transform", "compare", plan_id, "--model", "rpt_orders"
    )
    assert compared["data"]["status"] == "equivalent", compared["data"]

    assert _run(jinja_repo, capsys, "transform", "apply", plan_id)["status"] == "ok"
    built = _build_verify(jinja_repo, capsys, plan_id)
    records = _verification(built)
    assert {records[m]["status"] for m in SCOPED["outputs"]} == {"satisfied"}
    # The empty table is judged by its columns.
    assert _rows(jinja_repo, "select count(*) from rpt_empty") == [(0,)]
    assert _columns(jinja_repo, "rpt_empty") == ["order_id"]
    assert records[FLAG] == {
        "model": FLAG,
        "check": "reference_discovery",
        "status": "unchecked",
        "reason_code": "discovery_incomplete",
    }
    obligations = built["data"]["completion"]["obligations"]
    assert not obligations["contradicted"], obligations
    assert {"model": FLAG, "kind": "retirement"} in [
        {"model": o["model"], "kind": o["kind"]} for o in obligations["unchecked"]
    ]
    assert FLAG in (jinja_repo / "shop/pkgs/tiny/models/pkg_reader.sql").read_text()

    # Repeating the finished scoped change is the same no-op as a full one,
    # although the dynamic read is still there.
    again = _remove(jinja_repo, capsys, RETIRE_SQL, SCOPED)
    assert again["status"] == "ok", again
    assert again["data"]["plan_id"] is None
    assert again["data"]["retirement"]["status"] == "already_complete"


def test_a_retired_column_that_survives_is_caught(jinja_repo, capsys):
    planned = _remove(jinja_repo, capsys, PERMANENT_SQL, SCOPED)
    assert planned["data"]["retirement"]["verdict"] == "contradicted"
    checks = {c["check"] for c in _records(planned)["rpt_empty"]["checks"]}
    assert "removed_output_present" in checks
    plan_id = planned["data"]["plan_id"]
    assert _run(jinja_repo, capsys, "transform", "apply", plan_id)["status"] == "ok"
    built = _build_verify(jinja_repo, capsys, plan_id)
    codes = {
        (f["identifier"], f["code"]) for f in built["data"]["verification"]["findings"]
    }
    assert ("rpt_empty", "retirement_output_present") in codes
    assert ("rpt_orders", "retirement_output_present") in codes


def test_a_retained_column_that_disappears_is_caught(jinja_repo, capsys):
    dropped = {
        **RETIRE_SQL,
        "rpt_orders": (
            "with orders as (select * from {{ ref('stg_orders') }})\n"
            "select o.order_id\nfrom orders o\n"
        ),
    }
    planned = _remove(jinja_repo, capsys, dropped, SCOPED)
    checks = {c["check"] for c in _records(planned)["rpt_orders"]["checks"]}
    assert "expected_output_missing" in checks
    plan_id = planned["data"]["plan_id"]
    assert _run(jinja_repo, capsys, "transform", "apply", plan_id)["status"] == "ok"
    built = _build_verify(jinja_repo, capsys, plan_id)
    codes = {
        (f["identifier"], f["code"]) for f in built["data"]["verification"]["findings"]
    }
    assert ("rpt_orders", "retirement_output_missing") in codes


def test_a_skipped_model_is_not_judged_by_its_stale_relation(jinja_repo, capsys):
    """The old ``rpt_orders`` still has the retired column; it proves nothing."""

    assert "region_label" in _columns(jinja_repo, "rpt_orders")
    broken = {
        **RETIRE_SQL,
        "stg_broken": "select cast('x' as integer) as order_id, 1 as amount\n",
        "rpt_orders": "select order_id, amount from {{ ref('stg_broken') }}\n",
    }
    contract = {
        **SCOPED,
        "outputs": {
            **SCOPED["outputs"],
            "stg_broken": {"expected": ["order_id", "amount"]},
        },
    }
    planned = _remove(jinja_repo, capsys, broken, contract)
    assert planned["status"] == "ok", planned
    plan_id = planned["data"]["plan_id"]
    assert _run(jinja_repo, capsys, "transform", "apply", plan_id)["status"] == "ok"
    built = _build_verify(jinja_repo, capsys, plan_id)
    records = _verification(built)
    assert records["rpt_orders"]["status"] == "unchecked"
    assert records["rpt_orders"]["reason_code"] == "not_built"
    assert "region_label" in _columns(jinja_repo, "rpt_orders")
    codes = {
        (f["identifier"], f["code"])
        for f in (built["data"].get("verification") or {}).get("findings") or []
    }
    assert ("rpt_orders", "retirement_output_present") not in codes
    assert records[FLAG]["status"] == "unchecked"


def test_scoped_contracts_are_refused_outside_their_bounds(repo, capsys):
    # Nothing here stops the full check, so a scoped contract is refused.
    refused = _remove(repo, capsys, RETIRE_SQL, SCOPED)
    assert refused["status"] == "error"
    assert "discovery is complete" in " ".join(map(str, refused["errors"]))


def test_a_scoped_plan_document_carries_the_scope_to_the_build(jinja_repo, capsys):
    planned = _remove(jinja_repo, capsys, RETIRE_SQL, SCOPED)
    plan_id = planned["data"]["plan_id"]
    exported = _run(jinja_repo, capsys, "transform", "export", plan_id)["data"]
    document = exported.get("plan") or exported
    assert document["retirement"]["scope"] == "authored"
    assert document["retirement"]["discovery"]["status"] == "unchecked"
    path = jinja_repo / "plan.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    assert _run(jinja_repo, capsys, "transform", "apply", plan_id)["status"] == "ok"
    built = _run(
        jinja_repo,
        capsys,
        "transform",
        "build",
        "--target",
        "dev",
        "--verify",
        "--for-plan-file",
        str(path),
    )
    records = _verification(built)
    assert records[FLAG]["check"] == "reference_discovery"
    assert records["rpt_orders"]["status"] == "satisfied"
