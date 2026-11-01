"""Cube model files behind the semantic-source seam.

Cube is a **semantic layer**, not a transformation project, and it sits beside
one the way SLayer and native Ossie do: `project.format: dbt` keeps the
transformation tiers, and under `semantic.vendor: cube` the configured Cube
model files are the repository's one semantic layer. The engine builds this
source from `semantic.cube` through :data:`..config.SEMANTIC_SOURCE_FACTORIES`,
so MetricFlow semantic models are not read while Cube is configured.

It implements three source capabilities in :mod:`..semantic_source`:
:class:`~..semantic_source.SemanticCatalogSource` for the read catalog and
:class:`~..semantic_source.SemanticSnapshotSource` for the drift fingerprint,
and :class:`~..semantic_source.SemanticSchemaSource`, which holds the cubes and
views to dex's warehouse metadata and turns what drifted into removals
(:mod:`.schema`, :mod:`.removal`), plus the declaration channel
(`declared_definitions()`) that carries declared keys and joins to
exploration. It is also a :class:`~..edits.SemanticEditTarget`, which
`semantic cube define|update|plan` authors through (:mod:`.authoring`), and a
:class:`~..semantic_source.SemanticDerivationSource`, which turns a declared dbt
mart into a cube (:mod:`.derivation`). What maps and what does not is in
:mod:`.catalog`; which rules a file is held to is in :mod:`.validator` and
:mod:`.loader`.

**Static, and never guessed.** dex reads plain YAML cubes and views, validated
by its own port of Cube's model rules. A templated file, a JS model and an
`extends` are compiled-only: named, never translated. Nothing here starts Node,
calls a Cube server, or opens a warehouse connection, and reading never writes
a file.

Nothing in this module imports sqlglot. :mod:`.lineage`, which does, is imported
on first read, by a source that :func:`.dependency.ensure_available` let
construct.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..errors import ConfigurationError, ProjectError, RepoRootRequiredError
from .dependency import CubeDependencyError, ensure_available

if TYPE_CHECKING:
    from ..cache import Dataset
    from ..edits import ApplyResult, SemanticDocumentView, SemanticEditValidation
    from ..maintain.drift import DriftFinding
    from ..maintain.reconcile import Proposal
    from ..maintain.snapshot import SemanticLayerSnapshot
    from ..project_definitions import ProjectDefinitions
    from ..semantic_catalog import SemanticCatalogView
    from ..semantic_source import SemanticSourceContext
    from ..transform.plans import PlanEdit
    from .catalog import Resolution
    from .loader import LoadResult

__all__ = [
    "VENDOR_NAME",
    "CubeSemanticLayer",
    "build_semantic_layer",
]

#: The semantic vendor this source answers for, as `semantic.vendor` spells it.
VENDOR_NAME = "cube"


class CubeSemanticLayer:
    """The Cube model files committed to this repository.

    Holds its coordinates and reads nothing, because construction is cheap by
    contract: dex builds a source per command. **One instance is meant to live
    for one command**: it memoizes the validated set, and a model file is an
    artifact a later command may rewrite.
    """

    def __init__(
        self,
        repo_root: Path | str,
        files: list[str],
        connector: str | None = None,
        data_source: str = "default",
        url: str | None = None,
    ) -> None:
        self.repo_root = Path(repo_root)
        self.files = list(files)
        self.connector = connector
        self.data_source = data_source
        self.url = url
        self._loaded: LoadResult | None = None
        self._resolved: Resolution | None = None

    @classmethod
    def from_context(cls, context: SemanticSourceContext) -> CubeSemanticLayer:
        """Build from configuration, refusing coordinates it cannot honor.

        The options are checked by the model the committed file is, so a host
        building this source directly cannot hand it a path the configuration
        would have refused. The extra is checked last, so a typo in a path is
        reported before an install is asked for.
        """

        from pydantic import ValidationError

        from ..config import CubeSemanticConfig

        if context.repo_root is None:
            raise RepoRootRequiredError(
                "the Cube semantic source needs a repo root: its models are "
                "git-reviewable files in the repository, so build the engine "
                "with DexEngine.from_repo(repo_root) or pass repo_root="
            )
        options = dict(context.options or {})
        if unknown := sorted(set(options) - set(CubeSemanticConfig.model_fields)):
            raise ConfigurationError(
                "the Cube semantic source takes `files`, `url` and `data_source`, "
                f"and got: {', '.join(unknown)}. An option dex accepted and "
                "ignored would be indistinguishable from one it honored"
            )
        try:
            section = CubeSemanticConfig(**options)
        except ValidationError as exc:
            problems = "; ".join(
                str(error.get("ctx", {}).get("error") or error["msg"])
                for error in exc.errors()
            )
            raise ConfigurationError(problems) from exc
        if not section.files:
            raise ConfigurationError(
                "the Cube semantic source needs semantic.cube.files: the Cube "
                "model files to read, named relative to the repository root"
            )
        ensure_available()
        return cls(
            context.repo_root,
            section.files,
            context.connector,
            data_source=section.data_source,
            url=section.url,
        )

    def loaded(self) -> LoadResult:
        """This command's one validated read of the configured files.

        What a renderer plans over (#913) and what authoring compares against
        (#911), so neither reads a file a second time. A refused element is
        absent here and named in the notes.
        """

        return self._load()

    # --- the declaration channel ----------------------------------------------

    def declared_definitions(self) -> ProjectDefinitions:
        """What the models declare: keys, joins, and the relations they read.

        **This must not raise.** Exploration runs against raw warehouses, and
        every failure this source has is a state a user reaches by an ordinary
        typo. Each yields what could be read, with a note naming the file.
        """

        from ..project_definitions import ProjectDefinitions
        from . import catalog as catalog_mod

        try:
            loaded, resolved = self._load(), self._resolution()
        except CubeDependencyError as exc:
            return ProjectDefinitions(notes=[str(exc)])
        except (ImportError, OSError, ValueError) as exc:
            return ProjectDefinitions(notes=[f"Cube models could not be read: {exc}"])
        if loaded.empty:
            return ProjectDefinitions(notes=loaded.notes())
        return catalog_mod.definitions(loaded, resolved, notes=loaded.notes())

    # --- the catalog channel ---------------------------------------------------

    def semantic_catalog(self) -> SemanticCatalogView:
        """The models as a read catalog.

        **Raises where the declaration channel degrades**: a set of files none
        of which could be read is the answer to the caller's question, and an
        empty catalog would read as "this layer declares nothing". A set holding
        only compiled-only definitions is not unreadable: it answers with those
        named.
        """

        from . import catalog as catalog_mod

        loaded = self._load()
        if loaded.errors and loaded.empty and not loaded.compiled_only:
            listed = "\n  ".join(d.render() for d in loaded.errors)
            raise ProjectError(
                "the configured Cube model files could not be read as a semantic "
                f"layer:\n  {listed}"
            )
        return catalog_mod.semantic_catalog(
            loaded,
            self._resolution(),
            data_source=self.data_source,
            notes=loaded.notes(),
        )

    # --- the snapshot channel --------------------------------------------------

    def semantic_layer(self) -> SemanticLayerSnapshot:
        """The models' fingerprint. Degrades the way :meth:`declared_definitions`
        does, for the same reason."""

        from ..maintain.snapshot import SemanticLayerSnapshot
        from . import snapshot as snapshot_mod

        try:
            loaded, resolved = self._load(), self._resolution()
        except CubeDependencyError as exc:
            return SemanticLayerSnapshot(notes=[str(exc)])
        except (ImportError, OSError, ValueError) as exc:
            return SemanticLayerSnapshot(
                notes=[f"Cube models could not be read: {exc}"]
            )
        return snapshot_mod.semantic_layer(loaded, resolved, self.repo_root)

    # --- the schema channel (#912) ------------------------------------------------

    def declared_schema_drift(
        self, current: list[Dataset], baseline: list[Dataset]
    ) -> tuple[list[DriftFinding], list[str]]:
        """The cubes and views held to dex's warehouse metadata; see :mod:`.schema`.

        Degrades the way :meth:`semantic_layer` does: a set that cannot be read
        compares nothing and says why.
        """

        try:
            loaded, resolved = self._load(), self._resolution()
            from .schema import declared_schema_drift
        except CubeDependencyError as exc:
            return [], [str(exc)]
        except (ImportError, OSError, ValueError) as exc:
            return [], [f"Cube models were not compared to the warehouse: {exc}"]
        return declared_schema_drift(
            loaded, resolved, current, baseline, connector=self.connector
        )

    def handles_finding(self, finding: DriftFinding) -> bool:
        from .removal import CODES

        return finding.code in CODES and finding.data.get("model_file") is not None

    def schema_reconcile(
        self, findings: list[DriftFinding]
    ) -> tuple[list[Proposal], list[PlanEdit], list[str]]:
        """Each removal the schema axis recorded, as an edit of its model file."""

        from .authoring import STRICT_RULES
        from .removal import proposals_and_edits

        own = [finding for finding in findings if self.handles_finding(finding)]
        if not own:
            return [], [], []
        contents = {
            name: source.content
            for name, source in self.semantic_edit_view().files.items()
        }
        strict: dict[str, list[str]] = {}
        for diagnostic in self._load().diagnostics:
            if diagnostic.rule in STRICT_RULES:
                strict.setdefault(diagnostic.file, []).append(diagnostic.element)
        return proposals_and_edits(own, contents, strict)

    # --- native semantic-document authoring (#911) ------------------------------

    def semantic_edit_view(self) -> SemanticDocumentView:
        """The configured YAML model files' bytes and hashes, keyed as configured."""

        from .authoring import load_edit_view

        return load_edit_view(self)

    def semantic_editing_surface(self) -> list[str]:
        """Exactly the configured YAML model files, relative to the repository.

        No directory is listed, so nothing beside a configured file is inside
        the surface, and a JavaScript model is not on it: dex writes plain YAML.
        """

        from .authoring import editing_surface

        return editing_surface(self)

    def write_semantic_edits(
        self, edits: Any, *, confirmed: bool = False
    ) -> ApplyResult:
        """Write the authored bytes, all or none, refusing a stale pin."""

        from .authoring import write_edits

        return write_edits(self, list(edits), confirmed=confirmed)

    def validate_semantic_edits(
        self, edits: Any, mode: str, *, cache: Any = None
    ) -> SemanticEditValidation:
        """Validate the files these edits would leave, as the reader reads them.

        ``cache`` is accepted and not read: grounding authored cubes against the
        warehouse is `maintain`'s job, not authoring's.
        """

        from .authoring import validate_plan

        return validate_plan(self, list(edits), mode)

    # --- the derivation channel (#927, #911) -------------------------------------

    
    def declaration_models(self, documents: dict[str, str]) -> dict[str, set[str]]:
        """Cubes in ``documents`` and the members each exposes (#1030)."""

        from .derivation import declaration_models as _models

        return _models(documents)

    def derive_semantic(self, request: Any) -> Any:
        """Declared marts as Cube model files; see :mod:`.derivation`.

        Reads only the request and the configured files, and validates with the
        checks authoring runs, so no Cube server or warehouse is opened.
        """

        ensure_available()
        from .derivation import derive

        return derive(self, request)

    def _load(self) -> LoadResult:
        if self._loaded is None:
            from .loader import load_files

            self._loaded = load_files(self.repo_root, self.files)
        return self._loaded

    def _resolution(self) -> Resolution:
        if self._resolved is None:
            ensure_available()
            from .catalog import resolution

            self._resolved = resolution(self._load(), self.connector)
        return self._resolved


def build_semantic_layer(context: SemanticSourceContext) -> CubeSemanticLayer:
    """The factory :data:`..config.SEMANTIC_SOURCE_FACTORIES` names for Cube."""

    return CubeSemanticLayer.from_context(context)
