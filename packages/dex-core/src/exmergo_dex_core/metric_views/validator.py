"""Dex's port of the Databricks Metric View YAML 1.1 rules, for one view.

Databricks publishes the specification as prose
(https://docs.databricks.com/aws/en/uc-semantics/metric-views/yaml-reference)
and enforces it when ``CREATE VIEW ... WITH METRICS`` runs, in a workspace. dex
reads the committed YAML without one, so the rules it relies on are restated
here, each as one row of :data:`RULES`. A later authoring release reuses the
same rows, escalating the ones that only warn while reading.

**What this module judges.** One view's structure: the version, the source, the
field and measure lists and every key on them, the join tree, the agent
metadata limits, a measure's ``window`` and ``partition``, ``parameters`` and
``materialization``. What needs the expressions parsed (which join a field reads,
whether ``MEASURE()`` resolves, whether a join's ``on`` names a known join) is
:mod:`.loader`'s, through :mod:`.lineage`.

**Element versus view.** A problem in one field, measure or join refuses that
element and keeps the rest of the view. A problem in what the whole view rests
on (its version, its source, its parameters, its materialization) refuses the
view, because Databricks would refuse to create it.

Pure Python: no sqlglot, no I/O.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from ..errors import DexError
from .dependency import METRIC_VIEW_SPEC_VERSION
from .model import FIELD, MEASURE, Join, Member, MetricView, Wildcard

__all__ = [
    "AGGREGATE_FUNCTIONS",
    "CARDINALITIES",
    "ERROR",
    "RULES",
    "WARNING",
    "Finding",
    "check_view",
    "derived_name",
]

ERROR = "error"
WARNING = "warning"

#: Every rule this port applies, with the severity it carries while reading.
#: A warning is a rule Databricks enforces that dex reports rather than refuses
#: on read, because the definition is still readable; authoring refuses them.
RULES: dict[str, str] = {
    "root": ERROR,
    "version": ERROR,
    "source": ERROR,
    "empty_view": ERROR,
    "fields_and_dimensions": WARNING,
    "unknown_key": WARNING,
    "type": ERROR,
    "member": ERROR,
    "empty_name": ERROR,
    "duplicate_name": ERROR,
    "derived_name": WARNING,
    "wildcard": WARNING,
    "wildcard_metadata": ERROR,
    "display_name": ERROR,
    "synonyms": ERROR,
    "format": ERROR,
    "window": ERROR,
    "partition": ERROR,
    "window_and_partition": ERROR,
    "join": ERROR,
    "join_condition": ERROR,
    "on_and_using": WARNING,
    "duplicate_join": ERROR,
    "cardinality": ERROR,
    "cardinality_mix": ERROR,
    "rely": ERROR,
    "parameters": ERROR,
    "parameter_default_order": ERROR,
    "materialization": ERROR,
    # Applied by the loader, over the parsed expressions.
    "unknown_measure": ERROR,
    "forward_measure": ERROR,
    "measure_cycle": ERROR,
    "nested_join_path": ERROR,
    "forward_field": WARNING,
    "filter_reads_field": WARNING,
    "one_to_many_field": ERROR,
    "unparsed_expression": WARNING,
}

_TOP_LEVEL = (
    "version",
    "comment",
    "source",
    "filter",
    "joins",
    "fields",
    "dimensions",
    "measures",
    "materialization",
    "parameters",
)
_MEMBER_KEYS = ("name", "expr", "comment", "display_name", "format", "synonyms")
_MEASURE_KEYS = (*_MEMBER_KEYS, "window", "partition")
_JOIN_KEYS = ("name", "source", "on", "using", "cardinality", "rely", "joins")

CARDINALITIES = ("many_to_one", "one_to_many")

#: The `outer_aggregate` of a level-of-detail (`partition`) measure.
_OUTER_AGGREGATES = ("sum", "avg", "min", "max", "count", "median")

#: Aggregate functions a measure's expression may be made of, as the catalog's
#: aggregation vocabulary spells each. `approx_count_distinct` keeps its own name:
#: it is an estimate, and calling it `count_distinct` would claim an exact count.
AGGREGATE_FUNCTIONS: dict[str, str] = {
    "sum": "sum",
    "count": "count",
    "avg": "average",
    "mean": "average",
    "min": "min",
    "max": "max",
    "median": "median",
    "approx_count_distinct": "count_distinct_approx",
}

_NAME_LIMIT = 255
_SYNONYM_LIMIT = 10
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_QUALIFIED_COLUMN = re.compile(
    r"^\s*(?:(?:`[^`]+`|[A-Za-z_]\w*)\s*\.\s*)*(`[^`]+`|[A-Za-z_]\w*)\s*$"
)
_CAST_COLUMN = re.compile(
    r"^\s*(?:try_)?cast\s*\(\s*((?:(?:`[^`]+`|[A-Za-z_]\w*)\s*\.\s*)*"
    r"(?:`[^`]+`|[A-Za-z_]\w*))\s+as\s+[^()]+(?:\([^()]*\))?\s*\)\s*$",
    re.IGNORECASE,
)
_WILDCARD = re.compile(r"\.\s*\*(\s+except\s*\(.*\))?\s*$", re.IGNORECASE | re.DOTALL)
_OFFSET = re.compile(r"^-?\d+\s+(day|days|month|months|year|years)$", re.IGNORECASE)
_UNIT = re.compile(r"^(day|days|month|months|year|years)$", re.IGNORECASE)

#: The SQL types a parameter standing for a window's number may have.
_INTEGER_TYPES = frozenset(
    {"tinyint", "byte", "smallint", "short", "int", "integer", "bigint", "long"}
)

_FORMAT_KEYS: dict[str, tuple[str, ...]] = {
    "number": ("decimal_places", "hide_group_separator", "abbreviation"),
    "currency": (
        "currency_code",
        "decimal_places",
        "hide_group_separator",
        "abbreviation",
    ),
    "percentage": ("decimal_places", "hide_group_separator"),
    "byte": ("decimal_places", "hide_group_separator"),
    "date": ("date_format", "leading_zeros"),
    "date_time": ("date_format", "time_format", "leading_zeros"),
}
_DATE_FORMATS = (
    "locale_short_month",
    "locale_long_month",
    "year_month_day",
    "locale_number_month",
    "year_week",
)
_TIME_FORMATS = ("no_time", "locale_hour_minute", "locale_hour_minute_second")
_ABBREVIATIONS = ("none", "compact", "scientific")
_DECIMAL_TYPES = ("max", "exact", "all")


@dataclass(frozen=True)
class Finding:
    """One rule a view broke, at the element it broke it."""

    element: str
    rule: str
    message: str

    @property
    def severity(self) -> str:
        """The severity :data:`RULES` gives this finding's rule."""
        return RULES[self.rule]


