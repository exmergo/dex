"""``dex maintain incidents`` (#1472): the incidents the sources hold, in one
bounded, priced read.

Each project here is a directory holding what a dbt build leaves behind: a
``dbt_project.yml``, a manifest, and where a test runs, ``run_results.json`` and
the dbt log naming its invocation. Where the Elementary package is installed,
its own ``dbt_project.yml`` sits under ``dbt_packages/``, the manifest declares
its two tables, and a DuckDB file holds them, built column for column from the
recorded 0.26.1 layout as ``test_elementary_incidents.py`` builds them.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from exmergo_dex_core import DexEngine
from exmergo_dex_core.cli import dispatch
from exmergo_dex_core.config import DexConfig, PostgresTarget
from exmergo_dex_core.envelope import Paradigm
from exmergo_dex_core.guards.cost_guard import ConfirmationRequiredError, CostGate
from exmergo_dex_core.maintain import incident_sources
from exmergo_dex_core.maintain.elementary_layouts import layout_for
from exmergo_dex_core.maintain.incident_sources import (
    _allowlist_refusal,
    _Located,
)
from exmergo_dex_core.maintain.results import IncidentsResult
from exmergo_dex_core.results import to_envelope

duckdb = pytest.importorskip("duckdb")

RELEASE = "0.26.1"
SCHEMA = "main_elementary"
START = datetime(2026, 11, 1, 0, 0, 0)

TEST = "test.shop.not_null_orders_id.abc123"
OTHER = "test.shop.not_null_customers_id.def456"

_DUCKDB_TYPES = {
    "boolean": "BOOLEAN",
    "timestamp": "TIMESTAMP",
    "int": "INTEGER",
    "bigint": "BIGINT",
    "float": "DOUBLE",
    "long_string": "VARCHAR",
    "string": "VARCHAR",
}


@dataclass(frozen=True)
class Run:
    id: str
    started: datetime
    version: str = RELEASE


@dataclass(frozen=True)
class Result:
    id: str
    run: str
    test: str = TEST
    status: str = "pass"


def _hourly(count: int, start: datetime = START) -> list[Run]:
    return [Run(f"inv-{n}", start + timedelta(hours=n)) for n in range(1, count + 1)]


def _recent(count: int) -> list[Run]:
    """Hourly runs ending within the last hour, so an open incident is not
    stale at call time."""

    now = datetime.now(UTC).replace(tzinfo=None, minute=0, second=0, microsecond=0)
    return _hourly(count, now - timedelta(hours=count))


# --- the project ------------------------------------------------------------------


def _model(name: str, *, package: str = "shop", schema: str = "main") -> dict:
    return {
        "unique_id": f"model.{package}.{name}",
        "resource_type": "model",
        "package_name": package,
        "name": name,
        "database": "dev",
        "schema": schema,
        "config": {"enabled": True},
    }


def _test(uid: str, model: str) -> dict:
    return {
        "unique_id": uid,
        "resource_type": "test",
        "package_name": "shop",
        "config": {"enabled": True, "severity": "ERROR"},
        "depends_on": {"nodes": [f"model.shop.{model}"]},
        "attached_node": f"model.shop.{model}",
    }


def _manifest(*, elementary: bool) -> dict[str, Any]:
    nodes = {
        "model.shop.orders": _model("orders"),
        "model.shop.customers": _model("customers"),
        TEST: _test(TEST, "orders"),
        OTHER: _test(OTHER, "customers"),
    }
    if elementary:
        for table in ("dbt_invocations", "elementary_test_results"):
            node = _model(table, package="elementary", schema=SCHEMA)
            nodes[node["unique_id"]] = node
        for hook in ("on-run-start", "on-run-end"):
            uid = f"operation.elementary.elementary-{hook}-0"
            nodes[uid] = {
                "unique_id": uid,
                "resource_type": "operation",
                "package_name": "elementary",
            }
    return {
        "metadata": {"project_name": "shop", "adapter_type": "duckdb"},
        "nodes": nodes,
        "macros": {},
        "disabled": {},
    }


def _project(root: Path, *, elementary: bool = False) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "dbt_project.yml").write_text("name: shop\nversion: '1.0'\n")
    target = root / "target"
    target.mkdir(exist_ok=True)
    (target / "manifest.json").write_text(json.dumps(_manifest(elementary=elementary)))
    if elementary:
        package = root / "dbt_packages" / "elementary"
        package.mkdir(parents=True, exist_ok=True)
        (package / "dbt_project.yml").write_text(
            f"name: elementary\nversion: '{RELEASE}'\n"
        )
    return root


def _run_results(
    root: Path,
    statuses: dict[str, str],
    *,
    invocation: str = "dbt-inv-1",
    at: datetime = START + timedelta(days=30),
    started: datetime | None = None,
) -> None:
    """``at`` is dbt's ``generated_at``, the run's end; ``started`` its
    ``invocation_started_at``, a minute before by default."""

    target = root / "target"
    begun = at - timedelta(minutes=1) if started is None else started
    document = {
        "metadata": {
            "invocation_id": invocation,
            "generated_at": at.strftime("%Y-%m-%dT%H:%M:%S.000000Z"),
            "invocation_started_at": begun.strftime("%Y-%m-%dT%H:%M:%S.000000Z"),
        },
        "results": [
            {"unique_id": uid, "status": status, "message": "a failing value"}
            for uid, status in statuses.items()
        ],
        "args": {"which": "build"},
    }
    # No dbt log beside it, so the adapter reads the run as found, with a note:
    # the ids here name Elementary's runs too, and are not UUID-shaped as the
    # log reader's are.
    (target / "run_results.json").write_text(json.dumps(document))


# --- Elementary's tables ------------------------------------------------------------


def _create(con, table: str) -> None:
    columns = ", ".join(
        f'"{name}" {_DUCKDB_TYPES[kind]}'
        for name, kind in layout_for(RELEASE).tables[table].columns
    )
    con.execute(f"CREATE TABLE {SCHEMA}.{table} ({columns})")


def _warehouse(path: Path, runs: Sequence[Run], results: Sequence[Result]) -> Path:
    con = duckdb.connect(str(path))
    con.execute(f"CREATE SCHEMA {SCHEMA}")
    _create(con, "dbt_invocations")
    _create(con, "elementary_test_results")
    for run in runs:
        con.execute(
            "INSERT INTO main_elementary.dbt_invocations "
            "(invocation_id, run_started_at, generated_at, created_at, command, "
            "elementary_version, git_sha, target_name) "
            "VALUES (?, ?, ?, ?, 'build', ?, ?, 'dev')",
            [
                run.id,
                run.started.strftime("%Y-%m-%d %H:%M:%S"),
                run.started.strftime("%Y-%m-%d %H:%M:%S"),
                run.started + timedelta(minutes=5),
                run.version,
                "a" * 40,
            ],
        )
    starts = {run.id: run.started for run in runs}
    for result in results:
        at = starts[result.run] + timedelta(minutes=4)
        con.execute(
            "INSERT INTO main_elementary.elementary_test_results "
            "(id, invocation_id, test_unique_id, test_execution_id, test_type, "
            "status, severity, detected_at, created_at, failures, result_rows, "
            "test_results_description) "
            "VALUES (?, ?, ?, ?, 'dbt_test', ?, 'ERROR', ?, ?, ?, ?, ?)",
            [
                result.id,
                result.run,
                result.test,
                f"{result.test}.{result.run}",
                result.status,
                at,
                at + timedelta(minutes=1),
                0 if result.status == "pass" else 1,
                '[{"id": null}]',
                "a value the test failed on",
            ],
        )
    con.close()
    return path


def _results(runs: Sequence[Run], *, failing: Sequence[str] = ()) -> list[Result]:
    bad = set(failing)
    return [
        Result(f"r-{run.id}", run.id, status="fail" if run.id in bad else "pass")
        for run in runs
    ]


def _elementary_project(
    tmp_path: Path, runs: Sequence[Run], results: Sequence[Result]
) -> tuple[Path, Path]:
    root = _project(tmp_path / "project", elementary=True)
    warehouse = _warehouse(tmp_path / "dev.duckdb", runs, results)
    return root, warehouse


def _engine(root: Path, warehouse: Path | None = None, **kwargs) -> DexEngine:
    if warehouse is None:
        warehouse = root.parent / "empty.duckdb"
        if not warehouse.exists():
            duckdb.connect(str(warehouse)).close()
    return DexEngine(
        connector="duckdb", path=str(warehouse), repo_root=str(root), **kwargs
    )


def _data(result: IncidentsResult) -> dict[str, Any]:
    return to_envelope(result).data


# --- acceptance: dbt's run results, no Elementary ---------------------------------


def test_one_failing_test_and_no_elementary_is_one_dbt_incident_of_unknown_start(
    tmp_path: Path,
):
    root = _project(tmp_path / "project")
    _run_results(root, {TEST: "fail", OTHER: "pass"})
    data = _data(_engine(root).incidents())

    [incident] = data["incidents"]
    assert incident["opening_source"] == "dbt_run_results"
    assert incident["subject"]["key"] == TEST
    assert incident["first_observed"] == "unknown"
    assert incident["state"] == "open"
    assert data["sources"]["dbt_run_results"]["status"] == "read"
    assert data["window"]["count"] == 1
    assert data["window"]["invocations"] is None
    assert data["window"]["first"]["invocation_id"] == "dbt-inv-1"


def test_the_same_project_with_the_test_passing_returns_no_incident(tmp_path: Path):
    root = _project(tmp_path / "project")
    _run_results(root, {TEST: "pass", OTHER: "pass"})
    data = _data(_engine(root).incidents())

    assert data["incidents"] == []
    assert data["sources"]["dbt_run_results"]["status"] == "read"


def test_no_message_from_a_result_reaches_the_envelope(tmp_path: Path):
    root = _project(tmp_path / "project")
    _run_results(root, {TEST: "fail"})
    assert "a failing value" not in json.dumps(_data(_engine(root).incidents()))


# --- acceptance: Elementary's tables ------------------------------------------------


def test_one_failing_test_with_elementary_is_one_elementary_incident(tmp_path: Path):
    runs = _recent(3)
    root, warehouse = _elementary_project(
        tmp_path, runs, _results(runs, failing=["inv-3"])
    )
    data = _data(_engine(root, warehouse).incidents())

    [incident] = data["incidents"]
    assert incident["opening_source"] == "elementary"
    assert incident["subject"]["key"] == TEST
    assert incident["state"] == "open"
    # A pass precedes the failure in the window, so its start is known.
    assert incident["first_observed"]["run"] == "inv-3"
    assert data["sources"]["elementary"]["status"] == "read"
    assert data["sources"]["elementary"]["release"] == RELEASE


def test_the_same_project_with_elementary_and_the_test_passing_returns_none(
    tmp_path: Path,
):
    runs = _hourly(3)
    root, warehouse = _elementary_project(tmp_path, runs, _results(runs))
    data = _data(_engine(root, warehouse).incidents())

    assert data["incidents"] == []
    assert data["sources"]["elementary"]["status"] == "read"


# --- acceptance: whether the package is there ---------------------------------------


def test_with_no_package_elementary_is_missing_and_the_next_step_names_the_install(
    tmp_path: Path,
):
    root = _project(tmp_path / "project")
    _run_results(root, {TEST: "pass"})
    data = _data(_engine(root).incidents())

    elementary = data["sources"]["elementary"]
    assert (elementary["status"], elementary["reason_code"]) == (
        "missing",
        "not_installed",
    )
    [step] = [s for s in data["next_steps"] if s["command"] == "dex init"]
    assert "Elementary package install" in step["purpose"]


def test_with_the_package_elementary_is_read_and_no_install_is_offered(
    tmp_path: Path,
):
    runs = _hourly(2)
    root, warehouse = _elementary_project(tmp_path, runs, _results(runs))
    data = _data(_engine(root, warehouse).incidents())

    assert data["sources"]["elementary"]["status"] == "read"
    assert "reason_code" not in data["sources"]["elementary"]
    assert not [s for s in data["next_steps"] if s["command"] == "dex init"]


def test_a_declared_package_not_installed_points_to_transform_deps(tmp_path: Path):
    root = _project(tmp_path / "project")
    (root / "packages.yml").write_text(
        "packages:\n  - package: elementary-data/elementary\n    version: 0.26.1\n"
    )
    data = _data(_engine(root).incidents())

    assert data["sources"]["elementary"]["reason_code"] == "not_installed"
    assert data["next_steps"][0]["command"] == "dex transform deps"


# --- acceptance: the source allowlist -----------------------------------------------


def _postgres_engine(root: Path, schemas: list[str]) -> DexEngine:
    config = DexConfig(connector="postgres", postgres=PostgresTarget(schemas=schemas))
    return DexEngine(config=config, repo_root=str(root))


def test_a_schema_outside_the_allowlist_is_refused_and_never_queried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    root = _project(tmp_path / "project", elementary=True)
    engine = _postgres_engine(root, ["analytics"])

    def no_connection(*_args, **_kwargs):
        raise AssertionError("the command opened a connection")

    monkeypatch.setattr(engine, "_adapter", no_connection)
    data = _data(engine.incidents())

    elementary = data["sources"]["elementary"]
    assert (elementary["status"], elementary["reason_code"]) == (
        "refused",
        "outside_allowlist",
    )
    command = "dex config set postgres.schemas '[analytics, main_elementary]'"
    assert elementary["config_set"] == command
    assert command in [s["command"] for s in data["next_steps"]]
    assert engine.store._queries == []


@pytest.mark.parametrize(
    ("connector", "allowlist"),
    [
        ("postgres", ["main_elementary"]),
        ("bigquery", ["main_elementary"]),
        ("bigquery", ["dev.main_elementary"]),
        ("snowflake", ["dev"]),
        ("databricks", ["dev"]),
    ],
)
def test_a_schema_inside_the_allowlist_is_not_refused(connector, allowlist):
    field = {
        "postgres": "schemas",
        "bigquery": "datasets",
        "snowflake": "databases",
        "databricks": "catalogs",
    }[connector]

    target = argparse.Namespace(**{field: allowlist})
    engine = argparse.Namespace(
        connector=connector,
        scopes=None,
        config=argparse.Namespace(connector=connector, **{connector: target}),
    )
    steps: list[dict[str, str]] = []
    refusal = _allowlist_refusal(engine, _Located("dev", SCHEMA), steps)
    assert refusal is None
    assert steps == []


def test_a_schema_inside_the_allowlist_is_read(tmp_path: Path):
    """DuckDB reads its whole file, so the package's schema is always inside."""

    runs = _hourly(2)
    root, warehouse = _elementary_project(tmp_path, runs, _results(runs))
    engine = _engine(root, warehouse)
    data = _data(engine.incidents())

    assert data["sources"]["elementary"]["status"] == "read"
    allowed = [e for e in engine.store._queries if e["decision"] == "allowed"]
    assert allowed and all(SCHEMA in e["sql"] for e in allowed)


