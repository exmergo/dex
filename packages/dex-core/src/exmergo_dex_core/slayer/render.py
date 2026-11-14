"""Rendering a SLayer query to SQL, and nothing past rendering.

One of the three modules in dex that import SLayer, beside :mod:`.loader` and
:mod:`.schema`. SLayer renders and dex executes: this module hands SLayer's query
engine a query and takes back the SQL it would run, and the statement then goes
through the same governed spine a MetricFlow-rendered one does. Nothing here can
execute.

**The storage is the models dex already validated.** ``SlayerQueryEngine`` needs
a ``StorageBackend``, and SLayer ships none that only reads: ``YAMLStorage``
runs layout migrations that rewrite files under the storage root, and
``SQLiteStorage`` migrates its database at construction. :class:`ValidatedModelStorage`
answers SLayer's reads from the :class:`~.loader.LoadedModel` set the command
already loaded, and every write, memory and embedding method raises, so a
planning path that tried to persist something would fail rather than write.

**A datasource is a dialect and nothing else.** SLayer reads a model's
datasource to pick the dialect it renders in.
:meth:`ValidatedModelStorage.get_datasource` answers with a name and a type and
no host, user, password, connection string or environment variable, so there is
nothing a client could connect with even if one were built. The type comes from
dex's own connector through :data:`SLAYER_TYPES`, an explicit table: SLayer
falls back to Postgres for a type it does not know, and a statement rendered in
another dialect would run and return wrong numbers, so a connector outside the
table is refused by name, the way ``explore.semantic.local_runtime`` refuses one
with no MetricFlow renderer.

**Rendering is a dry run.** :meth:`SlayerRenderer.render` calls
``execute_sync(query, dry_run=True)``, which returns before SLayer constructs a
SQL client. The engine is built with ``policy=None``, since a forced-filter
policy is applied by probing the live schema through SLayer's own connection,
and with a fixed clock, so a relative date (``last 7 days``) resolves to the
same SQL however long a command takes between rendering and pricing.

**A render runs with SLayer's reserved keywords, and only a render does.**
SLayer quotes a reserved-word identifier through sqlglot's generator sets,
which dex keeps at sqlglot's own outside a SLayer call
(:mod:`.sqlglot_keywords`), so the render installs SLayer's for its length.

**A result is named as SLayer would name it.** SLayer emits a projection alias
in a form the dialect accepts and decodes it on the rows it reads back:
BigQuery rejects a dotted output column, so ``orders.status`` reaches the
warehouse as ``orders___status``, and a dialect with an identifier length limit
fits a long alias to it. dex reads the rows, not SLayer, so the render carries
the decoding with it (:attr:`RenderedQuery.result_keys`) and a caller reads the
same ``model.column`` names on every connector. Redshift folds a quoted
identifier to lowercase unless ``enable_case_sensitive_identifier`` is on, so
there the decoding is keyed by the folded name, and a query whose result names
would fold onto one another is refused by name rather than answered with two
columns a caller cannot tell apart.

**A week starts on the day SLayer means.** SLayer's ``week`` is Monday-based
and ``week_sunday`` is the Monday week of the next day, moved back one. On
Snowflake both come out as ``DATE_TRUNC('WEEK', ...)``, whose start day follows
the session's ``WEEK_START`` parameter, so an account set to Sunday would bucket
``week`` from Sunday and ``week_sunday`` from Saturday, and nothing would say
so. The render rewrites every Snowflake week truncation to the Monday it means
through ``DAYOFWEEKISO``, which ignores ``WEEK_START``, so the buckets are the
same on every account (:func:`pin_snowflake_weeks`).
"""

from __future__ import annotations

# First, before any SLayer import: importing SLayer rewrites sqlglot's reserved
# keywords process-wide, and this module undoes that (see `sqlglot_keywords`).
from .sqlglot_keywords import slayer_keywords

# isort: split

import warnings
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, NoReturn