class _RefusedError(DexError):
    """The element being read is refused; the finding already says why.

    A control-flow signal inside this module, caught before anything returns:
    a caller sees the finding, never this.
    """


def derived_name(expr: str) -> str:
    """The name Databricks gives a field or measure written without one.

    A plain column keeps its final identifier, including inside a ``CAST``; any
    other expression keeps its text with the whitespace removed (``SUM(a+b)``),
    and has to be referenced in backticks.
    """

    cast = _CAST_COLUMN.match(expr)
    column = _QUALIFIED_COLUMN.match(cast.group(1) if cast else expr)
    if column:
        return column.group(1).strip("`")
    return re.sub(r"\s+", "", expr)


def check_view(
    raw: Any, file: str, name: str
) -> tuple[MetricView | None, list[Finding]]:
    """One view as dex reads it, or ``None`` with the findings that refused it."""

    findings: list[Finding] = []

    def find(element: str, rule: str, message: str) -> None:
        """Record one finding against this view."""
        findings.append(Finding(element, rule, message))

    if not isinstance(raw, Mapping):
        find(
            "(root)",
            "root",
            "a Metric View file is one YAML mapping (version, source, fields, "
            f"measures, ...), and this parsed as {type(raw).__name__}",
        )
        return None, findings
    for key in raw:
        if key not in _TOP_LEVEL:
            find(
                "(root)",
                "unknown_key",
                f"`{key}` is not a Metric View key; Databricks refuses it when the "
                "view is created",
            )

    try:
        version = _version(raw.get("version"), find)
        source = _required_text(raw, "source", "(root)", "source", find)
        comment = _optional_text(raw, "comment", "(root)", find)
        filter_ = _optional_text(raw, "filter", "(root)", find)
        parameters = _parameters(raw.get("parameters"), find)
        materialization = _materialization(raw.get("materialization"), find)
    except _RefusedError:
        return None, findings
    if parameters and materialization:
        find(
            "materialization",
            "materialization",
            "a view that declares `parameters` cannot be materialized: Databricks "
            "binds a parameter per query, so there is no one result to maintain",
        )
        return None, findings
    integers = frozenset(
        str(p["name"]).lower()
        for p in parameters
        if str(p.get("data_type", "")).strip().lower() in _INTEGER_TYPES
    )

    has_fields = bool(raw.get("fields"))
    has_dimensions = bool(raw.get("dimensions"))
    if has_fields and has_dimensions:
        # Databricks creates the view from `dimensions` and drops `fields`
        # (checked against `CREATE VIEW ... WITH METRICS`), and so does the Ossie
        # converter, so dex reads what a deployment reads and says what it drops.
        find(
            "(root)",
            "fields_and_dimensions",
            "both `fields` and `dimensions` are written. `dimensions` is the "
            "older spelling of `fields`, and Databricks reads `dimensions` and "
            "ignores `fields`, so dex does too: keep one",
        )
    fields_key = "dimensions" if has_dimensions else "fields"
    if not has_fields and not has_dimensions and not raw.get("measures"):
        find(
            "(root)",
            "empty_view",
            "a Metric View needs `fields` or `measures`, and this declares neither",
        )
        return None, findings

    wildcards: list[Wildcard] = []
    fields = _members(raw.get(fields_key), fields_key, FIELD, wildcards, find, integers)
    measures = _members(
        raw.get("measures"), "measures", MEASURE, wildcards, find, integers
    )
    fields, measures = _unique(fields, measures, find)
    joins = _joins(raw.get("joins"), (), None, find)

    return (
        MetricView(
            name=name,
            file=file,
            version=version,
            source=source,
            comment=comment,
            filter=filter_,
            fields=tuple(fields),
            measures=tuple(measures),
            joins=tuple(joins),
            parameters=tuple(parameters),
            materialization=materialization,
            wildcards=tuple(wildcards),
            fields_key=fields_key,
            raw=dict(raw),
        ),
        findings,
    )


