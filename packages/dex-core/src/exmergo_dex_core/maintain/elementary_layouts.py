"""The Elementary reader checks table layouts and refuses one it does not know (#1469).

Dex reads four of the Elementary package's tables as evidence
(``elementary_test_results``, ``dbt_invocations``, ``test_result_rows`` and
``data_monitoring_metrics``). Their layout and meaning can change between
releases without the package calling it a breaking change, and Dex does not go
through Elementary's own CLI, which would absorb that. So the reader holds each
table to a layout recorded per release, from the package's model files at that
release's tag, before the first query touches it:

- **The release first.** A release the reader has no recorded layout for is
  refused with #1445's codes (``elementary_release_untested``, or
  ``elementary_release_unknown`` where no release can be read), and the catalog
  is never asked. The reader's releases are its own and can be fewer than
  :data:`~exmergo_dex_core.transform.elementary.TESTED_RELEASES`, never more:
  loading the layouts refuses a release that is not one the fixture runs. Each
  release is matched exactly, so a release between two recorded ones is
  untested here, since nobody read its layout.
- **Then the catalog.** Each table's columns, as the warehouse's catalog lists
  them, are matched by name, case-insensitively (Snowflake folds unquoted names
  to upper case and keeps a quoted reserved word as written). Every column the
  reader uses must be there, in its type family. A table the catalog lists no
  column for is ``elementary_table_missing``: an empty answer is never a pass.
  A column that is gone is ``elementary_column_missing`` and one in another
  family is ``elementary_column_retyped``, each naming the table and the
  column.
- **A bounded check.** The catalog is another system's output, so the check
  reads at most :data:`MAX_CATALOG_COLUMNS` plus one of a table's columns and
  refuses a table with more (``elementary_catalog_too_large``). That bounds the
  check, not the connector: every adapter's ``table_columns``,
  ``table_metadata`` and ``list_objects`` fetches its whole answer first.

A catalog check sees names and types. It cannot see a column that kept both and
changed its meaning, which is why each release here also has a fixture whose
rows the reader's tests read back.

The layouts live in ``elementary_layouts.yml`` beside this module, the one file
the reader, its tests and the demo load. Reading a catalog is a metadata read:
free on every connector but Redshift Serverless, whose
``catalog_read_billed()`` says it wakes the workgroup, so a caller prices it
there as it prices any other read.
"""

from __future__ import annotations

import functools
import itertools
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from importlib import resources
from typing import Any

import yaml

from ..adapters.base import ColumnMeta
from ..errors import DexError
from ..transform.elementary import (
    RELEASE_UNKNOWN,
    RELEASE_UNTESTED,
    TESTED_RELEASES,
    ReleaseStatus,
    release_key,
)

TABLE_MISSING = "elementary_table_missing"
COLUMN_MISSING = "elementary_column_missing"
COLUMN_RETYPED = "elementary_column_retyped"
CATALOG_TOO_LARGE = "elementary_catalog_too_large"

#: The tables the reader reads, which every recorded release must lay out.
TABLES: tuple[str, ...] = (
    "elementary_test_results",
    "dbt_invocations",
    "test_result_rows",
    "data_monitoring_metrics",
)

#: The most columns read from one table's catalog listing. The widest table at
#: 0.26.1 declares 35, and ``on_schema_change: append_new_columns`` only adds
#: the columns later releases declare, so a listing past this is not a table
#: the package built.
MAX_CATALOG_COLUMNS = 500

#: Each type name the package's ``empty_column`` casts by, and the family the
#: adapter's type for it falls in. Any other name casts to the string type.
_ELEMENTARY_FAMILIES: Mapping[str, str] = {
    "boolean": "boolean",
    "timestamp": "timestamp",
    "int": "integer",
    "bigint": "integer",
    "float": "float",
    "long_string": "text",
    "string": "text",
}

#: Catalog type names by family, as each connector Dex has spells them.
_CATALOG_FAMILIES: Mapping[str, frozenset[str]] = {
    "text": frozenset({"VARCHAR", "TEXT", "STRING", "NVARCHAR", "CHARACTER VARYING"}),
    "integer": frozenset(
        {
            "TINYINT",
            "SMALLINT",
            "INT",
            "INTEGER",
            "BIGINT",
            "HUGEINT",
            "INT2",
            "INT4",
            "INT8",
            "INT16",
            "INT32",
            "INT64",
            "INT128",
            "INT256",
            "UINT8",
            "UINT16",
            "UINT32",
            "UINT64",
            "UINT128",
            "UINT256",
            # Databricks' type_name, where a column has no type_text.
            "LONG",
            "SHORT",
            "BYTE",
        }
    ),
    "float": frozenset(
        {
            "REAL",
            "FLOAT",
            "FLOAT4",
            "FLOAT8",
            "FLOAT32",
            "FLOAT64",
            "DOUBLE",
            "DOUBLE PRECISION",
        }
    ),
    "timestamp": frozenset({"DATETIME", "DATETIME2", "DATETIME64"}),
    "boolean": frozenset({"BOOLEAN", "BOOL"}),
}

