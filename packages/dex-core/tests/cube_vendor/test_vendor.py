"""Cube executing the query (#914): ``--api`` on a Cube Core, and Cube Cloud.

The fake server's ``/v1/load`` runs the recorded plan for the same query on the
same DuckDB file dex reads, so a vendor-executed result is held to the one dex
computes itself: the two executions have to agree to the row.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import httpx
import pytest

pytest.importorskip("sqlglot")
pytest.importorskip("httpx")
pytest.importorskip("duckdb")

from exmergo_dex_core.cli import main
from exmergo_dex_core.connect import SemanticSource
from exmergo_dex_core.cube.hosted import (
    CUBE_CLOUD_DISCLOSURE,
    CUBE_HOSTED_CATALOG_GAPS,
    CUBE_VENDOR_COST_WARNING,
    decode_load,
)
from exmergo_dex_core.cube.loader import load_files
from exmergo_dex_core.cube.transport import (
    LOAD_PATHS,
    RENDER_PATHS,
    CubeTransport,
    CubeTransportError,
)
from exmergo_dex_core.engine import DexEngine
from exmergo_dex_core.explore.semantic import resolve_semantic_layer
from exmergo_dex_core.explore.semantic.conformance import (
    SemanticBackendContract,
    SemanticCatalogContract,
)
from exmergo_dex_core.explore.semantic.cube import LocalCubeLayer
from exmergo_dex_core.explore.semantic.cube_vendor import CubeVendorLayer

from .cube_cases import MODEL, MODEL_FILES, QUERIES, VALUES, write_shop
from .fake_cube import FakeCube, meta_for

CLOUD_URL = "https://shop.aws-us-east-1.cubecloudapp.dev/cubejs-api"
CLOUD_TOKEN = "cube-cloud-token-0123456789abcdef"  # noqa: S105 (a test fixture)

# The cases a vendor execution answers as the render path does. `relative_range`
# is resolved by the server's clock, `golden` is the dialect fixture.
CASES = [name for name in QUERIES if name not in {"relative_range", "golden"}]


def _run(capsys, root: Path, *argv: str) -> dict:
    main(["--repo-root", str(root), *argv])
    return json.loads(capsys.readouterr().out)


def _config(root: Path, *, deployment: str | None = None, url: str | None = None):
    lines = [
        "connector: duckdb",
        "duckdb:",
        f"  path: {root / 'shop.duckdb'}",
        "semantic:",
        "  vendor: cube",
    ]
    if deployment:
        lines.append(f"  deployment: {deployment}")
    lines += ["  cube:", "    files:"]
    lines += [f"      - model/{name}" for name in MODEL_FILES]
    if url:
        lines.append(f"    url: {url}")
    (root / ".dex").mkdir(exist_ok=True)
    (root / ".dex" / "config.yml").write_text("\n".join(lines) + "\n", "utf-8")


@pytest.fixture
def shop(tmp_path: Path) -> Path:
    shutil.copytree(MODEL, tmp_path / "model")
    write_shop(tmp_path / "shop.duckdb")
    _config(tmp_path)
    return tmp_path


@pytest.fixture
def cloud(tmp_path: Path, monkeypatch) -> Path:
    shutil.copytree(MODEL, tmp_path / "model")
    write_shop(tmp_path / "shop.duckdb")
    _config(tmp_path, deployment="cube_cloud", url=CLOUD_URL)
    monkeypatch.setenv("CUBE_API_TOKEN", CLOUD_TOKEN)
    monkeypatch.delenv("CUBEJS_API_SECRET", raising=False)
    return tmp_path


@pytest.fixture(autouse=True)
def _no_wait(monkeypatch):
    monkeypatch.setattr("exmergo_dex_core.cube.transport.time.sleep", lambda _: None)


def _server(monkeypatch, root: Path, *, cloud: bool = False) -> FakeCube:
    cube = FakeCube("duckdb")
    cube.warehouse = root / "shop.duckdb"
    cube.install(monkeypatch)
    if cloud:
        cube.cloud_token = CLOUD_TOKEN
        monkeypatch.delenv("CUBEJS_API_SECRET", raising=False)
    return cube


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


def _rows(envelope: dict) -> list[tuple]:
    assert envelope["status"] == "ok", envelope["errors"]
    data = envelope["data"]
    out = []
    for row in data["cells"]:
        shaped = []
        for value in row:
            try:
                shaped.append(round(float(value), 6))
            except (TypeError, ValueError):
                shaped.append(str(value)[:19].replace("T", " "))
        out.append(tuple(shaped))
    return out


# --- --api on a Cube Core: the server executes ----------------------------------------


@pytest.mark.parametrize("case", CASES)
def test_api_answers_every_case_as_the_render_path_does(
    shop, monkeypatch, capsys, case
):
    cube = _server(monkeypatch, shop)
    local = _run(capsys, shop, *_argv(case))
    api = _run(capsys, shop, *_argv(case), "--api")

    assert api["data"]["columns"] == local["data"]["columns"]
    assert sorted(_rows(api)) == sorted(_rows(local))
    assert (api["data"]["execution"], api["data"]["deployment"]) == ("vendor", "local")
    assert api["warnings"].count(CUBE_VENDOR_COST_WARNING) == 1
    assert CUBE_VENDOR_COST_WARNING not in local["warnings"]
    assert api["cost"]["paradigm"] == "hosted"
    loads = [c for c in cube.calls if c.url.path.endswith("/v1/load")]
    assert len(loads) == 1


@pytest.mark.parametrize("case", sorted(VALUES))
def test_api_values_match_the_render_path(shop, monkeypatch, capsys, case):
    _server(monkeypatch, shop)
    dimension, metrics = VALUES[case]
    argv = ["explore", "semantic", "values", dimension]
    for metric in metrics:
        argv += ["--metric", metric]
    local = _run(capsys, shop, *argv)
    api = _run(capsys, shop, *argv, "--api")

    assert api["data"]["columns"] == local["data"]["columns"]
    assert sorted(_rows(api)) == sorted(_rows(local))
    assert api["data"]["scoped_to"] == local["data"]["scoped_to"]
    assert CUBE_VENDOR_COST_WARNING in api["warnings"]


def test_api_on_a_core_plans_the_query_before_it_loads_it(shop, monkeypatch, capsys):
    """/v1/meta carries no sql_table, so the Core is asked for its plan (which
    runs nothing) and the plan's tables are held to the files before the load."""

    cube = _server(monkeypatch, shop)
    _run(capsys, shop, *_argv("by_status"), "--api")

    assert [c.url.path.rsplit("/v1", 1)[1] for c in cube.calls] == [
        "/meta",
        "/sql",
        "/load",
    ]


