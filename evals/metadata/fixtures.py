"""Fixtures and tasks: load them, and materialize a task into a workspace.

On disk, one directory per fixture under ``evals/metadata/fixtures/``::

    <fixture>/fixture.json        {"name", "description", "seed"}
    <fixture>/warehouse.py        tables(seed) -> list[Table]: the raw source data
    <fixture>/project/            the dbt project the agent starts from (its
                                  profiles.yml is generated, see write_profile)
    <fixture>/tasks/<id>/task.json   {"prompt", "kind": "build"|"fix", "models": [...]}
    <fixture>/tasks/<id>/truth.json  the known right answers (see score.py)
    <fixture>/tasks/<id>/setup/      optional overlay: the task's starting state
    <fixture>/tasks/<id>/reference/  a correct answer, overlaid on the start

Fixtures are written for this suite. None is derived from a benchmark task, so
the benchmarks stay usable as an independent check.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"
WAREHOUSE_FILE = "warehouse.duckdb"


@dataclass(frozen=True)
class Table:
    """One raw source table: ``schema.name``, typed columns, and its rows."""

    name: str
    columns: list[tuple[str, str]]
    rows: list[tuple[Any, ...]]


@dataclass(frozen=True)
class Task:
    fixture: str
    id: str
    prompt: str
    kind: str
    models: list[str]
    truth: dict[str, Any]
    directory: Path

    @property
    def key(self) -> str:
        return f"{self.fixture}/{self.id}"


@dataclass(frozen=True)
class Fixture:
    name: str
    description: str
    seed: int
    directory: Path
    tasks: list[Task] = field(default_factory=list)

    @property
    def project(self) -> Path:
        return self.directory / "project"

    def tables(self) -> list[Table]:
        spec = importlib.util.spec_from_file_location(
            f"evals_metadata_fixture_{self.name}", self.directory / "warehouse.py"
        )
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return list(module.tables(self.seed))


def load_fixtures(root: Path = FIXTURES_DIR) -> list[Fixture]:
    fixtures = []
    for directory in sorted(
        p for p in root.iterdir() if (p / "fixture.json").is_file()
    ):
        meta = json.loads((directory / "fixture.json").read_text(encoding="utf-8"))
        tasks = []
        for task_dir in sorted((directory / "tasks").iterdir()):
            if not (task_dir / "task.json").is_file():
                continue
            spec = json.loads((task_dir / "task.json").read_text(encoding="utf-8"))
            truth = json.loads((task_dir / "truth.json").read_text(encoding="utf-8"))
            tasks.append(
                Task(
                    fixture=meta["name"],
                    id=task_dir.name,
                    prompt=spec["prompt"],
                    kind=spec["kind"],
                    models=list(spec["models"]),
                    truth=truth,
                    directory=task_dir,
                )
            )
        fixtures.append(
            Fixture(
                name=meta["name"],
                description=meta["description"],
                seed=int(meta["seed"]),
                directory=directory,
                tasks=tasks,
            )
        )
    return fixtures


def find_tasks(
    fixtures: list[Fixture], *, fixture: list[str] | None, task: list[str] | None
) -> list[Task]:
    selected = []
    for fx in fixtures:
        if fixture and fx.name not in fixture:
            continue
        for t in fx.tasks:
            if task and t.id not in task and t.key not in task:
                continue
            selected.append(t)
    return selected


def fixture_of(task: Task, fixtures: list[Fixture]) -> Fixture:
    return next(fx for fx in fixtures if fx.name == task.fixture)


# --- the warehouse ----------------------------------------------------------


def build_warehouse(path: Path, tables: list[Table]) -> None:
    """Create a DuckDB file holding ``tables``. Create-only, like ``dex demo``."""

    import duckdb

    if path.exists():
        raise FileExistsError(path)
    con = duckdb.connect(str(path))
    try:
        for table in tables:
            schema, _, _name = table.name.rpartition(".")
            if schema:
                con.execute(f"create schema if not exists {schema}")
            cols = ", ".join(f"{name} {typ}" for name, typ in table.columns)
            con.execute(f"create table {table.name} ({cols})")
            if table.rows:
                marks = ", ".join("?" for _ in table.columns)
                con.executemany(
                    f"insert into {table.name} values ({marks})", table.rows
                )
        con.execute("checkpoint")
    finally:
        con.close()


def row_digest(tables: list[Table]) -> str:
    """A digest over every column and cell, so a fixture's data is pinned."""

    h = hashlib.sha256()
    for table in tables:
        h.update(table.name.encode())
        for name, typ in table.columns:
            h.update(f"{name}:{typ}".encode())
        for row in table.rows:
            h.update(repr(row).encode())
    return h.hexdigest()


