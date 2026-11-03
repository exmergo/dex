"""Both intentions from one initial project, on real dbt and DuckDB (#858).

Every expected schema and value below is written by hand from the fixture, and
read back with DuckDB directly, so nothing here trusts the fields under test.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from exmergo_dex_core.cli import main

pytestmark = pytest.mark.live_open_adapter

FLAG = "use_region_enrichment"
HAS_FLAG = f"var('{FLAG}')"

STG = (
    "select 1 as order_id, 'n' as region_id, 10 as amount\n"
    "union all select 2, 's', 20\nunion all select 3, 'missing', 30\n"
)
LOOKUP = (
    "select 'n' as region_id, 'North' as region_label\nunion all select 's', 'South'\n"
)
GUARD = "{% if " + HAS_FLAG + " %}"
RPT = """with orders as (select * from {{ ref('stg_orders') }})
GUARD
, regions as (select * from {{ ref('lookup_regions') }})
{% endif %}
select o.order_id, o.amount
GUARD, r.region_label{% endif %}
from orders o
GUARDleft join regions r on o.region_id = r.region_id{% endif %}
""".replace("GUARD", GUARD)
EMPTY = """select o.order_id
GUARD, 'x' as region_label{% endif %}
from {{ ref('stg_orders') }} o
where false
""".replace("GUARD", GUARD)
NESTED = """select order_id, 'base' as source_name from {{ ref('stg_orders') }}
GUARD
  {% if target.name == 'dev' %}
