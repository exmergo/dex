"""Snowflake Semantic View files behind the semantic-source seam.

A Semantic View layer is a **semantic layer**, not a transformation project, and
it sits beside one the way SLayer, Cube, native Ossie and Metric Views do:
`project.format: dbt` keeps the transformation tiers, and under
`semantic.vendor: snowflake` the configured Semantic View files are the
repository's one semantic layer. The engine builds this source from
`semantic.snowflake` through :data:`..config.SEMANTIC_SOURCE_FACTORIES`, so
MetricFlow semantic models are not read while it is configured.

It implements :class:`~..semantic_source.SemanticCatalogSource` for the read
catalog and :class:`~..semantic_source.SemanticSnapshotSource` for the drift
fingerprint, plus the declaration channel (`declared_definitions()`) that carries
declared keys and joins to exploration. It is also a
:class:`~..edits.SemanticEditTarget`, which `semantic snowflake
define|update|plan` authors through (:mod:`.authoring`, #1292), and a
:class:`~..semantic_source.SemanticDerivationSource`, which turns a declared dbt
mart into a logical table and annotates an existing one (:mod:`.derivation`).

**Static, and never deploying.** dex reads and writes the Semantic View YAML
committed here. Nothing in this module opens a Snowflake connection, reads what
an account has deployed, or calls ``SYSTEM$CREATE_SEMANTIC_VIEW_FROM_YAML``;
reading never writes a file, and authoring writes only through a stored plan.

Nothing here imports sqlglot. :mod:`.lineage`, which does, is imported on first
read, by a source that :func:`.dependency.ensure_available` let construct.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..errors import ConfigurationError, ProjectError, RepoRootRequiredError
from .dependency import SemanticViewDependencyError, ensure_available

if TYPE_CHECKING:
    from ..edits import ApplyResult, SemanticDocumentView, SemanticEditValidation
    from ..maintain.snapshot import SemanticLayerSnapshot
    from ..project_definitions import ProjectDefinitions
    from ..semantic_catalog import SemanticCatalogView
    from ..semantic_source import SemanticSourceContext
    from .catalog import Resolution
    from .loader import LoadResult

__all__ = [
    "VENDOR_NAME",
    "SemanticViewSemanticLayer",
    "build_semantic_layer",
]

#: The semantic vendor this source answers for, as `semantic.vendor` spells it.
VENDOR_NAME = "snowflake"


class SemanticViewSemanticLayer:
    """The Snowflake Semantic View YAML files committed to this repository.

    Holds its coordinates and reads nothing, because construction is cheap by
    contract: dex builds a source per command. **One instance is meant to live
    for one command**: it memoizes the validated set.
    """

    def __init__(
        self, repo_root: Path | str, files: list[str], connector: str | None = None
    ) -> None:
        """Hold the coordinates; nothing is read until a channel asks."""
        self.repo_root = Path(repo_root)
        self.files = list(files)
        self.connector = connector
        self._loaded: LoadResult | None = None
        self._resolved: Resolution | None = None

    @classmethod
    def from_context(cls, context: SemanticSourceContext) -> SemanticViewSemanticLayer:
        """Build from configuration, refusing coordinates it cannot honor.

        The options are checked by the model the committed file is, so a host
        building this source directly cannot hand it a path the configuration
        would have refused. The extra is checked last, so a typo in a path is
        reported before an install is asked for.
        """

        from pydantic import ValidationError

        from ..config import SnowflakeSemanticConfig

        if context.repo_root is None:
            raise RepoRootRequiredError(
                "the Snowflake Semantic View source needs a repo root: its views "
                "are git-reviewable files in the repository, so build the engine "
                "with DexEngine.from_repo(repo_root) or pass repo_root="
            )
        options = dict(context.options or {})
        allowed = set(SnowflakeSemanticConfig.model_fields)
        if unknown := sorted(set(options) - allowed):
            raise ConfigurationError(
                "the Snowflake Semantic View source takes `files`, and got: "
                f"{', '.join(unknown)}. An option dex accepted and ignored would "
                "be indistinguishable from one it honored"
            )
        try:
            section = SnowflakeSemanticConfig(**options)
        except ValidationError as exc:
            problems = "; ".join(
                str(error.get("ctx", {}).get("error") or error["msg"])
                for error in exc.errors()
            )
            raise ConfigurationError(problems) from exc
        if not section.files:
            raise ConfigurationError(
                "the Snowflake Semantic View source needs semantic.snowflake.files: "
                "the Semantic View YAML files to read, named relative to the "
                "repository root"
            )
        ensure_available()
        return cls(context.repo_root, section.files, context.connector)

    def loaded(self) -> LoadResult:
        """This command's one validated read of the configured files."""

        return self._load()

    # --- the declaration channel ----------------------------------------------

    def declared_definitions(self) -> ProjectDefinitions:
        """What the views declare: keys, joins, and the relations they read.

        **This must not raise.** Exploration runs against raw warehouses, and
        every failure this source has is a state a user reaches by an ordinary
        typo. Each yields what could be read, with a note naming the file.
        """

        from ..project_definitions import ProjectDefinitions
        from . import catalog as catalog_mod

        try:
            loaded, resolved = self._load(), self._resolution()
        except SemanticViewDependencyError as exc:
            return ProjectDefinitions(notes=[str(exc)])
        except (ImportError, OSError, ValueError) as exc:
            return ProjectDefinitions(
                notes=[f"Semantic View files could not be read: {exc}"]
            )
        if loaded.empty:
            return ProjectDefinitions(notes=loaded.notes())
        return catalog_mod.definitions(loaded, resolved, notes=loaded.notes())

    # --- the catalog channel ---------------------------------------------------

    def semantic_catalog(self) -> SemanticCatalogView:
        """The views as a read catalog.

        **Raises where the declaration channel degrades**: a set of files none of
        which could be read is the answer to the caller's question, and an empty
        catalog would read as "this layer declares nothing".
        """

        from . import catalog as catalog_mod

        loaded = self._load()
        if loaded.errors and loaded.empty:
            listed = "\n  ".join(d.render() for d in loaded.errors)
            raise ProjectError(
                "the configured Semantic View files could not be read as a semantic "
                f"layer:\n  {listed}"
            )
        return catalog_mod.semantic_catalog(
            loaded, self._resolution(), notes=loaded.notes()
        )

    # --- the snapshot channel --------------------------------------------------

    def semantic_layer(self) -> SemanticLayerSnapshot:
        """The views' fingerprint. Degrades the way :meth:`declared_definitions`
        does, for the same reason."""

        from ..maintain.snapshot import SemanticLayerSnapshot
        from . import snapshot as snapshot_mod

        try:
            loaded, resolved = self._load(), self._resolution()
        except SemanticViewDependencyError as exc:
            return SemanticLayerSnapshot(notes=[str(exc)])
        except (ImportError, OSError, ValueError) as exc:
            return SemanticLayerSnapshot(
                notes=[f"Semantic View files could not be read: {exc}"]
            )
        return snapshot_mod.semantic_layer(loaded, resolved)

    # --- native semantic-document authoring (#1292) ----------------------------

    def semantic_edit_view(self) -> SemanticDocumentView:
        """The configured files' bytes and hashes, keyed as configured."""

        from .authoring import load_edit_view

        return load_edit_view(self)

    def semantic_editing_surface(self) -> list[str]:
        """Exactly the configured files, relative to the repository. No
        directory is listed, so nothing beside a configured file is inside the
        surface."""

        from .authoring import editing_surface

        return editing_surface(self)

    def write_semantic_edits(
        self, edits: Any, *, confirmed: bool = False
    ) -> ApplyResult:
        """Write the authored bytes, all or none, refusing a stale pin."""

        ensure_available()
        from .authoring import write_edits

        return write_edits(self, list(edits), confirmed=confirmed)

    def validate_semantic_edits(
        self, edits: Any, mode: str, *, cache: Any = None
    ) -> SemanticEditValidation:
        """Validate the files these edits would leave, as the reader reads them.

        ``cache`` is accepted and not read: grounding authored views against the
        warehouse is `maintain`'s job, not authoring's.
        """

        ensure_available()
        from .authoring import validate_plan

        return validate_plan(self, list(edits), mode)

    # --- the derivation channel (#927, #1292) -----------------------------------

    def declaration_models(self, documents: dict[str, str]) -> dict[str, set[str]]:
        """Logical tables in ``documents`` and the members each exposes (#1030)."""

        from .derivation import declaration_models as _models

        return _models(documents)

    def derive_semantic(self, request: Any) -> Any:
        """Declared marts as logical tables; see :mod:`.derivation`.

        Reads only the request and the configured files, and validates with the
        checks authoring runs, so no Snowflake account or warehouse is opened.
        """

        ensure_available()
        from .derivation import derive

        return derive(self, request)

    def _load(self) -> LoadResult:
        """This command's validated read of the configured files, memoized."""
        if self._loaded is None:
            ensure_available()
            from .loader import load_files

            self._loaded = load_files(self.repo_root, self.files)
        return self._loaded

    def _resolution(self) -> Resolution:
        """This command's resolution of the loaded set, memoized."""
        if self._resolved is None:
            ensure_available()
            from .catalog import resolution

            self._resolved = resolution(self._load(), self.connector)
        return self._resolved


def build_semantic_layer(context: SemanticSourceContext) -> SemanticViewSemanticLayer:
    """The factory :data:`..config.SEMANTIC_SOURCE_FACTORIES` names for Snowflake
    Semantic Views."""

    return SemanticViewSemanticLayer.from_context(context)
