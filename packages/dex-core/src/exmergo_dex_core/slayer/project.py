"""SLayer model files behind the semantic-source seam.

SLayer is a **semantic layer**, not a transformation project, and it sits beside
one the way native Ossie does: `project.format: dbt` keeps the transformation
tiers, and under `semantic.vendor: slayer` the configured SLayer model files are
the repository's one semantic layer. The engine builds this source from
`semantic.slayer` through :data:`..config.SEMANTIC_SOURCE_FACTORIES`, so
MetricFlow semantic models are not read while SLayer is configured.

It implements the two source capabilities in :mod:`..semantic_source`:
:class:`~..semantic_source.SemanticCatalogSource` for the read catalog and
:class:`~..semantic_source.SemanticSnapshotSource` for the drift fingerprint,
plus the declaration channel (`declared_definitions()`) that carries declared
keys and joins to exploration, and :class:`~..edits.SemanticEditTarget` for
`semantic slayer define|update|plan`. What maps and what does not is in
:mod:`.catalog`; what authoring accepts is in :mod:`.authoring`.

SLayer never touches the warehouse under dex, and nothing here opens SLayer
storage: the coordinates are paths to committed model files and nothing else.
Datasource files and `priority.yaml` are refused at configuration, because a
datasource carries connection credentials.

Nothing in this module imports `slayer`. The reader that does, :mod:`.loader`,
is imported only on first read (and the renderer, :mod:`.render`, only by a
caller that renders a query), by a source that
:func:`.dependency.ensure_available` let construct, so a base install can
import and run every non-SLayer command.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..edits import SemanticEditValidation
from ..errors import ConfigurationError, ProjectError, RepoRootRequiredError
from .dependency import SlayerDependencyError, ensure_available

if TYPE_CHECKING:
    from ..cache import Dataset
    from ..edits import ApplyResult, SemanticDocumentView
    from ..maintain.drift import DriftFinding
    from ..maintain.reconcile import Proposal
    from ..maintain.snapshot import SemanticLayerSnapshot
    from ..project_definitions import ProjectDefinitions
    from ..semantic_catalog import SemanticCatalogView
    from ..semantic_source import SemanticSourceContext
    from ..transform.plans import PlanEdit
    from .loader import LoadedModel, LoadResult

__all__ = [
    "VENDOR_NAME",
    "SlayerSemanticLayer",
    "build_semantic_layer",
]

#: The semantic vendor this source answers for, as `semantic.vendor` spells it.
VENDOR_NAME = "slayer"


class SlayerSemanticLayer:
    """The SLayer semantic layer committed to this repository.

    Holds its coordinates and reads nothing, because construction is cheap by
    contract: dex builds a source per command, and one that read files here
    would pay for every command that never looks at a semantic layer. The one
    thing construction does check is the extra, through distribution metadata
    rather than an import, because a source built without it could answer
    nothing at all.

    **One instance is meant to live for one command.** It memoizes the validated
    model set, and a model file is an artifact a later command may rewrite, so
    an instance held across commands would serve the models as they were.
    """

    def __init__(
        self,
        repo_root: Path | str,
        base_dir: str,
        models: list[str],
        connector: str | None = None,
    ) -> None:
        self.repo_root = Path(repo_root)
        self.base_dir = base_dir
        self.models = list(models)
        self.connector = connector
        self._loaded: LoadResult | None = None

    @classmethod
    def from_context(cls, context: SemanticSourceContext) -> SlayerSemanticLayer:
        """Build from configuration, refusing coordinates it cannot honor.

        The options are checked by the same model the committed file is, so a
        host that builds this source directly cannot hand it a path the
        configuration would have refused. Every coordinate refusal names a
        config line, and no repository file is read. The extra is checked last,
        so a typo in a path is reported before an install is asked for.
        """

        from pydantic import ValidationError

        from ..config import SlayerSemanticConfig

        if context.repo_root is None:
            raise RepoRootRequiredError(
                "the SLayer semantic source needs a repo root: its models are "
                "git-reviewable files in the repository, so build the engine "
                "with DexEngine.from_repo(repo_root) or pass repo_root="
            )
        options = dict(context.options or {})
        if unknown := sorted(set(options) - set(SlayerSemanticConfig.model_fields)):
            raise ConfigurationError(
                "the SLayer semantic source takes `base_dir` and `models`, and "
                f"got: {', '.join(unknown)}. An option dex accepted and ignored "
                "would be indistinguishable from one it honored"
            )
        try:
            section = SlayerSemanticConfig(**options)
        except ValidationError as exc:
            problems = "; ".join(
                str(error.get("ctx", {}).get("error") or error["msg"])
                for error in exc.errors()
            )
            raise ConfigurationError(problems) from exc
        if not section.models or section.base_dir is None:
            raise ConfigurationError(
                "the SLayer semantic source needs semantic.slayer.base_dir and "
                "semantic.slayer.models: the SLayer model files to read, named "
                "relative to that storage root"
            )
        ensure_available()
        return cls(
            context.repo_root, section.base_dir, section.models, context.connector
        )

    def model_paths(self) -> list[Path]:
        """Every configured model file, resolved inside the repository."""

        root = self.repo_root / self.base_dir
        return [root / name for name in self.models]

    def validated_models(self) -> list[LoadedModel]:
        """Every model that passed validation, from this command's one load.

        What the renderer stores, so a query is planned over exactly the models
        the catalog reported, without reading a file a second time. A model with
        an error is absent here and named in the catalog's notes.
        """

        return list(self._load().models)

    # --- the declaration channel ----------------------------------------------

    def declared_definitions(self) -> ProjectDefinitions:
        """What the models declare: keys, joins, and the relations they read.

        **This must not raise.** Exploration runs against raw warehouses, and
        every failure this source has (a missing file, a path escaping the
        storage root, unparseable YAML, a model SLayer refuses) is a state a user
        reaches by an ordinary typo. Each yields what could be read, with a note
        naming the file and the fix.
        """

        from ..project_definitions import ProjectDefinitions
        from . import catalog as catalog_mod

        try:
            loaded = self._load()
        except SlayerDependencyError as exc:
            return ProjectDefinitions(notes=[str(exc)])
        except (ImportError, OSError, ValueError) as exc:
            return ProjectDefinitions(notes=[f"SLayer models could not be read: {exc}"])
        if not loaded.models:
            return ProjectDefinitions(notes=loaded.notes())
        return catalog_mod.definitions(
            loaded.models, connector=self.connector, notes=loaded.notes()
        )

    # --- the catalog channel ---------------------------------------------------

    def semantic_catalog(self) -> SemanticCatalogView:
        """The models as a read catalog.

        **Raises where the declaration channel degrades**: a caller here asked
        what the layer contains, so a set of model files none of which could be
        read is the answer to their question, and an empty catalog would read as
        "this layer declares nothing". Where some models read and some did not,
        the catalog answers for the ones that did and carries a note for each
        that did not.
        """

        from . import catalog as catalog_mod

        loaded = self._load()
        if loaded.errors and not loaded.models:
            listed = "\n  ".join(d.render() for d in loaded.errors)
            raise ProjectError(
                "the configured SLayer models could not be read as a semantic "
                f"layer:\n  {listed}"
            )
        return catalog_mod.semantic_catalog(
            loaded.models, connector=self.connector, notes=loaded.notes()
        )

    # --- the snapshot channel --------------------------------------------------

    def semantic_layer(self) -> SemanticLayerSnapshot:
        """The models' own fingerprint: each model, saved measure, declared key,
        and join, with a content hash.

        Degrades the way :meth:`declared_definitions` does, for the same reason.
        """

        from ..maintain.snapshot import SemanticLayerSnapshot
        from . import snapshot as snapshot_mod

        try:
            loaded = self._load()
        except SlayerDependencyError as exc:
            return SemanticLayerSnapshot(notes=[str(exc)])
        except (ImportError, OSError, ValueError) as exc:
            return SemanticLayerSnapshot(
                notes=[f"SLayer models could not be read: {exc}"]
            )
        return snapshot_mod.semantic_layer(loaded, connector=self.connector)

    # --- the schema channel ----------------------------------------------------

    def declared_schema_drift(
        self, current: list[Dataset], baseline: list[Dataset]
    ) -> tuple[list[DriftFinding], list[str]]:
        """The models held to dex's warehouse metadata, by SLayer's own rules.

        Degrades the way :meth:`declared_definitions` does: a model set that
        cannot be read compares nothing and says why.
        """

        try:
            loaded = self._load()
            from .schema import declared_schema_drift
        except SlayerDependencyError as exc:
            return [], [str(exc)]
        except (ImportError, OSError, ValueError) as exc:
            return [], [f"SLayer models were not compared to the warehouse: {exc}"]
        return declared_schema_drift(
            loaded, current, baseline, connector=self.connector
        )

    def handles_finding(self, finding: DriftFinding) -> bool:
        from .removal import CODES

        return finding.code in CODES

    def schema_reconcile(
        self, findings: list[DriftFinding]
    ) -> tuple[list[Proposal], list[PlanEdit], list[str]]:
        """Each removal the schema axis recorded, as an edit of its model file."""

        from .removal import proposals_and_edits

        own = [finding for finding in findings if self.handles_finding(finding)]
        if not own:
            return [], [], []
        contents = {
            name: source.content
            for name, source in self.semantic_edit_view().files.items()
        }
        return proposals_and_edits(own, contents)

    # --- native semantic-document authoring ------------------------------------

    def semantic_edit_view(self) -> SemanticDocumentView:
        """The configured model files' bytes and hashes, keyed as configured."""

        from .authoring import load_edit_view

        return load_edit_view(self)

    def semantic_editing_surface(self) -> list[str]:
        """Exactly the configured model files, relative to the storage root.

        No directory is listed, so nothing beside a configured model, a
        datasource file least of all, is inside the surface.
        """

        return list(self.models)

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
        """Validate the models these edits would leave, as SLayer reads them.

        ``cache`` is accepted and not read: grounding authored models against
        the warehouse is not part of this release.
        """

        ensure_available()
        from .authoring import validate_plan

        return validate_plan(self, list(edits), mode)

    # --- the derivation channel (#927) ------------------------------------------

    def declaration_models(self, documents: dict[str, str]) -> dict[str, set[str]]:
        """SLayer models in ``documents`` and the columns each exposes (#1030)."""

        from .derivation import declaration_models as _models

        return _models(documents)

    def derive_semantic(self, request: Any) -> Any:
        """Declared marts as SLayer model files; see :mod:`.derivation`.

        Reads only the request and the configured files, and validates with the
        same checks authoring runs, so no SLayer storage, datasource or warehouse
        is opened.
        """

        ensure_available()
        from .derivation import derive

        return derive(self, request)

    def _load(self) -> LoadResult:
        if self._loaded is None:
            ensure_available()
            from .loader import load_models

            self._loaded = load_models(self.repo_root, self.base_dir, self.models)
        return self._loaded


def build_semantic_layer(context: SemanticSourceContext) -> SlayerSemanticLayer:
    """The factory :data:`..config.SEMANTIC_SOURCE_FACTORIES` names for SLayer."""

    return SlayerSemanticLayer.from_context(context)