# --- acceptance: a billed connector -------------------------------------------------


def _billed(engine: DexEngine, monkeypatch: pytest.MonkeyPatch, *, confirmed: bool):
    """The engine's DuckDB adapter, gated as a billed connector's is."""

    opened = engine._adapter

    def gated(*args, **kwargs):
        adapter = opened(*args, **kwargs)
        adapter.cost_gate = CostGate(
            paradigm=Paradigm.BYTES_SCANNED,
            ceiling=10_000_000.0 if confirmed else None,
            session_ceiling=100_000_000.0,
            session_spent=0.0,
            confirmed=confirmed,
            connector="bigquery",
            command="maintain incidents",
        )
        return adapter

    monkeypatch.setattr(engine, "_adapter", gated)


def test_a_billed_connector_without_confirm_asks_and_reads_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    runs = _hourly(2)
    root, warehouse = _elementary_project(tmp_path, runs, _results(runs))
    engine = _engine(root, warehouse)
    _billed(engine, monkeypatch, confirmed=False)

    with pytest.raises(ConfirmationRequiredError) as asked:
        engine.incidents()
    assert [e for e in engine.store._queries if e["decision"] == "allowed"] == []
    # One ask for the whole read, all five statements on it, rather than the
    # first phase's three counts.
    assert asked.value.request.data["command"] == "maintain incidents"
    assert list(asked.value.request.data["per_table_bytes"]) == [
        "count invocations",
        "count result rows",
        "count result rows with no invocation",
        "read invocations",
        "read result rows",
    ]


