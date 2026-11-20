"""The generated-test parity harness on DuckDB, in the offline suite (#1450).

Each row of :data:`generated_parity.ROWS` builds a violating and a clean
fixture model through the CLI and holds the generated test's verdict to
``maintain verify``'s. The same runner carries Postgres and the billed dialects
in ``integration.yml`` (#1451). The planted macros show the harness is not
vacuous: a ``duckdb__`` override that passes a duplicated key fails it, and a
broken override for another dialect leaves DuckDB's answer alone, since dbt's
dispatch picks one implementation per adapter.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from transform.generated_parity import ROWS, UNIQUE_COMBINATION, run_parity

from exmergo_dex_core import generated_tests as gt
from exmergo_dex_core.cli import main

pytest.importorskip("dbt.cli.main")
pytest.importorskip("dbt.adapters.duckdb")


@pytest.fixture
def repo(dbt_project_dir: Path, tmp_path: Path) -> Path:
    # Only the fixture models: the project's own staging model reads nothing
    # this dev database has, and is never selected.
    (tmp_path / ".dex").mkdir()
    (tmp_path / ".dex" / "config.yml").write_text(
        f"connector: duckdb\nduckdb:\n  path: {tmp_path / 'dev.duckdb'}\n",
        encoding="utf-8",
    )
    return tmp_path


def _runner(capsys):
    def run(argv: list[str]) -> tuple[int, dict]:
        rc = main(argv)
        return rc, json.loads(capsys.readouterr().out)

    return run


def _planted(adapter: str, sql: str) -> dict:
    """A per-dialect implementation of the test, as a project would override it."""

    return {
        "path": f"macros/{adapter}__dex_unique_combination.sql",
        "kind": "macro_sql",
        "content": (
            f"{{% macro {adapter}__dex_unique_combination(model, columns) %}}\n"
            f"{sql}\n{{% endmacro %}}\n"
        ),
    }


#: Passes on any model: no row, whatever the grain holds.
PASSES_ANYWAY = "select 1 as dex_duplicate_rows where 1 = 0"
#: Not SQL any warehouse runs.
BROKEN = "this is not sql"


def test_every_row_agrees_with_maintain_verify_on_duckdb(
    repo: Path, dbt_project_dir: Path, capsys
):
    report = run_parity(_runner(capsys), repo, dbt_project_dir, connector="duckdb")

    assert report.disagreements == [], report.disagreements
    assert report.agreed == [row.test for row in ROWS]
    assert report.unsupported == []


def test_the_plan_writes_the_shipped_macro_beside_the_rendered_test(
    repo: Path, dbt_project_dir: Path, capsys
):
    report = run_parity(_runner(capsys), repo, dbt_project_dir, connector="duckdb")

    paths = report.envelopes["plan"]["data"]["paths"]
    assert "macros/dex_unique_combination.sql" in paths
    written = (dbt_project_dir / "macros" / "dex_unique_combination.sql").read_text()
    assert "{% test dex_unique_combination(model, columns) %}" in written
    # Counts only: the test selects the duplicate count, never a key column.
    assert "select dex_duplicate_rows" in written


def test_a_macro_that_passes_a_duplicated_key_fails_the_harness(
    repo: Path, dbt_project_dir: Path, capsys
):
    report = run_parity(
        _runner(capsys),
        repo,
        dbt_project_dir,
        connector="duckdb",
        extra_edits=[_planted("duckdb", PASSES_ANYWAY)],
    )

    violating = UNIQUE_COMBINATION.model("violating")
    assert report.disagreements == [
        f"dex_unique_combination on {violating}: the test should fail on the "
        "violating fixture, dbt says 'pass'"
    ]


def test_a_broken_macro_for_another_dialect_leaves_duckdb_agreeing(
    repo: Path, dbt_project_dir: Path, capsys
):
    # #1451: one dialect's macro is that dialect's job's to catch, no other's.
    report = run_parity(
        _runner(capsys),
        repo,
        dbt_project_dir,
        connector="duckdb",
        extra_edits=[_planted("bigquery", BROKEN), _planted("snowflake", BROKEN)],
    )

    assert report.disagreements == [], report.disagreements


def test_a_dialect_the_test_does_not_support_renders_nothing_and_says_so(
    repo: Path, dbt_project_dir: Path, capsys, monkeypatch: pytest.MonkeyPatch
):
    # #1451: a kind that does not list the connector renders no test there,
    # and maintain verify alone still answers.
    shipped = next(k for k in gt.kinds() if k.name == gt.UNIQUE_COMBINATION)
    narrowed = gt.GeneratedTestKind(
        shipped.name,
        shipped.cadence,
        shipped.instances,
        macro=shipped.macro,
        dialects=frozenset({"postgres"}),
    )
    gt.unregister(shipped.name)
    gt.register(narrowed)
    try:
        report = run_parity(_runner(capsys), repo, dbt_project_dir, connector="duckdb")
    finally:
        gt.unregister(shipped.name)
        gt.register(shipped)

    assert report.disagreements == [], report.disagreements
    assert report.unsupported == [gt.UNIQUE_COMBINATION]
    planned = report.envelopes["plan"]["data"]
    assert planned["generated_tests"]["counts"] == {"not_rendered": 2}
    assert "macros/dex_unique_combination.sql" not in planned["paths"]
