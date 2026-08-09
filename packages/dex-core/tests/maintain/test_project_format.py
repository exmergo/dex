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

import sys
import types

import pytest

from exmergo_dex_core.dbt_project import ProjectDefinitions, SourceFile
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

# The sidecar names its model with dbt's `stg_` prefix. That is not incidental
# and the test below pins why: placement alone does not carry the naming.
_SIDECAR_CONTENT = (
    "version: 2\n"
    "models:\n"
    "  - name: stg_orders\n"
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
    """

    name = "sidecar"

    def __init__(self, context) -> None:
        super().__init__(context)
        self.written: list = []

    def load(self) -> SidecarView:
        root = self.context.repo_root or "."
        return SidecarView(
            root,
            {
                _SIDECAR: SourceFile(
                    path=_SIDECAR, content=_SIDECAR_CONTENT, sha256="unused"
                )
            },
        )

    def edit_path(self, kind: EditKind, model: str) -> str | None:
        if kind is EditKind.SCHEMA_YML:
            return f"declarations/{model}.yml"
        return None

    def write_edits(self, edits, project_dir=None, *, confirmed: bool = False):
        self.written.append((edits, project_dir, confirmed))
        return []


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
    return maintain_repo


def test_the_plan_store_refuses_a_placed_edit_outside_dbts_editing_surface(
    sidecar_repo,
):
    """The third gate, which neither #257 nor #258 describes.

    With the write gate asking a capability and reconcile asking the format
    where the edit goes, the edit is still refused, and by neither of those: at
    plan time `plans.plan` calls `load_project` and validates every path against
    *dbt's* `model_paths`, whatever format produced the edit. So a second format
    naming a file outside `models/` cannot store a plan even once both of the
    filed issues are resolved exactly as proposed.

    Pinned as the current behavior rather than fixed here. The containment check
    is a safety property, not an oversight (writes are confined to a declared
    editing surface), so widening it means the format declaring that surface,
    which is a design question and not this seam's to answer.
    """

    sidecar_repo.snapshot()
    sidecar_repo.sql("INSERT INTO orders SELECT * FROM orders WHERE order_id <= 10")
    sidecar_repo.dex("maintain", "grain")

    rc, payload = sidecar_repo.dex("maintain", "reconcile", "grain")

    assert rc == 1 and payload["status"] == "error"
    assert any(
        "outside the project's model and macro paths" in message
        for message in payload["errors"]
    )


@pytest.mark.xfail(
    strict=True,
    reason="the plan store validates every edit against dbt's model paths; a "
    "format has no way to declare its own editing surface yet",
)
def test_a_placing_format_receives_the_test_edit_in_its_own_sidecar(sidecar_repo):
    """The end state, once the third gate above opens.

    Placement is doing its job by this point: reconcile asked the format and
    built the edit against `declarations/orders.yml` instead of the scaffold
    path. What is missing is permission to store a plan touching it.
    """

    sidecar_repo.snapshot()
    sidecar_repo.sql("INSERT INTO orders SELECT * FROM orders WHERE order_id <= 10")
    sidecar_repo.dex("maintain", "grain")

    rc, payload = sidecar_repo.dex("maintain", "reconcile", "grain")

    assert rc == 0 and payload["status"] == "ok", payload.get("errors")
    diff = next((d for d in payload.get("diffs") or [] if d["path"] == _SIDECAR), None)
    assert diff is not None, payload
    assert "unique" in diff["unified"]
    # And nothing was planned against the dbt convention it used to assume.
    assert not any(
        d["path"].startswith("models/staging/") for d in payload.get("diffs") or []
    )


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