# --- the view ------------------------------------------------------------------


def _version(value: Any, find: Any) -> str:
    """The view's `version`, as text, or a refusal unless it is the pinned 1.1."""
    # `version: 1.1` unquoted parses as a float, which is how Databricks' own
    # examples write it; `1.10` would too, so the text is compared, not a number.
    if value is None:
        find(
            "(root)",
            "version",
            f"`version` is required; dex reads Metric View YAML "
            f"{METRIC_VIEW_SPEC_VERSION}",
        )
        raise _RefusedError
    text = str(value).strip()
    if text != METRIC_VIEW_SPEC_VERSION:
        find(
            "(root)",
            "version",
            f"version {text} is not one dex reads: it reads Metric View YAML "
            f"{METRIC_VIEW_SPEC_VERSION} only. Re-save the definition as "
            f"`version: {METRIC_VIEW_SPEC_VERSION}` (Databricks' `ALTER VIEW ... AS "
            "$$...$$`), copying any Unity Catalog comments into `comment` first",
        )
        raise _RefusedError
    return text


def _required_text(
    raw: Mapping[str, Any], key: str, element: str, rule: str, find: Any
) -> str:
    """``raw[key]`` as non-empty text, or a refusal under ``rule``."""
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        find(element, rule, f"`{key}` is required and has to be non-empty text")
        raise _RefusedError
    return value.strip()


def _optional_text(
    raw: Mapping[str, Any], key: str, element: str, find: Any
) -> str | None:
    """``raw[key]`` as text, ``None`` where absent, or a refusal for another type."""
    value = raw.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        find(element, "type", f"`{key}` has to be text, got {type(value).__name__}")
        raise _RefusedError
    return value


