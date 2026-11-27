"""The demo's dbt project, its seeded builds and Elementary's record of them (#1477).

`dex demo` writes a small dbt project beside the warehouse, and seeds the history
of seven weekly builds of it: Elementary's ``dbt_invocations`` and
``elementary_test_results`` in the warehouse file, and the newest build's
manifest and ``run_results.json`` in the project. In the last two builds one test
fails, because of the batch the warehouse loads twice. So the first `dex maintain
incidents` reads an incident from the ``elementary`` source with the build it was
first observed in, and `dex maintain diagnose` names its cause, with no build, no
credentials and no network.

The seeded builds are dated back from when the demo ran, not from the warehouse's
fixed anchor, so the incident is still open when the tour reaches it. Determinism
is therefore pinned at a fixed generation time, and a second time proves that
only those times move, each by exactly the shift.

The kinds of assertion: that determinism; the artifacts agreeing with the data
and each other; the seeded tables being the reader's recorded layout; the release
the resolver reads; create-only; and the acceptance, driven through the CLI with
Python's sockets refused.
"""

from __future__ import annotations

import itertools
import json
import re
import shlex
import shutil
import socket
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from exmergo_dex_core.cli import main
from exmergo_dex_core.demo import DEMO_FILENAME, build_tables, row_digest
from exmergo_dex_core.demo import commands as demo_commands
from exmergo_dex_core.demo import elementary as seeded
from exmergo_dex_core.demo.project import (
    BUILD_INVOCATION_ID,
    DEMO_DIR,
    FAILING_TEST,
    PROJECT_DIR,
    build_project,
    duplicated_keys,
    history_tables,
    project_digest,
    write_project,
)

#: A fixed generation time, at which the seeded history is pinned.
REFERENCE = datetime(2026, 11, 24, 12, 0, 0, tzinfo=UTC)

# The pinned digests at REFERENCE. If an edit moves either, re-read the generated
# project, the history and the incident they give (the counts below, the tour),
# then update the value.
GOLDEN_PROJECT_DIGEST = (
    "218c769a51ad5d0926d7f35ffdc197def6f28ca3967c2728637223ba114f8dd7"
)
GOLDEN_HISTORY_DIGEST = (
    "b153c4ce178845bc4ac12cd7d61eb8daf13f85f2050fa4fdd9e2891146ae9073"
)

#: Values of order_items.order_item_id that occur more than once: the rows the
#: dbt `unique` test returns, which a real `dbt build` of the project reports
#: as `failures` (956, measured with dbt 1.12.5 and dbt-duckdb 1.11.1).
DUPLICATED_ITEM_IDS = 956

#: How Elementary 0.26.1 casts each layout type name on DuckDB (`empty_column`),
#: written out here rather than read off the module, so a seed typed wrongly
#: cannot agree with itself.
DUCKDB_TYPES = {
    "boolean": "BOOLEAN",
    "timestamp": "TIMESTAMP",
    "int": "INTEGER",
    "bigint": "BIGINT",
    "float": "DOUBLE",
    "long_string": "VARCHAR",
    "string": "VARCHAR",
}

_ISO_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"
_ISO = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z")
_TEXT = re.compile(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}")


def _runs(at: datetime = REFERENCE):
    return seeded.seeded_runs(at)


def _project(at: datetime = REFERENCE):
    return build_project(build_tables(), DEMO_FILENAME, _runs(at))


def _history(at: datetime = REFERENCE):
    return history_tables(build_tables(), DEMO_FILENAME, _runs(at))


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


# --- determinism, at a fixed generation time --------------------------------------


def test_the_project_is_byte_for_byte_the_documented_project():
    assert project_digest(_project()) == GOLDEN_PROJECT_DIGEST
    assert project_digest(_project()) == GOLDEN_PROJECT_DIGEST


def test_the_seeded_history_is_byte_for_byte_the_documented_history():
    history = _history()
    assert row_digest(history) == GOLDEN_HISTORY_DIGEST
    assert {t.qualified: len(t.rows) for t in history} == {
        "main_elementary.dbt_invocations": 6,
        "main_elementary.elementary_test_results": 24,
        "main_elementary.test_result_rows": 0,
        "main_elementary.data_monitoring_metrics": 0,
    }


