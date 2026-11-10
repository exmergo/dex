"""Draft the project override that repairs a failing package model (#1229).

A package model that fails to build blocks every deliverable that reads it, and
the repair dbt supports (#860) is a project model of the same name with the
package's node disabled. Writing it by hand is mechanical work that goes wrong
in the same few ways: a package macro called unqualified no longer resolves
from the project, a ``var()`` the package's own ``dbt_project.yml`` set is
undefined in the project (or, worse, the project defines it to read the model
being overridden, which is a cycle), the package's materialization and schema
are lost, and the conversion itself is guessed. This module does that work.

What it changes, and nothing else:

- the package's own SQL (``raw_code``), copied with every unqualified call to a
  macro the package defines qualified with the package's name;
- each ``var()`` read whose value the override would not see as the package
  did, resolved to the package's value (a relation-valued read is inlined);
- the package node's resolved ``materialized``, ``schema`` and ``alias``;
- inside the calls the warehouse error names, the columns of the offending type,
  converted only where the cached profile settles what they hold. A column the
  evidence does not settle is left as the package wrote it, ``unresolved``.

It writes nothing: the edits become an ordinary plan through ``transform plan``.
It never touches ``dbt_packages/`` and never adds a filter.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from ..errors import RequestError

#: Where an override lives under the project's first model path.
OVERRIDE_DIR = "overrides"

#: The raw SQL call sites that can carry each failing function, by the name the
#: error gives it. A BigQuery ``Invalid cast ... to TIMESTAMP`` comes out of
#: ``dbt.date_trunc`` (which casts its argument) as often as an explicit cast.
_SITE_FUNCTIONS = {
    "coalesce": ("coalesce",),
    "date_trunc": ("date_trunc", "timestamp_trunc", "datetime_trunc"),
    "cast": ("date_trunc", "timestamp_trunc", "datetime_trunc", "cast", "safe_cast"),
}

_INTEGER_PREFIXES = (
    "INT",
    "BIGINT",
    "SMALLINT",
    "TINYINT",
    "HUGEINT",
    "UBIGINT",
    "UINTEGER",
    "USMALLINT",
    "UTINYINT",
    "UHUGEINT",
)
_STRING_PREFIXES = ("VARCHAR", "STRING", "TEXT", "CHAR", "NVARCHAR", "BPCHAR")


class RepairRefusedError(RequestError):
    """The node cannot be drafted as an override; the message says why."""


def family(data_type: str | None) -> str | None:
    """``integer``, ``string``, ``timestamp``, ``date`` or ``None``."""

    upper = (data_type or "").strip().upper()
    if not upper or upper == "STRING_LITERAL":
        return None
    if upper.startswith(("TIMESTAMP", "DATETIME")):
        return "timestamp"
    if upper == "DATE":
        return "date"
    if upper.startswith(_INTEGER_PREFIXES) and not upper.startswith("INTERVAL"):
        return "integer"
    if upper.startswith(_STRING_PREFIXES):
        return "string"
    return None


@dataclass(frozen=True)
class TypeFailure:
    """What a warehouse typing error says: the function, the argument types it
    named, the family that did not fit, and the family the call needed."""

    function: str
    types: tuple[str, ...]
    offending: str | None
    target: str | None

    def record(self) -> dict[str, Any]:
        """The failure as ``data.repair.error`` reports it."""

        return {
            "recognized": True,
            "function": self.function,
            "types": list(self.types),
            "offending_type": self.offending,
            "target_type": self.target,
        }


_FUNCTION_NAMES = {
    "coalesce": "coalesce",
    "date_trunc": "date_trunc",
    "timestamp_trunc": "date_trunc",
    "datetime_trunc": "date_trunc",
}


def _failure(function: str, types: list[str]) -> TypeFailure | None:
    name = _FUNCTION_NAMES.get(function.lower())
    if name is None:
        return None
    families = [family(t) for t in types]
    offending = next((f for f in families if f in ("integer", "string")), None)
    if name == "coalesce":
        target = next((f for f in families if f in ("timestamp", "date")), None)
    else:
        target = "timestamp"
    return TypeFailure(name, tuple(t.strip() for t in types), offending, target)


_DUCKDB_MIX = re.compile(
    r"Cannot mix values of type ([A-Z_ ]+?) and ([A-Z_ ]+?) in (\w+)", re.IGNORECASE
)
_DUCKDB_NO_FUNCTION = re.compile(
    r"No function matches the given name and argument types '(\w+)\(([^)]*)\)'",
    re.IGNORECASE,
)
# BigQuery has written both "... COALESCE for argument types: A, B" and, more
# recently, "... COALESCE" with "Argument types: A, B" on the next line.
_BIGQUERY_SIGNATURE = re.compile(
    r"No matching signature for (?:function|operator) (\w+)\s*"
    r"(?:for argument types:|Argument types:)\s*([A-Z0-9_, <>]+)",
    re.IGNORECASE,
)
_BIGQUERY_CAST = re.compile(r"Invalid cast from (\w+) to (\w+)", re.IGNORECASE)


def parse_type_failure(message: str | None) -> TypeFailure | None:
    """The typing failure a dbt message reports, or ``None`` where Dex does not
    recognise it. DuckDB and BigQuery are read; any other text is ``None``."""

    if not message:
        return None
    if match := _DUCKDB_MIX.search(message):
        return _failure(match.group(3), [match.group(1), match.group(2)])
    if match := _DUCKDB_NO_FUNCTION.search(message):
        return _failure(match.group(1), match.group(2).split(","))
    if match := _BIGQUERY_SIGNATURE.search(message):
        return _failure(match.group(1), match.group(2).split(","))
    if match := _BIGQUERY_CAST.search(message):
        source, target = family(match.group(1)), family(match.group(2))
        if target in ("timestamp", "date"):
            return TypeFailure(
                "cast",
                (match.group(1), match.group(2)),
                source if source in ("integer", "string") else None,
                target,
            )
    return None


# --- conversions --------------------------------------------------------------

#: Per connector: the string type a NULL passes through, and the epoch readers.
#: An expression here may land inside a quoted macro argument
#: (``dbt.date_trunc('day', '<here>')``), so none of them carries a quote.
_DIALECT = {
    "duckdb": {
        "string": "varchar",
        "epoch_seconds": "epoch_ms(cast({c} as bigint) * 1000)",
        "epoch_millis": "epoch_ms(cast({c} as bigint))",
    },
    "bigquery": {
        "string": "string",
        "epoch_seconds": "timestamp_seconds(cast({c} as int64))",
        "epoch_millis": "timestamp_millis(cast({c} as int64))",
    },
    "snowflake": {
        "string": "varchar",
        "epoch_seconds": "to_timestamp_ntz(cast({c} as number), 0)",
        "epoch_millis": "to_timestamp_ntz(cast({c} as number), 3)",
    },
}


def conversion_sql(kind: str, target: str, connector: str, column: str) -> str | None:
    """The expression that reads ``column`` as ``target``, or ``None`` where
    ``connector`` has no reviewed spelling for it."""

    spelled = _DIALECT.get(connector)
    if spelled is None:
        return None
    if kind == "null_cast":
        return f"cast(cast({column} as {spelled['string']}) as {target})"
    if kind == "iso_parse":
        return f"cast({column} as {target})"
    template = spelled.get(kind)
    if template is None:
        return None
    expression = template.format(c=column)
    return expression if target == "timestamp" else f"cast({expression} as date)"


@dataclass
class Conversion:
    """One offending column and what the evidence settles about it."""

    column: str
    status: str = "unresolved"
    relation: str | None = None
    from_type: str | None = None
    to_type: str | None = None
    kind: str | None = None
    expression: str | None = None
    meaning_changing: bool | None = None
    evidence: str | None = None
    detail: str | None = None
    reason_code: str | None = None
    sites: int = 0
    next: str | None = None
    #: The failing call the conversion is written inside (``coalesce``, ...).
    call: str | None = None
    #: The exact text written at each site, so a later pass can blank it.
    written: list[str] = field(default_factory=list)

    def record(self) -> dict[str, Any]:
        """The conversion as ``data.repair.conversions[]`` reports it."""

        return {
            key: value
            for key, value in self.__dict__.items()
            if value is not None
            and key != "written"
            and not (key == "sites" and value == 0)
        }


_SHAPE_KINDS = {
    "epoch_seconds": ("epoch_seconds", True),
    "epoch_millis": ("epoch_millis", True),
    "iso_date": ("iso_parse", False),
    "iso_datetime": ("iso_parse", False),
}


def settle(
    conversion: Conversion,
    profile: Any,
    dataset: Any,
    failure: TypeFailure,
    connector: str,
) -> None:
    """Fill ``conversion`` from the column's cached profile, or say why not.

    ``profile`` is the cache's :class:`ColumnProfile`; ``dataset`` the
    :class:`Dataset` it belongs to. Evidence is read, never guessed: a column
    whose profile settles nothing stays ``unresolved``.
    """

    from ..explore.profile import content_shape

    conversion.from_type = profile.data_type
    conversion.to_type = failure.target
    observed = family(profile.data_type)
    if observed != failure.offending:
        conversion.reason_code = "stale_profile"
        conversion.detail = (
            f"the cached profile types {conversion.column} {profile.data_type}, "
            f"not the {failure.offending} the error names"
        )
        conversion.next = f"dex explore profile {dataset.identifier}"
        return
    rows = dataset.row_count
    if profile.null_fraction == 1.0 or rows == 0:
        kind, changing = "null_cast", False
        conversion.detail = (
            f"every row of {dataset.identifier}.{conversion.column} is NULL in the "
            f"profile of {dataset.profiled_at or 'an unknown time'}, so the cast "
            "changes no value; a value arriving later still has to read as "
            f"{failure.target} text"
        )
    elif getattr(profile, "pii", None) is not None:
        conversion.reason_code = "pii_flagged"
        conversion.detail = (
            f"{conversion.column} is flagged as personal data, so the profile "
            "measured no content shape for it"
        )
        return
    else:
        shape = content_shape(list(dataset.data_quality or []), conversion.column)
        if shape is None or (
            shape.startswith("iso") != (failure.offending == "string")
        ):
            conversion.reason_code = "ambiguous_content"
            conversion.detail = (
                f"the profile of {dataset.identifier}.{conversion.column} shows "
                "neither all NULL nor one temporal shape on nearly every value"
            )
            return
        kind, changing = _SHAPE_KINDS[shape]
        conversion.detail = {
            "epoch_seconds": "nearly every non-null value is in the unix-epoch-"
            "seconds range, so it is read as seconds since 1970-01-01 UTC",
            "epoch_millis": "nearly every non-null value is in the unix-epoch-"
            "milliseconds range, so it is read as milliseconds since 1970-01-01 UTC",
            "iso_date": "nearly every non-null value is a YYYY-MM-DD date",
            "iso_datetime": "nearly every non-null value is a "
            "YYYY-MM-DD HH:MM:SS timestamp",
        }[shape]
    expression = conversion_sql(kind, failure.target or "timestamp", connector, "{c}")
    if expression is None:
        conversion.reason_code = "unsupported_on_connector"
        conversion.detail = f"Dex has no reviewed {kind} spelling for {connector}"
        return
    conversion.status = "resolved"
    conversion.kind = kind
    conversion.meaning_changing = changing
    conversion.evidence = "data"
    conversion.expression = expression


# --- locating and rewriting call sites -----------------------------------------

_COMMENT = re.compile(r"\{#.*?#\}|--[^\n]*|/\*.*?\*/", re.DOTALL)


def _masked(sql: str) -> str:
    """``sql`` with its comments blanked, offsets and newlines kept."""

    return _COMMENT.sub(
        lambda m: re.sub(r"[^\n]", " ", m.group(0)),
        sql,
    )


def _closing(text: str, open_paren: int) -> int | None:
    depth = 0
    for index in range(open_paren, len(text)):
        if text[index] == "(":
            depth += 1
        elif text[index] == ")":
            depth -= 1
            if depth == 0:
                return index
    return None


def call_spans(sql: str, function: str) -> list[tuple[int, int]]:
    """Each call site in ``sql`` that can carry ``function``, as the span of its
    arguments. A dotted macro spelling counts (``dbt.date_trunc(``)."""

    names = _SITE_FUNCTIONS.get(function, (function,))
    masked = _masked(sql)
    pattern = re.compile(
        r"(?<![\w])(?:\w+\.)?(?:" + "|".join(map(re.escape, names)) + r")\s*\(",
        re.IGNORECASE,
    )
    spans = []
    for match in pattern.finditer(masked):
        close = _closing(masked, match.end() - 1)
        if close is not None:
            spans.append((match.end(), close))
    return spans


def _occurrences(
    sql: str, spans: list[tuple[int, int]], column: str
) -> list[tuple[int, int]]:
    """Where ``column`` is read inside the spans, as an expression: optionally
    qualified, never an alias, a function name or part of another name."""

    masked = _masked(sql)
    token = re.compile(
        r"(?<![\w.])(?:[A-Za-z_]\w*\.)?" + re.escape(column) + r"(?![\w(])",
        re.IGNORECASE,
    )
    found: set[tuple[int, int]] = set()
    for start, end in spans:
        for match in token.finditer(masked, start, end):
            before = masked[start : match.start()].rstrip().lower()
            if before.endswith(" as") or before == "as":
                continue
            found.add((match.start(), match.end()))
    return sorted(found)


def columns_in_sites(sql: str, function: str) -> list[str]:
    """Every identifier-looking token inside the call sites, lowercased, in
    first-seen order: the candidates a profile's column list narrows."""

    masked = _masked(sql)
    seen: dict[str, None] = {}
    for start, end in call_spans(sql, function):
        for match in re.finditer(
            r"(?<![\w])([A-Za-z_]\w*)(?![\w(.])", masked[start:end]
        ):
            seen.setdefault(match.group(1).lower(), None)
    return list(seen)


def apply_conversions(
    sql: str,
    function: str,
    conversions: list[Conversion],
    *,
    visible: str | None = None,
) -> str:
    """``sql`` with each resolved conversion written over its column inside the
    failing calls. Sets ``sites`` on every conversion; a resolved one that no
    site reads becomes ``unresolved`` (``site_not_found``)."""

    # Sites and columns are found in ``visible``: ``sql`` with the conversions
    # an earlier pass wrote blanked out, so a cast that pass inserted is never
    # read as a new site, nor its column converted twice. Same offsets.
    seen = visible if visible is not None else sql
    spans = call_spans(seen, function)
    replacements: list[tuple[int, int, str]] = []
    for conversion in conversions:
        places = _occurrences(seen, spans, conversion.column)
        conversion.sites = len(places)
        if conversion.status != "resolved":
            continue
        if not places:
            conversion.status = "unresolved"
            conversion.reason_code = "site_not_found"
            conversion.detail = (
                f"no call the error names reads {conversion.column} in the "
                "package's SQL"
            )
            continue
        for start, end in places:
            text = (conversion.expression or "{c}").format(c=sql[start:end])
            conversion.written.append(text)
            replacements.append((start, end, text))

    for start, end, text in sorted(replacements, reverse=True):
        sql = sql[:start] + text + sql[end:]
    for conversion in conversions:
        if conversion.status == "resolved" and conversion.expression:
            conversion.expression = conversion.expression.format(c=conversion.column)
    return sql


# --- restating the package's SQL ------------------------------------------------


def qualify_macros(sql: str, package: str, macros: set[str]) -> tuple[str, list[str]]:
    """Every unqualified call inside a jinja region to a macro ``package``
    defines, prefixed with ``package.``. Returns the SQL and the names changed.

    From the package's own node an unqualified name resolves to the package's
    macro first; from the project it does not, so the restated call names it.
    """

    from ..dbt_project import jinja_regions

    if not macros:
        return sql, []
    regions, _masked_source = jinja_regions(sql)
    edits: list[tuple[int, int, str]] = []
    changed: dict[str, None] = {}
    for region in regions:
        for call in region.calls:
            if call.callee not in macros or call.callee_span is None:
                continue
            start, _end = call.callee_span
            if start > 0 and sql[start - 1] == ".":
                continue
            edits.append((start, start, f"{package}."))
            changed.setdefault(call.callee, None)
    for start, end, text in sorted(edits, reverse=True):
        sql = sql[:start] + text + sql[end:]
    return sql, list(changed)


_MISSING = object()


def _jinja_literal(value: Any) -> str:
    """``value`` as a jinja literal: JSON, with ``none`` for null."""

    if value is None:
        return "none"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list):
        return "[" + ", ".join(_jinja_literal(v) for v in value) + "]"
    if isinstance(value, dict):
        return (
            "{"
            + ", ".join(
                f"{json.dumps(str(k))}: {_jinja_literal(v)}" for k, v in value.items()
            )
            + "}"
        )
    return json.dumps(value)


