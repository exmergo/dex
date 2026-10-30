"""The governed Cube query (#913): Cube renders, dex executes.

Each test runs the command surface on a real DuckDB warehouse, with a Cube
server that answers from what Cube Core v1.8.14 actually rendered for the same
query (:mod:`.fake_cube`). Numbers are held to the same question asked in
hand-written SQL. The refusals are each the order's own step: the PII gate
before anything is sent, the stale-renderer check before anything is rendered,
the plan checks before anything is bound, and the spine before anything runs.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

pytest.importorskip("sqlglot")
pytest.importorskip("httpx")
pytest.importorskip("duckdb")

import duckdb

from exmergo_dex_core.cli import main
from exmergo_dex_core.cube.loader import load_files

from .cube_cases import MODEL, MODEL_FILES, QUERIES, VALUES, write_shop
from .fake_cube import SECRET, FakeCube, meta_for, recorded


def _run(capsys, root: Path, *argv: str) -> dict:
    main(["--repo-root", str(root), *argv])
    return json.loads(capsys.readouterr().out)


def _config(
    root: Path, *, connector: str = "duckdb", extra: str = "", files=None
) -> None:
    names = files or [f"model/{name}" for name in MODEL_FILES]
    warehouse = (
        f"duckdb:\n  path: {root / 'shop.duckdb'}\n" if connector == "duckdb" else ""
    )
    (root / ".dex").mkdir(exist_ok=True)
    (root / ".dex" / "config.yml").write_text(
        f"connector: {connector}\n"
        + warehouse
        + extra
        + "semantic:\n  vendor: cube\n  cube:\n    files:\n"
        + "".join(f"      - {name}\n" for name in names),
        encoding="utf-8",
    )


@pytest.fixture
def shop(tmp_path: Path) -> Path:
    shutil.copytree(MODEL, tmp_path / "model")
    write_shop(tmp_path / "shop.duckdb")
    _config(tmp_path)
    return tmp_path


@pytest.fixture
def cube(monkeypatch) -> FakeCube:
    return FakeCube("duckdb").install(monkeypatch)


def _by_hand(root: Path, sql: str) -> list[tuple]:
    conn = duckdb.connect(str(root / "shop.duckdb"), read_only=True)
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


def _query(capsys, root: Path, case: str, *extra: str) -> dict:
    spec = QUERIES[case]
    argv = ["explore", "semantic", "query", *spec["metrics"]]
    for token in spec.get("group_by", []):
        argv += ["--group-by", token]
    for clause in spec.get("where", []):
        argv += ["--where", clause]
    for token in spec.get("order_by", []):
        argv.append(f"--order-by={token}")
    if spec.get("grain"):
        argv += ["--grain", spec["grain"]]
    if spec.get("limit"):
        argv += ["--limit", str(spec["limit"])]
    return _run(capsys, root, *argv, *extra)


def _cells(envelope: dict) -> list[list]:
    assert envelope["status"] == "ok", envelope["errors"]
    return envelope["data"]["cells"]


def _num(value) -> float:
    return round(float(value), 6)


# --- the governed path, held to hand-written SQL --------------------------------------


def test_a_measure_by_a_dimension_matches_hand_written_sql(shop, cube, capsys):
    envelope = _query(capsys, shop, "by_status")
    data = envelope["data"]

    assert data["columns"] == ["orders.status", "orders.revenue", "orders.count"]
    assert (data["backend"], data["vendor"], data["execution"]) == (
        "local",
        "cube",
        "dex",
    )
    assert [[s, _num(r), c] for s, r, c in _cells(envelope)] == [
        [s, _num(r), c]
        for s, r, c in _by_hand(
            shop,
            "SELECT status, sum(total), count(id) FROM app.orders"
            " GROUP BY 1 ORDER BY 2 DESC",
        )
    ]
    assert not any("cost guard" in w for w in envelope["warnings"])
    assert cube.paths == ["/cubejs-api/v1/meta", "/cubejs-api/v1/sql"]


def test_a_time_grain_matches_hand_written_sql(shop, cube, capsys):
    envelope = _query(capsys, shop, "by_month")

    assert envelope["data"]["columns"] == ["orders.ordered_at__month", "orders.revenue"]
    got = [(str(m)[:7], _num(r)) for m, r in _cells(envelope)]
    want = [
        (str(m)[:7], _num(r))
        for m, r in _by_hand(
            shop,
            "SELECT strftime(date_trunc('month', timezone('UTC', ordered_at)),"
            " '%Y-%m'),"
            " sum(total) FROM app.orders GROUP BY 1 ORDER BY 1",
        )
    ]
    assert got == want


def test_grain_flag_buckets_the_grouped_time_dimension(shop, cube, capsys):
    envelope = _query(capsys, shop, "by_grain_flag")

    assert envelope["data"]["columns"] == ["orders.ordered_at__quarter", "orders.count"]
    assert [c for _, c in _cells(envelope)] == [
        c
        for _, c in _by_hand(
            shop,
            "SELECT date_trunc('quarter', timezone('UTC', ordered_at)) q, count(*)"
            " FROM app.orders GROUP BY 1 ORDER BY 1",
        )
    ]


def test_a_custom_granularity_runs(shop, cube, capsys):
    envelope = _query(capsys, shop, "fiscal_year")

    assert envelope["data"]["columns"] == [
        "orders.ordered_at__fiscal_year",
        "orders.revenue",
    ]
    assert sorted(_num(r) for _, r in _cells(envelope)) == sorted(
        _num(r)
        for _, r in _by_hand(
            shop,
            "SELECT date_trunc('year', timezone('UTC', ordered_at) - INTERVAL 3 MONTH),"
            " sum(total) FROM app.orders GROUP BY 1",
        )
    )


def test_json_filters_and_a_filtered_measure(shop, cube, capsys):
    envelope = _query(capsys, shop, "filtered")

    assert sorted((s, c, _num(p or 0)) for s, c, p in _cells(envelope)) == sorted(
        (s, c, _num(p or 0))
        for s, c, p in _by_hand(
            shop,
            "SELECT status, count(id), sum(CASE WHEN status = 'paid' THEN total END)"
            " FROM app.orders WHERE status IN ('paid', 'shipped') GROUP BY 1",
        )
    )


def test_a_measure_filter_is_a_having(shop, cube, capsys):
    envelope = _query(capsys, shop, "having")

    assert sorted(map(tuple, _cells(envelope))) == sorted(
        _by_hand(
            shop,
            "SELECT status, count(id) FROM app.orders GROUP BY 1 HAVING count(id) > 2",
        )
    )


def test_a_query_across_two_joins(shop, cube, capsys):
    envelope = _query(capsys, shop, "two_hops")

    assert envelope["data"]["columns"] == [
        "products.category",
        "orders.status",
        "order_items.units",
        "order_items.line_revenue",
    ]
    assert [[c, s, u, _num(r)] for c, s, u, r in _cells(envelope)] == [
        [c, s, u, _num(r)]
        for c, s, u, r in _by_hand(
            shop,
            "SELECT p.category, o.status, sum(i.quantity),"
            " sum(i.quantity * i.unit_price)"
            " FROM app.order_items i LEFT JOIN app.orders o ON i.order_id = o.id"
            " LEFT JOIN app.products p ON i.product_id = p.id"
            " GROUP BY 1, 2 ORDER BY 1, 2",
        )
    ]


def test_a_view_member_runs_as_the_cube_member_it_exposes(shop, cube, capsys):
    envelope = _query(capsys, shop, "view")

    assert envelope["data"]["columns"] == ["order_facts.status", "order_facts.revenue"]
    assert {s: _num(r) for s, r in _cells(envelope)} == {
        s: _num(r)
        for s, r in _by_hand(
            shop, "SELECT status, sum(total) FROM app.orders GROUP BY 1"
        )
    }


def test_a_rolling_window_is_bounded_by_the_date_range(shop, cube, capsys):
    """The `inDateRange` on the grouped time dimension reaches Cube as its
    `dateRange`, which is what bounds the window's series."""

    envelope = _query(capsys, shop, "rolling")
    sent = json.loads(cube.calls[-1].content)["query"]

    assert sent["timeDimensions"][0]["dateRange"] == ["2026-01-01", "2026-01-10"]
    assert "filters" not in sent
    got = [(str(day)[:10], _num(total or 0)) for day, total in _cells(envelope)]
    want = [
        (str(day)[:10], _num(total or 0))
        for day, total in _by_hand(
            shop,
            "SELECT d, (SELECT sum(total) FROM app.orders o"
            " WHERE timezone('UTC', o.ordered_at)"
            " >= d + INTERVAL 1 DAY - INTERVAL 7 DAY"
            " AND timezone('UTC', o.ordered_at) < d + INTERVAL 1 DAY)"
            " FROM generate_series(TIMESTAMP '2026-01-01', TIMESTAMP '2026-01-10',"
            " INTERVAL 1 DAY) t(d) ORDER BY 1",
        )
    ]
    assert got == want


