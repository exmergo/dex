"""A project format that is not dbt, driven all the way through `maintain`.

This is the check the conformance suite cannot make. Conformance proves a format
is *callable*: dex can ask it for layers and what comes back has the right shape.
It cannot prove dex *calls* it, and for three releases the answer was that dex did
not, because every maintain command reached `dbt_project.load` directly. So the
format here is deliberately one with no files, no paths, and no repository, named
in configuration exactly as a third party would name theirs, and everything below
asserts on what a real command actually returned.

The warehouse is the shared DuckDB baseline repo, which still has a dbt project in
it. That is on purpose: the project on disk is what dex would have read if the
seam were not load-bearing, so any assertion that comes back describing the
in-memory graph instead is describing the seam.
"""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest

from exmergo_dex_core.dbt_project import (
    ApplyResult,
    Conflict,
    ProjectDefinitions,
    SourceFile,
    content_hash,
)
from exmergo_dex_core.maintain.snapshot import (
    SemanticLayer,
    SourceTable,
    TransformLayer,
)
from exmergo_dex_core.transform.plans import EditKind

_NOTE = "models are assets in an orchestrated graph, so no file backs a definition"


class GraphProject:
    """Tier 2 and not tier 3: a reduction of a running graph.

    Declares one source that is not in the warehouse (`raw.shipments`), which is
    what gives `maintain schema` something to find without touching a table.
    """

    name = "graph"

    def __init__(self, context) -> None:
        self.context = context

    def definitions(self) -> ProjectDefinitions:
        return ProjectDefinitions(
            present=True, built_relation_names=["orders"], notes=[_NOTE]
        )

    def transform_layer(self) -> TransformLayer:
        return TransformLayer(
            models=["orders"],
            sources=[SourceTable(source_name="raw", table="shipments")],
            notes=[_NOTE],
        )

    def semantic_layer(self) -> SemanticLayer:
        return SemanticLayer(notes=[_NOTE])


@pytest.fixture
def graph_repo(maintain_repo, monkeypatch):
    """The baseline repo, reconfigured to read the graph format instead of dbt."""

    module = types.ModuleType("dex_graph_format")
    module.graph_project = GraphProject
    monkeypatch.setitem(sys.modules, "dex_graph_format", module)

    config = maintain_repo.root / ".dex" / "config.yml"
    config.write_text(
        config.read_text(encoding="utf-8")
        + "project:\n  format: dex_graph_format:graph_project\n",
        encoding="utf-8",
    )
    return maintain_repo


def test_a_named_non_dbt_format_becomes_the_drift_baseline(graph_repo):
    """The snapshot pins the format's layers, and says what it could not supply.

    `file_count` is zero beside a model, which is a shape nobody can interpret
    without the note, and the note is the format's own words rather than dex
    guessing at why.
    """

    payload = graph_repo.snapshot()

    assert payload["data"]["transform_layer"]["model_count"] == 1
    assert payload["data"]["transform_layer"]["file_count"] == 0
    assert _NOTE in payload["warnings"]


def test_the_format_produces_a_drift_report_not_just_a_green_contract(graph_repo):
    """A finding traceable to the graph rather than to the dbt project on disk.

    `raw.shipments` exists only in the format's transform layer, and the dbt
    project in this repo declares `main.orders` instead, so a `dangling_source`
    naming `raw.shipments` can only have come through the seam.
    """

    graph_repo.snapshot()

    rc, payload = graph_repo.dex("maintain", "schema")

    assert rc == 0 and payload["status"] == "ok"
    dangling = [
        f for f in payload["data"]["findings"] if f["code"] == "dangling_source"
    ]
    assert [f["identifier"] for f in dangling] == ["raw.shipments"]
    assert dangling[0]["severity"] == "high"
    # No path was supplied, so the finding omits the key rather than sending an
    # analyst to a file that is not there.
    assert "declared_in" not in dangling[0].get("data", {})
    assert _NOTE in payload["warnings"]


def test_declining_the_write_tier_makes_reconcile_advisory_by_declaration(graph_repo):
    """The proposal-only path, reached because the format said so.

    Reconcile still surfaces every finding: declining the write tier removes
    dex's authority to author an edit, not the operator's need to see the drift.
    """

    graph_repo.snapshot()
    graph_repo.dex("maintain", "schema")

    rc, payload = graph_repo.dex("maintain", "reconcile")

    assert rc == 0 and payload["status"] == "ok"
    assert payload["data"]["proposals"]
    assert {p["kind"] for p in payload["data"]["proposals"]} == {"advisory"}
    assert payload["data"]["mechanical_count"] == 0
    assert payload.get("diffs") in (None, [])
    assert not (graph_repo.root / ".dex" / "plans").exists()
    assert any(
        "'graph' project format does not implement the write tier" in w
        for w in payload["warnings"]
    )