union all
select order_id, 'region' as source_name from {{ ref('stg_orders') }}
  {% endif %}
{% endif %}
""".replace("GUARD", GUARD)
ORIGINAL = {"rpt_orders": RPT, "rpt_empty": EMPTY, "rpt_nested": NESTED}

RETIRE_SQL = {
    "rpt_orders": (
        "with orders as (select * from {{ ref('stg_orders') }})\n"
        "select o.order_id, o.amount\nfrom orders o\n"
    ),
    "rpt_empty": "select o.order_id\nfrom {{ ref('stg_orders') }} o\nwhere false\n",
    "rpt_nested": (
        "select order_id, 'base' as source_name from {{ ref('stg_orders') }}\n"
    ),
}
PERMANENT_SQL = {
    "rpt_orders": (
        "with orders as (select * from {{ ref('stg_orders') }})\n"
        ", regions as (select * from {{ ref('lookup_regions') }})\n"
        "select o.order_id, o.amount, r.region_label\nfrom orders o\n"
        "left join regions r on o.region_id = r.region_id\n"
    ),
    "rpt_empty": (
        "select o.order_id, 'x' as region_label\n"
        "from {{ ref('stg_orders') }} o\nwhere false\n"
    ),
    "rpt_nested": (
        "select order_id, 'base' as source_name from {{ ref('stg_orders') }}\n"
        "union all\n"
        "select order_id, 'region' as source_name from {{ ref('stg_orders') }}\n"
    ),
}
RETIRE_OUTPUTS = {
    "rpt_orders": {"expected": ["order_id", "amount"], "removed": ["region_label"]},
    "rpt_empty": {"expected": ["order_id"], "removed": ["region_label"]},
    "rpt_nested": {"expected": ["order_id", "source_name"]},
}
PERMANENT_OUTPUTS = {
    "rpt_orders": {"expected": ["order_id", "amount", "region_label"]},
    "rpt_empty": {"expected": ["order_id", "region_label"]},
    "rpt_nested": {"expected": ["order_id", "source_name"]},
}
RETIRE = {"behavior": "retire", "retained_value": False, "outputs": RETIRE_OUTPUTS}
PERMANENT = {
    "behavior": "permanent",
    "retained_value": True,
    "outputs": PERMANENT_OUTPUTS,
}


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def repo(tmp_path: Path, capsys) -> Path:
    pytest.importorskip("dbt.adapters.duckdb")
    _write(
        tmp_path / ".dex/config.yml",
        f"connector: duckdb\nduckdb:\n  path: {tmp_path / 'dev.duckdb'}\n"
        "dbt_project_dir: shop\ndbt_target: dev\n",
    )
    shop = tmp_path / "shop"
    _write(
        shop / "dbt_project.yml",
        'name: shop\nversion: "1.0"\nprofile: shop\nmodel-paths: [models]\n'
        "models:\n  +materialized: table\n"
        f"vars:\n  {FLAG}: true\n",
    )
    _write(
        shop / "profiles.yml",
        "shop:\n  target: dev\n  outputs:\n    dev:\n      type: duckdb\n"
        f"      path: {tmp_path / 'dev.duckdb'}\n",
    )
    _write(shop / "models/stg_orders.sql", STG)
    _write(shop / "models/lookup_regions.sql", LOOKUP)
    for name, sql in ORIGINAL.items():
        _write(shop / f"models/{name}.sql", sql)
    # An out-of-scope reader, its default deciding what it does afterwards.
    _write(shop / "packages.yml", "packages:\n  - local: pkgs/tiny\n")
    _write(
        shop / "pkgs/tiny/dbt_project.yml",
        'name: tiny\nversion: "1.0"\nprofile: shop\nmodel-paths: [models]\n',
    )
    _write(
        shop / "pkgs/tiny/models/pkg_reader.sql",
        f"select {{{{ var('{FLAG}', false) }}}} as flag\n",
    )
    _run(tmp_path, capsys, "transform", "deps")
    built = _run(tmp_path, capsys, "transform", "build", "--target", "dev")
    assert built["status"] == "ok", built
    return tmp_path


def _run(repo: Path, capsys, *argv: str) -> dict:
    capsys.readouterr()
    main(["--repo-root", str(repo), *argv])
    return json.loads(capsys.readouterr().out)


def _remove(repo: Path, capsys, sql: dict, contract: dict) -> dict:
    payload = repo / "payload.json"
    payload.write_text(
        json.dumps(
            {
                "edits": [
                    {"path": f"models/{n}.sql", "kind": "model_sql", "content": c}
                    for n, c in sql.items()
                ],
                "retirement": contract,
            }
        ),
        encoding="utf-8",
    )
    return _run(
        repo, capsys, "transform", "remove", "var", FLAG, "--edits-file", str(payload)
    )


def _columns(repo: Path, relation: str) -> list[str]:
    import duckdb

    with duckdb.connect(str(repo / "dev.duckdb"), read_only=True) as con:
        return [r[0] for r in con.execute(f"describe {relation}").fetchall()]


def _rows(repo: Path, sql: str) -> list[tuple]:
    import duckdb

    with duckdb.connect(str(repo / "dev.duckdb"), read_only=True) as con:
        return con.execute(sql).fetchall()


def _records(envelope: dict) -> dict[str, dict]:
    return {r["model"]: r for r in envelope["data"]["retirement"]["models"]}


def _build_verify(repo: Path, capsys, plan_id: str) -> dict:
    return _run(
        repo,
        capsys,
        "transform",
        "build",
        "--target",
        "dev",
        "--verify",
        "--for-plan",
        plan_id,
    )


@pytest.mark.parametrize(
    ("sql", "contract", "columns", "rows"),
    [
        (
            RETIRE_SQL,
            RETIRE,
            {
                "rpt_orders": ["order_id", "amount"],
                "rpt_empty": ["order_id"],
            },
            3,
        ),
        (
            PERMANENT_SQL,
            PERMANENT,
            {
                "rpt_orders": ["order_id", "amount", "region_label"],
                "rpt_empty": ["order_id", "region_label"],
            },
            6,
        ),
    ],
    ids=["retire", "permanent"],
)
def test_each_intention_verifies_from_the_same_project(
    repo: Path, capsys, sql, contract, columns, rows
):
    planned = _remove(repo, capsys, sql, contract)
    assert planned["status"] == "ok", planned
    retirement = planned["data"]["retirement"]
    assert retirement["verdict"] == "satisfied", retirement
    records = _records(planned)
    assert all(r["matches_retained"] for r in records.values())
    assert retirement["package_scope"]["readers"][0]["package"] == "tiny"
    # The branch that went is on the record too, and it differs from the kept one.
    orders = records["rpt_orders"]["branches"]
    assert ("region_label" in orders["other"]) is (contract["retained_value"] is False)
    plan_id = planned["data"]["plan_id"]

    for model in sql:
        compared = _run(repo, capsys, "transform", "compare", plan_id, "--model", model)
        assert compared["data"]["status"] == "equivalent", compared["data"]
        assert compared["data"]["baseline_vars"] == {FLAG: contract["retained_value"]}

    assert _run(repo, capsys, "transform", "apply", plan_id)["status"] == "ok"
    built = _build_verify(repo, capsys, plan_id)
    completion = built["data"]["completion"]
    assert not completion["obligations"]["contradicted"], completion
    records = {r["model"]: r for r in built["data"]["verification"]["retirement"]}
    assert {records[m]["status"] for m in contract["outputs"]} == {"satisfied"}
    for model, expected in columns.items():
        assert _columns(repo, model) == expected
    assert _rows(repo, "select count(*) from rpt_nested") == [(rows,)]
    assert _rows(repo, "select count(*) from rpt_empty") == [(0,)]
    # The flag's spelling is gone from what the project owns.
    project = (repo / "shop/dbt_project.yml").read_text(encoding="utf-8")
    assert FLAG not in project
    # The package stayed byte-identical.
    assert FLAG in (repo / "shop/pkgs/tiny/models/pkg_reader.sql").read_text()

    # Repeating the finished change is a clear no-op.
    again = _remove(repo, capsys, sql, contract)
    assert again["status"] == "ok"
    assert again["data"]["plan_id"] is None
    assert again["data"]["retirement"]["status"] == "already_complete"


def test_the_benchmark_failure_is_caught_at_plan_compare_and_build(repo, capsys):
    """Retire was asked for; the enabled branch was kept and the flag removed.

    The empty model is the one row comparison cannot judge.
    """

    planned = _remove(repo, capsys, PERMANENT_SQL, RETIRE)
    assert planned["status"] == "ok"
    assert planned["data"]["retirement"]["verdict"] == "contradicted"
    records = _records(planned)
    checks = {c["check"] for c in records["rpt_empty"]["checks"]}
    assert {"removed_output_present", "proposal_differs_from_retained_branch"} <= checks
    orders = {c["check"] for c in records["rpt_orders"]["checks"]}
    assert "reads_outside_retained_branch" in orders
    assert any("region_label" in w for w in planned["warnings"])

    plan_id = planned["data"]["plan_id"]
    for model in ("rpt_orders", "rpt_empty"):
        compared = _run(repo, capsys, "transform", "compare", plan_id, "--model", model)
        assert compared["data"]["status"] == "different", compared["data"]
        assert compared["data"]["reason_code"] == "schemas_differ"

    assert _run(repo, capsys, "transform", "apply", plan_id)["status"] == "ok"
    built = _build_verify(repo, capsys, plan_id)
    codes = {
        (f["identifier"], f["code"]) for f in built["data"]["verification"]["findings"]
    }
    assert ("rpt_empty", "retirement_output_present") in codes
    assert _rows(repo, "select count(*) from rpt_empty") == [(0,)]
    contradicted = built["data"]["completion"]["obligations"]["contradicted"]
    assert {"rpt_empty", "rpt_orders"} <= {
        o["model"] for o in contradicted if o["kind"] == "retirement"
    }


def test_the_mirror_failure_is_caught_too(repo, capsys):
    planned = _remove(repo, capsys, RETIRE_SQL, PERMANENT)
    assert planned["data"]["retirement"]["verdict"] == "contradicted"
    checks = {c["check"] for c in _records(planned)["rpt_orders"]["checks"]}
    assert {"expected_output_missing", "dropped_retained_read"} <= checks


def test_a_contract_naming_the_wrong_branch_is_caught(repo, capsys):
    """Correct SQL and outputs, with the default's value stated by habit."""

    planned = _remove(repo, capsys, RETIRE_SQL, {**RETIRE, "retained_value": True})
    assert planned["data"]["retirement"]["verdict"] == "contradicted"
    assert "matches_retained" in _records(planned)["rpt_orders"]
    assert _records(planned)["rpt_orders"]["matches_retained"] is False