def test_limit_is_sent_to_cube_and_the_cap_still_binds(shop, cube, capsys):
    envelope = _query(capsys, shop, "average")
    sent = json.loads(cube.calls[-1].content)["query"]

    assert sent["limit"] == 3
    assert envelope["data"]["row_count"] == 3
    unlimited = _query(capsys, shop, "by_status")
    assert json.loads(cube.calls[-1].content)["query"]["limit"] == 51
    assert unlimited["data"]["truncated"] is False


# --- values ----------------------------------------------------------------------


def _values(capsys, root: Path, case: str) -> dict:
    dimension, metrics = VALUES[case]
    argv = ["explore", "semantic", "values", dimension]
    for metric in metrics:
        argv += ["--metric", metric]
    return _run(capsys, root, *argv)


def test_a_dimension_s_values_are_its_column_s_domain(shop, cube, capsys):
    envelope = _values(capsys, shop, "values_status")

    assert envelope["data"]["dimension"] == "orders.status"
    assert envelope["data"]["scoped_to"] == []
    assert sorted(row[0] for row in _cells(envelope)) == sorted(
        row[0] for row in _by_hand(shop, "SELECT DISTINCT status FROM app.orders")
    )


def test_values_scoped_to_a_metric_name_it(shop, cube, capsys):
    envelope = _values(capsys, shop, "values_scoped")

    assert envelope["data"]["scoped_to"] == ["order_items.units"]
    assert envelope["data"]["columns"][0] == "products.category"
    assert {row[0] for row in _cells(envelope)} == {"grocery", "home"}