def test_the_flag_falls_back_to_dbt_for_one_command(graph_repo):
    """`--project-format dbt` reads the project on disk, leaving config alone.

    The same escape hatch `--cache-backend` provides, and the useful direction is
    this one: back to the shipped format for a single command, without editing a
    committed file.
    """

    rc, payload = graph_repo.dex("--project-format", "dbt", "maintain", "snapshot")

    assert rc == 0 and payload["status"] == "ok"
    assert payload["data"]["transform_layer"]["file_count"] > 0
    assert _NOTE not in payload["warnings"]


_SIDECAR = "declarations/orders.yml"

# The model is named `orders`, not `stg_orders`. The earlier version of this
# fixture used dbt's prefix because reconcile matched it inside the YAML, so a
# correctly placed file was still missed on its contents. Naming it in the
# format's own vocabulary is what pins that the convention no longer leaks
# through either half.
_SIDECAR_CONTENT = (
    "version: 2\n"
    "models:\n"
    "  - name: orders\n"
    "    columns:\n"
    "      - name: order_id\n"
    "        tests: [not_null]\n"
)


class SidecarView:
    """The half of a graph-derived project that is a file someone wrote."""

    def __init__(self, root: str, files: dict[str, SourceFile]) -> None:
        self.root = root
        self.files = files


class SidecarGraphProject(GraphProject):
    """Tier 3 for one channel only, which is the shape #258 is about.

    The models are a reduction of a running graph and cannot receive an authored
    staging model. The declared keys live in a hand-written YAML sidecar that
    nothing regenerates, and that file can receive a `unique` test. Answering
    `None` for one kind and a path for the other is the whole point of the seam:
    a single boolean here would have to be wrong about one of the two.

    It reads and writes its sidecar for real, because the assertions below follow
    a proposal through `transform apply` and a stub write path would prove only
    that dex called something.
    """

    name = "sidecar"

    def __init__(self, context) -> None:
        super().__init__(context)
        self.written: list = []

    def _root(self) -> Path:
        return Path(self.context.repo_root or ".")

    def load(self) -> SidecarView:
        root = self._root()
        files = {}
        for path in sorted(root.glob("declarations/*.yml")):
            content = path.read_text(encoding="utf-8")
            files[f"declarations/{path.name}"] = SourceFile(
                path=f"declarations/{path.name}",
                content=content,
                sha256=content_hash(content),
            )
        return SidecarView(str(root), files)

    def edit_path(self, kind: EditKind, model: str) -> str | None:
        if kind is EditKind.SCHEMA_YML:
            return f"declarations/{model}.yml"
        return None

    def editing_surface(self) -> list[str]:
        return ["declarations"]

    def write_edits(self, edits, project_dir=None, *, confirmed: bool = False):
        self.written.append((edits, project_dir, confirmed))
        root = Path(project_dir) if project_dir else self._root()
        conflicts, staged = [], []
        for edit in edits:
            target = root / edit.path
            current = target.read_text(encoding="utf-8") if target.is_file() else None
            found = content_hash(current) if current is not None else None
            if found != edit.old_content_hash:
                conflicts.append(
                    Conflict(
                        path=edit.path,
                        expected_sha256=edit.old_content_hash,
                        found_sha256=found,
                    )
                )
            staged.append((target, edit))
        if conflicts and not confirmed:
            return ApplyResult(written=[], diffs=[], conflicts=conflicts)
        for target, edit in staged:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(edit.new_content, encoding="utf-8")
        return ApplyResult(
            written=[e.path for _, e in staged], diffs=[], conflicts=conflicts
        )


@pytest.fixture
def sidecar_repo(maintain_repo, monkeypatch):
    module = types.ModuleType("dex_sidecar_format")
    module.sidecar_project = SidecarGraphProject
    monkeypatch.setitem(sys.modules, "dex_sidecar_format", module)

    config = maintain_repo.root / ".dex" / "config.yml"
    config.write_text(
        config.read_text(encoding="utf-8")
        + "project:\n  format: dex_sidecar_format:sidecar_project\n",
        encoding="utf-8",
    )
    sidecar = maintain_repo.root / _SIDECAR
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    sidecar.write_text(_SIDECAR_CONTENT, encoding="utf-8")
    return maintain_repo