def _parameters(value: Any, find: Any) -> list[dict[str, Any]]:
    """The view's `parameters`, each with a name and a type, defaults in order."""
    if value is None:
        return []
    if not isinstance(value, list):
        find("parameters", "parameters", "`parameters` has to be a list")
        raise _RefusedError
    found: list[dict[str, Any]] = []
    defaulted: str | None = None
    for index, entry in enumerate(value):
        where = f"parameters[{index}]"
        if not isinstance(entry, Mapping):
            find(where, "parameters", "a parameter is a mapping")
            raise _RefusedError
        for key in entry:
            if key not in ("name", "data_type", "default"):
                find(where, "unknown_key", f"`{key}` is not a parameter key")
        name = entry.get("name")
        if not isinstance(name, str) or not _IDENTIFIER.match(name):
            find(
                where, "parameters", "a parameter needs a `name` that is an identifier"
            )
            raise _RefusedError
        if (
            not isinstance(entry.get("data_type"), str)
            or not entry["data_type"].strip()
        ):
            find(
                f"parameters['{name}']",
                "parameters",
                "a parameter needs a `data_type` (a SQL type such as `double`)",
            )
            raise _RefusedError
        if "default" in entry:
            defaulted = defaulted or name
        elif defaulted:
            find(
                f"parameters['{name}']",
                "parameter_default_order",
                f"follows `{defaulted}`, which has a default, and has none: once "
                "one parameter has a default, every later one needs one",
            )
            raise _RefusedError
        found.append(dict(entry))
    return found


def _materialization(value: Any, find: Any) -> dict[str, Any] | None:
    """The view's `materialization`, checked structurally and carried as written."""
    if value is None:
        return None
    where = "materialization"
    if not isinstance(value, Mapping):
        find(where, "materialization", "`materialization` has to be a mapping")
        raise _RefusedError
    for key in value:
        if key not in ("schedule", "mode", "materialized_views"):
            find(where, "unknown_key", f"`{key}` is not a materialization key")
    if value.get("mode") != "relaxed":
        find(where, "materialization", "`mode` is required and has to be `relaxed`")
        raise _RefusedError
    views = value.get("materialized_views")
    if not isinstance(views, list) or not views:
        find(where, "materialization", "`materialized_views` is a non-empty list")
        raise _RefusedError
    unaggregated = 0
    for index, entry in enumerate(views):
        here = f"{where}.materialized_views[{index}]"
        if not isinstance(entry, Mapping) or not isinstance(entry.get("name"), str):
            find(here, "materialization", "each materialized view needs a `name`")
            raise _RefusedError
        kind = entry.get("type")
        if kind not in ("aggregated", "unaggregated"):
            find(
                here,
                "materialization",
                "`type` is `aggregated` or `unaggregated`",
            )
            raise _RefusedError
        if kind == "unaggregated":
            unaggregated += 1
            if entry.get("dimensions") or entry.get("measures"):
                find(
                    here,
                    "materialization",
                    "an unaggregated materialized view takes no dimensions or measures",
                )
                raise _RefusedError
        elif not entry.get("dimensions") and not entry.get("measures"):
            find(
                here,
                "materialization",
                "an aggregated materialized view needs `dimensions` or `measures`",
            )
            raise _RefusedError
    if unaggregated > 1:
        find(
            where,
            "materialization",
            "at most one materialized view may be `unaggregated`",
        )
        raise _RefusedError
    return dict(value)


# --- fields and measures ---------------------------------------------------------


def _members(
    value: Any,
    key: str,
    kind: str,
    wildcards: list[Wildcard],
    find: Any,
    integers: frozenset[str] = frozenset(),
) -> list[Member]:
    """Every readable field or measure of one list; a refused entry is left out."""
    if value is None:
        return []
    if not isinstance(value, list):
        find(key, "type", f"`{key}` has to be a list")
        return []
    found: list[Member] = []
    for index, entry in enumerate(value):
        try:
            member = _member(entry, key, index, kind, wildcards, find, integers)
        except _RefusedError:
            continue
        if member is not None:
            found.append(member)
    return found


