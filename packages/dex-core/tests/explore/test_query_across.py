"""`explore query --across`: one probe over mirrored environments (#545)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from exmergo_dex_core.cli import main


def _run(argv: list[str], capsys, *, expect_error: bool = False) -> dict:
    rc = main(argv)
    out = capsys.readouterr().out
    assert out.count("\n") == 1, "exactly one envelope line"
    payload = json.loads(out)
    if expect_error:
        assert rc == 1 and payload["status"] == "error", payload
    else:
        assert rc == 0 and payload["status"] == "ok", payload
    return payload


@pytest.fixture
def mirrored(tmp_path: Path) -> Path:
    """dev_raw and prod_raw hold the same tables with different data; qa_raw has
    no orders at all, which is the shape a half-provisioned environment has."""

    duckdb = pytest.importorskip("duckdb")
    path = tmp_path / "mirrors.duckdb"
    conn = duckdb.connect(str(path))
    for schema, rows in (
        ("dev_raw", [(1, "shipped"), (2, "shipped")]),
        ("prod_raw", [(1, "shipped"), (2, "cancelled"), (3, "returned")]),
    ):
        conn.execute(f"CREATE SCHEMA {schema}")
        conn.execute(f"CREATE TABLE {schema}.orders (id INTEGER, status VARCHAR)")
        # `schema` is one of the two fixed names above, never caller input.
        conn.executemany(f"INSERT INTO {schema}.orders VALUES (?, ?)", rows)  # noqa: S608
        conn.execute(f"CREATE TABLE {schema}.users (id INTEGER, email VARCHAR)")
        conn.execute(f"INSERT INTO {schema}.users VALUES (1, 'a@example.com')")  # noqa: S608
    conn.execute("CREATE SCHEMA qa_raw")
    conn.execute("CREATE TABLE qa_raw.placeholder (id INTEGER)")
    conn.close()
    return path


def _repo(mirrored: Path, tmp_path: Path, capsys) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _run(["explore", "map", "--path", str(mirrored), "--repo-root", str(repo)], capsys)
    return repo


def _across(
    mirrored: Path, repo: Path, capsys, *args: str, expect_error: bool = False
) -> dict:
    return _run(
        ["explore", "query", *args, "--path", str(mirrored), "--repo-root", str(repo)],
        capsys,
        expect_error=expect_error,
    )


def test_one_probe_runs_once_per_target_with_results_keyed_by_target(
    mirrored: Path, tmp_path: Path, capsys
):
    repo = _repo(mirrored, tmp_path, capsys)

    data = _across(
        mirrored,
        repo,
        capsys,
        "select status, count(*) as n from {ds}.orders group by 1 order by 1",
        "--across",
        "dev_raw,prod_raw",
    )["data"]

    assert data["across"] == ["dev_raw", "prod_raw"]
    results = {r["target"]: r for r in data["results"]}
    assert set(results) == {"dev_raw", "prod_raw"}
    assert all(r["template"] == 0 and r["status"] == "ok" for r in data["results"])
    # Same columns everywhere, so the variants line up; different answers, which
    # is the question being asked.
    assert results["dev_raw"]["columns"] == results["prod_raw"]["columns"]
    assert results["dev_raw"]["cells"] == [["shipped", 2]]
    assert results["prod_raw"]["cells"] == [
        ["cancelled", 1],
        ["returned", 1],
        ["shipped", 1],
    ]


def test_variants_of_one_template_sit_together_in_template_order(
    mirrored: Path, tmp_path: Path, capsys
):
    repo = _repo(mirrored, tmp_path, capsys)

    data = _across(
        mirrored,
        repo,
        capsys,
        "select count(*) as n from {ds}.orders",
        "select count(*) as n from {ds}.users",
        "--across",
        "dev_raw",
        "--across",
        "prod_raw",
    )["data"]

    assert [(r["template"], r["target"]) for r in data["results"]] == [
        (0, "dev_raw"),
        (0, "prod_raw"),
        (1, "dev_raw"),
        (1, "prod_raw"),
    ]


def test_templates_can_come_from_a_sql_file(mirrored: Path, tmp_path: Path, capsys):
    repo = _repo(mirrored, tmp_path, capsys)
    sql_file = tmp_path / "probes.sql"
    sql_file.write_text(
        "select count(*) as n from {ds}.orders;\n"
        "select count(*) as n from {ds}.users;\n",
        encoding="utf-8",
    )

    data = _across(
        mirrored,
        repo,
        capsys,
        "--sql-file",
        str(sql_file),
        "--across",
        "dev_raw,prod_raw",
    )["data"]

    assert data["statement_count"] == 4
    assert {(r["template"], r["target"]) for r in data["results"]} == {
        (0, "dev_raw"),
        (0, "prod_raw"),
        (1, "dev_raw"),
        (1, "prod_raw"),
    }


def test_a_target_that_fails_is_named_and_the_others_still_answer(
    mirrored: Path, tmp_path: Path, capsys
):
    repo = _repo(mirrored, tmp_path, capsys)

    payload = _across(
        mirrored,
        repo,
        capsys,
        "select count(*) as n from {ds}.orders",
        "--across",
        "dev_raw,qa_raw,prod_raw",
        expect_error=True,
    )

    by_target = {r["target"]: r for r in payload["data"]["results"]}
    assert by_target["dev_raw"]["status"] == "ok"
    assert by_target["prod_raw"]["status"] == "ok"
    assert by_target["qa_raw"]["status"] != "ok"
    assert len(payload["errors"]) == 1
    assert "(target qa_raw)" in payload["errors"][0]


def test_the_firewall_adjudicates_every_variant(mirrored: Path, tmp_path: Path, capsys):
    """PII rules apply per relation: a value-carrying projection of a flagged
    column is refused on every target, never waved through on one of them."""

    repo = _repo(mirrored, tmp_path, capsys)

    payload = _across(
        mirrored,
        repo,
        capsys,
        "select email from {ds}.users",
        "--across",
        "dev_raw,prod_raw",
        expect_error=True,
    )

    assert {r["status"] for r in payload["data"]["results"]} == {"refused"}
    assert any("(target dev_raw)" in e for e in payload["errors"])
    assert any("(target prod_raw)" in e for e in payload["errors"])


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        (["select count(*) from orders", "--across", "dev_raw"], "names no {ds}"),
        (
            ["select count(*) from {ds}.orders", "--across", "dev_raw; drop"],
            "is not a dataset name",
        ),
        (
            ["select count(*) from {ds}.orders", "--across", "dev_raw,dev_raw"],
            "more than once",
        ),
        (
            ["select count(*) from {ds}.orders", "--across", ","],
            "at least one target",
        ),
    ],
)
def test_a_malformed_across_call_is_refused_before_anything_runs(
    mirrored: Path, tmp_path: Path, capsys, args, expected
):
    repo = _repo(mirrored, tmp_path, capsys)

    payload = _across(mirrored, repo, capsys, *args, expect_error=True)

    assert payload["data"] == {}
    assert expected in payload["errors"][0]


def test_the_statement_limit_counts_every_variant(
    mirrored: Path, tmp_path: Path, capsys
):
    repo = _repo(mirrored, tmp_path, capsys)
    templates = [f"select {i} as n from {{ds}}.orders" for i in range(6)]  # noqa: S608

    payload = _across(
        mirrored,
        repo,
        capsys,
        *templates,
        "--across",
        "dev_raw,prod_raw",
        expect_error=True,
    )

    assert "is 12 statements" in payload["errors"][0]
    assert "max_statements" in payload["errors"][0]


def test_an_ordinary_batch_payload_is_unchanged(mirrored: Path, tmp_path: Path, capsys):
    repo = _repo(mirrored, tmp_path, capsys)

    data = _across(
        mirrored,
        repo,
        capsys,
        "select count(*) as n from dev_raw.orders",
        "select count(*) as n from prod_raw.orders",
    )["data"]

    assert "across" not in data
    assert all("target" not in r and "template" not in r for r in data["results"])


def test_across_takes_the_dev_target_flag_apart_from_its_datasets(
    mirrored: Path, tmp_path: Path, capsys
):
    # #1133: `--target dev` names the dbt dev target, not an `--across` dataset.
    # On DuckDB it widens nothing, says so, and leaves the fan-out unchanged.
    repo = _repo(mirrored, tmp_path, capsys)
    data = _across(
        mirrored,
        repo,
        capsys,
        "select count(*) as n from {ds}.orders",
        "--across",
        "dev_raw,prod_raw",
        "--target",
        "dev",
    )["data"]
    assert data["across"] == ["dev_raw", "prod_raw"]
    assert [r["cells"] for r in data["results"]] == [[[2]], [[3]]]
    assert any("--target dev widened nothing" in n for n in data["notes"])
