"""Native Ossie semantic-source construction coverage."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from exmergo_dex_core.adapters.project import ProjectContext
from exmergo_dex_core.adapters.project_resolver import SHIPPED, build_project
from exmergo_dex_core.errors import (
    ConfigurationError,
    ProjectError,
    RepoRootRequiredError,
)
from exmergo_dex_core.ossie import OssieSemanticLayer
from exmergo_dex_core.project_definitions import ProjectDefinitions
from exmergo_dex_core.semantic_source import SemanticSourceContext

from .conftest import document, model, reference_document, write


def context(root: Path, *names: str, connector: str = "duckdb", **options):
    return SemanticSourceContext(
        repo_root=str(root),
        connector=connector,
        options={"files": list(names), **options},
    )


def project_context(root: Path, *names: str, connector: str = "duckdb"):
    """The same coordinates shaped as a *project* context.

    Only the negative tests need this: they prove the project resolver refuses
    the name, and the resolver takes the project shape.
    """

    return ProjectContext(
        repo_root=str(root), connector=connector, options={"files": list(names)}
    )


# --- construction ----------------------------------------------------------


def test_ossie_is_not_a_shipped_project_format(repo: Path):
    """Native semantics must not impersonate a transformation project."""

    assert "ossie" not in SHIPPED
    with pytest.raises(ConfigurationError, match="unknown project format"):
        build_project("ossie", project_context(repo, "commerce.ossie.yaml"))


def test_resolving_the_dbt_name_does_not_import_the_ossie_reader():
    """The CLI runs a fresh process per command, so a dbt repository should not
    pay to import a schema validator to resolve the name `dbt`."""

    import subprocess
    import sys

    probe = (
        "import sys;"
        "import exmergo_dex_core.adapters.project_resolver as r;"
        "r.resolve_project_factory('dbt');"
        "print('exmergo_dex_core.ossie' in sys.modules)"
    )

    out = subprocess.run(  # noqa: S603  (a fixed argv, no shell)
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True
    )

    assert out.stdout.strip() == "False"


def test_the_source_needs_a_repository(repo: Path):
    with pytest.raises(RepoRootRequiredError, match="repo root"):
        OssieSemanticLayer.from_context(
            SemanticSourceContext(options={"files": ["commerce.ossie.yaml"]})
        )


def test_an_option_the_source_cannot_honor_is_refused_by_name(repo: Path):
    """Accepted-and-ignored is indistinguishable from honored, right up until
    dex is reading a different project than the configuration named."""

    with pytest.raises(ConfigurationError, match="documents"):
        OssieSemanticLayer.from_context(
            context(repo, "commerce.ossie.yaml", documents=["x"])
        )


@pytest.mark.parametrize(
    ("files", "expected"),
    [
        (None, "needs `files`"),
        ([], "empty"),
        ("commerce.ossie.yaml", "list of document paths"),
        (["schema.yml"], "not a native Ossie document"),
        (["../out.ossie.yaml"], "inside the repository"),
        (["/abs.ossie.yaml"], "inside the repository"),
        (["a.ossie.yaml", "a.ossie.yaml"], "same document twice"),
    ],
)
def test_bad_coordinates_are_refused_where_they_are_written(
    repo: Path, files, expected
):
    options = {} if files is None else {"files": files}

    with pytest.raises(ConfigurationError, match=expected):
        OssieSemanticLayer.from_context(
            SemanticSourceContext(
                repo_root=str(repo), connector="duckdb", options=options
            )
        )


def test_construction_reads_nothing(tmp_path: Path):
    """Cheap by contract: dex builds a project per command, and a factory that
    parsed would charge every command that never looks at a semantic layer.

    Also why a *missing* document is not a construction error: refusing here
    would make `explore map` on a raw warehouse fail over a typo in a
    semantic-layer path.
    """

    source = OssieSemanticLayer.from_context(context(tmp_path, "absent.ossie.yaml"))

    assert source.files == ["absent.ossie.yaml"]


# --- the capabilities, and the tiers it deliberately does not reach ---------


def test_the_source_answers_both_semantic_capabilities(repo: Path):
    """A catalog to read and a fingerprint to compare, and nothing wider."""

    from exmergo_dex_core.semantic_source import (
        SemanticCatalogSource,
        SemanticSnapshotSource,
    )

    source = OssieSemanticLayer.from_context(context(repo, "commerce.ossie.yaml"))

    assert isinstance(source, SemanticCatalogSource)
    assert isinstance(source, SemanticSnapshotSource)
    assert source.semantic_catalog().semantic_models
    assert source.semantic_layer().semantic_models


def test_the_source_satisfies_no_project_tier(repo: Path):
    """Claiming a tier structurally is claiming it to `maintain reconcile` and
    to `transform apply`.

    A transformation project owns a model graph, compilation, targets, and a
    write surface into dbt's own files. Ossie owns none of those, so satisfying
    even the narrowest tier would tell reconcile it may propose edits this
    source cannot apply, and would let a repository be reasoned about as though
    a document set were a model graph. The tiers are structural, so this is a
    real risk rather than a documentation point: two vestigial members were all
    it took before.
    """

    from exmergo_dex_core.adapters.project import (
        EditableProject,
        ExploreProject,
        MaintainProject,
        PlacingProject,
    )

    source = OssieSemanticLayer.from_context(context(repo, "commerce.ossie.yaml"))

    for tier in (ExploreProject, MaintainProject, EditableProject, PlacingProject):
        assert not isinstance(source, tier), tier.__name__
    assert not hasattr(source, "name")
    assert not hasattr(source, "definitions")
    assert not hasattr(source, "transform_layer")


def test_the_runtime_layer_satisfies_no_project_tier(repo: Path):
    """The same for the object the explore surface holds, which wraps the
    reader and would otherwise inherit the question."""

    from exmergo_dex_core.adapters.project import (
        EditableProject,
        ExploreProject,
        MaintainProject,
        PlacingProject,
    )
    from exmergo_dex_core.config import DexConfig, save_config
    from exmergo_dex_core.engine import DexEngine
    from exmergo_dex_core.explore.semantic import resolve_semantic_layer

    save_config(
        DexConfig(
            connector="duckdb",
            semantic={"vendor": "ossie", "ossie": {"files": ["commerce.ossie.yaml"]}},
        ),
        repo,
    )
    layer = resolve_semantic_layer(DexEngine.from_repo(str(repo)), local=True)

    for tier in (ExploreProject, MaintainProject, EditableProject, PlacingProject):
        assert not isinstance(layer, tier), tier.__name__


def test_no_semantic_route_builds_its_source_through_the_project_factory(
    repo: Path, monkeypatch
):
    """The construction path is the thing this issue moved, so it is the thing
    a regression would move back.

    Spying on the factory rather than grepping for an import: a route that
    reached it through a helper, or through the engine's own project accessor,
    would satisfy a grep and still be building a semantic source as a project.
    """

    from exmergo_dex_core.adapters import project_resolver
    from exmergo_dex_core.config import DexConfig, save_config
    from exmergo_dex_core.engine import DexEngine

    save_config(
        DexConfig(
            connector="duckdb",
            semantic={"vendor": "ossie", "ossie": {"files": ["commerce.ossie.yaml"]}},
        ),
        repo,
    )

    def refuse(*args, **kwargs):
        raise AssertionError("a semantic source was built through the project factory")

    monkeypatch.setattr(project_resolver, "build_project", refuse)
    monkeypatch.setattr(project_resolver, "construct_project", refuse)

    engine = DexEngine.from_repo(str(repo))
    source = engine.semantic_catalog_source()

    assert source.semantic_catalog().semantic_models
    assert source.semantic_layer().semantic_models
    assert source.declared_definitions().declared_keys


@pytest.mark.parametrize(
    "sabotage",
    [
        pytest.param(lambda root: None, id="no_document"),
        pytest.param(
            lambda root: (root / "commerce.ossie.yaml").write_text("a: [1,\n b: {\n"),
            id="unparseable",
        ),
        pytest.param(
            lambda root: (root / "commerce.ossie.yaml").write_text("version: '0.1'\n"),
            id="fails_the_schema",
        ),
        pytest.param(
            lambda root: (root / "commerce.ossie.yaml").write_text("- a\n- b\n"),
            id="not_a_mapping",
        ),
        pytest.param(
            lambda root: (root / "commerce.ossie.yaml").mkdir(),
            id="a_directory",
        ),
    ],
)
def test_definitions_never_raises_whatever_the_documents_are(tmp_path: Path, sabotage):
    """The tier's real contract, and behavioural rather than structural.

    Exploration runs against raw warehouses where a semantic layer is absent, so
    every one of these is a state a user reaches on an ordinary day. A format
    that raised would turn each into an outage.
    """

    sabotage(tmp_path)
    source = OssieSemanticLayer.from_context(context(tmp_path, "commerce.ossie.yaml"))

    definitions = source.declared_definitions()

    assert isinstance(definitions, ProjectDefinitions)
    assert definitions.declared_keys == []
    assert definitions.foreign_keys == []
    assert definitions.notes, (
        "an empty result with no note is indistinguishable from a layer that "
        "genuinely declares nothing"
    )


def test_definitions_degrades_with_a_note_when_the_extra_is_absent(
    repo: Path, monkeypatch
):
    """Absent on tier 1, refused on the catalog channel, and the asymmetry is
    the contract: one caller asked about a warehouse, the other about the layer."""

    import builtins

    real = builtins.__import__

    def refuse(name, *args, **kwargs):
        if name == "jsonschema" or name.startswith("jsonschema."):
            raise ImportError("no validator")
        return real(name, *args, **kwargs)

    source = OssieSemanticLayer.from_context(context(repo, "commerce.ossie.yaml"))
    monkeypatch.setattr(builtins, "__import__", refuse)

    definitions = source.declared_definitions()

    assert definitions.declared_keys == []
    assert any("exmergo-dex-core[ossie]" in note for note in definitions.notes)


def test_the_catalog_channel_refuses_a_document_it_cannot_read(tmp_path: Path):
    (tmp_path / "broken.ossie.yaml").write_text("a: [1,\n", encoding="utf-8")
    source = OssieSemanticLayer.from_context(context(tmp_path, "broken.ossie.yaml"))

    with pytest.raises(ProjectError, match="could not be read"):
        source.semantic_catalog()


def test_reading_the_same_source_twice_agrees(repo: Path):
    """Rules out a read that consumes, which surfaces as a second command
    mysteriously seeing less than the first."""

    source = OssieSemanticLayer.from_context(context(repo, "commerce.ossie.yaml"))

    assert source.declared_definitions() == source.declared_definitions()
    first, second = source.semantic_catalog(), source.semantic_catalog()
    assert [m.name for m in first.metrics] == [m.name for m in second.metrics]


# --- semantic configuration -------------------------------------------------


def test_semantic_configuration_builds_the_catalog(repo: Path):
    """Ossie is configured on the semantic axis and supplies the catalog."""

    from exmergo_dex_core.config import DexConfig, save_config
    from exmergo_dex_core.engine import DexEngine

    def catalog_of(config: dict) -> list[str]:
        save_config(DexConfig(**config), repo)
        engine = DexEngine.from_repo(str(repo))
        view = engine.semantic_catalog_source().semantic_catalog()
        return [m.name for m in view.semantic_models]

    beside_dbt = catalog_of(
        {
            "connector": "duckdb",
            "semantic": {
                "vendor": "ossie",
                "ossie": {"files": ["commerce.ossie.yaml"]},
            },
        }
    )
    assert "commerce.orders" in beside_dbt


def test_project_format_ossie_is_not_migrated(repo: Path):
    """Ossie has no project-format compatibility route."""

    from exmergo_dex_core.config import DexConfig, save_config
    from exmergo_dex_core.engine import DexEngine

    save_config(
        DexConfig(
            connector="duckdb",
            project={
                "format": "ossie",
                "options": {"files": ["commerce.ossie.yaml"]},
            },
        ),
        repo,
    )

    with pytest.raises(ConfigurationError, match="unknown project format 'ossie'"):
        DexEngine.from_repo(str(repo))


def test_the_connector_reaches_the_semantic_layer(repo: Path):
    """The slot exists because identifier arity, quoting, and case folding are
    the connector's rules, and a format that guessed them would link a
    declaration to the wrong column or to none."""

    from exmergo_dex_core.config import DexConfig, save_config
    from exmergo_dex_core.engine import DexEngine

    save_config(
        DexConfig(
            connector="clickhouse",
            semantic={
                "vendor": "ossie",
                "ossie": {"files": ["commerce.ossie.yaml"]},
            },
        ),
        repo,
    )

    from exmergo_dex_core.explore.semantic import resolve_semantic_layer

    layer = resolve_semantic_layer(DexEngine.from_repo(str(repo)))

    assert layer._source.connector == "clickhouse"
    # ClickHouse relations are two parts, so a three-part source is not one.
    semantic_models = layer.list_definitions().view.semantic_models
    assert all(model.relation is None for model in semantic_models)


def test_several_documents_are_read_as_one_layer(tmp_path: Path):
    write(tmp_path, "one.ossie.yaml", reference_document())
    second = document(model("logistics", *[]))
    second["semantic_model"][0]["datasets"] = [
        {
            "name": "shipments",
            "source": "demo.main.shipments",
            "fields": [
                {
                    "name": "shipment_id",
                    "expression": {
                        "dialects": [
                            {"dialect": "ANSI_SQL", "expression": "shipment_id"}
                        ]
                    },
                }
            ],
        }
    ]
    write(tmp_path, "two.ossie.json", second)

    source = OssieSemanticLayer.from_context(
        context(tmp_path, "one.ossie.yaml", "two.ossie.json")
    )
    names = [m.name for m in source.semantic_catalog().semantic_models]

    assert "commerce.orders" in names
    assert "logistics.shipments" in names


def test_a_json_document_reads_identically_to_its_yaml_twin(tmp_path: Path):
    write(tmp_path, "a.ossie.yaml", reference_document())
    (tmp_path / "b.ossie.json").write_text(
        json.dumps(reference_document()), encoding="utf-8"
    )

    def names(name: str) -> list[str]:
        source = OssieSemanticLayer.from_context(context(tmp_path, name))
        view = source.semantic_catalog()
        return [d.name for d in view.dimensions]

    assert names("a.ossie.yaml") == names("b.ossie.json")