def _member(
    entry: Any,
    key: str,
    index: int,
    kind: str,
    wildcards: list[Wildcard],
    find: Any,
    integers: frozenset[str] = frozenset(),
) -> Member | None:
    """One field or measure, ``None`` for a wildcard, or a refusal."""
    where = f"{key}[{index}]"
    if not isinstance(entry, Mapping):
        find(where, "member", "each entry is a mapping with at least an `expr`")
        raise _RefusedError
    expr = entry.get("expr")
    if not isinstance(expr, str) or not expr.strip():
        find(where, "member", "`expr` is required and has to be non-empty text")
        raise _RefusedError
    if _WILDCARD.search(expr):
        if any(k != "expr" for k in entry):
            find(
                where,
                "wildcard_metadata",
                f"`{expr.strip()}` is a wildcard, which takes no `name` or other "
                "metadata",
            )
            raise _RefusedError
        find(
            where,
            "wildcard",
            f"`{expr.strip()}` is expanded by Databricks from the warehouse schema "
            "when the view is created; dex does not expand it, so the members it "
            "adds are not listed",
        )
        wildcards.append(Wildcard(kind=kind, expr=expr.strip()))
        return None

    allowed = _MEASURE_KEYS if kind == MEASURE else _MEMBER_KEYS
    for name in entry:
        if name not in allowed:
            find(
                where,
                "unknown_key",
                f"`{name}` is not a {'measure' if kind == MEASURE else 'field'} key",
            )

    derived = "name" not in entry
    if derived:
        name = derived_name(expr)
        find(
            where,
            "derived_name",
            f"has no `name`, so it is named `{name}` by the rule Databricks "
            "applies (DBR 19 and later; earlier runtimes refuse a field or "
            "measure without one)",
        )
    else:
        raw_name = entry.get("name")
        if not isinstance(raw_name, str) or not raw_name.strip():
            find(where, "empty_name", "an explicit `name` cannot be empty")
            raise _RefusedError
        name = raw_name.strip()
    where = f"{key}['{name}']"

    comment = entry.get("comment")
    if comment is not None and not isinstance(comment, str):
        find(where, "type", "`comment` has to be text")
        raise _RefusedError
    display_name = entry.get("display_name")
    if display_name is not None and (
        not isinstance(display_name, str) or len(display_name) > _NAME_LIMIT
    ):
        find(
            where,
            "display_name",
            f"`display_name` is text of at most {_NAME_LIMIT} characters",
        )
        raise _RefusedError
    synonyms = _synonyms(entry.get("synonyms"), where, find)
    format_ = _format(entry.get("format"), where, find)

    window: tuple[dict[str, Any], ...] = ()
    partition: dict[str, Any] | None = None
    if kind == MEASURE:
        if entry.get("window") is not None and entry.get("partition") is not None:
            find(
                where,
                "window_and_partition",
                "a measure has a `window` or a `partition`, never both",
            )
            raise _RefusedError
        window = _window(entry.get("window"), where, find, integers)
        partition = _partition(entry.get("partition"), where, find)

    return Member(
        kind=kind,
        name=name,
        expr=expr.strip(),
        comment=comment,
        display_name=display_name,
        synonyms=synonyms,
        format=format_,
        window=window,
        partition=partition,
        derived_name=derived,
        raw=dict(entry),
    )


def _synonyms(value: Any, where: str, find: Any) -> tuple[str, ...]:
    """A member's `synonyms`, within Unity Catalog's count and length limits."""
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        find(where, "synonyms", "`synonyms` is a list of text")
        raise _RefusedError
    if len(value) > _SYNONYM_LIMIT or any(len(v) > _NAME_LIMIT for v in value):
        find(
            where,
            "synonyms",
            f"`synonyms` holds at most {_SYNONYM_LIMIT} entries of at most "
            f"{_NAME_LIMIT} characters each, and this holds {len(value)}",
        )
        raise _RefusedError
    return tuple(value)