def test_values_free_mode_refuses_the_command(tmp_path: Path):
    from exmergo_dex_core.values_free import ValuesFreeError

    root = _project(tmp_path / "project")
    _run_results(root, {TEST: "fail"})
    with pytest.raises(ValuesFreeError):
        _engine(root, result_mode="values_free").incidents()


def test_the_cli_on_a_billed_connector_without_confirm_answers_needs_confirmation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    runs = _hourly(2)
    root, warehouse = _elementary_project(tmp_path, runs, _results(runs))
    engine = _engine(root, warehouse)
    _billed(engine, monkeypatch, confirmed=False)
    args = argparse.Namespace(
        group="maintain", subcommand="incidents", objects=[], invocations=None
    )

    envelope = dispatch(args, engine)
    assert envelope.status == "needs_confirmation"
    assert [e for e in engine.store._queries if e["decision"] == "allowed"] == []


def test_a_billed_connector_with_confirm_runs_the_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    runs = _hourly(2)
    root, warehouse = _elementary_project(
        tmp_path, runs, _results(runs, failing=["inv-2"])
    )
    engine = _engine(root, warehouse)
    _billed(engine, monkeypatch, confirmed=True)
    admitted: list[float] = []
    admit = CostGate.preflight_command

    def counted(self, estimate):
        admitted.append(estimate)
        return admit(self, estimate)

    monkeypatch.setattr(CostGate, "preflight_command", counted)

    data = _data(engine.incidents())
    assert data["sources"]["elementary"]["status"] == "read"
    assert len(data["incidents"]) == 1
    assert data["price"]["estimate"] is not None
    assert data["price"]["settled"] is not None
    assert data["price"]["settled"] == data["spend"]
    assert [e for e in engine.store._queries if e["decision"] == "allowed"]
    # The whole read once, then each of the reader's three phases through the
    # handshake it was handed, so every phase still meets the gate.
    assert len(admitted) == 4


