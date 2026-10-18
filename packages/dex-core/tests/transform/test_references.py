"""`transform references`: where a name is used, and whether dex believes itself.

The acceptance criteria of the issue this implements are the first five tests,
named after what they assert rather than after the machinery. The rest cover the
edges that make the difference between a report and a report worth acting on: the
two-argument `ref()` the old regex misread, the sanitizer boundary a column called
`api_key` walks straight into, and the seed rows that must never be read.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from exmergo_dex_core.cli import main
from exmergo_dex_core.dbt_project import load
from exmergo_dex_core.edits import EditOp
from exmergo_dex_core.references import ReferenceIndex, find_references
from exmergo_dex_core.transform.propagate import PropagationRefusedError, propagate


def _run(argv: list[str], capsys) -> tuple[int, dict]:
    rc = main(argv)
    out = capsys.readouterr().out
    assert out.count("\n") == 1, "exactly one line on stdout"
    return rc, json.loads(out)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A project carrying one of every indirection the issue names.

    Deliberately not the shared `dbt_project_dir`: this feature is about what a
    grep misses, so the fixture has to contain the things a grep misses. A var
    read in three places, a macro that reads it, a column named only in a test, a
    `ref()` composed from a var, and a package shipping a model the project
    shadows.
    """

    root = tmp_path / "analytics"
    _write(
        root / "dbt_project.yml",
        "name: analytics\n"
        'version: "1.0.0"\n'
        "profile: analytics\n"
        "vars:\n"
        "  using_department: true\n"
        "models:\n"
        "  analytics:\n"
        "    +vars:\n"
        "      scoped_var: 1\n",
    )
    _write(root / "packages.yml", "packages:\n  - package: acme/utils\n")
    _write(
        root / "models" / "stg_departments.sql",
        "select\n"
        "    department_id,\n"
        "    department_name\n"
        "from {{ source('raw', 'departments') }}\n",
    )
    _write(
        root / "models" / "fct_orders.sql",
        "{{ config(materialized='table') }}\n"
        "select\n"
        "    o.order_id,\n"
        "    {% if var('using_department') %}\n"
        "    d.department_name,\n"
        "    {% endif %}\n"
        "    {{ dept_label('d.department_name') }} as label\n"
        "from {{ ref('stg_departments') }} d\n"
        "join {{ ref(var('orders_model')) }} o on o.department_id = d.department_id\n",
    )
    _write(
        root / "models" / "schema.yml",
        "version: 2\n"
        "models:\n"
        "  - name: stg_departments\n"
        "    columns:\n"
        "      - name: department_id\n"
        "        tests:\n"
        "          - unique\n"
        "          - relationships:\n"
        "              to: ref('fct_orders')\n"
        "              field: order_ref_column\n"
        "      - name: department_name\n"
        "sources:\n"
        "  - name: raw\n"
        "    tables:\n"
        "      - name: departments\n"
        "        columns:\n"
        "          - name: department_name\n"
        "semantic_models:\n"
        "  - name: departments\n"
        "    model: ref('stg_departments')\n"
        "    entities:\n"
        "      - name: department\n"
        "        expr: department_id\n"
        "    dimensions:\n"
        "      - name: dept_label\n"
        '        expr: "upper(department_name)"\n'
        "    measures:\n"
        "      - name: dept_count\n"
        "        expr: department_id\n"
        "metrics:\n"
        "  - name: departments_total\n"
        "    type: simple\n"
        "    type_params:\n"
        "      measure: dept_count\n",
    )
    _write(
        root / "macros" / "dept_label.sql",
        "{% macro dept_label(column) %}\n"
        "    case when {{ var('using_department') }} then {{ column }} end\n"
        "{% endmacro %}\n",
    )
    _write(
        root / "seeds" / "dept_codes.csv",
        "department_id,api_key\n1,secret-value-one\n2,secret-value-two\n",
    )
    _write(
        root / "dbt_packages" / "utils" / "dbt_project.yml",
        "name: utils\nprofile: utils\n",
    )
    _write(
        root / "dbt_packages" / "utils" / "models" / "stg_departments.sql",
        "select department_id from {{ source('raw', 'departments') }}\n",
    )
    return root