import sqlglot
from slayer.core.models import DatasourceConfig, SlayerModel
from slayer.engine.query_engine import SlayerQueryEngine
from slayer.sql.dialects import dialect_for_ds_type
from slayer.storage.base import StorageBackend
from sqlglot import exp

from ..explore.semantic.backend import SemanticBackendError
from .loader import LoadedModel

__all__ = [
    "EXCLUDED",
    "SLAYER_TYPES",
    "RenderedQuery",
    "SlayerRenderer",
    "ValidatedModelStorage",
    "pin_snowflake_weeks",
    "slayer_type",
]

#: dex connector -> the SLayer datasource ``type`` that renders in its dialect.
#: Explicit, and a subset of the connectors local MetricFlow renders for: a
#: connector joins only once a golden of its rendered SQL has been read for that
#: dialect (``tests/slayer_vendor/test_goldens.py``). The ones left out, and
#: why, are :data:`EXCLUDED`.
SLAYER_TYPES: dict[str, str] = {
    "duckdb": "duckdb",
    "bigquery": "bigquery",
    "snowflake": "snowflake",
    "postgres": "postgres",
    "redshift": "redshift",
}

#: Connectors with a SLayer dialect that dex still will not render for, each
#: with the reason a refusal quotes.
EXCLUDED: dict[str, str] = {
    "databricks": (
        "SLayer renders a Databricks time bucket with Spark's TRUNC, which knows "
        "only year, quarter, month and week and answers NULL for a day, hour, "
        "minute or second grain, and it renders a median with PERCENTILE_APPROX, "
        "an approximation; either would run and return the wrong numbers"
    ),
    "clickhouse": (
        "SLayer probes a ClickHouse server's profile through a live client while "
        "preparing a correlated semi-join, which a dry run would not survive"
    ),
    "trino": (
        "SLayer has a Trino dialect, but no golden of the SQL it renders for "
        "Trino has been read yet, so dex cannot vouch that its numbers are right"
    ),
}


#: SLayer datasource types whose warehouse folds a quoted identifier to
#: lowercase by default, so a result column comes back under a name the
#: statement did not spell.
FOLDS_QUOTED_IDENTIFIERS: frozenset[str] = frozenset({"redshift"})


def slayer_type(connector: str | None) -> str:
    """The SLayer datasource type for ``connector``, or a refusal naming it."""

    mapped = SLAYER_TYPES.get(connector or "")
    if mapped is None:
        supported = ", ".join(sorted(SLAYER_TYPES))
        reason = EXCLUDED.get(connector or "")
        if reason is not None:
            raise SemanticBackendError(
                f"no SLayer dialect for connector '{connector}': {reason}. SLayer "
                f"metric queries support {supported}"
            )
        raise SemanticBackendError(
            f"no SLayer dialect for connector '{connector}'; SLayer metric queries "
            f"support {supported}. dex will not let SLayer render a metric in its "
            "Postgres fallback for a connector it does not name, because the SQL "
            "would run and the numbers would be wrong"
        )
    return mapped


def _read_only(what: str) -> NoReturn:
    raise RuntimeError(
        f"dex's SLayer storage is read-only: {what} is not permitted. SLayer "
        "renders under dex and never persists anything"
    )