# --- acceptance: the window ---------------------------------------------------------


def test_the_default_window_covers_at_most_twenty_invocations(tmp_path: Path):
    runs = _hourly(25)
    root, warehouse = _elementary_project(tmp_path, runs, _results(runs))
    data = _data(_engine(root, warehouse).incidents())

    window = data["window"]
    assert (window["invocations"], window["count"]) == (20, 20)
    assert window["first"]["invocation_id"] == "inv-6"
    assert window["last"]["invocation_id"] == "inv-25"
    assert data["sources"]["elementary"]["measure"]["invocations"] == 20
    assert data["sources"]["elementary"]["windowed"] is True


def test_fifty_invocations_cover_at_most_fifty(tmp_path: Path):
    runs = _hourly(60)
    root, warehouse = _elementary_project(tmp_path, runs, _results(runs))
    data = _data(_engine(root, warehouse).incidents(invocations=50))

    window = data["window"]
    assert (window["invocations"], window["count"]) == (50, 50)
    assert window["first"]["invocation_id"] == "inv-11"


def test_a_window_wider_than_the_table_reads_it_whole(tmp_path: Path):
    runs = _hourly(5)
    root, warehouse = _elementary_project(tmp_path, runs, _results(runs))
    data = _data(_engine(root, warehouse).incidents())

    assert data["window"]["count"] == 5
    assert data["sources"]["elementary"]["windowed"] is False