def test_only_the_seeded_build_times_move_with_the_generation_time():
    """Generated later, every time in the history and the artifacts moves by
    exactly the shift, and nothing else changes: the offsets are pinned."""

    shift = timedelta(days=3, hours=5, minutes=7)
    for before, after in zip(_history(), _history(REFERENCE + shift), strict=True):
        assert before.columns == after.columns
        for old, new in zip(before.rows, after.rows, strict=True):
            for a, b in zip(old, new, strict=True):
                if isinstance(a, datetime):
                    assert b - a == shift
                elif isinstance(a, str) and _TEXT.fullmatch(a):
                    moved = datetime.fromisoformat(b) - datetime.fromisoformat(a)
                    assert moved == shift
                else:
                    assert a == b

    def back(text: str) -> str:
        return _ISO.sub(
            lambda m: (datetime.strptime(m.group(), _ISO_FORMAT) - shift).strftime(
                _ISO_FORMAT
            ),
            text,
        )

    later = _project(REFERENCE + shift)
    assert [(path, back(text)) for path, text in later.files] == list(_project().files)
    assert later.sources_written_at - shift == _project().sources_written_at


def test_the_only_clock_the_demo_reads_dates_the_seeded_builds():
    """The renderers take the generation time; the command reads it, once."""

    from exmergo_dex_core.demo import project

    for module in (project, seeded):
        source = Path(module.__file__).read_text(encoding="utf-8")
        for forbidden in (
            "date.today",
            "datetime.today",
            "datetime.now",
            "datetime.utcnow",
            "time.time",
            "time.monotonic",
            "uuid.uuid",
        ):
            assert forbidden not in source, (module.__name__, forbidden)
    command = Path(demo_commands.__file__).read_text(encoding="utf-8")
    assert command.count("datetime.now(") == 1
    assert "seeded_runs(datetime.now(UTC))" in command


# --- the history and the artifacts agree with the data and with each other --------


def test_the_failing_test_passes_four_weeks_and_fails_the_last_two():
    runs = _runs()
    assert [r.hooks for r in runs] == [True, True, True, True, False, True, True]
    assert [r.outcome for r in runs][-2:] == ["fail", "fail"]
    gaps = {b.started_at - a.started_at for a, b in itertools.pairwise(runs)}
    assert gaps == {timedelta(days=7)}
    assert runs[-1].started_at < REFERENCE

    invocations, results, *_ = _history()
    index = {name: i for i, (name, _) in enumerate(results.columns)}
    by_run: dict[str, str] = {}
    for row in results.rows:
        status = row[index["status"]]
        if row[index["test_unique_id"]] == FAILING_TEST:
            by_run[row[index["invocation_id"]]] = status
            if status == "fail":
                assert row[index["failures"]] == DUPLICATED_ITEM_IDS
        else:
            assert status == "pass"
        assert row[index["result_rows"]] is None, "no sampled row is seeded"
    assert [by_run.get(r.invocation_id) for r in runs] == [
        "pass",
        "pass",
        "pass",
        "pass",
        None,
        "fail",
        "fail",
    ]
    assert len(invocations.rows) == 6, "the hooks-off week writes no row"


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


def test_the_build_dbt_recorded_is_the_newest_run_elementary_recorded():
    newest = _runs()[-1]
    run = _document(_project(), "target/run_results.json")
    assert run["metadata"]["invocation_id"] == newest.invocation_id
    assert newest.invocation_id == BUILD_INVOCATION_ID
    assert run["metadata"]["invocation_started_at"] == newest.started_at.strftime(
        _ISO_FORMAT
    )


def test_every_result_names_a_node_the_manifest_holds_and_the_test_reads_one_source():
    """What diagnosis walks: the failing test's model, and the one source it
    reads, which carries the column the test claims unique."""

    project = _project()
    manifest = _document(project, "target/manifest.json")
    run = _document(project, "target/run_results.json")
    assert {r["unique_id"] for r in run["results"]} <= set(manifest["nodes"])
    _, results, *_ = _history()
    index = {name: i for i, (name, _) in enumerate(results.columns)}
    assert {row[index["test_unique_id"]] for row in results.rows} <= set(
        manifest["nodes"]
    )

    test = manifest["nodes"][FAILING_TEST]
    assert test["test_metadata"]["name"] == "unique"
    assert test["test_metadata"]["kwargs"]["column_name"] == "order_item_id"
    model = manifest["nodes"][test["attached_node"]]
    [source] = model["depends_on"]["nodes"]
    assert manifest["sources"][source]["identifier"] == "order_items"
    assert manifest["sources"][source]["schema"] == "main"
    sql = dict(project.files)["models/staging/stg_order_items.sql"]
    assert "order_item_id" in sql and "source('shop', 'order_items')" in sql
    # Where the manifest says the package builds its tables is where they are.
    package = manifest["nodes"]["model.elementary.dbt_invocations"]
    assert package["schema"] == seeded.SCHEMA