def _grain_drift(repo):
    repo.snapshot()
    repo.sql("INSERT INTO orders SELECT * FROM orders WHERE order_id <= 10")
    repo.dex("maintain", "grain")


def test_a_placing_format_receives_the_test_edit_in_its_own_sidecar(sidecar_repo):
    """The third gate, open: a format's own surface is what its edits are checked
    against.

    This was a strict xfail through the spike that found the gate. Reconcile
    asked the format where the edit lands and built it against
    `declarations/orders.yml`, and then `plans.plan` refused it, because
    containment validated every path against *dbt's* `model_paths` whatever
    format produced the edit. The format now declares the surface it owns and
    the check reads that declaration.
    """

    _grain_drift(sidecar_repo)

    rc, payload = sidecar_repo.dex("maintain", "reconcile", "grain")

    assert rc == 0 and payload["status"] == "ok", payload.get("errors")
    diff = next((d for d in payload.get("diffs") or [] if d["path"] == _SIDECAR), None)
    assert diff is not None, payload
    assert "unique" in diff["unified"]
    # And nothing was planned against the dbt convention it used to assume.
    assert not any(
        d["path"].startswith("models/staging/") for d in payload.get("diffs") or []
    )


def test_the_edit_is_pinned_to_the_formats_file_not_dbts_view_of_it(sidecar_repo):
    """The diff is the one-line change, not the whole file appearing from nowhere.

    Containment and the hash pin are halves of one question, and widening the
    first without moving the second is a quiet defect rather than a refusal: the
    edit lands in the format's keyspace while `old_content_hash` comes from dbt's
    view, which does not have the file. It pins as a create, the reviewable diff
    shows a hand-written file as brand new, and the apply that follows reports a
    conflict on a file nobody touched.
    """

    _grain_drift(sidecar_repo)

    rc, payload = sidecar_repo.dex("maintain", "reconcile", "grain")

    assert rc == 0, payload.get("errors")
    diff = next(d for d in payload["diffs"] if d["path"] == _SIDECAR)
    # A file the plan believes is absent diffs against /dev/null and renders
    # every line as an addition. This one was read from the format's own view, so
    # it diffs against itself and the untouched declarations survive as context.
    assert "/dev/null" not in diff["unified"], diff["unified"]
    # Context lines exist only against a file the plan knew was there. (The
    # additions are noisier than the change, because the edit round-trips the
    # YAML through `safe_dump`, which is how the scaffolded dbt path renders too.)
    assert " version: 2" in diff["unified"].splitlines()
    assert "unique" in diff["unified"]


def test_the_plan_applies_through_the_formats_own_write_path(sidecar_repo):
    """A plan a format can store and not apply is not a write path.

    `plans.apply` wrote every plan with dbt's module-level writer, which resolves
    each edit under the dbt project and re-hashes what it finds on disk. So the
    plan this format can now store would still have been refused one stage later,
    and the `write_edits` it implemented to reach tier 3 would never have been
    called.
    """

    _grain_drift(sidecar_repo)
    rc, payload = sidecar_repo.dex("maintain", "reconcile", "grain")
    assert rc == 0, payload.get("errors")

    rc, applied = sidecar_repo.dex("transform", "apply", payload["data"]["plan_id"])

    assert rc == 0, applied.get("errors")
    written = (sidecar_repo.root / _SIDECAR).read_text(encoding="utf-8")
    assert "unique" in written
    assert "not_null" in written


def test_the_format_declaring_its_surface_does_not_widen_it(sidecar_repo):
    """Containment stays a safety property; only the authority moved.

    The format says which paths it owns, and dex still refuses anything outside
    them. A format that could name any path would not be declaring a surface, it
    would be turning the check off, and the check is what keeps a mistaken or
    adversarial path from reaching the rest of the repository.
    """

    from exmergo_dex_core.transform.plans import PlanError, contained_key

    surface = SidecarGraphProject.editing_surface(None)

    assert contained_key("declarations/orders.yml", surface)
    for refused in (
        "declarations_backup/orders.yml",  # a sibling sharing the prefix
        "models/staging/stg_orders.yml",  # inside dbt's surface, not this one
        "../outside.yml",  # an escape, never a format's to permit
    ):
        with pytest.raises(PlanError):
            contained_key(refused, surface)