@pytest.mark.parametrize("invocations", [0, -1, 2001])
def test_a_window_outside_the_bound_is_refused(tmp_path: Path, invocations: int):
    root = _project(tmp_path / "project")
    with pytest.raises(ValueError, match="--invocations"):
        _engine(root).incidents(invocations=invocations)


# --- acceptance: the engine and the CLI agree ---------------------------------------


def test_the_engine_method_returns_what_the_cli_returns(tmp_path: Path):
    runs = _hourly(4)
    root, warehouse = _elementary_project(
        tmp_path, runs, _results(runs, failing=["inv-4"])
    )
    _run_results(root, {TEST: "fail"}, invocation="inv-4", at=runs[-1].started)
    library = _data(_engine(root, warehouse).incidents(["orders"], invocations=3))

    args = argparse.Namespace(
        group="maintain", subcommand="incidents", objects=["orders"], invocations=3
    )
    envelope = dispatch(args, _engine(root, warehouse))
    assert envelope.status == "ok"
    for volatile in (library, envelope.data):
        for incident in volatile["incidents"]:
            incident.pop("schedule", None)
    assert envelope.data == library


# --- one invocation order across the two sources ------------------------------------


def test_a_dbt_run_elementary_also_recorded_takes_elementary_s_place(tmp_path: Path):
    runs = _hourly(3)
    root, warehouse = _elementary_project(
        tmp_path, runs, _results(runs, failing=["inv-3"])
    )
    _run_results(root, {TEST: "fail"}, invocation="inv-3", at=runs[-1].started)
    data = _data(_engine(root, warehouse).incidents())

    [incident] = data["incidents"]
    assert incident["opening_source"] == "elementary"
    sources = {o["source"] for o in incident["observations"]}
    assert sources == {"elementary", "dbt_run_results"}
    assert data["sources"]["dbt_run_results"]["folded"] is True
    assert data["sources"]["dbt_run_results"]["placed"] == "in_window"
    assert data["window"]["count"] == 3
    assert data["window"]["after_window"] == []


def test_a_dbt_run_after_the_window_is_placed_after_it(tmp_path: Path):
    runs = _hourly(3)
    root, warehouse = _elementary_project(tmp_path, runs, _results(runs))
    _run_results(root, {TEST: "fail"}, invocation="dbt-later")
    data = _data(_engine(root, warehouse).incidents())

    [incident] = data["incidents"]
    assert incident["opening_source"] == "dbt_run_results"
    assert data["sources"]["dbt_run_results"]["placed"] == "after_window"
    # Folded after the window, and named apart: the count is Elementary's.
    assert (data["window"]["count"], data["window"]["last"]["invocation_id"]) == (
        3,
        "inv-3",
    )
    assert data["window"]["after_window"] == [
        {
            "invocation_id": "dbt-later",
            "sequence": 4,
            "at": "2026-12-01T00:00:00+00:00",
        }
    ]