@dataclass
class VarScope:
    """The ``vars:`` each side of the override sees.

    ``root`` is the project's whole ``vars:`` mapping, ``package_vars`` the
    package's own, ``scopes`` every name a ``vars:`` key can be scoped to (the
    project and each installed package).
    """

    root_project: str
    package: str
    root: Mapping[str, Any]
    package_vars: Mapping[str, Any]
    scopes: set[str]

    def _global(self, mapping: Mapping[str, Any]) -> dict[str, Any]:
        return {
            str(k): v
            for k, v in mapping.items()
            if not (str(k) in self.scopes and isinstance(v, Mapping))
        }

    def _scoped(self, mapping: Mapping[str, Any], name: str) -> dict[str, Any]:
        scoped = mapping.get(name)
        return dict(scoped) if isinstance(scoped, Mapping) else {}

    def package_sees(self, name: str) -> Any:
        """What the package's node read for ``name``: the project scoped to the
        package, then the project's globals, then the package's own vars."""

        for layer in (
            self._scoped(self.root, self.package),
            self._global(self.root),
            self._scoped(self.package_vars, self.package),
            self._global(self.package_vars),
        ):
            if name in layer:
                return layer[name]
        return _MISSING

    def override_sees(self, name: str) -> Any:
        """What a project model reads for ``name``."""

        for layer in (
            self._scoped(self.root, self.root_project),
            self._global(self.root),
        ):
            if name in layer:
                return layer[name]
        return _MISSING

    def package_names(self) -> set[str]:
        """Every var the package's view defines."""

        names: set[str] = set()
        for layer in (
            self._scoped(self.root, self.package),
            self._global(self.root),
            self._scoped(self.package_vars, self.package),
            self._global(self.package_vars),
        ):
            names |= set(layer)
        return names