# --- workspaces -------------------------------------------------------------

_IGNORED = shutil.ignore_patterns("target", "logs", "dbt_packages", "*.duckdb*", ".dex")


def overlay(source: Path, destination: Path) -> None:
    """Copy ``source``'s tree over ``destination``. A file named ``*.delete``
    removes its namesake, so a setup can take a model away."""

    if not source.is_dir():
        return
    for path in sorted(source.rglob("*")):
        if path.is_dir():
            continue
        relative = path.relative_to(source)
        if path.name.endswith(".delete"):
            (destination / relative.with_name(path.name[: -len(".delete")])).unlink(
                missing_ok=True
            )
            continue
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)


def starting_project(task: Task, fixture: Fixture, destination: Path) -> Path:
    """The project as the agent receives it: the fixture plus the task's setup."""

    shutil.copytree(fixture.project, destination, ignore=_IGNORED)
    overlay(task.directory / "setup", destination)
    write_profile(destination)
    return destination


def write_profile(project: Path) -> None:
    """The dev profile, written rather than committed.

    The repository ignores every ``profiles.yml`` so a credential can never be
    committed by accident, and a fixture's profile holds nothing but a local
    DuckDB path, so it is generated from the profile ``dbt_project.yml`` names.
    """

    match = re.search(
        r"^profile:\s*[\"']?([\w-]+)",
        (project / "dbt_project.yml").read_text(encoding="utf-8"),
        re.MULTILINE,
    )
    if match is None:
        raise ValueError(f"{project / 'dbt_project.yml'} names no profile")
    (project / "profiles.yml").write_text(
        f"{match.group(1)}:\n"
        "  target: dev\n"
        "  outputs:\n"
        "    dev:\n"
        "      type: duckdb\n"
        f"      path: {WAREHOUSE_FILE}\n"
        "      schema: main\n"
        "      threads: 1\n",
        encoding="utf-8",
    )


def materialize(
    task: Task,
    fixture: Fixture,
    destination: Path,
    *,
    agent_config: str | None = None,
    reference: bool = False,
) -> Path:
    """Lay a task out as a working repository at ``destination``.

    The warehouse is generated fresh, ``.dex/config.yml`` points Dex at it, and the
    tree is committed as a git baseline (Dex finds its config by walking up to the
    git root, and a reviewer reads the agent's work as a diff). ``agent_config``
    becomes ``CLAUDE.md``: the harness places the block ``dex procedure --block``
    returns, as #503 requires, and nothing else. ``reference`` overlays the task's
    reference answer instead of leaving the work to an agent.
    """

    destination.mkdir(parents=True, exist_ok=False)
    project = starting_project(task, fixture, destination / "project_tmp")
    for item in project.iterdir():
        shutil.move(str(item), destination / item.name)
    project.rmdir()
    build_warehouse(destination / WAREHOUSE_FILE, fixture.tables())
    dex_dir = destination / ".dex"
    dex_dir.mkdir()
    (dex_dir / "config.yml").write_text(
        f"connector: duckdb\nduckdb:\n  path: {WAREHOUSE_FILE}\n", encoding="utf-8"
    )
    (destination / ".gitignore").write_text(
        "target/\nlogs/\ndbt_packages/\n*.duckdb\n*.duckdb.wal\n.dex/cache.json\n"
        ".dex/plans/\n.dex/spend.jsonl\n.user.yml\n",
        encoding="utf-8",
    )
    if agent_config is not None:
        (destination / "CLAUDE.md").write_text(agent_config, encoding="utf-8")
    _git(destination, "init", "-q")
    _git(destination, "add", "-A")
    _git(destination, "commit", "-q", "-m", f"fixture {task.key}")
    if reference:
        overlay(task.directory / "reference", destination)
    return destination


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=dex-evals",
            "-c",
            "user.email=evals@example.invalid",
            "-c",
            "commit.gpgsign=false",
            *args,
        ],
        cwd=cwd,
        check=True,
        capture_output=True,
    )
