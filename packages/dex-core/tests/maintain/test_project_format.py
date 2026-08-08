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

from exmergo_dex_core.dbt_project import ProjectDefinitions
from exmergo_dex_core.maintain.snapshot import (
    SemanticLayer,
    SourceTable,
    TransformLayer,
)

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