def resolve_vars(
    sql: str, scope: VarScope
) -> tuple[str, list[dict[str, Any]], dict[str, Any], list[str]]:
    """Make each ``var()`` the package SQL reads give the override what it gave
    the package. Returns the SQL, what changed (``vars``), the defaults to add
    under ``vars.<root project>`` in ``dbt_project.yml``, and warnings.

    A relation-valued var read as a whole ``{{ var('x') }}`` is inlined, which is
    what breaks a cycle where the project's own ``x`` reads the overridden model.
    """

    from ..dbt_project import jinja_regions

    regions, masked = jinja_regions(sql)
    edits: list[tuple[int, int, str]] = []
    changed: list[dict[str, Any]] = []
    defaults: dict[str, Any] = {}
    warnings: list[str] = []
    read: set[str] = set()
    for region in regions:
        for call in region.calls:
            if call.callee != "var" or not call.args or call.args[0] is None:
                continue
            if call.callee_span is None:
                continue
            name = call.args[0]
            read.add(name)
            seen, override = scope.package_sees(name), scope.override_sees(name)
            if seen is _MISSING or seen == override:
                continue
            start = call.callee_span[0]
            close = _closing(masked, call.callee_span[1])
            if close is None or (start > 0 and sql[start - 1] == "."):
                continue
            relation = isinstance(seen, str) and "{{" in seen
            if relation and call.spans_region and region.kind == "expression":
                edits.append((region.start, region.end, seen.strip()))
                changed.append({"var": name, "change": "inlined"})
                continue
            if relation:
                warnings.append(
                    f"var('{name}') is a relation the package resolves as "
                    f"{seen!r}, read inside a larger expression; the draft keeps "
                    "the read, so check what the project gives it"
                )
                continue
            if override is _MISSING:
                text = f"var({json.dumps(name)}, {_jinja_literal(seen)})"
                changed.append({"var": name, "change": "defaulted"})
            else:
                text = _jinja_literal(seen)
                changed.append({"var": name, "change": "literal"})
            edits.append((start, close + 1, text))
    # A var a package macro reads by a name the SQL passes as a string
    # (`fill_pass_through_columns('task_pass_through_columns')`) cannot be given
    # a default at the read, so it goes into the project, scoped to it.
    literals = {
        m.group(2)
        for region in regions
        for m in re.finditer(r"(['\"])(\w+)\1", region.body)
    }
    for name in sorted((literals & scope.package_names()) - read):
        seen, override = scope.package_sees(name), scope.override_sees(name)
        if seen is _MISSING or override is not _MISSING:
            continue
        if isinstance(seen, str) and "{{" in seen:
            warnings.append(
                f"the package sets var '{name}' to a relation, read by a macro; "
                "the draft does not set it for the project"
            )
            continue
        defaults[name] = seen
        changed.append({"var": name, "change": "project_default"})
    for start, end, text in sorted(edits, reverse=True):
        sql = sql[:start] + text + sql[end:]
    # One entry per var: a package reads its pass-through list in two places.
    unique = list({(c["var"], c["change"]): c for c in changed}.values())
    return sql, unique, defaults, list(dict.fromkeys(warnings))


