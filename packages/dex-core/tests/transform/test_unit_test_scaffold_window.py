"""`transform test --scaffold` places rows on a calendar window's boundary (#1112).

A trailing-window model scaffolded with one example row per input produces a
unit test that cannot tell a six-day window from a seven-day one, or a day
window from a row window. These tests check the rows the scaffold places
instead: the current day, the last day the window includes, the first it
excludes, and a gap a row-count frame would cross, with the partition held
constant and the aggregated column numbered so the expected sums decode.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
import yaml

from exmergo_dex_core.cache import CacheProvenance, ColumnProfile, Dataset, DexCache
from exmergo_dex_core.dbt_project import load
from exmergo_dex_core.transform.test_scaffold import unit_test_scaffold

_COLUMNS = [
    ColumnProfile(name="depot", data_type="VARCHAR"),
    ColumnProfile(name="pickup_date", data_type="DATE"),
    ColumnProfile(name="picked_at", data_type="TIMESTAMP"),
    ColumnProfile(name="parcels", data_type="INTEGER"),
    ColumnProfile(name="pickup_id", data_type="INTEGER"),
    ColumnProfile(name="depot_id", data_type="INTEGER"),
]


def _cache() -> DexCache:
    return DexCache(
        datasets=[
            Dataset(identifier="dev.main.stg_pickups", columns=_COLUMNS),
            Dataset(
                identifier="dev.main.stg_depots",
                columns=[
                    ColumnProfile(name="depot_id", data_type="INTEGER"),
                    ColumnProfile(name="region", data_type="VARCHAR"),
                ],
            ),
        ],
        provenance=CacheProvenance(connector="duckdb"),
    )


def _scaffold(project: Path, sql: str):
    (project / "models" / "staging" / "fct_window.sql").write_text(
        sql, encoding="utf-8"
    )
    scaffold = unit_test_scaffold(load(project), _cache(), "fct_window")
    test = yaml.safe_load(scaffold.edits[0].new_content)["unit_tests"][0]
    return scaffold, test


def _window(
    frame: str, order: str = "pickup_date", function: str = "sum(parcels)"
) -> str:
    return (
        f"select depot, {order}, {function} over (partition by depot order by {order} "  # noqa: S608
        f"{frame}) as trailing from {{{{ ref('stg_pickups') }}}}\n"
    )


def _dates(rows: list[dict], column: str = "pickup_date") -> list[str]:
    return [str(row[column]) for row in rows]


def test_a_trailing_day_window_gets_its_boundary_and_a_gap(dbt_project_dir: Path):
    scaffold, test = _scaffold(
        dbt_project_dir,
        _window("range between interval '6 day' preceding and current row"),
    )
    rows = test["given"][0]["rows"]
    # A = 2024-01-31; last included A-6; first excluded A-7; the gap A-14.
    assert _dates(rows) == ["2024-01-17", "2024-01-24", "2024-01-25", "2024-01-31"]
    assert {row["depot"] for row in rows} == {"example"}
    assert [row["parcels"] for row in rows] == [1, 10, 100, 1000]
    assert test["expect"]["rows"] == [{}]

    (boundary,) = scaffold.window_boundaries
    assert boundary["input"] == "stg_pickups"
    assert boundary["order_by"] == "pickup_date"
    assert boundary["bound"]["value"] == 6 and boundary["bound"]["unit"] == "day"
    roles = [r["tests"] for r in boundary["placed_dates"]]
    assert "last day the window includes" in roles[2]
    assert "first day the window excludes" in roles[1]
    assert "gap" in roles[0]
    assert boundary["numbered_columns"] == ["parcels"]
    assert scaffold.window_notes == []


def test_the_placed_rows_tell_every_neighbouring_frame_apart(dbt_project_dir: Path):
    """The point of the rows, checked by arithmetic: the trailing sums the
    model, a day wider, a day narrower and a six-row frame produce all differ."""

    _, test = _scaffold(
        dbt_project_dir,
        _window("range between interval '6 day' preceding and current row"),
    )
    rows = [
        (date.fromisoformat(str(r["pickup_date"])), r["parcels"])
        for r in test["given"][0]["rows"]
    ]

    def by_days(width: int) -> list[int]:
        return [
            sum(p for d2, p in rows if 0 <= (d - d2).days <= width) for d, _ in rows
        ]

    def by_rows(width: int) -> list[int]:
        return [
            sum(p for _, p in rows[max(0, i - width) : i + 1]) for i in range(len(rows))
        ]

    model = by_days(6)
    assert (
        len({tuple(model), tuple(by_days(7)), tuple(by_days(5)), tuple(by_rows(6))})
        == 4
    )


def test_a_following_bound_places_its_rows_ahead(dbt_project_dir: Path):
    _, test = _scaffold(
        dbt_project_dir,
        _window("range between current row and interval '3 days' following"),
    )
    assert _dates(test["given"][0]["rows"]) == [
        "2024-01-31",
        "2024-02-03",
        "2024-02-04",
        "2024-02-08",
    ]


def test_a_week_bound_is_counted_in_days_and_a_timestamp_keeps_its_type(
    dbt_project_dir: Path,
):
    _, test = _scaffold(
        dbt_project_dir,
        _window(
            "range between interval 1 week preceding and current row", order="picked_at"
        ),
    )
    assert _dates(test["given"][0]["rows"], "picked_at") == [
        "2024-01-15 00:00:00",
        "2024-01-23 00:00:00",
        "2024-01-24 00:00:00",
        "2024-01-31 00:00:00",
    ]


def test_an_ordering_column_traced_through_a_cte_still_gets_rows(dbt_project_dir: Path):
    sql = (
        "with p as (select depot, pickup_date as day, parcels "
        "from {{ ref('stg_pickups') }})\n"
        "select depot, day, sum(parcels) over (partition by depot order by day "
        "range between interval '2 day' preceding and current row) as t from p\n"
    )
    scaffold, test = _scaffold(dbt_project_dir, sql)
    assert _dates(test["given"][0]["rows"]) == [
        "2024-01-25",
        "2024-01-28",
        "2024-01-29",
        "2024-01-31",
    ]
    assert scaffold.window_boundaries[0]["order_by"] == "pickup_date"


def test_a_joined_input_still_matches_and_keeps_one_row(dbt_project_dir: Path):
    """Only the windowed input gets boundary rows. The join key keeps the one
    example value on both sides, so every placed row still finds its match."""

    sql = (
        "select p.depot, p.pickup_date, d.region, sum(p.parcels) over "
        "(partition by p.depot order by p.pickup_date range between interval "
        "'6 day' preceding and current row) as t\n"
        "from {{ ref('stg_pickups') }} p\n"
        "join {{ ref('stg_depots') }} d on d.depot_id = p.depot_id\n"
    )
    _, test = _scaffold(dbt_project_dir, sql)
    pickups, depots = test["given"]
    assert len(pickups["rows"]) == 4 and len(depots["rows"]) == 1
    assert {row["depot_id"] for row in pickups["rows"]} == {
        depots["rows"][0]["depot_id"]
    }


@pytest.mark.parametrize(
    "frame,order,code",
    [
        (
            "range between interval '1 month' preceding and current row",
            "pickup_date",
            "unit_not_supported_for_fixture",
        ),
        (
            "range between interval '6 day' preceding and current row",
            "date_trunc('day', picked_at)",
            "ordering_not_an_input_column",
        ),
        (
            "range between interval '6 day' preceding and current row",
            "pickup_id",
            "ordering_not_temporal",
        ),
    ],
)
def test_a_window_it_cannot_place_rows_for_is_noted_and_keeps_one_row(
    dbt_project_dir: Path, frame: str, order: str, code: str
):
    sql = (
        f"select depot, pickup_date, picked_at, pickup_id, sum(parcels) over "  # noqa: S608
        f"(partition by depot order by {order} {frame}) as t "
        "from {{ ref('stg_pickups') }}\n"
    )
    scaffold, test = _scaffold(dbt_project_dir, sql)
    assert len(test["given"][0]["rows"]) == 1
    assert scaffold.window_boundaries == []
    assert [n["reason_code"] for n in scaffold.window_notes] == [code]


def test_a_rows_frame_or_an_unbounded_range_is_left_as_it_was(dbt_project_dir: Path):
    """No calendar boundary to reach, so no rows placed and nothing to note."""

    for frame in (
        "rows between 6 preceding and current row",
        "range between unbounded preceding and current row",
    ):
        scaffold, test = _scaffold(dbt_project_dir, _window(frame))
        assert len(test["given"][0]["rows"]) == 1
        assert scaffold.window_boundaries == [] and scaffold.window_notes == []


@pytest.mark.parametrize(
    "frame,order,code",
    [
        # Bare numeric RANGE over a non-date column keeps numeric_range_offset.
        (
            "range between 6 preceding and current row",
            "parcels",
            "numeric_range_offset",
        ),
        (
            "range between interval (parcels) day preceding and current row",
            "pickup_date",
            "unsupported_interval_bound",
        ),
    ],
)
def test_a_range_boundary_it_cannot_reach_is_noted_rather_than_silent(
    dbt_project_dir: Path, frame: str, order: str, code: str
):
    """Found dogfooding on BigQuery (#1112): a RANGE frame whose bound is not a
    day interval still has a boundary, and one example row cannot reach it."""

    sql = (
        "select depot, pickup_date, sum(parcels) over (partition by depot "  # noqa: S608
        f"order by {order} {frame}) as t from {{{{ ref('stg_pickups') }}}}\n"
    )
    scaffold, test = _scaffold(dbt_project_dir, sql)
    assert len(test["given"][0]["rows"]) == 1
    assert scaffold.window_boundaries == []
    assert [n["reason_code"] for n in scaffold.window_notes] == [code]


def test_bigquery_unix_date_window_gets_day_boundaries(dbt_project_dir: Path):
    sql = (
        "select depot, pickup_date, sum(parcels) over (partition by depot "
        "order by unix_date(pickup_date) range between 6 preceding and current row) "
        "as trailing from {{ ref('stg_pickups') }}\n"
    )
    scaffold, test = _scaffold(dbt_project_dir, sql)
    rows = test["given"][0]["rows"]
    assert _dates(rows) == ["2024-01-17", "2024-01-24", "2024-01-25", "2024-01-31"]
    assert scaffold.window_notes == []
    (boundary,) = scaffold.window_boundaries
    assert boundary["input"] == "stg_pickups"
    assert boundary["order_by"] == "pickup_date"
    assert boundary["bound"]["value"] == 6 and boundary["bound"]["unit"] == "day"


def test_date_diff_day_window_gets_day_boundaries(dbt_project_dir: Path):
    sql = (
        "select depot, pickup_date, sum(parcels) over (partition by depot "
        "order by date_diff('day', date '1970-01-01', pickup_date) "
        "range between 6 preceding and current row) as trailing "
        "from {{ ref('stg_pickups') }}\n"
    )
    scaffold, test = _scaffold(dbt_project_dir, sql)
    rows = test["given"][0]["rows"]
    assert _dates(rows) == ["2024-01-17", "2024-01-24", "2024-01-25", "2024-01-31"]
    assert scaffold.window_notes == []
    (boundary,) = scaffold.window_boundaries
    assert boundary["input"] == "stg_pickups"
    assert boundary["order_by"] == "pickup_date"
    assert boundary["bound"]["value"] == 6 and boundary["bound"]["unit"] == "day"


def test_bigquery_dialect_parser_level_unix_date_window():
    import sqlglot

    from exmergo_dex_core.transform.window_fixture import boundary_rows

    for order_by in (
        "unix_date(pickup_date)",
        "date_diff(pickup_date, date '1970-01-01', day)",
    ):
        sql = (
            "select depot, pickup_date, parcels, "  # noqa: S608
            f"sum(parcels) over (partition by depot order by {order_by} "
            "range between 6 preceding and current row) as t "
            "from stg_pickups"
        )
        tree = sqlglot.parse_one(sql, read="bigquery")
        res = boundary_rows(
            tree=tree,
            inputs_by_lower={"stg_pickups": "stg_pickups"},
            reads={"stg_pickups": {"depot", "pickup_date", "parcels"}},
            types={
                "stg_pickups": {
                    "depot": "STRING",
                    "pickup_date": "DATE",
                    "parcels": "INT64",
                }
            },
            example_value=lambda t: (
                1 if "INT" in t else ("2024-01-01" if "DATE" in t else "example")
            ),
        )
        assert res.notes == []
        assert len(res.boundaries) == 1
        boundary = res.boundaries[0]
        assert boundary["input"] == "stg_pickups"
        assert boundary["order_by"] == "pickup_date"
        assert boundary["bound"]["value"] == 6 and boundary["bound"]["unit"] == "day"
        rows = res.rows["stg_pickups"]
        assert len(rows) == 4
        assert [str(r["pickup_date"]) for r in rows] == [
            "2024-01-17",
            "2024-01-24",
            "2024-01-25",
            "2024-01-31",
        ]


def test_the_scaffold_parses_under_dbt_and_reports_its_boundaries(
    dbt_project_dir: Path, tmp_path: Path, capsys
):
    """Through the CLI: dbt's own parse gates the plan, and the envelope names
    the window and what each placed date tests."""

    pytest.importorskip("dbt.cli.main")
    import json

    from exmergo_dex_core.cli import main

    duckdb = pytest.importorskip("duckdb")
    warehouse = tmp_path / "w.duckdb"
    con = duckdb.connect(str(warehouse))
    con.execute(
        "create table pickups as select * from "
        "(values ('north', date '2024-01-01', 3)) t(depot, pickup_date, parcels)"
    )
    con.close()
    (dbt_project_dir / "models" / "staging" / "_sources.yml").write_text(
        "version: 2\nsources:\n  - name: main\n    schema: main\n    tables:\n"
        "      - name: pickups\n",
        encoding="utf-8",
    )
    (dbt_project_dir / "models" / "staging" / "fct_window.sql").write_text(
        "select depot, pickup_date, sum(parcels) over (partition by depot order by "
        "pickup_date range between interval '6 day' preceding and current row) as t "
        "from {{ source('main', 'pickups') }}\n",
        encoding="utf-8",
    )
    assert (
        main(["--repo-root", str(tmp_path), "--path", str(warehouse), "explore", "map"])
        == 0
    )
    capsys.readouterr()
    rc = main(
        ["--repo-root", str(tmp_path), "transform", "test", "--scaffold", "fct_window"]
    )
    envelope = json.loads(capsys.readouterr().out)
    assert rc == 0 and envelope["status"] == "ok", envelope
    (boundary,) = envelope["data"]["window_boundaries"]
    assert boundary["input"] == "pickups"
    assert any("sit on the boundary" in w for w in envelope["warnings"])
