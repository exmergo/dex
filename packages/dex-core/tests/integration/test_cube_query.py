"""Live runs of a Cube metric query: a real Cube Core renders (#913) or executes (#914).

The unit tests (``tests/cube_vendor``) replay what Cube rendered; these ask a
running Cube Core for it, so a planner change, a model the server refuses, or a
render the recordings no longer match fails here. The server is the render-only
container ``scripts/setup_cube_dev.sh`` starts: it knows the warehouse's type and
holds no credential for it, so every answer below was executed by dex.

DuckDB runs over the fixture rows the unit tests use, written to a file this
suite owns. Postgres runs over the ``app`` schema ``scripts/postgres_seed.sql``
seeds, read as ``dex_ro``. ClickHouse and Trino (#1410) run over the ``app``
tables ``scripts/setup_clickhouse_dev.sh`` and ``scripts/setup_trino_dev.sh``
seed. Each runs the query cases in ``BY_HAND`` and is held to the same question
asked in hand-written SQL.

``--api`` (#914) asks the *executing* containers ``scripts/setup_cube_dev.sh
--execute`` starts the same questions: Cube runs them on the same rows (DuckDB
in memory from ``scripts/cube/shop.sql``, Postgres as ``dex_ro``), and every
answer is held to the same hand-written SQL, and carries the cost-guard warning.

Needs ``CUBEJS_API_SECRET`` and ``DEX_TEST_CUBE_URL_DUCKDB`` /
``DEX_TEST_CUBE_URL_POSTGRES`` (``source ~/.cache/dex-dogfood/cube.env``), plus
``DEX_TEST_PG_DSN`` for Postgres, and the ``[semantic-api]`` extra for httpx. The
``cube_execute`` tests need ``DEX_TEST_CUBE_EXEC_URL_DUCKDB`` /
``DEX_TEST_CUBE_EXEC_URL_POSTGRES`` too.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest
import yaml
from cube_vendor.cube_cases import MODEL, MODEL_FILES, QUERIES, VALUES, write_shop

from exmergo_dex_core.cli import main

from .conftest import assert_ok, integration_budget, trino_client

pytestmark = [pytest.mark.integration, pytest.mark.cube]


def _run(capsys, root: Path, *argv: str) -> tuple[int, dict]:
    rc = main(["--repo-root", str(root), *argv])
    return rc, json.loads(capsys.readouterr().out)


def _argv(case: str) -> list[str]:
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
    return argv


def _repo(root: Path, connector: str, url: str, extra: dict) -> Path:
    shutil.copytree(MODEL, root / "model")
    config = {
        "connector": connector,
        **extra,
        "semantic": {
            "vendor": "cube",
            "cube": {"url": url, "files": [f"model/{n}" for n in MODEL_FILES]},
        },
    }
    (root / ".dex").mkdir(exist_ok=True)
    (root / ".dex" / "config.yml").write_text(yaml.safe_dump(config), encoding="utf-8")
    return root


def _rounded(rows) -> list[tuple]:
    def one(value):
        if value is None or isinstance(value, str):
            return value
        try:
            return round(float(value), 4)
        except (TypeError, ValueError):
            return str(value)[:19]

    # A NULL sorts with no other value, so order by the cells as text.
    return sorted((tuple(one(v) for v in row) for row in rows), key=repr)


# The question each case asks, by hand, over the same tables. `{tz}` buckets a
# timestamp in UTC in the warehouse's own idiom.
BY_HAND = {
    "by_status": "SELECT status, sum(total), count(id) FROM app.orders GROUP BY 1",
    "by_month": ("SELECT {month}, sum(total) FROM app.orders GROUP BY 1"),
    "by_grain_flag": "SELECT {quarter}, count(id) FROM app.orders GROUP BY 1",
    "filtered": (
        "SELECT status, count(id), sum(CASE WHEN status = 'paid' THEN total END)"
        " FROM app.orders WHERE status IN ('paid', 'shipped') GROUP BY 1"
    ),
    "having": (
        "SELECT status, count(id) FROM app.orders GROUP BY 1 HAVING count(id) > 2"
    ),
    "two_hops": (
        "SELECT p.category, o.status, sum(i.quantity), sum(i.quantity * i.unit_price)"
        " FROM app.order_items i LEFT JOIN app.orders o ON i.order_id = o.id"
        " LEFT JOIN app.products p ON i.product_id = p.id GROUP BY 1, 2"
    ),
    "view": "SELECT status, sum(total) FROM app.orders GROUP BY 1",
}

IDIOMS = {
    "duckdb": {
        "month": "date_trunc('month', timezone('UTC', ordered_at))",
        "quarter": "date_trunc('quarter', timezone('UTC', ordered_at))",
    },
    "postgres": {
        "month": "date_trunc('month', ordered_at AT TIME ZONE 'UTC')",
        "quarter": "date_trunc('quarter', ordered_at AT TIME ZONE 'UTC')",
    },
    "clickhouse": {
        "month": "toStartOfMonth(toTimeZone(ordered_at, 'UTC'))",
        "quarter": "toStartOfQuarter(toTimeZone(ordered_at, 'UTC'))",
    },
    # The seeded column carries no zone, and a UTC session reads it as UTC.
    "trino": {
        "month": "date_trunc('month', ordered_at)",
        "quarter": "date_trunc('quarter', ordered_at)",
    },
}


def _check_every_case(
    capsys, root: Path, connector: str, by_hand, confirm: list[str], *, api=False
):
    execution = "vendor" if api else "dex"
    flags = [*confirm, *(["--api"] if api else [])]
    for case, sql in BY_HAND.items():
        rc, envelope = _run(capsys, root, *_argv(case), *flags)
        data = assert_ok(rc, envelope)["data"]
        assert (data["vendor"], data["execution"]) == ("cube", execution), case
        guarded = [w for w in envelope["warnings"] if "cost guard" in w]
        assert len(guarded) == (1 if api else 0), case
        want = _rounded(by_hand(sql.format(**IDIOMS[connector])))
        got = _rounded(data["cells"])
        if case.startswith("by_") and case != "by_status":
            # A bucket comes back as the warehouse's timestamp; compare the day.
            want = [(str(b)[:10], *rest) for b, *rest in want]
            got = [(str(b)[:10], *rest) for b, *rest in got]
        assert got == want, case

    for case, (dimension, metrics) in VALUES.items():
        argv = ["explore", "semantic", "values", dimension]
        for metric in metrics:
            argv += ["--metric", metric]
        rc, envelope = _run(capsys, root, *argv, *flags)
        data = assert_ok(rc, envelope)["data"]
        assert data["execution"] == execution, case
        assert data["scoped_to"] == metrics, case
        assert data["cells"], case


# --- DuckDB ------------------------------------------------------------------------


def test_duckdb_answers_every_case_rendered_by_a_live_cube(tmp_path, capsys):
    import duckdb

    root = _repo(
        tmp_path,
        "duckdb",
        os.environ["DEX_TEST_CUBE_URL_DUCKDB"],
        {"duckdb": {"path": str(tmp_path / "shop.duckdb")}},
    )
    write_shop(tmp_path / "shop.duckdb")

    def by_hand(sql: str):
        conn = duckdb.connect(str(tmp_path / "shop.duckdb"), read_only=True)
        try:
            return conn.execute(sql).fetchall()
        finally:
            conn.close()

    _check_every_case(capsys, root, "duckdb", by_hand, [])


# --- Postgres ----------------------------------------------------------------------


@pytest.mark.postgres
def test_postgres_answers_every_case_rendered_by_a_live_cube(
    tmp_path, capsys, pg_dsn, monkeypatch
):
    import psycopg

    monkeypatch.setenv("DATABASE_URL", pg_dsn)
    root = _repo(
        tmp_path,
        "postgres",
        os.environ["DEX_TEST_CUBE_URL_POSTGRES"],
        {"postgres": {"schemas": ["app"]}, "budget": integration_budget(None)},
    )
    _, asked = _run(capsys, root, *_argv("by_status"))
    assert asked["status"] == "needs_confirmation", asked["errors"]

    def by_hand(sql: str):
        with psycopg.connect(pg_dsn) as conn:
            return conn.execute(sql).fetchall()

    _check_every_case(
        capsys, root, "postgres", by_hand, ["--confirm", "--budget", "30"]
    )


@pytest.mark.postgres
def test_a_live_cube_serving_an_edited_model_is_refused_as_stale(
    tmp_path, capsys, pg_dsn, monkeypatch
):
    """The server was started on `scripts/cube/model`; the repository edits a
    measure's SQL after it started, which Cube outside dev mode never sees."""

    monkeypatch.setenv("DATABASE_URL", pg_dsn)
    root = _repo(
        tmp_path,
        "postgres",
        os.environ["DEX_TEST_CUBE_URL_POSTGRES"],
        {"postgres": {"schemas": ["app"]}, "budget": integration_budget(None)},
    )
    orders = root / "model" / "cubes" / "orders.yml"
    orders.write_text(
        orders.read_text().replace(
            "      - name: revenue\n        sql: total\n",
            "      - name: revenue\n        sql: total * 2\n",
        ),
        encoding="utf-8",
    )

    _, envelope = _run(capsys, root, *_argv("by_status"))

    assert envelope["reason"] == "guard", envelope
    assert "orders.revenue has different SQL there" in envelope["errors"][0]