def test_a_core_reading_another_table_is_refused_before_it_runs_anything(
    shop, monkeypatch, capsys
):
    cube = _server(monkeypatch, shop)
    plan = json.loads(json.dumps(cube.recordings["by_status"]["response"]))
    plan["sql"]["sql"][0] = plan["sql"]["sql"][0].replace(
        "app.orders", "app.orders_old"
    )
    cube.render = lambda query: plan

    envelope = _run(capsys, shop, *_argv("by_status"), "--api")

    assert envelope["status"] == "error"
    assert "orders_old" in envelope["errors"][0]
    assert not [c for c in cube.calls if c.url.path.endswith("/v1/load")]


def test_an_unreadable_meta_refuses_before_anything_runs(shop, monkeypatch, capsys):
    cube = FakeCube("duckdb")
    cube.warehouse = shop / "shop.duckdb"
    original = cube.handler

    def handler(request):
        if request.url.path.endswith("/v1/meta"):
            cube.calls.append(request)
            return httpx.Response(503, json={"error": "unavailable"})
        return original(request)

    cube.handler = handler
    cube.install(monkeypatch)
    envelope = _run(capsys, shop, *_argv("by_status"), "--api")

    assert envelope["status"] == "error"
    assert not [c for c in cube.calls if c.url.path.endswith("/v1/load")]


def test_a_view_member_exposing_a_pii_member_is_refused(cloud, monkeypatch, capsys):
    """A view member carries no meta of its own; the cube member it exposes is
    marked, and that mark governs the view member too."""

    cube = _server(monkeypatch, cloud, cloud=True)
    for element in cube.meta["cubes"]:
        for entry in element["dimensions"]:
            if entry["name"] == "orders.status":
                entry["meta"] = {"pii": True}
    envelope = _run(
        capsys,
        cloud,
        "explore",
        "semantic",
        "query",
        "order_facts.revenue",
        "--group-by",
        "order_facts.status",
    )

    assert envelope["status"] == "error"
    assert "order_facts.status" in envelope["errors"][0]
    assert not [c for c in cube.calls if c.url.path.endswith("/v1/load")]