_RESTATED_CONFIG = ("materialized", "schema", "alias")


def restate_config(sql: str, config: Mapping[str, Any]) -> tuple[str, list[str]]:
    """``sql`` led by a ``config()`` carrying the package node's resolved
    ``materialized``, ``schema`` and ``alias``, for each key the SQL's own
    ``config()`` does not set. The package's ``dbt_project.yml`` applied them to
    its node and would not apply them to a project model."""

    from ..dbt_project import jinja_calls

    written = {
        key
        for call in jinja_calls(sql)
        if call.callee == "config"
        for key in _RESTATED_CONFIG
        if re.search(rf"\b{key}\s*=", sql)
    }
    keys = [
        key
        for key in _RESTATED_CONFIG
        if key not in written and isinstance(config.get(key), str) and config.get(key)
    ]
    if not keys:
        return sql, []
    line = (
        "{{ config("
        + ", ".join(f"{key}={json.dumps(config[key])}" for key in keys)
        + ") }}\n"
    )
    return line + sql, keys


# --- dbt_project.yml in place ----------------------------------------------------


def _line_start(text: str, index: int) -> int:
    return text.rfind("\n", 0, index) + 1


def _block(parts: list[str], value: Any, column: int) -> str:
    lines = []
    for depth, part in enumerate(parts):
        pad = " " * (column + 2 * depth)
        if depth == len(parts) - 1:
            lines.append(f"{pad}{part}: {json.dumps(value)}")
        else:
            lines.append(f"{pad}{part}:")
    return "\n".join(lines) + "\n"