def test_values_of_a_bucketed_time_dimension(shop, cube, capsys):
    envelope = _values(capsys, shop, "values_month")

    assert envelope["data"]["columns"] == ["orders.ordered_at__month"]
    assert len(_cells(envelope)) == len(
        _by_hand(
            shop,
            "SELECT DISTINCT date_trunc('month', timezone('UTC', ordered_at))"
            " FROM app.orders",
        )
    )


# --- the PII gate, before anything is sent ----------------------------------------


def test_a_pii_group_by_is_refused_before_cube_is_asked(shop, cube, capsys):
    envelope = _run(
        capsys,
        shop,
        "explore",
        "semantic",
        "query",
        "customers.count",
        "--group-by",
        "customers.email",
    )

    assert envelope["status"] == "error" and envelope["reason"] == "guard"
    assert "customers.email" in envelope["errors"][0] and "PII" in envelope["errors"][0]
    assert cube.calls == []


def test_a_pii_filter_is_refused_before_cube_is_asked(shop, cube, capsys):
    clause = '{"member": "customers.email", "operator": "contains", "values": ["@"]}'
    envelope = _run(
        capsys, shop, "explore", "semantic", "query", "orders.count", "--where", clause
    )

    assert envelope["reason"] == "guard" and "customers.email" in envelope["errors"][0]
    assert cube.calls == []