def test_cube_cloud_never_renders(cloud, monkeypatch, capsys):
    cube = _server(monkeypatch, cloud, cloud=True)
    _run(capsys, cloud, *_argv("by_status"))

    assert {c.url.path.rsplit("/v1", 1)[1] for c in cube.calls} == {"/meta", "/load"}


def test_a_load_past_its_deadline_says_so(shop, monkeypatch, capsys):
    cube = _server(monkeypatch, shop)
    cube.waits = 5
    monkeypatch.setattr("exmergo_dex_core.cube.transport._LOAD_DEADLINE_SECONDS", 0.0)
    envelope = _run(capsys, shop, *_argv("by_status"), "--api")

    assert envelope["status"] == "error"
    assert "still running the query" in envelope["errors"][0]


def test_a_quoted_value_spanning_lines_is_masked(shop, monkeypatch, capsys):
    cube = _server(monkeypatch, shop)
    cube.load_error = (
        400,
        "Conversion Error: Could not convert string 'alice@example.com\nsecond "
        "line' to INT32",
    )
    envelope = _run(capsys, shop, *_argv("by_status"), "--api")

    dumped = json.dumps(envelope)
    assert [c for c in cube.calls if c.url.path.endswith("/v1/load")]
    assert (
        "Could not convert string <value withheld> to INT32" in (envelope["errors"][0])
    )
    assert "alice@example.com" not in dumped
    assert "second line" not in dumped


def test_a_load_whose_body_outlasts_the_deadline_is_stopped(monkeypatch):
    """HTTPX bounds the wait for each chunk, not the whole body: a server that
    keeps dripping bytes is stopped at the deadline, while a body that arrives
    in time is kept."""

    clock = {"now": 0.0}
    monkeypatch.setattr(
        "exmergo_dex_core.cube.transport.time.monotonic", lambda: clock["now"]
    )
    monkeypatch.setenv("CUBEJS_API_SECRET", "s")

    pulled = []

    def stream(seconds_per_chunk):
        class Body(httpx.SyncByteStream):
            def __iter__(self):
                for piece in (b'{"data": [', b'{"a": "1"}', b"]}"):
                    pulled.append(piece)
                    clock["now"] += seconds_per_chunk
                    yield piece

        return Body()

    def client(seconds_per_chunk):
        def handler(request):
            return httpx.Response(200, stream=stream(seconds_per_chunk))

        return httpx.Client(transport=httpx.MockTransport(handler))

    # 50 seconds a chunk: the second arrives at 100, past the 90-second
    # deadline, and the read stops there without asking for the third. A
    # read that stalls instead is bounded by its own timeout, which is never
    # more than the time the deadline had left when the request started.
    slow = CubeTransport(None, execution="vendor", client=client(50.0))
    with pytest.raises(CubeTransportError, match="still running the query"):
        slow.load({"measures": ["orders.count"]})
    assert len(pulled) == 2

    clock["now"] = 0.0
    pulled.clear()
    quick = CubeTransport(None, execution="vendor", client=client(1.0))
    assert quick.load({"measures": ["orders.count"]})["data"] == [{"a": "1"}]


def _plan(cube, **changes):
    plan = json.loads(json.dumps(cube.recordings["by_status"]["response"]))
    plan["sql"].update(changes)
    return plan


def test_a_core_planning_on_another_data_source_is_refused(shop, monkeypatch, capsys):
    cube = _server(monkeypatch, shop)
    plan = _plan(cube, dataSource="reporting")
    cube.render = lambda query: plan

    envelope = _run(capsys, shop, *_argv("by_status"), "--api")

    assert envelope["status"] == "error"
    assert "data source 'reporting'" in envelope["errors"][0]
    assert not [c for c in cube.calls if c.url.path.endswith("/v1/load")]


def test_the_data_source_is_checked_even_when_the_tables_cannot_be(
    shop, monkeypatch, capsys
):
    """An empty plan leaves the tables unchecked, never the data source."""

    cube = _server(monkeypatch, shop)
    cube.render = lambda query: {"sql": {"sql": [], "dataSource": "reporting"}}

    envelope = _run(capsys, shop, *_argv("by_status"), "--api")

    assert envelope["status"] == "error"
    assert "data source 'reporting'" in envelope["errors"][0]
    assert not [c for c in cube.calls if c.url.path.endswith("/v1/load")]


