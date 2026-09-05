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
from exmergo_dex_core.transform.plans import EditKind, contained_key

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


class ViewlessSidecarProject(GraphProject):
    """Tier 3 for one channel only, and no way to read the keyspace it places in.

    Split out from :class:`SidecarGraphProject` rather than written twice: the
    only difference is `load()`, which is the member a second format implemented
    without being asked to and dex called anyway. Everything below is what a
    format got right and still could not complete a reconcile with.

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
            # The writer honors the surface the format declared, rather than
            # trusting whoever built the edit to have honored it. `write_edits`
            # is a public method, so the plan-time check is not the only door.
            contained_key(edit.path, self.editing_surface())
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


class SidecarGraphProject(ViewlessSidecarProject):
    """The whole seam: the sidecar can be read as well as placed into and written.

    `load()` is what carries the declarations dex pins an edit against and reads
    before it proposes one. It lives here rather than on the base above only so
    the base can stand in for the format that omits it.
    """

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


def _schema_drift(repo, *statements):
    repo.snapshot()
    repo.sql(*statements)
    repo.dex("maintain", "schema")


def test_a_placing_format_receives_a_column_edit_in_its_own_sidecar(sidecar_repo):
    """The schema axis reaches a format that declines the staging model channel.

    Placement decides where an edit lands, not what dex is allowed to author. A
    column appearing upstream is a fact about the declaration, and the
    declaration is a file this format placed and can receive. Declining the SQL
    half is not a reason to forfeit the half that was never dbt SQL.
    """

    _schema_drift(sidecar_repo, "ALTER TABLE orders ADD COLUMN discount DOUBLE")

    rc, payload = sidecar_repo.dex("maintain", "reconcile", "schema")

    assert rc == 0 and payload["status"] == "ok", payload.get("errors")
    mechanical = [p for p in payload["data"]["proposals"] if p["kind"] == "mechanical"]
    assert mechanical, payload
    assert mechanical[0]["paths"] == [_SIDECAR]
    diff = next(d for d in payload.get("diffs") or [] if d["path"] == _SIDECAR)
    assert "discount" in diff["unified"]
    # Nothing was authored against dbt's convention, which this format does not have.
    assert not any(
        d["path"].startswith("models/staging/") for d in payload.get("diffs") or []
    )


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


_HAND_WRITTEN = (
    "version: 2\n"
    "models:\n"
    "  # the orders asset, declared by hand\n"
    "  - name: orders\n"
    "    columns:\n"
    "      - name: order_id\n"
    "        description: the natural key\n"
    "        tests: [not_null]\n"
    "      - name: customer_id\n"
    "        tests: [not_null]\n"
    "      - name: status\n"
    "        tests: [not_null]\n"
)


def test_the_column_edit_is_a_splice_that_leaves_the_hand_written_file_alone(
    sidecar_repo,
):
    """The declaration is a file a person wrote, so dex changes bytes, not shape.

    Reprinting the document a YAML parser produced would drop the comment and
    reflow every line, and a reviewer would be reading a whole-file diff to find
    the one column that moved. The comment and the description below are the
    things a round trip loses, which is why they are what this asserts on.
    """

    (sidecar_repo.root / _SIDECAR).write_text(_HAND_WRITTEN, encoding="utf-8")
    _schema_drift(sidecar_repo, "ALTER TABLE orders ADD COLUMN discount DOUBLE")

    rc, payload = sidecar_repo.dex("maintain", "reconcile", "schema")

    assert rc == 0, payload.get("errors")
    diff = next(d for d in payload["diffs"] if d["path"] == _SIDECAR)
    assert diff["deletions"] == 0 and diff["additions"] == 1, diff["unified"]

    sidecar_repo.dex("transform", "apply", payload["data"]["plan_id"])
    written = (sidecar_repo.root / _SIDECAR).read_text(encoding="utf-8")
    assert "# the orders asset, declared by hand" in written
    assert "description: the natural key" in written
    assert "- name: discount" in written


def test_a_dropped_column_takes_its_own_entry_and_not_its_neighbour(sidecar_repo):
    """Removing the middle of three leaves the other two.

    A block entry's span is measured by indentation rather than from the parser's
    end mark, which lands inside the *next* entry. Trusting the mark makes a
    removal take the column below it with it, in a diff the reviewer reads as one
    deletion. The middle column is the only position where that is visible.
    """

    (sidecar_repo.root / _SIDECAR).write_text(_HAND_WRITTEN, encoding="utf-8")
    _schema_drift(sidecar_repo, "ALTER TABLE orders DROP customer_id")

    rc, payload = sidecar_repo.dex("maintain", "reconcile", "schema")
    assert rc == 0, payload.get("errors")
    sidecar_repo.dex("transform", "apply", payload["data"]["plan_id"])

    written = (sidecar_repo.root / _SIDECAR).read_text(encoding="utf-8")
    assert "- name: customer_id" not in written
    assert "- name: order_id" in written and "- name: status" in written
    assert "description: the natural key" in written


def test_a_retype_is_advisory_because_the_declaration_carries_no_type(sidecar_repo):
    """A type change is surfaced, not written, and the action says why.

    Nothing dex authors declares a type, and the type it holds is the connector's
    own spelling rather than a canonical one, so a written type would be one
    warehouse's word for the column rather than the column's.
    """

    (sidecar_repo.root / _SIDECAR).write_text(_HAND_WRITTEN, encoding="utf-8")
    _schema_drift(sidecar_repo, "ALTER TABLE orders ALTER amount TYPE DECIMAL(10,2)")

    rc, payload = sidecar_repo.dex("maintain", "reconcile", "schema")

    assert rc == 0, payload.get("errors")
    retyped = [
        p for p in payload["data"]["proposals"] if p["finding_code"] == "column_retyped"
    ]
    assert [p["kind"] for p in retyped] == ["advisory"]
    assert "DOUBLE -> DECIMAL(10,2)" in retyped[0]["action"]
    assert retyped[0]["paths"] == []
    assert payload["data"].get("plan_id") is None
    assert not payload.get("diffs")


def test_a_nullability_change_adds_and_removes_the_not_null_test(sidecar_repo):
    """Both directions, because the warehouse moves both ways.

    A column that stopped accepting nulls gains the test; one that started
    accepting them loses it, because the declaration asserts something the source
    no longer guarantees and a test that cannot pass is not a safeguard.
    """

    sidecar = sidecar_repo.root / _SIDECAR
    sidecar.write_text(
        "version: 2\n"
        "models:\n"
        "  - name: orders\n"
        "    columns:\n"
        "      - name: order_id\n"
        "        tests: [not_null, unique]\n"
        "      - name: status\n",
        encoding="utf-8",
    )
    sidecar_repo.sql("ALTER TABLE orders ALTER order_id SET NOT NULL")
    sidecar_repo.dex("explore", "map")
    sidecar_repo.snapshot()
    sidecar_repo.sql("ALTER TABLE orders ALTER order_id DROP NOT NULL")
    sidecar_repo.dex("maintain", "schema")

    rc, payload = sidecar_repo.dex("maintain", "reconcile", "schema")

    assert rc == 0, payload.get("errors")
    diff = next(d for d in payload["diffs"] if d["path"] == _SIDECAR)
    # The other declared test survives: only the one the drift is about moves.
    assert "-        tests: [not_null, unique]" in diff["unified"]
    assert "+        tests: [unique]" in diff["unified"]


def test_grain_and_schema_drift_on_one_table_arrive_as_one_edit(sidecar_repo):
    """Two axes, one declaration, one edit.

    Both axes land in the file the format placed for this table. Two edits on one
    path pin the same content hash, so the second overwrites the first and the
    change the reviewer approved is not the change that gets written.
    """

    (sidecar_repo.root / _SIDECAR).write_text(_HAND_WRITTEN, encoding="utf-8")
    sidecar_repo.snapshot()
    sidecar_repo.sql(
        "ALTER TABLE orders ADD COLUMN discount DOUBLE",
        "INSERT INTO orders SELECT * FROM orders WHERE order_id <= 10",
    )
    sidecar_repo.dex("maintain", "schema")
    sidecar_repo.dex("maintain", "grain")

    rc, payload = sidecar_repo.dex("maintain", "reconcile")

    assert rc == 0, payload.get("errors")
    assert [d["path"] for d in payload["diffs"]].count(_SIDECAR) == 1

    sidecar_repo.dex("transform", "apply", payload["data"]["plan_id"])
    written = (sidecar_repo.root / _SIDECAR).read_text(encoding="utf-8")
    # Neither half was dropped on the way through.
    assert "- name: discount" in written
    assert "unique" in written


def test_a_declarations_file_packing_many_models_gets_a_warning_not_a_guess(
    sidecar_repo,
):
    """Dex names the model after the file the format chose, and will not guess.

    The format answered where this table's declaration lives. A file holding
    several models has no entry that answer identifies, and picking one would be
    iteration order rather than a fact about the project.
    """

    (sidecar_repo.root / _SIDECAR).write_text(
        "version: 2\n"
        "models:\n"
        "  - name: orders_core\n"
        "    columns:\n"
        "      - name: order_id\n"
        "  - name: orders_extra\n"
        "    columns:\n"
        "      - name: status\n",
        encoding="utf-8",
    )
    _schema_drift(sidecar_repo, "ALTER TABLE orders ADD COLUMN discount DOUBLE")

    rc, payload = sidecar_repo.dex("maintain", "reconcile", "schema")

    assert rc == 0, payload.get("errors")
    added = next(
        p for p in payload["data"]["proposals"] if p["finding_code"] == "column_added"
    )
    assert added["kind"] == "advisory"
    assert "declares no model named 'orders'" in added["action"]
    assert not payload.get("diffs")


def test_a_declarations_file_dex_cannot_span_stays_advisory(sidecar_repo):
    """A structure the splice cannot be trusted through is declined by name.

    Tabs make indentation ambiguous, and every offset in this path is computed
    from indentation. Refusing and saying which structure is the answer; a splice
    into a file dex cannot read the shape of would land somewhere nobody chose.
    """

    (sidecar_repo.root / _SIDECAR).write_text(
        "version: 2\nmodels:\n  - name: orders\n    columns:\n\t- name: order_id\n",
        encoding="utf-8",
    )
    _schema_drift(sidecar_repo, "ALTER TABLE orders ADD COLUMN discount DOUBLE")

    rc, payload = sidecar_repo.dex("maintain", "reconcile", "schema")

    assert rc == 0, payload.get("errors")
    added = next(
        p for p in payload["data"]["proposals"] if p["finding_code"] == "column_added"
    )
    assert added["kind"] == "advisory"
    assert "indents with tabs" in added["action"]
    assert not payload.get("diffs")


def test_a_column_dropped_by_drift_gets_no_unique_test_proposed_on_it(sidecar_repo):
    """The two axes disagree about a column, and the removal wins.

    A key that lost uniqueness and was then dropped upstream is one event, not
    two. Proposing a test on an entry the same plan removes would build an edit
    that contradicts itself, so the drop stands and the test says why it did not.
    """

    (sidecar_repo.root / _SIDECAR).write_text(_HAND_WRITTEN, encoding="utf-8")
    sidecar_repo.snapshot()
    sidecar_repo.sql(
        "INSERT INTO orders SELECT * FROM orders WHERE order_id <= 10",
    )
    sidecar_repo.dex("maintain", "grain")
    sidecar_repo.sql("ALTER TABLE orders DROP order_id")
    sidecar_repo.dex("maintain", "schema")

    rc, payload = sidecar_repo.dex("maintain", "reconcile")

    assert rc == 0, payload.get("errors")
    lost = next(
        (
            p
            for p in payload["data"]["proposals"]
            if p["finding_code"] == "key_lost_uniqueness"
        ),
        None,
    )
    assert lost is not None and lost["paths"] == []
    assert any("being removed from" in w for w in payload["warnings"])


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
    # Context lines exist only against a file the plan knew was there, and the
    # change is one line: the edit is a splice into the bytes that were there
    # rather than a reprint of the document they parsed into.
    assert "       - name: order_id" in diff["unified"].splitlines()
    assert diff["additions"] == 1 and diff["deletions"] == 1, diff["unified"]
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


@pytest.fixture
def viewless_repo(maintain_repo, monkeypatch):
    """The same repository, wired to the format that cannot be read."""

    module = types.ModuleType("dex_viewless_format")
    module.viewless_project = ViewlessSidecarProject
    monkeypatch.setitem(sys.modules, "dex_viewless_format", module)

    config = maintain_repo.root / ".dex" / "config.yml"
    config.write_text(
        config.read_text(encoding="utf-8")
        + "project:\n  format: dex_viewless_format:viewless_project\n",
        encoding="utf-8",
    )
    sidecar = maintain_repo.root / _SIDECAR
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    sidecar.write_text(_SIDECAR_CONTENT, encoding="utf-8")
    return maintain_repo


def test_a_format_that_cannot_be_read_degrades_and_names_the_member(viewless_repo):
    """The gap arrives as a warning, not as `AttributeError` mid-command.

    This is the format the write tier let all the way through: it implements
    every declared method, omits the one no protocol declared, answers tier 3,
    passes the gate, and then raised from inside a reconcile someone ran. What it
    gets now is what a narrower format has always got, advisory proposals and a
    warning, and the warning names the member rather than sending the
    implementer to the two they already wrote.
    """

    _grain_drift(viewless_repo)

    rc, payload = viewless_repo.dex("maintain", "reconcile", "grain")

    assert rc == 0 and payload["status"] == "ok", payload.get("errors")
    assert payload["data"].get("plan_id") is None
    assert not payload.get("diffs")
    warning = next(w for w in payload["warnings"] if "PlacingProject" in w)
    assert "`load()`" in warning
    # And not sent to the members it already implements.
    named = warning.split("missing")[1].split(".")[0]
    assert "edit_path" not in named and "editing_surface" not in named


def test_an_authored_plan_against_an_unreadable_format_says_so(viewless_repo, tmp_path):
    """`transform plan` is the other caller, and it used to fall back silently.

    The format is not asked for a view it cannot produce, so dex goes back to
    discovering a dbt project, which is what every caller predating the seam
    gets. That fallback is the right behavior and a baffling refusal on its own:
    the edit is turned down for being outside *dbt's* model paths, when the
    format placed it inside the surface it declared, so the message describes a
    project the author was not editing. The gap rides along to say why dbt's
    surface was the one consulted.
    """

    payload_file = _authored_edit(tmp_path, _AUTHORED)

    rc, payload = viewless_repo.dex(
        "transform", "plan", "add a unique test", "--edits-file", str(payload_file)
    )

    assert rc != 0
    reported = json.dumps(payload["errors"])
    assert "outside" in reported
    assert "`load()`" in reported and "PlacingProject" in reported


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
