"""Native Apache Ossie documents behind the semantic-layer seam.

Ossie is a **semantic layer**, not a transformation project. Its documents own
semantic metadata and declared relationships; they do not own dbt models,
snapshots, or writeback.

Ossie is configured through ``semantic.vendor: ossie`` and
``semantic.ossie.files``, whether or not the repository also has a dbt project.
The semantic backend builds this reader from those coordinates; dbt, when
present, remains responsible for the transformation-project tiers.

**The tier reached today is tier 2 plus the catalog channel** (#409): this
class also answers ``transform_layer()``/``semantic_layer()``, the snapshot
fingerprints ``maintain snapshot``/``maintain check`` diff against a baseline,
so a repository whose semantic vendor is Ossie gets a real drift baseline for
its declared keys and relationships. Tier 3 (a preservation contract for
writing documents back) is not claimed here: growing the write methods would
claim them to ``maintain reconcile`` as well, and that is its own work.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..errors import ConfigurationError, ProjectError, RepoRootRequiredError
from . import catalog as catalog_mod
from .loader import DOCUMENT_SUFFIXES, LoadResult, OssieDependencyError, load_documents

if TYPE_CHECKING:
    from ..adapters.project import ProjectContext
    from ..maintain.snapshot import SemanticLayer, TransformLayer
    from ..project_definitions import ProjectDefinitions
    from ..semantic_catalog import SemanticCatalogView

__all__ = [
    "FORMAT_NAME",
    "OssieProject",
    "OssieSemanticLayer",
    "build_semantic_layer",
]

FORMAT_NAME = "ossie"


class OssieSemanticLayer:
    """The native Ossie semantic layer.

    Holds its coordinates and reads nothing until asked, because construction is
    cheap by contract: dex builds one project per command rather than holding
    one, so a factory that parsed would pay for every command that never looks at
    a semantic layer.

    **One instance is meant to live for one command.** It memoizes the validated
    document set, and a document on disk is an artifact a future `semantic ossie
    apply` will rewrite, so an instance held across commands would serve the
    documents as they were before the write.
    """

    # This identifies the semantic format; it is not a transformation project
    # format and is never selected by project.format.
    name = FORMAT_NAME

    def __init__(
        self,
        repo_root: Path | str,
        files: Sequence[str],
        connector: str | None = None,
    ) -> None:
        self.repo_root = Path(repo_root)
        self.files = list(files)
        self.connector = connector
        self._loaded: LoadResult | None = None

    @classmethod
    def from_context(cls, context: ProjectContext) -> OssieSemanticLayer:
        """Build from configuration, refusing coordinates it cannot honor.

        Every refusal here is about a committed config line, so each one names
        the line to fix. Nothing is read: a file that is named but absent, or
        named and malformed, is a *document* problem and is reported through
        `definitions()` and `semantic_catalog()`, which are the channels whose
        callers can degrade. Refusing it here would make `explore map` on a raw
        warehouse fail because of a typo in a semantic-layer path.
        """

        if context.repo_root is None:
            raise RepoRootRequiredError(
                "the ossie project format needs a repo root: its documents are "
                "git-reviewable files in the repository, so build the engine "
                "with DexEngine.from_repo(repo_root) or pass repo_root="
            )
        options = dict(context.options or {})
        files = _files(options.pop("files", None))
        if unknown := sorted(options):
            named = ", ".join(unknown)
            raise ConfigurationError(
                f"the ossie project format takes one option, `files`, and got: "
                f"{named}. An option dex accepted and ignored would be "
                "indistinguishable from one it honored, right up until dex was "
                "reading a different project than the configuration named"
            )
        return cls(Path(context.repo_root), files, context.connector)

    # --- tier 1 ---------------------------------------------------------------

    def declared_definitions(self) -> ProjectDefinitions:
        """What the documents declare: dataset keys, joins, and relations.

        **This must not raise**, and here that is a promise with real work
        behind it rather than a formality. Exploration runs against raw
        warehouses where a semantic layer is absent, and every failure this
        format has (a missing file, a path escaping the repository, unparseable
        YAML, a document that fails the schema, the `[ossie]` extra not
        installed) is a state a user will reach. Each one yields the empty view
        with a note naming the file and the fix.
        """

        from ..project_definitions import ProjectDefinitions

        try:
            loaded = self._load()
        except OssieDependencyError as exc:
            return ProjectDefinitions(notes=[str(exc)])
        except (OSError, ValueError) as exc:
            return ProjectDefinitions(
                notes=[f"native Ossie documents could not be read: {exc}"]
            )
        if not loaded.documents:
            return ProjectDefinitions(notes=loaded.notes())
        return catalog_mod.definitions(
            loaded.documents, connector=self.connector, notes=loaded.notes()
        )

    def definitions(self) -> ProjectDefinitions:
        """Deprecated alias for :meth:`declared_definitions`."""

        return self.declared_definitions()

    # --- the catalog channel, beside tier 2 -----------------------------------

    def semantic_catalog(self) -> SemanticCatalogView:
        """The documents as a read catalog.

        **Raises where tier 1 degrades**, and the asymmetry is the contract
        rather than an inconsistency. A caller here asked what the semantic layer
        contains, so an unreadable document is the answer to their question and
        an empty catalog would read as "this layer declares nothing". A caller on
        tier 1 asked about a warehouse and happens to have a project; there, the
        same condition is a footnote.
        """

        loaded = self._load()
        if loaded.errors and not loaded.documents:
            listed = "\n  ".join(d.render() for d in loaded.errors)
            raise ProjectError(
                "the configured native Ossie documents could not be read as a "
                f"semantic layer:\n  {listed}"
            )
        return catalog_mod.semantic_catalog(
            loaded.documents, connector=self.connector, notes=loaded.notes()
        )

    # --- tier 2 -----------------------------------------------------------

    def transform_layer(self) -> TransformLayer:
        """The document set's own fingerprint (#409): file hashes and nothing
        else, since Ossie declares no build step. See
        :func:`.snapshot.transform_layer` for what that means for the rest of
        the shape.

        **This must not raise**, matching tier 1's promise rather than
        dbt's tier-2 contract of raising when there is no project: an unreadable
        document is exactly the ordinary condition tier 1 already degrades
        around, and a repository whose semantic vendor is Ossie should not
        lose its whole drift baseline because one file could not be hashed.
        """

        from . import snapshot as snapshot_mod

        return snapshot_mod.transform_layer(self.repo_root, self.files)

    def semantic_layer(self) -> SemanticLayer:
        """The documents' own fingerprint (#409): named definitions, declared
        keys, and composite relationships, each with a content hash.

        Degrades the same way :meth:`declared_definitions` does, for the same
        reason: every failure this format has is a state a user reaches by an
        ordinary typo or a missing extra, not a wiring mistake that should
        propagate.
        """

        from ..maintain.snapshot import SemanticLayer as _SemanticLayerSnapshot
        from . import snapshot as snapshot_mod

        try:
            loaded = self._load()
        except OssieDependencyError as exc:
            return _SemanticLayerSnapshot(notes=[str(exc)])
        except (OSError, ValueError) as exc:
            return _SemanticLayerSnapshot(
                notes=[f"native Ossie documents could not be read: {exc}"]
            )
        return snapshot_mod.semantic_layer(loaded, connector=self.connector)

    def _load(self) -> LoadResult:
        if self._loaded is None:
            self._loaded = load_documents(
                self.repo_root, self.files, connector=self.connector
            )
        return self._loaded


# Historical public name for integrations that construct the reader directly.
OssieProject = OssieSemanticLayer


def build_semantic_layer(context: ProjectContext) -> OssieSemanticLayer:
    """Build the native reader from the semantic configuration coordinates."""

    return OssieSemanticLayer.from_context(context)


def _files(value: Any) -> list[str]:
    """The configured document list, validated as coordinates.

    Shape, suffix, confinement and duplication are checked here as well as in the
    loader, and the duplication is deliberate: this runs against a committed
    config line and can name it, while the loader runs against whatever it is
    handed and has to survive a caller that built its coordinates elsewhere.
    """

    if value is None:
        raise ConfigurationError(
            "the Ossie semantic reader needs `files`: native documents named "
            "relative to the repository root. Configure them under "
            "`semantic.ossie.files` in .dex/config.yml"
        )
    if isinstance(value, str) or not isinstance(value, Sequence | Mapping):
        raise ConfigurationError(
            "`files` for the ossie project format is a list of document paths, "
            f"and got {type(value).__name__}. One document is a list of one"
        )
    if isinstance(value, Mapping):
        raise ConfigurationError(
            "`files` for the ossie project format is a list of document paths, "
            "and got a mapping"
        )
    files = [str(item) for item in value]
    if not files:
        raise ConfigurationError(
            "`files` for the ossie project format is empty. A format configured "
            "to read no documents declares nothing, which is what leaving "
            "`project.format` unset already does"
        )
    listed = ", ".join(DOCUMENT_SUFFIXES)
    for name in files:
        if not name.endswith(DOCUMENT_SUFFIXES):
            raise ConfigurationError(
                f"'{name}' is not a native Ossie document: they are named "
                f"{listed}. The suffix is what tells dex a repository file is "
                "one of the documents this format owns"
            )
        if Path(name).is_absolute() or ".." in Path(name).parts:
            raise ConfigurationError(
                f"'{name}' has to name a file inside the repository, written "
                "relative to its root. dex reads native Ossie documents only "
                "from the repository it was pointed at"
            )
    if duplicated := sorted({n for n in files if files.count(n) > 1}):
        raise ConfigurationError(
            f"the ossie project format was given the same document twice: "
            f"{', '.join(duplicated)}. Reading one document twice declares "
            "everything in it twice, which is a duplicate-name refusal rather "
            "than a larger layer"
        )
    return files