def test_an_empty_plan_is_disclosed_not_a_crash(shop, monkeypatch, capsys):
    cube = _server(monkeypatch, shop)
    cube.render = lambda query: {"sql": {"sql": [], "dataSource": "default"}}

    envelope = _run(capsys, shop, *_argv("by_status"), "--api")

    assert envelope["status"] == "ok", envelope["errors"]
    assert any("its plan carried no SQL" in n for n in envelope["data"]["notes"])


def test_a_plan_naming_no_data_source_is_refused(shop, monkeypatch, capsys):
    cube = _server(monkeypatch, shop)
    cube.render = lambda query: {"sql": {"sql": []}}

    envelope = _run(capsys, shop, *_argv("by_status"), "--api")

    assert envelope["status"] == "error"
    assert "names no data source" in envelope["errors"][0]
    assert not [c for c in cube.calls if c.url.path.endswith("/v1/load")]


def test_an_unchecked_plan_is_said_on_that_result_only(shop, monkeypatch, capsys):
    """One layer, two requests: a note the first earned is not on the second."""

    from exmergo_dex_core.explore.semantic import SemanticQuery

    cube = _server(monkeypatch, shop)
    plan = _plan(cube, preAggregations=[{"preAggregationId": "orders.rollup"}])
    cube.render = lambda query: plan
    layer = resolve_semantic_layer(DexEngine.from_repo(shop), api=True)
    spec = QUERIES["by_status"]
    query = SemanticQuery(
        metrics=spec["metrics"],
        group_by=spec.get("group_by", []),
        order_by=spec.get("order_by", []),
    )
    first = layer.query(query)
    cube.render = None
    second = layer.query(query)

    assert any("pre-aggregation" in n for n in first.notes)
    assert not any("pre-aggregation" in n for n in second.notes)


def test_continue_wait_is_polled_until_the_rows_arrive(shop, monkeypatch, capsys):
    cube = _server(monkeypatch, shop)
    cube.waits = 3
    envelope = _run(capsys, shop, *_argv("by_status"), "--api")

    assert envelope["status"] == "ok", envelope["errors"]
    assert len([c for c in cube.calls if c.url.path.endswith("/v1/load")]) == 4


def test_a_core_serving_another_model_is_refused_before_it_runs_anything(
    shop, monkeypatch, capsys
):
    cube = _server(monkeypatch, shop)
    edited = (
        (shop / "model" / "cubes" / "orders.yml")
        .read_text()
        .replace("type: sum", "type: max", 1)
    )
    other = shop / "other"
    shutil.copytree(shop / "model", other)
    (other / "cubes" / "orders.yml").write_text(edited)
    names = sorted(str(path.relative_to(other)) for path in other.rglob("*.yml"))
    cube.meta = meta_for(load_files(other, names))

    envelope = _run(capsys, shop, *_argv("by_status"), "--api")

    assert envelope["status"] == "error"
    assert "serves a different model" in envelope["errors"][0]
    assert not [c for c in cube.calls if c.url.path.endswith("/v1/load")]


def test_a_pii_member_is_refused_before_anything_is_sent(shop, monkeypatch, capsys):
    cube = _server(monkeypatch, shop)
    envelope = _run(
        capsys,
        shop,
        "explore",
        "semantic",
        "query",
        "customers.count",
        "--group-by",
        "customers.email",
        "--api",
    )

    assert envelope["status"] == "error"
    assert "PII" in envelope["errors"][0]
    assert not [c for c in cube.calls if c.url.path.endswith("/v1/load")]


def test_a_member_the_model_marks_pii_is_refused_on_that_word(
    shop, monkeypatch, capsys
):
    cube = _server(monkeypatch, shop)
    for element in cube.meta["cubes"]:
        for entry in element["dimensions"]:
            if entry["name"] == "orders.status":
                entry["meta"] = {"pii": True}
    envelope = _run(capsys, shop, *_argv("by_status"), "--api")

    assert envelope["status"] == "error"
    assert "orders.status" in envelope["errors"][0]
    assert not [c for c in cube.calls if c.url.path.endswith("/v1/load")]


def test_a_warehouse_error_relayed_by_load_is_masked(shop, monkeypatch, capsys):
    cube = _server(monkeypatch, shop)
    cube.load_error = (
        400,
        "Conversion Error: Could not convert string 'alice@example.com' to INT32",
    )
    envelope = _run(capsys, shop, *_argv("by_status"), "--api")

    assert envelope["status"] == "error"
    assert "alice@example.com" not in json.dumps(envelope)
    assert "could not resolve the metric query" in envelope["errors"][0]