def test_a_dbt_run_before_the_window_is_left_out_of_the_fold(tmp_path: Path):
    runs = _hourly(3)
    root, warehouse = _elementary_project(tmp_path, runs, _results(runs))
    _run_results(root, {TEST: "fail"}, invocation="dbt-earlier", at=START)
    data = _data(_engine(root, warehouse).incidents())

    assert data["incidents"] == []
    assert data["sources"]["dbt_run_results"]["folded"] is False
    assert data["sources"]["dbt_run_results"]["placed"] == "unplaced"
    assert data["window"]["count"] == 3


def test_a_dbt_run_that_ended_after_the_window_but_started_before_it_is_unplaced(
    tmp_path: Path,
):
    """dbt's ``generated_at`` is the run's end and Elementary's start is a
    start: a run that began before the newest run and ended after it is not
    later than it, so it gets no place after the window."""

    runs = _hourly(3)
    root, warehouse = _elementary_project(tmp_path, runs, _results(runs))
    newest = runs[-1].started
    _run_results(
        root,
        {TEST: "fail"},
        invocation="dbt-overlapping",
        at=newest + timedelta(minutes=5),
        started=newest - timedelta(minutes=5),
    )
    data = _data(_engine(root, warehouse).incidents())

    assert data["incidents"] == []
    assert data["sources"]["dbt_run_results"]["placed"] == "unplaced"


def test_a_later_dbt_run_never_lifts_the_count_past_the_window(tmp_path: Path):
    runs = _hourly(25)
    root, warehouse = _elementary_project(tmp_path, runs, _results(runs))
    _run_results(root, {TEST: "fail"}, invocation="dbt-later")
    window = _data(_engine(root, warehouse).incidents())["window"]

    assert (window["invocations"], window["count"]) == (20, 20)
    assert [p["invocation_id"] for p in window["after_window"]] == ["dbt-later"]


def test_a_read_refused_after_it_was_priced_keeps_its_price(tmp_path: Path):
    runs = [*_hourly(2), Run("inv-old", START, version="0.16.4")]
    root, warehouse = _elementary_project(tmp_path, runs, _results(runs))
    data = _data(_engine(root, warehouse).incidents())

    elementary = data["sources"]["elementary"]
    assert (elementary["status"], elementary["reason_code"]) == (
        "refused",
        "elementary_rows_untested_release",
    )
    assert data["price"]["estimate"] == 0.0
    assert "read result rows" in data["price"]["per_statement"]


# --- the selector -------------------------------------------------------------------


def test_a_selector_keeps_only_the_named_model_s_tests(tmp_path: Path):
    root = _project(tmp_path / "project")
    _run_results(root, {TEST: "fail", OTHER: "fail"})
    data = _data(_engine(root).incidents(["customers"]))

    assert [i["subject"]["key"] for i in data["incidents"]] == [OTHER]
    assert data["scope"] == {"objects": ["customers"], "tests": 1}


def test_a_name_no_model_has_selects_nothing_and_says_so(tmp_path: Path):
    root = _project(tmp_path / "project")
    _run_results(root, {TEST: "fail"})
    result = _engine(root).incidents(["nowhere"])

    assert result.incidents == []
    assert any("nowhere" in warning for warning in result.warnings)


# --- reading nothing is an answer ---------------------------------------------------


def test_no_manifest_is_reported_for_every_source(tmp_path: Path):
    root = tmp_path / "project"
    root.mkdir()
    (root / "dbt_project.yml").write_text("name: shop\n")
    data = _data(_engine(root).incidents())

    assert {name: s["reason_code"] for name, s in data["sources"].items()} == {
        "elementary": "no_manifest",
        "dbt_run_results": "no_manifest",
    }
    assert data["incidents"] == []


def test_no_run_results_is_missing_rather_than_an_empty_history(tmp_path: Path):
    root = _project(tmp_path / "project")
    data = _data(_engine(root).incidents())

    dbt = data["sources"]["dbt_run_results"]
    assert (dbt["status"], dbt["reason_code"]) == ("missing", "no_run_results")


def test_the_command_lists_both_sources_in_their_precedence():
    assert incident_sources.SOURCES == ("elementary", "dbt_run_results")


# --- an installed package that holds nothing to read --------------------------------


def test_a_package_whose_tables_are_not_built_is_missing(tmp_path: Path):
    root = _project(tmp_path / "project", elementary=True)
    _run_results(root, {TEST: "fail"})
    data = _data(_engine(root).incidents())

    elementary = data["sources"]["elementary"]
    assert (elementary["status"], elementary["reason_code"]) == (
        "missing",
        "tables_absent",
    )
    # The other source still answers.
    assert [i["opening_source"] for i in data["incidents"]] == ["dbt_run_results"]