class ValidatedModelStorage(StorageBackend):
    """A SLayer ``StorageBackend`` over models dex has already validated.

    Read-only by construction: the reads planning makes are answered from the
    model set given at construction, and every method that would persist a
    model, a datasource, a priority list, a memory or an embedding raises.
    Every model must carry a ``data_source``, because SLayer keys a model by
    ``(data_source, name)`` and resolves its dialect through it; a model with
    none is refused by name rather than rendered in a dialect nobody chose.
    """

    def __init__(self, models: Sequence[LoadedModel], *, connector: str) -> None:
        self._type = slayer_type(connector)
        orphans = sorted(
            f"'{m.model.name}' ({m.file})" for m in models if not m.model.data_source
        )
        if orphans:
            raise SemanticBackendError(
                f"SLayer model{'s' if len(orphans) > 1 else ''} "
                f"{', '.join(orphans)} declare{'' if len(orphans) > 1 else 's'} no "
                "`data_source`. SLayer renders a model in its datasource's dialect "
                "and keys it by (data_source, name), so set `data_source` on the "
                "model and keep the file at models/<data_source>/<name>.yaml"
            )
        self._models: dict[tuple[str, str], SlayerModel] = {
            (m.model.data_source, m.model.name): m.model
            for m in models
            if m.model.data_source
        }

    @property
    def datasource_type(self) -> str:
        """The SLayer datasource type every model here renders as."""

        return self._type

    # --- what planning reads ---------------------------------------------------

    async def get_model(
        self, name: str, data_source: str | None = None
    ) -> SlayerModel | None:
        target = await self._resolve_target_or_none(name, data_source=data_source)
        if target is None:
            return None
        model = self._models.get(target)
        # A copy, as SLayer's own storage returns, so planning cannot alter the
        # validated set the rest of the command reads.
        return model.model_copy(deep=True) if model is not None else None

    async def _list_all_model_identities(self) -> list[tuple[str, str]]:
        return sorted(self._models)

    async def get_datasource(self, name: str) -> DatasourceConfig | None:
        if name not in {data_source for data_source, _ in self._models}:
            return None
        return DatasourceConfig(name=name, type=self._type)

    async def list_datasources(self) -> list[str]:
        return sorted({data_source for data_source, _ in self._models})

    async def get_datasource_priority(self) -> list[str]:
        # The loader refuses a model name declared twice, so a bare name never
        # needs a priority list to disambiguate it.
        return []

    # --- everything that would persist -----------------------------------------

    async def _save_model_impl(self, model: SlayerModel) -> None:
        _read_only("saving a model")

    async def _delete_model_row(self, *, data_source: str, name: str) -> bool:
        _read_only("deleting a model")

    async def update_column_sampled(self, *args: Any, **kwargs: Any) -> bool:
        _read_only("recording a column sample")

    async def _save_datasource_impl(self, datasource: DatasourceConfig) -> None:
        _read_only("saving a datasource")

    async def _delete_datasource_row(self, name: str) -> bool:
        _read_only("deleting a datasource")

    async def _set_datasource_priority_raw(self, priority: list[str]) -> None:
        _read_only("setting the datasource priority")

    async def _save_memory_row(self, memory: Any) -> None:
        _read_only("saving a memory")

    async def _get_memory_row(self, memory_id: str) -> Any:
        _read_only("reading a memory")

    async def _list_memories_rows(self, *args: Any, **kwargs: Any) -> Any:
        _read_only("listing memories")

    async def _delete_memory_row(self, memory_id: str) -> bool:
        _read_only("deleting a memory")

    async def _next_memory_seq(self) -> str:
        _read_only("allocating a memory id")

    async def save_embedding(self, row: Any) -> None:
        _read_only("saving an embedding")

    async def get_embedding(self, *, canonical_id: str, embedding_model_name: str):
        _read_only("reading an embedding")

    async def list_embeddings(self, *, embedding_model_name: str) -> list[Any]:
        _read_only("listing embeddings")

    async def delete_embeddings_for_canonical(self, *, canonical_id_prefix: str) -> int:
        _read_only("deleting embeddings")


@dataclass(frozen=True)
class RenderedQuery:
    """The SQL SLayer would run, and the names its result columns mean.

    ``result_keys`` maps each column name the warehouse returns to the
    ``model.column`` key SLayer itself would report for it; a name the
    warehouse already returns as its key is not listed.
    """

    sql: str
    result_keys: dict[str, str] = field(default_factory=dict)


