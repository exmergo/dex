"""What the Cube reader refuses, what it only warns about, and what it names as
compiled-only (#910).

Every refusal names the file and the element, and a refused element is left out
of the set rather than read best-effort.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("sqlglot")

from exmergo_dex_core.cube.loader import load_files, validate_model_contents
from exmergo_dex_core.cube.validator import canonical_key

from .conftest import ORDERS_LAYER, cube, source, view, write, write_layer


def _rules(loaded, severity: str = "error") -> set[tuple[str, str, str]]:
    return {
        (d.file, d.element, d.rule)
        for d in loaded.diagnostics
        if d.severity == severity
    }


def _load(root: Path, *cubes, views=()):
    return load_files(root, write_layer(root, *cubes, views=views))


# --- the acceptance fixtures -----------------------------------------------------


def test_a_duplicate_cube_or_view_name_is_refused_everywhere_it_is_declared(tmp_path):
    names = write(tmp_path, "model/a.yml", cube("orders"))
    names += write(
        tmp_path,
        "model/b.yml",
        cube("customers"),
        views=[view("orders", {"join_path": "customers", "includes": "*"})],
    )

    loaded = load_files(tmp_path, names)

    assert [c.name for c in loaded.cubes] == ["customers"]
    assert loaded.views == []
    assert {
        ("model/a.yml", "cubes['orders']", "duplicate"),
        ("model/b.yml", "views['orders']", "duplicate"),
    } <= _rules(loaded)


def test_a_join_to_an_unknown_cube_is_refused_naming_the_join(tmp_path):
    orders = cube(
        "orders",
        joins=[
            {
                "name": "nowhere",
                "sql": "{CUBE}.x = {nowhere}.x",
                "relationship": "many_to_one",
            }
        ],
    )

    loaded = _load(tmp_path, orders)

    assert loaded.cubes == []
    assert (
        "model/orders.yml",
        "cubes['orders'].joins['nowhere']",
        "join_target",
    ) in _rules(loaded)


def test_an_unresolved_member_reference_is_refused_naming_the_member(tmp_path):
    orders = cube(
        "orders",
        measures=[{"name": "revenue", "sql": "{CUBE}.amount", "type": "sum"}],
        dimensions=[
            {"name": "amount_band", "sql": "{amount_typo} > 100", "type": "boolean"}
        ],
    )

    loaded = _load(tmp_path, orders)

    assert loaded.cubes == []
    (diagnostic,) = [d for d in loaded.errors if d.rule == "unresolved_reference"]
    assert diagnostic.element == "cubes['orders'].dimensions['amount_band']"
    assert "amount_typo" in diagnostic.message


def test_a_cube_with_both_sql_and_sql_table_is_refused(tmp_path):
    both = {"name": "orders", "sql": "SELECT 1", "sql_table": "demo.main.orders"}
    loaded = _load(tmp_path, both)

    assert loaded.cubes == []
    assert ("model/orders.yml", "cubes['orders']", "sql_xor_sql_table") in _rules(
        loaded
    )


def test_a_view_join_path_no_declared_join_supports_is_refused(tmp_path):
    loaded = _load(
        tmp_path,
        cube("orders"),
        cube("customers"),
        views=[view("sales", {"join_path": "orders.customers", "includes": "*"})],
    )

    assert loaded.views == []
    (diagnostic,) = [d for d in loaded.errors if d.rule == "join_path"]
    assert diagnostic.file == "model/views.yml"
    assert diagnostic.element == "views['sales'].cubes['orders.customers']"
    assert "declares no join to 'customers'" in diagnostic.message


# --- the rest of the set rules -----------------------------------------------------


def test_a_refused_cube_retracts_the_joins_that_reach_it(tmp_path):
    broken = cube("customers", dimensions=[{"name": "id", "type": "number"}])
    orders = cube(
        "orders",
        joins=[
            {
                "name": "customers",
                "sql": "{CUBE}.c = {customers}.id",
                "relationship": "many_to_one",
            }
        ],
    )

    loaded = _load(tmp_path, orders, broken)

    assert loaded.cubes == []
    join = next(d for d in loaded.errors if d.rule == "join_target")
    assert "could not be read" in join.message


def test_two_unaliased_joins_to_one_cube_are_refused(tmp_path):
    join = {
        "name": "customers",
        "sql": "{CUBE}.a = {customers}.id",
        "relationship": "many_to_one",
    }
    loaded = _load(tmp_path, cube("orders", joins=[join, join]), cube("customers"))

    assert (
        "model/orders.yml",
        "cubes['orders'].joins['customers']",
        "duplicate_join",
    ) in _rules(loaded)


def test_a_view_include_the_cube_does_not_declare_is_refused(tmp_path):
    loaded = _load(
        tmp_path,
        cube(
            "orders", dimensions=[{"name": "status", "sql": "status", "type": "string"}]
        ),
        views=[
            view("sales", {"join_path": "orders", "includes": ["status", "missing"]})
        ],
    )

    assert loaded.views == []
    assert any(d.rule == "include" and "'missing'" in d.message for d in loaded.errors)


def test_a_view_reaching_one_cube_by_two_paths_from_one_root_is_refused(tmp_path):
    def join(target):
        return {
            "name": target,
            "sql": f"{{CUBE}}.k = {{{target}}}.k",
            "relationship": "many_to_one",
        }

    loaded = _load(
        tmp_path,
        cube("a", joins=[join("b"), join("d")]),
        cube("b", joins=[join("c")]),
        cube("d", joins=[join("c")]),
        cube("c"),
        views=[
            view(
                "v",
                {"join_path": "a.b.c", "includes": "*", "prefix": True, "alias": "c1"},
                {"join_path": "a.d.c", "includes": "*", "prefix": True, "alias": "c2"},
            )
        ],
    )

    assert any(d.rule == "join_paths" for d in loaded.errors)


@pytest.mark.parametrize(
    ("member", "rule"),
    [
        ({"name": "x", "type": "colour", "sql": "x"}, "dimension_type"),
        ({"name": "x", "type": "string"}, "dimension_sql"),
        ({"name": "9x", "type": "string", "sql": "x"}, "name"),
        ({"name": "x", "type": "string", "sql": "x", "primary_key": "yes"}, "type"),
        ({"name": "x", "type": "switch"}, "switch_values"),
        (
            {
                "name": "x",
                "type": "string",
                "sql": "x",
                "granularities": [{"name": "g", "interval": "1 week"}],
            },
            "granularities",
        ),
        (
            {
                "name": "x",
                "type": "time",
                "sql": "x",
                "granularities": [{"name": "g", "interval": "every week"}],
            },
            "granularity_interval",
        ),
        (
            {
                "name": "x",
                "type": "time",
                "sql": "x",
                "granularities": [{"name": "fiscal", "sql": "x"}],
            },
            "granularity_sql",
        ),
    ],
)
def test_a_malformed_dimension_is_refused_by_rule(tmp_path, member, rule):
    loaded = _load(tmp_path, cube("orders", dimensions=[member]))

    assert loaded.cubes == []
    assert rule in {d.rule for d in loaded.errors}


@pytest.mark.parametrize(
    ("member", "rule"),
    [
        ({"name": "x", "type": "total", "sql": "x"}, "measure_type"),
        ({"name": "x", "type": "sum"}, "measure_sql"),
        ({"name": "x", "type": "number_agg", "sql": "x"}, "measure_type"),
        (
            {
                "name": "x",
                "type": "sum",
                "sql": "x",
                "rolling_window": {"trailing": "a week"},
            },
            "rolling_window",
        ),
        (
            {"name": "x", "type": "sum", "sql": "x", "filters": [{"where": "x"}]},
            "measure_filter",
        ),
    ],
)
def test_a_malformed_measure_is_refused_by_rule(tmp_path, member, rule):
    loaded = _load(tmp_path, cube("orders", measures=[member]))

    assert loaded.cubes == []
    assert rule in {d.rule for d in loaded.errors}


def test_a_duplicate_member_is_refused_and_a_cross_kind_one_warned(tmp_path):
    twice = cube(
        "orders",
        dimensions=[
            {"name": "status", "sql": "status", "type": "string"},
            {"name": "status", "sql": "status", "type": "string"},
        ],
    )
    shared = cube(
        "items",
        dimensions=[{"name": "qty", "sql": "qty", "type": "number"}],
        measures=[{"name": "qty", "sql": "qty", "type": "sum"}],
    )

    loaded = _load(tmp_path, twice, shared)

    assert [c.name for c in loaded.cubes] == ["items"]
    assert (
        "model/orders.yml",
        "cubes['orders'].dimensions['status']",
        "duplicate",
    ) in _rules(loaded)
    assert any(d.rule == "ambiguous_member" for d in loaded.diagnostics)


def test_an_unknown_key_is_a_warning_and_the_cube_is_still_read(tmp_path):
    """Cube refuses a key its schema does not declare; dex only warns, because a
    key this port does not know may be one a newer Cube added."""

    loaded = _load(
        tmp_path,
        cube(
            "orders",
            owner_team="growth",
            dimensions=[{"name": "id", "sql": "id", "type": "number", "colour": "red"}],
        ),
    )

    assert [c.name for c in loaded.cubes] == ["orders"]
    assert {d.rule for d in loaded.diagnostics} == {"unknown_key"}
    assert {d.severity for d in loaded.diagnostics} == {"warning"}


def test_camel_case_keys_and_legacy_relationships_read_as_cube_reads_them(tmp_path):
    orders = {
        "name": "orders",
        "sqlTable": "demo.main.orders",
        "joins": [
            {
                "name": "customers",
                "sql": "{CUBE}.c = {customers}.id",
                "relationship": "belongsTo",
            },
            {
                "name": "items",
                "sql": "{CUBE}.id = {items}.order_id",
                "relationship": "hasMany",
            },
            {
                "name": "notes",
                "sql": "{CUBE}.id = {notes}.order_id",
                "relationship": "has_one",
            },
        ],
        "measures": [{"name": "buyers", "sql": "c", "type": "countDistinct"}],
        "dimensions": [
            {"name": "id", "sql": "id", "type": "number", "primaryKey": True}
        ],
    }

    loaded = _load(tmp_path, orders, cube("customers"), cube("items"), cube("notes"))

    (read,) = [c for c in loaded.cubes if c.name == "orders"]
    assert loaded.errors == []
    assert read.sql_table == "demo.main.orders"
    assert [j.relationship for j in read.joins] == [
        "many_to_one",
        "one_to_many",
        "one_to_one",
    ]
    assert read.measures[0].type == "count_distinct"
    assert read.dimensions[0].primary_key is True
    assert (
        canonical_key("propagateFiltersToSubQuery") == "propagate_filters_to_sub_query"
    )


def test_an_unexpected_top_level_key_and_view_groups_are_named(tmp_path):
    names = write(
        tmp_path,
        "model/orders.yml",
        raw="cubes:\n  - name: orders\n    sql_table: demo.main.orders\n"
        "view_groups:\n  - name: sales\n    views: [x]\nmetrics: []\n",
    )

    loaded = load_files(tmp_path, names)

    assert [c.name for c in loaded.cubes] == ["orders"]
    assert ("model/orders.yml", "(root)", "top_level_key") in _rules(loaded)
    assert ("model/orders.yml", "view_groups", "not_mapped") in _rules(
        loaded, "warning"
    )


def test_malformed_yaml_and_a_missing_file_are_refused_with_the_file_named(tmp_path):
    names = write(tmp_path, "model/broken.yml", raw="cubes: [x,\n")

    loaded = load_files(tmp_path, [*names, "model/absent.yml"])

    assert _rules(loaded) == {
        ("model/broken.yml", "(root)", "malformed"),
        ("model/absent.yml", "(config)", "missing"),
    }


def test_a_file_symlinked_out_of_the_repository_is_not_read(tmp_path):
    outside = tmp_path.parent / f"{tmp_path.name}-secret.yml"
    outside.write_text("password: hunter2\n", encoding="utf-8")
    link = tmp_path / "model" / "orders.yml"
    link.parent.mkdir(parents=True)
    link.symlink_to(outside)

    loaded = load_files(tmp_path, ["model/orders.yml"])

    (diagnostic,) = loaded.diagnostics
    assert diagnostic.rule == "confinement"
    assert "hunter2" not in diagnostic.render()


def test_the_brace_escape_is_a_literal_brace(tmp_path):
    loaded = _load(
        tmp_path,
        cube(
            "orders",
            dimensions=[
                {"name": "tag", "sql": "'\\{' || {CUBE}.tag || '\\}'", "type": "string"}
            ],
        ),
    )

    assert loaded.errors == []


def test_context_variables_are_not_unresolved_references(tmp_path):
    loaded = _load(
        tmp_path,
        cube(
            "orders",
            dimensions=[
                {
                    "name": "local_at",
                    "sql": "{SQL_UTILS.convertTz(CUBE.ordered_at)}",
                    "type": "time",
                },
                {
                    "name": "tenant",
                    "sql": "{SECURITY_CONTEXT.tenant_id}",
                    "type": "string",
                },
            ],
        ),
    )

    assert loaded.errors == []


# --- compiled-only ----------------------------------------------------------------


def test_compiled_only_files_and_elements_are_named_and_never_translated(tmp_path):
    names = write_layer(tmp_path, *ORDERS_LAYER)
    names += write(
        tmp_path,
        "model/templated.yml",
        raw="cubes:\n  - name: t\n    sql_table: {{ env('T') }}\n",
    )
    names += write(
        tmp_path, "model/legacy.js", raw="cube(`legacy`, { sql_table: `x` });\n"
    )
    names += write(
        tmp_path,
        "model/child.yml",
        cube("base_orders"),
        {
            "name": "eu_orders",
            "extends": "base_orders",
            "sql_table": "demo.main.eu_orders",
        },
    )

    loaded = load_files(tmp_path, names)

    assert {c.name for c in loaded.cubes} == {"orders", "customers", "base_orders"}
    assert [(i.file, i.element) for i in loaded.compiled_only] == [
        ("model/templated.yml", None),
        ("model/legacy.js", None),
        ("model/child.yml", "cubes['eu_orders']"),
    ]
    assert loaded.errors == []
    notes = loaded.notes()
    assert any(n.startswith("model/legacy.js: compiled-only") for n in notes)


def test_a_join_to_a_cube_only_a_compiled_only_file_could_define_is_a_warning(tmp_path):
    names = write_layer(
        tmp_path,
        cube(
            "orders",
            joins=[
                {
                    "name": "legacy",
                    "sql": "{CUBE}.l = {legacy}.id",
                    "relationship": "many_to_one",
                }
            ],
        ),
    )
    names += write(tmp_path, "model/legacy.js", raw="cube(`legacy`, {});\n")

    loaded = load_files(tmp_path, names)

    assert [c.name for c in loaded.cubes] == ["orders"]
    (diagnostic,) = [d for d in loaded.diagnostics if d.rule == "join_target"]
    assert diagnostic.severity == "warning"
    assert "compiled-only" in diagnostic.message


def test_validating_contents_runs_the_same_rules_without_a_file(tmp_path):
    loaded = validate_model_contents(
        {"model/a.yml": "cubes:\n  - name: a\n    sql: SELECT 1\n    sql_table: t\n"}
    )

    assert {d.rule for d in loaded.errors} == {"sql_xor_sql_table"}
    assert not any(tmp_path.iterdir())


def test_reading_writes_nothing(tmp_path):
    layer = source(
        tmp_path,
        *ORDERS_LAYER,
        views=[view("sales", {"join_path": "orders", "includes": "*"})],
    )
    before = {p: p.stat().st_mtime_ns for p in tmp_path.rglob("*")}

    layer.semantic_catalog()
    layer.declared_definitions()
    layer.semantic_layer()

    assert {p: p.stat().st_mtime_ns for p in tmp_path.rglob("*")} == before