def test_a_pii_dimension_s_values_are_refused(shop, cube, capsys):
    envelope = _run(capsys, shop, "explore", "semantic", "values", "customers.email")

    assert envelope["reason"] == "guard" and "PII" in envelope["errors"][0]
    assert cube.calls == []


def test_a_profiled_flag_refuses_a_neutral_name(shop, cube, capsys):
    """The cache's flag decides, not the name: a column profiled as PII under a
    name the heuristic would pass is refused through the catalog's column."""

    from exmergo_dex_core.cache import (
        ColumnProfile,
        Dataset,
        DexCache,
        PIICategory,
        PIIFlag,
    )
    from exmergo_dex_core.storage import FilesystemStore

    FilesystemStore(shop).save_cache(
        DexCache(
            datasets=[
                Dataset(
                    identifier="shop.app.orders",
                    columns=[
                        ColumnProfile(
                            name="status",
                            data_type="VARCHAR",
                            pii=PIIFlag(category=PIICategory.NAME, confidence=0.95),
                        )
                    ],
                )
            ]
        )
    )
    envelope = _query(capsys, shop, "by_status")

    assert envelope["reason"] == "guard" and "orders.status" in envelope["errors"][0]
    assert cube.calls == []


@pytest.mark.parametrize(
    "clause",
    [
        '{"member": "orders.status", "operator": "between", "values": ["a"]}',
        '{"member": "orders.status", "operator": "equals", "values": [{"x": 1}]}',
        '{"member": "orders.status", "operator": "equals", "sql": "1=1"}',
        '{"or": []}',
        '{"and": [{"member": "orders.status", "operator": "set"}], "or": []}',
        '{"member": "status", "operator": "set"}',
    ],
)
def test_an_unreadable_filter_fails_closed(shop, cube, capsys, clause):
    envelope = _run(
        capsys, shop, "explore", "semantic", "query", "orders.count", "--where", clause
    )

    assert envelope["status"] == "error" and envelope["reason"] == "guard"
    assert "could not read which members" in envelope["errors"][0]
    assert cube.calls == []


def test_a_filter_that_is_not_json_is_named(shop, cube, capsys):
    envelope = _run(
        capsys,
        shop,
        "explore",
        "semantic",
        "query",
        "orders.count",
        "--where",
        "status = 'paid'",
    )

    assert envelope["status"] == "error"
    assert "is JSON" in envelope["errors"][0]
    assert cube.calls == []


# --- translation refusals --------------------------------------------------------


@pytest.mark.parametrize(
    ("argv", "says"),
    [
        (["orders.nope"], "no such metric"),
        (["orders.status"], "is a dimension"),
        (["orders.count", "--group-by", "orders.revenue"], "is a measure"),
        (["orders.count", "--grain", "month"], "has no metric_time"),
        (
            [
                "orders.count",
                "--group-by",
                "orders.ordered_at__month",
                "--grain",
                "year",
            ],
            "name one",
        ),
        (
            ["orders.count", "--group-by", "orders.ordered_at", "--grain", "decade"],
            "decade",
        ),
        (["orders.count", "--order-by", "orders.revenue"], "own metrics"),
    ],
)
def test_a_request_cube_cannot_be_sent_is_refused_by_name(
    shop, cube, capsys, argv, says
):
    envelope = _run(capsys, shop, "explore", "semantic", "query", *argv)

    assert envelope["status"] == "error", envelope
    assert says in envelope["errors"][0]
    assert "/cubejs-api/v1/sql" not in cube.paths


# --- the renderer is not a reviewed artifact -------------------------------------


def _served(meta: dict, name: str) -> dict:
    for element in meta["cubes"]:
        for key in ("measures", "dimensions"):
            for entry in element[key]:
                if entry["name"] == name:
                    return entry
    raise KeyError(name)


