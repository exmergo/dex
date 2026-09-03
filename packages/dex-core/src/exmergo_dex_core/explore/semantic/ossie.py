"""The catalog-only semantic backend for native Apache Ossie documents.

Thin on purpose. The reading is the project format's job and reaches this
backend through the same injected-format seam `LocalMetricFlowBackend` uses, so
what is left here is provenance, the capability declarations, and two refusals.

**The refusals are the honest answer, not a gap to be closed later by this
class.** Ossie specifies interchange metadata and not a portable query runtime:
there is no filter grammar, no join planning, no pagination, and no cost posture
to inherit. A backend that rendered a query anyway would be inventing execution
semantics and attributing them to the document's author. Upstream has an active
working group on a query language and a reference engine, so this refusal is
true today and is on the roadmap to become false; when it does, it becomes a
declared runtime adapter with its own governed contract rather than a loosening
here.
"""

from __future__ import annotations

from typing import Any

from ...errors import ProjectError
from .backend import EXECUTION_DEX, BackendDescriptor, SemanticBackendError
from .catalog import SemanticCatalog

# Fields of the neutral catalog that Ossie structurally cannot fill, named per
# element kind so a caller can tell a structural absence from a field the author
# left blank. This is the shape the shipped catalog contract polices, and the
# distinction it exists for: "the layer declares no label" and "this format has
# nowhere to put a label" read identically in a payload otherwise.
#
# The whole-concept absences are the interesting entries. Ossie has no measures
# and no entities at all, so every declared field of both kinds is unavailable
# rather than the kinds being quietly empty. `metrics.dimensions` is the one that
# changes behavior: it is why `--for-dimension` refuses instead of answering
# "no metric can be grouped that way", which would be a false statement about
# the layer rather than a small one.
OSSIE_CATALOG_GAPS: dict[str, list[str]] = {
    "semantic_models": ["model_ref", "agg_time_dimension", "primary_entity"],
    "metrics": [
        "dimensions",
        "input_measures",
        "composition",
        "filter",
        "time_axis",
        "queryable_granularities",
        "label",
    ],
    "dimensions": ["queryable_granularities"],
    "entities": ["name", "type", "label", "description", "roles"],
    "measures": [
        "name",
        "agg",
        "expr",
        "agg_time_dimension",
        "label",
        "description",
        "semantic_model",
        "column",
    ],
}

_NO_RUNTIME = (
    "Apache Ossie specifies interchange metadata, not a portable query runtime: "
    "it defines no filter grammar, no join planning, and no execution semantics, "
    "so dex has nothing to render a governed statement from"
)


class LocalOssieLayer:
    """The local native-Ossie semantic layer."""

    name = "local"
    vendor = "ossie"
    deployment = "local"
    execution = EXECUTION_DEX
    catalog_gaps = OSSIE_CATALOG_GAPS
    descriptor = BackendDescriptor(
        name=name,
        vendor=vendor,
        deployment=deployment,
        execution=execution,
        catalog_gaps=OSSIE_CATALOG_GAPS,
    )

    def __init__(self, project: Any) -> None:
        self._project = project

    @classmethod
    def from_engine(cls, engine: Any) -> LocalOssieLayer:
        """Build directly from semantic configuration, never a project format."""

        if engine.repo_root is None:
            raise SemanticBackendError(
                "reading native Apache Ossie documents needs a repository to "
                "read them from: they are git-reviewable files, so build the "
                "engine with DexEngine.from_repo(repo_root)"
            )
        from ...ossie import OssieSemanticLayer

        semantic = engine.config.semantic
        return cls(
            OssieSemanticLayer(
                engine.repo_root,
                semantic.ossie.files,
                engine.connector or engine.config.connector,
            )
        )

    def list_definitions(self) -> SemanticCatalog:
        project = self._project
        try:
            view = project.semantic_catalog()
        except ProjectError as exc:
            raise SemanticBackendError(str(exc)) from exc
        return SemanticCatalog.from_view(view, self)

    def declared_relationships(self) -> list[Any]:
        """The native declaration channel, including composite ordered pairs."""

        return self._project.declared_definitions().declared_relationships

    def query(self, _q: Any) -> Any:
        raise SemanticBackendError(
            f"{_NO_RUNTIME}. Read the layer with `explore semantic list`, then "
            "query the physical relations it names with `explore query`, which "
            "runs under the firewall and the cost guard"
        )

    def values(self, _dimension: str, _metrics: list[str]) -> Any:
        raise SemanticBackendError(
            f"{_NO_RUNTIME}, and no distinct-values API either. A dimension "
            "backed by a bare column names its relation and column in the "
            "catalog; profile that relation instead"
        )

    def filter_refs(self, _clauses: list[str]) -> list[str] | None:
        """No filter dialect to read, because there is no query to filter.

        `None` is the declining answer the contract defines, and it is reached
        only by a caller that got past `query`, which nothing does.
        """

        return None


# Compatibility name for integrations importing the original runtime seam.
LocalOssieBackend = LocalOssieLayer
