"""`transform plan --scaffold`: staging skeletons from the exploration cache."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from exmergo_dex_core.cli import main
from exmergo_dex_core.transform.scaffold import ScaffoldError, _merge_sources


def test_merge_preserves_existing_source_properties():
    original = """version: 2
# User configuration
sources:
  - name: main
    schema: main
    database: raw_db
    tables:
      - name: customers
        identifier: raw_customers
        description: Customer records
        columns:
          - name: id
            tests: [unique]
"""
    merged = _merge_sources(original, {"main": {"events"}})
    assert merged.replace("      - name: events\n", "") == original
    assert (
        yaml.safe_load(merged)["sources"][0]["tables"][1]["identifier"]
        == "raw_customers"
    )


def test_merge_preserves_sources_sharing_a_schema():
    original = """version: 2
sources:
  - name: app
    schema: main
    tables:
      - name: customers
  - name: billing
    schema: main
    tables:
      - name: invoices
"""
    merged = _merge_sources(original, {"main": {"events"}})
    sources = yaml.safe_load(merged)["sources"]
    assert sources[1:] == yaml.safe_load(original)["sources"]
    assert sources[0]["name"] == "main"
    assert original[original.index("  - name: app") :] in merged


def test_merge_existing_table_preserves_order_and_comments():
    original = """version: 2
sources:
  - name: main # keep this
    tables:
      - name: orders
      - name: customers
