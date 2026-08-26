"""`transform rename` / `transform remove`: the whole change, or none of it.

The first test is the issue's own acceptance case. The rest are the refusals, and
they carry the weight: this command's value is not that it can rewrite five files,
it is that it will not rewrite four of them. Each refusal is a way the reference
report can fall short of complete, and acting on a short report is the quiet
failure the whole feature exists to prevent.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from exmergo_dex_core.cli import main
from exmergo_dex_core.dbt_project import EditOp, load
from exmergo_dex_core.transform.plans import EditKind, PlanEdit
from exmergo_dex_core.transform.propagate import PropagationRefusedError, propagate


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A chain deep enough for a rename to have somewhere to go wrong.

    `stg_orders` -> `int_orders` -> three marts, plus a `stg_customers` that
    carries a column of the same name in a different lineage. That last part is
    the point of the fixture: a project-wide rename would rewrite it, and a
    lineage-scoped one must not.
    """

    root = tmp_path / "shop"
    _write(
        root / "dbt_project.yml",
        'name: shop\nprofile: shop\nversion: "1.0"\n'
        "model-paths: [models]\n"
        "vars:\n  using_department: true\n",
    )
    _write(
        root / "models/staging/stg_orders.sql",
        "select\n"
        "    order_id,          -- the natural key\n"
        "    customer_id,\n"
        "    amount\n"
        "from {{ source('raw', 'orders') }}\n",
    )
    _write(
        root / "models/staging/stg_customers.sql",
        "select\n    order_id,\n    region\nfrom {{ source('raw', 'customers') }}\n",
    )
    _write(
        root / "models/marts/int_orders.sql",
        "select\n    o.order_id,\n    o.amount\nfrom {{ ref('stg_orders') }} o\n",
    )
    _write(
        root / "models/marts/mart_a.sql",
        "select\n    order_id,\n    amount\nfrom {{ ref('int_orders') }}\n",
    )
    _write(
        root / "models/marts/mart_b.sql",
        "select\n    order_id\nfrom {{ ref('int_orders') }}\n",
    )
    _write(
        root / "models/staging/schema.yml",
        "version: 2\n"
        "sources:\n  - name: raw\n    tables:\n      - name: orders\n"
        "      - name: customers\n"
        "models:\n"
        "  - name: stg_orders\n"
        '    description: "one row per order_id"\n'
        "    columns:\n"
        "      - name: order_id\n"
        "        description: the order_id itself\n"
        "        tests:\n          - unique\n",
    )
    return root


def _propagate(root: Path, *args, **kwargs):
    return propagate(load(root), root, *args, **kwargs)


class TestColumnRename:
    def test_a_rename_across_the_lineage_is_one_plan(self, project: Path):
        """The issue's acceptance case: every model plus the yml, together."""

        result = _propagate(project, "column", "stg_orders.order_id", "order_key")

        paths = {edit.path for edit in result.edits}
        assert paths == {
            "models/staging/stg_orders.sql",
            "models/marts/int_orders.sql",
            "models/marts/mart_a.sql",
            "models/marts/mart_b.sql",
            "models/staging/schema.yml",
        }
        assert result.sites == {"select_column": 4, "yaml_column": 1}

    def test_a_same_named_column_in_another_lineage_is_untouched(self, project: Path):
        result = _propagate(project, "column", "stg_orders.order_id", "order_key")

        assert "models/staging/stg_customers.sql" not in {e.path for e in result.edits}

    def test_prose_that_contains_the_name_is_left_alone(self, project: Path):
        result = _propagate(project, "column", "stg_orders.order_id", "order_key")

        yml = next(e for e in result.edits if e.path.endswith("schema.yml"))
        assert 'description: "one row per order_id"' in yml.new_content
        assert "description: the order_id itself" in yml.new_content
        assert "- name: order_key" in yml.new_content

    def test_comments_and_formatting_survive_byte_for_byte(self, project: Path):
        result = _propagate(project, "column", "stg_orders.order_id", "order_key")

        sql = next(e for e in result.edits if e.path.endswith("stg_orders.sql"))
        assert "order_key,          -- the natural key" in sql.new_content

    def test_a_bare_column_name_is_refused_with_the_models_that_define_it(
        self, project: Path
    ):
        """A report may answer imprecisely; a rewrite may not.

        Renaming a bare name project-wide would rewrite every unrelated column
        that happens to share it, and the result would compile.
        """

        with pytest.raises(PropagationRefusedError) as exc:
            _propagate(project, "column", "order_id", "order_key")

        assert "stg_orders.order_id" in str(exc.value)
        assert "stg_customers.order_id" in str(exc.value)