# --- the seeded tables are the reader's recorded layout ----------------------------


def _differences(columns: Callable[[str], tuple[tuple[str, str], ...]]) -> list[str]:
    """Each seeded table whose columns are not, column for column, the
    recorded layout's, with the type the package casts each one to on DuckDB."""

    from exmergo_dex_core.maintain.elementary_layouts import layout_for

    layout = layout_for(seeded.RELEASE)
    return [
        table
        for table in layout.tables
        if table not in seeded.NOT_SEEDED
        and columns(table)
        != tuple(
            (name, DUCKDB_TYPES[kind]) for name, kind in layout.tables[table].columns
        )
    ]


def _layout_check(path: Path):
    from exmergo_dex_core.adapters.duckdb import DuckDBAdapter
    from exmergo_dex_core.maintain.elementary_layouts import (
        adapter_catalog,
        check_layouts,
        layout_for,
    )

    adapter = DuckDBAdapter(path)
    try:
        catalog = adapter_catalog(adapter, f"{path.stem}.{seeded.SCHEMA}")
        tables = [
            t for t in layout_for(seeded.RELEASE).tables if t not in seeded.NOT_SEEDED
        ]
        return check_layouts(seeded.RELEASE, catalog, tables=tables)
    finally:
        adapter.close()


def test_the_seeded_tables_are_the_recorded_layout_column_for_column():
    """Every column the recorded layout lists, in its order, with its DuckDB
    type, and nothing else: a column renamed, dropped, added or retyped in the
    seed fails here, including one the reader does not read."""

    from exmergo_dex_core.maintain.elementary_layouts import layout_for

    assert _differences(seeded._columns) == []
    layout = layout_for(seeded.RELEASE)
    assert {t.name: t.columns for t in _history()} == {
        table: seeded._columns(table)
        for table in layout.tables
        if table not in seeded.NOT_SEEDED
    }


def test_the_demo_s_tables_pass_the_reader_s_layout_check(tmp_path: Path, capsys):
    pytest.importorskip("duckdb")
    _run(["--repo-root", str(tmp_path), "demo"], capsys)
    checked = _layout_check(tmp_path / DEMO_FILENAME)
    assert checked.release == seeded.RELEASE
    assert {t.table for t in checked.tables} == {
        "dbt_invocations",
        "elementary_test_results",
        "test_result_rows",
        "data_monitoring_metrics",
    }


def test_a_seed_with_one_column_renamed_fails_the_comparison_and_the_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
):
    """`status` renamed in the seeded results: the column-for-column comparison
    names the table, and the reader's own check refuses the demo it builds."""

    pytest.importorskip("duckdb")
    from exmergo_dex_core.maintain.elementary_layouts import ElementaryLayoutError

    real = seeded._columns

    def renamed(table: str) -> tuple[tuple[str, str], ...]:
        return tuple(
            ("test_status" if name == "status" else name, kind)
            for name, kind in real(table)
        )

    assert _differences(renamed) == ["elementary_test_results"]
    monkeypatch.setattr(seeded, "_columns", renamed)
    monkeypatch.setattr(
        seeded,
        "_row",
        lambda table, values: tuple(
            values.get("status" if name == "test_status" else name)
            for name, _ in renamed(table)
        ),
    )
    _run(["--repo-root", str(tmp_path), "demo"], capsys)
    with pytest.raises(ElementaryLayoutError):
        _layout_check(tmp_path / DEMO_FILENAME)


# --- the recorded release ----------------------------------------------------------


def _runtime(root: Path):
    from exmergo_dex_core.transform.elementary import read

    project = root / PROJECT_DIR
    manifest = json.loads(
        (project / "target" / "manifest.json").read_text(encoding="utf-8")
    )
    return read(project, manifest)