@pytest.fixture
def index(project: Path) -> ReferenceIndex:
    return ReferenceIndex(load(project))


# --- the acceptance criteria -----------------------------------------------------


def test_a_var_used_in_a_model_a_macro_and_the_project_yml_returns_all_three(index):
    hits, _limits = index.references_to("using_department", "var")
    assert {hit.path for hit in hits} == {
        "dbt_project.yml",
        "macros/dept_label.sql",
        "models/fct_orders.sql",
    }
    # The model's read is inside an `{% if %}`, and the macro's is inside a macro
    # body. Neither is reachable by reading the rendered SQL, which is the whole
    # reason this command exists rather than a grep.
    assert {hit.form for hit in hits} == {"project_yml_var", "var_call"}


def test_a_column_named_only_in_a_schema_yml_test_is_found(index):
    hits, _limits = index.references_to("order_ref_column", "column")
    assert [(hit.path, hit.form) for hit in hits] == [
        ("models/schema.yml", "yaml_test_column")
    ]


def test_a_dynamically_composed_ref_is_unresolved_rather_than_dropped(index):
    unresolved = index.indeterminate_for("model")
    assert [(u.path, u.line, u.form) for u in unresolved] == [
        ("models/fct_orders.sql", 9, "ref_call")
    ]
    # It carries no name on purpose: dex read a reference it could not resolve,
    # which is a fact about that call site and not about any one target.
    assert unresolved[0].name is None
    assert unresolved[0].resolution == "indeterminate"


def test_a_package_override_is_reported_as_both(index):
    hits, _limits = index.references_to("stg_departments", "model")
    definitions = {hit.path: hit for hit in hits if hit.form == "definition"}
    assert set(definitions) == {
        "models/stg_departments.sql",
        "dbt_packages/utils/models/stg_departments.sql",
    }
    assert "shadows" in definitions["models/stg_departments.sql"].note
    assert (
        "shadowed" in definitions["dbt_packages/utils/models/stg_departments.sql"].note
    )


def test_the_report_states_whether_it_believes_itself_complete(project):
    view = load(project)
    # The project holds one unresolvable `ref()`, so a model query cannot be
    # complete and must not claim to be.
    models = find_references(view, ["stg_departments"], kind="model")
    assert models.completeness == "incomplete"
    assert models.indeterminate
    assert any("could not be resolved" in limit for limit in models.limits)

    # A var query has nothing standing in its way, and says so.
    variables = find_references(view, ["using_department"], kind="var")
    assert variables.completeness == "complete"
    assert variables.limits == []


def test_unparseable_yaml_limits_references_and_refuses_rewrites(tmp_path):
    root = tmp_path / "analytics"
    _write(root / "dbt_project.yml", "name: analytics\nprofile: analytics\n")
    _write(root / "models" / "stg_x.sql", "select 1 as id\n")
    _write(root / "models" / "broken.yml", "models: [\n")
    view = load(root)

    report = find_references(view, ["stg_x"], kind="model")
    assert report.completeness == "incomplete"
    assert any("models/broken.yml" in limit for limit in report.limits)
    with pytest.raises(PropagationRefusedError, match=r"models/broken\.yml"):
        propagate(view, root, "model", "stg_x", "stg_y")
    with pytest.raises(PropagationRefusedError, match=r"models/broken\.yml"):
        propagate(view, root, "model", "stg_x")


# --- resolution edges ------------------------------------------------------------


def test_a_version_keyword_on_ref_resolves_to_the_model(project):
    """`ref('model', v=1)` resolves to the model. The version keyword sits
    beside the name."""

    _write(
        project / "models" / "uses_version.sql",
        "select * from {{ ref('stg_departments', v=1) }}\n"
        "union all\n"
        "select * from {{ ref('stg_departments', version=1) }}\n",
    )
    index = ReferenceIndex(load(project))
    hits, _limits = index.references_to("stg_departments", "model")
    assert {
        (hit.path, hit.line, hit.form)
        for hit in hits
        if hit.path == "models/uses_version.sql"
    } == {
        ("models/uses_version.sql", 1, "ref_call"),
        ("models/uses_version.sql", 3, "ref_call"),
    }
    assert [
        item
        for item in index.indeterminate_for("model")
        if item.path == "models/uses_version.sql"
    ] == []