#: Exact numeric types, an integer where their scale is absent or zero.
#: Snowflake's catalog token ``FIXED`` carries no scale, and is how it lists
#: every ``NUMBER``, ``bigint`` included. So a scale the catalog does not show
#: (Snowflake's token, a bare ``numeric`` on Postgres or Redshift) reads as an
#: integer: an integer column retyped to such a decimal passes. The package
#: declares no decimal column at 0.26.1.
_EXACT_NUMERIC = frozenset({"NUMBER", "NUMERIC", "DECIMAL", "FIXED"})

#: Wrappers that say nothing about the family (ClickHouse).
_WRAPPERS = re.compile(r"^(?:NULLABLE|LOWCARDINALITY)\((.*)\)$")


@dataclass(frozen=True)
class TableLayout:
    """One table at one release: every declared column, and those the reader uses."""

    table: str
    source: str
    #: ``(name, the package's type name)``, in the package's order.
    columns: tuple[tuple[str, str], ...]
    reads: tuple[str, ...]

    def family(self, column: str) -> str:
        """The type family of a declared column."""

        [kind] = [kind for name, kind in self.columns if name == column]
        return _ELEMENTARY_FAMILIES[kind]


@dataclass(frozen=True)
class ReleaseLayout:
    """Every recorded table at one release."""

    release: str
    tables: Mapping[str, TableLayout]


@dataclass(frozen=True)
class LayoutProblem:
    """One way the catalog differs from the recorded layout."""

    code: str
    table: str
    column: str | None = None
    expected: str | None = None
    found: str | None = None

    def render(self) -> str:
        where = self.table if self.column is None else f"{self.table}.{self.column}"
        if self.code == TABLE_MISSING:
            return f"{where}: the catalog lists no column for this table"
        if self.code == COLUMN_MISSING:
            return f"{where}: the column is not in the table"
        if self.code == COLUMN_RETYPED:
            return (
                f"{where}: recorded as {self.expected}, the catalog says {self.found}"
            )
        return f"{where}: the catalog lists more than {MAX_CATALOG_COLUMNS} columns"


class ElementaryLayoutError(DexError):
    """The reader refused: a release it has no layout for, or a layout it does
    not know.

    ``code`` is the first reason: a release code from #1445, or one of
    :data:`TABLE_MISSING`, :data:`COLUMN_MISSING`, :data:`COLUMN_RETYPED` and
    :data:`CATALOG_TOO_LARGE`. ``problems`` lists every table and column that
    differs (empty for a release refusal).
    """

    def __init__(
        self,
        code: str,
        release: str | None,
        problems: Sequence[LayoutProblem] = (),
    ) -> None:
        self.code = code
        self.release = release
        self.problems = tuple(problems)
        super().__init__(self._message())

    def _message(self) -> str:
        if not self.problems:
            if self.code == RELEASE_UNKNOWN:
                return "the installed Elementary release cannot be read"
            return (
                f"Elementary {self.release} has no recorded table layout; the "
                f"reader reads {', '.join(READER_RELEASES)}"
            )
        lines = "; ".join(problem.render() for problem in self.problems)
        return f"Elementary {self.release}'s tables differ from its layout: {lines}"


@dataclass(frozen=True)
class CheckedTable:
    """A table the catalog matched: each column the reader uses, as the
    catalog spells it, so a query quotes the name the warehouse holds."""

    table: str
    columns: Mapping[str, str]


@dataclass(frozen=True)
class LayoutCheck:
    """A passed check: the release whose layout held, and each table checked."""

    release: str
    tables: tuple[CheckedTable, ...]


#: One table's columns as the catalog lists them. An empty answer, or a
#: ``LookupError``, is a table the catalog does not have.
Catalog = Callable[[str], Iterable[ColumnMeta]]


