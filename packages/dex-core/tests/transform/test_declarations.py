"""Declarations: what a model means, rendered into its existing YAML entry (#491)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from exmergo_dex_core.cli import main
from exmergo_dex_core.dbt_project import DbtProjectView, SourceFile
from exmergo_dex_core.transform.declarations import (
    DeclarationError,
    apply_declarations,
    decisions,
    parse_declarations,
    render,
    schema_path,
)
from exmergo_dex_core.transform.plans import EditKind, PlanEdit

ORDERS_SQL = (
    "select id as order_id, customer_id, status, total as order_total\n"
    "from {{ ref('stg_orders') }}\n"
)

FULL = {
    "model": "fct_orders",
    "description": "One row per order placed through the web store.",
    "grain": ["order_id"],
    "population": {
        "rule": "Completed and shipped orders; cancelled and returned are excluded.",
        "filters": [{"column": "status", "excludes": ["cancelled", "returned"]}],
    },
    "columns": {
        "order_id": {"role": "key", "description": "The order."},
        "customer_id": {
            "role": "foreign_key",
            "references": "dim_customers.customer_id",
            "null_rule": "never",
        },
        "status": {"role": "dimension"},
        "order_total": {
            "role": "measure",
            "aggregation": "sum",
            "additivity": "additive",
            "unit": "currency:USD",
        },
    },
    "assumptions": [
        {
            "decision": "Which order statuses count as revenue",
            "chosen": "completed and shipped",
            "evidence": "precedent",
            "detail": "every model reading stg_orders.status excludes them",
        }
    ],
}


def _view(files: dict[str, str]) -> DbtProjectView:
    return DbtProjectView(
        root=".",
        project_name="p",
        profile_name="p",
        files={
            path: SourceFile(path=path, content=content, sha256="x")
            for path, content in files.items()
        },
    )


def _project(schema: str | None = None) -> dict[str, str]:
    files = {
        "models/marts/fct_orders.sql": ORDERS_SQL,
        "models/marts/dim_customers.sql": "select 1 as customer_id, 'x' as name\n",
    }
    if schema is not None:
        files["models/marts/_marts.yml"] = schema
    return files


def _entry(content: str, model: str = "fct_orders") -> dict:
    parsed = yaml.safe_load(content)
    return next(m for m in parsed["models"] if m["name"] == model)


# --- the payload ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("entry", "expected"),
    [
        ({"model": "m", "columns": {"a": {"role": "metric"}}}, "columns.a.role"),
        (
            {"model": "m", "columns": {"a": {"additivity": "mostly"}}},
            "columns.a.additivity",
        ),
        (
            {"model": "m", "assumptions": [{"evidence": "vibes"}]},
            "assumptions.0.evidence",
        ),
        ({"model": "m", "owner": "me"}, "owner is not a declaration field"),
        ({"description": "no model"}, "model is required"),
    ],
)
def test_a_malformed_declaration_is_refused_naming_the_field(entry, expected):
    with pytest.raises(DeclarationError, match="declarations\\[0\\]") as caught:
        parse_declarations([entry])

    assert expected in str(caught.value)


def test_a_model_declared_twice_in_one_payload_is_refused():
    with pytest.raises(DeclarationError, match="declared twice"):
        parse_declarations([{"model": "m"}, {"model": "m"}])


# --- rendering ------------------------------------------------------------------


HAND_WRITTEN = """version: 2

models:
  # orders, owned by the web team
  - name: fct_orders
    config:
      materialized: table
      meta:
        contains_pii: true
    columns:
      - name: order_id
        tests: [not_null]   # keep me

  - name: dim_customers
    description: untouched
