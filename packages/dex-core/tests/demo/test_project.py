"""The demo's dbt project and its seeded build (#1477, part 1).

`dex demo` writes a small dbt project beside the warehouse, with the artifacts
its last build left: a manifest, and run results in which one test failed
because of the batch the warehouse loads twice. So the first `dex maintain
incidents` has an incident to read, with no build, no credentials and no
network, and `dex maintain diagnose` names its cause.

Three kinds of assertion. Determinism, pinned as the warehouse's is. The
artifacts' own consistency (the failure is the one the data plants, every test
the run reports is one the manifest holds). And the acceptance, driven through
the CLI with the network refused: the demo then the printed command returns the
seeded incident, and the same demo with the failing result removed returns none.
"""

from __future__ import annotations

import json
import shlex
import socket
from pathlib import Path

import pytest

from exmergo_dex_core.cli import main
from exmergo_dex_core.demo import DEMO_FILENAME, build_tables
from exmergo_dex_core.demo.project import (
    BUILD_INVOCATION_ID,
    DEMO_DIR,
    FAILING_TEST,
    PROJECT_DIR,
    build_project,
    duplicated_keys,
    project_digest,
    write_project,
)

# The pinned digest over every file of the project the default demo writes. If
# an edit moves it, re-read the generated project and the incident it gives
# (the counts below, the README's tour), then update this value.
GOLDEN_PROJECT_DIGEST = (
    "60efac52619e3344a59f5efc4de03be78e31fc0753d5674b7f77e822a1788086"
)

#: Values of order_items.order_item_id that occur more than once: the rows the
#: dbt `unique` test returns, which a real `dbt build` of the project reports
#: as `failures` (956, measured with dbt 1.12.5 and dbt-duckdb 1.11.1).
DUPLICATED_ITEM_IDS = 956


def _project():
    return build_project(build_tables(), DEMO_FILENAME)


def _document(project, path: str) -> dict:
    return json.loads(dict(project.files)[path])


