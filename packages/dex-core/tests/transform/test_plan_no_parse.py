"""A transform plan stored without running dbt (#1517).

A host that must not execute repository-controlled code where it authors (Jinja,
macros, project hooks) stores the plan with ``no_parse`` and parses it in a
sandbox of its own. These tests make every subprocess fail, so a future change
that starts dbt on this path fails here rather than on the host, and they check
that each structural gate still refuses what it refused before.
"""

from __future__ import annotations

import importlib
import json
import subprocess
from pathlib import Path

import pytest

from exmergo_dex_core import DexEngine
from exmergo_dex_core.cli import main
from exmergo_dex_core.edits import EditOp
from exmergo_dex_core.transform.plans import EditKind, PlanEdit
from exmergo_dex_core.transform.validate import EditValidationError

MODEL = "select 1 as order_id, 10.0 as amount\n"
SCHEMA = (
    "version: 2\n"
    "models:\n"
    "  - name: stg_orders\n"
    "    columns:\n"
    "      - name: order_id\n"
    "        tests: [unique, not_null]\n"
)
DECLARATION = {
    "model": "stg_orders",
    "description": "One row per order.",
    "grain": ["order_id"],
    "columns": {
        "order_id": {"role": "key"},
        "amount": {
            "role": "measure",
            "aggregation": "sum",
            "additivity": "additive",
            "unit": "currency:USD",
        },
    },
}


@pytest.fixture
def no_dbt(monkeypatch) -> None:
    """Every way the plan path could start dbt now fails the test."""

    def refuse(*args, **_kwargs):
        raise AssertionError(f"a no_parse plan started a subprocess: {args!r}")

    monkeypatch.setattr(subprocess, "run", refuse)
    monkeypatch.setattr(subprocess, "Popen", refuse)
    build = importlib.import_module("exmergo_dex_core.transform.build")
    monkeypatch.setattr(build, "shadow_parse", refuse)
    monkeypatch.setattr(build, "_default_runner", refuse)
    compile_module = importlib.import_module("exmergo_dex_core.transform.compile")
    monkeypatch.setattr(compile_module, "compile_models", refuse)


def _sql(path: str, content: str) -> PlanEdit:
    return PlanEdit(
        path=path, new_content=content, op=EditOp.UPSERT, kind=EditKind.MODEL_SQL
    )


def _yml(path: str, content: str) -> PlanEdit:
    return PlanEdit(
        path=path, new_content=content, op=EditOp.UPSERT, kind=EditKind.SCHEMA_YML
    )


def _plan(repo: Path, edits: list[PlanEdit], **kwargs):
    with DexEngine.from_repo(str(repo), connector="duckdb") as engine:
        return engine.plan("orders", edits=edits, no_parse=True, **kwargs)


def test_a_model_and_its_properties_are_stored_without_dbt(
    dbt_project_dir: Path, no_dbt
):
    result = _plan(
        dbt_project_dir.parent,
        [
            _sql("models/staging/stg_orders.sql", MODEL),
            _yml("models/staging/stg_orders.yml", SCHEMA),
        ],
        declarations=[DECLARATION],
        attribute_rows=False,
    )

    assert result.plan_id
    assert "models/staging/stg_orders.sql" in result.paths
    assert "models/staging/stg_orders.yml" in result.paths
    assert result.diffs
    # The declaration still rendered into the model's YAML and was drafted.
    assert "stg_orders" in (result.declarations or {})
    assert any("dbt parse not run" in w for w in result.warnings)


def test_a_model_with_control_flow_is_refused_rather_than_stored_unchecked(
    dbt_project_dir: Path, no_dbt
):
    with pytest.raises(EditValidationError) as raised:
        _plan(
            dbt_project_dir.parent,
            [
                _sql(
                    "models/staging/stg_flagged.sql",
                    "{% if var('x', true) %}select 1 as id{% else %}"
                    "select 2 as id{% endif %}\n",
                )
            ],
        )

    message = str(raised.value)
    assert "models/staging/stg_flagged.sql" in message
    assert "no_parse" in message
    assert "control flow" in message


def test_a_column_listing_model_is_checked_statically_and_named(
    dbt_project_dir: Path, no_dbt
):
    result = _plan(
        dbt_project_dir.parent,
        [
            _sql(
                "models/staging/stg_all.sql",
                "select {{ dbt_utils.star(ref('stg_customers')) }}\n"
                "from {{ ref('stg_customers') }}\n",
            )
        ],
        attribute_rows=False,
    )

    assert result.plan_id
    assert any(
        "stg_all.sql" in w and "column-listing macro was not expanded" in w
        for w in result.warnings
    )