def test_cube_api_token_is_refused_on_a_core(shop, monkeypatch, capsys):
    _server(monkeypatch, shop)
    monkeypatch.setenv("CUBE_API_TOKEN", CLOUD_TOKEN)
    envelope = _run(capsys, shop, *_argv("by_status"), "--api")

    assert envelope["status"] == "error"
    assert (
        "CUBE_API_TOKEN is set and semantic.deployment is local"
        in (envelope["errors"][0])
    )
    assert CLOUD_TOKEN not in json.dumps(envelope)


# --- the transports -------------------------------------------------------------------


def test_a_render_transport_cannot_reach_load_nor_a_load_transport_sql(monkeypatch):
    monkeypatch.setenv("CUBEJS_API_SECRET", "s")
    render = CubeTransport(None)
    loader = CubeTransport(None, execution="vendor")

    assert render.paths == RENDER_PATHS
    assert loader.paths == LOAD_PATHS
    with pytest.raises(CubeTransportError, match="/v1/load"):
        render.load({"measures": ["orders.count"]})
    with pytest.raises(CubeTransportError, match="/v1/sql"):
        loader.sql({"measures": ["orders.count"]})


def test_a_load_token_claims_meta_and_data(monkeypatch):
    import base64

    from exmergo_dex_core.cube.transport import LOAD_SCOPES, mint_token

    token = mint_token("s", now=1_000, scopes=LOAD_SCOPES)
    payload = token.split(".")[1]
    claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    assert claims["dex_scopes"] == ["meta", "data"]


@pytest.mark.parametrize(
    ("url", "named"),
    [
        ("https://evil.example.com/cubejs-api", "not a Cube Cloud host"),
        ("https://cubecloudapp.dev.evil.example/cubejs-api", "not a Cube Cloud host"),
        ("http://shop.aws-us-east-1.cubecloudapp.dev/cubejs-api", "over http"),
    ],
)
def test_a_cloud_host_outside_the_allowlist_is_refused_before_the_token(
    monkeypatch, url, named
):
    read = []
    with pytest.raises(CubeTransportError, match=named):
        CubeTransport(url, deployment="cube_cloud", token=lambda: read.append(1) or "t")
    assert read == []


@pytest.mark.parametrize(
    "url", ["http://localhost:4000/cubejs-api", "https://127.0.0.1/cubejs-api"]
)
def test_a_loopback_host_never_receives_a_cloud_token(monkeypatch, url):
    read = []
    with pytest.raises(CubeTransportError, match="not a Cube Cloud host"):
        CubeTransport(url, deployment="cube_cloud", token=lambda: read.append(1) or "t")
    assert read == []


def test_a_cloud_host_is_admitted_only_under_cube_cloud(monkeypatch):
    monkeypatch.setenv("CUBEJS_API_SECRET", "s")
    with pytest.raises(CubeTransportError, match="only a Cube server on this machine"):
        CubeTransport(CLOUD_URL)
    assert CubeTransport(CLOUD_URL, deployment="cube_cloud").base_url == CLOUD_URL


def test_a_supplied_token_reads_nothing_ambient(monkeypatch):
    monkeypatch.setenv("DEX_CUBE_URL", "https://other.aws-eu-1.cubecloudapp.dev/x")
    monkeypatch.setenv("DEX_CUBE_ALLOWED_HOSTS", "cube.example.com")
    transport = CubeTransport(CLOUD_URL, deployment="cube_cloud", token=lambda: "t")

    assert transport.base_url == CLOUD_URL
    with pytest.raises(CubeTransportError, match="not a Cube Cloud host"):
        CubeTransport(
            "https://cube.example.com/cubejs-api",
            deployment="cube_cloud",
            token=lambda: "t",
        )


def test_a_supplied_token_is_refused_on_a_core():
    with pytest.raises(CubeTransportError, match=r"semantic\.deployment is local"):
        CubeTransport(None, token=lambda: "t")


def test_cube_cloud_needs_a_url(monkeypatch):
    monkeypatch.delenv("DEX_CUBE_URL", raising=False)
    with pytest.raises(CubeTransportError, match=r"semantic\.cube\.url"):
        CubeTransport(None, deployment="cube_cloud")