def test_a_two_argument_ref_resolves_to_the_model_not_the_package(tmp_path, project):
    _write(
        project / "models" / "uses_package.sql",
        "select * from {{ ref('utils', 'stg_departments') }}\n",
    )
    hits, _limits = ReferenceIndex(load(project)).references_to(
        "stg_departments", "model"
    )
    assert ("models/uses_package.sql", "ref_call") in {
        (hit.path, hit.form) for hit in hits
    }
    # The regex this replaced captured the first argument, so the same line used
    # to resolve to a model called `utils` that does not exist.
    assert not ReferenceIndex(load(project)).references_to("utils", "model")[0]


def test_a_computed_semantic_expr_is_unresolved_not_absent(index):
    unresolved = index.indeterminate_for("column")
    assert [(u.path, u.form) for u in unresolved] == [
        ("models/schema.yml", "semantic_expr")
    ]
    assert "computed expr" in unresolved[0].note


def test_a_semantic_model_and_a_metric_reach_their_own_kinds(index):
    assert index.kinds_of("department") == ["entity"]
    assert index.kinds_of("dept_count") == ["measure"]
    assert index.kinds_of("departments_total") == ["metric"]
    measures, _limits = index.references_to("dept_count", "measure")
    assert {hit.form for hit in measures} == {
        "semantic_definition",
        "metric_input_measure",
    }


def test_a_generic_test_invocation_is_a_macro_reference(index):
    hits, _limits = index.references_to("unique", "macro")
    assert [(hit.path, hit.form) for hit in hits] == [
        ("models/schema.yml", "yaml_test_ref")
    ]


def test_a_column_passed_to_a_macro_as_a_string_is_found(index):
    hits, _limits = index.references_to("department_name", "column")
    macro_args = [hit for hit in hits if hit.form == "macro_arg_column"]
    assert [(hit.path, hit.line) for hit in macro_args] == [
        ("models/fct_orders.sql", 7)
    ]
    assert "matched by name" in macro_args[0].note


def test_a_dotted_name_reads_as_a_source_before_a_qualified_column(index):
    assert index.kinds_of("raw.departments") == ["source"]


def test_a_qualified_column_separates_lineage_from_the_same_name_elsewhere(
    project,
):
    _write(
        project / "models" / "unrelated.sql",
        "select department_name from {{ source('raw', 'departments') }}\n",
    )
    index = ReferenceIndex(load(project))
    hits, _limits = index.references_to("stg_departments.department_name", "column")
    scopes = {hit.path: hit.note for hit in hits if hit.form == "select_column"}
    assert scopes["models/stg_departments.sql"] == "lineage_resolved"
    assert scopes["models/fct_orders.sql"] == "lineage_resolved"
    # `unrelated` never ref()s stg_departments, so its column of the same name is
    # reported and marked rather than silently folded in or silently dropped.
    assert scopes["models/unrelated.sql"] == "same_name_elsewhere"

    # A shared schema.yml is not a node, so lineage is answered from the entry a
    # column is documented under and not from the file's name. Both of these live
    # in the same file: one documents the model, the other documents the source.
    yaml_hits = sorted(
        (hit.line, hit.note) for hit in hits if hit.form == "yaml_column"
    )
    # The first is documented under `models: - name: stg_departments`, the second
    # under the `raw.departments` source. Same file, same column name, different
    # answers, and the file's own name says nothing about either.
    assert [note for _line, note in yaml_hits] == [
        "lineage_resolved",
        "same_name_elsewhere",
    ]


def test_kinds_are_reported_without_being_asked_for(index):
    # `dept_label` is a macro and a dimension. A caller who had to pass --kind
    # would have to already know that, which is the thing they are asking about.
    assert index.kinds_of("dept_label") == ["macro", "dimension"]


# --- the guardrails --------------------------------------------------------------


