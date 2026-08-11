"""One contract, every project format: the assertions each format owes its callers.

The contract itself lives in `exmergo_dex_core.adapters.conformance`, shipped in the
wheel so a format outside this distribution runs exactly these assertions. This file
is the in-repo consumer of it, and it is deliberately the same few lines a third
party writes: if driving the shipped contract were awkward here, it would be awkward
there.

Format-specific behavior lives in tests/transform/test_dbt_project.py. What is here
is only "does the one shipped format satisfy the seam", plus the control below that
keeps the seam load-bearing rather than merely defined.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from exmergo_dex_core import dbt_project
from exmergo_dex_core.adapters.conformance import (
    DeclaringProjectContract,
    EditableProjectContract,
    MaintainProjectContract,
    PlacingProjectContract,
    ProjectFactoryContract,
    SemanticProjectContract,
)
from exmergo_dex_core.adapters.project import (
    DbtProject,
    EditableProject,
    ProjectContext,
    tier_of,
)
from exmergo_dex_core.config import DexConfig
from exmergo_dex_core.dbt_project import ProjectDefinitions
from exmergo_dex_core.engine import DexEngine
from exmergo_dex_core.explore import commands as explore_commands
from exmergo_dex_core.maintain.drift import schema_drift
from exmergo_dex_core.maintain.snapshot import (
    SemanticLayer,
    Snapshot,
    SourceTable,
    TransformLayer,
)
from exmergo_dex_core.storage import MemoryStore

_PROJECT_YML = (
    'name: dex_test\nversion: "1.0.0"\nprofile: dex_test\nmodel-paths: ["models"]\n'
)


def _project(root: Path, schema: str | None = None) -> Path:
    """A minimal dbt project, optionally carrying one schema.yml."""

    project = root / "analytics"
    (project / "models").mkdir(parents=True, exist_ok=True)
    (project / "dbt_project.yml").write_text(_PROJECT_YML, encoding="utf-8")
    (project / "models" / "stg_customers.sql").write_text(
        "select 1 as id\n", encoding="utf-8"
    )
    if schema is not None:
        (project / "models" / "schema.yml").write_text(schema, encoding="utf-8")
    return project


def _staged_conflict(root: Path):
    """A dbt project whose planned target a human changed after planning.

    Shared by both tier-3 conformance subclasses below rather than written twice,
    because the two differ in how the project is *constructed* and not at all in
    what the conflict looks like.
    """

    project = _project(root)
    target = project / "models" / "stg_customers.sql"
    edit = dbt_project.Edit(
        path="models/stg_customers.sql",
        new_content="select 2 as id\n",
        old_content_hash=dbt_project.content_hash(target.read_text(encoding="utf-8")),
    )
    # The human, arriving after the plan pinned the hash above.
    target.write_text("select 3 as id -- mine\n", encoding="utf-8")

    return (
        DbtProject(root, project),
        project,
        [edit],
        lambda: target.read_text(encoding="utf-8"),
    )


class TestDbtProject(
    DeclaringProjectContract,
    SemanticProjectContract,
    PlacingProjectContract,
    EditableProjectContract,
):
    @pytest.fixture(autouse=True)
    def _root(self, tmp_path: Path):
        # One root per assertion: a project is read from disk, so two assertions
        # writing different schema.yml files must not share a directory.
        self.root = tmp_path

    def make_project(self) -> DbtProject:
        project = _project(self.root)
        return DbtProject(self.root, project)

    def an_edit_against_a_changed_target(self):
        return _staged_conflict(self.root)

    def placeable_model(self) -> str:
        # The warehouse table, not `stg_orders`: dbt's scaffold prefix is applied
        # by `edit_path` on the way out, and passing the prefixed name here would
        # assert the convention twice and prove it once.
        return "orders"

    def make_unreadable_project(self) -> DbtProject:
        # A dbt_project.yml that is not parseable YAML. dbt is a filesystem format,
        # so this state is real for it, which is why the hook is overridden rather
        # than left to skip.
        project = self.root / "broken"
        project.mkdir()
        (project / "dbt_project.yml").write_text("name: [unclosed\n", encoding="utf-8")
        return DbtProject(self.root, project)

    def a_project_declaring_a_unique_key(self) -> tuple[DbtProject, str, str]:
        project = _project(
            self.root,
            "version: 2\n"
            "models:\n"
            "  - name: stg_customers\n"
            "    columns:\n"
            "      - name: id\n"
            "        tests: [unique]\n",
        )
        return DbtProject(self.root, project), "stg_customers", "id"

    def a_project_declaring_a_join(self) -> tuple[DbtProject, str, str, str, str]:
        project = _project(
            self.root,
            "version: 2\n"
            "models:\n"
            "  - name: stg_orders\n"
            "    columns:\n"
            "      - name: customer_id\n"
            "        tests:\n"
            "          - relationships:\n"
            "              to: ref('stg_customers')\n"
            "              field: id\n",
        )
        return (
            DbtProject(self.root, project),
            "stg_orders",
            "customer_id",
            "stg_customers",
            "id",
        )

    def a_project_declaring_a_join_with_differently_named_sides(self):
        # dbt's `relationships` test exists to express exactly this, and the
        # fixture above already happens to use it: `customer_id` on one side, `id`
        # on the other. Returned again through the widened hook rather than left to
        # skip, so the assertion that the two stay apart actually runs.
        return self.a_project_declaring_a_join()

    def a_project_declaring_a_composite_key(self):
        # Four columns, not two: a format special-casing the pair would pass a
        # two-column fixture. dbt carries this as a model-level
        # `unique_combination_of_columns`, which is a different construct from the
        # column-level `unique` above and lands in a different field.
        project = _project(
            self.root,
            "version: 2\n"
            "models:\n"
            "  - name: stg_customers\n"
            "    tests:\n"
            "      - unique_combination_of_columns:\n"
            "          combination_of_columns:\n"
            "            - tenant_id\n"
            "            - id\n"
            "            - valid_from\n"
            "            - source_system\n",
        )
        return (
            DbtProject(self.root, project),
            "stg_customers",
            ("tenant_id", "id", "valid_from", "source_system"),
        )

    def a_project_declaring_a_semantic_model(self):
        """One semantic model covering all three ways a field resolves.

        A bare `expr` is the column, an absent one means the field's own name is,
        and a computed expression resolves to nothing. The third is the case worth
        having: `markup` is `type: categorical` AND unresolved, so it must appear in
        `dimensions` mapped to None and must NOT appear in `categorical_dimensions`,
        whose values are required strings. A format that collapses those two
        properties either drops the field or invents a column for it.
        """

        project = _project(self.root)
        (project / "models" / "semantic.yml").write_text(
            "semantic_models:\n"
            "  - name: customers_sm\n"
            "    model: ref('stg_customers')\n"
            "    dimensions:\n"
            "      - name: signed_up_at\n"
            "        type: time\n"
            "      - name: region\n"
            "        type: categorical\n"
            "        expr: region_code\n"
            "      - name: markup\n"
            "        type: categorical\n"
            "        expr: base_rate * 1.2\n"
            "    measures:\n"
            "      - name: revenue\n"
            "        agg: sum\n"
            "        expr: revenue_net\n"
            "      - name: blended\n"
            "        agg: sum\n"
            "        expr: revenue_net + adjustments\n",
            encoding="utf-8",
        )
        return (
            DbtProject(self.root, project),
            "customers_sm",
            {"signed_up_at": "signed_up_at", "region": "region_code", "markup": None},
            {"revenue": "revenue_net", "blended": None},
        )


class TestDbtProjectFactory(ProjectFactoryContract, EditableProjectContract):
    """The shipped format, built the way configuration builds it.

    The same few lines a third party writes, and the reason the construction half
    is worth running in-repo: every other assertion above constructs `DbtProject`
    directly, which is the one path a host reaching dex as a subprocess cannot
    take.

    `tier` is the write tier rather than the read one because that is what the
    factory actually returns, and a factory silently building one tier lower than
    the format implements is exactly what this assertion is for.
    """

    tier = EditableProject

    @pytest.fixture(autouse=True)
    def _root(self, tmp_path: Path):
        self.root = tmp_path

    def build(self, context: ProjectContext) -> DbtProject:
        return DbtProject.from_context(context)

    def empty_context(self) -> ProjectContext:
        _project(self.root)
        return ProjectContext(repo_root=str(self.root), project_dir="analytics")

    def an_edit_against_a_changed_target(self):
        return _staged_conflict(self.root)


class _PathlessProject:
    """A tier-2 format reduced from objects rather than files.

    The stand-in for the class of format the seam exists for: its models,
    sources, and definitions come from a graph held in memory, so there is no
    file per declaration to name and no file hash to record. It is here because
    the shipped dbt format will never exercise the optional `path` or the layer
    `notes` -- it always has both -- so running the contract against dbt alone
    would leave the pathless half of tier 2 unasserted.
    """

    name = "pathless"

    _NOTE = "definitions are objects in a graph, so no file provenance exists"

    def definitions(self) -> ProjectDefinitions:
        return ProjectDefinitions(
            present=True, built_relation_names=["orders"], notes=[self._NOTE]
        )

    def transform_layer(self) -> TransformLayer:
        return TransformLayer(
            models=["orders"],
            sources=[SourceTable(source_name="raw", table="orders")],
            notes=[self._NOTE],
        )

    def semantic_layer(self) -> SemanticLayer:
        return SemanticLayer(notes=[self._NOTE])


class TestPathlessProject(MaintainProjectContract):
    """The shipped contract, run against a format that names no files."""

    def make_project(self) -> _PathlessProject:
        return _PathlessProject()


def test_a_pathless_format_declines_the_write_tier():
    """Tier 2 and not tier 3, which is the answer the tiers exist to let it give.

    A reduction of a running graph cannot receive an edit, so declining
    `EditableProject` is correct rather than partial. Asserted here because the
    tier is what `reconcile`'s write guard is meant to read.
    """

    assert tier_of(_PathlessProject()) == 2


def test_a_pathless_source_omits_provenance_from_a_dangling_finding():
    """No path means no `declared_in` key, not a null one.

    The finding stays actionable through `identifier`; a null-valued provenance
    key would only force a reader to branch. Paired with
    `tests/maintain/test_schema.py`, which asserts the dbt path still carries the
    file it always had.
    """

    snap = Snapshot(
        created_at="2026-01-01T00:00:00+00:00",
        transform_layer=_PathlessProject().transform_layer(),
    )

    findings = schema_drift([], snap, None, None)

    dangling = [f for f in findings if f.code == "dangling_source"]
    assert len(dangling) == 1
    assert dangling[0].identifier == "raw.orders"
    assert dangling[0].severity == "high"
    assert "declared_in" not in dangling[0].data


def _engine(root: Path, **config) -> DexEngine:
    return DexEngine(repo_root=str(root), config=DexConfig(**config))


def test_explore_reads_the_project_through_the_seam(monkeypatch, tmp_path: Path):
    """The control: explore's project read goes through the seam, not around it.

    Every other assertion in this file would pass just as well if
    `_project_definitions` called `dbt_project.definitions` directly, because
    `DbtProject.definitions` delegates to it -- the results are identical by
    construction. That is exactly what makes this test necessary: without it "the
    suite is green" would not distinguish a seam that is load-bearing from one that
    is merely defined, which is the state this whole protocol exists to leave.
    """

    project = _project(tmp_path)
    calls: list[tuple[Path, Path | None]] = []
    real = DbtProject.definitions

    def recording(self: DbtProject):
        calls.append((self.repo_root, self.project_dir))
        return real(self)

    monkeypatch.setattr(DbtProject, "definitions", recording)

    defs = explore_commands._project_definitions(
        _engine(tmp_path, dbt_project_dir="analytics"), use_project=True
    )

    assert calls == [(tmp_path, project)], (
        "explore did not read the project through the project seam; it is defined "
        "but not load-bearing"
    )
    assert defs.present


class _RecordedProject(_PathlessProject):
    """A tier-2 format that counts what maintain asked it for."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def transform_layer(self) -> TransformLayer:
        self.calls.append("transform_layer")
        return super().transform_layer()

    def semantic_layer(self) -> SemanticLayer:
        self.calls.append("semantic_layer")
        return super().semantic_layer()


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("snapshot", ["transform_layer", "semantic_layer"]),
        ("schema_drift", ["transform_layer"]),
        ("check", ["transform_layer", "semantic_layer"]),
        ("semantic_drift", ["transform_layer", "semantic_layer"]),
    ],
)
def test_maintain_reads_the_layers_through_the_seam(
    duckdb_file: Path, tmp_path: Path, command: str, expected: list[str]
):
    """The same control for tier 2, and it is what makes the tier real.

    `DbtProject.transform_layer` delegates to the same function `maintain` used to
    call on a view it loaded itself, so every drift assertion in the suite passes
    either way. What separates the two is whether a format that is *not* a dbt
    project is ever consulted, which is the entire content of the tier. Driving a
    pathless format through the engine is the only way to ask that question.

    The exact call list matters as much as the count: `schema` needs one layer and
    reading both would make a format pay for a channel that command never uses.
    """

    project = _RecordedProject()
    engine = DexEngine(
        repo_root=str(tmp_path),
        config=DexConfig(connector="duckdb", duckdb={"path": str(duckdb_file)}),
        store=MemoryStore(),
        project_format=project,
    )
    engine.snapshot()
    project.calls.clear()

    getattr(engine, command)()

    assert project.calls == expected, (
        f"maintain {command} did not read its layers through MaintainProject; the "
        "tier is defined but not load-bearing"
    )