def _format(value: Any, where: str, find: Any) -> dict[str, Any] | None:
    """A member's `format`, checked against the keys its `type` allows."""
    if value is None:
        return None
    here = f"{where}.format"
    if not isinstance(value, Mapping):
        find(here, "format", "`format` is a mapping with a `type`")
        raise _RefusedError
    kind = value.get("type")
    if not isinstance(kind, str) or kind not in _FORMAT_KEYS:
        find(
            here,
            "format",
            f"`type` is one of {', '.join(_FORMAT_KEYS)}, got {kind!r}",
        )
        raise _RefusedError
    for key in value:
        if key != "type" and key not in _FORMAT_KEYS[kind]:
            find(here, "unknown_key", f"`{key}` is not a key of a `{kind}` format")
    if kind == "currency":
        code = value.get("currency_code")
        if not isinstance(code, str) or not re.fullmatch(r"[A-Za-z]{3}", code):
            find(
                here,
                "format",
                "a currency format needs `currency_code`, an ISO-4217 code (USD)",
            )
            raise _RefusedError
    places = value.get("decimal_places")
    if places is not None:
        if not isinstance(places, Mapping) or places.get("type") not in _DECIMAL_TYPES:
            find(
                here,
                "format",
                "`decimal_places` is a mapping whose `type` is max, exact or all",
            )
            raise _RefusedError
        count = places.get("places")
        if places["type"] != "all" and (
            not isinstance(count, int)
            or isinstance(count, bool)
            or not 0 <= count <= 10
        ):
            find(
                here,
                "format",
                f"`decimal_places` of type {places['type']} needs `places`, 0 to 10",
            )
            raise _RefusedError
    for key in ("hide_group_separator", "leading_zeros"):
        if key in value and not isinstance(value[key], bool):
            find(here, "format", f"`{key}` is true or false")
            raise _RefusedError
    if "abbreviation" in value and value["abbreviation"] not in _ABBREVIATIONS:
        find(
            here,
            "format",
            f"`abbreviation` is one of {', '.join(_ABBREVIATIONS)}",
        )
        raise _RefusedError
    date_format = value.get("date_format")
    time_format = value.get("time_format")
    if date_format is not None:
        allowed = (*_DATE_FORMATS, "no_date") if kind == "date_time" else _DATE_FORMATS
        if date_format not in allowed:
            find(here, "format", f"`date_format` is one of {', '.join(allowed)}")
            raise _RefusedError
    if time_format is not None and time_format not in _TIME_FORMATS:
        find(here, "format", f"`time_format` is one of {', '.join(_TIME_FORMATS)}")
        raise _RefusedError
    if date_format == "no_date" and time_format == "no_time":
        find(here, "format", "`date_format` and `time_format` cannot both be none")
        raise _RefusedError
    return dict(value)


def _window(
    value: Any, where: str, find: Any, integers: frozenset[str] = frozenset()
) -> tuple[dict[str, Any], ...]:
    """A measure's `window` specs, each with an `order` and a valid `range`.

    The number in a `range` or an `offset` may be an integer parameter the view
    declares, by its bare name; one naming no such parameter is refused.
    """
    if value is None:
        return ()
    if not isinstance(value, list) or not value:
        find(where, "window", "`window` is a non-empty list of window specs")
        raise _RefusedError
    for index, spec in enumerate(value):
        here = f"{where}.window[{index}]"
        if not isinstance(spec, Mapping):
            find(here, "window", "a window spec is a mapping")
            raise _RefusedError
        for key in spec:
            if key not in ("order", "range", "semiadditive", "offset"):
                find(here, "unknown_key", f"`{key}` is not a window key")
        if not isinstance(spec.get("order"), str) or not spec["order"].strip():
            find(here, "window", "`order` is required: the field the window orders by")
            raise _RefusedError
        if not _range_ok(spec.get("range")):
            find(
                here,
                "window",
                "`range` is current, cumulative, all, or trailing|leading <n> "
                f"[<unit>] [inclusive|exclusive]; got {spec.get('range')!r}",
            )
            raise _RefusedError
        magnitude = spec["range"].split()[1:2]
        if (
            magnitude
            and not magnitude[0].isdigit()
            and (magnitude[0].lower() not in integers)
        ):
            find(
                here,
                "window",
                f"`range` reads `{magnitude[0]}`, which names no integer parameter "
                "of this view",
            )
            raise _RefusedError
        if spec.get("semiadditive") not in (None, "first", "last"):
            find(here, "window", "`semiadditive` is first or last")
            raise _RefusedError
        offset = spec.get("offset")
        if offset is not None and not _offset_ok(offset, integers):
            find(
                here,
                "window",
                "`offset` is a signed integer, `<n> day|month|year[s]`, or an "
                "integer parameter of this view in place of `<n>`",
            )
            raise _RefusedError
    return tuple(dict(spec) for spec in value)


