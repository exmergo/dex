"""`transform plan --scaffold`: staging skeletons from the exploration cache."""

from __future__ import annotations

import json
from pathlib import Path

from exmergo_dex_core.cli import main


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