def test_a_seed_contributes_its_header_and_never_a_data_row(index, project):
    hits, _limits = index.references_to("api_key", "column")
    assert [(hit.path, hit.form) for hit in hits] == [
        ("seeds/dept_codes.csv", "seed_header")
    ]
    every_name = {
        reference.name
        for references in index._by_name.values()
        for reference in references
    }
    assert not any(name and "secret-value" in name for name in every_name), (
        "a seed's data rows are project data and never enter the index"
    )


def test_a_target_named_like_a_secret_survives_the_envelope_sanitizer(
    project, tmp_path, capsys
):
    # The sanitizer matches *key* names against secret-like substrings and raises
    # rather than redacting, so a column legitimately called `api_key` would take
    # the whole command down if names were ever used as JSON keys.
    rc, envelope = _run(
        ["--repo-root", str(tmp_path), "transform", "references", "api_key"],
        capsys,
    )
    assert rc == 0
    assert envelope["data"]["targets"][0]["name"] == "api_key"


def test_the_report_opens_no_connection(project, tmp_path, capsys, monkeypatch):
    from exmergo_dex_core.engine import DexEngine

    monkeypatch.setattr(
        DexEngine,
        "_adapter",
        lambda *a, **k: pytest.fail("references opened a connection"),
    )
    rc, _envelope = _run(
        ["--repo-root", str(tmp_path), "transform", "references", "using_department"],
        capsys,
    )
    assert rc == 0


# --- the payload -----------------------------------------------------------------


def test_the_cap_binds_is_counted_and_is_lifted_by_full(project):
    for index_number in range(60):
        _write(
            project / "models" / f"gen_{index_number}.sql",
            "select department_id from {{ ref('stg_departments') }}\n",
        )
    view = load(project)
    capped = find_references(view, ["department_id"], kind="column")
    assert capped.notes and "not listed" in capped.notes[0]
    assert any("capped" in limit or "not listed" in limit for limit in capped.notes)
    assert len(capped.targets[0]["files"]) <= 50

    full = find_references(view, ["department_id"], kind="column", full=True)
    assert full.notes == []
    assert len(full.targets[0]["files"]) > 50


def test_the_verdict_comes_before_the_occurrences(project, tmp_path, capsys):
    rc, envelope = _run(
        ["--repo-root", str(tmp_path), "transform", "references", "using_department"],
        capsys,
    )
    assert rc == 0
    keys = list(envelope["data"])
    assert keys[:4] == ["completeness", "limits", "indeterminate", "targets"]


def test_a_name_nothing_uses_says_so_rather_than_returning_nothing(
    project, tmp_path, capsys
):
    rc, envelope = _run(
        ["--repo-root", str(tmp_path), "transform", "references", "no_such_thing"],
        capsys,
    )
    assert rc == 0
    assert envelope["data"]["targets"] == [
        {"name": "no_such_thing", "kinds": [], "found": False, "files": []}
    ]


def test_an_unknown_kind_refuses_with_an_envelope_naming_the_kinds(
    project, tmp_path, capsys
):
    # argparse `choices` would exit before an envelope existed, and every command
    # owes the caller exactly one line of JSON.
    rc, envelope = _run(
        [
            "--repo-root",
            str(tmp_path),
            "transform",
            "references",
            "x",
            "--kind",
            "nonsense",
        ],
        capsys,
    )
    assert rc == 1
    assert envelope["status"] == "error"
    assert envelope["reason"] == "request"
    assert "measure" in envelope["errors"][0]


def test_several_names_are_answered_in_one_call(project, tmp_path, capsys):
    rc, envelope = _run(
        [
            "--repo-root",
            str(tmp_path),
            "transform",
            "references",
            "using_department",
            "stg_departments",
        ],
        capsys,
    )
    assert rc == 0
    assert {target["name"] for target in envelope["data"]["targets"]} == {
        "using_department",
        "stg_departments",
    }