def _offset_ok(value: Any, integers: frozenset[str]) -> bool:
    """Whether ``value`` is a window `offset`: a signed integer, ``<n> <unit>``,
    or a declared integer parameter standing for ``<n>``."""

    if isinstance(value, int) and not isinstance(value, bool):
        return True
    if not isinstance(value, str):
        return False
    text = value.strip()
    if _OFFSET.match(text):
        return True
    tokens = text.split()
    return (
        1 <= len(tokens) <= 2
        and tokens[0].lower() in integers
        and (len(tokens) == 1 or bool(_UNIT.match(tokens[1])))
    )


def _range_ok(value: Any) -> bool:
    """Whether ``value`` is a window `range` the specification documents."""
    if not isinstance(value, str):
        return False
    tokens = value.lower().split()
    if tokens in (["current"], ["cumulative"], ["all"]):
        return True
    if len(tokens) < 2 or tokens[0] not in ("trailing", "leading"):
        return False
    if not (tokens[1].isdigit() or _IDENTIFIER.match(tokens[1])):
        return False
    rest = tokens[2:]
    if rest and rest[-1] in ("inclusive", "exclusive"):
        rest = rest[:-1]
    return len(rest) <= 1 and all(t.isalpha() for t in rest)


def _partition(value: Any, where: str, find: Any) -> dict[str, Any] | None:
    """A measure's `partition` (level of detail), checked and carried as written."""
    if value is None:
        return None
    here = f"{where}.partition"
    if not isinstance(value, Mapping):
        find(
            here, "partition", "`partition` is a mapping of include and outer_aggregate"
        )
        raise _RefusedError
    for key in value:
        if key not in ("include", "outer_aggregate"):
            find(here, "unknown_key", f"`{key}` is not a partition key")
    include = value.get("include")
    if not isinstance(include, list) or not all(isinstance(i, str) for i in include):
        find(here, "partition", "`include` is a list of field names")
        raise _RefusedError
    outer = value.get("outer_aggregate")
    if not isinstance(outer, str) or outer.lower() not in _OUTER_AGGREGATES:
        find(
            here,
            "partition",
            "`outer_aggregate` is one of "
            f"{', '.join(a.upper() for a in _OUTER_AGGREGATES)}",
        )
        raise _RefusedError
    return dict(value)


def _unique(
    fields: list[Member], measures: list[Member], find: Any
) -> tuple[list[Member], list[Member]]:
    """Names are unique across fields and measures, ignoring case; the later of
    two is refused, so the earlier keeps its references."""

    seen: dict[str, Member] = {}
    kept: dict[str, list[Member]] = {FIELD: [], MEASURE: []}
    for member in (*fields, *measures):
        earlier = seen.get(member.name.lower())
        if earlier is not None:
            listed = "measures" if member.kind == MEASURE else "fields"
            find(
                f"{listed}['{member.name}']",
                "duplicate_name",
                f"a {earlier.kind} named `{earlier.name}` already exists; names are "
                "unique across fields and measures, ignoring case",
            )
            continue
        seen[member.name.lower()] = member
        kept[member.kind].append(member)
    return kept[FIELD], kept[MEASURE]


# --- joins -----------------------------------------------------------------------