def _parse_table(table: str, raw: Mapping[str, Any]) -> TableLayout:
    columns = tuple((str(name), str(kind)) for name, kind in raw["columns"])
    names = [name for name, _ in columns]
    reads = tuple(str(name) for name in raw["reads"])
    if len(set(names)) != len(names):
        raise ValueError(f"{table} declares a column twice")
    # The package casts any other name to its string type, but a name this file
    # spells wrong would then read as text, so only the names it casts by pass.
    if odd := sorted({kind for _, kind in columns} - set(_ELEMENTARY_FAMILIES)):
        raise ValueError(f"{table} records type names the package does not use: {odd}")
    if unknown := [name for name in reads if name not in names]:
        raise ValueError(f"{table} reads undeclared columns {unknown}")
    return TableLayout(table, str(raw["source"]), columns, reads)


def _parse_release(release: str, raw: Mapping[str, Any]) -> ReleaseLayout:
    if release_key(release) is None:
        raise ValueError(f"{release!r} is not a release")
    tested = {release_key(tested) for tested in TESTED_RELEASES}
    if release_key(release) not in tested:
        raise ValueError(
            f"{release} has a recorded layout but is not in TESTED_RELEASES; "
            "the reader may read only releases the Elementary fixture runs"
        )
    tables = {
        str(name): _parse_table(str(name), table)
        for name, table in raw["tables"].items()
    }
    # A release recording fewer tables, or a table reading nothing, would pass
    # a catalog that holds none of what the reader reads.
    if missing := sorted(set(TABLES) - set(tables)):
        raise ValueError(f"{release} records no layout for {missing}")
    if empty := sorted(name for name, table in tables.items() if not table.reads):
        raise ValueError(f"{release} reads no column of {empty}")
    return ReleaseLayout(release, tables)


class _UniqueKeyLoader(yaml.SafeLoader):
    """A safe loader that refuses a key written twice in one mapping, at any
    level. YAML keeps the second, so a second release, table or column block
    would otherwise replace the first before any check could see it."""

    def construct_mapping(self, node: yaml.MappingNode, deep: bool = False):
        keys = [self.construct_object(key, deep=deep) for key, _ in node.value]
        if repeated := sorted({str(k) for k in keys if keys.count(k) > 1}):
            raise ValueError(f"the layout file writes a key twice: {repeated}")
        return super().construct_mapping(node, deep=deep)


def parse_layouts(text: str) -> Mapping[str, ReleaseLayout]:
    """The layouts a file holds, by release, refusing a malformed one."""

    raw = yaml.load(text, Loader=_UniqueKeyLoader)  # noqa: S506 (a SafeLoader)
    layouts = {
        str(release): _parse_release(str(release), body)
        for release, body in raw["releases"].items()
    }
    if not layouts:
        raise ValueError("the layout file records no release")
    keys = [release_key(release) for release in layouts]
    if len(set(keys)) != len(keys):
        raise ValueError(f"the layout file records one release twice: {list(layouts)}")
    return layouts


@functools.cache
def layouts() -> Mapping[str, ReleaseLayout]:
    """The shipped layouts, read once from ``elementary_layouts.yml``."""

    asset = resources.files("exmergo_dex_core.maintain") / "elementary_layouts.yml"
    return parse_layouts(asset.read_text(encoding="utf-8"))


def _by_key(release: str) -> tuple:
    return release_key(release) or ()


#: The releases the reader supports, oldest first: those with a recorded layout.
READER_RELEASES: tuple[str, ...] = tuple(sorted(layouts(), key=_by_key))


def reader_status(release: str | None) -> ReleaseStatus:
    """``supported`` for a release with a recorded layout, matched exactly;
    ``untested`` for any other release; ``unknown`` where none can be read."""

    key = release_key(release)
    if key is None:
        return "unknown"
    if any(key == release_key(known) for known in READER_RELEASES):
        return "supported"
    return "untested"


def layout_for(release: str | None) -> ReleaseLayout:
    """The recorded layout for ``release``, or the release refusal."""

    status = reader_status(release)
    if status == "unknown":
        raise ElementaryLayoutError(RELEASE_UNKNOWN, release)
    if status == "untested":
        raise ElementaryLayoutError(RELEASE_UNTESTED, release)
    key = release_key(release)
    [known] = [known for known in READER_RELEASES if release_key(known) == key]
    return layouts()[known]