def set_nested(text: str, path: list[str], value: Any) -> str:
    """``text`` with ``path`` set to ``value``, written in place: every comment,
    key order and other line kept. A flow-style mapping on the path is refused,
    since rewriting it would reformat what a person wrote."""

    from ..yaml_spans import before_trailing_blanks, block_end

    try:
        root = yaml.compose(text) if text.strip() else None
    except yaml.YAMLError as exc:
        raise RepairRefusedError(f"dbt_project.yml does not parse: {exc}") from exc

    def insert(at: int, block: str) -> str:
        prefix = text[:at]
        if prefix and not prefix.endswith("\n"):
            prefix += "\n"
        return prefix + block + text[at:]

    if root is None:
        return insert(len(text), _block(path, value, 0))
    if not isinstance(root, yaml.MappingNode) or root.flow_style:
        raise RepairRefusedError("dbt_project.yml is not a block mapping")
    node: yaml.MappingNode = root
    for depth, part in enumerate(path):
        found = next(
            (
                (key, child)
                for key, child in node.value
                if isinstance(key, yaml.ScalarNode) and key.value == part
            ),
            None,
        )
        if found is None:
            if node.value:
                last_key = node.value[-1][0]
                column = node.value[0][0].start_mark.column
                start = _line_start(text, last_key.start_mark.index)
                end = before_trailing_blanks(text, block_end(text, start, column))
            else:
                column, end = 0, len(text)
            return insert(end, _block(path[depth:], value, column))
        key, child = found
        if depth == len(path) - 1:
            if isinstance(child, yaml.ScalarNode):
                if (
                    yaml.safe_load(
                        text[child.start_mark.index : child.end_mark.index] or "null"
                    )
                    == value
                ):
                    return text
                return (
                    text[: child.start_mark.index]
                    + json.dumps(value)
                    + text[child.end_mark.index :]
                )
            raise RepairRefusedError(
                f"dbt_project.yml holds a collection at {'.'.join(path)}"
            )
        if isinstance(child, yaml.ScalarNode) and child.tag.endswith(":null"):
            # `models:` with nothing under it: the rest goes on the next lines.
            eol = text.find("\n", key.end_mark.index)
            at = len(text) if eol < 0 else eol + 1
            return insert(
                at, _block(path[depth + 1 :], value, key.start_mark.column + 2)
            )
        if not isinstance(child, yaml.MappingNode) or child.flow_style:
            raise RepairRefusedError(
                f"dbt_project.yml writes {'.'.join(path[: depth + 1])} in a form "
                "Dex will not rewrite (a flow mapping or a scalar); add the "
                "setting by hand"
            )
        node = child
    return text


