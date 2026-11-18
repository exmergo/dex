"""Databricks Metric Views read from Unity Catalog alone
(``semantic.deployment: databricks_workspace``, #1364).

The Databricks counterpart of Snowflake's ``snowflake_account``, and like it read
by ``explore`` only. The catalog is every Metric View the source allowlist's
scopes hold: Unity Catalog's ``tables.list`` per scope, then ``tables.get`` per
view, whose ``view_definition`` is validated by the reader that reads a
repository file. No file is needed.

Two differences from dbt Cloud, both stated on every result:

1. **Execution stays with dex.** dex renders ``SELECT ... MEASURE(...)`` and runs
   it through its own Databricks connector, so ``execution`` is ``dex`` and the
   cost guard, the source allowlist and the PII gate apply exactly as under
   ``local``. Only the catalog source changes.
2. **There is no reviewed definition to hold the deployment to.** Under
   ``local`` a governed query answers with the repository's definition or not at
   all. Here the deployed definition is the only one, so the trust reverses:
   Unity Catalog is the source of truth, and every result says the definition
   was read from it and not checked against a file. A configured file describing
   a deployed view is named on that view, never silently passed over:
   ``--local`` holds the deployment to it.

The steps a governed query takes are :mod:`.metric_views`'s, in the same order,
except that the deployed view is located and read *first* (its tokens cannot be
resolved without it), and is not compared with anything. Both reads are Unity
Catalog REST metadata, so no SQL warehouse is woken before the PII gate has
spoken, and nothing is priced or spent before it.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ...errors import DexError
from .backend import (
    EXECUTION_DEX,
    BackendDescriptor,
    SemanticBackendError,
    SemanticQueryRefusedError,
)
from .catalog import SemanticCatalog
from .metric_views import METRIC_VIEW_CATALOG_GAPS, LocalMetricViewLayer
from .model import SemanticQuery

__all__ = ["WORKSPACE_DEPLOYMENT", "WorkspaceMetricViewLayer", "WorkspaceReader"]

#: The `semantic.deployment` this layer answers for.
WORKSPACE_DEPLOYMENT = "databricks_workspace"

#: ``(list_metric_views, read_metric_view_yaml)``: what Unity Catalog lists over
#: the allowlist's scopes, and one view's read-back. The adapter's two methods,
#: or a seeded pair in a test.
WorkspaceReader = tuple[Callable[[], list[str]], Callable[[str], str]]

_NAMING = (
    "each view is named by its Unity Catalog name (which Unity Catalog stores "
    "lower-cased), and its tokens are `<view>.<field>` and `<view>.<measure>`, "
    "matched ignoring case"
)


class WorkspaceMetricViewLayer(LocalMetricViewLayer):
    """Every Metric View the source allowlist holds in Unity Catalog."""

    name = WORKSPACE_DEPLOYMENT
    vendor = "databricks"
    deployment = WORKSPACE_DEPLOYMENT
    execution = EXECUTION_DEX
    catalog_gaps = METRIC_VIEW_CATALOG_GAPS
    #: ``list --deployed`` is accepted: this catalog already is the deployed one.
    reads_deployed = True
    #: Where the definitions behind every result came from, said structurally.
    #: The value `snowflake_account` uses too, so a host checks one value for
    #: "a deployed definition no reviewed file vouched for", whatever the vendor.
    definition_source = "account"
    descriptor = BackendDescriptor(
        name=name,
        vendor=vendor,
        deployment=deployment,
        execution=execution,
        catalog_gaps=METRIC_VIEW_CATALOG_GAPS,
    )

    def __init__(
        self,
        source: Any = None,
        engine: Any = None,
        *,
        reader: WorkspaceReader | None = None,
    ) -> None:
        """``source`` is the configured repository files, or None; they are read
        only to name the views they describe. ``reader`` replaces the adapter's
        two reads, for a caller with no engine."""
        super().__init__(source, engine)
        self._reader = reader
        self._command = "explore semantic list"
        self._wanted: set[str] | None = None
        self._read: Any = None
        self._load_result: Any = None
        self._resolved: Any = None

    @classmethod
    def from_engine(cls, engine: Any) -> WorkspaceMetricViewLayer:
        """Build from the engine; the repository's files are optional."""

        source = None
        if engine.repo_root is not None:
            try:
                source = engine.semantic_catalog_source()
            except (DexError, ValueError, OSError):
                source = None
        return cls(source, engine)

    # ---- the Unity Catalog read ----------------------------------------------

    def _workspace(self) -> Any:
        """This command's read of Unity Catalog, memoized."""

        if self._read is None:
            from ...metric_views.deployed import read_workspace

            if self._reader is not None:
                list_views, read_yaml = self._reader
            else:
                self._supported_connector(
                    f"`{self._command}` under semantic.deployment: "
                    "databricks_workspace",
                    advice="The catalog is read from Unity Catalog; set "
                    "connector: databricks, or use semantic.deployment: local to "
                    "read the repository's files",
                )
                # Inside another command (`explore relationships|map
                # --use-hosted-semantic-layer`) the engine already holds that
                # command's adapter. Opening one through the engine would
                # rebuild its cost gate, settling the outer command's gate early
                # and stamping its result with this read's empty one; the two
                # reads here are free REST metadata, so they ride the open one.
                adapter = getattr(self._dex, "_adapter_instance", None)
                if adapter is None:
                    adapter = self._governed_sql().adapter(self._command)
                list_views = adapter.list_metric_views
                read_yaml = adapter.read_metric_view_yaml
            self._read = read_workspace(
                list_views(), read_yaml, self._repository_views(), names=self._wanted
            )
        return self._read

    def _repository_views(self) -> list[Any]:
        """The configured files' views, to name the deployed views they
        describe. Never compared, and never a reason to fail."""

        if self._source is None or not getattr(self._source, "files", None):
            return []
        try:
            return list(self._source.loaded().views)
        except (DexError, ValueError, OSError):
            return []

    def _loaded(self):
        if self._load_result is None:
            from ...metric_views.loader import LoadResult

            read = self._workspace()
            self._held(read)
            self._load_result = LoadResult(views=list(read.views))
        return self._load_result

    def _held(self, read: Any) -> None:
        """Refuse, by name, a view a request names that the read could not
        answer for, before a token is resolved against what is left."""

        for name in sorted(self._wanted or ()):
            coverage = read.coverage_of(name)
            if coverage is None:
                raise SemanticBackendError(
                    f"no metric view named '{name}' is in the source allowlist's "
                    "scopes in Unity Catalog (semantic.deployment: "
                    "databricks_workspace reads the Metric Views tables.list "
                    "finds there). List them with `explore semantic list`, or add "
                    "the schema holding it to databricks.catalogs"
                )
            if coverage.status == "refused":
                raise SemanticQueryRefusedError(
                    f"refused: metric view '{name}' could not be read from Unity "
                    f"Catalog: {'; '.join(coverage.reasons)}"
                )

    def _resolution(self) -> Any:
        if self._resolved is None:
            from ...metric_views import catalog as catalog_mod

            self._resolved = catalog_mod.resolution(self._loaded(), self._connector())
        return self._resolved

    def _catalog_view(self):
        if self._view is None:
            from ...metric_views import catalog as catalog_mod

            self._view = catalog_mod.semantic_catalog(
                self._loaded(), self._resolution()
            )
        return self._view

    def _definitions(self) -> Any:
        from ...metric_views import catalog as catalog_mod

        return catalog_mod.definitions(self._loaded(), self._resolution())

    def _connector(self) -> str | None:
        if self._dex is None and self._reader is not None:
            return "databricks"
        return super()._connector()

    # ---- discovery ---------------------------------------------------------

    def list_definitions(self, *, deployed: bool = False) -> SemanticCatalog:
        """Every Metric View the allowlist holds, each with its coverage."""

        from ...metric_views.deployed import FULL, WORKSPACE_DISCLOSURE

        try:
            read = self._workspace()
            view = self._catalog_view()
        finally:
            if self._governed is not None:
                self._governed.release()
        notes = [WORKSPACE_DISCLOSURE, _NAMING]
        if not read.listed:
            notes.append(
                "no metric view is in the source allowlist's scopes in Unity "
                "Catalog, so this catalog is empty; tables.list listed nothing (a "
                "scope this identity cannot list contributes nothing)"
            )
        if any(c.status != FULL for c in read.coverage):
            notes.append(
                "coverage is not full for every view: see `coverage`. A limited "
                "view is missing what dex's reader refused in its read-back, and "
                "a refused one is not in this catalog at all"
            )
        if read.files:
            notes.append(
                "a configured repository file describes "
                f"{', '.join(sorted(read.files.values()))}: each such view is "
                "named in `coverage` with its file, and was not checked against "
                "it; `--local` holds the deployment to the file"
            )
        if deployed:
            notes.append(
                "--deployed changes nothing here: under semantic.deployment: "
                "databricks_workspace the catalog already is the deployed one"
            )
        return SemanticCatalog.from_view(
            view,
            self,
            notes=notes,
            source="account",
            coverage=[c.as_dict() for c in read.coverage],
        )

    def disclosure(self) -> list[str]:
        """The warnings every result of this layer carries."""

        from ...metric_views.deployed import WORKSPACE_DISCLOSURE

        return [WORKSPACE_DISCLOSURE]

    # ---- query -------------------------------------------------------------

    def query(self, q: SemanticQuery):
        """One governed query over the deployed views."""

        self._command = "explore semantic query"
        self._wanted = _views_named([*q.metrics, *q.group_by])
        try:
            return super().query(q)
        finally:
            if self._governed is not None:
                self._governed.release()

    def values(self, dimension: str, metrics: list[str]):
        """One field's values over the deployed views."""

        self._command = "explore semantic values"
        self._wanted = _views_named([dimension, *metrics])
        try:
            return super().values(dimension, metrics)
        finally:
            if self._governed is not None:
                self._governed.release()

    def _deployed(self, plan: Any, command: str) -> tuple[str, list[str]]:
        """The deployed view the plan names, held to nothing: Unity Catalog is
        the source of truth here. Refuses a query whose reach dex cannot read,
        since that is also what the PII gate and the pricing follow."""

        from ...metric_views.deployed import LIMITED, relied_on, unchecked_file_reason

        read = self._workspace()
        view = plan.view
        key = view.name.lower()
        where = read.locations.get(key)
        if where is None:  # pragma: no cover - _held refused it already
            raise SemanticQueryRefusedError(
                f"refused: metric view '{view.name}' is not in Unity Catalog"
            )
        reliance = relied_on(
            view,
            [item.member for item in plan.members],
            [item.member for item in plan.fields if item.grain is not None],
        )
        if reliance is None:
            raise SemanticQueryRefusedError(
                f"refused: dex could not read what this query relies on in the "
                f"metric view deployed as {where} (an expression it reaches does "
                "not parse, or names a member or join the read-back does not "
                "carry)"
            )
        notes = [
            f"executed against the metric view deployed as {where}, read back "
            "from Unity Catalog and not held to any repository file"
        ]
        coverage = read.coverage_of(view.name)
        if coverage is not None and coverage.status == LIMITED:
            notes.extend(coverage.reasons)
        if file := read.files.get(key):
            notes.append(unchecked_file_reason(file))
        if view.filter:
            notes.append(
                f"view '{view.name}' declares a `filter`, which Databricks applies "
                "to every query of it; dex does not repeat it"
            )
        return where, notes


def _views_named(tokens: list[str]) -> set[str] | None:
    """The views a request's tokens name (their first segment), or None to read
    every view when none can be told."""

    named = {token.split(".", 1)[0].strip().lower() for token in tokens if "." in token}
    named.discard("")
    return named or None