@pytest.fixture
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Any socket a command opens fails the test: the demo promises none.

    Python's sockets only: a connection made from native code (a DuckDB
    extension autoload, say) would pass it. The demo loads no extension."""

    def refuse(*_args, **_kwargs):
        raise AssertionError("the demo opened a network connection")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


def _run(argv: list[str], capsys) -> dict:
    rc = main(argv)
    out = capsys.readouterr().out
    assert out.count("\n") == 1, "exactly one envelope line"
    payload = json.loads(out)
    assert rc == (1 if payload["status"] == "error" else 0), payload
    return payload


def _printed(payload: dict, command: str) -> list[str]:
    [step] = [s for s in payload["data"]["next_steps"] if command in s["command"]]
    return shlex.split(step["command"])[1:]


# --- determinism ------------------------------------------------------------------


def test_the_project_is_byte_for_byte_the_documented_project():
    project = _project()
    assert project_digest(project) == GOLDEN_PROJECT_DIGEST
    assert project_digest(_project()) == GOLDEN_PROJECT_DIGEST


def test_the_project_follows_the_warehouse_file_it_is_written_for():
    """dbt-duckdb names the database after the file, so the manifest does too,
    and the profile points at the file beside the project."""

    project = build_project(build_tables(), "shop.duckdb")
    assert project_digest(project) != GOLDEN_PROJECT_DIGEST
    manifest = _document(project, "target/manifest.json")
    assert {s["database"] for s in manifest["sources"].values()} == {"shop"}
    assert "path: ../../shop.duckdb" in dict(project.files)["profiles.yml"]


def test_no_project_value_depends_on_the_wall_clock():
    from exmergo_dex_core.demo import project

    source = Path(project.__file__).read_text(encoding="utf-8")
    # A text check, so it catches the usual spellings only; the pinned digest
    # above is what holds the output still.
    for forbidden in (
        "date.today",
        "datetime.today",
        "datetime.now",
        "datetime.utcnow",
        "time.time",
        "time.monotonic",
        "uuid.uuid",
    ):
        assert forbidden not in source, forbidden


# --- the artifacts agree with the data and with each other ------------------------


def test_the_one_failing_test_fails_for_the_batch_loaded_twice():
    tables = build_tables()
    assert duplicated_keys(tables, "order_items", "order_item_id") == (
        DUPLICATED_ITEM_IDS
    )
    run = _document(_project(), "target/run_results.json")
    tests = {
        r["unique_id"]: r for r in run["results"] if r["unique_id"].startswith("test.")
    }
    assert {uid: r["status"] for uid, r in tests.items()} == {
        "test.analytics.unique_stg_orders_order_id.e3b841c71a": "pass",
        "test.analytics.not_null_stg_orders_order_id.81cfe2fe64": "pass",
        FAILING_TEST: "fail",
        "test.analytics.not_null_stg_order_items_order_item_id.26a7e2bc35": "pass",
    }
    assert tests[FAILING_TEST]["failures"] == DUPLICATED_ITEM_IDS
    assert run["args"]["which"] == "build"
    assert run["metadata"]["invocation_id"] == BUILD_INVOCATION_ID


def test_every_result_names_a_node_the_manifest_holds_and_the_test_reads_one_source():
    """What diagnosis walks: the failing test's model, and the one source it
    reads, which carries the column the test claims unique."""

    project = _project()
    manifest = _document(project, "target/manifest.json")
    run = _document(project, "target/run_results.json")
    assert {r["unique_id"] for r in run["results"]} <= set(manifest["nodes"])

    test = manifest["nodes"][FAILING_TEST]
    assert test["test_metadata"]["name"] == "unique"
    assert test["test_metadata"]["kwargs"]["column_name"] == "order_item_id"
    model = manifest["nodes"][test["attached_node"]]
    [source] = model["depends_on"]["nodes"]
    assert manifest["sources"][source]["identifier"] == "order_items"
    assert manifest["sources"][source]["schema"] == "main"
    sql = dict(project.files)["models/staging/stg_order_items.sql"]
    assert "order_item_id" in sql and "source('shop', 'order_items')" in sql


# --- create-only ------------------------------------------------------------------


def test_an_existing_project_directory_is_refused_before_anything_is_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
):
    """Refused before the warehouse is even generated, not removed after: a
    refused call has written nothing at all."""

    pytest.importorskip("duckdb")
    from exmergo_dex_core.demo import commands

    def never(*_args, **_kwargs):
        raise AssertionError("the warehouse was generated before the refusal")

    monkeypatch.setattr(commands, "generate_demo_warehouse", never)
    (tmp_path / DEMO_DIR).mkdir()
    (tmp_path / DEMO_DIR / "keep.txt").write_text("mine", encoding="utf-8")

    payload = _run(["--repo-root", str(tmp_path), "demo"], capsys)

    assert payload["status"] == "error"
    assert payload["reason"] == "guard"
    assert "never opens or replaces" in payload["errors"][0]
    assert sorted(p.name for p in tmp_path.iterdir()) == [DEMO_DIR]
    assert sorted(p.name for p in (tmp_path / DEMO_DIR).iterdir()) == ["keep.txt"]
    assert (tmp_path / DEMO_DIR / "keep.txt").read_text(encoding="utf-8") == "mine"


def test_a_failed_project_write_leaves_nothing_behind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
):
    """A half-written project, warehouse or config would read as real to the
    next command, so a failure part way removes all three."""

    pytest.importorskip("duckdb")
    opened: list[Path] = []
    real_open = Path.open

    def failing_open(self: Path, mode: str = "r", *args, **kwargs):
        if mode == "x":
            opened.append(self)
            if len(opened) == 3:
                raise OSError("disk full")
        return real_open(self, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", failing_open)
    payload = _run(["--repo-root", str(tmp_path), "demo"], capsys)
    assert payload["status"] == "error"
    assert len(opened) == 3
    assert list(tmp_path.rglob("*")) == []


def test_a_warehouse_named_like_the_project_directory_is_refused_first(
    tmp_path: Path, capsys
):
    """`dex demo demo_project` would make the warehouse file the directory the
    project is written into; it is refused up front, naming why, and writes
    nothing."""

    pytest.importorskip("duckdb")
    payload = _run(["--repo-root", str(tmp_path), "demo", DEMO_DIR], capsys)

    assert payload["status"] == "error"
    assert payload["reason"] == "request"
    assert "cannot also be the warehouse file" in payload["errors"][0]
    assert list(tmp_path.iterdir()) == []


def test_the_sources_read_as_written_before_the_build_that_read_them(
    tmp_path: Path,
):
    """A reader comparing the sources' times with the manifest's would
    otherwise call the seeded artifacts stale on every run."""

    from exmergo_dex_core.dbt_project import load, manifest_freshness

    project = _project()
    write_project(tmp_path, project)
    freshness = manifest_freshness(load(tmp_path / PROJECT_DIR))
    assert freshness["manifest_present"] is True
    assert freshness["stale"] is False


def test_write_project_refuses_a_directory_that_appeared(tmp_path: Path):
    (tmp_path / DEMO_DIR).mkdir()
    from exmergo_dex_core.demo import DemoTargetExistsError

    with pytest.raises(DemoTargetExistsError):
        write_project(tmp_path, _project())


# --- acceptance: dex demo, then dex maintain incidents ----------------------------


def test_the_demo_then_maintain_incidents_returns_the_seeded_incident(
    run_from, no_network, capsys
):
    pytest.importorskip("duckdb")
    demo = _run(run_from.argv("demo"), capsys)
    data = _run(_printed(demo, "maintain incidents"), capsys)["data"]

    [incident] = data["incidents"]
    assert incident["subject"] == {"kind": "unique_id", "key": FAILING_TEST}
    assert incident["state"] == "open"
    assert incident["opening_source"] == "dbt_run_results"
    # One build is all dbt's run results hold, so the start is unknown.
    assert incident["first_observed"] == "unknown"
    assert incident["last_observed"]["run"] == BUILD_INVOCATION_ID
    assert data["sources"]["dbt_run_results"]["status"] == "read"
    assert data["sources"]["elementary"] == {
        "status": "missing",
        "reason_code": "not_installed",
        "detail": "the project does not install the Elementary package",
    }
    assert any("first_observed: unknown" in note for note in data["notes"])


def test_the_same_demo_with_the_failing_result_removed_returns_no_incident(
    run_from, no_network, capsys
):
    pytest.importorskip("duckdb")
    demo = _run(run_from.argv("demo"), capsys)
    path = run_from.root / PROJECT_DIR / "target" / "run_results.json"
    run = json.loads(path.read_text(encoding="utf-8"))
    run["results"] = [r for r in run["results"] if r["unique_id"] != FAILING_TEST]
    path.write_text(json.dumps(run), encoding="utf-8")

    data = _run(_printed(demo, "maintain incidents"), capsys)["data"]

    assert data["incidents"] == []
    # Read, so the empty list is an answer and not a source that went unread.
    assert data["sources"]["dbt_run_results"]["status"] == "read"


def test_diagnosis_names_the_batch_loaded_twice_as_the_cause(
    run_from, no_network, capsys
):
    """The demo-level check #1475 left to this issue: the cause diagnosis
    names is the defect the warehouse plants, measured on the source the
    failing test's model reads."""

    pytest.importorskip("duckdb")
    demo = _run(run_from.argv("demo"), capsys)
    data = _run(_printed(demo, "maintain diagnose"), capsys)["data"]

    [diagnosis] = data["diagnoses"]
    assert diagnosis["subject"] == FAILING_TEST
    assert diagnosis["verdict"] == "cause"
    [cause] = diagnosis["causes"]
    assert (cause["owner"], cause["kind"]) == ("loader", "duplicate_load")
    assert cause["relation"] == "dex_demo.main.order_items"
    assert cause["columns"] == ["order_item_id"]
    assert cause["finding"] == "declared_grain_not_unique"
    assert cause["evidence"]["row_count"] == 14000
    assert cause["evidence"]["distinct_count"] == 13000
    [step] = diagnosis["next_steps"]
    assert step["command"].startswith("dex transform plan ")