# --- the draft -------------------------------------------------------------------


@dataclass
class Draft:
    """The override being drafted: its SQL body, the project file, and what was
    read and converted on the way. :func:`finish` turns it into the record."""

    name: str
    package: str
    unique_id: str
    override_path: str
    body: str
    project_yml: str
    disabled_at: str
    restated: dict[str, Any]
    conversions: list[Conversion] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)
    bind_check: dict[str, Any] = field(default_factory=lambda: {"status": "not_run"})
    record: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    @property
    def override_sql(self) -> str:
        """The override file: a header naming what it replaces and each
        conversion with its evidence, then the restated SQL."""

        header = [
            f"-- Project override of {self.package}.{self.name}, drafted by "
            "`dex transform repair`.",
            f"-- {self.package}.{self.name} is disabled in dbt_project.yml; this "
            "model replaces it.",
        ]
        header.extend(
            f"-- {c.column}: {c.kind} to {c.to_type}"
            + (" (changes what the value means)" if c.meaning_changing else "")
            + f": {c.detail}."
            for c in self.conversions
            if c.status == "resolved"
        )
        text = "\n".join(header) + "\n" + self.body.lstrip("\n")
        return text if text.endswith("\n") else text + "\n"


def _relation(manifest: Mapping[str, Any], unique_id: str) -> str | None:
    from ..dbt_project import strip_relation_quoting

    for section in ("nodes", "sources", "seeds", "snapshots"):
        node = (manifest.get(section) or {}).get(unique_id)
        if isinstance(node, Mapping) and node.get("relation_name"):
            return strip_relation_quoting(str(node["relation_name"]))
    return None


def upstream_relations(
    manifest: Mapping[str, Any], unique_id: str
) -> list[tuple[str, str]]:
    """``(unique_id, relation)`` for every node above ``unique_id``, nearest
    first, sources and seeds included."""

    nodes = {**(manifest.get("nodes") or {}), **(manifest.get("sources") or {})}
    out: list[tuple[str, str]] = []
    seen = {unique_id}
    frontier = [unique_id]
    while frontier:
        following = []
        for current in frontier:
            node = nodes.get(current)
            parents = (
                ((node or {}).get("depends_on") or {}).get("nodes") or []
                if isinstance(node, Mapping)
                else []
            )
            for parent in parents:
                if parent in seen:
                    continue
                seen.add(parent)
                following.append(parent)
                relation = _relation(manifest, parent)
                if relation:
                    out.append((parent, relation))
        frontier = following
    return out


def _cached(cache: Any, relation: str) -> Any:
    from ..cache import match_identifier

    if cache is None:
        return None
    known = [d.identifier for d in cache.datasets]
    matches = match_identifier(relation, known)
    if len(matches) != 1:
        parts = relation.split(".")
        if len(parts) >= 2:
            matches = match_identifier(".".join(parts[-2:]), known)
    if len(matches) != 1:
        return None
    return next(d for d in cache.datasets if d.identifier == matches[0])


def _profile_command(manifest: Mapping[str, Any], unique_id: str, relation: str) -> str:
    dev = unique_id.split(".", 1)[0] in ("model", "seed", "snapshot")
    return f"dex explore profile {relation}" + (" --target dev" if dev else "")


def find_conversions(
    raw_sql: str,
    failure: TypeFailure,
    manifest: Mapping[str, Any],
    unique_id: str,
    cache: Any,
    connector: str,
) -> list[Conversion]:
    """The columns inside the failing calls whose type fits the error, each
    settled from the nearest upstream relation the cache profiles with it."""

    candidates = columns_in_sites(raw_sql, failure.function)
    upstream = upstream_relations(manifest, unique_id)
    profiled = [(uid, rel, _cached(cache, rel)) for uid, rel in upstream]
    conversions: list[Conversion] = []
    for column in candidates:
        holder = next(
            (
                (dataset, profile)
                for _uid, _rel, dataset in profiled
                if dataset is not None
                for profile in dataset.columns
                if profile.name.lower() == column
            ),
            None,
        )
        if holder is None:
            continue
        dataset, profile = holder
        # Only a column of the type the error names is converted: the other
        # side of a coalesce (`due_on`, a timestamp) is what it must match.
        if family(profile.data_type) != failure.offending:
            continue
        conversion = Conversion(column=profile.name, relation=dataset.identifier)
        settle(conversion, profile, dataset, failure, connector)
        conversions.append(conversion)
    if conversions:
        return conversions
    # Nothing the failing call reads has the offending type in a profile: the
    # input is unprofiled, or profiled before the loader changed its type.
    missing = [(uid, rel) for uid, rel, dataset in profiled if dataset is None]
    stale = [(uid, rel) for uid, rel, dataset in profiled if dataset is not None]
    pool = missing or stale
    if not pool:
        return []
    uid, relation = next(
        ((u, r) for u, r in pool if u.startswith(("source.", "seed."))), pool[0]
    )
    return [
        Conversion(
            column="*",
            reason_code="unprofiled_input" if missing else "stale_profile",
            relation=relation,
            detail=(
                f"no profiled input gives a column of the call the {failure.offending} "
                f"type the error names; {relation} is "
                + (
                    "not in the .dex/ cache"
                    if missing
                    else "profiled, perhaps before its type changed"
                )
            ),
            next=_profile_command(manifest, uid, relation),
        )
    ]