def test_maintain_loads_the_dbt_project_once_per_command(monkeypatch, tmp_path: Path):
    """Routing through the tier must not cost a second full load.

    `transform_layer()` and `semantic_layer()` each need the view, and three of the
    four commands that read layers need both, so a project that reloaded per
    accessor would double the most expensive read on the maintain path: `load`
    walks the model and macro trees, reads and hashes every file, and parses a
    `manifest.json` that is routinely several megabytes. Nothing about the results
    would change, which is why this is asserted on the call count.
    """

    _project(tmp_path)
    loads: list[Path] = []
    real = dbt_project.load

    def counting(project_dir):
        loads.append(Path(project_dir))
        return real(project_dir)

    monkeypatch.setattr(dbt_project, "load", counting)

    project = DbtProject.from_context(
        ProjectContext(repo_root=str(tmp_path), project_dir="analytics")
    )
    project.transform_layer()
    project.semantic_layer()

    assert len(loads) == 1, f"expected one load per instance, got {len(loads)}"


def test_the_seam_finds_an_unpinned_project_in_a_subdirectory(tmp_path: Path):
    """Both tiers resolve the same project when nothing pinned a directory.

    `definitions()` discovers; `load()` used to resolve straight to the repo root,
    so a project one directory down was found by tier 1 and missing to tier 2. The
    two tiers disagreeing about which project they are reading is the kind of split
    that surfaces as drift nobody can reproduce.
    """

    project = _project(tmp_path)
    seam = DbtProject.from_context(ProjectContext(repo_root=str(tmp_path)))

    assert seam.definitions().present
    assert Path(seam.load().root) == project.resolve()


