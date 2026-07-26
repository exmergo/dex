"""The programmatic API: one object that owns a connection, a config, and a store.

``DexEngine`` is what a Python caller uses instead of shelling out to the CLI and
parsing stdout. It returns domain objects and :class:`~.results.Result` records;
the :class:`~.envelope.Envelope` never crosses this boundary. The CLI is the
first consumer of this API rather than a parallel implementation of it, which is
what keeps the two from drifting.

    from exmergo_dex_core import DexEngine

    with DexEngine(connector="duckdb", path="shop.duckdb") as eng:
        mapped = eng.map()
        rows = eng.query("select status, count(*) from orders group by status")

Nothing is written to disk by that snippet: the default store is a
:class:`~.storage.MemoryStore`, so importing this package into a project cannot
leave a ``.dex/`` directory behind as a side effect. Pass ``store=`` for anything
durable, and see :class:`~.storage.Store` for what a backend has to implement.

Two boundaries are worth naming, because both are easy to blur:

**Scratch state versus the project.** ``store`` holds the exploration cache, the
reconcile baseline, the ledgers, and the transform plans. Delete all of it and
nothing canonical is lost. ``repo_root`` locates the things that are files by
nature and stay git-reviewable: the dbt project, ``profiles.yml``, a DuckDB file,
the credential files a connector discovers. A backend choice moves the first and
never the second. A host that would rather not have credentials discovered from
its filesystem at all can supply the connection instead; see ``connection=`` and
:class:`~.connect.ConnectionSource`.

**One engine, one principal.** See the class docstring.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import command_args, connect
from .config import DexConfig, load_config
from .connect import ConnectionSource
from .results import ConnectResult
from .storage import FilesystemStore, MemoryStore, Store

if TYPE_CHECKING:
    from .adapters.base import Adapter
    from .explore.results import (
        ClusterResult,
        InventoryResult,
        MapResult,
        ProfileResult,
        QueryResult,
        RelationshipsResult,
        SemanticListResult,
        SemanticQueryResult,
    )
    from .maintain.results import DriftResult, ReconcileResult, SnapshotResult
    from .transform.plans import PlanEdit
    from .transform.results import (
        ApplyResult,
        BuildResult,
        DepsResult,
        InitResult,
        MacroListResult,
        MacroResult,
        PlanListResult,
        PlanResult,
    )


class DexEngine:
    """A connection, a configuration, and a place to keep scratch state.

    **Scope one engine to one principal and one session.** Never process-lived,
    never module-scope. Two facts make a shared engine unsafe: the exploration
    cache retains value ranges and exact counts for profiled columns, and the
    query firewall resolves every table reference against that cache, so its
    membership decides what a query is permitted to name and under whose PII
    policy. An engine reused across principals therefore both discloses one
    tenant's data shape to another and applies the wrong authorization surface.
    Constructing one per request is cheap and is the intended usage; the default
    ``MemoryStore`` makes that the safe default automatically. A host that wants
    state to outlive a request should implement :class:`~.storage.Store` over its
    own session or database, keyed by its own tenancy boundary, rather than
    reaching for a longer-lived engine.

    **An explicit config is never supplemented from disk.** Pass ``config=`` and
    no ``.dex/config.yml`` is read, including one that happens to sit above the
    working directory. Without that rule a container with a stray config anywhere
    up its tree would silently inherit another project's connector, budget, and
    PII overrides, which is a wrong-connection bug that presents as working
    software. :meth:`from_repo` is the constructor that does read from disk, and
    it says so in its name.

    **Supplying a connection supplies identity.** By default dex discovers the
    credential from process-ambient state, which is right for one person at a
    terminal and cannot express per-end-user access control in a process serving
    several: ambient is process-wide. Pass ``connection=`` (a
    :class:`~.connect.ConnectionSource`) and the host owns authentication, so dex
    knows the principal only as what ``capabilities()`` reports. Two things do not
    move with it. dex still builds the cost gate from ``store``, so the per-command
    ceiling and the cumulative session ceiling bind exactly as they do on a
    discovered connection; handing that to an integrator would let a fumbled
    ``session_spent`` silently disarm the brake in the deployment where a runaway
    agent loop is most expensive. And dex closes nothing it reached through the
    source, since the caller that opened a connection is the one still holding it.

    The connection opens lazily on first use and is held until :meth:`close`,
    because re-opening a Snowflake or Databricks session per call is expensive
    and can wake a warehouse. Holding it is not an invitation to share the
    engine; see above. With ``connection=`` that invariant sharpens rather than
    changes: an engine outliving a request now outlives a credential too. Each
    call still gets its own cost gate, because a gate is per-command state: it
    accumulates what one command charged, remembers whether that command was
    confirmed, and labels that command's ledger entries.

    ``scopes`` narrows the source allowlist and belongs to the engine rather than
    to a call, for the same reason the cache does: it is part of what this
    principal may see, and changing it is a different visibility posture, so it
    deserves a different engine.
    """

    def __init__(
        self,
        *,
        connector: str | None = None,
        path: str | None = None,
        config: DexConfig | None = None,
        store: Store | None = None,
        repo_root: str | Path | None = None,
        scopes: list[str] | None = None,
        project: str | None = None,
        datasets: list[str] | None = None,
        budget: float | None = None,
        confirmed: bool = False,
        connection: ConnectionSource | None = None,
    ) -> None:
        # Two config attributes, deliberately. `config` is what commands read and
        # is always a real object; `_declared` records whether one was actually
        # resolved, which is what separates "this project selected duckdb" from
        # "nothing selected anything" and keeps the no-silent-default refusal
        # honest. DexConfig's own connector default is a real choice for a config
        # that exists, never a fallback for one that does not.
        self._declared = config
        self.config: DexConfig = config if config is not None else DexConfig()
        self.store: Store = store if store is not None else MemoryStore()
        self.repo_root: str | None = None if repo_root is None else str(repo_root)
        self.connector = connector
        self.path = path
        self.scopes = scopes
        self.project = project
        self.datasets = datasets
        # Held, not consumed: the same source opens the held connection and the
        # transform preflight's separate one, so both run as the same principal.
        self.connection = connection
        # Session defaults for the confirm handshake. Per-call arguments override
        # them; a confirmed engine confirms every call it makes, which is the
        # reason to prefer confirming the call.
        self.budget = budget
        self.confirmed = confirmed
        self._adapter_instance: Adapter | None = None

    @classmethod
    def from_repo(cls, repo_root: str | Path, **overrides: Any) -> DexEngine:
        """Build from a project on disk: filesystem store, config from ``.dex/``.

        The CLI's constructor, and the only path that reads configuration from
        the filesystem. When no config resolves, the engine stays unresolved
        rather than defaulting, so the refusal fires on first connection attempt
        instead of at construction (commands that need no warehouse still work
        outside a project).
        """

        root = str(repo_root)
        overrides.setdefault("store", FilesystemStore(root))
        overrides.setdefault("config", load_config(root))
        return cls(repo_root=root, **overrides)

    # --- the single adapter funnel ------------------------------------------

    def _adapter(
        self,
        command: str | None = None,
        *,
        budget: float | None = None,
        confirmed: bool | None = None,
    ) -> Adapter:
        """The open adapter, opening it on first use. The only opener in the tree.

        Everything that touches a warehouse comes through here, which is what
        makes the connection seam a single place to reason about: one point where
        the credential is established, discovered or host-supplied, and one point
        where the cost gate is attached.

        The gate is rebuilt per call even though the connection is not, because
        the two have different lifetimes. Reusing a gate across commands would
        let one command's confirmation stand in for the next one's, accumulate
        estimates until an in-budget call is refused, and file every ledger entry
        under whichever command opened the connection first.

        On the *first* call there is nothing to rebuild: opening already builds a
        gate for this command, so the settings are passed down instead. Building
        a second one to replace it would re-read the whole spend ledger for the
        same answer, and the CLI's one-command-per-process shape means that
        wasted read would land on every single invocation.
        """

        budget = self.budget if budget is None else budget
        confirmed = self.confirmed if confirmed is None else confirmed

        if self._adapter_instance is None:
            self._adapter_instance = connect.open_adapter(
                connector=self.connector,
                path=self.path,
                project=self.project,
                datasets=self.datasets,
                scopes=self.scopes,
                repo_root="." if self.repo_root is None else self.repo_root,
                config=self._config_for_open(),
                store=self.store,
                budget=budget,
                confirmed=confirmed,
                command=command,
                connection=self.connection,
            )
            return self._adapter_instance

        adapter = self._adapter_instance
        if command_args.cost_gate(adapter) is not None:
            adapter.cost_gate = connect.new_cost_gate(
                adapter.name,
                self.config,
                self.store,
                budget=budget,
                confirmed=confirmed,
                command=command,
            )
        return adapter

    def _config_for_open(self) -> DexConfig:
        """The config handed to the opener, refusing when nothing selected one.

        Always a real object, so the opener never falls back to reading a file.
        The refusal that would otherwise live down there is raised here instead,
        where the engine knows all three inputs that could have named a
        connector.
        """

        if self._declared is None and self.connector is None and self.path is None:
            raise connect.no_connector_selected(self.repo_root)
        return self.config

    @property
    def has_declared_config(self) -> bool:
        """Whether a configuration was actually resolved, injected or loaded.

        Distinct from ``config`` being truthy: ``DexConfig()`` has a connector
        default, which is a real choice for a config that exists and never a
        fallback for one that does not. ``transform init`` is the caller that
        needs the difference, because it must refuse rather than guess.
        """

        return self._declared is not None

    def project_dir(self) -> Path:
        """The dbt project directory: the config pin wins, discovery is the default."""

        from .dbt_project import find_project

        root = self.require_repo_root("locating the dbt project")
        # Absolute so downstream dbt subprocess calls (which pin cwd to this dir)
        # never re-resolve a relative --project-dir against it and double the path.
        if self.config.dbt_project_dir:
            return (Path(root) / self.config.dbt_project_dir).resolve()
        return find_project(root).resolve()

    def require_repo_root(self, what: str) -> str:
        """The repo root, or a refusal naming what needed it.

        The dbt project is a filesystem artifact by design and stays one, so the
        commands that read or write it cannot run against an engine that was
        never told where the project lives. Saying which operation needed it
        beats a bare ``None`` several frames down.
        """

        if self.repo_root is None:
            raise ValueError(
                f"{what} needs a repo root: the dbt project is a git-reviewable "
                "filesystem artifact, so build the engine with "
                "DexEngine.from_repo(repo_root) or pass repo_root="
            )
        return self.repo_root

    # --- connect --------------------------------------------------------------

    def connect_test(self) -> ConnectResult:
        """Open the connection and report what this connector can do.

        Free on every connector, and the first thing worth running against a new
        target: it proves credential discovery works and names the cost paradigm
        that will govern everything after it.
        """

        adapter = self._adapter("connect test")
        return ConnectResult(
            capabilities=adapter.capabilities(),
            cost=command_args.preflight_cost(adapter),
        )

    # --- explore --------------------------------------------------------------
    #
    # Thin by design. The orchestration lives in the command modules next to the
    # engines it drives, so this class stays a surface a caller can read top to
    # bottom, and the CLI and a library call run identical code.

    def inventory(self, *, rank: bool = False) -> InventoryResult:
        from .explore import commands as explore

        return explore.inventory(self, rank=rank)

    def profile(
        self,
        *objects: str,
        refresh: bool = False,
        use_project: bool = False,
    ) -> ProfileResult:
        from .explore import commands as explore

        return explore.profile(
            self, list(objects), refresh=refresh, use_project=use_project
        )

    def relationships(
        self,
        *,
        verify: bool = False,
        refresh: bool = False,
        use_project: bool = False,
    ) -> RelationshipsResult:
        from .explore import commands as explore

        return explore.relationships(
            self, verify=verify, refresh=refresh, use_project=use_project
        )

    def map(
        self,
        *,
        full: bool = False,
        verify: bool = False,
        refresh: bool = False,
        use_project: bool = False,
    ) -> MapResult:
        from .explore import commands as explore

        return explore.map(
            self, full=full, verify=verify, refresh=refresh, use_project=use_project
        )

    def query(self, sql: str) -> QueryResult:
        from .explore import commands as explore

        return explore.query(self, sql)

    def cluster(
        self,
        obj: str,
        *,
        features: list[str] | None = None,
        k: int | None = None,
    ) -> ClusterResult:
        from .explore import commands as explore

        return explore.cluster(self, obj, features=features, k=k)

    def semantic_list(
        self, *, api: bool = False, local: bool = False
    ) -> SemanticListResult:
        from .explore import commands as explore

        return explore.semantic_list(self, api=api, local=local)

    def semantic_query(
        self,
        *metrics: str,
        group_by: list[str] | None = None,
        where: list[str] | None = None,
        order_by: list[str] | None = None,
        grain: str | None = None,
        limit: int | None = None,
        api: bool = False,
        local: bool = False,
    ) -> SemanticQueryResult:
        from .explore import commands as explore

        return explore.semantic_query(
            self,
            list(metrics),
            group_by=group_by,
            where=where,
            order_by=order_by,
            grain=grain,
            limit=limit,
            api=api,
            local=local,
        )

    # --- maintain -------------------------------------------------------------

    def snapshot(self) -> SnapshotResult:
        from .maintain import commands as maintain

        return maintain.snapshot(self)

    def check(self) -> DriftResult:
        from .maintain import commands as maintain

        return maintain.check(self)

    def schema_drift(self, objects: list[str] | None = None) -> DriftResult:
        from .maintain import commands as maintain

        return maintain.schema_drift(self, objects)

    def volume_drift(self, objects: list[str] | None = None) -> DriftResult:
        from .maintain import commands as maintain

        return maintain.volume_drift(self, objects)

    def grain_drift(self, objects: list[str] | None = None) -> DriftResult:
        from .maintain import commands as maintain

        return maintain.grain_drift(self, objects)

    def semantic_drift(self, objects: list[str] | None = None) -> DriftResult:
        from .maintain import commands as maintain

        return maintain.semantic_drift(self, objects)

    def reconcile(self, drift_class: str | None = None) -> ReconcileResult:
        from .maintain import commands as maintain

        return maintain.reconcile(self, drift_class)

    # --- transform --------------------------------------------------------
    #
    # Every method here needs `repo_root`: the dbt project is the source of
    # truth and stays a git-reviewable filesystem artifact, so an engine that
    # was never told where the project lives refuses rather than inventing one.

    def init_project(
        self,
        name: str,
        *,
        connector: str | None = None,
        path: str | None = None,
        layered_schemas: bool = False,
    ) -> InitResult:
        from .transform import commands as transform

        return transform.init_project(
            self,
            name,
            connector=connector,
            path=path,
            layered_schemas=layered_schemas,
        )

    def plan(
        self,
        intent: str,
        *,
        edits: list[PlanEdit] | None = None,
        scaffold: list[str] | None = None,
    ) -> PlanResult:
        from .transform import commands as transform

        return transform.plan(self, intent, edits=edits, scaffold=scaffold)

    def apply(self, plan_id: str | None = None) -> ApplyResult:
        from .transform import commands as transform

        return transform.apply(self, plan_id)

    def plans(self) -> PlanListResult:
        from .transform import commands as transform

        return transform.plans(self)

    def macro(self, name: str | None = None) -> MacroListResult | MacroResult:
        from .transform import commands as transform

        return transform.macro(self, name)

    def build(
        self, *, target: str | None = None, select: str | None = None
    ) -> BuildResult:
        from .transform import commands as transform

        return transform.build(self, target=target, select=select)

    def deps(self) -> DepsResult:
        from .transform import commands as transform

        return transform.deps(self)

    def semantic_define(
        self, intent: str, edits: list[PlanEdit], *, no_parse: bool = False
    ) -> PlanResult:
        from .transform import commands as transform

        return transform.semantic_define(self, intent, edits, no_parse=no_parse)

    def semantic_update(
        self, intent: str, edits: list[PlanEdit], *, no_parse: bool = False
    ) -> PlanResult:
        from .transform import commands as transform

        return transform.semantic_update(self, intent, edits, no_parse=no_parse)

    def semantic_plan(
        self, intent: str, edits: list[PlanEdit], *, no_parse: bool = False
    ) -> PlanResult:
        from .transform import commands as transform

        return transform.semantic_plan(self, intent, edits, no_parse=no_parse)

    # --- lifecycle ------------------------------------------------------------

    def close(self) -> None:
        """Close the held connection. Idempotent."""

        adapter = self._adapter_instance
        self._adapter_instance = None
        if adapter is not None:
            adapter.close()

    def __enter__(self) -> DexEngine:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
