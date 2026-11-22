"""``dex_referenced_key``: the test a ``references`` implies on its parent (#1457).

A column declared ``references: parent.column`` assumes ``column`` is unique in
``parent``: a join to a repeated key multiplies rows. With
``declarations.render_tests`` on, the plan renders a count-only uniqueness test
under that column, in the **parent's** YAML entry, unless the parent declares
the column as its whole grain (whose ``unique`` asserts it). A parent with no
YAML entry renders nothing and says so. Whether the test agrees with
``maintain verify`` (a repeated key fails it, and the child joining it gets
``row_fanout``) is the parity harness's question; these are the rendering, the
gating, the file it writes and the SQL it compiles to.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from exmergo_dex_core import generated_tests as gt
from exmergo_dex_core.cli import main
from exmergo_dex_core.dbt_project import DbtProjectView, SourceFile
from exmergo_dex_core.transform import scaffold
from exmergo_dex_core.transform.claims import claim_id
from exmergo_dex_core.transform.declarations import (
    apply_declarations,
    parse_declarations,
    post_change_files,
)
from exmergo_dex_core.transform.plans import EditKind, PlanEdit
from exmergo_dex_core.transform.validate import validate_edit

NAME = gt.REFERENCED_KEY
PARENT_YML = "models/marts/dim_customers.yml"


def _project(**extra: str) -> dict[str, str]:
    files = {
        "models/marts/fct_orders.sql": "select 1 as order_id, 7 as customer_id\n",
        "models/marts/fct_returns.sql": "select 1 as return_id, 7 as customer_id\n",
        "models/marts/dim_customers.sql": (
            "select 7 as customer_id, 'emea' as region, 'x' as name\n"
        ),
        PARENT_YML: (
            "version: 2\nmodels:\n  - name: dim_customers\n"
            "    description: One row per customer.\n"
        ),
    }
    files.update(extra)
    return files


def _view(files: dict[str, str], root: str = ".") -> DbtProjectView:
    return DbtProjectView(
        root=root,
        project_name="p",
        profile_name="p",
        files={
            path: SourceFile(path=path, content=content, sha256="x")
            for path, content in files.items()
        },
    )


def _apply(
    declarations: list[dict],
    files: dict[str, str] | None = None,
    render_tests=True,
    root: str = ".",
) -> tuple[dict[str, str], list[dict]]:
    """The project as the plan leaves it, and ``data.generated_tests`` rows
    for this kind."""

    view = _view(files if files is not None else _project(), root)
    generated: list[dict] = []
    edits, _warnings = apply_declarations(
        parse_declarations(declarations),
        view,
        [],
        render_tests=render_tests,
        generated=generated,
    )
    return post_change_files(view, edits), [g for g in generated if g["test"] == NAME]


def _references(child: str = "fct_orders", target: str = "dim_customers") -> dict:
    return {
        "model": child,
        "columns": {"customer_id": {"references": f"{target}.customer_id"}},
    }


def _tests(content: str, model: str, column: str) -> list:
    entry = next(e for e in yaml.safe_load(content)["models"] if e["name"] == model)
    found = next((c for c in entry.get("columns") or [] if c["name"] == column), {})
    return [
        t for t in found.get("data_tests") or [] if isinstance(t, dict) and NAME in t
    ]


CLAIM = claim_id("dim_customers", "referenced_key", ["customer_id"], {"check": NAME})


# --- the kind ---------------------------------------------------------------------


def test_it_ships_as_an_every_run_test_on_the_parent_on_every_connector():
    [kind] = [k for k in gt.kinds() if k.name == NAME]
    assert kind.cadence == gt.EVERY_RUN
    assert kind.macro == NAME
    assert kind.dialects == gt.ALL_CONNECTORS
    assert kind.on_parent
    assert NAME in scaffold.MACRO_ASSETS
    assert gt.unknown_entries([NAME]) == []


def test_a_model_s_own_declaration_gives_it_nothing():
    [declaration] = parse_declarations([_references()])
    assert gt._referenced_keys(declaration, None) == []


# --- where it renders -------------------------------------------------------------


def test_declaring_a_child_renders_the_test_into_the_parent_s_entry():
    files, rows = _apply([_references()])

    [test] = _tests(files[PARENT_YML], "dim_customers", "customer_id")
    assert test == {
        NAME: {
            "config": {
                "tags": [gt.EVERY_RUN],
                "meta": {"disable_test_samples": True, "dex": {"claim": CLAIM}},
            },
        }
    }
    # The child's own entry holds its relationships test and nothing of this.
    assert NAME not in files["models/marts/fct_orders.yml"]
    assert rows == [
        {
            "model": "dim_customers",
            "column": "customer_id",
            "test": NAME,
            "cadence": gt.EVERY_RUN,
            "claim": CLAIM,
            "status": "added",
            "referenced_by": ["fct_orders.customer_id"],
        }
    ]


def test_two_children_referencing_one_column_render_one_test():
    files, rows = _apply([_references("fct_orders"), _references("fct_returns")])

    assert len(_tests(files[PARENT_YML], "dim_customers", "customer_id")) == 1
    [row] = rows
    assert row["referenced_by"] == [
        "fct_orders.customer_id",
        "fct_returns.customer_id",
    ]


def test_the_parent_s_description_and_other_bytes_are_kept():
    files, _rows = _apply([_references()])
    assert "description: One row per customer." in files[PARENT_YML]


def test_rendering_again_adds_nothing():
    once, _rows = _apply([_references()])
    twice, rows = _apply([_references()], files=once)

    assert twice[PARENT_YML] == once[PARENT_YML]
    assert [r["status"] for r in rows] == ["already_listed"]


def test_declaring_the_parent_renders_it_for_a_references_declared_earlier():
    declared, _rows = _apply([_references()], render_tests=False)
    assert NAME not in declared[PARENT_YML]

    files, rows = _apply(
        [{"model": "dim_customers", "description": "One row per customer."}],
        files=declared,
    )
    assert len(_tests(files[PARENT_YML], "dim_customers", "customer_id")) == 1
    assert [r["status"] for r in rows] == ["added"]


def test_a_references_the_plan_declares_neither_side_of_renders_nothing():
    declared, _rows = _apply([_references()], render_tests=False)
    files, rows = _apply(
        [{"model": "fct_returns", "description": "One row per return."}],
        files=declared,
    )
    assert NAME not in files[PARENT_YML]
    assert rows == []


# --- what renders nothing ---------------------------------------------------------


def test_a_parent_whose_grain_is_the_column_gets_no_extra_test():
    files, rows = _apply(
        [_references(), {"model": "dim_customers", "grain": ["customer_id"]}]
    )

    assert NAME not in files[PARENT_YML]
    assert "unique" in files[PARENT_YML]
    assert [(r["status"], r["reason"]) for r in rows] == [
        ("not_rendered", gt.COVERED_BY_BUILTIN)
    ]


def test_a_grain_declared_earlier_with_another_spelling_also_covers_it():
    declared, _rows = _apply([{"model": "dim_customers", "grain": ["Customer_ID"]}])
    files, rows = _apply([_references()], files=declared)

    assert NAME not in files[PARENT_YML]
    assert [r["reason"] for r in rows] == [gt.COVERED_BY_BUILTIN]


def test_a_composite_grain_holding_the_column_does_not_cover_it():
    files, rows = _apply(
        [_references(), {"model": "dim_customers", "grain": ["customer_id", "region"]}]
    )
    assert len(_tests(files[PARENT_YML], "dim_customers", "customer_id")) == 1
    assert [r["status"] for r in rows] == ["added"]


def test_a_hand_written_unique_test_on_the_column_covers_it():
    files, rows = _apply(
        [_references()],
        files=_project(
            **{
                PARENT_YML: (
                    "version: 2\nmodels:\n  - name: dim_customers\n    columns:\n"
                    "      - name: customer_id\n        data_tests: [unique]\n"
                )
            }
        ),
    )
    assert NAME not in files[PARENT_YML]
    assert [r["reason"] for r in rows] == [gt.COVERED_BY_BUILTIN]


@pytest.mark.parametrize(
    "unique",
    [
        "- unique:\n            config:\n              where: is_current\n",
        "- unique:\n            config:\n              enabled: false\n",
        '- unique:\n            config:\n              enabled: "{{ false }}"\n',
    ],
)
def test_a_filtered_or_disabled_unique_test_does_not_cover_it(unique):
    # dbt applies `where` first, so a key repeated outside it goes unseen.
    files, rows = _apply(
        [_references()],
        files=_project(
            **{
                PARENT_YML: (
                    "version: 2\nmodels:\n  - name: dim_customers\n    columns:\n"
                    "      - name: customer_id\n        data_tests:\n          "
                    + unique
                )
            }
        ),
    )
    assert len(_tests(files[PARENT_YML], "dim_customers", "customer_id")) == 1
    assert [r["status"] for r in rows] == ["added"]


def test_a_parent_with_no_yaml_entry_renders_nothing_and_no_entry_is_made():
    project = _project()
    del project[PARENT_YML]
    files, rows = _apply([_references()], files=project)

    assert not any(
        "dim_customers" in content
        for path, content in files.items()
        if path.endswith(".yml") and path != "models/marts/fct_orders.yml"
    )
    assert [(r["status"], r["reason"]) for r in rows] == [
        ("not_rendered", gt.PARENT_UNDECLARED)
    ]


def test_a_seed_parent_renders_nothing():
    files, rows = _apply(
        [_references(target="country_codes")],
        files=_project(**{"seeds/country_codes.csv": "customer_id,name\n7,x\n"}),
    )
    assert NAME not in "".join(files.values())
    assert [(r["model"], r["reason"]) for r in rows] == [
        ("country_codes", gt.PARENT_UNDECLARED)
    ]


def test_a_package_model_parent_renders_nothing(tmp_path: Path):
    package = tmp_path / "dbt_packages" / "shop"
    (package / "models").mkdir(parents=True)
    (package / "dbt_project.yml").write_text(
        "name: shop\nversion: '1.0'\nconfig-version: 2\n", encoding="utf-8"
    )
    (package / "models" / "shop_customers.sql").write_text(
        "select 7 as customer_id\n", encoding="utf-8"
    )
    files, rows = _apply([_references(target="shop_customers")], root=str(tmp_path))
    assert NAME not in "".join(files.values())
    assert [(r["model"], r["reason"]) for r in rows] == [
        ("shop_customers", gt.PARENT_IN_PACKAGE)
    ]


def test_a_column_the_parent_provably_does_not_output_renders_nothing():
    # A references to a column the parent lacks is refused when declared, so
    # this is one a later SQL edit left behind, read from the YAML.
    child_yml = (
        "version: 2\nmodels:\n  - name: fct_orders\n    columns:\n"
        "      - name: customer_id\n        config:\n          meta:\n"
        "            dex:\n              references: dim_customers.legacy_id\n"
    )
    files, rows = _apply(
        [{"model": "dim_customers", "description": "One row per customer."}],
        files=_project(**{"models/marts/fct_orders.yml": child_yml}),
    )
    assert NAME not in files[PARENT_YML]
    assert [(r["column"], r["reason"]) for r in rows] == [
        ("legacy_id", gt.COLUMN_NOT_OUTPUT)
    ]


# --- the switch -------------------------------------------------------------------


def test_the_switch_off_renders_nothing_and_says_why():
    files, rows = _apply([_references()], render_tests=False)
    assert NAME not in files[PARENT_YML]
    assert [(r["status"], r["reason"]) for r in rows] == [
        ("not_rendered", "render_tests_off")
    ]


@pytest.mark.parametrize("setting", [[NAME], [gt.EVERY_RUN]])
def test_a_list_naming_the_kind_or_its_cadence_turns_it_on(setting):
    files, _rows = _apply([_references()], render_tests=setting)
    assert NAME in files[PARENT_YML]


def test_a_list_naming_only_the_scheduled_cadence_leaves_it_off():
    files, rows = _apply([_references()], render_tests=[gt.SCHEDULED])
    assert NAME not in files[PARENT_YML]
    assert [r["reason"] for r in rows] == ["kind_not_enabled"]


def test_a_test_listed_while_the_switch_is_off_is_kept_and_reported():
    once, _rows = _apply([_references()])
    files, rows = _apply([_references()], files=once, render_tests=False)

    assert files[PARENT_YML] == once[PARENT_YML]
    assert [r["status"] for r in rows] == ["listed_while_off"]


# --- a versioned parent -----------------------------------------------------------


def test_a_version_label_renders_into_that_version_s_item():
    project = _project(
        **{
            "models/marts/dim_teams_v1.sql": "select 7 as team_id\n",
            "models/marts/dim_teams_v2.sql": "select 7 as team_id, 'x' as name\n",
            "models/marts/dim_teams.yml": (
                "version: 2\nmodels:\n  - name: dim_teams\n    latest_version: 2\n"
                "    versions:\n      - v: 1\n      - v: 2\n"
            ),
        }
    )
    files, rows = _apply(
        [
            {
                "model": "fct_orders",
                "columns": {"customer_id": {"references": "dim_teams.v2.team_id"}},
            }
        ],
        files=project,
    )
    [entry] = yaml.safe_load(files["models/marts/dim_teams.yml"])["models"]
    v1, v2 = entry["versions"]
    assert NAME not in json.dumps(v1)
    assert NAME in json.dumps(v2)
    assert [(r["model"], r["status"]) for r in rows] == [("dim_teams.v2", "added")]


def test_a_bare_versioned_name_renders_on_its_default_version_only():
    # An unversioned ref() reads latest_version; on the shared entry the test
    # would also run on v1, which the child never joins.
    project = _project(
        **{
            "models/marts/dim_teams_v1.sql": "select 7 as team_id\n",
            "models/marts/dim_teams_v2.sql": "select 7 as team_id, 'x' as name\n",
            "models/marts/dim_teams.yml": (
                "version: 2\nmodels:\n  - name: dim_teams\n    latest_version: 2\n"
                "    versions:\n      - v: 1\n      - v: 2\n"
            ),
        }
    )
    files, rows = _apply(
        [
            {
                "model": "fct_orders",
                "columns": {"customer_id": {"references": "dim_teams.team_id"}},
            }
        ],
        files=project,
    )
    [entry] = yaml.safe_load(files["models/marts/dim_teams.yml"])["models"]
    v1, v2 = entry["versions"]
    assert NAME not in json.dumps(v1)
    assert NAME not in json.dumps(entry.get("columns") or [])
    assert NAME in json.dumps(v2)
    assert [(r["model"], r["status"]) for r in rows] == [("dim_teams.v2", "added")]


# --- the macro file and the plan ----------------------------------------------------


def _asset() -> str:
    return scaffold.macro_edit(NAME, "macros").new_content or ""


def test_the_shipped_macro_passes_the_macro_file_check():
    validate_edit(
        PlanEdit(
            path=f"macros/{NAME}.sql", kind=EditKind.MACRO_SQL, new_content=_asset()
        )
    )


def _repo(tmp_path: Path, render_tests: str) -> Path:
    (tmp_path / ".dex").mkdir(exist_ok=True)
    (tmp_path / ".dex" / "config.yml").write_text(
        f"connector: duckdb\nduckdb:\n  path: {tmp_path / 'dev.duckdb'}\n"
        f"declarations:\n  render_tests: {render_tests}\n",
        encoding="utf-8",
    )
    return tmp_path


def _write_parent(project: Path) -> None:
    marts = project / "models" / "marts"
    marts.mkdir(parents=True, exist_ok=True)
    (marts / "dim_customers.sql").write_text(
        "select 7 as customer_id, 'x' as name\n", encoding="utf-8"
    )
    (marts / "dim_customers.yml").write_text(
        "version: 2\nmodels:\n  - name: dim_customers\n"
        "    description: One row per customer.\n",
        encoding="utf-8",
    )


def _plan(tmp_path: Path, capsys, declaration: dict) -> dict:
    payload = tmp_path / "edits.json"
    payload.write_text(
        json.dumps(
            {
                "edits": [
                    {
                        "path": "models/marts/fct_orders.sql",
                        "kind": "model_sql",
                        "content": (
                            "select o.order_id, o.customer_id, c.name\n"
                            "from (select 1 as order_id, 7 as customer_id) o\n"
                            "left join {{ ref('dim_customers') }} c\n"
                            "  on o.customer_id = c.customer_id\n"
                        ),
                    }
                ],
                "declarations": [declaration],
            }
        ),
        encoding="utf-8",
    )
    main(
        [
            "--repo-root",
            str(tmp_path),
            "transform",
            "plan",
            "orders",
            "--edits-file",
            str(payload),
        ]
    )
    return json.loads(capsys.readouterr().out)


def test_the_plan_writes_the_parent_s_yaml_and_the_macro_as_diffs(
    dbt_project_dir: Path, tmp_path: Path, capsys
):
    _write_parent(dbt_project_dir)
    planned = _plan(_repo(tmp_path, "true"), capsys, _references())

    assert planned["status"] == "ok", planned["errors"]
    paths = planned["data"]["paths"]
    assert f"macros/{NAME}.sql" in paths
    assert "models/marts/dim_customers.yml" in paths
    [row] = [
        r for r in planned["data"]["generated_tests"]["tests"] if r["test"] == NAME
    ]
    assert (row["model"], row["status"]) == ("dim_customers", "added")


def test_transform_declare_on_the_parent_renders_the_test_it_owes(
    dbt_project_dir: Path, tmp_path: Path, capsys
):
    _write_parent(dbt_project_dir)
    marts = dbt_project_dir / "models" / "marts"
    (marts / "fct_orders.sql").write_text(
        "select 1 as order_id, 7 as customer_id\n", encoding="utf-8"
    )
    (marts / "fct_orders.yml").write_text(
        "version: 2\nmodels:\n  - name: fct_orders\n    columns:\n"
        "      - name: customer_id\n        config:\n          meta:\n"
        "            dex:\n              references: dim_customers.customer_id\n",
        encoding="utf-8",
    )
    _repo(tmp_path, "true")
    main(["--repo-root", str(tmp_path), "transform", "declare", "dim_customers"])
    declared = json.loads(capsys.readouterr().out)

    assert declared["status"] == "ok", declared["errors"]
    assert declared["data"]["plan_id"] is not None, declared["data"]["skipped"]
    assert "models/marts/dim_customers.yml" in declared["data"]["paths"]


def test_transform_macro_lists_it(dbt_project_dir: Path, tmp_path: Path, capsys):
    _repo(tmp_path, "false")
    main(["--repo-root", str(tmp_path), "transform", "macro"])
    listed = json.loads(capsys.readouterr().out)
    assert NAME in {m["name"] for m in listed["data"]["macros"]}


# --- what it compiles to, against DuckDB --------------------------------------------


def _failures(tmp_path: Path, select: str) -> tuple[str, int, str]:
    """Build ``select`` as a view, run the shipped test on its ``customer_id``
    column, and return dbt's status, failure count and compiled SQL."""

    import subprocess
    import sys

    pytest.importorskip("dbt.adapters.duckdb")
    project = tmp_path / "proj"
    (project / "models").mkdir(parents=True)
    (project / "macros").mkdir()
    (project / "dbt_project.yml").write_text(
        "name: p\nversion: '1.0'\nprofile: p\nconfig-version: 2\n", encoding="utf-8"
    )
    (project / "profiles.yml").write_text(
        "p:\n  target: dev\n  outputs:\n    dev:\n      type: duckdb\n"
        f"      path: {tmp_path / 'w.duckdb'}\n      threads: 1\n",
        encoding="utf-8",
    )
    (project / "macros" / f"{NAME}.sql").write_text(_asset(), encoding="utf-8")
    (project / "models" / "m.sql").write_text(select, encoding="utf-8")
    (project / "models" / "schema.yml").write_text(
        yaml.safe_dump(
            {
                "version": 2,
                "models": [
                    {
                        "name": "m",
                        "columns": [{"name": "customer_id", "data_tests": [NAME]}],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    dbt = Path(sys.executable).parent / "dbt"
    if not dbt.is_file():
        pytest.skip("needs dbt beside the interpreter")
    args = ["--project-dir", str(project), "--profiles-dir", str(project)]
    subprocess.run(  # noqa: S603 - the venv's own dbt, on a fixture project
        [str(dbt), "build", *args], cwd=project, capture_output=True, check=False
    )
    results = json.loads((project / "target" / "run_results.json").read_text())
    [result] = [r for r in results["results"] if r["unique_id"].startswith("test.")]
    compiled = next((project / "target" / "compiled").rglob(f"{NAME}*.sql"))
    return result["status"], int(result["failures"] or 0), compiled.read_text()


def _keys(*values: str) -> str:
    first, *rest = values
    return " ".join(
        [f"select {first} as customer_id", *(f"union all select {v}" for v in rest)]
    )


def test_a_repeated_key_fails_with_the_rows_beyond_the_first_and_never_a_key(
    tmp_path: Path,
):
    # 2 three times and 3 twice: three rows beyond the first of each; a macro
    # counting repeated keys would say two.
    status, failures, sql = _failures(tmp_path, _keys("1", "2", "2", "2", "3", "3"))
    assert (status, failures) == ("fail", 3)
    assert "customer_id" not in sql.split("from", 1)[0]  # a count, never a key


def test_repeated_nulls_are_no_key_and_pass(tmp_path: Path):
    null = "cast(null as integer)"
    assert _failures(tmp_path, _keys("1", "2", null, null))[:2] == ("pass", 0)


def test_unique_keys_pass(tmp_path: Path):
    assert _failures(tmp_path, _keys("1", "2", "3"))[:2] == ("pass", 0)
