"""Values-free mode against the seeded Postgres (#591).

The shipped conformance suite proves the guarantee on DuckDB with sentinel data.
This proves it where a real server writes the error text: Postgres quotes the
input a cast failed on (`invalid input syntax for type integer: "..."`), so a
warehouse error is the leak the mode most needs to close. The seeded data has no
sentinels, so profile checks inspect the value-bearing fields directly. Counts
remain available even when they happen to equal a source value (the orders row
count and maximum id are both 2000). The values to search for are read from a
full-mode profile first: whatever that run surfaced, the values-free run must
not.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from .conftest import assert_ok
from .test_postgres_connect import run_cli, seed_repo

pytestmark = [pytest.mark.integration, pytest.mark.postgres]

# Cast a text column that holds no integers; Postgres quotes the first value.
CAST_FAILURE = "select sum(cast(email as integer)) from app.customers"


@pytest.fixture(autouse=True)
def _dsn_env(pg_dsn, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", pg_dsn)
    monkeypatch.delenv("DEX_RESULT_MODE", raising=False)


def _surfaced_values(envelope: dict) -> set[str]:
    """Every min, max and value-domain value a full profile reported, as text
    long enough to be distinctive rather than a digit that occurs anywhere."""

    found: set[str] = set()
    for dataset in envelope["data"]["datasets"]:
        for column in dataset["columns"]:
            for key in ("min_value", "max_value"):
                if column.get(key) is not None:
                    found.add(str(column[key]))
            for entry in (column.get("value_domain") or {}).get("values", []):
                found.add(str(entry["value"]))
    return {value for value in found if len(value) >= 4}


def _counts(value) -> set[str]:
    """Every integer the envelope reports under a ``*_count`` key, as text. The
    mode keeps counts, so a source value that equals one (the orders row count
    and maximum id are both 2000) is not a leak when the count carries it."""

    found: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if key.endswith("count") and isinstance(item, int):
                found.add(str(item))
            found |= _counts(item)
    elif isinstance(value, list):
        for item in value:
            found |= _counts(item)
    return found


def test_values_free_profile_and_query_withhold_values_but_keep_counts(
    tmp_path: Path, capsys
):
    seed_repo(tmp_path, schemas=["app"], budget=120)
    root = ["--repo-root", str(tmp_path)]
    rc, full = run_cli(
        [
            *root,
            "explore",
            "profile",
            "app.orders",
            "app.products",
            "--columns",
            "all",
            "--confirm",
        ],
        capsys,
    )
    assert_ok(rc, full)
    value_fields = {"min_value", "max_value", "value_domain"}
    for field in value_fields:
        assert any(
            column.get(field) is not None
            for dataset in full["data"]["datasets"]
            for column in dataset["columns"]
        ), f"the full profile surfaced no {field} to withhold"
    surfaced = _surfaced_values(full)
    assert surfaced, "the full profile surfaced no value to search for"

    rc, moded = run_cli(
        [
            *root,
            "explore",
            "profile",
            "app.orders",
            "app.products",
            "--columns",
            "all",
            "--values-free",
            "--confirm",
        ],
        capsys,
    )
    assert_ok(rc, moded)
    assert moded["data"]["result_mode"] == "values_free"
    assert {d["identifier"]: d["row_count"] for d in moded["data"]["datasets"]} == {
        d["identifier"]: d["row_count"] for d in full["data"]["datasets"]
    }
    for dataset in moded["data"]["datasets"]:
        assert set(dataset["withheld_fields"]) == value_fields
        for column in dataset["columns"]:
            assert not value_fields.intersection(column)
    text = json.dumps(moded)
    searched = surfaced - _counts(moded["data"])
    assert searched, "every surfaced value coincides with a count; nothing to search"
    assert not [value for value in searched if value in text]

    rc, counted = run_cli(
        [
            *root,
            "explore",
            "query",
            "--values-free",
            "--confirm",
            "select count(*), count(distinct status), avg(total) from app.orders",
        ],
        capsys,
    )
    assert_ok(rc, counted)
    assert counted["data"]["row_count"] == 1

    _rc, grouped = run_cli(
        [
            *root,
            "explore",
            "query",
            "--values-free",
            "--confirm",
            "select status, count(*) from app.orders group by status",
        ],
        capsys,
    )
    assert grouped["status"] == "error"
    assert grouped["data"]["code"] == "group_by_column"
    assert "cells" not in grouped["data"]


def test_a_postgres_cast_error_keeps_its_words_and_never_its_input(
    tmp_path: Path, capsys
):
    seed_repo(tmp_path, schemas=["app"], budget=120)
    root = ["--repo-root", str(tmp_path)]
    assert_ok(
        *run_cli([*root, "explore", "profile", "app.customers", "--confirm"], capsys)
    )

    _rc, full = run_cli([*root, "explore", "query", "--confirm", CAST_FAILURE], capsys)
    _rc, moded = run_cli(
        [*root, "explore", "query", "--values-free", "--confirm", CAST_FAILURE],
        capsys,
    )

    # Outside the mode the warehouse's words survive, but the value Postgres
    # quotes is masked (#1070): the error names the failure, never the row.
    assert full["reason"] == "execution_failure"
    assert "invalid input syntax for type integer" in full["errors"][0]
    assert "<value withheld>" in full["errors"][0]
    assert "@" not in json.dumps(full)
    assert moded["reason"] == "execution_failure"
    assert moded["data"]["code"] == "warehouse_refused"
    assert "@" not in json.dumps(moded)