def test_a_join_kept_without_its_column_is_caught(repo, capsys):
    kept = {
        **RETIRE_SQL,
        "rpt_orders": (
            "with orders as (select * from {{ ref('stg_orders') }})\n"
            ", regions as (select * from {{ ref('lookup_regions') }})\n"
            "select o.order_id, o.amount\nfrom orders o\n"
            "left join regions r on o.region_id = r.region_id\n"
        ),
    }
    planned = _remove(repo, capsys, kept, RETIRE)
    checks = {c["check"] for c in _records(planned)["rpt_orders"]["checks"]}
    assert "reads_outside_retained_branch" in checks


def test_an_unread_cte_and_an_orphaned_model_are_named(repo, capsys):
    leftover = {
        **RETIRE_SQL,
        "rpt_orders": (
            "with orders as (select * from {{ ref('stg_orders') }})\n"
            ", unused as (select 1 as x)\n"
            "select o.order_id, o.amount\nfrom orders o\n"
        ),
    }
    planned = _remove(repo, capsys, leftover, RETIRE)
    checks = {c["check"] for c in _records(planned)["rpt_orders"]["checks"]}
    assert "unused_cte" in checks
    assert planned["data"]["retirement"]["orphaned_models"] == ["lookup_regions"]


def test_a_removed_column_still_read_downstream_is_named(repo, capsys):
    _write(
        repo / "shop/models/rpt_downstream.sql",
        "select region_label from {{ ref('rpt_orders') }}\n",
    )
    planned = _remove(repo, capsys, RETIRE_SQL, RETIRE)
    checks = {c["check"] for c in _records(planned)["rpt_orders"]["checks"]}
    assert "removed_column_still_read" in checks