def test_declared_but_uninstalled_packages_make_the_report_incomplete(tmp_path):
    root = tmp_path / "analytics"
    _write(
        root / "dbt_project.yml",
        'name: analytics\nversion: "1.0.0"\nprofile: analytics\n',
    )
    _write(root / "packages.yml", "packages:\n  - package: acme/utils\n")
    _write(root / "models" / "a.sql", "select 1 as id\n")
    report = find_references(load(root), ["id"], kind="column")
    assert report.completeness == "incomplete"
    assert any("not installed" in limit for limit in report.limits)


# --- what dbt resolves that dex does not edit --------------------------------------


def test_names_dbt_resolves_without_an_editable_file_are_declared_not_defined(project):
    """A Python model, a versioned model's base name and a YAML snapshot exist to
    dbt, and none has a file a rename could edit. `declarations_of` reports them
    for a caller asking whether a name exists; every query `transform references`
    and the rename guards answer from stays exactly as it was.
    """

    _write(
        project / "models" / "py_orders.py",
        "def model(dbt, session):\n    return dbt.ref('stg_departments')\n",
    )
    _write(project / "models" / "dim_orders_v1.sql", "select 1 as id\n")
    _write(
        project / "models" / "versions.yml",
        "version: 2\nmodels:\n  - name: dim_orders\n    versions:\n      - v: 1\n",
    )
    _write(
        project / "snapshots" / "snapshots.yml",
        "snapshots:\n"
        "  - name: orders_snapshot\n"
        "    relation: source('raw', 'departments')\n",
    )
    index = ReferenceIndex(load(project))

    node_kinds = ("model", "seed", "snapshot")
    assert {
        name: [
            (d.path, d.form, d.kind) for d in index.declarations_of(name, node_kinds)
        ]
        for name in ("py_orders", "dim_orders", "orders_snapshot")
    } == {
        "py_orders": [("models/py_orders.py", "python_model", "model")],
        "dim_orders": [("models/versions.yml", "yaml_model_declaration", "model")],
        "orders_snapshot": [
            ("snapshots/snapshots.yml", "yaml_snapshot_declaration", "snapshot")
        ],
    }
    for name in ("py_orders", "dim_orders", "orders_snapshot"):
        assert index.node_kind(name) is None
        for kind in node_kinds:
            assert index.definitions_of(name, kind) == []
            hits, _limits = index.references_to(name, kind)
            # The YAML entries were always read as entries; nothing new appears.
            assert {hit.form for hit in hits} <= {"yaml_model_entry"}
    assert index.kinds_of("py_orders") == []

    # A semantic model's own declarations are found the same way, and are still
    # the references `transform references` has always reported them as.
    (measure,) = index.declarations_of("dept_count", ("measure",))
    assert (measure.path, measure.form) == ("models/schema.yml", "semantic_definition")
    assert index.definitions_of("dept_count", "measure") == []


def test_a_yaml_snapshot_relation_is_an_occurrence(tmp_path):
    """A dbt 1.9 snapshot's `relation:` names a source or a model.

    The key is also what declares the snapshot. Recording only the declaration
    drops the relation, so a query for `raw.orders` never sees this file.
    """

    root = tmp_path / "analytics"
    _write(
        root / "dbt_project.yml",
        'name: analytics\nversion: "1.0.0"\nprofile: analytics\n',
    )
    _write(root / "models" / "stg_departments.sql", "select 1 as id\n")
    _write(
        root / "snapshots" / "snapshots.yml",
        "snapshots:\n"
        "  - name: orders_snapshot\n"
        "    relation: source('raw', 'orders')\n"
        "  - name: dept_snapshot\n"
        "    relation: ref('stg_departments')\n",
    )
    index = ReferenceIndex(load(root))

    sources, _limits = index.references_to("raw.orders", "source")
    assert [(hit.path, hit.line, hit.form, hit.kind) for hit in sources] == [
        ("snapshots/snapshots.yml", 3, "yaml_snapshot_relation", "source")
    ]
    relation = [
        hit
        for hit in index.references_to("stg_departments", "model")[0]
        if hit.form == "yaml_snapshot_relation"
    ]
    assert [(hit.path, hit.line, hit.kind) for hit in relation] == [
        ("snapshots/snapshots.yml", 5, "model")
    ]