def test_decode_load_reads_numbers_by_the_annotation():
    body = {
        "data": [
            {"orders.status": "paid", "orders.revenue": "21.75", "orders.count": "4"},
            {"orders.status": "12", "orders.revenue": "1e3", "orders.count": None},
        ],
        "annotation": {
            "measures": {
                "orders.revenue": {"type": "number"},
                "orders.count": {"type": "number"},
            },
            "dimensions": {"orders.status": {"type": "string"}},
        },
    }
    names = {
        "orders.status": "orders.status",
        "orders.revenue": "orders.revenue",
        "orders.count": "orders.count",
    }

    columns, types, cells = decode_load(body, names, list(names))

    assert columns == ["orders.status", "orders.revenue", "orders.count"]
    assert types == ["string", "number", "number"]
    assert cells == [["paid", 21.75, 4], ["12", 1000.0, None]]


# --- a time bucket reads one way whoever executes it (#1497) --------------------------


def _bucketed(cube: FakeCube, grain: str) -> list[str]:
    """``by_month`` recorded again at ``grain``, and the argv that asks for it."""

    plan = json.dumps(cube.recordings["by_month"]).replace("month", grain)
    cube.recordings[f"by_{grain}"] = json.loads(plan)
    token = f"orders.ordered_at__{grain}"
    return [
        "explore",
        "semantic",
        "query",
        "orders.revenue",
        "--group-by",
        token,
        f"--order-by={token}",
    ]


@pytest.mark.parametrize("grain", ["day", "month", "hour", "minute"])
def test_a_time_bucket_reads_the_same_under_api_and_local(
    shop, monkeypatch, capsys, grain
):
    argv = _bucketed(_server(monkeypatch, shop), grain)
    local = _run(capsys, shop, *argv, "--local")
    api = _run(capsys, shop, *argv, "--api")

    assert local["status"] == api["status"] == "ok", (local["errors"], api["errors"])
    assert local["data"]["types"][0] == api["data"]["types"][0] == "timestamp"
    buckets = [row[0] for row in local["data"]["cells"]]
    assert buckets == [row[0] for row in api["data"]["cells"]]
    assert buckets and all(b.endswith("+00:00") for b in buckets)


def test_a_values_bucket_reads_the_same_under_api_and_local(shop, monkeypatch, capsys):
    _server(monkeypatch, shop)
    dimension, _ = VALUES["values_month"]
    argv = ["explore", "semantic", "values", dimension]
    local = _run(capsys, shop, *argv, "--local")
    api = _run(capsys, shop, *argv, "--api")

    assert local["status"] == api["status"] == "ok", (local["errors"], api["errors"])
    assert local["data"]["types"][0] == api["data"]["types"][0] == "timestamp"
    buckets = [row[0] for row in local["data"]["cells"]]
    assert buckets == [row[0] for row in api["data"]["cells"]]
    assert buckets and all(b.endswith("+00:00") for b in buckets)


@pytest.mark.parametrize(
    ("kind", "spell"),
    [
        # BigQuery: a TIMESTAMP comes back offset.
        ("TIMESTAMP", lambda v: v + "+00:00"),
        # Snowflake: Cube's bucket is a TIMESTAMP_NTZ, naive.
        ("TIMESTAMP_NTZ", lambda v: v),
        # Postgres and Redshift: naive, under the driver's own type name.
        ("timestamp without time zone", lambda v: v),
    ],
)
def test_a_billed_connector_s_bucket_type_is_reported_as_api_reports_it(
    shop, monkeypatch, capsys, kind, spell
):
    from exmergo_dex_core.adapters.base import QueryResult
    from exmergo_dex_core.adapters.duckdb import DuckDBAdapter

    run_query = DuckDBAdapter.run_query

    def as_billed(self, sql, **kwargs):
        result = run_query(self, sql, **kwargs)
        return QueryResult(
            columns=result.columns,
            types=[kind, *result.types[1:]],
            cells=[[spell(row[0]), *row[1:]] for row in result.cells],
            truncated=result.truncated,
        )

    _server(monkeypatch, shop)
    api = _run(capsys, shop, *_argv("by_month"), "--api")
    monkeypatch.setattr(DuckDBAdapter, "run_query", as_billed)
    local = _run(capsys, shop, *_argv("by_month"), "--local")

    assert local["data"]["types"][0] == api["data"]["types"][0] == "timestamp"
    assert [r[0] for r in local["data"]["cells"]] == [
        r[0] for r in api["data"]["cells"]
    ]