"""


def test_a_declaration_lands_in_the_existing_entry_and_nothing_else_moves():
    [declaration] = parse_declarations([FULL])

    out = render(HAND_WRITTEN, declaration)
    entry = _entry(out)

    # Bytes dex does not own survive: the comments, the other model, the PII
    # stamp and the column's own test list under its own key.
    assert "  # orders, owned by the web team\n" in out
    assert "tests: [not_null, unique]   # keep me" in out
    assert _entry(out, "dim_customers") == {
        "name": "dim_customers",
        "description": "untouched",
    }
    assert entry["config"]["materialized"] == "table"
    assert entry["config"]["meta"]["contains_pii"] is True
    # Only one entry for the model, which dbt would otherwise refuse.
    assert [m["name"] for m in yaml.safe_load(out)["models"]].count("fct_orders") == 1

    dex = entry["config"]["meta"]["dex"]
    assert dex["grain"] == ["order_id"]
    assert dex["population"]["filters"] == [
        {"column": "status", "excludes": ["cancelled", "returned"]}
    ]
    assert dex["assumptions"][0]["evidence"] == "precedent"
    assert entry["description"] == FULL["description"]

    columns = {c["name"]: c for c in entry["columns"]}
    assert columns["order_id"]["description"] == "The order."
    assert columns["customer_id"]["config"]["meta"]["dex"] == {
        "role": "foreign_key",
        "null_rule": "never",
        "references": "dim_customers.customer_id",
    }
    # A new list follows the spelling the file already uses (`tests:` here).
    assert columns["customer_id"]["tests"] == [
        "not_null",
        {
            "relationships": {
                "arguments": {"to": "ref('dim_customers')", "field": "customer_id"}
            }
        },
    ]
    assert columns["order_total"]["config"]["meta"]["dex"]["unit"] == "currency:USD"


def test_a_single_column_grain_is_tested_unique_and_not_null():
    [declaration] = parse_declarations([{"model": "fct_orders", "grain": ["order_id"]}])

    entry = _entry(render(None, declaration))

    assert entry["columns"] == [
        {"name": "order_id", "data_tests": ["unique", "not_null"]}
    ]


def test_a_composite_grain_is_declared_but_not_tested():
    [declaration] = parse_declarations(
        [{"model": "fct_orders", "grain": ["order_id", "status"]}]
    )

    entry = _entry(render(None, declaration))

    assert entry["config"]["meta"]["dex"]["grain"] == ["order_id", "status"]
    assert "columns" not in entry


def test_a_later_partial_declaration_keeps_what_an_earlier_one_said():
    [first] = parse_declarations([FULL])
    [second] = parse_declarations(
        [{"model": "fct_orders", "description": "Orders, restated."}]
    )

    out = render(render(HAND_WRITTEN, first), second)
    dex = _entry(out)["config"]["meta"]["dex"]

    assert _entry(out)["description"] == "Orders, restated."
    assert dex["grain"] == ["order_id"]
    assert dex["assumptions"][0]["chosen"] == "completed and shipped"


def test_rendering_the_same_declaration_twice_changes_nothing_the_second_time():
    [declaration] = parse_declarations([FULL])

    once = render(HAND_WRITTEN, declaration)

    assert render(once, declaration) == once


def test_a_flow_style_entry_is_refused_rather_than_reflowed():
    [declaration] = parse_declarations([{"model": "fct_orders", "description": "x"}])
    from exmergo_dex_core.transform.rewrite import RewriteError

    with pytest.raises(RewriteError, match="flow style"):
        render("models:\n  - {name: fct_orders}\n", declaration)


# --- where it lands -------------------------------------------------------------


def test_the_model_s_existing_entry_is_found_in_a_shared_file():
    files = _project(HAND_WRITTEN)

    assert schema_path(files, "fct_orders") == "models/marts/_marts.yml"


def test_a_model_with_no_entry_gets_one_beside_its_sql():
    [declaration] = parse_declarations([FULL])

    edits, _warnings = apply_declarations([declaration], _view(_project()), [])

    [edit] = edits
    assert edit.path == "models/marts/fct_orders.yml"
    assert edit.kind is EditKind.SCHEMA_YML
    assert _entry(edit.new_content)["config"]["meta"]["dex"]["grain"] == ["order_id"]


def test_a_declaration_folds_into_the_schema_edit_the_payload_already_carries():
    [declaration] = parse_declarations([{"model": "fct_orders", "description": "x"}])
    authored = PlanEdit(
        path="models/marts/_marts.yml",
        kind=EditKind.SCHEMA_YML,
        new_content=HAND_WRITTEN,
    )

    edits, _warnings = apply_declarations([declaration], _view(_project()), [authored])

    [edit] = edits
    assert edit.path == authored.path
    assert _entry(edit.new_content)["description"] == "x"


# --- refusals and warnings ------------------------------------------------------


@pytest.mark.parametrize(
    ("declaration", "expected"),
    [
        ({"model": "fct_nothing"}, "is not a model in this project"),
        (
            {"model": "fct_orders", "columns": {"margin": {"role": "measure"}}},
            "does not produce",
        ),
        ({"model": "fct_orders", "grain": ["order_key"]}, "grain column"),
        (
            {
                "model": "fct_orders",
                "columns": {"customer_id": {"references": "dim_people.id"}},
            },
            "no model, seed or snapshot named 'dim_people'",
        ),
        (
            {
                "model": "fct_orders",
                "columns": {"customer_id": {"references": "dim_customers.cust_key"}},
            },
            "does not produce a column 'cust_key'",
        ),
        (
            {
                "model": "fct_orders",
                "columns": {"customer_id": {"references": "dim_customers"}},
            },
            "<model>.<column>",
        ),
    ],
)
def test_a_declaration_that_contradicts_the_project_is_refused(declaration, expected):
    parsed = parse_declarations([declaration])

    with pytest.raises(DeclarationError) as caught:
        apply_declarations(parsed, _view(_project()), [])

    assert expected in str(caught.value)


def test_a_filter_on_a_column_that_looks_like_personal_data_is_refused():
    parsed = parse_declarations(
        [
            {
                "model": "fct_orders",
                "population": {
                    "filters": [{"column": "email", "excludes": ["a@example.com"]}]
                },
            }
        ]
    )

    with pytest.raises(DeclarationError, match="committed to git"):
        apply_declarations(parsed, _view(_project()), [])


def test_a_reviewed_filter_column_is_cleared_by_pii_overrides():
    parsed = parse_declarations(
        [
            {
                "model": "fct_orders",
                "population": {"filters": [{"column": "email", "excludes": ["x"]}]},
            }
        ]
    )

    apply_declarations(
        parsed, _view(_project()), [], pii_overrides={"fct_orders.email"}
    )


def test_missing_intent_warns_and_never_refuses():
    parsed = parse_declarations([{"model": "fct_orders"}])

    edits, warnings = apply_declarations(parsed, _view(_project()), [])

    assert edits == [] or all(e.kind is EditKind.SCHEMA_YML for e in edits)
    assert any("declares no description, grain, assumptions" in w for w in warnings)
    assert any("with no declared role" in w for w in warnings)


# --- decisions ------------------------------------------------------------------


def test_decisions_are_read_back_from_the_rendered_yaml():
    [declaration] = parse_declarations([FULL])
    files = {"models/marts/_marts.yml": render(HAND_WRITTEN, declaration)}

    assert decisions(files, {"fct_orders"}) == [
        {
            "model": "fct_orders",
            "decision": "Which order statuses count as revenue",
            "chosen": "completed and shipped",
            "evidence": "precedent",
        }
    ]
    assert decisions(files, {"dim_customers"}) == []


# --- reconcile agrees on the lookup ---------------------------------------------


def test_reconcile_finds_a_staging_model_declared_in_a_shared_file(tmp_path: Path):
    from exmergo_dex_core.adapters.project import DbtProject

    project = tmp_path / "p"
    (project / "models" / "staging").mkdir(parents=True)
    (project / "dbt_project.yml").write_text(
        "name: p\nversion: '1.0'\nprofile: p\n", encoding="utf-8"
    )
    (project / "models" / "staging" / "stg_orders.sql").write_text(
        "select 1 as id\n", encoding="utf-8"
    )
    (project / "models" / "staging" / "_staging.yml").write_text(
        "version: 2\nmodels:\n  - name: stg_orders\n    columns:\n      - name: id\n",
        encoding="utf-8",
    )

    placement = DbtProject(repo_root=project, project_dir=project)

    assert (
        placement.edit_path(EditKind.SCHEMA_YML, "orders")
        == "models/staging/_staging.yml"
    )
    # A model the project does not have yet goes where the scaffold puts one.
    assert (
        placement.edit_path(EditKind.SCHEMA_YML, "refunds")
        == "models/staging/stg_refunds.yml"
    )

    from exmergo_dex_core.dbt_project import load as load_project
    from exmergo_dex_core.maintain.declare import DeclarationEdits, Placed

    placed = DeclarationEdits(load_project(project), placement).resolve("orders")
    assert placed == Placed("models/staging/_staging.yml", "stg_orders")


# --- end to end -----------------------------------------------------------------


def _run(argv: list[str], capsys) -> tuple[int, dict]:
    rc = main(argv)
    out = capsys.readouterr().out
    assert out.count("\n") == 1, "exactly one line on stdout"
    return rc, json.loads(out)


def _payload(tmp_path: Path, body: dict) -> str:
    path = tmp_path / "payload.json"
    path.write_text(json.dumps(body), encoding="utf-8")
    return str(path)


STAGING = {
    "model": "stg_customers",
    "description": "One row per customer.",
    "grain": ["id"],
    "columns": {"id": {"role": "key"}, "email": {"role": "attribute"}},
    "assumptions": [
        {
            "decision": "Whether test accounts count as customers",
            "chosen": "they do",
            "evidence": "default",
        }
    ],
}


def test_plan_then_apply_declares_the_model_where_it_already_lives(
    dbt_project_dir: Path, tmp_path: Path, capsys
):
    rc, envelope = _run(
        [
            "--repo-root",
            str(tmp_path),
            "transform",
            "plan",
            "declare stg_customers",
            "--edits-file",
            _payload(tmp_path, {"declarations": [STAGING]}),
        ],
        capsys,
    )

    assert rc == 0, envelope
    # Into the shared schema.yml the project already declares it in, never a
    # second `stg_customers.yml` beside it.
    assert envelope["data"]["paths"] == ["models/staging/schema.yml"]
    assert envelope["data"]["decisions"] == [
        {
            "model": "stg_customers",
            "decision": "Whether test accounts count as customers",
            "chosen": "they do",
            "evidence": "default",
        }
    ]

    rc, applied = _run(
        [
            "--repo-root",
            str(tmp_path),
            "transform",
            "apply",
            envelope["data"]["plan_id"],
        ],
        capsys,
    )

    assert rc == 0, applied
    assert applied["data"]["decisions"] == envelope["data"]["decisions"]
    written = (dbt_project_dir / "models/staging/schema.yml").read_text(
        encoding="utf-8"
    )
    entry = _entry(written, "stg_customers")
    assert entry["config"]["meta"]["dex"]["grain"] == ["id"]
    # The column's own `tests:` list keeps its key and gains `unique`.
    id_column = next(c for c in entry["columns"] if c["name"] == "id")
    assert id_column["tests"] == ["not_null", "unique"]


def test_a_malformed_declaration_is_an_error_envelope_naming_the_fix(
    dbt_project_dir: Path, tmp_path: Path, capsys
):
    rc, envelope = _run(
        [
            "--repo-root",
            str(tmp_path),
            "transform",
            "plan",
            "declare",
            "--edits-file",
            _payload(
                tmp_path,
                {
                    "declarations": [
                        {"model": "stg_customers", "columns": {"phone": {}}}
                    ]
                },
            ),
        ],
        capsys,
    )

    assert rc == 1
    assert envelope["status"] == "error"
    assert "does not produce" in envelope["errors"][0]


def test_a_plan_writing_model_sql_with_no_declaration_warns(
    dbt_project_dir: Path, tmp_path: Path, capsys
):
    rc, envelope = _run(
        [
            "--repo-root",
            str(tmp_path),
            "transform",
            "plan",
            "new mart",
            "--edits-file",
            _payload(
                tmp_path,
                {
                    "edits": [
                        {
                            "path": "models/marts/fct_customers.sql",
                            "kind": "model_sql",
                            "content": "select id from {{ ref('stg_customers') }}\n",
                        }
                    ]
                },
            ),
        ],
        capsys,
    )

    assert rc == 0, envelope
    assert any(
        "declares nothing about what it means" in w for w in envelope["warnings"]
    )
    assert envelope["data"]["decisions"] == []


def test_a_semantic_plan_carries_declarations_beside_its_definitions(
    dbt_project_dir: Path, tmp_path: Path, capsys
):
    semantic_yaml = (
        "semantic_models:\n"
        "  - name: customers\n"
        "    model: ref('stg_customers')\n"
        "    entities:\n"
        "      - name: customer\n"
        "        type: primary\n"
        "        expr: id\n"
        "    measures:\n"
        "      - name: customer_count\n"
        "        agg: count\n"
        "        expr: id\n"
    )
    rc, envelope = _run(
        [
            "--repo-root",
            str(tmp_path),
            "semantic",
            "define",
            "customers",
            "--edits-file",
            _payload(
                tmp_path,
                {
                    "edits": [
                        {
                            "path": "models/semantic/customers.yml",
                            "content": semantic_yaml,
                        }
                    ],
                    "declarations": [STAGING],
                },
            ),
        ],
        capsys,
    )

    assert rc == 0, envelope
    assert sorted(envelope["data"]["paths"]) == [
        "models/semantic/customers.yml",
        "models/staging/schema.yml",
    ]
    assert [d["model"] for d in envelope["data"]["decisions"]] == ["stg_customers"]


def test_a_schema_edit_dbt_cannot_parse_is_refused_before_it_is_stored(
    dbt_project_dir: Path, tmp_path: Path, capsys
):
    """A second entry for a model already declared elsewhere is dbt's
    `DuplicatePatchPathError`; the parse gate now runs for `schema_yml` edits, so
    it is refused at plan time rather than at build."""

    pytest.importorskip("dbt.adapters.duckdb")
    rc, envelope = _run(
        [
            "--repo-root",
            str(tmp_path),
            "transform",
            "plan",
            "second entry",
            "--edits-file",
            _payload(
                tmp_path,
                {
                    "edits": [
                        {
                            "path": "models/staging/stg_customers.yml",
                            "kind": "schema_yml",
                            "content": "version: 2\nmodels:\n  - name: stg_customers\n",
                        }
                    ]
                },
            ),
        ],
        capsys,
    )

    assert rc == 1
    assert "dbt parse failed" in envelope["errors"][0]


def test_build_reports_the_decisions_of_the_models_it_ran(
    dbt_project_dir: Path, tmp_path: Path, capsys, monkeypatch
):
    import importlib
    import subprocess

    rc, envelope = _run(
        [
            "--repo-root",
            str(tmp_path),
            "transform",
            "plan",
            "declare stg_customers",
            "--edits-file",
            _payload(tmp_path, {"declarations": [STAGING]}),
        ],
        capsys,
    )
    assert rc == 0, envelope
    _run(
        [
            "--repo-root",
            str(tmp_path),
            "transform",
            "apply",
            envelope["data"]["plan_id"],
        ],
        capsys,
    )

    run_results = dbt_project_dir / "target" / "run_results.json"
    build_module = importlib.import_module("exmergo_dex_core.transform.build")

    def fake(timeout: float, cwd, env=None):
        def run(argv: list[str]):
            run_results.parent.mkdir(parents=True, exist_ok=True)
            run_results.write_text(
                json.dumps(
                    {
                        "results": [
                            {
                                "unique_id": "model.dex_test.stg_customers",
                                "status": "success",
                                "execution_time": 0.1,
                                "adapter_response": {},
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            return subprocess.CompletedProcess(argv, 0, "", "")

        return run

    monkeypatch.setattr(build_module, "_default_runner", fake)
    rc, built = _run(
        [
            "--repo-root",
            str(tmp_path),
            "transform",
            "build",
            "--target",
            "dev",
            "--confirm",
        ],
        capsys,
    )

    assert rc == 0, built
    assert [d["decision"] for d in built["data"]["decisions"]] == [
        "Whether test accounts count as customers"
    ]


def test_a_command_that_takes_no_declarations_refuses_a_payload_carrying_them(
    dbt_project_dir: Path, tmp_path: Path, capsys
):
    _rc, envelope = _run(
        [
            "--repo-root",
            str(tmp_path),
            "transform",
            "rename",
            "model",
            "stg_customers",
            "stg_people",
            "--edits-file",
            _payload(tmp_path, {"edits": [], "declarations": [STAGING]}),
        ],
        capsys,
    )

    assert envelope["status"] == "error"
    assert "does not take declarations" in envelope["errors"][0]