# --- the seeded manifest is the one dbt would write -------------------------------


def test_dbt_parses_the_project_into_the_nodes_the_seeded_manifest_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The test ids carry hashes dbt derives, and the incident is keyed by
    them, so a real parse of the generated project has to name the same nodes,
    each reading the same parents: otherwise the first real build would read
    as a different test from the one the seeded run failed."""

    pytest.importorskip("dbt.cli.main")
    pytest.importorskip("dbt.adapters.duckdb")
    from dbt.cli.main import dbtRunner

    for name in ("DBT_TARGET_PATH", "DBT_ENGINE_TARGET_PATH", "DBT_PROFILES_DIR"):
        monkeypatch.delenv(name, raising=False)
    project = _project()
    base = tmp_path / PROJECT_DIR
    for relative, text in project.sources:
        (base / relative).parent.mkdir(parents=True, exist_ok=True)
        (base / relative).write_text(text, encoding="utf-8")
    monkeypatch.chdir(base)
    result = dbtRunner().invoke(
        ["parse", "--project-dir", str(base), "--profiles-dir", str(base)]
    )
    assert result.success, result.exception

    parsed = json.loads((base / "target" / "manifest.json").read_text("utf-8"))
    seeded = _document(project, "target/manifest.json")

    def lineage(manifest: dict) -> dict[str, list[str]]:
        return {
            uid: sorted(node["depends_on"]["nodes"])
            for uid, node in manifest["nodes"].items()
            if node["package_name"] == "analytics"
        }

    assert lineage(parsed) == lineage(seeded)
    assert set(parsed["sources"]) == set(seeded["sources"])
    for uid in lineage(seeded):
        for field in ("database", "schema", "name", "test_metadata", "attached_node"):
            assert parsed["nodes"][uid].get(field) == seeded["nodes"][uid].get(field), (
                uid,
                field,
            )