def test_dynamic_yaml_snapshot_relations_limit_only_their_reference_kinds(tmp_path):
    root = tmp_path / "analytics"
    _write(
        root / "dbt_project.yml",
        'name: analytics\nversion: "1.0.0"\nprofile: analytics\n',
    )
    _write(root / "models" / "orders.sql", "select 1 as id\n")
    _write(
        root / "snapshots" / "snapshots.yml",
        "snapshots:\n"
        "  - name: dynamic_source\n"
        "    relation: source('raw', var('table_name'))\n"
        "  - name: dynamic_model\n"
        "    relation: ref(var('model_name'))\n"
        "  - name: literal_source\n"
        "    relation: source('raw', 'orders')\n"
        "  - name: literal_model\n"
        "    relation: ref('orders')\n",
    )
    view = load(root)
    index = ReferenceIndex(view)
    for kind, name, line in (("source", "raw.orders", 3), ("model", "orders", 5)):
        report = find_references(view, [name], kind=kind, index=index)
        assert report.completeness == "incomplete"
        unresolved = [
            (hit.path, hit.line, hit.kind, hit.name) for hit in report.indeterminate
        ]
        assert unresolved == [("snapshots/snapshots.yml", line, kind, None)]
        assert any("could not be resolved" in limit for limit in report.limits)
        assert any(
            hit.form == "yaml_snapshot_relation"
            for hit in index.references_to(name, kind)[0]
        )
    seed_report = find_references(view, ["orders"], kind="seed", index=index)
    assert seed_report.completeness == "complete"


def test_a_python_model_lists_its_ref_and_source_reads(tmp_path):
    """Literal `dbt.ref` and `dbt.source` calls are occurrences.

    A comment and a string that look like calls are not. When every call is a
    literal, a model query can still call itself complete.
    """

    root = tmp_path / "analytics"
    _write(
        root / "dbt_project.yml",
        'name: analytics\nversion: "1.0.0"\nprofile: analytics\n',
    )
    _write(root / "models" / "stg_orders.sql", "select 1 as id\n")
    _write(root / "seeds" / "country_codes.csv", "code\nIT\n")
    _write(
        root / "models" / "py_orders.py",
        "def model(dbt, session):\n"
        "    # dbt.ref('ghost')\n"
        "    note = \"dbt.source('raw', 'ghost')\"\n"
        '    orders = dbt.ref("stg_orders")\n'
        "    raw = dbt.source('raw', 'orders')\n"
        "    packaged = dbt.ref('utils', \"stg_orders\")\n"
        "    codes = dbt.ref('country_codes')\n"
        "    return orders\n",
    )
    index = ReferenceIndex(load(root))

    python_refs = [
        hit
        for hit in index.references_to("stg_orders", "model")[0]
        if hit.form == "python_ref"
    ]
    assert [(hit.path, hit.line, hit.qualifier) for hit in python_refs] == [
        ("models/py_orders.py", 4, None),
        ("models/py_orders.py", 6, "utils"),
    ]
    sources, _limits = index.references_to("raw.orders", "source")
    assert [(hit.path, hit.line, hit.form) for hit in sources] == [
        ("models/py_orders.py", 5, "python_source")
    ]
    seeds, _limits = index.references_to("country_codes", "seed")
    assert [(hit.path, hit.line, hit.form, hit.kind) for hit in seeds] == [
        ("seeds/country_codes.csv", 1, "definition", "seed"),
        ("models/py_orders.py", 7, "python_ref", "seed"),
    ]
    assert index.references_to("ghost", "model")[0] == []
    assert index.references_to("raw.ghost", "source")[0] == []
    assert index.parents_of("py_orders") == {"stg_orders", "country_codes"}

    report = find_references(load(root), ["stg_orders"], kind="model")
    assert report.completeness == "complete"
    assert report.limits == []