class TestRefusals:
    def test_an_unresolvable_reference_refuses_and_says_why_it_differs(
        self, project: Path
    ):
        """Stricter than the delete guard, deliberately.

        A dangling dynamic ref left by a delete is unsatisfiable, so that guard
        warns. The same reference in a rename's path is satisfiable by hand, so
        this refuses and the message says so.
        """

        _write(
            project / "models/marts/mart_dyn.sql",
            "select * from {{ ref(var('which')) }}\n",
        )

        with pytest.raises(PropagationRefusedError) as exc:
            _propagate(project, "model", "stg_orders", "stg_order_events")

        assert "mart_dyn.sql:1" in str(exc.value)
        assert "delete guard" in str(exc.value)

    def test_a_column_passed_to_a_macro_as_a_string_blocks_the_rename(
        self, project: Path
    ):
        """dex cannot tell a column argument from a label, so it will not choose.

        Rewriting a label changes what the project reports without changing what
        it computes, which is worse than refusing.
        """

        _write(
            project / "models/marts/mart_macro.sql",
            "select {{ cents_to_dollars('amount') }} as d\n"
            "from {{ ref('int_orders') }}\n",
        )

        with pytest.raises(PropagationRefusedError) as exc:
            _propagate(project, "column", "stg_orders.amount", "amount_cents")

        assert "mart_macro.sql" in str(exc.value)
        assert "as a label" in str(exc.value)

    def test_a_name_a_package_also_defines_refuses_naming_both(self, project: Path):
        """Renaming this project's copy un-shadows the package's.

        The package's model would then resolve under the old name, and dex does
        not edit installed packages, so it cannot make the other half of the
        change.
        """

        _write(project / "packages.yml", "packages:\n  - package: acme/utils\n")
        pkg = project / "dbt_packages/utils"
        _write(pkg / "dbt_project.yml", 'name: utils\nprofile: u\nversion: "1.0"\n')
        _write(pkg / "models/stg_orders.sql", "select 1 as x\n")

        with pytest.raises(PropagationRefusedError) as exc:
            _propagate(project, "model", "stg_orders", "stg_order_events")

        assert "utils" in str(exc.value)
        assert "shadow" in str(exc.value)

    def test_a_name_that_is_not_used_says_so_rather_than_planning_nothing(
        self, project: Path
    ):
        with pytest.raises(PropagationRefusedError, match="nothing to change"):
            _propagate(project, "var", "no_such_var", "x")

    def test_a_semantic_kind_points_at_the_command_that_owns_it(self, project: Path):
        with pytest.raises(PropagationRefusedError, match="semantic update"):
            _propagate(project, "metric", "revenue", "total_revenue")

    def test_renaming_a_column_to_its_own_name_is_a_no_op_not_an_empty_plan(
        self, project: Path
    ):
        """`old` carries the model that scopes it and `new` never does.

        So `stg_orders.order_id` renamed to `order_id` is a no-op that a plain
        string comparison does not see, and it used to reach the plan store as a
        set of edits that all filtered out.
        """

        with pytest.raises(PropagationRefusedError, match="already its name"):
            _propagate(project, "column", "stg_orders.order_id", "order_id")


class TestNodeRename:
    def test_a_model_rename_is_a_move_plus_every_referrer(self, project: Path):
        result = _propagate(project, "model", "stg_orders", "stg_order_events")

        by_path = {(e.path, e.op) for e in result.edits}
        assert ("models/staging/stg_orders.sql", EditOp.DELETE) in by_path
        assert ("models/staging/stg_order_events.sql", EditOp.UPSERT) in by_path
        moved = next(e for e in result.edits if e.path.endswith("stg_order_events.sql"))
        assert "order_id,          -- the natural key" in moved.new_content
        referrer = next(e for e in result.edits if e.path.endswith("int_orders.sql"))
        assert "{{ ref('stg_order_events') }}" in referrer.new_content

    def test_a_source_rename_changes_each_half_of_the_call(self, project: Path):
        result = _propagate(project, "source", "raw.orders", "landing.order_events")

        sql = next(e for e in result.edits if e.path.endswith("stg_orders.sql"))
        assert "{{ source('landing', 'order_events') }}" in sql.new_content