# --- ClickHouse and Trino (#1410) --------------------------------------------------


@pytest.mark.clickhouse
def test_clickhouse_answers_every_case_rendered_by_a_live_cube(
    tmp_path, capsys, ch_dsn, monkeypatch
):
    import clickhouse_connect

    monkeypatch.setenv("CLICKHOUSE_URL", ch_dsn)
    root = _repo(
        tmp_path,
        "clickhouse",
        os.environ["DEX_TEST_CUBE_URL_CLICKHOUSE"],
        {"clickhouse": {"databases": ["app"]}, "budget": integration_budget(None)},
    )
    _, asked = _run(capsys, root, *_argv("by_status"))
    assert asked["status"] == "needs_confirmation", asked["errors"]

    def by_hand(sql: str):
        client = clickhouse_connect.get_client(dsn=ch_dsn)
        try:
            # Cube's ClickHouse driver and dex's adapter both run with
            # join_use_nulls = 1, so an unmatched row is NULL, as in SQL.
            return client.query(sql, settings={"join_use_nulls": 1}).result_rows
        finally:
            client.close()

    _check_every_case(
        capsys, root, "clickhouse", by_hand, ["--confirm", "--budget", "30"]
    )


@pytest.mark.trino
def test_trino_answers_every_case_rendered_by_a_live_cube(
    tmp_path, capsys, trino_url, monkeypatch
):
    """dex names every Trino relation with its catalog, so the model here is the
    copy the ``cube`` job starts its Trino renderer on (``--model``): each
    ``sql_table`` under the ``memory`` catalog. The seeded timestamps carry no
    zone and the Trino client sends this machine's, so off UTC the buckets
    only match the hand-written ones because dex pins the session."""

    monkeypatch.setenv("TRINO_URL", trino_url)
    root = _repo(
        tmp_path,
        "trino",
        os.environ["DEX_TEST_CUBE_URL_TRINO"],
        {"trino": {"catalogs": ["memory"]}, "budget": integration_budget(None)},
    )
    for name in MODEL_FILES:
        file = root / "model" / name
        file.write_text(
            file.read_text().replace("sql_table: app.", "sql_table: memory.app."),
            encoding="utf-8",
        )
    _, asked = _run(capsys, root, *_argv("by_status"))
    assert asked["status"] == "needs_confirmation", asked["errors"]

    def by_hand(sql: str):
        conn = trino_client(trino_url)
        try:
            cursor = conn.cursor()
            cursor.execute("SET TIME ZONE 'UTC'")
            cursor.fetchall()
            cursor.execute(sql.replace(" app.", " memory.app."))
            return cursor.fetchall()
        finally:
            conn.close()

    _check_every_case(capsys, root, "trino", by_hand, ["--confirm", "--budget", "30"])