@pytest.mark.parametrize(
    ("edit", "says"),
    [
        (
            lambda m: _served(m, "orders.revenue").update(sql="`${CUBE}.total * 2`"),
            "different SQL",
        ),
        (
            lambda m: _served(m, "orders.revenue").update(aggType="avg"),
            "aggregates as avg",
        ),
        (
            lambda m: _served(m, "orders.status").update(name="orders.state"),
            "not in the server",
        ),
        (
            lambda m: _served(m, "orders.paid_revenue").update(filters=[]),
            "different filters",
        ),
        (
            lambda m: _served(m, "orders.ordered_at").update(granularities=[]),
            "custom granularities",
        ),
        (
            lambda m: _served(m, "order_facts.revenue").update(
                aliasMember="orders.count"
            ),
            "exposes orders.count",
        ),
    ],
)
def test_a_stale_renderer_is_refused_naming_the_member(shop, cube, capsys, edit, says):
    edit(cube.meta)
    case = "view" if "order_facts" in says or "exposes" in says else "by_status"
    if "filters" in says:
        case = "filtered"
    if "granularities" in says:
        case = "fiscal_year"
    envelope = _query(capsys, shop, case)

    assert envelope["status"] == "error" and envelope["reason"] == "guard"
    assert "serves a different model" in envelope["errors"][0]
    assert says in envelope["errors"][0]
    assert "/cubejs-api/v1/sql" not in cube.paths


def test_the_recorded_meta_agrees_with_the_model_it_was_recorded_from(
    shop, cube, capsys
):
    """The stale check passes on a server serving exactly these files, for every
    case: it compares definitions, and a false positive here would refuse every
    query a real Cube answers."""

    for case in QUERIES:
        assert _query(capsys, shop, case)["status"] == "ok", case


def test_a_derived_meta_matches_the_recorded_one_on_what_the_check_reads():
    loaded = load_files(MODEL, MODEL_FILES)
    derived = meta_for(loaded)
    from .fake_cube import recorded_meta

    real = recorded_meta()
    for element in derived["cubes"]:
        for key in ("measures", "dimensions"):
            for entry in element[key]:
                served = _served(real, entry["name"])
                for field in ("type", "aggType", "sql", "aliasMember"):
                    if field in entry:
                        assert served.get(field) == entry[field], (entry["name"], field)


# --- the plan has to be the whole answer -----------------------------------------


def _with(cube: FakeCube, case: str, **changes) -> None:
    body = json.loads(json.dumps(cube.recordings[case]["response"]))
    body["sql"].update(changes)
    cube.responses[case] = body


@pytest.mark.parametrize(
    ("changes", "says"),
    [
        (
            {
                "preAggregations": [
                    {"preAggregationId": "orders.by_status", "tableName": "x"}
                ]
            },
            "pre-aggregation (orders.by_status)",
        ),
        ({"external": True}, "external store"),
        ({"lambdaQueries": {"q": ["select 1", []]}}, "lambda"),
        ({"dataSource": "analytics"}, "data source 'analytics'"),
    ],
)
def test_a_plan_that_is_not_one_warehouse_statement_is_refused(
    shop, cube, capsys, changes, says
):
    _with(cube, "by_status", **changes)
    envelope = _query(capsys, shop, "by_status")

    assert envelope["status"] == "error" and envelope["reason"] == "guard"
    assert says in envelope["errors"][0]


def test_more_than_one_statement_is_refused(shop, cube, capsys):
    one = cube.recordings["by_status"]["response"]
    cube.responses["by_status"] = [one, one]
    envelope = _query(capsys, shop, "by_status")

    assert (
        envelope["reason"] == "guard"
        and "more than one statement" in envelope["errors"][0]
    )


def test_a_plan_over_a_table_no_cube_names_is_refused(shop, cube, capsys):
    """What a matched rollup reads, even where `preAggregations` says nothing."""

    body = json.loads(json.dumps(cube.recordings["by_status"]["response"]))
    body["sql"]["sql"][0] = body["sql"]["sql"][0].replace(
        "app.orders", "prod_pre_aggregations.orders_by_status"
    )
    cube.responses["by_status"] = body
    envelope = _query(capsys, shop, "by_status")

    assert envelope["reason"] == "guard"
    assert "prod_pre_aggregations.orders_by_status" in envelope["errors"][0]