def type_family(data_type: str) -> str | None:
    """The family a catalog type name falls in, ``None`` where it is none of
    ``text``, ``integer``, ``float``, ``timestamp`` and ``boolean``."""

    text = " ".join(data_type.split()).upper()
    while match := _WRAPPERS.match(text):
        text = match.group(1).strip()
    # A list of any type (Postgres and DuckDB write `TIMESTAMP[]`) is no
    # family, whatever it is a list of.
    if "[" in text:
        return None
    if text.startswith("TIMESTAMP"):
        return "timestamp"
    base = re.split(r"[(<]", text, maxsplit=1)[0].strip()
    if base in _EXACT_NUMERIC:
        scale = re.search(r",\s*(\d+)\s*\)", text)
        return "integer" if scale is None or int(scale.group(1)) == 0 else None
    for family, names in _CATALOG_FAMILIES.items():
        if base in names:
            return family
    return None


def _listed(table: str, catalog: Catalog) -> list[ColumnMeta]:
    try:
        return list(itertools.islice(catalog(table), MAX_CATALOG_COLUMNS + 1))
    except LookupError:
        return []


def _check_table(
    layout: TableLayout, catalog: Catalog
) -> tuple[CheckedTable | None, list[LayoutProblem]]:
    listed = _listed(layout.table, catalog)
    if not listed:
        return None, [LayoutProblem(TABLE_MISSING, layout.table)]
    if len(listed) > MAX_CATALOG_COLUMNS:
        return None, [LayoutProblem(CATALOG_TOO_LARGE, layout.table)]
    by_name = {column.name.casefold(): column for column in listed}
    problems: list[LayoutProblem] = []
    spelled: dict[str, str] = {}
    for name in layout.reads:
        found = by_name.get(name.casefold())
        expected = layout.family(name)
        if found is None:
            problems.append(LayoutProblem(COLUMN_MISSING, layout.table, name))
        elif type_family(found.data_type) != expected:
            problems.append(
                LayoutProblem(
                    COLUMN_RETYPED, layout.table, name, expected, found.data_type
                )
            )
        else:
            spelled[name] = found.name
    return CheckedTable(layout.table, spelled), problems


def check_layouts(
    release: str | None,
    catalog: Catalog,
    *,
    tables: Sequence[str] | None = None,
) -> LayoutCheck:
    """Hold the catalog's tables to ``release``'s recorded layout.

    The release is settled before ``catalog`` is called, so a release the
    reader has no layout for never reaches the warehouse. ``tables`` narrows
    the check to the tables a caller reads; by default every recorded table is
    checked. Raises :class:`ElementaryLayoutError` naming every problem.
    """

    layout = layout_for(release)
    selected = list(layout.tables) if tables is None else list(tables)
    if not selected:
        raise ValueError("no table to check: a check of nothing is not a pass")
    if unknown := [table for table in selected if table not in layout.tables]:
        raise ValueError(f"no recorded layout for {unknown}")
    checked: list[CheckedTable] = []
    problems: list[LayoutProblem] = []
    for table in selected:
        result, found = _check_table(layout.tables[table], catalog)
        problems.extend(found)
        if result is not None:
            checked.append(result)
    if problems:
        raise ElementaryLayoutError(problems[0].code, layout.release, problems)
    return LayoutCheck(layout.release, tuple(checked))


def adapter_catalog(adapter: Any, namespace: str) -> Catalog:
    """A :data:`Catalog` over ``adapter`` for the tables in ``namespace``.

    ``namespace`` is the schema the package writes, qualified as the adapter
    names relations (``dev.main_elementary`` on DuckDB, whose relations are
    ``database.schema.name``). It reads the adapter's ``table_columns`` where
    it offers one (DuckDB, whose ``table_metadata`` also counts rows) and
    ``table_metadata`` otherwise, which answers from the catalog alone. Each
    relation is first found in ``list_objects()``, the adapter's scoped
    inventory, and named as the inventory spells it.
    """

    columns = getattr(adapter, "table_columns", None)
    listed: dict[str, str] = {}

    def listing(table: str) -> Iterable[ColumnMeta]:
        # Every adapter but DuckDB raises its own connection error for a
        # relation outside its inventory, so the inventory is asked first, once,
        # and a relation it does not list reads as missing. A namespace outside
        # the configured scope therefore reads as every table missing.
        if not listed:
            listed.update(
                (meta.identifier.casefold(), meta.identifier)
                for meta in adapter.list_objects()
            )
        identifier = listed.get(f"{namespace}.{table}".casefold())
        if identifier is None:
            raise LookupError(f"{namespace}.{table}")
        if columns is not None:
            return columns(identifier)
        return adapter.table_metadata(identifier)[1]

    return listing