def override_path(model_paths: list[str], name: str) -> str:
    """Where the override of ``name`` is written."""

    base = (model_paths or ["models"])[0].strip("/") or "models"
    return f"{base}/{OVERRIDE_DIR}/{name}.sql"


def draft(
    *,
    node: Mapping[str, Any],
    unique_id: str,
    manifest: Mapping[str, Any],
    message: str | None,
    project_yml: str,
    root_project: str,
    root_vars: Mapping[str, Any],
    package_vars: Mapping[str, Any],
    installed: set[str],
    model_paths: list[str],
    cache: Any,
    connector: str,
) -> Draft:
    """The override of the failing package ``node``, restated, with the
    conversion the build's ``message`` names. Call :func:`add_failure` for each
    further failure a check reveals, then :func:`finish`."""

    package = str(node.get("package_name") or unique_id.split(".")[1])
    name = str(node.get("name") or unique_id.split(".")[-1])
    raw = str(node.get("raw_code") or "")
    if not raw.strip():
        raise RepairRefusedError(
            f"the manifest holds no SQL for {package}.{name}; build the project "
            "once so dbt writes it"
        )
    macros = {
        str(macro.get("name"))
        for macro in (manifest.get("macros") or {}).values()
        if isinstance(macro, Mapping) and macro.get("package_name") == package
    }
    sql, qualified = qualify_macros(raw, package, macros)
    scope = VarScope(
        root_project=root_project,
        package=package,
        root=root_vars,
        package_vars=package_vars,
        scopes={root_project, package, *installed},
    )
    sql, var_changes, defaults, var_warnings = resolve_vars(sql, scope)
    sql, restated = restate_config(sql, node.get("config") or {})

    fqn = [str(part) for part in (node.get("fqn") or [package, name])]
    if fqn and fqn[0] != package:
        fqn = [package, *fqn]
    text = set_nested(project_yml, ["models", *fqn, "+enabled"], False)
    for var_name, value in sorted(defaults.items()):
        text = set_nested(text, ["vars", root_project, var_name], value)

    drafted = Draft(
        name=name,
        package=package,
        unique_id=unique_id,
        override_path=override_path(model_paths, name),
        body=sql,
        project_yml=text,
        disabled_at="models." + ".".join(fqn) + ".+enabled",
        restated={
            "macros_qualified": qualified,
            "vars": var_changes,
            "config": restated,
        },
        warnings=list(var_warnings),
    )
    add_failure(
        drafted,
        message,
        source="build",
        manifest=manifest,
        cache=cache,
        connector=connector,
    )
    return drafted


def add_failure(
    drafted: Draft,
    message: str | None,
    *,
    source: str,
    manifest: Mapping[str, Any],
    cache: Any,
    connector: str,
) -> bool:
    """Read one more failure of the draft and convert what it names. Returns
    whether a new conversion was resolved, which is what makes another check
    worth running. A column already converted inside that call is not again."""

    failure = parse_type_failure(message)
    entry: dict[str, Any] = (
        failure.record() if failure is not None else {"recognized": False}
    )
    entry["source"] = source
    drafted.errors.append(entry)
    if failure is None or failure.offending is None or failure.target is None:
        entry["recognized"] = False
        return False
    done = {(c.call, c.column.lower()) for c in drafted.conversions}
    visible = _without(
        drafted.body, [text for c in drafted.conversions for text in c.written]
    )
    found = [
        conversion
        for conversion in find_conversions(
            visible, failure, manifest, drafted.unique_id, cache, connector
        )
        if (failure.function, conversion.column.lower()) not in done
    ]
    for conversion in found:
        conversion.call = failure.function
    drafted.body = apply_conversions(
        drafted.body,
        failure.function,
        [c for c in found if c.column != "*"],
        visible=visible,
    )
    drafted.conversions.extend(found)
    return any(c.status == "resolved" for c in found)


def _without(sql: str, expressions: list[str]) -> str:
    """``sql`` with every occurrence of ``expressions`` blanked, offsets kept."""

    for expression in sorted(set(expressions), key=len, reverse=True):
        sql = sql.replace(expression, " " * len(expression))
    return sql


