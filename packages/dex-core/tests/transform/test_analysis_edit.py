"""`analysis_sql` end to end: containment to the analysis paths, and the two
things an analysis is not.

dbt compiles an analysis and never runs it, so it builds no relation, nothing can
`ref()` it, and it contributes nothing to the cost of a build. That makes it the
cheapest thing in the project and the one most likely to be mistaken for free
rein: dex still holds it to the same read-only SELECT as a model, because that is
a guarantee dex makes about its own writes rather than a rule dbt imposes.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from exmergo_dex_core.cli import main
from exmergo_dex_core.dbt_project import DbtProjectError, node_files
from exmergo_dex_core.dbt_project import load as load_project
from exmergo_dex_core.storage import FilesystemStore
from exmergo_dex_core.transform.plans import EditKind, PlanEdit, PlanError
from exmergo_dex_core.transform.plans import plan as make_plan
from exmergo_dex_core.transform.validate import EditValidationError, validate_edit

ANALYSIS = """-- Scratch: how lumpy is the customer distribution?
select
    email,
    count(*) as rows_per_email
from {{ ref('stg_customers') }}
group by 1
order by 2 desc
"""


def _run(argv: list[str], capsys) -> tuple[int, dict]:
    rc = main(argv)
    out = capsys.readouterr().out
    assert out.count("\n") == 1, "exactly one line on stdout"
    return rc, json.loads(out)


def _edits_file(tmp_path: Path, *entries: dict) -> Path:
    payload = tmp_path / "edits.json"
    payload.write_text(json.dumps({"edits": list(entries)}), encoding="utf-8")
    return payload


def _analysis(content: str, path: str = "analyses/email_skew.sql") -> PlanEdit:
    return PlanEdit(path=path, kind=EditKind.ANALYSIS_SQL, new_content=content)


# --- the query check --------------------------------------------------------------


def test_an_analysis_validates():
    assert validate_edit(_analysis(ANALYSIS)) == []


def test_an_analysis_with_no_ref_is_not_warned_about():
    # Unlike a singular test, an analysis that names no table is not suspicious:
    # a scratch query over literals is a legitimate thing to keep in the repo.
    assert validate_edit(_analysis("select 1 as answer\n")) == []


@pytest.mark.parametrize(
    ("content", "fix_named"),
    [
        pytest.param("delete from customers\n", "read-only SELECT", id="delete"),
        pytest.param("create table t as select 1\n", "read-only SELECT", id="ddl"),
        pytest.param(
            "select 1;\nselect 2;\n", "exactly one statement", id="two_statements"
        ),
    ],
)
def test_a_writing_analysis_is_refused(content: str, fix_named: str):
    # dbt would compile any of these happily, because it never runs an analysis.
    # dex refuses them anyway: compiled SQL sitting in target/ is one copy-paste
    # from being run by hand, and read-only against data is not conditional on
    # who presses the button.
    with pytest.raises(EditValidationError, match=fix_named):
        validate_edit(_analysis(content))


def test_an_analysis_that_is_entirely_jinja_warns():
    assert validate_edit(_analysis("{{ some_macro() }}\n")) == [
        "analyses/email_skew.sql: analysis is entirely jinja; SELECT-only check skipped"
    ]


# --- containment and kind agreement ----------------------------------------------


def test_an_analysis_plans_applies_and_lands_as_a_create_diff(
    dbt_project_dir: Path, tmp_path: Path, capsys
):
    payload = _edits_file(
        tmp_path,
        {
            "path": "analyses/email_skew.sql",
            "kind": "analysis_sql",
            "content": ANALYSIS,
        },
        {
            "path": "analyses/schema.yml",
            "kind": "schema_yml",
            "content": (
                "version: 2\n"
                "analyses:\n"
                "  - name: email_skew\n"
                "    description: how lumpy the customer distribution is\n"
            ),
        },
    )
    rc, envelope = _run(
        [
            "--repo-root",
            str(tmp_path),
            "transform",
            "plan",
            "look at email skew",
            "--edits-file",
            str(payload),
        ],
        capsys,
    )
    assert rc == 0, envelope["errors"]
    assert envelope["data"]["paths"] == [
        "analyses/email_skew.sql",
        "analyses/schema.yml",
    ]
    created = {d["path"]: d for d in envelope["diffs"]}["analyses/email_skew.sql"]
    assert created["op"] == "create"
    assert created["deletions"] == 0
    assert "rows_per_email" in created["unified"]

    rc, envelope = _run(["--repo-root", str(tmp_path), "transform", "apply"], capsys)
    assert rc == 0, envelope["errors"]
    written = dbt_project_dir / "analyses" / "email_skew.sql"
    assert written.read_text(encoding="utf-8") == ANALYSIS


def test_custom_analysis_paths_are_honored(dbt_project_dir: Path, tmp_path: Path):
    project_yml = dbt_project_dir / "dbt_project.yml"
    project_yml.write_text(
        project_yml.read_text(encoding="utf-8") + 'analysis-paths: ["scratch"]\n',
        encoding="utf-8",
    )
    store = FilesystemStore(tmp_path)
    stored, _diffs, _warnings = make_plan(
        "look",
        [_analysis(ANALYSIS, "scratch/email_skew.sql")],
        dbt_project_dir,
        tmp_path,
        store=store,
    )
    assert stored.edits[0].path == "scratch/email_skew.sql"

    with pytest.raises(DbtProjectError, match=r"analysis \(scratch\)"):
        make_plan("look", [_analysis(ANALYSIS)], dbt_project_dir, tmp_path, store=store)


def test_kind_and_surface_must_agree_in_both_directions(
    dbt_project_dir: Path, tmp_path: Path
):
    store = FilesystemStore(tmp_path)
    with pytest.raises(PlanError, match="analysis paths"):
        make_plan(
            "bad",
            [_analysis(ANALYSIS, "models/staging/email_skew.sql")],
            dbt_project_dir,
            tmp_path,
            store=store,
        )

    model_in_analyses = PlanEdit(
        path="analyses/x.sql", kind=EditKind.MODEL_SQL, new_content="select 1\n"
    )
    with pytest.raises(PlanError, match="analysis_sql"):
        make_plan("bad", [model_in_analyses], dbt_project_dir, tmp_path, store=store)


def test_the_refusal_reads_as_english_for_a_vowel_initial_name(
    dbt_project_dir: Path, tmp_path: Path
):
    # Found by dogfooding: "a analysis_sql edit ... which is a analysis path"
    # reads as a typo in the one message whose whole job is to be trusted, and
    # `analysis` is the first family name that starts with a vowel.
    store = FilesystemStore(tmp_path)
    with pytest.raises(PlanError) as misfiled_kind:
        make_plan(
            "bad",
            [_analysis(ANALYSIS, "tests/scratch.sql")],
            dbt_project_dir,
            tmp_path,
            store=store,
        )
    assert "an analysis_sql edit" in str(misfiled_kind.value)

    with pytest.raises(PlanError) as misfiled_file:
        make_plan(
            "bad",
            [
                PlanEdit(
                    path="analyses/x.sql",
                    kind=EditKind.MODEL_SQL,
                    new_content="select 1\n",
                )
            ],
            dbt_project_dir,
            tmp_path,
            store=store,
        )
    assert "which is an analysis path" in str(misfiled_file.value)


def test_a_schema_yml_beside_an_analysis_is_accepted(
    dbt_project_dir: Path, tmp_path: Path
):
    properties = PlanEdit(
        path="analyses/schema.yml",
        kind=EditKind.SCHEMA_YML,
        new_content="version: 2\nanalyses:\n  - name: email_skew\n",
    )
    stored, _diffs, _warnings = make_plan(
        "document the analysis",
        [properties],
        dbt_project_dir,
        tmp_path,
        store=FilesystemStore(tmp_path),
    )
    assert stored.edits[0].path == "analyses/schema.yml"


# --- the project view: an analysis is loaded, and is not a node ------------------


def test_an_analysis_is_loaded_but_is_not_a_node(dbt_project_dir: Path):
    analyses = dbt_project_dir / "analyses"
    analyses.mkdir()
    (analyses / "email_skew.sql").write_text(ANALYSIS, encoding="utf-8")
    (analyses / "schema.yml").write_text("version: 2\n", encoding="utf-8")

    view = load_project(dbt_project_dir)
    assert "analyses/email_skew.sql" in view.files
    assert "analyses/schema.yml" in view.files
    assert set(node_files(view)) == {"models/staging/stg_customers.sql"}


# --- the build ------------------------------------------------------------------


def test_an_analysis_is_not_priced():
    from exmergo_dex_core.transform.build import _PRICED_RESOURCE_TYPES

    # An analysis is compiled and never run, so it issues no billed statement.
    # Pinned beside the kinds that do so a future edit to the set cannot quietly
    # start charging for one.
    assert "analysis" not in _PRICED_RESOURCE_TYPES
    assert {"model", "snapshot", "test"} <= _PRICED_RESOURCE_TYPES


def test_a_dev_build_compiles_an_applied_analysis_and_materializes_nothing(
    dbt_project_dir: Path, tmp_path: Path, capsys
):
    duckdb = pytest.importorskip("duckdb")

    payload = _edits_file(
        tmp_path,
        {
            "path": "analyses/email_skew.sql",
            "kind": "analysis_sql",
            "content": ANALYSIS,
        },
    )
    rc, envelope = _run(
        [
            "--repo-root",
            str(tmp_path),
            "transform",
            "plan",
            "look at email skew",
            "--edits-file",
            str(payload),
        ],
        capsys,
    )
    assert rc == 0, envelope["errors"]
    rc, _envelope = _run(["--repo-root", str(tmp_path), "transform", "apply"], capsys)
    assert rc == 0

    rc, envelope = _run(
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
    assert rc == 0, envelope["errors"]

    # dbt's semantics, checked rather than trusted. dbt parses the file from the
    # analysis paths and registers it as an analysis node, which is what proves
    # the apply put it where dbt looks. It is not in the build graph, so it is
    # never built, never materialized, and `dbt build` does not even compile it
    # (only `dbt compile` does, which is why this asserts on the manifest rather
    # than on target/compiled).
    names = {n["name"] for n in envelope["data"]["nodes"]}
    assert "email_skew" not in names
    manifest = json.loads(
        (dbt_project_dir / "target" / "manifest.json").read_text(encoding="utf-8")
    )
    assert "analysis.dex_test.email_skew" in manifest["nodes"]

    con = duckdb.connect(str(tmp_path / "dev.duckdb"), read_only=True)
    try:
        relations = {
            row[0]
            for row in con.execute(
                "select table_name from information_schema.tables"
            ).fetchall()
        }
    finally:
        con.close()
    assert "email_skew" not in relations