def test_packages_declared_and_not_installed_are_the_parsing_sides_to_report(
    dbt_project_dir: Path, no_dbt
):
    (dbt_project_dir / "packages.yml").write_text(
        "packages:\n  - package: dbt-labs/dbt_utils\n    version: 1.3.0\n",
        encoding="utf-8",
    )

    result = _plan(
        dbt_project_dir.parent,
        [_yml("models/staging/stg_orders.yml", SCHEMA)],
    )

    assert result.plan_id


def test_an_orphaning_delete_is_still_refused(dbt_project_dir: Path, no_dbt):
    (dbt_project_dir / "models" / "staging" / "stg_reader.sql").write_text(
        "select * from {{ ref('stg_customers') }}\n", encoding="utf-8"
    )

    with pytest.raises(Exception, match="stg_reader"):
        _plan(
            dbt_project_dir.parent,
            [
                PlanEdit(
                    path="models/staging/stg_customers.sql",
                    new_content=None,
                    op=EditOp.DELETE,
                    kind=EditKind.MODEL_SQL,
                )
            ],
        )


def test_a_seed_carrying_personal_data_is_still_refused(dbt_project_dir: Path, no_dbt):
    with pytest.raises(Exception, match="email"):
        _plan(
            dbt_project_dir.parent,
            [
                PlanEdit(
                    path="seeds/contacts.csv",
                    new_content="id,email\n1,someone@example.com\n",
                    op=EditOp.UPSERT,
                    kind=EditKind.SEED_CSV,
                )
            ],
        )


VERSIONED = """\
version: 2
models:
  - name: stg_customers
    latest_version: 1
    columns:
      - name: id
    versions:
      - v: 1
        defined_in: stg_customers
        config:
          alias: stg_customers
      - v: 2
"""


def test_model_versions_are_reported_unchecked_with_no_parse_to_read(
    dbt_project_dir: Path, no_dbt
):
    result = _plan(
        dbt_project_dir.parent,
        [
            _sql("models/staging/stg_customers_v2.sql", "select 1 as id\n"),
            _yml("models/staging/schema.yml", VERSIONED),
        ],
        attribute_rows=False,
    )

    manifest = result.model_versions["stg_customers"]["manifest"]
    assert manifest["status"] == "unchecked"
    assert manifest["reason_code"] == "parse_skipped"


def _cli(repo: Path, body: dict, capsys, *flags: str) -> tuple[int, dict]:
    payload = repo / "payload.json"
    payload.write_text(json.dumps(body), encoding="utf-8")
    rc = main(
        [
            "--repo-root",
            str(repo),
            "transform",
            "plan",
            "orders",
            *flags,
            "--edits-file",
            str(payload),
        ]
    )
    return rc, json.loads(capsys.readouterr().out)


def test_the_cli_flag_stores_the_plan_without_dbt(
    dbt_project_dir: Path, no_dbt, capsys
):
    rc, envelope = _cli(
        dbt_project_dir.parent,
        {
            "edits": [
                {
                    "path": "models/staging/stg_orders.sql",
                    "kind": "model_sql",
                    "content": MODEL,
                }
            ],
            "declarations": [DECLARATION],
        },
        capsys,
        "--no-parse",
        "--no-attribute-rows",
    )

    assert rc == 0, envelope
    assert envelope["data"]["plan_id"]


def test_a_toggle_contract_is_refused_under_no_parse(
    dbt_project_dir: Path, no_dbt, capsys
):
    (dbt_project_dir / "models" / "staging" / "stg_fees.sql").write_text(
        "select 1 as id{% if var('fees', true) %}, 2 as fee{% endif %}\n",
        encoding="utf-8",
    )
    rc, envelope = _cli(
        dbt_project_dir.parent,
        {
            "edits": [
                {
                    "path": "models/staging/stg_orders.sql",
                    "kind": "model_sql",
                    "content": MODEL,
                }
            ],
            "toggle": {
                "variable": "fees",
                "on_value": True,
                "off_value": False,
                "columns": ["fee"],
            },
        },
        capsys,
        "--no-parse",
    )

    assert rc == 1 and envelope["status"] == "error"
    assert "toggle" in envelope["errors"][0]
    assert "--no-parse" in envelope["errors"][0]


def test_without_the_flag_the_parse_gate_still_runs(dbt_project_dir: Path, monkeypatch):
    build = importlib.import_module("exmergo_dex_core.transform.build")
    calls: list[list[str]] = []

    def parses(_project, edits, **_kwargs):
        calls.append([e.path for e in edits])
        return {"available": True, "reason": None, "success": True, "messages": []}

    monkeypatch.setattr(build, "shadow_parse", parses)
    with DexEngine.from_repo(str(dbt_project_dir.parent), connector="duckdb") as engine:
        result = engine.plan(
            "orders",
            edits=[_yml("models/staging/stg_orders.yml", SCHEMA)],
        )

    assert result.plan_id
    assert calls, "the default plan skipped dbt's parse"
