"""`transform repair` turns a failing package model into a reviewable plan (#1229).

Real dbt on DuckDB, the package installed from a local git repository as in
`test_package_documented_override.py`. The package's model fails twice over: it
coalesces a timestamp with an all-NULL column the loader typed INTEGER, and it
truncates a column holding epoch seconds. It also calls its own macros
unqualified, reads a relation through a var the project defines to read the
model itself (a cycle once the override is the project's), and hands a
pass-through var to a macro by name. The draft has to settle all of it, convert
only the two columns, compile, bind, build cold, keep every row and pass the
package's own tests.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from exmergo_dex_core.cli import main

duckdb = pytest.importorskip("duckdb")
pytest.importorskip("dbt.adapters.duckdb")
pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")

PACKAGE_MODEL = """\
with base as (
    select {{ task_columns() }} {{ passthrough('pass_through') }}
    from {{ var('task_source') }}
)
select
    id,
    coalesce(created_at, legacy_ts) as created_ts,
    cast({{ dbt.date_trunc('day', 'due_epoch') }} as date) as due_date
from base
"""

MACROS = """\
{% macro task_columns() %}id, created_at, legacy_ts, due_epoch{% endmacro %}
{% macro passthrough(name) %}{% for c in var(name) %}, {{ c }}{% endfor %}{% endmacro %}
"""

SEED = (
    "id,created_at,legacy_ts,due_epoch,label\n"
    "1,2024-01-01 00:00:00,,1704067200,a\n"
    "2,,,1706745600,b\n"
    "3,2024-02-01 00:00:00,,1709251200,c\n"
)


def _git(cwd: Path, *argv: str) -> str:
    """Run git in ``cwd`` with a throwaway identity, returning stdout."""

    return subprocess.run(  # noqa: S603
        ["git", "-c", "user.email=t@example.com", "-c", "user.name=t", *argv],  # noqa: S607
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _make_project(tmp_path: Path) -> Path:
    """A DuckDB project reading a local git package whose model fails twice over."""

    package = tmp_path / "tiny_pkg"
    (package / "models").mkdir(parents=True)
    (package / "macros").mkdir()
    (package / "dbt_project.yml").write_text(
        yaml.safe_dump(
            {
                "name": "tiny_pkg",
                "version": "1.0",
                "config-version": 2,
                "model-paths": ["models"],
                "macro-paths": ["macros"],
                "models": {"tiny_pkg": {"+materialized": "table"}},
                "vars": {
                    "tiny_pkg": {
                        "task_source": "{{ ref('raw_task') }}",
                        "pass_through": [],
                    }
                },
            },
            sort_keys=False,
        )
    )
    (package / "macros" / "cols.sql").write_text(MACROS)
    (package / "models" / "pkg_task.sql").write_text(PACKAGE_MODEL)
    (package / "models" / "pkg_task.yml").write_text(
        "version: 2\nmodels:\n  - name: pkg_task\n    columns:\n"
        "      - name: id\n        tests: [not_null, unique]\n"
    )
    _git(package, "init", "-q", ".")
    _git(package, "add", "-A")
    _git(package, "commit", "-qm", "init")

    root = tmp_path / "proj"
    (root / "models").mkdir(parents=True)
    (root / "seeds").mkdir()
    (root / ".dex").mkdir()
    (root / ".git").mkdir()
    (root / "dbt_project.yml").write_text(
        "name: proj\nversion: '1.0'\nconfig-version: 2\nprofile: proj\n"
        "model-paths: [models]\nseed-paths: [seeds]\n"
        "# The project reads the package's model through the same var name.\n"
        "vars:\n  proj:\n    task_source: \"{{ ref('pkg_task') }}\"\n"
        "seeds:\n  proj:\n    raw_task:\n      +column_types:\n"
        "        {id: integer, created_at: timestamp, legacy_ts: integer,"
        " due_epoch: integer}\n"
    )
    (root / "profiles.yml").write_text(
        "proj:\n  target: dev\n  outputs:\n    dev:\n      type: duckdb\n"
        "      path: ./dev.duckdb\n"
    )
    (root / "packages.yml").write_text(
        yaml.safe_dump(
            {
                "packages": [
                    {
                        "git": f"file://{package}",
                        "revision": _git(package, "rev-parse", "HEAD"),
                    }
                ]
            }
        )
    )
    (root / "seeds" / "raw_task.csv").write_text(SEED)
    (root / "models" / "rpt.sql").write_text(
        "select id, created_ts, due_date from {{ ref('pkg_task') }}\n"
    )
    (root / ".dex" / "config.yml").write_text(
        "connector: duckdb\nduckdb:\n  path: dev.duckdb\n"
    )
    return root


@pytest.fixture
def project(tmp_path, monkeypatch):
    """The project, as the working directory."""

    root = _make_project(tmp_path)
    monkeypatch.chdir(root)
    monkeypatch.setenv("DEX_ALLOW_UNSERIALIZED_LEDGER", "1")
    return root


def _dex(capsys, *argv: str) -> dict:
    """Run the CLI and return its envelope."""

    main(list(argv))
    return json.loads(capsys.readouterr().out)


def _failed_build(capsys) -> dict:
    """Install the package and run the build that fails on it."""

    assert _dex(capsys, "transform", "deps")["status"] == "ok"
    failed = _dex(capsys, "transform", "build", "--target", "dev")
    assert failed["status"] == "error"
    return failed


def test_the_failed_build_points_at_the_repair_ahead_of_any_cut(project, capsys):
    """The blocker and the diagnosis both carry the one command."""

    failed = _failed_build(capsys)
    [blocker] = failed["data"]["completion"]["blockers"]
    assert blocker["next"] == "repair: dex transform repair pkg_task"
    [entry] = failed["data"]["prerequisites"]
    assert entry["next"] == "repair: dex transform repair pkg_task"
    [override] = [o for o in entry["options"] if o["kind"] == "project_override"]
    assert override["repair"] == "dex transform repair pkg_task"
    assert "`dex transform repair pkg_task` drafts it" in override["how"]


def test_an_unprofiled_input_leaves_the_conversion_unresolved(project, capsys):
    """With nothing profiled the draft converts nothing and names the profile."""

    _failed_build(capsys)
    drafted = _dex(capsys, "transform", "repair", "pkg_task")
    assert drafted["status"] == "ok", drafted["errors"]
    summary = drafted["data"]["repair"]
    assert summary["status"] == "unresolved"
    [repair] = summary["nodes"]
    assert repair["status"] == "unresolved"
    [conversion] = repair["conversions"]
    assert conversion["reason_code"] == "unprofiled_input"
    assert conversion["next"].startswith("dex explore profile ")
    assert "raw_task" in conversion["next"]
    override = next(
        d["unified"] for d in drafted["diffs"] if d["path"].endswith("pkg_task.sql")
    )
    # The column stays as the package wrote it: never guessed.
    assert "coalesce(created_at, legacy_ts)" in override
    assert "edit each unresolved conversion" in drafted["data"]["next"]


def test_the_draft_converts_both_columns_builds_cold_and_keeps_every_row(
    project, capsys
):
    """The acceptance path: draft, apply, `--cold-deps --verify`, validated."""

    failed = _failed_build(capsys)
    assert "Cannot mix values of type TIMESTAMP and INTEGER" in json.dumps(failed)
    assert _dex(capsys, "explore", "profile", "main.raw_task")["status"] == "ok"

    drafted = _dex(capsys, "transform", "repair", "pkg_task")
    assert drafted["status"] == "ok", drafted["errors"]
    data = drafted["data"]
    # `repair` leads the payload, right after the plan's identity.
    assert list(data)[:3] == ["plan_id", "intent", "repair"]
    summary = data["repair"]
    assert summary["status"] == "resolved", summary
    assert summary["compiled"] is True
    assert summary["plan_id"] == data["plan_id"]
    [repair] = summary["nodes"]
    assert repair["status"] == "resolved", repair
    assert repair["bind_check"] == {"status": "bound"}
    assert repair["population"] == "kept"
    by_column = {c["column"]: c for c in repair["conversions"]}
    assert set(by_column) == {"legacy_ts", "due_epoch"}
    null_cast = by_column["legacy_ts"]
    assert null_cast["kind"] == "null_cast"
    assert null_cast["meaning_changing"] is False
    assert null_cast["evidence"] == "data"
    assert null_cast["call"] == "coalesce"
    epoch = by_column["due_epoch"]
    assert epoch["kind"] == "epoch_seconds"
    assert epoch["meaning_changing"] is True
    assert epoch["call"] == "date_trunc"
    # The first failure came from the build, the second from binding the draft.
    assert [e["source"] for e in repair["errors"]] == ["build", "bind_check"]
    assert any("changes what the value means" in w for w in drafted["warnings"])
    restated = repair["restated"]
    assert set(restated["macros_qualified"]) == {"task_columns", "passthrough"}
    assert {"var": "task_source", "change": "inlined"} in restated["vars"]
    assert {"var": "pass_through", "change": "project_default"} in restated["vars"]
    assert restated["config"] == ["materialized"]

    override = (project / "models" / "overrides" / "pkg_task.sql").exists()
    assert override is False  # nothing applied by drafting
    plan_id = data["plan_id"]
    assert _dex(capsys, "transform", "apply", plan_id)["status"] == "ok"
    text = (project / "models" / "overrides" / "pkg_task.sql").read_text()
    assert "{{ tiny_pkg.task_columns() }}" in text
    assert "from {{ ref('raw_task') }}" in text
    assert "coalesce(created_at, cast(cast(legacy_ts as varchar) as timestamp))" in text
    assert "epoch_ms(cast(due_epoch as bigint) * 1000)" in text
    config = yaml.safe_load((project / "dbt_project.yml").read_text())
    assert config["models"]["tiny_pkg"]["pkg_task"]["+enabled"] is False
    assert config["vars"]["proj"]["pass_through"] == []
    # Written in place: the project's own comment survives.
    assert (
        "# The project reads the package's model"
        in (project / "dbt_project.yml").read_text()
    )

    built = _dex(
        capsys,
        "transform",
        "build",
        "--target",
        "dev",
        "--cold-deps",
        "--verify",
        "--for-plan",
        plan_id,
    )
    assert built["status"] == "ok", built["errors"]
    completion = built["data"]["completion"]
    assert completion["outcome"] == "validated"
    # Every check held: built, the package's tests, cold, and the row count.
    assert completion["obligations"]["repair"] == [
        {"model": "pkg_task", "kind": "repair", "status": "satisfied"}
    ]
    assert (
        built["data"]["cold_verification"]["installed_edits"]["changed"]["count"] == 0
    )

    with duckdb.connect(str(project / "dev.duckdb"), read_only=True) as con:
        source = con.execute("select count(*) from main.raw_task").fetchone()[0]
        rows = con.execute(
            "select id, created_ts is null, cast(due_date as varchar) "
            "from main.pkg_task order by id"
        ).fetchall()
    assert source == len(rows) == 3
    assert rows == [
        (1, False, "2024-01-01"),
        (2, True, "2024-02-01"),
        (3, False, "2024-03-01"),
    ]


def test_a_plain_build_leaves_the_cold_obligation_open(project, capsys):
    """Only `--cold-deps` shows the repair survives a reinstall."""

    _failed_build(capsys)
    _dex(capsys, "explore", "profile", "main.raw_task")
    plan_id = _dex(capsys, "transform", "repair", "pkg_task")["data"]["plan_id"]
    _dex(capsys, "transform", "apply", plan_id)
    built = _dex(capsys, "transform", "build", "--target", "dev", "--for-plan", plan_id)
    assert built["status"] == "ok", built["errors"]
    obligations = {
        o["kind"]: o for o in built["data"]["completion"]["obligations"]["repair"]
    }
    assert obligations["repair_cold_deps"]["status"] == "unchecked"
    assert obligations["repair_cold_deps"]["reason_code"] == "not_cold"
    assert obligations["repair_row_count"]["reason_code"] == "verification_not_run"


def test_the_repair_refuses_what_it_cannot_draft(project, capsys):
    """No build, a project model, and a model that did not fail are refused."""

    nothing = _dex(capsys, "transform", "repair", "pkg_task")
    assert nothing["status"] == "error"
    assert "run `dex transform build --target dev` first" in nothing["errors"][0]

    _failed_build(capsys)
    seed = _dex(capsys, "transform", "repair", "raw_task")
    assert seed["status"] == "error"
    assert "did not fail" in seed["errors"][0]

    blank = _dex(capsys, "transform", "repair")
    assert blank["status"] == "error"
    assert "needs the failing node" in blank["errors"][0]

    _dex(capsys, "explore", "profile", "main.raw_task")
    plan_id = _dex(capsys, "transform", "repair", "pkg_task")["data"]["plan_id"]
    _dex(capsys, "transform", "apply", plan_id)
    (project / "models" / "overrides" / "pkg_task.sql").write_text(
        "select id, cast(null as timestamp) + 1 as created_ts, 1 as due_date "
        "from {{ ref('raw_task') }}\n"
    )
    _dex(capsys, "transform", "build", "--target", "dev")
    owned = _dex(capsys, "transform", "repair", "pkg_task")
    assert owned["status"] == "error"
    assert "project owns pkg_task" in owned["errors"][0]