def test_the_resolver_reads_the_demo_s_recorded_release(tmp_path: Path, capsys):
    pytest.importorskip("duckdb")
    _run(["--repo-root", str(tmp_path), "demo"], capsys)
    from exmergo_dex_core.transform.elementary_install import INSTALL_RELEASE, detect

    found = detect(tmp_path / PROJECT_DIR)
    assert found["declared"] and found["declared_in"] == "packages.yml"
    # Declared at the release it records: the newest the reader supports.
    assert found["declared_version"] == found["release"] == INSTALL_RELEASE
    runtime = _runtime(tmp_path)
    assert runtime.installed and runtime.enabled
    assert runtime.release == seeded.RELEASE
    assert runtime.release_status == "supported"
    assert runtime.uploads.get("invocations") is not False
    assert runtime.uploads.get("test_results") is not False


def test_with_the_recorded_file_removed_the_resolver_reads_release_unknown(
    tmp_path: Path, capsys
):
    pytest.importorskip("duckdb")
    from exmergo_dex_core.transform.elementary import RELEASE_REASON_CODES

    _run(["--repo-root", str(tmp_path), "demo"], capsys)
    record = tmp_path / PROJECT_DIR / "dbt_packages" / "elementary" / "dbt_project.yml"
    record.unlink()
    runtime = _runtime(tmp_path)
    assert runtime.release is None
    assert RELEASE_REASON_CODES[runtime.release_status] == "elementary_release_unknown"


# --- create-only --------------------------------------------------------------------


def test_an_existing_project_directory_is_refused_before_anything_is_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
):
    """Refused before the warehouse is even generated, not removed after: a
    refused call has written nothing at all."""

    pytest.importorskip("duckdb")

    def never(*_args, **_kwargs):
        raise AssertionError("the warehouse was generated before the refusal")

    monkeypatch.setattr(demo_commands, "generate_demo_warehouse", never)
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