def test_sql_rendered_for_another_warehouse_is_refused(shop, cube, capsys):
    cube.responses["golden"] = recorded("postgres")["golden"]["response"]
    envelope = _query(capsys, shop, "golden")

    assert envelope["reason"] == "guard"
    assert "another warehouse than duckdb" in envelope["errors"][0]
    assert "CUBEJS_DB_TYPE=duckdb" in envelope["errors"][0]


def test_a_cube_on_another_data_source_is_refused_before_rendering(
    tmp_path, capsys, monkeypatch
):
    shutil.copytree(MODEL, tmp_path / "model")
    orders = tmp_path / "model" / "cubes" / "orders.yml"
    orders.write_text(
        orders.read_text().replace(
            "    sql_table: app.orders\n",
            "    sql_table: app.orders\n    data_source: analytics\n",
        ),
        encoding="utf-8",
    )
    write_shop(tmp_path / "shop.duckdb")
    _config(tmp_path)
    cube = FakeCube("duckdb").install(monkeypatch)

    envelope = _query(capsys, tmp_path, "by_status")

    assert envelope["reason"] == "guard"
    assert "orders (analytics)" in envelope["errors"][0]
    assert cube.calls == []


def test_a_relation_the_connection_lacks_is_refused_before_spend(
    tmp_path, capsys, monkeypatch
):
    shutil.copytree(MODEL, tmp_path / "model")
    (tmp_path / "model" / "cubes" / "ghost.yml").write_text(
        "cubes:\n  - name: ghost\n    sql_table: app.ghost\n"
        "    measures:\n      - name: count\n        type: count\n",
        encoding="utf-8",
    )
    write_shop(tmp_path / "shop.duckdb")
    _config(
        tmp_path, files=[*(f"model/{n}" for n in MODEL_FILES), "model/cubes/ghost.yml"]
    )
    loaded = load_files(
        tmp_path, [*(f"model/{n}" for n in MODEL_FILES), "model/cubes/ghost.yml"]
    )
    cube = FakeCube("duckdb", meta=meta_for(loaded)).install(monkeypatch)
    cube.render = lambda _q: {
        "sql": {
            "sql": [
                'SELECT count(*) "ghost__count" FROM app.ghost AS "ghost" LIMIT 51',
                [],
            ],
            "preAggregations": [],
            "external": False,
            "lambdaQueries": {},
            "dataSource": "default",
            "aliasNameToMember": {"ghost__count": "ghost.count"},
        }
    }

    envelope = _run(capsys, tmp_path, "explore", "semantic", "query", "ghost.count")

    assert envelope["status"] == "error"
    assert "app.ghost" in envelope["errors"][0]
    assert "does not have" in envelope["errors"][0]


# --- the connectors, the hosts and the token -------------------------------------


@pytest.mark.parametrize("connector", ["databricks", "clickhouse", "trino"])
def test_a_connector_with_no_reviewed_golden_is_refused_by_name(
    tmp_path, capsys, monkeypatch, connector
):
    shutil.copytree(MODEL, tmp_path / "model")
    _config(tmp_path, connector=connector)
    cube = FakeCube("duckdb").install(monkeypatch)

    envelope = _query(capsys, tmp_path, "by_status")

    assert envelope["status"] == "error"
    assert f"not on {connector}" in envelope["errors"][0]
    assert cube.calls == []