def test_an_unresolved_python_ref_makes_model_queries_incomplete(tmp_path):
    root = tmp_path / "analytics"
    _write(
        root / "dbt_project.yml",
        'name: analytics\nversion: "1.0.0"\nprofile: analytics\n',
    )
    _write(root / "models" / "stg_orders.sql", "select 1 as id\n")
    _write(
        root / "models" / "py_orders.py",
        "def model(dbt, session):\n    return dbt.ref(model_name)\n",
    )
    index = ReferenceIndex(load(root))

    unresolved = index.indeterminate_for("model")
    assert [(hit.path, hit.line, hit.form, hit.name) for hit in unresolved] == [
        ("models/py_orders.py", 2, "python_ref", None)
    ]
    report = find_references(load(root), ["stg_orders"], kind="model")
    assert report.completeness == "incomplete"
    assert any("could not be resolved" in limit for limit in report.limits)


def test_a_versioned_python_ref_resolves_to_the_model(tmp_path):
    """`dbt.ref("other_model", v=1)` and `version=` name the model.

    A package-qualified call names the model too. The version keyword is not
    an unread argument.
    """

    root = tmp_path / "analytics"
    _write(
        root / "dbt_project.yml",
        'name: analytics\nversion: "1.0.0"\nprofile: analytics\n',
    )
    _write(root / "models" / "other_model.sql", "select 1 as id\n")
    _write(
        root / "models" / "py_orders.py",
        "def model(dbt, session):\n"
        '    pinned = dbt.ref("other_model", v=1)\n'
        '    named = dbt.ref("other_model", version=1)\n'
        '    packaged = dbt.ref("utils", "other_model", version=2)\n'
        "    return pinned\n",
    )
    index = ReferenceIndex(load(root))

    python_refs = [
        hit
        for hit in index.references_to("other_model", "model")[0]
        if hit.form == "python_ref"
    ]
    assert [(hit.path, hit.line, hit.qualifier) for hit in python_refs] == [
        ("models/py_orders.py", 2, None),
        ("models/py_orders.py", 3, None),
        ("models/py_orders.py", 4, "utils"),
    ]
    assert [
        item
        for item in index.indeterminate_for("model")
        if item.path == "models/py_orders.py"
    ] == []
    assert index.parents_of("py_orders") == {"other_model"}


def test_a_versioned_python_ref_does_not_block_an_unrelated_model(tmp_path):
    """A version keyword does not make every model query incomplete.

    Renaming a model the call does not name is the move of that model.
    """

    root = tmp_path / "analytics"
    _write(
        root / "dbt_project.yml",
        'name: analytics\nversion: "1.0.0"\nprofile: analytics\n',
    )
    _write(root / "models" / "stg_orders.sql", "select 1 as id\n")
    _write(root / "models" / "other_model.sql", "select 1 as id\n")
    _write(
        root / "models" / "py_orders.py",
        "def model(dbt, session):\n"
        '    pinned = dbt.ref("other_model", v=1)\n'
        '    named = dbt.ref("other_model", version=1)\n'
        '    packaged = dbt.ref("utils", "other_model", version=2)\n'
        "    return pinned\n",
    )
    view = load(root)

    report = find_references(view, ["stg_orders"], kind="model")
    assert report.completeness == "complete"
    assert report.limits == []
    result = propagate(view, root, "model", "stg_orders", "stg_bookings")
    assert [(edit.op, edit.path) for edit in result.edits] == [
        (EditOp.DELETE, "models/stg_orders.sql"),
        (EditOp.UPSERT, "models/stg_bookings.sql"),
    ]
    moved = next(
        edit for edit in result.edits if edit.path == "models/stg_bookings.sql"
    )
    assert moved.new_content == "select 1 as id\n"