class TestRemoval:
    def test_a_surviving_read_refuses_and_names_it(self, project: Path):
        """dex removes a declaration and will not invent what a read becomes.

        `{% if var('flag') %}` can be dropped or unguarded; only the caller
        knows which.
        """

        _write(
            project / "models/marts/mart_dept.sql",
            "select\n    1 as x\n    {% if var('using_department') %}\n"
            "    , 2 as y\n    {% endif %}\nfrom {{ ref('int_orders') }}\n",
        )

        with pytest.raises(PropagationRefusedError) as exc:
            _propagate(project, "var", "using_department")

        assert "mart_dept.sql" in str(exc.value)
        assert "--edits-file" in str(exc.value)

    def test_the_callers_read_edits_ride_in_the_same_plan(self, project: Path):
        """What makes the removal atomic without dex guessing at semantics."""

        _write(
            project / "models/marts/mart_dept.sql",
            "select\n    1 as x\n    {% if var('using_department') %}\n"
            "    , 2 as y\n    {% endif %}\nfrom {{ ref('int_orders') }}\n",
        )

        result = _propagate(
            project,
            "var",
            "using_department",
            extra_edits=[
                PlanEdit(
                    path="models/marts/mart_dept.sql",
                    new_content="select\n    1 as x,\n    2 as y\n"
                    "from {{ ref('int_orders') }}\n",
                    kind=EditKind.MODEL_SQL,
                )
            ],
        )

        paths = {edit.path for edit in result.edits}
        assert paths == {"dbt_project.yml", "models/marts/mart_dept.sql"}
        project_yml = next(e for e in result.edits if e.path == "dbt_project.yml")
        assert "using_department" not in project_yml.new_content

    def test_removing_a_column_takes_its_projection_and_its_yml_entry(
        self, project: Path
    ):
        _write(
            project / "models/marts/int_orders.sql",
            "select\n    o.amount\nfrom {{ ref('stg_orders') }} o\n",
        )
        _write(
            project / "models/marts/mart_a.sql",
            "select\n    amount\nfrom {{ ref('int_orders') }}\n",
        )
        _write(
            project / "models/marts/mart_b.sql",
            "select\n    amount\nfrom {{ ref('int_orders') }}\n",
        )

        result = _propagate(project, "column", "stg_orders.order_id")

        paths = {edit.path for edit in result.edits}
        assert paths == {"models/staging/stg_orders.sql", "models/staging/schema.yml"}
        sql = next(e for e in result.edits if e.path.endswith("stg_orders.sql"))
        assert "order_id" not in sql.new_content
        assert "customer_id" in sql.new_content


class TestCommandSurface:
    def test_the_command_stores_a_plan_and_reports_the_sites(
        self, project: Path, capsys
    ):
        rc = main(
            [
                "--repo-root",
                str(project.parent),
                "transform",
                "rename",
                "column",
                "stg_orders.order_id",
                "order_key",
            ]
        )
        payload = json.loads(capsys.readouterr().out)

        assert rc == 0
        assert payload["status"] == "ok"
        assert payload["data"]["change"] == "stg_orders.order_id -> order_key"
        assert payload["data"]["site_count"] == 5
        assert payload["data"]["plan_id"]

    def test_a_refusal_is_one_error_envelope_not_a_traceback(
        self, project: Path, capsys
    ):
        rc = main(
            [
                "--repo-root",
                str(project.parent),
                "transform",
                "rename",
                "column",
                "order_id",
                "order_key",
            ]
        )
        out = capsys.readouterr().out

        assert out.count("\n") == 1
        payload = json.loads(out)
        assert rc != 0
        assert payload["status"] == "error"
        assert "stg_orders.order_id" in json.dumps(payload["errors"])