def test_a_remote_host_is_refused_before_the_secret_is_read(
    tmp_path, capsys, monkeypatch
):
    shutil.copytree(MODEL, tmp_path / "model")
    write_shop(tmp_path / "shop.duckdb")
    _config(tmp_path, extra="")
    config = tmp_path / ".dex" / "config.yml"
    config.write_text(
        config.read_text().replace(
            "  cube:\n", "  cube:\n    url: https://cube.example.com/cubejs-api\n"
        ),
        encoding="utf-8",
    )
    cube = FakeCube("duckdb").install(monkeypatch)
    monkeypatch.delenv("CUBEJS_API_SECRET")
    monkeypatch.delenv("DEX_CUBE_ALLOWED_HOSTS", raising=False)

    envelope = _query(capsys, tmp_path, "by_status")

    assert envelope["reason"] == "guard"
    assert "cube.example.com" in envelope["errors"][0]
    assert "DEX_CUBE_ALLOWED_HOSTS" in envelope["errors"][0]
    assert cube.calls == []

    monkeypatch.setenv("DEX_CUBE_ALLOWED_HOSTS", "cube.example.com")
    monkeypatch.setenv("CUBEJS_API_SECRET", SECRET)
    admitted = _query(capsys, tmp_path, "by_status")
    assert admitted["status"] == "ok", admitted["errors"]
    assert {r.url.host for r in cube.calls} == {"cube.example.com"}


def test_no_secret_is_named_as_the_missing_environment(shop, cube, capsys, monkeypatch):
    monkeypatch.delenv("CUBEJS_API_SECRET")
    envelope = _query(capsys, shop, "by_status")

    assert envelope["status"] == "error"
    assert "CUBEJS_API_SECRET" in envelope["errors"][0]


def test_a_rejected_token_says_which_secret_to_check(shop, monkeypatch, capsys):
    FakeCube("duckdb", secret="the-server-s-own").install(monkeypatch)  # noqa: S106
    monkeypatch.setenv("CUBEJS_API_SECRET", "a-different-one")
    envelope = _query(capsys, shop, "by_status")

    assert "rejected dex's token (HTTP 403)" in envelope["errors"][0]


def test_a_query_cube_cannot_plan_is_a_resolution_error(shop, cube, capsys):
    cube.render = None
    cube.recordings = {}
    envelope = _query(capsys, shop, "by_status")

    assert envelope["status"] == "error"
    assert envelope["errors"][0].startswith("could not resolve the metric query")


def test_hosted_execution_is_not_offered(shop, cube, capsys):
    envelope = _query(capsys, shop, "by_status", "--api")

    assert envelope["status"] == "error"
    assert "no hosted execution" in envelope["errors"][0]
    assert cube.calls == []


def test_the_secret_and_the_token_never_reach_an_envelope(
    shop, cube, capsys, monkeypatch
):
    outputs = [json.dumps(_query(capsys, shop, case)) for case in QUERIES]
    outputs.append(json.dumps(_values(capsys, shop, "values_status")))
    cube.status = 500
    outputs.append(json.dumps(_query(capsys, shop, "by_status")))
    cube.status = None
    FakeCube("duckdb", secret="other").install(monkeypatch)  # noqa: S106
    monkeypatch.setenv("CUBEJS_API_SECRET", SECRET)
    outputs.append(json.dumps(_query(capsys, shop, "by_status")))

    tokens = {r.headers["Authorization"] for r in cube.calls}
    joined = "\n".join(outputs)
    assert tokens and SECRET not in joined
    assert not any(token in joined for token in tokens)
    assert "eyJ" not in joined


def test_each_request_carries_a_fresh_short_lived_token(shop, cube, capsys):
    import base64

    _query(capsys, shop, "by_status")
    for request in cube.calls:
        payload = request.headers["Authorization"].split(".")[1]
        claims = json.loads(
            base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
        )
        assert claims["exp"] - claims["iat"] == 60
        assert claims["dex_scopes"] == ["meta", "sql"]


# --- a billed connector prices before any spend ----------------------------------


