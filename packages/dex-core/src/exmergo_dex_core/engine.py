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
:meth:`DexEngine.from_repo` is the other way in: it reads ``.dex/config.yml`` and
builds whichever backend ``cache.backend`` names, which is how the CLI reaches a
backend dex does not ship.

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

from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import command_args, connect
from .config import CacheConfig, DexConfig, ProjectConfig, load_config
from .connect import ConnectionSource, SemanticSource
from .envelope import Paradigm
from .errors import RepoRootRequiredError, StoreRequiredError
from .results import ConnectResult
from .storage import ExploreStore, MemoryStore, Store, StoreContext, build_store

if TYPE_CHECKING:
    from .adapters.base import Adapter
    from .adapters.project import (
        EditableProject,
        ExploreProject,
        MaintainProject,
        ProjectFactory,
    )
    from .explore.results import (
        ClusterResult,
        DiagramResult,
        InventoryResult,
        MapResult,
        ProfileResult,
        QueryBatchResult,
        QueryResult,
        RelationshipsResult,
        SemanticListResult,
        SemanticQueryResult,
        SemanticValuesResult,
    )
    from .maintain.results import (
        DriftResult,
        ReconcileResult,
        SnapshotResult,
        VerifyResult,
    )
    from .references import ReferencesResult
    from .transform.plans import PlanEdit
    from .transform.results import (
        ApplyResult,
        BuildResult,
        DepsResult,
        InitResult,
        MacroListResult,
        MacroResult,
        PlacementResult,
        PlanListResult,
        PlanResult,
        PropagationResult,
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

    The semantic layer is a second service with a credential of its own, so it has
    its own parameter: ``semantic_source=`` (a
    :class:`~.connect.SemanticSource`) supplies the hosted dbt Cloud Semantic Layer
    token. A deployment may use one, the other, or both, and a hosted metric query
    is the one surface that needs nothing on the filesystem at all: no project, no
    store, no connector. Note the cost posture there is not dex's to set, because
    dbt Cloud owns that warehouse connection and executes server-side.

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
        store: ExploreStore | None = None,
        repo_root: str | Path | None = None,
        scopes: list[str] | None = None,
        project: str | None = None,
        datasets: list[str] | None = None,
        budget: float | None = None,
        confirmed: bool = False,
        connection: ConnectionSource | None = None,
        semantic_source: SemanticSource | None = None,
        project_format: ExploreProject | None = None,
    ) -> None:
        # Two config attributes, deliberately. `config` is what commands read and
        # is always a real object; `_declared` records whether one was actually
        # resolved, which is what separates "this project selected duckdb" from
        # "nothing selected anything" and keeps the no-silent-default refusal
        # honest. DexConfig's own connector default is a real choice for a config
        # that exists, never a fallback for one that does not.
        self._declared = config
        self.config: DexConfig = config if config is not None else DexConfig()
        self.store: ExploreStore = store if store is not None else MemoryStore()
        self.repo_root: str | None = None if repo_root is None else str(repo_root)
        self.connector = connector
        self.path = path
        # Populated only by the run-directory DuckDB auto-detect (issue #199);
        # empty otherwise. A caller must never miss a guess dex made on its
        # behalf, so this is read back and folded into every envelope, not just
        # the one that happened to trigger it (`_adapter` may be called once and
        # cached, with later commands in the same process never re-resolving).
        self.connection_warnings: list[str] = []
        self.scopes = scopes
        self.project = project
        self.datasets = datasets
        # Held, not consumed: the same source opens the held connection and the
        # transform preflight's separate one, so both run as the same principal.
        self.connection = connection
        # The semantic layer is a second service with its own credential, so it
        # gets its own source: a hosted dbt Cloud token is not what a warehouse
        # read authenticates with, and a deployment may well use one and not the
        # other. Read once per semantic command, never held.
        self.semantic_source = semantic_source
        # Session defaults for the confirm handshake. Per-call arguments override
        # them; a confirmed engine confirms every call it makes, which is the
        # reason to prefer confirming the call.
        self.budget = budget
        self.confirmed = confirmed
        self._adapter_instance: Adapter | None = None
        # The project seam, in its two shapes. `_project_format` is an instance a
        # host handed us and always wins; `_project_factory` is what a configured
        # name resolved to, called fresh per command. See `project_format()` for
        # why one is held and the other is not. The private names are not
        # squeamishness: `self.project` is the warehouse billing project, taken
        # long before this seam existed.
        self._project_format = project_format
        self._project_name = "dbt"
        self._project_factory: ProjectFactory | None = None
        self._project_options: Mapping[str, Any] = {}

    @classmethod
    def from_repo(
        cls,
        repo_root: str | Path,
        *,
        cache_backend: str | None = None,
        project_format_name: str | None = None,
        **overrides: Any,
    ) -> DexEngine:
        """Build from a project on disk: config from ``.dex/``, store from config.

        The CLI's constructor, and the only path that reads configuration from
        the filesystem. When no config resolves, the engine stays unresolved
        rather than defaulting, so the refusal fires on first connection attempt
        instead of at construction (commands that need no warehouse still work
        outside a project).

        The store is whatever ``cache.backend`` selects, defaulting to the
        filesystem backend, so a repo that configures nothing behaves exactly as
        it did before the setting existed. A ``store=`` passed here wins over the
        configuration: a caller holding an instance has already made the decision
        that configuration exists to make for the callers who are not.

        ``cache_backend`` overrides the configured name for one run, and takes
        the configured ``cache.options`` with it only when it names the backend
        those options were written for. Options are not namespaced by backend, so
        carrying them onto a different one would hand a backend another backend's
        coordinates; the most useful thing this override does is fall back to the
        filesystem backend for one command, and that would refuse outright if a
        custom backend's options came along.

        The project format resolves the same way and takes the same override
        rules, with one difference: what is held is the *factory*, not a built
        project. A store is state with a lifetime and is built once; a project is
        a read of an artifact a later command may rewrite, so it is built per
        command. See :meth:`project_format`. ``project_format_name`` is the CLI's
        ``--project-format``, and a ``project_format=`` instance passed here wins
        over both, exactly as ``store=`` does.
        """

        root = str(repo_root)
        overrides.setdefault("config", load_config(root))
        if "store" not in overrides:
            cache = getattr(overrides["config"], "cache", None) or CacheConfig()
            selected = cache_backend or cache.backend
            options = cache.options if selected == cache.backend else {}
            overrides["store"] = build_store(
                selected, StoreContext(repo_root=root, options=options)
            )
        engine = cls(repo_root=root, **overrides)
        if engine._project_format is None:
            from .adapters.project_resolver import resolve_project_factory

            declared = getattr(overrides["config"], "project", None) or ProjectConfig()
            chosen = project_format_name or declared.format
            engine._project_name = chosen
            engine._project_factory = resolve_project_factory(chosen)
            engine._project_options = (
                declared.options if chosen == declared.format else {}
            )
        return engine

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

        A rebuild settles the outgoing gate first. It has normally settled
        itself already, on its own way out; this covers the command that raised
        past that point, so the headroom it booked is released by the next
        command rather than held until the UTC rollover.
        """

        budget = self.budget if budget is None else budget
        confirmed = self.confirmed if confirmed is None else confirmed

        if self._adapter_instance is None:
            # Resolved first, as its own statement: it may auto-detect a
            # connector and path (issue #199), and `self.connector`/`self.path`
            # below must see that resolution, not the pre-resolution values a
            # single call's keyword arguments would read in write order.
            resolved_config = self._config_for_open()
            self._adapter_instance = connect.open_adapter(
                connector=self.connector,
                path=self.path,
                project=self.project,
                datasets=self.datasets,
                scopes=self.scopes,
                repo_root="." if self.repo_root is None else self.repo_root,
                config=resolved_config,
                store=self.store,
                budget=budget,
                confirmed=confirmed,
                command=command,
                connection=self.connection,
            )
            return self._adapter_instance

        adapter = self._adapter_instance
        outgoing = command_args.cost_gate(adapter)
        if outgoing is not None:
            outgoing.settle()
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

        Before refusing outright, one narrow exception (issue #199): with no
        config anywhere and nothing explicit, a run directory holding exactly
        one ``*.duckdb`` file is not a phantom target, it is the one thing the
        caller almost certainly meant, and reading it is the first thirty
        seconds of the zero-credential on-ramp. Two or more is exactly the
        ambiguity this whole gate exists to refuse, so that still raises, now
        naming both. The exception is deliberately this narrow: an explicit
        ``--connector``/``--path`` or any config, even one naming a different
        file, already took one of the three inputs above non-None and never
        reaches this method at all. A ``repo_root`` of ``None`` is the other
        spelling this method already had (a library caller holding no repo
        concept at all, not merely one that defaults to "."), and has no run
        directory to look in, so it keeps its original, unconditional refusal
        rather than scanning whatever the process's own cwd happens to be.
        """

        if self._declared is None and self.connector is None and self.path is None:
            if self.repo_root is None:
                raise connect.no_connector_selected(None)
            candidates = connect.duckdb_candidates(self.repo_root)
            if len(candidates) > 1:
                raise connect.no_connector_selected(
                    self.repo_root, ambiguous_duckdb=candidates
                )
            if len(candidates) == 1:
                found = candidates[0]
                self.connector = "duckdb"
                self.path = str(found)
                self.connection_warnings.append(
                    f"no connector configured; using the one DuckDB file in "
                    f"this directory ({found.name}) since nothing else named a "
                    "target. Make this explicit with --path "
                    f"{found} or duckdb.path: {found} in .dex/config.yml"
                )
            else:
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

    @property
    def paradigm(self) -> Paradigm | None:
        """The cost paradigm of the connector in play, ``None`` when nothing
        selected one.

        Resolved from the connector's name rather than from an open adapter,
        which is what lets a refusal name the paradigm even when the refusal
        *was* the connection failing, and after the adapter has been closed.

        The three-input test is the same one :meth:`_config_for_open` refuses
        on, and for the same reason: ``DexConfig.connector`` has a default, so
        reading it when nothing declared a config would answer ``free_local``
        for a project that never chose DuckDB.
        """

        if self._declared is None and self.connector is None and self.path is None:
            return None
        return connect.paradigm_for(
            self.connector or self.config.connector, self.config
        )

    def settled_spend(self) -> dict | None:
        """What the command that just ran actually billed, settled and read
        back from the gate, or ``None`` when nothing billed anything.

        The success path reaches this through ``stamp_spend`` on the result;
        this is the same number for a caller holding an exception instead,
        which is why it lives on the engine rather than inside a command. An
        adapter that was never opened has no gate and so no spend.
        """

        from .guards.cost_guard import spend_field

        adapter = self._adapter_instance
        gate = None if adapter is None else command_args.cost_gate(adapter)
        if adapter is None or gate is None:
            return None
        spend = command_args.settled_spend(adapter)
        # Nothing billed, nothing to report: a refusal that stopped before the
        # first statement is free, and a spend block of zeroes on it would read
        # as a claim about money where the honest answer is silence. The day's
        # cumulative total rides along only when this command contributed to it.
        if not spend or not spend.get(spend_field(gate.paradigm)):
            return None
        return spend

    def project_dir(self) -> Path:
        """The dbt project directory: the config pin wins, discovery is the default."""

        from .dbt_project import find_project

        root = self.require_repo_root("locating the dbt project")
        # Absolute so downstream dbt subprocess calls (which pin cwd to this dir)
        # never re-resolve a relative --project-dir against it and double the path.
        if self.config.dbt_project_dir:
            return (Path(root) / self.config.dbt_project_dir).resolve()
        return find_project(root).resolve()

    def project_format(self) -> ExploreProject:
        """The project this engine reads, built from whatever named it.

        The seam every command reads a project through. Three sources, in
        precedence order: an instance the host passed as ``project_format=``, the
        format ``project.format`` selected (or ``--project-format`` overrode), and
        the shipped dbt format when nothing selected anything.

        **A project dex built is not held across commands, and that is
        deliberate.** ``self._adapter_instance`` is held because a warehouse
        session is expensive to reopen and can wake a warehouse; a project is a
        read of an artifact on disk that ``transform apply`` and ``transform
        build`` rewrite, so holding one would serve a later command the project as
        it was before the write, and a drift report computed against a stale
        project is wrong rather than merely slow. Construction is cheap by
        contract (see :class:`~.adapters.project.ProjectContext`) and the expensive
        part, the load itself, is memoized inside the instance for the command
        that holds it.

        A host-supplied instance *is* held, because its lifetime is the host's to
        decide: a host that reduced a project from a graph it owns knows when that
        graph changed and dex does not.

        The name says ``format`` because the plain one is taken: ``self.project``
        is the warehouse billing project, and has been since before this seam
        existed.
        """

        if self._project_format is not None:
            return self._project_format

        from .adapters.project import ProjectContext
        from .adapters.project_resolver import SHIPPED, construct_project

        # Through `construct_project` rather than calling the factory directly,
        # so the tier floor is checked on what a configured name actually built.
        # A factory that returns the wrong thing otherwise degrades into a project
        # that answers nothing, and the first symptom is a command warning about a
        # missing tier several frames from the config line that caused it.
        return construct_project(
            self._project_name,
            self._project_factory or SHIPPED["dbt"],
            ProjectContext(
                repo_root=self.repo_root,
                project_dir=self.config.dbt_project_dir,
                options=self._project_options,
            ),
        )

    def maintain_project(self) -> MaintainProject | None:
        """The project when it can serve as a drift baseline, else ``None``.

        The project tiers are nested, and a format implementing only
        :class:`~.adapters.project.ExploreProject` is complete rather than
        partial: it contributes declared keys and joins to exploration and simply
        cannot be a baseline. So ``None`` is an answer here rather than a failure,
        the same way it is in :meth:`editable_project`, and the caller decides
        whether its command can proceed without one.

        What is *not* an answer is a format that could not be built at all. That
        propagates, because it is a wiring mistake in a committed file: every
        command will hit it, so degrading to a warning would hide the one thing
        the operator needs to fix.
        """

        from .adapters.project import MaintainProject as _MaintainProject

        project = self.project_format()
        return project if isinstance(project, _MaintainProject) else None

    def project_tier_note(self) -> str:
        """Why the configured format cannot be a baseline, in one clause.

        Folded into the warning each maintain command already emits when it has
        no project, so a narrow format reads as a narrow format rather than as an
        absent one. The tier is counted rather than named: telling someone whose
        format implements nothing that it "implements only the explore tier"
        sends them looking for the method they already have.
        """

        from .adapters.project import tier_of

        project = self.project_format()
        named = getattr(project, "name", type(project).__name__)
        return (
            f"the '{named}' project format reaches tier {tier_of(project)} of 3, "
            "so it cannot produce the two snapshot layers a baseline is made of; "
            "implement transform_layer() and semantic_layer() to reach "
            "exmergo_dex_core.adapters.project.MaintainProject"
        )

    def editable_project(self) -> EditableProject | None:
        """The project when it declares it can receive edits, else ``None``.

        ``None`` is an answer rather than a failure, and the caller that asks is
        ``maintain reconcile``: a format declining the write tier gets
        proposal-only reconciliation, which is the correct behavior and the reason
        the tiers exist instead of a ``writeback`` flag. Returning ``None`` rather
        than raising is what lets the caller degrade instead of refusing.
        """

        from .adapters.project import EditableProject as _EditableProject

        project = self.project_format()
        return project if isinstance(project, _EditableProject) else None

    def require_repo_root(self, what: str) -> str:
        """The repo root, or a refusal naming what needed it.

        The dbt project is a filesystem artifact by design and stays one, so the
        commands that read or write it cannot run against an engine that was
        never told where the project lives. Saying which operation needed it
        beats a bare ``None`` several frames down.
        """

        if self.repo_root is None:
            raise RepoRootRequiredError(
                f"{what} needs a repo root: the dbt project is a git-reviewable "
                "filesystem artifact, so build the engine with "
                "DexEngine.from_repo(repo_root) or pass repo_root="
            )
        return self.repo_root

    def require_full_store(self, what: str) -> Store:
        """The store the transform surface needs, or a refusal naming what needed it.

        The storage contract is tiered: a host that only explores implements
        :class:`~.storage.ExploreStore` and stops there, which is a complete
        implementation of a declared contract rather than a partial one. The plan
        members it leaves out are exactly what transform stores its reviewable
        diffs in, so this is the one boundary where the wider tier is required and
        the one place the difference is worth a runtime check.
        """

        store = self.store
        if not isinstance(store, Store):
            missing = sorted(
                name
                for name in (
                    "save_plan",
                    "load_plan",
                    "list_plans",
                    "latest_plan",
                    "plan_locator",
                )
                if not hasattr(store, name)
            )
            raise StoreRequiredError(
                f"{what} needs a full store and this one implements the explore "
                f"tier only (missing: {', '.join(missing)}). Transform keeps its "
                "plans in the store, so a backend serving transform has to "
                "implement exmergo_dex_core.storage.Store, not just ExploreStore"
            )
        return store

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
        check_cumulative: bool = False,
    ) -> ProfileResult:
        from .explore import commands as explore

        return explore.profile(
            self,
            list(objects),
            refresh=refresh,
            use_project=use_project,
            check_cumulative=check_cumulative,
        )

    def relationships(
        self,
        *,
        verify: bool = False,
        infer_by_overlap: bool = False,
        refresh: bool = False,
        use_project: bool = False,
    ) -> RelationshipsResult:
        from .explore import commands as explore

        return explore.relationships(
            self,
            verify=verify,
            infer_by_overlap=infer_by_overlap,
            refresh=refresh,
            use_project=use_project,
        )

    def map(
        self,
        *,
        full: bool = False,
        detail: bool = False,
        verify: bool = False,
        infer_by_overlap: bool = False,
        refresh: bool = False,
        use_project: bool = False,
    ) -> MapResult:
        from .explore import commands as explore

        return explore.map(
            self,
            full=full,
            infer_by_overlap=infer_by_overlap,
            detail=detail,
            verify=verify,
            refresh=refresh,
            use_project=use_project,
        )

    def query(self, sql: str, *, auto_profile: bool | None = None) -> QueryResult:
        from .explore import commands as explore

        return explore.query(self, sql, auto_profile=auto_profile)

    def query_batch(
        self, *sql: str, auto_profile: bool | None = None
    ) -> QueryBatchResult:
        """Several statements in one call, each with its own verdict.

        A separate method rather than an overload of :meth:`query`, because the
        two differ in more than arity: a refusal raises there and is reported
        here, so a caller that wanted one answer never has to check a status field
        for it.
        """

        from .explore import commands as explore

        return explore.query_batch(self, list(sql), auto_profile=auto_profile)

    def cluster(
        self,
        obj: str,
        *,
        features: list[str] | None = None,
        k: int | None = None,
        auto_profile: bool | None = None,
    ) -> ClusterResult:
        from .explore import commands as explore

        return explore.cluster(
            self, obj, features=features, k=k, auto_profile=auto_profile
        )

    def diagram(self, *, full: bool = False) -> DiagramResult:
        """The cached map as a Mermaid ER diagram. Reads the store, nothing else.

        Free on every connector and the one explore method that opens no
        connection at all, so a host holding a populated store can call it with
        no credential in play. See :mod:`~.explore.diagram` for what the
        rendered cardinalities are and are not allowed to claim.
        """

        from .explore import commands as explore

        return explore.diagram(self, full=full)

    # The two semantic methods import from `explore.semantic.commands`, not from
    # `explore.commands`: on the hosted backend dbt Cloud renders and executes the
    # SQL, so this is the one guarded surface that needs no dialect engine and no
    # warehouse client. Together with a `SemanticSource`, that is what lets a host
    # serve metric queries with nothing on the filesystem and only [semantic-api]
    # installed. Reaching through the heavier module would quietly require both.
    def semantic_list(
        self,
        *,
        metrics: list[str] | None = None,
        for_dimensions: list[str] | None = None,
        search: list[str] | None = None,
        full: bool = False,
        api: bool = False,
        local: bool = False,
    ) -> SemanticListResult:
        from .explore.semantic import commands as semantic_cmds

        return semantic_cmds.semantic_list(
            self,
            metrics=metrics,
            for_dimensions=for_dimensions,
            search=search,
            full=full,
            api=api,
            local=local,
        )

    def semantic_values(
        self,
        dimension: str,
        *,
        metrics: list[str] | None = None,
        api: bool = False,
        local: bool = False,
    ) -> SemanticValuesResult:
        from .explore.semantic import commands as semantic_cmds

        return semantic_cmds.semantic_values(
            self, dimension, metrics=metrics, api=api, local=local
        )

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
        from .explore.semantic import commands as semantic_cmds

        return semantic_cmds.semantic_query(
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

    def verify(self, objects: list[str] | None = None) -> VerifyResult:
        from .maintain import commands as maintain

        return maintain.verify(self, objects)

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
        in_place: bool = False,
    ) -> InitResult:
        from .transform import commands as transform

        return transform.init_project(
            self,
            name,
            connector=connector,
            path=path,
            layered_schemas=layered_schemas,
            in_place=in_place,
        )

    def plan(
        self,
        intent: str,
        *,
        edits: list[PlanEdit] | None = None,
        scaffold: list[str] | None = None,
        attribute_rows: bool | None = None,
    ) -> PlanResult:
        from .transform import commands as transform

        return transform.plan(
            self,
            intent,
            edits=edits,
            scaffold=scaffold,
            attribute_rows=attribute_rows,
        )

    def apply(self, plan_id: str | None = None) -> ApplyResult:
        from .transform import commands as transform

        return transform.apply(self, plan_id)

    def plans(self) -> PlanListResult:
        from .transform import commands as transform

        return transform.plans(self)

    def macro(self, name: str | None = None) -> MacroListResult | MacroResult:
        from .transform import commands as transform

        return transform.macro(self, name)

    def references(
        self,
        names: list[str],
        *,
        kind: str | None = None,
        full: bool = False,
    ) -> ReferencesResult:
        # From `references` rather than `transform.commands`: this one reads the
        # project's files and needs neither the plan store nor the dialect engine,
        # and reaching it through the authoring module would require both.
        from . import references as references_mod

        return references_mod.run(self, names, kind=kind, full=full)

    def rename(
        self,
        kind: str,
        old: str,
        new: str,
        *,
        edits_file: str | None = None,
    ) -> PropagationResult:
        from .transform import commands as transform

        return transform.rename(self, kind, old, new, edits_file=edits_file)

    def remove(
        self, kind: str, name: str, *, edits_file: str | None = None
    ) -> PropagationResult:
        from .transform import commands as transform

        return transform.remove(self, kind, name, edits_file=edits_file)

    def place(
        self,
        column: str,
        targets: list[str],
        expression: str,
        *,
        explain: bool = False,
    ) -> PlacementResult:
        from .transform import commands as transform

        return transform.place(self, column, targets, expression, explain=explain)

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
        """Close the held connection, releasing any held spend headroom first.

        Idempotent. The CLI closes from a ``finally``, so this is the funnel that
        catches a command interrupted partway: without it, a gate that had been
        admitted but never settled would hold its estimate against the day's
        cumulative ceiling until the UTC rollover. Releasing must not be able to
        stop the connection closing, so a store that cannot be reached at this
        point loses the release rather than leaking the connection.
        """

        adapter = self._adapter_instance
        self._adapter_instance = None
        if adapter is None:
            return
        gate = command_args.cost_gate(adapter)
        try:
            if gate is not None:
                gate.settle()
        finally:
            adapter.close()

    def __enter__(self) -> DexEngine:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