def test_an_unknown_python_ref_keyword_stays_unresolved(tmp_path):
    """A keyword other than `v` or `version`, and `**kwargs`, stay unread."""

    root = tmp_path / "analytics"
    _write(
        root / "dbt_project.yml",
        'name: analytics\nversion: "1.0.0"\nprofile: analytics\n',
    )
    _write(root / "models" / "stg_orders.sql", "select 1 as id\n")
    _write(
        root / "models" / "py_orders.py",
        "def model(dbt, session):\n"
        "    dbt.ref('other_model', where='active')\n"
        "    return dbt.ref('other_model', **opts)\n",
    )
    view = load(root)
    index = ReferenceIndex(view)

    unresolved = index.indeterminate_for("model")
    assert [(hit.path, hit.line, hit.form, hit.name) for hit in unresolved] == [
        ("models/py_orders.py", 2, "python_ref", None),
        ("models/py_orders.py", 3, "python_ref", None),
    ]
    assert index.references_to("other_model", "model")[0] == []
    report = find_references(view, ["stg_orders"], kind="model")
    assert report.completeness == "incomplete"
    with pytest.raises(PropagationRefusedError, match=r"models/py_orders\.py:2"):
        propagate(view, root, "model", "stg_orders", "stg_bookings")


def test_a_dynamic_python_ref_with_a_version_stays_unresolved(tmp_path):
    """`dbt.ref(model_name, v=1)` still names nothing dex can read."""

    root = tmp_path / "analytics"
    _write(
        root / "dbt_project.yml",
        'name: analytics\nversion: "1.0.0"\nprofile: analytics\n',
    )
    _write(root / "models" / "stg_orders.sql", "select 1 as id\n")
    _write(
        root / "models" / "py_orders.py",
        "def model(dbt, session):\n    return dbt.ref(model_name, v=1)\n",
    )
    view = load(root)
    index = ReferenceIndex(view)

    unresolved = index.indeterminate_for("model")
    assert [(hit.path, hit.line, hit.form, hit.name) for hit in unresolved] == [
        ("models/py_orders.py", 2, "python_ref", None)
    ]
    report = find_references(view, ["stg_orders"], kind="model")
    assert report.completeness == "incomplete"
    with pytest.raises(PropagationRefusedError, match=r"models/py_orders\.py:2"):
        propagate(view, root, "model", "stg_orders", "stg_bookings")


def test_an_unparsed_python_model_limits_relation_queries(tmp_path):
    """A Python model dex cannot parse may hide a ref or a source.

    Model, source, seed, and snapshot queries say so. A var query did not miss
    a read by skipping the file, and stays complete.
    """

    root = tmp_path / "analytics"
    _write(
        root / "dbt_project.yml",
        'name: analytics\nversion: "1.0.0"\nprofile: analytics\n',
    )
    _write(root / "models" / "stg_orders.sql", "select 1 as id\n")
    _write(
        root / "models" / "py_orders.py",
        "def model(dbt, session)\n    return dbt.ref('stg_orders')\n",
    )
    view = load(root)

    models = find_references(view, ["stg_orders"], kind="model")
    sources = find_references(view, ["raw.orders"], kind="source")
    seeds = find_references(view, ["country_codes"], kind="seed")
    snapshots = find_references(view, ["orders_snapshot"], kind="snapshot")
    variables = find_references(view, ["using_department"], kind="var")
    assert models.completeness == "incomplete"
    assert sources.completeness == "incomplete"
    assert seeds.completeness == "incomplete"
    assert snapshots.completeness == "incomplete"
    assert any(
        "models/py_orders.py" in limit and "could not be parsed" in limit
        for limit in models.limits
    )
    assert variables.completeness == "complete"
    assert variables.limits == []
    hits, _limits = ReferenceIndex(view).references_to("stg_orders", "model")
    assert {hit.form for hit in hits} == {"definition"}


def test_a_python_model_in_a_package_is_declared_by_that_package(project):
    _write(
        project / "dbt_packages" / "utils" / "models" / "py_util.py",
        "def model(dbt, session):\n    return None\n",
    )
    (declared,) = ReferenceIndex(load(project)).declarations_of("py_util", ("model",))
    assert declared.package == "utils"
    assert declared.path == "dbt_packages/utils/models/py_util.py"


def test_a_yaml_file_that_does_not_parse_is_unread_not_a_global_limit(project):
    """Grounding uses unread files only when a definition may be missing."""

    _write(project / "models" / "broken.yml", "models: [unclosed\n")
    _write(project / "models" / "empty.yml", "")
    index = ReferenceIndex(load(project))

    assert index.unread == ["models/broken.yml"]
    assert not any("broken.yml" in limit for limit in index.limits)