def test_tables_with_no_run_yet_are_missing_rather_than_a_clean_history(
    tmp_path: Path,
):
    root, warehouse = _elementary_project(tmp_path, [], [])
    data = _data(_engine(root, warehouse).incidents())

    elementary = data["sources"]["elementary"]
    assert (elementary["status"], elementary["reason_code"]) == (
        "missing",
        "elementary_no_invocations",
    )


def test_a_package_that_does_not_upload_test_results_is_missing(tmp_path: Path):
    runs = _hourly(2)
    root, warehouse = _elementary_project(tmp_path, runs, _results(runs))
    (root / "dbt_project.yml").write_text(
        "name: shop\nversion: '1.0'\nvars:\n  disable_tests_results: true\n"
    )
    engine = _engine(root, warehouse)
    data = _data(engine.incidents())

    elementary = data["sources"]["elementary"]
    assert (elementary["status"], elementary["reason_code"]) == (
        "missing",
        "not_uploading",
    )
    assert engine.store._queries == []


def test_a_package_disabled_for_the_target_is_missing(tmp_path: Path):
    root = _project(tmp_path / "project", elementary=True)
    manifest = json.loads((root / "target" / "manifest.json").read_text())
    for uid in [u for u in manifest["nodes"] if u.startswith("model.elementary.")]:
        manifest["disabled"][uid] = [manifest["nodes"].pop(uid)]
    (root / "target" / "manifest.json").write_text(json.dumps(manifest))
    data = _data(_engine(root).incidents())

    assert data["sources"]["elementary"]["reason_code"] == "disabled"


def test_a_scope_that_narrows_the_schema_out_is_refused_with_no_config_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    root = _project(tmp_path / "project", elementary=True)
    config = DexConfig(
        connector="postgres",
        postgres=PostgresTarget(schemas=["analytics", SCHEMA]),
    )
    engine = DexEngine(config=config, repo_root=str(root), scopes=["analytics"])
    monkeypatch.setattr(engine, "_adapter", lambda *a, **k: pytest.fail("opened"))
    data = _data(engine.incidents())

    elementary = data["sources"]["elementary"]
    assert (elementary["status"], elementary["reason_code"]) == (
        "refused",
        "outside_allowlist",
    )
    assert "--scope" in elementary["detail"]
    assert "config_set" not in elementary
    assert not [s for s in data["next_steps"] if "config set" in s["command"]]


# --- the final pass on #1572 --------------------------------------------------------


def test_an_unavailable_connection_refuses_elementary_and_still_reads_dbt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from exmergo_dex_core.errors import NoConnectorSelectedError

    root = _project(tmp_path / "project", elementary=True)
    _run_results(root, {TEST: "fail"})
    engine = _engine(root)

    def unavailable(*_args, **_kwargs):
        raise NoConnectorSelectedError("no connector is configured or inferable")

    monkeypatch.setattr(engine, "_adapter", unavailable)
    data = _data(engine.incidents())

    elementary = data["sources"]["elementary"]
    assert (elementary["status"], elementary["reason_code"]) == (
        "refused",
        "connection_unavailable",
    )
    assert data["sources"]["dbt_run_results"]["status"] == "read"
    assert [i["opening_source"] for i in data["incidents"]] == ["dbt_run_results"]


def test_an_unreadable_catalog_refuses_elementary_and_still_reads_dbt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from exmergo_dex_core.errors import ConnectorError

    runs = _hourly(2)
    root, warehouse = _elementary_project(tmp_path, runs, _results(runs))
    _run_results(root, {TEST: "fail"}, invocation="dbt-later")
    engine = _engine(root, warehouse)
    opened = engine._adapter

    def no_catalog(*args, **kwargs):
        adapter = opened(*args, **kwargs)

        def refuse():
            raise ConnectorError("the catalog could not be listed")

        adapter.list_objects = refuse
        return adapter

    monkeypatch.setattr(engine, "_adapter", no_catalog)
    data = _data(engine.incidents())

    assert data["sources"]["elementary"]["reason_code"] == "catalog_unavailable"
    assert [i["opening_source"] for i in data["incidents"]] == ["dbt_run_results"]