def test_a_live_cube_enforces_dex_scopes_and_refuses_load():
    """The server was started mounting scripts/cube/cube.js; its contextToApiScopes
    restricts endpoints to the token's dex_scopes, so /v1/load returns 403 (#971)."""

    import httpx

    from exmergo_dex_core.cube.transport import mint_token

    url = os.environ["DEX_TEST_CUBE_URL_DUCKDB"]
    token = mint_token(os.environ["CUBEJS_API_SECRET"])
    headers = {"Authorization": token}

    meta = httpx.get(f"{url}/v1/meta", headers=headers, timeout=10.0)
    assert meta.status_code == 200

    load = httpx.post(
        f"{url}/v1/load",
        headers=headers,
        json={"query": {"measures": ["orders.count"]}},
        timeout=10.0,
    )
    assert load.status_code == 403


# --- --api: an executing Cube Core runs the query (#914) -----------------------------


def _duckdb_by_hand(path: Path):
    import duckdb

    def by_hand(sql: str):
        conn = duckdb.connect(str(path), read_only=True)
        try:
            return conn.execute(sql).fetchall()
        finally:
            conn.close()

    return by_hand


@pytest.mark.cube_execute
def test_duckdb_api_answers_every_case_executed_by_a_live_cube(tmp_path, capsys):
    """The executing container built its in-memory DuckDB from shop.sql; dex's
    file is built from the same statements, so the hand-written answer and
    Cube's are over one set of rows."""

    root = _repo(
        tmp_path,
        "duckdb",
        os.environ["DEX_TEST_CUBE_EXEC_URL_DUCKDB"],
        {"duckdb": {"path": str(tmp_path / "shop.duckdb")}},
    )
    write_shop(tmp_path / "shop.duckdb")

    _check_every_case(
        capsys, root, "duckdb", _duckdb_by_hand(tmp_path / "shop.duckdb"), [], api=True
    )