class SlayerRenderer:
    """SLayer's query engine, reduced to turning a query into SQL.

    ``now`` pins the clock for every render this instance makes; it defaults to
    the moment of construction, so one command renders against one instant.
    """

    def __init__(
        self,
        models: Sequence[LoadedModel],
        *,
        connector: str,
        now: datetime | None = None,
    ) -> None:
        self.storage = ValidatedModelStorage(models, connector=connector)
        instant = now if now is not None else datetime.now()
        clock: Callable[[], datetime] = lambda: instant  # noqa: E731
        self._engine = SlayerQueryEngine(self.storage, policy=None, clock=clock)

    def render(self, query: Any) -> RenderedQuery:
        """The SQL SLayer would run for ``query``, rendered without executing it."""

        ds_type = self.storage.datasource_type
        # SLayer also raises its grain advisories as Python warnings (an
        # associated grouping, a broadcast); dex states what they mean in the
        # result's notes, and nothing but the envelope may reach the output.
        with slayer_keywords(), warnings.catch_warnings():
            warnings.simplefilter("ignore")
            response = self._engine.execute_sync(query, dry_run=True)
            dialect = dialect_for_ds_type(ds_type)
            emitted = {dialect.emit_alias(key): key for key in response.columns}
            sql = response.sql
            if sql and ds_type == "snowflake":
                sql = pin_snowflake_weeks(sql)
        if not sql:
            raise SemanticBackendError(
                "SLayer rendered no SQL for this query, so there is nothing for "
                "dex to run"
            )
        if ds_type in FOLDS_QUOTED_IDENTIFIERS:
            emitted = _folded(emitted)
        return RenderedQuery(
            sql=sql,
            result_keys={name: key for name, key in emitted.items() if name != key},
        )


def _folded(emitted: dict[str, str]) -> dict[str, str]:
    """``emitted`` keyed by the lowercase name a folding warehouse returns.

    A cluster with case-sensitive identifiers returns the name as spelt, which
    is already its own key, so the folded mapping answers both settings. Two
    keys that fold to one name are refused, since the rows could not say which
    column is which.
    """

    folded: dict[str, str] = {}
    clashes: dict[str, set[str]] = {}
    for name, key in emitted.items():
        lower = name.lower()
        if lower in folded and folded[lower] != key:
            clashes.setdefault(lower, {folded[lower]}).add(key)
        folded[lower] = key
    if clashes:
        named = "; ".join(
            " and ".join(f"'{key}'" for key in sorted(keys))
            for keys in clashes.values()
        )
        raise SemanticBackendError(
            f"the result columns {named} differ only in case, and Redshift "
            "returns a quoted name folded to lowercase, so dex could not tell "
            "them apart in the rows. Rename one of the SLayer columns or "
            "measures, or query them separately"
        )
    return folded


def pin_snowflake_weeks(sql: str) -> str:
    """``sql`` with every Snowflake week truncation pinned to Monday.

    ``DATE_TRUNC('WEEK', x)`` becomes ``DATEADD(DAY, 1 - DAYOFWEEKISO(x),
    DATE_TRUNC('DAY', x))``: the same Monday on an account whose ``WEEK_START``
    is the default, and still Monday on one set to any other day. The
    expression keeps the operand's type (a date stays a date), and SLayer's
    ``week_sunday`` composes this truncation, so it is pinned to Sunday by the
    same rewrite. A statement with no week truncation is returned unchanged,
    byte for byte. Called inside :func:`slayer_keywords`, because the statement
    is regenerated with the identifiers SLayer quoted.
    """

    tree = sqlglot.parse_one(sql, read="snowflake")
    pinned = False

    def pin(node: exp.Expression) -> exp.Expression:
        nonlocal pinned
        if not isinstance(node, (exp.DateTrunc, exp.TimestampTrunc)):
            return node
        if node.text("unit").upper() != "WEEK":
            return node
        pinned = True
        operand = node.this
        offset = exp.Sub(
            this=exp.Literal.number(1),
            expression=exp.Anonymous(this="DAYOFWEEKISO", expressions=[operand.copy()]),
        )
        day = exp.TimestampTrunc(this=operand.copy(), unit=exp.var("DAY"))
        return exp.Anonymous(this="DATEADD", expressions=[exp.var("DAY"), offset, day])

    rewritten = tree.transform(pin)
    if not pinned:
        return sql
    return rewritten.sql(dialect="snowflake", pretty=True)