def test_a_cost_refusal_at_the_connection_stays_the_command_s(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from exmergo_dex_core.guards.cost_guard import CeilingRequiredError

    root = _project(tmp_path / "project", elementary=True)
    _run_results(root, {TEST: "fail"})
    engine = _engine(root)

    def ceiling(*_args, **_kwargs):
        raise CeilingRequiredError("no ceiling is committed")

    monkeypatch.setattr(engine, "_adapter", ceiling)
    with pytest.raises(CeilingRequiredError):
        engine.incidents()


def test_every_statement_runs_on_the_gate_that_took_the_reservation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The engine builds a new gate on every `_adapter` call, as `_billed`
    does, so a second acquisition would leave the reservation behind."""

    runs = _hourly(2)
    root, warehouse = _elementary_project(tmp_path, runs, _results(runs))
    engine = _engine(root, warehouse)
    _billed(engine, monkeypatch, confirmed=True)
    gated = engine._adapter
    acquired: list[Any] = []

    def counted(*args, **kwargs):
        adapter = gated(*args, **kwargs)
        acquired.append(adapter.cost_gate)
        return adapter

    monkeypatch.setattr(engine, "_adapter", counted)
    admitted_on: list[int] = []
    admit = CostGate.preflight_command

    def on(self, estimate):
        admitted_on.append(id(self))
        return admit(self, estimate)

    monkeypatch.setattr(CostGate, "preflight_command", on)

    data = _data(engine.incidents())
    assert data["sources"]["elementary"]["status"] == "read"
    assert len(acquired) == 1
    # The whole-read reservation and each phase's admission, on one gate.
    assert len(admitted_on) == 4
    assert set(admitted_on) == {id(acquired[0])}


def test_a_claim_too_long_to_key_a_subject_falls_back_to_the_unique_id(
    tmp_path: Path,
):
    root = _project(tmp_path / "project")
    manifest = json.loads((root / "target" / "manifest.json").read_text())
    manifest["nodes"][TEST]["config"]["meta"] = {"dex": {"claim": "c" * 513}}
    (root / "target" / "manifest.json").write_text(json.dumps(manifest))
    _run_results(root, {TEST: "fail"})
    result = _engine(root).incidents()

    [incident] = _data(result)["incidents"]
    assert incident["subject"]["key"] == TEST
    assert any("longer than 512" in warning for warning in result.warnings)


def test_a_read_that_cannot_be_priced_is_never_asked_at_a_lower_price(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from exmergo_dex_core.explore import commands as explore_commands

    runs = _hourly(2)
    root, warehouse = _elementary_project(tmp_path, runs, _results(runs))
    _run_results(root, {TEST: "fail"}, invocation="dbt-later")
    engine = _engine(root, warehouse)

    def unpriceable(*_args, **_kwargs):
        raise RuntimeError("the dry run failed")

    monkeypatch.setattr(explore_commands, "_profile_estimate", unpriceable)
    data = _data(engine.incidents())

    elementary = data["sources"]["elementary"]
    assert (elementary["status"], elementary["reason_code"]) == ("refused", "unpriced")
    assert "the dry run failed" not in elementary["detail"]
    assert [e for e in engine.store._queries if e["decision"] == "allowed"] == []
    assert data["sources"]["dbt_run_results"]["status"] == "read"


def test_a_mixed_release_refusal_advises_invocations_not_since(tmp_path: Path):
    runs = [*_hourly(2), Run("inv-old", START, version="0.16.4")]
    root, warehouse = _elementary_project(tmp_path, runs, _results(runs))
    detail = _data(_engine(root, warehouse).incidents())["sources"]["elementary"][
        "detail"
    ]

    assert "--invocations" in detail
    assert "since" not in detail.replace("since the upgrade", "")


def test_a_reused_engine_reports_no_spend_for_a_read_of_files_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import shutil

    runs = _hourly(2)
    root, warehouse = _elementary_project(tmp_path, runs, _results(runs))
    engine = _engine(root, warehouse)
    _billed(engine, monkeypatch, confirmed=True)
    assert engine.incidents().spend is not None

    # The same engine, and now a project with no Elementary: files alone.
    shutil.rmtree(root / "dbt_packages")
    (root / "target" / "manifest.json").write_text(
        json.dumps(_manifest(elementary=False))
    )
    _run_results(root, {TEST: "fail"})
    second = engine.incidents()

    assert second.spend is None
    assert second.price == {"estimate": None, "settled": None}


def test_a_dataset_narrowing_on_bigquery_is_held_before_any_connection():
    target = argparse.Namespace(datasets=["dev.analytics", "dev.main_elementary"])
    engine = argparse.Namespace(
        connector="bigquery",
        datasets=["dev.analytics"],
        scopes=None,
        config=argparse.Namespace(connector="bigquery", bigquery=target),
    )
    refusal = _allowlist_refusal(engine, _Located("dev", SCHEMA), [])

    assert refusal is not None
    assert refusal.reason_code == "outside_allowlist"
    assert refusal.detail is not None and "--dataset" in refusal.detail