def _joins(
    value: Any, parent: tuple[str, ...], inherited: str | None, find: Any
) -> list[Join]:
    """One level of the join tree; a refused join takes its children with it."""
    if value is None:
        return []
    where = f"joins['{'.'.join(parent)}'].joins" if parent else "joins"
    if not isinstance(value, list):
        find(where, "join", "`joins` has to be a list")
        return []
    found: list[Join] = []
    names: set[str] = set()
    for index, entry in enumerate(value):
        try:
            join = _join(entry, parent, index, inherited, find)
        except _RefusedError:
            continue
        if join.name.lower() in names:
            find(
                f"joins['{join.dotted}']",
                "duplicate_join",
                f"a sibling join is already named `{join.name}`; a join's name is "
                "the prefix its columns are read through, so it has to be unique",
            )
            continue
        names.add(join.name.lower())
        found.append(join)
    return found


def _join(
    entry: Any,
    parent: tuple[str, ...],
    index: int,
    inherited: str | None,
    find: Any,
) -> Join:
    """One join and the joins nested under it, or a refusal."""
    where = (
        f"joins['{'.'.join(parent)}'].joins[{index}]" if parent else f"joins[{index}]"
    )
    if not isinstance(entry, Mapping):
        find(where, "join", "a join is a mapping with a `name` and a `source`")
        raise _RefusedError
    name = entry.get("name")
    if not isinstance(name, str) or not _IDENTIFIER.match(name):
        find(where, "join", "a join needs a `name` that is an identifier")
        raise _RefusedError
    if name.lower() == "source":
        find(
            where,
            "join",
            "`source` names the view's own source in every expression, so no join "
            "can be called that",
        )
        raise _RefusedError
    path = (*parent, name)
    where = f"joins['{'.'.join(path)}']"
    for key in entry:
        if key not in _JOIN_KEYS:
            find(where, "unknown_key", f"`{key}` is not a join key")
    source = entry.get("source")
    if not isinstance(source, str) or not source.strip():
        find(where, "join", "`source` is required: a table name or a query")
        raise _RefusedError

    on = entry.get("on")
    using = entry.get("using")
    if on is not None and not (isinstance(on, str) and on.strip()):
        find(where, "join_condition", "`on` has to be a boolean SQL expression")
        raise _RefusedError
    if using is not None and (
        not isinstance(using, list)
        or not using
        or not all(isinstance(u, str) and u.strip() for u in using)
    ):
        find(where, "join_condition", "`using` is a non-empty list of column names")
        raise _RefusedError
    if on is None and using is None:
        find(where, "join_condition", "a join needs `on` or `using`")
        raise _RefusedError
    if on is not None and using is not None:
        find(
            where,
            "on_and_using",
            "declares both `on` and `using`; Databricks reads `on`, and so does dex",
        )
        using = None

    cardinality = entry.get("cardinality", "many_to_one")
    if cardinality not in CARDINALITIES:
        find(
            where,
            "cardinality",
            f"`cardinality` is many_to_one (the default) or one_to_many, got "
            f"{cardinality!r}",
        )
        raise _RefusedError
    if inherited is not None and cardinality != inherited:
        find(
            where,
            "cardinality_mix",
            f"is {cardinality} under a {inherited} join; every join below a join "
            "keeps that join's cardinality",
        )
        raise _RefusedError

    rely = entry.get("rely")
    at_most_one: bool | None = None
    if rely is not None:
        if not isinstance(rely, Mapping):
            find(where, "rely", "`rely` is a mapping")
            raise _RefusedError
        for key in rely:
            if key != "at_most_one_match":
                find(where, "unknown_key", f"`rely.{key}` is not a rely key")
        if "at_most_one_match" in rely:
            if not isinstance(rely["at_most_one_match"], bool):
                find(where, "rely", "`rely.at_most_one_match` is true or false")
                raise _RefusedError
            at_most_one = rely["at_most_one_match"]

    children = _joins(entry.get("joins"), path, cardinality, find)
    return Join(
        name=name,
        path=path,
        source=source.strip(),
        on=on.strip() if isinstance(on, str) else None,
        using=tuple(u.strip() for u in using) if using else None,
        cardinality=cardinality,
        at_most_one_match=at_most_one,
        joins=tuple(children),
        raw=dict(entry),
    )