def test_an_interrupted_project_write_leaves_nothing_behind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """An interrupt part way through the project write is cleaned up like a
    failure: a config and warehouse left behind would point at a project that
    is not there, and the next run would refuse the warehouse."""

    pytest.importorskip("duckdb")
    opened: list[Path] = []
    real_open = Path.open

    def interrupted_open(self: Path, mode: str = "r", *args, **kwargs):
        if mode == "x":
            opened.append(self)
            if len(opened) == 3:
                raise KeyboardInterrupt
        return real_open(self, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", interrupted_open)
    with pytest.raises(KeyboardInterrupt):
        main(["--repo-root", str(tmp_path), "demo"])
    # The third exclusive open is inside the project write: the config was
    # created first, so the interrupt had a config and a warehouse to undo.
    assert opened[0].name == "config.yml"
    assert PROJECT_DIR in opened[2].as_posix()
    assert list(tmp_path.rglob("*")) == []


def test_a_failed_write_keeps_a_dex_directory_that_was_there_before(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
):
    """The rollback removes the .dex/ directory only where this run made it:
    one that was there before, even empty, stays."""

    pytest.importorskip("duckdb")
    (tmp_path / ".dex").mkdir()
    real_open = Path.open

    def failing_open(self: Path, mode: str = "r", *args, **kwargs):
        if mode == "x" and PROJECT_DIR in self.as_posix():
            raise OSError("disk full")
        return real_open(self, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", failing_open)
    payload = _run(["--repo-root", str(tmp_path), "demo"], capsys)

    assert payload["status"] == "error"
    assert [p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*")] == [".dex"]


def test_a_config_that_appears_while_the_demo_runs_is_left_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
):
    """Two demos in one directory can both find no config. The one that loses
    the race refuses rather than replacing the other's config, and removes only
    what it made itself: its warehouse goes, the other's config stays."""

    pytest.importorskip("duckdb")
    from exmergo_dex_core.demo import commands

    theirs = tmp_path / ".dex" / "config.yml"
    theirs.parent.mkdir()
    theirs.write_text("connector: duckdb\nduckdb:\n  path: theirs.duckdb\n")
    before = theirs.read_bytes()
    # The other run wrote its config after this one looked for one.
    monkeypatch.setattr(commands, "resolve_dex_root", lambda _root: None)

    payload = _run(["--repo-root", str(tmp_path), "demo"], capsys)

    assert payload["status"] == "error"
    assert payload["reason"] == "guard"
    assert "appeared while dex demo ran" in payload["errors"][0]
    assert theirs.read_bytes() == before
    assert sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*")) == [
        ".dex",
        ".dex/config.yml",
    ]


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

    write_project(tmp_path, _project())
    freshness = manifest_freshness(load(tmp_path / PROJECT_DIR))
    assert freshness["manifest_present"] is True
    assert freshness["stale"] is False


def test_the_sources_are_dated_before_the_oldest_run(tmp_path: Path):
    """Before every seeded build, not only the newest, so no build in the
    history reads a source written after it ran."""

    project = _project()
    runs = _runs()
    assert project.sources_written_at < min(run.started_at for run in runs)
    write_project(tmp_path, project)
    for relative, _ in project.sources:
        mtime = (tmp_path / PROJECT_DIR / relative).stat().st_mtime
        assert mtime == project.sources_written_at.timestamp(), relative
        assert mtime < runs[0].started_at.timestamp(), relative


def test_a_sub_second_generation_time_dates_the_runs_to_the_second():
    """The runs are dated to the whole second, so a demo generated at any
    moment within one second seeds the same history, and every time the
    artifacts and tables print carries no fraction of the clock."""

    later = REFERENCE + timedelta(microseconds=654321)
    assert _runs(later) == _runs()
    assert all(
        run.started_at.microsecond == 0 and run.completed_at.microsecond == 0
        for run in _runs(later)
    )
    assert project_digest(_project(later)) == GOLDEN_PROJECT_DIGEST


def test_write_project_refuses_a_directory_that_appeared(tmp_path: Path):
    (tmp_path / DEMO_DIR).mkdir()
    from exmergo_dex_core.demo import DemoTargetExistsError

    with pytest.raises(DemoTargetExistsError):
        write_project(tmp_path, _project())


def test_a_directory_that_appears_after_the_check_is_refused_and_kept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """`demo_project/` made by someone else between the collision check and the
    write is refused as a guard, not as a crash, and kept whole: the demo's
    directory is created exclusively, so the rollback never reaches one it did
    not make."""

    from exmergo_dex_core.demo import DemoTargetExistsError
    from exmergo_dex_core.demo import project as project_module

    theirs = tmp_path / DEMO_DIR
    real = project_module.project_collision
    calls: list[Path] = []

    def appears_after_the_check(root: Path):
        calls.append(root)
        if len(calls) == 1:
            # The check sees nothing; the directory lands just after it.
            theirs.mkdir()
            (theirs / "keep.txt").write_text("mine", encoding="utf-8")
            return None
        return real(root)

    monkeypatch.setattr(project_module, "project_collision", appears_after_the_check)

    with pytest.raises(DemoTargetExistsError, match="never opens or replaces"):
        write_project(tmp_path, _project())

    assert len(calls) == 2, "refused by the second look, after the mkdir failed"
    assert sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*")) == [
        DEMO_DIR,
        f"{DEMO_DIR}/keep.txt",
    ]
    assert (theirs / "keep.txt").read_text(encoding="utf-8") == "mine"