@pytest.fixture
def authored_repo(tmp_path, dex, monkeypatch):
    """The sidecar format alone, with no dbt project sharing its root.

    Deliberately not `sidecar_repo`. That fixture puts `dbt_project.yml` and
    `declarations/` at the same path, so dbt's directory and the format's view
    root coincide and a caller confusing the two still lands on the right file.
    Separating them is what makes the confusion observable, and separated is the
    normal case for a format that is not dbt: it has no reason to be rooted where
    a dbt project happens to sit, or for one to exist at all.
    """

    module = types.ModuleType("dex_sidecar_format")
    module.sidecar_project = SidecarGraphProject
    monkeypatch.setitem(sys.modules, "dex_sidecar_format", module)

    root = tmp_path / "repo"
    (root / ".dex").mkdir(parents=True)
    (root / ".dex" / "config.yml").write_text(
        "project:\n  format: dex_sidecar_format:sidecar_project\n", encoding="utf-8"
    )
    sidecar = root / _SIDECAR
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    sidecar.write_text(_SIDECAR_CONTENT, encoding="utf-8")
    return root


def _authored_edit(tmp_path: Path, content: str) -> Path:
    payload = tmp_path / "edits.json"
    payload.write_text(
        json.dumps(
            {"edits": [{"path": _SIDECAR, "kind": "schema_yml", "content": content}]}
        ),
        encoding="utf-8",
    )
    return payload


_AUTHORED = _SIDECAR_CONTENT.replace("tests: [not_null]", "tests: [not_null, unique]")


def test_an_authored_plan_needs_no_dbt_project_when_the_format_declares_one(
    authored_repo, tmp_path, dex
):
    """`transform plan` reaches a format that brought its own project.

    The agent-authored path asked the engine for dbt's directory unconditionally
    and passed it beside the format, so a repository with no `dbt_project.yml`
    failed while locating a project nothing was going to read. That is the whole
    write surface for an authored edit (`transform plan`, `transform macro`, and
    every `semantic define|update|plan` route through the same call), so a format
    could declare a surface, place an edit into it, and still never be reachable
    except through reconcile.
    """

    rc, payload = dex(
        "--repo-root",
        str(authored_repo),
        "transform",
        "plan",
        "add a unique test",
        "--edits-file",
        str(_authored_edit(tmp_path, _AUTHORED)),
    )

    assert rc == 0 and payload["status"] == "ok", payload.get("errors")
    assert payload["data"]["paths"] == [_SIDECAR]


def test_an_authored_edit_is_pinned_and_applied_where_the_format_put_it(
    authored_repo, tmp_path, dex
):
    """Both halves come from the format, with a dbt project present to be wrong about.

    dbt lives in a subdirectory here, so a plan pinned against dbt's directory
    resolves this edit under `analytics/` on the way out: the file it hashed is
    not the file it writes. That reads as a create against a hand-written file,
    and the apply that follows either conflicts on a file nobody edited or lands
    the write in a tree that never had one.
    """

    (authored_repo / "analytics").mkdir()
    (authored_repo / "analytics" / "dbt_project.yml").write_text(
        "name: dex_test\nversion: '1.0.0'\nprofile: dex_test\n", encoding="utf-8"
    )

    rc, payload = dex(
        "--repo-root",
        str(authored_repo),
        "transform",
        "plan",
        "add a unique test",
        "--edits-file",
        str(_authored_edit(tmp_path, _AUTHORED)),
    )
    assert rc == 0, payload.get("errors")
    # Pinned against the file that is there, so the diff is the one-line change
    # rather than a hand-written file appearing from nowhere.
    diff = next(d for d in payload["diffs"] if d["path"] == _SIDECAR)
    assert "/dev/null" not in diff["unified"], diff["unified"]

    rc, applied = dex(
        "--repo-root",
        str(authored_repo),
        "transform",
        "apply",
        payload["data"]["plan_id"],
    )

    assert rc == 0, applied.get("errors")
    assert not applied["data"].get("conflicts")
    assert applied["data"]["written"] == [_SIDECAR]
    assert "unique" in (authored_repo / _SIDECAR).read_text(encoding="utf-8")
    assert not (authored_repo / "analytics" / _SIDECAR).exists()


def test_a_placing_format_still_declines_the_staging_model_channel(sidecar_repo):
    """`None` for MODEL_SQL keeps the scaffold channel advisory, by declaration.

    Placement decides where, not what. The staging model's content is dbt SQL the
    scaffold generates, so a format that cannot host that content declines the
    kind and reconcile degrades to advice instead of writing dbt into its tree.
    """

    sidecar_repo.snapshot()
    sidecar_repo.dex("maintain", "schema")

    rc, payload = sidecar_repo.dex("maintain", "reconcile", "schema")

    assert rc == 0 and payload["status"] == "ok"
    assert {p["kind"] for p in payload["data"]["proposals"]} == {"advisory"}
    assert not any(d["path"].endswith(".sql") for d in payload.get("diffs") or [])