def test_a_semantic_or_schema_reference_to_a_removed_column_is_named(repo, capsys):
    _write(
        repo / "shop/models/schema.yml",
        "models:\n  - name: rpt_orders\n    columns:\n      - name: region_label\n",
    )
    planned = _remove(repo, capsys, RETIRE_SQL, RETIRE)
    found = [
        c
        for c in _records(planned)["rpt_orders"]["checks"]
        if c["check"] == "removed_column_still_read"
    ]
    assert found
    assert "models/schema.yml" in found[0]["detail"]
    # The model's own SELECT never counts: that is what `outputs` judges.
    assert "rpt_orders.sql" not in found[0]["detail"]


def test_a_package_parent_reading_the_flag_is_named_rather_than_compared(repo, capsys):
    """Removing the definition moves what the package model compiles to."""

    _write(
        repo / "shop/models/rpt_from_pkg.sql",
        "select flag from {{ ref('pkg_reader') }}\n",
    )
    assert _run(repo, capsys, "transform", "build", "--target", "dev")["status"] == "ok"
    sql = {
        **RETIRE_SQL,
        "rpt_from_pkg": "select flag, 1 as x from {{ ref('pkg_reader') }}\n",
    }
    outputs = {**RETIRE_OUTPUTS, "rpt_from_pkg": {"expected": ["flag", "x"]}}
    planned = _remove(repo, capsys, sql, {**RETIRE, "outputs": outputs})
    compared = _run(
        repo,
        capsys,
        "transform",
        "compare",
        planned["data"]["plan_id"],
        "--model",
        "rpt_from_pkg",
    )
    assert compared["data"]["reason_code"] == "inputs_unsynchronized", compared["data"]
    assert "pkg_reader" in compared["data"]["detail"]


def test_a_stale_plan_is_not_compared_and_a_conflict_is_not_overwritten(repo, capsys):
    planned = _remove(repo, capsys, RETIRE_SQL, RETIRE)
    plan_id = planned["data"]["plan_id"]
    _write(repo / "shop/models/rpt_empty.sql", EMPTY + "-- a human edit\n")
    compared = _run(
        repo, capsys, "transform", "compare", plan_id, "--model", "rpt_orders"
    )
    assert compared["data"]["reason_code"] == "plan_stale"
    applied = _run(repo, capsys, "transform", "apply", plan_id)
    assert applied["status"] == "needs_confirmation"
    # Atomic: the conflict withheld every file.
    assert HAS_FLAG in (repo / "shop/models/rpt_orders.sql").read_text()
    assert FLAG in (repo / "shop/dbt_project.yml").read_text()


def test_a_project_scoped_definition_left_behind_is_named(repo, capsys):
    project = repo / "shop/dbt_project.yml"
    text = project.read_text(encoding="utf-8").replace(
        f"vars:\n  {FLAG}: true\n", f"vars:\n  shop:\n    {FLAG}: true\n"
    )
    project.write_text(text, encoding="utf-8")
    planned = _remove(repo, capsys, RETIRE_SQL, RETIRE)
    assert planned["data"]["retirement"]["project_scoped_definition_left"] is True
    assert any("own `vars:` scope" in w for w in planned["warnings"])


def test_a_build_for_a_plan_document_holds_the_contract_too(repo, capsys):
    planned = _remove(repo, capsys, PERMANENT_SQL, RETIRE)
    plan_id = planned["data"]["plan_id"]
    exported = _run(repo, capsys, "transform", "export", plan_id)["data"]
    document = repo / "plan.json"
    document.write_text(json.dumps(exported.get("plan") or exported), encoding="utf-8")
    assert _run(repo, capsys, "transform", "apply", plan_id)["status"] == "ok"
    built = _run(
        repo,
        capsys,
        "transform",
        "build",
        "--target",
        "dev",
        "--verify",
        "--for-plan-file",
        str(document),
    )
    codes = {f["code"] for f in built["data"]["verification"]["findings"]}
    assert "retirement_output_present" in codes


def test_the_contract_travels_in_the_plan_document(repo, capsys):
    planned = _remove(repo, capsys, RETIRE_SQL, RETIRE)
    plan_id = planned["data"]["plan_id"]
    exported = _run(repo, capsys, "transform", "export", plan_id)["data"]
    document = exported.get("plan") or exported
    assert document["retirement"]["retained_value"] is False

    from exmergo_dex_core.transform.portable import plan_digest

    honest = plan_digest(document)
    swapped = json.loads(json.dumps(document))
    swapped["retirement"]["retained_value"] = True
    assert plan_digest(swapped) != honest
    without = {k: v for k, v in document.items() if k != "retirement"}
    assert plan_digest(without) != honest