def test_the_project_files_carry_no_carriage_return_on_any_platform(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The files are written with `\\n` line endings everywhere, so the pinned
    digest holds on Windows too. The platform default is made `\\r\\n` here,
    as it is on Windows, so the check means the same on every platform."""

    real_open = Path.open

    def windows_default(self: Path, mode: str = "r", *args, **kwargs):
        if "w" in mode or "x" in mode:
            kwargs.setdefault("newline", "\r\n")
        return real_open(self, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", windows_default)
    written = write_project(tmp_path, _project())

    assert written
    for relative in written:
        assert b"\r" not in (tmp_path / relative).read_bytes(), relative


def test_a_dangling_link_where_the_project_goes_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys, symlinks
):
    """A link at `demo_project` whose target is gone still names a path the
    demo would write through, so it is a collision like a directory, refused
    before anything is written, and left as it is."""

    pytest.importorskip("duckdb")
    from exmergo_dex_core.demo import commands

    def never(*_args, **_kwargs):
        raise AssertionError("the warehouse was generated before the refusal")

    monkeypatch.setattr(commands, "generate_demo_warehouse", never)
    link = tmp_path / DEMO_DIR
    link.symlink_to(tmp_path / "gone", target_is_directory=True)

    payload = _run(["--repo-root", str(tmp_path), "demo"], capsys)

    assert payload["status"] == "error"
    assert payload["reason"] == "guard"
    assert "never opens or replaces" in payload["errors"][0]
    assert link.is_symlink() and not link.exists()
    assert [p.name for p in tmp_path.iterdir()] == [DEMO_DIR]


def test_a_config_write_that_fails_part_way_removes_the_file_it_opened(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """`_create_config` leaves no torn config, and no record of one, so the
    caller's rollback has nothing of its to find."""

    from exmergo_dex_core.config import DexConfig, DuckDBTarget
    from exmergo_dex_core.demo.commands import _create_config

    class Torn:
        def __init__(self, handle) -> None:
            self._handle = handle

        def __enter__(self):
            return self

        def __exit__(self, *_exc) -> bool:
            self._handle.close()
            return False

        def write(self, text: str) -> int:
            self._handle.write(text[:8])
            raise OSError("disk full")

    real_open = Path.open

    def torn_open(self: Path, mode: str = "r", *args, **kwargs):
        handle = real_open(self, mode, *args, **kwargs)
        return Torn(handle) if mode == "x" else handle

    monkeypatch.setattr(Path, "open", torn_open)
    path = tmp_path / ".dex" / "config.yml"
    made: list[Path] = []

    with pytest.raises(OSError, match="disk full"):
        _create_config(
            DexConfig(connector="duckdb", duckdb=DuckDBTarget(path="x.duckdb")),
            path,
            made,
        )

    assert not path.exists()
    assert made == []


def test_an_interrupt_as_the_config_is_written_still_removes_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The config is recorded for the rollback when it is opened, not after
    `_create_config` returns, so an interrupt landing as it returns still
    finds it to remove, with the warehouse."""

    pytest.importorskip("duckdb")
    from exmergo_dex_core.demo import commands

    real = commands._create_config

    def interrupted_on_return(*args, **kwargs):
        real(*args, **kwargs)
        raise KeyboardInterrupt

    monkeypatch.setattr(commands, "_create_config", interrupted_on_return)
    with pytest.raises(KeyboardInterrupt):
        main(["--repo-root", str(tmp_path), "demo"])
    assert list(tmp_path.rglob("*")) == []


def test_an_interrupt_while_the_project_is_rendered_removes_the_warehouse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Rendering the project runs after the warehouse is generated, inside the
    guarded block, so an interrupt there leaves no warehouse for the next run
    to refuse."""

    pytest.importorskip("duckdb")
    from exmergo_dex_core.demo import commands

    def interrupted(*_args, **_kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(commands, "build_project", interrupted)
    with pytest.raises(KeyboardInterrupt):
        main(["--repo-root", str(tmp_path), "demo"])
    assert list(tmp_path.rglob("*")) == []


# --- acceptance: dex demo, then dex maintain incidents ----------------------------


def test_the_demo_then_maintain_incidents_returns_the_incident_from_elementary(
    run_from, no_network, capsys
):
    pytest.importorskip("duckdb")
    demo = _run(run_from.argv("demo"), capsys)
    data = _run(_printed(demo, "maintain incidents"), capsys)["data"]

    [incident] = data["incidents"]
    assert incident["subject"] == {"kind": "unique_id", "key": FAILING_TEST}
    assert incident["state"] == "open"
    assert incident["opening_source"] == "elementary"
    # First observed in the first failing build, a week before the newest.
    assert incident["first_observed"]["run"] == seeded.invocation_id(1)
    assert incident["first_observed"]["source"] == "elementary"
    # The break runs back to the last pass, across the week the hooks were off.
    assert incident["onset"]["after"]["run"] == seeded.invocation_id(3)
    assert incident["onset"]["missed_runs"] == 1
    assert "missed_run" in incident["onset"]["reasons"]
    elementary = data["sources"]["elementary"]
    assert elementary["status"] == "read"
    assert elementary["release"] == seeded.RELEASE
    assert elementary["measure"]["invocations"] == 6
    # dbt's run results hold the newest build, which Elementary placed.
    assert data["sources"]["dbt_run_results"]["placed"] == "in_window"
    assert data["window"]["last"]["invocation_id"] == BUILD_INVOCATION_ID


def test_the_same_demo_with_the_failing_runs_removed_returns_no_incident(
    run_from, no_network, monkeypatch: pytest.MonkeyPatch, capsys
):
    """The seed with its two failing builds left out: Elementary and dbt both
    read, and neither holds a failure."""

    pytest.importorskip("duckdb")
    kept = tuple(run for run in seeded._SCHEDULE if run[2] != "fail")
    monkeypatch.setattr(
        demo_commands, "seeded_runs", lambda at: seeded.seeded_runs(at, kept)
    )
    demo = _run(run_from.argv("demo"), capsys)
    data = _run(_printed(demo, "maintain incidents"), capsys)["data"]

    assert data["incidents"] == []
    elementary = data["sources"]["elementary"]
    assert elementary["status"] == "read"
    # The four passing builds were read; the hooks-off week has no row.
    assert elementary["measure"]["invocations"] == 4
    # The newest build is now the hooks-off week, which Elementary holds no
    # row of, so dbt's run is placed after its window.
    dbt = data["sources"]["dbt_run_results"]
    assert dbt["status"] == "read"
    assert dbt["placed"] == "after_window"


def test_without_the_package_dbt_s_run_results_alone_return_it_with_no_start(
    run_from, no_network, capsys
):
    """Part 1's arm, kept: with the package's declaration and record removed,
    dbt's run results are the only source, the incident has no known start,
    and with the failing result removed too there is none."""

    pytest.importorskip("duckdb")
    demo = _run(run_from.argv("demo"), capsys)
    project = run_from.root / PROJECT_DIR
    (project / "packages.yml").unlink()
    shutil.rmtree(project / "dbt_packages")

    data = _run(_printed(demo, "maintain incidents"), capsys)["data"]
    [incident] = data["incidents"]
    assert incident["opening_source"] == "dbt_run_results"
    assert incident["first_observed"] == "unknown"
    assert data["sources"]["elementary"]["reason_code"] == "not_installed"

    path = project / "target" / "run_results.json"
    run = json.loads(path.read_text(encoding="utf-8"))
    run["results"] = [r for r in run["results"] if r["unique_id"] != FAILING_TEST]
    path.write_text(json.dumps(run), encoding="utf-8")
    data = _run(_printed(demo, "maintain incidents"), capsys)["data"]
    assert data["incidents"] == []
    assert data["sources"]["dbt_run_results"]["status"] == "read"


def test_diagnosis_names_the_batch_loaded_twice_as_the_cause(
    run_from, no_network, capsys
):
    """The demo-level check #1475 left to this issue: the cause diagnosis
    names is the defect the warehouse plants, measured on the source the
    failing test's model reads, over the break Elementary's history bounds."""

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
    assert diagnosis["range"]["after"]["run"] == seeded.invocation_id(3)
    assert diagnosis["range"]["through"]["run"] == seeded.invocation_id(1)
    assert diagnosis["range"]["missed_runs"] == 1
    [window] = diagnosis["unseen"]
    assert window["reason"] == "no_snapshot_series"
    [step] = diagnosis["next_steps"]
    assert step["command"].startswith("dex transform plan ")


def test_diagnosis_names_the_semantic_model_keyed_on_the_failing_column(
    run_from, no_network, capsys
):
    """#1478: beside the cause, the diagnosis names who reads what broke. The
    demo's ``order_items`` semantic model is keyed on the failing test's column,
    so its entity is named; ``orders`` and its metrics read no column of
    ``stg_order_items`` and are not."""

    pytest.importorskip("duckdb")
    demo = _run(run_from.argv("demo"), capsys)
    data = _run(_printed(demo, "maintain diagnose"), capsys)["data"]

    [diagnosis] = data["diagnoses"]
    readers = diagnosis["semantic_readers"]
    assert readers["status"] == "read"
    assert readers["tested"] == [
        {
            "test": FAILING_TEST,
            "model": "stg_order_items",
            "columns": ["order_item_id"],
            "version": None,
        }
    ]
    assert readers["readers"] == [
        {
            "semantic_model": "order_items",
            "model": "stg_order_items",
            "kind": "entity",
            "name": "order_item",
            "column": "order_item_id",
            "metrics": [],
        }
    ]
    assert readers["unresolved"] == []
    assert readers["unread_files"] == []


def test_the_demo_s_semantic_layer_is_listed_from_its_seeded_artifacts(
    run_from, no_network, capsys
):
    """``explore semantic list --local`` reads the compiled semantic manifest
    a parse leaves in ``target/``, which the demo seeds with its build."""

    _run(run_from.argv("demo"), capsys)
    payload = _run(run_from.argv("explore", "semantic", "list", "--local"), capsys)

    assert payload["status"] == "ok", payload["errors"]
    data = payload["data"]
    assert [m["name"] for m in data["semantic_models"]] == ["order_items", "orders"]
    assert sorted(m["name"] for m in data["metrics"]) == [
        "average_order_value",
        "orders",
        "revenue",
    ]


# --- the seeded manifest is the one dbt would write -------------------------------


def test_dbt_parses_the_project_into_the_nodes_the_seeded_manifest_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The test ids carry hashes dbt derives, and the incident is keyed by
    them, so a real parse of the generated project has to name the same nodes,
    each reading the same parents: otherwise the first real build would read
    as a different test from the one the seeded runs failed. The recorded
    package parses as an installed package with no models of its own."""

    pytest.importorskip("dbt.cli.main")
    pytest.importorskip("dbt.adapters.duckdb")
    from dbt.cli.main import dbtRunner

    for name in ("DBT_TARGET_PATH", "DBT_ENGINE_TARGET_PATH", "DBT_PROFILES_DIR"):
        monkeypatch.delenv(name, raising=False)
    project = _project()
    base = tmp_path / PROJECT_DIR
    for relative, text in project.files:
        if relative.startswith("target/"):
            continue
        (base / relative).parent.mkdir(parents=True, exist_ok=True)
        (base / relative).write_text(text, encoding="utf-8")
    monkeypatch.chdir(base)
    result = dbtRunner().invoke(
        ["parse", "--project-dir", str(base), "--profiles-dir", str(base)]
    )
    assert result.success, result.exception

    parsed = json.loads((base / "target" / "manifest.json").read_text("utf-8"))
    seeded_manifest = _document(project, "target/manifest.json")

    def lineage(manifest: dict) -> dict[str, list[str]]:
        return {
            uid: sorted(node["depends_on"]["nodes"])
            for uid, node in manifest["nodes"].items()
            if node["package_name"] == "analytics"
        }

    assert lineage(parsed) == lineage(seeded_manifest)
    assert set(parsed["sources"]) == set(seeded_manifest["sources"])
    # The semantic layer (#1478): what MetricFlow reads is the seeded file,
    # byte for byte once parsed, and the manifest's entries match dbt's own.
    # Strict on purpose, against the dbt uv.lock pins (DBT_VERSION): a bump
    # that changes what dbt writes fails here first. Update _DSI_VERSION and
    # the entries in demo/project.py to the new release, then the digest.
    semantic = (base / "target" / "semantic_manifest.json").read_text("utf-8")
    assert json.loads(semantic) == _document(project, "target/semantic_manifest.json")

    def entries(manifest: dict, section: str) -> dict[str, dict]:
        return {
            uid: {
                key: value.replace("\\", "/") if key.endswith("path") else value
                for key, value in entry.items()
                if key != "created_at"
            }
            for uid, entry in manifest[section].items()
        }

    for section in ("semantic_models", "metrics"):
        assert entries(parsed, section) == entries(seeded_manifest, section), section
    for map_name in ("parent_map", "child_map"):
        for uid, held in parsed[map_name].items():
            if uid.startswith(("semantic_model.", "metric.", "model.analytics.")):
                assert sorted(held) == sorted(seeded_manifest[map_name][uid]), uid
    spine = "model.analytics.metricflow_time_spine"
    assert (
        parsed["nodes"][spine]["time_spine"]
        == seeded_manifest["nodes"][spine]["time_spine"]
    )
    for uid in lineage(seeded_manifest):
        for field in ("database", "schema", "name", "test_metadata", "attached_node"):
            assert parsed["nodes"][uid].get(field) == seeded_manifest["nodes"][uid].get(
                field
            ), (uid, field)