def test_utc_timestamps_spells_one_instant_one_way():
    from exmergo_dex_core.explore.semantic.policy import utc_timestamps

    cells = [
        ["2025-03-01T00:00:00.000", "a"],
        ["2025-03-01T01:00:00+01:00", "b"],
        ["2025-03-01", "c"],
        [None, "d"],
        ["not a time", "e"],
    ]
    types, spelled = utc_timestamps(["t", "s"], ["time", "string"], cells, ["t"])

    assert types == ["timestamp", "string"]
    assert spelled == [
        ["2025-03-01T00:00:00+00:00", "a"],
        ["2025-03-01T00:00:00+00:00", "b"],
        ["2025-03-01T00:00:00+00:00", "c"],
        [None, "d"],
        ["not a time", "e"],
    ]


# --- Cube Cloud ----------------------------------------------------------------------


def test_cube_cloud_executes_by_default_and_says_where_its_catalog_came_from(
    cloud, monkeypatch, capsys
):
    cube = _server(monkeypatch, cloud, cloud=True)
    envelope = _run(capsys, cloud, *_argv("by_status"))

    assert envelope["status"] == "ok", envelope["errors"]
    data = envelope["data"]
    assert (data["backend"], data["deployment"], data["execution"]) == (
        "cube_cloud",
        "cube_cloud",
        "vendor",
    )
    assert data["definition_source"] == "cube_cloud"
    assert CUBE_VENDOR_COST_WARNING in envelope["warnings"]
    assert CUBE_CLOUD_DISCLOSURE in envelope["warnings"]
    assert {str(c.url).split("/v1")[0] for c in cube.calls} == {CLOUD_URL}


def test_cube_cloud_list_reads_the_compiled_meta(cloud, monkeypatch, capsys):
    cube = _server(monkeypatch, cloud, cloud=True)
    envelope = _run(capsys, cloud, "explore", "semantic", "list")

    assert envelope["status"] == "ok", envelope["errors"]
    data = envelope["data"]
    assert data["unavailable"] == CUBE_HOSTED_CATALOG_GAPS
    assert "compiled_only" not in data
    assert {m["name"] for m in data["semantic_models"]} >= {"orders", "order_facts"}
    assert not any(m.get("relation") for m in data["semantic_models"])
    assert [c.url.path for c in cube.calls] == ["/cubejs-api/v1/meta"]


def test_cube_cloud_answers_as_the_core_does(cloud, monkeypatch, capsys):
    _server(monkeypatch, cloud, cloud=True)
    api = _run(capsys, cloud, *_argv("by_month"))
    local = _run(capsys, cloud, *_argv("by_month"), "--local")

    assert (local["data"]["execution"], local["data"]["deployment"]) == (
        "dex",
        "cube_cloud",
    )
    assert CUBE_VENDOR_COST_WARNING not in local["warnings"]
    assert sorted(_rows(api)) == sorted(_rows(local))


def test_cube_cloud_without_a_token_names_the_variable(cloud, monkeypatch, capsys):
    _server(monkeypatch, cloud, cloud=True)
    monkeypatch.delenv("CUBE_API_TOKEN")
    envelope = _run(capsys, cloud, *_argv("by_status"))

    assert envelope["status"] == "error"
    assert "CUBE_API_TOKEN, which is not set" in envelope["errors"][0]


def test_a_rejected_cloud_token_says_which_token_to_check(cloud, monkeypatch, capsys):
    cube = _server(monkeypatch, cloud, cloud=True)
    cube.cloud_token = "another"  # noqa: S105
    envelope = _run(capsys, cloud, *_argv("by_status"))

    assert envelope["status"] == "error"
    assert "check that CUBE_API_TOKEN is a current API token" in envelope["errors"][0]


def test_the_cloud_token_never_reaches_an_envelope(cloud, monkeypatch, capsys):
    cube = _server(monkeypatch, cloud, cloud=True)
    outputs = [
        _run(capsys, cloud, "explore", "semantic", "list"),
        _run(capsys, cloud, *_argv("by_status")),
        _run(capsys, cloud, *_argv("by_status"), "--local"),
        _run(capsys, cloud, "explore", "semantic", "values", "orders.status"),
    ]
    cube.load_error = (500, f"boom {CLOUD_TOKEN}")
    outputs.append(_run(capsys, cloud, *_argv("by_status")))

    assert CLOUD_TOKEN not in json.dumps(outputs)


def test_a_host_supplied_token_reaches_cube_cloud(cloud, monkeypatch):
    cube = _server(monkeypatch, cloud, cloud=True)
    monkeypatch.delenv("CUBE_API_TOKEN")
    engine = DexEngine.from_repo(
        cloud, semantic_source=SemanticSource(token=lambda: CLOUD_TOKEN)
    )

    layer = resolve_semantic_layer(engine)
    catalog = layer.list_definitions()

    assert isinstance(layer, CubeVendorLayer)
    assert catalog.semantic_models
    assert cube.calls


