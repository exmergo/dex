"""`transform test --scaffold <model>`: dbt unit_tests: skeletons from a
model's own ref()/source() inputs (issue #215)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

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
    assert rc == 0 and envelope["status"] == "ok", envelope


def _write_customer_orders_model(dbt_project_dir: Path, sql: str) -> None:
    marts = dbt_project_dir / "models" / "marts"
    marts.mkdir(parents=True, exist_ok=True)
    (marts / "customer_orders.sql").write_text(sql, encoding="utf-8")
    # dbt refuses an unresolved source() unless it is declared; the model's
    # own SQL can name one dex has never scaffolded a declaration for.
    (marts / "_sources.yml").write_text(
        "version: 2\n"
        "sources:\n"
        "  - name: main\n"
        "    schema: main\n"
        "    tables:\n"
        "      - name: customers\n"
        "      - name: orders\n",
        encoding="utf-8",
    )


_TWO_INPUT_MODEL = (
    "select\n"
    "    c.id as customer_id,\n"
    "    o.id as order_id\n"
    "from {{ source('main', 'customers') }} c\n"
    "join {{ source('main', 'orders') }} o on o.customer_id = c.id\n"
)


def test_scaffold_produces_a_given_block_per_input_with_only_columns_read(
    dbt_project_dir: Path, duckdb_file: Path, tmp_path: Path, capsys
):
    """Acceptance: a model with two inputs produces a given block per input,
    with only the columns actually read, correctly typed."""

    _write_customer_orders_model(dbt_project_dir, _TWO_INPUT_MODEL)
    _seed_cache(tmp_path, duckdb_file, capsys)

    rc, envelope = _run(
        [
            "--repo-root",
            str(tmp_path),
            "transform",
            "test",
            "--scaffold",
            "customer_orders",
        ],
        capsys,
    )
    assert rc == 0, envelope
    assert envelope["status"] == "ok"
    assert envelope["data"]["model"] == "customer_orders"
    assert set(envelope["data"]["inputs"]) == {"customers", "orders"}
    assert envelope["data"]["paths"] == ["models/marts/test_customer_orders.yml"]

    diff = envelope["diffs"][0]["unified"]
    added = [line[1:] for line in diff.splitlines() if line.startswith("+")]
    content = "\n".join(added)

    assert "unit_tests:" in content
    assert "name: test_customer_orders" in content
    assert "model: customer_orders" in content
    assert "input: source('main', 'customers')" in content
    assert "input: source('main', 'orders')" in content

    # customers.id is read (the join key alias); customers.email is not, and
    # must not appear anywhere in the customers given block.
    customers_block = content.split("source('main', 'customers')")[1].split(
        "source('main', 'orders')"
    )[0]
    assert "id: 1" in customers_block
    assert "email" not in customers_block

    # orders.customer_id (the join predicate) and orders.id are read;
    # orders.total is not.
    orders_block = content.split("source('main', 'orders')")[1]
    assert "id: 1" in orders_block
    assert "customer_id: 1" in orders_block
    assert "total" not in orders_block

    # Never invents the expectation: the stub is empty and says so.
    assert "expect:" in content
    assert "TODO" in content
    assert "- {}" in content

    # Still a plan: nothing written into the project.
    assert not (dbt_project_dir / "models/marts/test_customer_orders.yml").exists()


def test_scaffold_warns_the_stub_expectation_fails_until_filled_in(
    dbt_project_dir: Path, duckdb_file: Path, tmp_path: Path, capsys
):
    _write_customer_orders_model(dbt_project_dir, _TWO_INPUT_MODEL)
    _seed_cache(tmp_path, duckdb_file, capsys)
    rc, envelope = _run(
        [
            "--repo-root",
            str(tmp_path),
            "transform",
            "test",
            "--scaffold",
            "customer_orders",
        ],
        capsys,
    )
    assert rc == 0, envelope
    assert any("fails until you fill in" in w for w in envelope["warnings"])


def test_scaffolded_unit_test_parses_under_dbt_and_applies_cleanly(
    dbt_project_dir: Path, duckdb_file: Path, tmp_path: Path, capsys
):
    """Acceptance: the scaffold parses under dbt (shadow-parse gates the plan
    itself, before this even runs) and, once applied, is a real project file."""

    pytest.importorskip("dbt.cli.main")
    _write_customer_orders_model(dbt_project_dir, _TWO_INPUT_MODEL)
    _seed_cache(tmp_path, duckdb_file, capsys)

    rc, envelope = _run(
        [
            "--repo-root",
            str(tmp_path),
            "transform",
            "test",
            "--scaffold",
            "customer_orders",
        ],
        capsys,
    )
    assert rc == 0, envelope
    plan_id = envelope["data"]["plan_id"]

    rc, envelope = _run(
        ["--repo-root", str(tmp_path), "transform", "apply", plan_id], capsys
    )
    assert rc == 0, envelope
    assert (dbt_project_dir / "models/marts/test_customer_orders.yml").is_file()


def test_scaffold_a_model_with_no_inputs_is_a_clean_error(
    dbt_project_dir: Path, duckdb_file: Path, tmp_path: Path, capsys
):
    _write_customer_orders_model(dbt_project_dir, "select 1 as x\n")
    _seed_cache(tmp_path, duckdb_file, capsys)
    rc, envelope = _run(
        [
            "--repo-root",
            str(tmp_path),
            "transform",
            "test",
            "--scaffold",
            "customer_orders",
        ],
        capsys,
    )
    assert rc == 1
    assert envelope["status"] == "error"
    assert "no ref()/source() inputs" in envelope["errors"][0]


def test_scaffold_a_bare_select_star_over_two_joined_inputs_is_ambiguous(
    dbt_project_dir: Path, duckdb_file: Path, tmp_path: Path, capsys
):
    """A `select *` spanning more than one joined source cannot be attributed
    to either input's alias, so it is refused rather than guessed at."""

    _write_customer_orders_model(
        dbt_project_dir,
        "select *\n"
        "from {{ source('main', 'customers') }} c\n"
        "join {{ source('main', 'orders') }} o on o.customer_id = c.id\n",
    )
    _seed_cache(tmp_path, duckdb_file, capsys)
    rc, envelope = _run(
        [
            "--repo-root",
            str(tmp_path),
            "transform",
            "test",
            "--scaffold",
            "customer_orders",
        ],
        capsys,
    )
    assert rc == 0, envelope
    assert envelope["status"] == "ok"


