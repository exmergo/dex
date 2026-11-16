"""Snowflake Semantic Views read from the account alone
(``semantic.deployment: snowflake_account``, #1357).

The Snowflake counterpart of the hosted dbt Cloud deployment, and like it read by
``explore`` only. The catalog is every semantic view the source allowlist's scopes
hold: a warehouse-free ``SHOW SEMANTIC VIEWS`` per scope, then
``SYSTEM$READ_YAML_FROM_SEMANTIC_VIEW`` per view, each read-back validated by the
reader that reads a repository file. No file is needed.

Two differences from dbt Cloud, both stated on every result:

1. **Execution stays with dex.** dex renders ``SEMANTIC_VIEW(...)`` and runs it
   through its own Snowflake connector, so ``execution`` is ``dex`` and the
   cost guard, the source allowlist and the PII gate apply exactly as under
   ``local``. Only the catalog source changes.
2. **There is no reviewed definition to hold the deployment to.** Under
   ``local`` a governed query answers with the repository's definition or not at
   all. Here the deployed definition is the only one, so the trust reverses: the
   account is the source of truth, and every result says the definition was read
   from the account and not checked against a file. A configured file
   describing a deployed view is named on that view, never silently passed
   over: ``--local`` holds the deployment to it.

The steps a governed query takes are :mod:`.semantic_views`'s, in the same
order, except that the deployed view is located and read *first* (its tokens
cannot be resolved without it), and is not compared with anything. Both reads
are answered by Snowflake's cloud-services layer without a warehouse, so the PII
gate still refuses before any warehouse statement, and nothing is priced or
spent before it.
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
from .model import SemanticQuery
from .semantic_views import SEMANTIC_VIEW_CATALOG_GAPS, LocalSemanticViewLayer

__all__ = ["ACCOUNT_DEPLOYMENT", "AccountReader", "AccountSemanticViewLayer"]

#: The `semantic.deployment` this layer answers for.
ACCOUNT_DEPLOYMENT = "snowflake_account"

#: ``(list_semantic_views, read_semantic_view_yaml)``: what SHOW lists over the
#: allowlist's scopes, and one view's read-back. The adapter's two methods, or a
#: seeded pair in a test.
AccountReader = tuple[Callable[[], list[str]], Callable[[str], str]]

_SPELLING = (
    "a name a configured repository file also declares is spelled as the file "
    "spells it, and any other as Snowflake returns it (an unquoted name comes "
    "back upper-cased); tokens match ignoring case either way"
)


class AccountSemanticViewLayer(LocalSemanticViewLayer):
    """Every semantic view the source allowlist holds on the account."""

    name = ACCOUNT_DEPLOYMENT
    vendor = "snowflake"
    deployment = ACCOUNT_DEPLOYMENT
    execution = EXECUTION_DEX
    catalog_gaps = SEMANTIC_VIEW_CATALOG_GAPS
    #: ``list --deployed`` is accepted: this catalog already is the deployed one.
    reads_deployed = True
    metricless_queries = True
    #: Where the definitions behind every result came from, said structurally.
    definition_source = "account"
    descriptor = BackendDescriptor(
        name=name,
        vendor=vendor,
        deployment=deployment,
        execution=execution,
        catalog_gaps=SEMANTIC_VIEW_CATALOG_GAPS,
    )

    def __init__(
        self,
        source: Any = None,
        engine: Any = None,
        *,
        reader: AccountReader | None = None,
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
    def from_engine(cls, engine: Any) -> AccountSemanticViewLayer:
        """Build from the engine; the repository's files are optional."""

        source = None
        if engine.repo_root is not None:
            try:
                source = engine.semantic_catalog_source()
            except (DexError, ValueError, OSError):
                source = None
        return cls(source, engine)

    # ---- the account read --------------------------------------------------

    def _account(self) -> Any:
        """This command's read of the account, memoized."""

        if self._read is None:
            from ...semantic_views.deployed import read_account

            if self._reader is not None:
                list_views, read_yaml = self._reader
            else:
                self._supported_connector(
                    f"`{self._command}` under semantic.deployment: snowflake_account",
                    advice="The catalog is read from the account; set "
                    "connector: snowflake, or use semantic.deployment: local to "
                    "read the repository's files",
                )
                # Inside another command (`explore relationships|map
                # --use-hosted-semantic-layer`) the engine already holds that
                # command's adapter. Opening one through the engine would
                # rebuild its cost gate, settling the outer command's gate
                # early and stamping its result with this read's empty one; the
                # two reads here are free, so they ride the open adapter.
                adapter = getattr(self._dex, "_adapter_instance", None)
                if adapter is None:
                    adapter = self._governed_sql().adapter(self._command)
                list_views = adapter.list_semantic_views
                read_yaml = adapter.read_semantic_view_yaml
            self._read = read_account(
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
            from ...semantic_views.loader import LoadResult

            read = self._account()
            self._held(read)
            self._load_result = LoadResult(views=list(read.views))
        return self._load_result

    def _held(self, read: Any) -> None:
        """Refuse, by name, a view a request names that the account read could
        not answer for, before a token is resolved against what is left."""

        for name in sorted(self._wanted or ()):
            coverage = read.coverage_of(name)
            if coverage is None:
                raise SemanticBackendError(
                    f"no semantic view named '{name}' is in the source allowlist's "
                    "scopes on this account (semantic.deployment: "
                    "snowflake_account reads the views SHOW SEMANTIC VIEWS lists "
                    "there). List them with `explore semantic list`, or add the "
                    "schema holding it to snowflake.databases"
                )
            if coverage.status == "refused":
                raise SemanticQueryRefusedError(
                    f"refused: semantic view '{name}' could not be read from the "
                    f"account: {'; '.join(coverage.reasons)}"
                )

    def _resolution(self) -> Any:
        if self._resolved is None:
            from ...semantic_views import catalog as catalog_mod

            self._resolved = catalog_mod.resolution(self._loaded(), self._connector())
        return self._resolved

    def _catalog_view(self):
        if self._view is None:
            from ...semantic_views import catalog as catalog_mod

            self._view = catalog_mod.semantic_catalog(
                self._loaded(), self._resolution()
            )
        return self._view

    def _definitions(self) -> Any:
        from ...semantic_views import catalog as catalog_mod

        return catalog_mod.definitions(self._loaded(), self._resolution())

    def _connector(self) -> str | None:
        if self._dex is None and self._reader is not None:
            return "snowflake"
        return super()._connector()

    # ---- discovery ---------------------------------------------------------

    def list_definitions(self, *, deployed: bool = False) -> SemanticCatalog:
        """Every semantic view the allowlist holds, each with its coverage."""

        from ...semantic_views.deployed import ACCOUNT_DISCLOSURE

        try:
            read = self._account()
            view = self._catalog_view()
        finally:
            if self._governed is not None:
                self._governed.release()
        notes = [ACCOUNT_DISCLOSURE, _SPELLING]
        if not read.listed:
            notes.append(
                "no semantic view is in the source allowlist's scopes on this "
                "account, so this catalog is empty; SHOW SEMANTIC VIEWS listed "
                "nothing (a scope this role cannot list contributes nothing)"
            )
        if any(c.status != "full" for c in read.coverage):
            notes.append(
                "coverage is not full for every view: see `coverage`. A limited "
                "view reads members it does not carry, and a refused one is not "
                "in this catalog at all"
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
                "snowflake_account the catalog already is the deployed one"
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

        from ...semantic_views.deployed import ACCOUNT_DISCLOSURE

        return [ACCOUNT_DISCLOSURE]

    # ---- query -------------------------------------------------------------

    def query(self, q: SemanticQuery):
        """One governed query over the account's views."""

        self._command = "explore semantic query"
        self._wanted = _views_named([*q.metrics, *q.group_by])
        try:
            return super().query(q)
        finally:
            if self._governed is not None:
                self._governed.release()

    def values(self, dimension: str, metrics: list[str]):
        """One dimension's values over the account's views."""

        self._command = "explore semantic values"
        self._wanted = _views_named([dimension, *metrics])
        try:
            return super().values(dimension, metrics)
        finally:
            if self._governed is not None:
                self._governed.release()

    def _deployed(self, plan: Any, command: str) -> tuple[str, list[str]]:
        """The deployed view the plan names, held to nothing: the account is
        the source of truth here. Refuses a query relying on a member the
        read-back reads and does not carry."""

        from ...semantic_views.deployed import unchecked_file_reason
        from ...semantic_views.query import row_inputs

        read = self._account()
        view = plan.view
        key = view.name.lower()
        where = read.locations.get(key)
        if where is None:  # pragma: no cover - _held refused it already
            raise SemanticQueryRefusedError(
                f"refused: semantic view '{view.name}' is not on the account"
            )
        coverage = read.coverage_of(view.name)
        members = [item.member for item in plan.members]
        for item in plan.members:
            members.extend(row_inputs(view, item.member))
        if coverage is not None and coverage.status == "limited":
            unread = {e.lower() for e in _unresolved(read, key)}
            relied = sorted(
                {_element(m) for m in members if _element(m).lower() in unread}
            )
            if relied:
                raise SemanticQueryRefusedError(
                    f"refused: this query relies on {', '.join(relied)} of the view "
                    f"deployed as {where}, which reads members its read-back does "
                    "not carry (a private member this role cannot read back, or a "
                    "base column dex cannot vouch for), so the PII gate could not "
                    "screen what it returns"
                )
        notes = [f"executed against the semantic view deployed as {where}"]
        if coverage is not None and coverage.status == "limited":
            notes.extend(coverage.reasons)
        if file := read.files.get(key):
            notes.append(unchecked_file_reason(file))
        return where, notes


def _views_named(tokens: list[str]) -> set[str] | None:
    """The views a request's tokens name (their first segment), or None to read
    every view when none can be told."""

    named = {token.split(".", 1)[0].strip().lower() for token in tokens if "." in token}
    named.discard("")
    return named or None


def _element(member: Any) -> str:
    """Where a member sits, as the reader's diagnostics name it."""

    if member.table is None:
        return f"metrics['{member.name}']"
    listing = {
        "dimension": "dimensions",
        "time_dimension": "time_dimensions",
        "fact": "facts",
        "metric": "metrics",
    }[member.kind]
    return f"tables['{member.table}'].{listing}['{member.name}']"


def _unresolved(read: Any, key: str) -> list[str]:
    """The elements the view's read-back reads without carrying."""

    return list(read.unresolved.get(key, []))