def finish(drafted: Draft) -> dict[str, Any]:
    """The record ``data.repair`` reports, and the warnings that go with it."""

    unresolved = [c for c in drafted.conversions if c.status != "resolved"]
    resolved = [c for c in drafted.conversions if c.status == "resolved"]
    unrecognized = [e for e in drafted.errors if not e.get("recognized")]
    warnings = drafted.warnings
    if unrecognized:
        warnings.append(
            f"Dex does not recognise a failure of {drafted.name} as a type error it "
            "can convert; the draft leaves that part as the package wrote it, so "
            "make the fix in the draft before applying it"
        )
    if drafted.errors and not drafted.conversions and not unrecognized:
        warnings.append(
            f"no column inside the failing call of {drafted.name} has the type the "
            "error names in a profiled input; the draft restates the package's SQL "
            "without a conversion"
        )
    warnings.extend(
        f"{drafted.name}.{c.column}: the conversion is unresolved "
        f"({c.reason_code}): {c.detail}"
        + (f"; run `{c.next}` and repair again" if c.next else "")
        for c in unresolved
    )
    warnings.extend(
        f"{drafted.name}.{c.column}: {c.kind} changes what the value means "
        f"({c.detail}); its evidence tier is `data`, so relay it as a decision"
        for c in resolved
        if c.meaning_changing
    )
    if drafted.bind_check.get("status") == "failed":
        warnings.append(
            f"the drafted {drafted.name} still does not bind on the dev target: "
            + str(drafted.bind_check.get("detail") or "")
        )
    converted = bool(resolved) and not unresolved and not unrecognized
    bound = drafted.bind_check.get("status")
    # `resolved` is converted *and* bound on dev. Converted with no bind (dbt
    # absent, no connection, or a connector with no free, row-free check) is
    # `unbound`: nothing has checked the draft's types.
    status = (
        "resolved"
        if converted and bound == "bound"
        else "unbound"
        if converted and bound != "failed"
        else "unresolved"
    )
    drafted.record = {
        "node": drafted.name,
        "package": drafted.package,
        "unique_id": drafted.unique_id,
        "status": status,
        "override": {"path": drafted.override_path, "disables": drafted.disabled_at},
        "errors": drafted.errors,
        "conversions": [c.record() for c in drafted.conversions],
        "bind_check": drafted.bind_check,
        "restated": drafted.restated,
        # The draft writes expressions inside the named calls, macro names, var
        # reads and config, and never a filter, join or grouping: no row moves.
        "population": "kept",
    }
    return drafted.record


#: How many failures one repair reads: the build's, then each the bind check
#: of the draft reveals. A warehouse reports one typing error at a time.
MAX_PASSES = 4


def bind_check(
    adapter: Any, connector: str, sql: str
) -> tuple[str, str | None, str | None]:
    """Whether the compiled draft binds on the dev target, reading no row.

    ``("bound", None, None)``, ``("failed", detail, text)`` or
    ``("not_checked", reason, None)``. DuckDB binds
    ``select * from (<draft>) limit 0``, which plans the query and returns
    nothing; BigQuery dry-runs it, which is free. Elsewhere there is no check
    that is both free and row-free, so none runs.

    ``detail`` is the adapter's own one-line, masked refusal, which is what may
    be kept. ``text`` is the whole exception chain, masked too, read only to
    recognise the failure: an adapter trims a refusal to its first line, and
    BigQuery puts the argument types on the next one.
    """

    from ..value_mask import mask_values

    body = sql.strip().rstrip(";")
    try:
        if connector == "duckdb":
            # The draft dbt compiled, wrapped to return no row; run_query
            # re-asserts it is a SELECT.
            adapter.run_query(
                f"select * from (\n{body}\n) as dex_bind_check limit 0",  # noqa: S608
                max_rows=1,
                timeout_seconds=60,
            )
        elif connector == "bigquery":
            adapter.query_estimate(body)
        else:
            return (
                "not_checked",
                f"{connector} has no free, row-free check of a query's types",
                None,
            )
    except Exception as exc:
        chain, seen = [], exc
        while seen is not None and len(chain) < 4:
            chain.append(str(seen))
            seen = seen.__cause__ or seen.__context__
        return "failed", mask_values(str(exc))[:600], mask_values("\n".join(chain))
    return "bound", None, None


def package_vars(project_dir: Path, package: str) -> dict[str, Any]:
    """The ``vars:`` an installed package's own ``dbt_project.yml`` declares."""

    from .package_overrides import _installed

    for name, directory, _roots in _installed(Path(project_dir)):
        if name != package:
            continue
        try:
            loaded = yaml.safe_load(
                (directory / "dbt_project.yml").read_text(encoding="utf-8")
            )
        except (OSError, UnicodeError, yaml.YAMLError):
            return {}
        declared = loaded.get("vars") if isinstance(loaded, dict) else None
        return dict(declared) if isinstance(declared, dict) else {}
    return {}


def installed_names(project_dir: Path) -> set[str]:
    """Every installed package's name."""

    from .package_overrides import _installed

    return {name for name, _directory, _roots in _installed(Path(project_dir))}
