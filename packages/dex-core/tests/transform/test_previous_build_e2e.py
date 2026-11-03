"""A real DuckDB build reports previous failures without trusting stale rows."""

import json

import pytest

from exmergo_dex_core.cli import main


def test_repeated_failures_and_a_new_regression(dbt_project_dir, tmp_path, capsys):
    pytest.importorskip("dbt.adapters.duckdb")
    prerequisite = dbt_project_dir / "models" / "prerequisite.sql"
    broken = "{{ config(materialized='table') }}\nselect cast('bad' as integer) as id\n"
    prerequisite.write_text(broken)
    (dbt_project_dir / "models" / "report.sql").write_text(
        "{{ config(materialized='table') }}\nselect id from {{ ref('prerequisite') }}\n"
    )

    def build():
        main(
            [
                "--repo-root",
                str(tmp_path),
                "--connector",
                "duckdb",
                "transform",
                "build",
                "--target",
                "dev",
                "--verify",
            ]
        )
        return json.loads(capsys.readouterr().out)

    first = build()
    assert first["status"] == "error"
    [entry] = first["data"]["prerequisites"]
    assert entry["previous_run"]["available"] is False
    second = build()
    [entry] = second["data"]["prerequisites"]
    assert entry["previous_run"]["status"] == "error"
    assert (
        entry["previous_run"]["invocation_id"]
        == (first["data"]["evidence"]["invocation"]["invocation_id"])
    )
    assert second["data"]["completion"]["blockers"][0]["previous_status"] == "error"
    assert second["data"]["verification"]["scope"] == ["stg_customers"]
    assert second["data"]["verification"]["scope_not_built"] == [
        "prerequisite",
        "report",
    ]

    prerequisite.write_text("{{ config(materialized='table') }}\nselect 1 as id\n")
    repaired = build()
    assert repaired["status"] == "ok", repaired
    prerequisite.write_text(broken)
    regression = build()
    [entry] = regression["data"]["prerequisites"]
    assert entry["previous_run"]["status"] == "success"
    assert regression["data"]["verification"]["scope_not_built"] == [
        "prerequisite",
        "report",
    ]
    # The report relation still exists from the repaired build, but is not in scope.
    assert "report" not in regression["data"]["verification"]["scope"]