def test_a_billed_connector_asks_before_any_spend(
    fake_bq_client, tmp_path, capsys, monkeypatch
):
    import exmergo_dex_core.connect as connect_mod
    from exmergo_dex_core.adapters.bigquery import BigQueryAdapter
    from exmergo_dex_core.config import BigQueryTarget, load_config
    from exmergo_dex_core.connect import new_cost_gate
    from exmergo_dex_core.storage import FilesystemStore

    (tmp_path / "model").mkdir()
    (tmp_path / "model" / "customers.yml").write_text(
        "cubes:\n  - name: customers\n    sql_table: test-proj.shop.customers\n"
        "    dimensions:\n      - name: id\n        sql: id\n        type: number\n"
        "        primary_key: true\n"
        "    measures:\n      - name: count\n        type: count\n",
        encoding="utf-8",
    )
    _config(
        tmp_path,
        connector="bigquery",
        extra=(
            "bigquery:\n  project: test-proj\nbudget:\n  session_ceiling: 1073741824\n"
        ),
        files=["model/customers.yml"],
    )
    loaded = load_files(tmp_path, ["model/customers.yml"])
    cube = FakeCube("bigquery", meta=meta_for(loaded)).install(monkeypatch)
    cube.render = lambda _q: {
        "sql": {
            "sql": [
                "SELECT count(`customers`.id) `customers__count` \n"
                "FROM  test-proj.shop.customers  AS `customers`\nLIMIT 51",
                [],
            ],
            "preAggregations": [],
            "external": False,
            "lambdaQueries": {},
            "dataSource": "default",
            "aliasNameToMember": {"customers__count": "customers.count"},
        }
    }
    config = load_config(tmp_path)
    store = FilesystemStore(tmp_path)

    def opener(**opened):
        return BigQueryAdapter(
            project="test-proj",
            cost_gate=new_cost_gate(
                "bigquery",
                config,
                store,
                budget=opened.get("budget"),
                confirmed=opened.get("confirmed", False),
                command=opened.get("command"),
            ),
            target=BigQueryTarget(),
            client=fake_bq_client,
            principal_type="user",
        )

    monkeypatch.setattr(connect_mod, "open_adapter", opener)

    envelope = _run(capsys, tmp_path, "explore", "semantic", "query", "customers.count")

    assert envelope["status"] == "needs_confirmation", envelope["errors"]
    assert envelope["cost"]["estimate"] > 0
    assert fake_bq_client.query_calls and all(
        c.dry_run for c in fake_bq_client.query_calls
    )
    assert "test-proj.shop.customers" in fake_bq_client.query_calls[-1].sql.replace(
        "`", ""
    )


def test_cube_sql_runs_in_a_utc_session(shop, cube, capsys):
    """What Cube's own drivers set before running what Cube rendered: without
    it a timestamp with no zone is read in whatever zone the session holds."""

    cube.render = lambda _q: {
        "sql": {
            "sql": ["SELECT current_setting('TimeZone') \"orders__count\"", []],
            "preAggregations": [],
            "external": False,
            "lambdaQueries": {},
            "dataSource": "default",
            "aliasNameToMember": {"orders__count": "orders.count"},
        }
    }
    envelope = _run(capsys, shop, "explore", "semantic", "query", "orders.count")

    assert _cells(envelope) == [["UTC"]]


def test_a_changed_member_a_calculated_measure_reads_is_stale(
    tmp_path, capsys, monkeypatch
):
    """`aov` is `{revenue} / {order_count}` on both sides, and still means
    something else when the server's `revenue` differs."""

    from .conftest import ORDERS_LAYER, repo

    root = repo(tmp_path, *ORDERS_LAYER)
    files = [f"model/{entry['name']}.yml" for entry in ORDERS_LAYER]
    cube = FakeCube(meta=meta_for(load_files(root, files))).install(monkeypatch)
    _served(cube.meta, "orders.revenue")["sql"] = "`${CUBE}.amount * 2`"

    envelope = _run(capsys, root, "explore", "semantic", "query", "orders.aov")

    assert envelope["reason"] == "guard", envelope
    assert "orders.revenue has different SQL there" in envelope["errors"][0]
    assert cube.paths == ["/cubejs-api/v1/meta"]