def test_scaffold_qualified_star_over_a_single_source_expands_from_the_cache(
    dbt_project_dir: Path, duckdb_file: Path, tmp_path: Path, capsys
):
    """`select c.*` over one source is resolvable (the common staging shape):
    the cache supplies the real column list instead of a refusal."""

    _write_customer_orders_model(
        dbt_project_dir, "select c.*\nfrom {{ source('main', 'customers') }} c\n"
    )
    _seed_cache(tmp_path, duckdb_file, capsys)
    rc, envelope = _run(
        [
            "--repo-root",
            str(tmp_path),
            "transform",
            "test",
            "--scaffold",
            "customer_orders",
        ],
        capsys,
    )
    assert rc == 0, envelope
    diff = envelope["diffs"][0]["unified"]
    content = "\n".join(line[1:] for line in diff.splitlines() if line.startswith("+"))
    assert "id: 1" in content
    assert 'email: "example"' in content


def test_scaffold_ambiguous_unqualified_column_is_a_clean_error(
    dbt_project_dir: Path, duckdb_file: Path, tmp_path: Path, capsys
):
    _write_customer_orders_model(
        dbt_project_dir,
        "select id\n"
        "from {{ source('main', 'customers') }} c\n"
        "join {{ source('main', 'orders') }} o on o.customer_id = c.id\n",
    )
    _seed_cache(tmp_path, duckdb_file, capsys)
    rc, envelope = _run(
        [
            "--repo-root",
            str(tmp_path),
            "transform",
            "test",
            "--scaffold",
            "customer_orders",
        ],
        capsys,
    )
    assert rc == 1
    assert envelope["status"] == "error"
    assert "ambiguous" in envelope["errors"][0]


def test_scaffold_without_cache_is_a_clean_error(
    dbt_project_dir: Path, tmp_path: Path, capsys
):
    _write_customer_orders_model(dbt_project_dir, _TWO_INPUT_MODEL)
    rc, envelope = _run(
        [
            "--repo-root",
            str(tmp_path),
            "transform",
            "test",
            "--scaffold",
            "customer_orders",
        ],
        capsys,
    )
    assert rc == 1
    assert envelope["status"] == "error"
    assert "explore map" in envelope["errors"][0]


def test_scaffold_unknown_model_is_a_clean_error(
    dbt_project_dir: Path, duckdb_file: Path, tmp_path: Path, capsys
):
    _seed_cache(tmp_path, duckdb_file, capsys)
    rc, envelope = _run(
        [
            "--repo-root",
            str(tmp_path),
            "transform",
            "test",
            "--scaffold",
            "does_not_exist",
        ],
        capsys,
    )
    assert rc == 1
    assert envelope["status"] == "error"
    assert "no model named" in envelope["errors"][0]


def test_scaffold_with_no_model_argument_is_a_clean_error(tmp_path: Path, capsys):
    rc, envelope = _run(
        ["--repo-root", str(tmp_path), "transform", "test"],
        capsys,
    )
    assert rc == 1
    assert envelope["status"] == "error"