def test_the_write_tier_uses_the_directory_the_caller_passed(tmp_path: Path):
    """The applying caller's project wins over the one this instance was built for.

    A stored plan records the directory it was pinned against, relative to the repo
    root, and re-resolves it at apply time; a `DbtProject` built from engine
    configuration points wherever configuration said. Nothing makes those the same
    project, and the tier used to resolve from its own configuration, so the edits
    would land in the engine's project while the hashes were checked against that
    project's files rather than the plan's.

    Asserted here rather than in the shipped contract because `project_dir` is a
    slot for the formats keyed by a directory, so the assertion is dbt's to make.
    """

    configured = _project(tmp_path / "configured")
    pinned = _project(tmp_path / "pinned")
    edit = dbt_project.Edit(
        path="models/stg_customers.sql",
        new_content="select 2 as id\n",
        old_content_hash=dbt_project.content_hash(
            (pinned / "models" / "stg_customers.sql").read_text(encoding="utf-8")
        ),
    )

    seam = DbtProject(tmp_path / "configured", configured)
    result = seam.write_edits([edit], pinned)

    assert result.written == ["models/stg_customers.sql"]
    assert (pinned / "models" / "stg_customers.sql").read_text(
        encoding="utf-8"
    ) == "select 2 as id\n"
    assert (configured / "models" / "stg_customers.sql").read_text(
        encoding="utf-8"
    ) == "select 1 as id\n", (
        "the edit landed in the configured project rather than the one the caller "
        "named, which is the disagreement this parameter exists to settle"
    )


def test_the_write_tier_falls_back_to_the_configured_project(tmp_path: Path):
    """No directory from the caller means the configured pin, as it always did.

    The parameter widens the protocol; it does not change what a caller holding
    engine configuration and no plan gets. Asserted so the fallback is a decision
    on the record rather than something a later reader deletes as dead.
    """

    configured = _project(tmp_path / "configured")
    edit = dbt_project.Edit(
        path="models/stg_customers.sql",
        new_content="select 2 as id\n",
        old_content_hash=dbt_project.content_hash(
            (configured / "models" / "stg_customers.sql").read_text(encoding="utf-8")
        ),
    )

    seam = DbtProject(tmp_path / "configured", configured)
    result = seam.write_edits([edit])

    assert result.written == ["models/stg_customers.sql"]
    assert (configured / "models" / "stg_customers.sql").read_text(
        encoding="utf-8"
    ) == "select 2 as id\n"
