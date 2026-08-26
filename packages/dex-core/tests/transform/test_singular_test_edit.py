"""`test_sql` end to end: the two shapes that share the test paths, containment
to those paths, and the two things a test is not.

A singular test is a query dbt runs and counts the rows of; a generic test is a
macro under another keyword. Both live under `test-paths` and only the file's
content says which one was written, so the shape check has to read it. Neither
builds a relation and nothing can `ref()` either, which is why a test is not a
node here even though dbt calls it one.
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

SINGULAR = """-- Every order must reconcile against its items.
select
    o.id,
    o.total
from {{ ref('stg_customers') }} as o
where o.total < 0
"""

GENERIC = """{% test not_negative(model, column_name) %}
select *
from {{ model }}
where {{ column_name }} < 0
{% endtest %}
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


def _test(content: str, path: str = "tests/assert_totals_reconcile.sql") -> PlanEdit:
    return PlanEdit(path=path, kind=EditKind.TEST_SQL, new_content=content)


# --- the two shapes ---------------------------------------------------------------


def test_a_singular_test_validates():
    assert validate_edit(_test(SINGULAR)) == []


def test_a_generic_test_definition_validates():
    # dbt admits a generic test definition under the test paths as well as the
    # macro paths, so refusing it here would refuse a legitimate dbt file inside
    # the family this kind exists to open.
    assert validate_edit(_test(GENERIC, "tests/generic/not_negative.sql")) == []


def test_a_singular_test_against_nothing_warns_rather_than_refuses():
    # A test that names no table always passes, which is worse than no test. It
    # is a warning and not a refusal because an assertion over a literal is
    # unusual rather than wrong.
    warnings = validate_edit(_test("select 1 as id where 1 = 0\n"))
    assert warnings == [
        "tests/assert_totals_reconcile.sql: this test names no ref() or "
        "source(), so it runs against nothing and passes unconditionally"
    ]


def test_a_generic_test_never_draws_the_no_ref_warning():
    # A generic test's body reads `{{ model }}`, never `ref()`. Applying the
    # singular test's warning to both shapes would warn on every well-formed
    # generic test in the project.
    assert validate_edit(_test(GENERIC, "tests/generic/not_negative.sql")) == []


@pytest.mark.parametrize(
    ("content", "fix_named"),
    [
        pytest.param("drop table customers\n", "read-only SELECT", id="not_a_select"),
        pytest.param(
            "select 1;\nselect 2;\n", "exactly one statement", id="two_statements"
        ),
        pytest.param(
            "{% test x(model) %}\nselect 1\n", "unbalanced test", id="unclosed"
        ),
        pytest.param(
            "select 1\n{% endtest %}\n",
            r"needs at least one \{% test name",
            id="orphan_endtest",
        ),
        pytest.param(
            "{% test x(model) %}select 1{% endtest %}\nselect 2\n",
            "loose content",
            id="content_outside",
        ),
    ],
)
def test_a_broken_test_is_refused_with_the_fix(content: str, fix_named: str):
    with pytest.raises(EditValidationError, match=fix_named):
        validate_edit(_test(content, "tests/generic/x.sql"))


def test_a_test_that_is_entirely_jinja_says_so_and_claims_nothing_further():
    # One warning, not two. The no-ref() warning is suppressed here on purpose:
    # a query built inside a macro dex does not follow is content it has just
    # said it cannot read, so asserting the test names no table would be a guess.
    warnings = validate_edit(_test("{{ some_macro_that_builds_the_query() }}\n"))
    assert warnings == [
        "tests/assert_totals_reconcile.sql: test is entirely jinja; "
        "SELECT-only check skipped"
    ]


# --- containment and kind agreement ----------------------------------------------


def test_a_test_plans_applies_and_lands_as_a_create_diff(
    dbt_project_dir: Path, tmp_path: Path, capsys
):
    payload = _edits_file(
        tmp_path,
        {
            "path": "tests/assert_totals_reconcile.sql",
            "kind": "test_sql",
            "content": SINGULAR,
        },
        {
            "path": "tests/schema.yml",
            "kind": "schema_yml",
            "content": (
                "version: 2\n"
                "data_tests:\n"
                "  - name: assert_totals_reconcile\n"
                "    config:\n"
                "      severity: warn\n"
            ),
        },
    )
    rc, envelope = _run(
        [
            "--repo-root",
            str(tmp_path),
            "transform",
            "plan",
            "assert order totals reconcile",
            "--edits-file",
            str(payload),
        ],
        capsys,
    )
    assert rc == 0, envelope["errors"]
    assert envelope["data"]["paths"] == [
        "tests/assert_totals_reconcile.sql",
        "tests/schema.yml",
    ]
    diffs = {d["path"]: d for d in envelope["diffs"]}
    created = diffs["tests/assert_totals_reconcile.sql"]
    assert created["op"] == "create"
    assert created["deletions"] == 0
    assert "where o.total < 0" in created["unified"]

    rc, envelope = _run(["--repo-root", str(tmp_path), "transform", "apply"], capsys)
    assert rc == 0, envelope["errors"]
    written = dbt_project_dir / "tests" / "assert_totals_reconcile.sql"
    assert written.read_text(encoding="utf-8") == SINGULAR