@pytest.mark.cube_execute
@pytest.mark.postgres
def test_postgres_api_answers_every_case_executed_by_a_live_cube(
    tmp_path, capsys, pg_dsn, monkeypatch
):
    """Cube runs the query, so there is no estimate to confirm: the first call
    answers, with the cost guard said to be unavailable."""

    import psycopg

    monkeypatch.setenv("DATABASE_URL", pg_dsn)
    root = _repo(
        tmp_path,
        "postgres",
        os.environ["DEX_TEST_CUBE_EXEC_URL_POSTGRES"],
        {"postgres": {"schemas": ["app"]}, "budget": integration_budget(None)},
    )

    def by_hand(sql: str):
        with psycopg.connect(pg_dsn) as conn:
            return conn.execute(sql).fetchall()

    _check_every_case(capsys, root, "postgres", by_hand, [], api=True)


@pytest.mark.cube_execute
def test_a_live_executing_cube_refuses_a_load_token_a_render():
    """A load token claims meta and data, so the executing server refuses it
    /v1/sql, as the render server refuses a render token /v1/load."""

    import httpx

    from exmergo_dex_core.cube.transport import LOAD_SCOPES, mint_token

    url = os.environ["DEX_TEST_CUBE_EXEC_URL_DUCKDB"]
    token = mint_token(os.environ["CUBEJS_API_SECRET"], scopes=LOAD_SCOPES)
    query = {"query": {"measures": ["orders.count"]}}
    headers = {"Authorization": token}

    load = httpx.post(f"{url}/v1/load", headers=headers, json=query, timeout=30.0)
    assert load.status_code == 200, load.text
    sql = httpx.post(f"{url}/v1/sql", headers=headers, json=query, timeout=30.0)
    assert sql.status_code == 403