def test_a_host_supplied_token_is_refused_on_a_core(shop):
    engine = DexEngine.from_repo(
        shop, semantic_source=SemanticSource(token=lambda: CLOUD_TOKEN)
    )

    with pytest.raises(Exception, match="cube_cloud"):
        resolve_semantic_layer(engine)


def test_every_deployment_and_execution_resolves_its_own_layer(tmp_path, monkeypatch):
    monkeypatch.setenv("CUBE_API_TOKEN", CLOUD_TOKEN)
    roots = {}
    for name, deployment, url in (
        ("core", None, None),
        ("hosted", "cube_cloud", CLOUD_URL),
    ):
        roots[name] = tmp_path / name
        shutil.copytree(MODEL, roots[name] / "model")
        _config(roots[name], deployment=deployment, url=url)
    core = DexEngine.from_repo(roots["core"])
    hosted = DexEngine.from_repo(roots["hosted"])
    found = {
        (name, flag): resolve_semantic_layer(engine, **flags)
        for name, engine in (("local", core), ("cube_cloud", hosted))
        for flag, flags in (
            ("default", {}),
            ("--local", {"local": True}),
            ("--api", {"api": True}),
        )
    }

    def axes(layer):
        return type(layer), layer.descriptor.deployment, layer.descriptor.execution

    assert axes(found["local", "default"]) == (LocalCubeLayer, "local", "dex")
    assert axes(found["local", "--local"]) == (LocalCubeLayer, "local", "dex")
    assert axes(found["local", "--api"]) == (CubeVendorLayer, "local", "vendor")
    assert axes(found["cube_cloud", "default"]) == (
        CubeVendorLayer,
        "cube_cloud",
        "vendor",
    )
    assert axes(found["cube_cloud", "--local"]) == (
        LocalCubeLayer,
        "cube_cloud",
        "dex",
    )
    assert axes(found["cube_cloud", "--api"]) == (
        CubeVendorLayer,
        "cube_cloud",
        "vendor",
    )


# --- the contracts --------------------------------------------------------------------


class _Served:
    """A Cube server for a contract's own layer, serving its compiled meta."""

    root: Path

    @pytest.fixture(autouse=True)
    def _serve(self, tmp_path: Path, monkeypatch) -> None:
        self.root = tmp_path
        self.monkeypatch = monkeypatch
        monkeypatch.setattr(
            "exmergo_dex_core.cube.transport.time.sleep", lambda _: None
        )

    def _layer(self, source_obj, deployment: str):
        cube = FakeCube("duckdb", meta=meta_for(source_obj.loaded()))
        cube.load = lambda query: {"data": [], "annotation": {}}
        cube.install(self.monkeypatch)
        if deployment == "cube_cloud":
            cube.cloud_token = CLOUD_TOKEN
            source_obj.url = CLOUD_URL
            return CubeVendorLayer(
                source_obj, deployment=deployment, token=lambda: CLOUD_TOKEN
            )
        return CubeVendorLayer(source_obj, deployment=deployment)


class TestCubeVendorBackend(_Served, SemanticBackendContract):
    """``--api`` on a Cube Core, held to the backend contract."""

    def make_backend(self):
        from .test_reader import ORDERS_LAYER, source

        return self._layer(source(self.root, *ORDERS_LAYER), "local")


class TestCubeCloudBackend(_Served, SemanticBackendContract):
    """Cube Cloud's hosted catalog, held to the backend contract."""

    def make_backend(self):
        from .test_reader import ORDERS_LAYER, source

        return self._layer(source(self.root, *ORDERS_LAYER), "cube_cloud")


class TestCubeCloudReferenceLayer(_Served, SemanticCatalogContract):
    """The catalog contract against the reference layer, as Cube Cloud compiles it."""

    def make_reference_backend(self):
        from .test_reader import reference_cube_models, repo

        engine = DexEngine.from_repo(repo(self.root, *reference_cube_models()))
        return self._layer(engine.semantic_catalog_source(), "cube_cloud")

    def reported_name(self, kind: str, name: str) -> str:
        from .test_reader import REFERENCE_METRIC_TOKENS

        return REFERENCE_METRIC_TOKENS[name] if kind == "metrics" else name
