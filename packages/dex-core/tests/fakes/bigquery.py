"""A stateful fake of the google-cloud-bigquery Client surface dex uses.

Behavioral, not a mock: it records every query call in order with its dry-run
flag and job config, prices each statement from the referenced tables' sizes,
and enforces ``maximum_bytes_billed`` the way the service does (a real
``BadRequest`` raised at ``result()``, nothing executed). Tests assert against
observable behavior (call ordering, configs, ledger effects) rather than call
signatures. It builds real ``SchemaField`` trees so the adapter's schema
walking sees genuine shapes, and raises real ``google.api_core`` exception
types so error translation is exercised for real.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

from google.api_core import exceptions as api_exceptions
from google.cloud import bigquery

# Statements that reference no known table are priced at this many bytes.
DEFAULT_QUERY_BYTES = 1_000


@dataclass
class FakeTable:
    project: str
    dataset_id: str
    table_id: str
    schema: list[bigquery.SchemaField]
    num_rows: int = 0
    num_bytes: int = 0
    table_type: str = "TABLE"
    require_partition_filter: bool = False
    location: str = "US"

    @property
    def identifier(self) -> str:
        return f"{self.project}.{self.dataset_id}.{self.table_id}"

    @property
    def quoted(self) -> str:
        return ".".join(
            f"`{part}`" for part in (self.project, self.dataset_id, self.table_id)
        )


@dataclass
class FakeQueryCall:
    sql: str
    dry_run: bool
    job_config: Any
    location: str | None


@dataclass
class FakeResult:
    """What an executed statement returns: dict-shaped rows plus an optional
    schema (defaulting to STRING fields named after the first row's keys)."""

    rows: list[dict]
    schema: list[bigquery.SchemaField] | None = None


class FakeRow:
    def __init__(self, values: dict):
        self._values = values

    def __getitem__(self, key: str):
        return self._values[key]

    def get(self, key: str, default=None):
        return self._values.get(key, default)

    def __iter__(self):
        return iter(self._values.values())


class FakeRowIterator(list):
    def __init__(self, rows: list[FakeRow], schema: list[bigquery.SchemaField]):
        super().__init__(rows)
        self.schema = schema


class FakeQueryJob:
    def __init__(
        self,
        *,
        job_id: str,
        total_bytes: int,
        error: Exception | None = None,
        result_payload: FakeResult | None = None,
        dry_run: bool = False,
        location: str | None = None,
    ):
        self.job_id = job_id
        self.location = location
        self.total_bytes_processed = total_bytes
        self.total_bytes_billed = 0 if dry_run else total_bytes
        self._error = error
        self._payload = result_payload or FakeResult(rows=[])
        self._dry_run = dry_run

    def result(self, timeout=None, max_results=None) -> FakeRowIterator:
        if self._error is not None:
            raise self._error
        rows = [FakeRow(values) for values in self._payload.rows]
        if max_results is not None:
            rows = rows[:max_results]
        schema = self._payload.schema
        if schema is None:
            keys = self._payload.rows[0].keys() if self._payload.rows else []
            schema = [bigquery.SchemaField(key, "STRING") for key in keys]
        return FakeRowIterator(rows, schema)


class FakeBigQueryClient:
    """Simulates exactly the client surface the adapter touches; anything else
    raises AttributeError, which is the point (the adapter must not grow calls
    the fake does not vouch for)."""

    def __init__(
        self,
        *,
        project: str,
        tables: list[FakeTable] | None = None,
        row_resolver: Callable[[str], FakeResult | list[dict]] | None = None,
        default_query_bytes: int = DEFAULT_QUERY_BYTES,
        empty_datasets: list[str] | None = None,
    ):
        self.project = project
        self.tables: dict[str, FakeTable] = {t.identifier: t for t in (tables or [])}
        # Datasets that exist but hold no table: the state a scratch dev dataset
        # is in before a first build, which a table-derived registry cannot
        # otherwise express. Bare names qualify against `project`.
        self.empty_datasets = {
            name if "." in name else f"{project}.{name}"
            for name in (empty_datasets or [])
        }
        self.row_resolver = row_resolver
        self.default_query_bytes = default_query_bytes
        self.query_calls: list[FakeQueryCall] = []
        self.cancelled_jobs: list[str] = []
        self.closed = False
        self._job_counter = 0
        # Test knobs. dry_run_underestimate simulates estimate drift (dry-run
        # reports fewer bytes than execution bills), which is what makes the
        # server-side maximum_bytes_billed backstop reachable. result_error is
        # raised at result() on executed jobs (e.g. a TimeoutError).
        self.dry_run_underestimate: float = 1.0
        self.result_error: Exception | None = None

    # --- metadata surface (free API calls) ------------------------------------

    def list_datasets(self, project: str | None = None, max_results: int | None = None):
        """Lazy, like the real client's HTTPIterator: the request goes out (and an
        unreachable project raises NotFound) only when the result is iterated. A
        caller that never drains it learns nothing, which is exactly how a
        not-really-checked project once read as a healthy one."""

        target = project or self.project

        def _pages():
            if target not in self._projects():
                raise api_exceptions.NotFound(f"project not found: {target}")
            dataset_ids = sorted(
                {t.dataset_id for t in self.tables.values() if t.project == target}
                | {
                    name.split(".", 1)[1]
                    for name in self.empty_datasets
                    if name.startswith(f"{target}.")
                }
            )
            for dataset_id in dataset_ids[:max_results] if max_results else dataset_ids:
                yield SimpleNamespace(dataset_id=dataset_id)

        return _pages()

    def get_dataset(self, dataset_ref: str):
        project, dataset_id = str(dataset_ref).rsplit(".", 1)
        known = any(
            t.project == project and t.dataset_id == dataset_id
            for t in self.tables.values()
        )
        if not known and str(dataset_ref) not in self.empty_datasets:
            raise api_exceptions.NotFound(f"dataset not found: {dataset_ref}")
        return SimpleNamespace(dataset_id=dataset_id, project=project)

    def _projects(self) -> set[str]:
        return (
            {self.project}
            | {t.project for t in self.tables.values()}
            | {name.split(".", 1)[0] for name in self.empty_datasets}
        )

    def list_tables(self, dataset_ref: str):
        _project, dataset_id = str(dataset_ref).rsplit(".", 1)
        return [
            SimpleNamespace(table_id=t.table_id, table_type=t.table_type)
            for t in sorted(self.tables.values(), key=lambda t: t.table_id)
            if t.dataset_id == dataset_id
        ]

    def get_table(self, ref: str) -> FakeTable:
        identifier = str(ref)
        if identifier not in self.tables:
            raise api_exceptions.NotFound(f"table not found: {identifier}")
        return self.tables[identifier]

    # --- query surface ---------------------------------------------------------

    def query(self, sql: str, job_config=None, location: str | None = None):
        dry_run = bool(job_config is not None and job_config.dry_run)
        self.query_calls.append(
            FakeQueryCall(
                sql=sql, dry_run=dry_run, job_config=job_config, location=location
            )
        )
        self._job_counter += 1
        job_id = f"fake-job-{self._job_counter}"
        total_bytes = self._bytes_for(sql)

        if dry_run:
            return FakeQueryJob(
                job_id=job_id,
                total_bytes=int(total_bytes * self.dry_run_underestimate),
                dry_run=True,
                location=location,
            )

        error = self.result_error or self._execution_error(sql, job_config, total_bytes)
        payload = None
        if error is None and self.row_resolver is not None:
            resolved = self.row_resolver(sql)
            payload = (
                resolved if isinstance(resolved, FakeResult) else FakeResult(resolved)
            )
        return FakeQueryJob(
            job_id=job_id,
            total_bytes=total_bytes,
            error=error,
            result_payload=payload,
            location=location,
        )

    def cancel_job(self, job_id: str, location: str | None = None):
        self.cancelled_jobs.append(job_id)

    def close(self):
        self.closed = True

    # --- pricing and failure simulation ----------------------------------------

    def _referenced(self, sql: str) -> list[FakeTable]:
        return [t for t in self.tables.values() if t.quoted in sql]

    def _bytes_for(self, sql: str) -> int:
        referenced = self._referenced(sql)
        if not referenced:
            return self.default_query_bytes
        return sum(t.num_bytes for t in referenced)

    def _execution_error(self, sql, job_config, total_bytes) -> Exception | None:
        for table in self._referenced(sql):
            if table.require_partition_filter and "WHERE" not in sql.upper():
                return api_exceptions.BadRequest(
                    f"Cannot query over table '{table.identifier}' without a "
                    "filter over column(s) that can be used for partition "
                    "elimination"
                )
        cap = getattr(job_config, "maximum_bytes_billed", None)
        if cap is not None and total_bytes > cap:
            return api_exceptions.BadRequest(
                f"Query exceeded limit for bytes billed: {cap}. "
                f"{total_bytes} or higher required."
            )
        return None