"""
    assert _merge_sources(original, {"main": {"customers"}}) == original


@pytest.mark.parametrize(
    "original",
    [
        "version: 2\nsources: []\n",
        "version: 2\n",
        "version: 2\nsources:\n  - name: main\n",
        "version: 2\nsources:\n  - name: main\n    tables: []\n",
    ],
)
def test_merge_empty_declarations(original):
    merged = _merge_sources(original, {"main": {"events"}})
    assert yaml.safe_load(merged)["sources"][0]["tables"] == [{"name": "events"}]


def test_merge_refuses_invalid_yaml():
    with pytest.raises(ScaffoldError, match="invalid YAML"):
        _merge_sources("version: 2\nsources: [\n", {"main": {"events"}})


def test_merge_missing_tables_does_not_modify_next_source():
    original = "version: 2\nsources:\n  - name: main\n  - name: other\n    tables: []\n"
    parsed = yaml.safe_load(_merge_sources(original, {"main": {"events"}}))
    assert parsed["sources"] == [
        {"name": "main", "tables": [{"name": "events"}]},
        {"name": "other", "tables": []},
    ]


def test_merge_refuses_alias_mutation():
    original = (
        "version: 2\nsources:\n  - name: main\n    tables: &tables\n"
        "      - name: customers\n  - name: other\n    tables: *tables\n"
    )
    with pytest.raises(ScaffoldError, match="anchors or aliases"):
        _merge_sources(original, {"main": {"events"}})


def _run(argv: list[str], capsys) -> tuple[int, dict]:
    rc = main(argv)
    out = capsys.readouterr().out
    assert out.count("\n") == 1, "exactly one line on stdout"
    return rc, json.loads(out)


def _seed_cache(tmp_path: Path, duckdb_file: Path, capsys) -> None:
    rc, envelope = _run(
        ["--repo-root", str(tmp_path), "--path", str(duckdb_file), "explore", "map"],
        capsys,
    )
    assert rc == 0 and envelope["status"] == "ok"


def test_scaffold_builds_staging_skeletons_with_pii_meta(
    dbt_project_dir: Path, duckdb_file: Path, tmp_path: Path, capsys
):
    _seed_cache(tmp_path, duckdb_file, capsys)
    rc, envelope = _run(
        [
            "--repo-root",
            str(tmp_path),
            "transform",
            "plan",
            "scaffold staging",
            "--scaffold",
            "customers",
            "--scaffold",
            "orders",
        ],
        capsys,
    )
    assert rc == 0, envelope
    assert envelope["status"] == "ok"
    paths = set(envelope["data"]["paths"])
    assert {
        "models/staging/_dex_sources.yml",
        "models/staging/stg_customers.sql",
        "models/staging/stg_customers.yml",
        "models/staging/stg_orders.sql",
        "models/staging/stg_orders.yml",
    } <= paths

    by_path = {d["path"]: d for d in envelope["diffs"]}
    orders_sql = by_path["models/staging/stg_orders.sql"]["unified"]
    assert "source('main', 'orders')" in orders_sql
    assert "customer_id" in orders_sql

    customers_yml = by_path["models/staging/stg_customers.yml"]["unified"]
    # The PII flag propagates into column meta as (category, confidence-derived
    # flag), never as an example value.
    assert "contains_pii: true" in customers_yml
    assert "pii_category: email" in customers_yml
    # No value from the warehouse appears in anything the scaffold GENERATED
    # (the fixture's own hand-written model legitimately shows on the removed
    # side of its replacement diff; that is repo content, not cache content).
    generated = [
        line
        for diff in envelope["diffs"]
        for line in diff["unified"].splitlines()
        if line.startswith("+")
    ]
    assert not any("a@example.com" in line for line in generated)

    # Key tests land on the candidate key.
    assert "unique" in customers_yml and "not_null" in customers_yml

    # Still a plan: nothing written into the project.
    assert not (dbt_project_dir / "models/staging/stg_orders.sql").exists()


def test_overridden_column_gets_no_pii_meta():
    """A column a human cleared via pii_overrides carries pii=None (with the
    audit field set), so the scaffold stamps no contains_pii, at either level."""

    from exmergo_dex_core.cache import ColumnProfile, Dataset
    from exmergo_dex_core.transform.scaffold import model_edits

    dataset = Dataset(
        identifier="db.main.region",
        columns=[
            ColumnProfile(name="r_regionkey", data_type="INTEGER", nullable=False),
            ColumnProfile(
                name="r_name",
                data_type="VARCHAR",
                pii=None,
                pii_overridden="name",
            ),
        ],
    )
    yaml_edit = next(e for e in model_edits(dataset) if e.path.endswith(".yml"))
    assert "contains_pii" not in yaml_edit.new_content
    assert "pii_category" not in yaml_edit.new_content


def test_scaffolded_plan_applies_cleanly(
    dbt_project_dir: Path, duckdb_file: Path, tmp_path: Path, capsys
):
    _seed_cache(tmp_path, duckdb_file, capsys)
    rc, envelope = _run(
        [
            "--repo-root",
            str(tmp_path),
            "transform",
            "plan",
            "scaffold",
            "--scaffold",
            "orders",
        ],
        capsys,
    )
    plan_id = envelope["data"]["plan_id"]
    rc, envelope = _run(
        ["--repo-root", str(tmp_path), "transform", "apply", plan_id], capsys
    )
    assert rc == 0, envelope
    assert envelope["status"] == "ok"
    assert (dbt_project_dir / "models/staging/stg_orders.sql").is_file()
    assert (dbt_project_dir / "models/staging/_dex_sources.yml").is_file()


def test_scaffold_without_cache_is_a_clean_error(
    dbt_project_dir: Path, tmp_path: Path, capsys
):
    rc, envelope = _run(
        [
            "--repo-root",
            str(tmp_path),
            "transform",
            "plan",
            "x",
            "--scaffold",
            "orders",
        ],
        capsys,
    )
    assert rc == 1
    assert envelope["status"] == "error"
    assert "explore map" in envelope["errors"][0]


def test_scaffold_sequential_calls_keep_earlier_sources(
    dbt_project_dir: Path, duckdb_file: Path, tmp_path: Path, capsys
):
    """Scaffolding sources one table per call must not drop earlier tables.

    Regression for the shared sources file being reprinted from only the
    current call's tables (#439): a second `--scaffold` call for a different
    table used to overwrite the file rather than add to it.
    """

    _seed_cache(tmp_path, duckdb_file, capsys)
    rc, envelope = _run(
        [
            "--repo-root",
            str(tmp_path),
            "transform",
            "plan",
            "scaffold customers",
            "--scaffold",
            "customers",
        ],
        capsys,
    )
    assert rc == 0, envelope
    rc, envelope = _run(
        [
            "--repo-root",
            str(tmp_path),
            "transform",
            "apply",
            envelope["data"]["plan_id"],
        ],
        capsys,
    )
    assert rc == 0, envelope

    rc, envelope = _run(
        [
            "--repo-root",
            str(tmp_path),
            "transform",
            "plan",
            "scaffold orders",
            "--scaffold",
            "orders",
        ],
        capsys,
    )
    assert rc == 0, envelope
    by_path = {d["path"]: d for d in envelope["diffs"]}
    sources_diff = by_path["models/staging/_dex_sources.yml"]["unified"]
    # The new table is the only addition; the earlier one is untouched context,
    # not removed and re-added.
    assert "      - name: customers" in sources_diff
    assert "+      - name: orders" in sources_diff
    assert "-      - name: customers" not in sources_diff

    rc, envelope = _run(
        [
            "--repo-root",
            str(tmp_path),
            "transform",
            "apply",
            envelope["data"]["plan_id"],
        ],
        capsys,
    )
    assert rc == 0, envelope
    content = (dbt_project_dir / "models/staging/_dex_sources.yml").read_text(
        encoding="utf-8"
    )
    assert "- name: customers" in content
    assert "- name: orders" in content


def test_scaffold_already_declared_table_is_a_no_op_on_sources(
    dbt_project_dir: Path, duckdb_file: Path, tmp_path: Path, capsys
):
    """Re-scaffolding a table the shared sources file already declares must not
    even show up as a reordering: no diff for that file at all."""

    _seed_cache(tmp_path, duckdb_file, capsys)
    rc, envelope = _run(
        [
            "--repo-root",
            str(tmp_path),
            "transform",
            "plan",
            "scaffold customers",
            "--scaffold",
            "customers",
        ],
        capsys,
    )
    assert rc == 0, envelope
    rc, envelope = _run(
        [
            "--repo-root",
            str(tmp_path),
            "transform",
            "apply",
            envelope["data"]["plan_id"],
        ],
        capsys,
    )
    assert rc == 0, envelope

    rc, envelope = _run(
        [
            "--repo-root",
            str(tmp_path),
            "transform",
            "plan",
            "scaffold customers again",
            "--scaffold",
            "customers",
        ],
        capsys,
    )
    assert rc == 0, envelope
    paths = {d["path"] for d in envelope["diffs"]}
    assert "models/staging/_dex_sources.yml" not in paths


def test_scaffold_unknown_table_is_a_clean_error(
    dbt_project_dir: Path, duckdb_file: Path, tmp_path: Path, capsys
):
    _seed_cache(tmp_path, duckdb_file, capsys)
    rc, envelope = _run(
        ["--repo-root", str(tmp_path), "transform", "plan", "x", "--scaffold", "nope"],
        capsys,
    )
    assert rc == 1
    assert envelope["status"] == "error"