def test_custom_test_paths_are_honored(dbt_project_dir: Path, tmp_path: Path):
    project_yml = dbt_project_dir / "dbt_project.yml"
    project_yml.write_text(
        project_yml.read_text(encoding="utf-8") + 'test-paths: ["assertions"]\n',
        encoding="utf-8",
    )
    store = FilesystemStore(tmp_path)
    stored, _diffs, _warnings = make_plan(
        "assert",
        [_test(SINGULAR, "assertions/assert_totals_reconcile.sql")],
        dbt_project_dir,
        tmp_path,
        store=store,
    )
    assert stored.edits[0].path == "assertions/assert_totals_reconcile.sql"

    # The default location is no longer part of the surface once moved:
    # containment refuses it before the kind is ever consulted, and the refusal
    # names where the test family actually is now.
    with pytest.raises(DbtProjectError, match=r"test \(assertions\)"):
        make_plan("assert", [_test(SINGULAR)], dbt_project_dir, tmp_path, store=store)


def test_kind_and_surface_must_agree_in_both_directions(
    dbt_project_dir: Path, tmp_path: Path
):
    store = FilesystemStore(tmp_path)
    with pytest.raises(PlanError, match="test paths"):
        make_plan(
            "bad",
            [_test(SINGULAR, "models/staging/assert_totals_reconcile.sql")],
            dbt_project_dir,
            tmp_path,
            store=store,
        )

    model_in_tests = PlanEdit(
        path="tests/x.sql", kind=EditKind.MODEL_SQL, new_content="select 1\n"
    )
    with pytest.raises(PlanError, match="test_sql"):
        make_plan("bad", [model_in_tests], dbt_project_dir, tmp_path, store=store)


def test_a_test_is_refused_under_the_analysis_paths(
    dbt_project_dir: Path, tmp_path: Path
):
    # The two new families are adjacent and both hold `.sql`, so the one
    # misfiling a caller is most likely to make is between them.
    with pytest.raises(PlanError, match="test paths"):
        make_plan(
            "bad",
            [_test(SINGULAR, "analyses/assert_totals_reconcile.sql")],
            dbt_project_dir,
            tmp_path,
            store=FilesystemStore(tmp_path),
        )


def test_a_schema_yml_beside_a_test_is_accepted(dbt_project_dir: Path, tmp_path: Path):
    properties = PlanEdit(
        path="tests/schema.yml",
        kind=EditKind.SCHEMA_YML,
        new_content="version: 2\ndata_tests:\n  - name: assert_totals_reconcile\n",
    )
    stored, _diffs, _warnings = make_plan(
        "document the test",
        [properties],
        dbt_project_dir,
        tmp_path,
        store=FilesystemStore(tmp_path),
    )
    assert stored.edits[0].path == "tests/schema.yml"


# --- the project view: a test is loaded, and is not a node -----------------------


def test_a_test_is_loaded_but_is_not_a_node(dbt_project_dir: Path):
    tests = dbt_project_dir / "tests"
    (tests / "generic").mkdir(parents=True)
    (tests / "assert_totals_reconcile.sql").write_text(SINGULAR, encoding="utf-8")
    (tests / "generic" / "not_negative.sql").write_text(GENERIC, encoding="utf-8")
    (tests / "schema.yml").write_text("version: 2\n", encoding="utf-8")

    view = load_project(dbt_project_dir)
    # Loaded, because a file dex can author and does not load hashes as absent:
    # a later edit registers as a create and the apply after it conflicts on a
    # file nobody touched.
    assert "tests/assert_totals_reconcile.sql" in view.files
    assert "tests/generic/not_negative.sql" in view.files
    assert "tests/schema.yml" in view.files

    # dbt calls a singular test a node. It is not one here, because `node_files`
    # answers "what does this project build", and a test builds no relation and
    # nothing can ref() it.
    assert set(node_files(view)) == {"models/staging/stg_customers.sql"}


def test_deleting_a_test_is_not_guarded_against_references(
    dbt_project_dir: Path, tmp_path: Path
):
    tests = dbt_project_dir / "tests"
    tests.mkdir()
    (tests / "assert_totals_reconcile.sql").write_text(SINGULAR, encoding="utf-8")

    delete = PlanEdit(
        path="tests/assert_totals_reconcile.sql",
        kind=EditKind.TEST_SQL,
        op="delete",
    )
    # Nothing can ref() a test, so there is no dangling reference to guard
    # against and the plan stands. The file-level guards still apply: the delete
    # is a diff pinned to the file's hash like any other edit.
    stored, diffs, _warnings = make_plan(
        "drop the assertion",
        [delete],
        dbt_project_dir,
        tmp_path,
        store=FilesystemStore(tmp_path),
    )
    assert stored.edits[0].op == "delete"
    assert diffs[0]["op"] == "delete"


# --- the build ------------------------------------------------------------------


def test_a_singular_test_is_priced():
    from exmergo_dex_core.transform.build import _PRICED_RESOURCE_TYPES

    # A test runs a scanning SELECT against the warehouse, so it costs money and
    # is priced. Already true for the generic tests in schema.yml; authoring
    # singular ones does not change it, and pinning it says so on purpose.
    assert "test" in _PRICED_RESOURCE_TYPES


def test_a_dev_build_runs_an_applied_singular_test(
    dbt_project_dir: Path, tmp_path: Path, capsys
):
    pytest.importorskip("duckdb")

    payload = _edits_file(
        tmp_path,
        {
            "path": "tests/assert_no_negative_ids.sql",
            "kind": "test_sql",
            "content": "select id from {{ ref('stg_customers') }} where id < 0\n",
        },
    )
    rc, envelope = _run(
        [
            "--repo-root",
            str(tmp_path),
            "transform",
            "plan",
            "assert no negative ids",
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
    # The acceptance criterion: `dbt build` picks the test up natively, so an
    # apply and a build is all it takes. No separate `dbt test` for the caller
    # to remember.
    statuses = {n["name"]: n["status"] for n in envelope["data"]["nodes"]}
    # dbt reports a passing test as "pass", not the "success" a model gets.
    assert statuses["assert_no_negative_ids"] == "pass"
