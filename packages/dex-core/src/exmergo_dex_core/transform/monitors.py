"""Vendor monitors written from a model's declaration (#1444, #1482, #1484,
#1485, #1486, #1591).

A declaration already says which column is a model's time. An observability
vendor's monitors need the same fact, and a project that writes it twice, by
hand, in two places, lets the two drift. With ``monitors.vendor`` set in
``.dex/config.yml``, a plan that renders a declaration also writes the
vendor's own tests into the same YAML entry, in the same plan and diff, parsed
by dbt before the plan is stored.

One vendor today, the Elementary dbt package, and five mapping rows:

* a column declared with role ``time`` becomes the ``timestamp_column`` of
  ``elementary.volume_anomalies`` and ``elementary.freshness_anomalies``.
  ``freshness_anomalies`` cannot run without one (the package lists it under
  ``mandatory_params``), and ``volume_anomalies`` without one counts the whole
  table once per run, trained only on run history, which is a different
  monitor from the one a time column gives, so neither is written without it;
* the declaration's ``population.filters`` become the ``where_expression`` of
  every monitor the model gets (#1483), so the monitors measure the rows the
  model promises and a row outside them bends no training history
  (:func:`where_expression`);
* a column declared with role ``dimension`` whose cached domain is small (at
  most :data:`DIMENSION_MAX_DISTINCT` distinct values) becomes the one
  ``dimensions`` entry of an ``elementary.dimension_anomalies`` (#1484), on
  the model's time column. One test per column, never one listing several:
  the package concatenates a ``dimensions`` list into one combined value
  (``dimension_monitoring_query``), so a list of three 20-value columns is up
  to 8,000 series, which is the cost the bound exists to keep out. The domain
  is read from the exploration cache's ``distinct_count`` and never measured
  here: a column with no cached count is left out and named with the
  ``explore profile`` that would supply one (:class:`Domains`). A column Dex
  flags as personal data is never a dimension here, at any domain size: the
  test groups by its values;
* a column declared ``unit: fraction`` or ``unit: percent`` (#1485), and a
  column declared ``role: measure`` with an ``additivity`` (#1591), becomes
  the column of an ``elementary.column_anomalies`` with an explicit list of
  monitors, on the model's time column. The unit gives ``min`` and ``max``,
  which watch the column shift inside the hard bound ``dex_unit_range``
  already checks on every run; the additivity gives ``sum``, ``average`` and
  ``zero_count`` for an additive measure, ``min``, ``max`` and ``average``
  for a semi-additive one, and ``average`` alone for a non-additive one, so
  ``sum`` is never written where adding the column means nothing. A column
  both rows reach gets one test whose list is the union, in the package's
  own order (:data:`COLUMN_MONITOR_ORDER`), never two. With no list the
  package would run its defaults, some of which mean nothing for some
  columns, so the list is always written. A column Dex flags as personal
  data gets none: the monitors store its values.

* a time column declaring a daily deadline, ``freshness: {deadline,
  timezone, days}`` (#1486), becomes an ``elementary.data_freshness_sla`` on
  it, with that ``sla_time``, ``timezone`` and, where ``days`` limits it, the
  ``day_of_week`` (Elementary's full day names). A ``max_lag`` is never read
  as a deadline: "six hours behind" is not "updated by 07:00". The test
  passes once the column holds a value from today in the zone, so on a
  ``business`` basis column (yesterday's events, loaded today) the plan
  warns. Withheld below Elementary 0.23.1, which first ships it, and where a
  population value holds a quote on a connector that escapes it with a
  backslash (:func:`_sla_breaks_on_quote`).

What this module never does:

* write a monitor where it will not run. The package has to be installed (or
  added by the same plan), and the repository's ``dbt_project.yml`` has to show
  its models enabled on the production target, read with ``target.name``
  replaced by the committed ``dbt_prod_target`` (the #1481 decision). Disabled
  there, or not readable, and nothing is written; the report says which;
* choose a statistic. Sensitivity, ``days_back``, seasonality and every other
  training setting stay the package's defaults;
* touch a monitor the project wrote by hand. A listed test of the same name
  (and, for a freshness monitor or a column monitor, the same
  ``timestamp_column``) is reported ``hand_written`` and left exactly as
  written;
* remove a monitor. One it wrote that the declaration no longer gives is
  reported ``stale``, for a reviewer to remove.

Each monitor it writes carries ``config.meta.dex.monitor`` (the vendor and the
declaration field it came from), so ``data.decisions`` is read back from the
YAML (:func:`decisions`) and a plan, its apply and a later build report the
same list.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from ..errors import RequestError
from ..semantic_declarations import Additivity, Declaration, PopulationFilter, Role
from .plans import PlanEdit

#: The vendors this release writes monitors for (``monitors.vendor``).
ELEMENTARY = "elementary"

#: The dbt tag every monitor Dex writes carries, so an orchestrator can select
#: or exclude them (``dbt build --exclude tag:dex_monitor``). Not a generated
#: test's cadence: monitors are not Dex's generated tests and are never priced
#: or counted as one.
TAG = "dex_monitor"

#: The key under ``config.meta.dex`` that marks a monitor Dex wrote.
MARKER = "monitor"

VOLUME = "volume_anomalies"
FRESHNESS = "freshness_anomalies"
DIMENSION = "dimension_anomalies"
COLUMN = "column_anomalies"
SLA = "data_freshness_sla"

#: The first Elementary release that ships ``data_freshness_sla`` (#1486): it
#: is absent at 0.23.0 and unchanged from 0.23.1 through 0.26.1. Below it the
#: test does not exist, and dbt's parse would refuse the plan.
SLA_SINCE = "0.23.1"

#: The monitors a declared unit gives a column (#1485): its minimum and
#: maximum, watched for a shift inside the bound ``dex_unit_range`` checks.
UNIT_MONITORS: dict[str, tuple[str, ...]] = {
    "fraction": ("min", "max"),
    "percent": ("min", "max"),
}

#: The monitors a measure's declared additivity gives it (#1591). A
#: per-bucket ``sum`` means something only where the measure adds up, so a
#: semi-additive (a balance) or non-additive (a ratio) measure never gets one.
ADDITIVITY_MONITORS: dict[Additivity, tuple[str, ...]] = {
    Additivity.ADDITIVE: ("sum", "average", "zero_count"),
    Additivity.SEMI_ADDITIVE: ("min", "max", "average"),
    Additivity.NON_ADDITIVE: ("average",),
}

#: The package's own order of its numeric column monitors
#: (``get_available_monitors``, ``column_numeric``, at its 0.26.1 tag): a
#: column's list is written in it, so a union reads the same whichever row
#: contributed first.
COLUMN_MONITOR_ORDER = (
    "min",
    "max",
    "zero_count",
    "zero_percent",
    "not_zero_percent",
    "average",
    "standard_deviation",
    "variance",
    "sum",
)

#: The most distinct values a dimension column may hold, by the exploration
#: cache's count, to become a ``dimension_anomalies`` (#1484). The package
#: trains one series per value, so a larger domain is that many series, most
#: too thin to train, and a wider scan on every run. Inclusive: a column of
#: exactly this many values is watched. Written into each such monitor's marker
#: (``max_distinct``), so the decision it reads back names the bound it was
#: written under.
DIMENSION_MAX_DISTINCT = 20

# The statuses a ``data.monitors`` row carries.
ADDED = "added"
ALREADY_LISTED = "already_listed"
HAND_WRITTEN = "hand_written"
NOT_RENDERED = "not_rendered"
STALE = "stale"

# Why a monitor was not written.
NO_TIME_COLUMN = "no_time_column"
AMBIGUOUS_TIME_COLUMN = "ambiguous_time_column"
COLUMN_NOT_OUTPUT = "column_not_output"
PACKAGE_NOT_INSTALLED = "package_not_installed"
PRODUCTION_DISABLED = "production_disabled"
PRODUCTION_UNKNOWN = "production_unknown"
DECLARATION_CHANGED = "declaration_changed"
RELEASE_UNSUPPORTED = "release_unsupported"
#: Why a ``data_freshness_sla`` alone is withheld (#1486): its population
#: holds a quote the package cannot re-escape (:func:`_sla_breaks_on_quote`).
SLA_FILTER_QUOTE = "sla_filter_quote"
# Why a model's population filters could not be written as a where_expression
# (#1483), so no monitor was written for it: one written without the filter
# would measure rows the model does not promise.
FILTER_COLUMN_NOT_OUTPUT = "filter_column_not_output"
FILTER_UNWRITABLE = "filter_unwritable"
# Why a dimension column was not written as a dimension_anomalies (#1484).
DOMAIN_TOO_LARGE = "domain_too_large"
NOT_PROFILED = "not_profiled"
PROFILE_AMBIGUOUS = "profile_ambiguous"
PII_FLAGGED = "pii_flagged"

#: The declaration field a ``where_expression`` is written from.
FILTER_FIELD = "population.filters"

# How the package's production setting was read.
ENABLED = "enabled"
DISABLED = "disabled"
UNKNOWN = "unknown"

# Why the production setting reads as it does.
NOT_SET = "not_set"
LITERAL = "literal"
TARGET_EXPRESSION = "target_expression"
PROD_TARGET_UNSET = "prod_target_unset"
EXPRESSION_UNREAD = "expression_unread"
PROJECT_UNREADABLE = "project_unreadable"

# How the package is present.
INSTALLED = "installed"
ADDED_BY_PLAN = "added_by_plan"
NOT_INSTALLED = "not_installed"


@dataclass(frozen=True)
class Gate:
    """Whether a plan may write monitors, and why not where it may not.

    ``package`` is ``installed``, ``added_by_plan`` (the plan's own
    ``packages_yml`` edit declares it) or ``not_installed``. ``production`` is
    ``enabled``, ``disabled`` or ``unknown``, read from the repository's
    ``dbt_project.yml`` as the plan leaves it, with ``production_reason``
    saying how: ``not_set`` (dbt's default, enabled), ``literal`` (a written
    ``true`` or ``false``), ``target_expression`` (a test of ``target.name``
    evaluated against ``prod_target``), ``prod_target_unset`` (such a test
    with no ``dbt_prod_target`` committed), ``expression_unread`` (any other
    Jinja) or ``project_unreadable``.
    """

    vendor: str
    package: str
    release: str | None
    production: str
    production_reason: str
    prod_target: str | None
    expression: str | None = None

    @property
    def open(self) -> bool:
        return self.package != NOT_INSTALLED and self.production == ENABLED

    @property
    def closed_reason(self) -> str | None:
        """The ``not_rendered`` reason every monitor takes, or ``None``."""

        if self.package == NOT_INSTALLED:
            return PACKAGE_NOT_INSTALLED
        if self.production == DISABLED:
            return PRODUCTION_DISABLED
        if self.production == UNKNOWN:
            return PRODUCTION_UNKNOWN
        return None

    def warning(self) -> str | None:
        """One plan-level sentence naming why no monitor was written."""

        reason = self.closed_reason
        if reason == PACKAGE_NOT_INSTALLED:
            return (
                "monitors.vendor is elementary, but the Elementary package is not "
                "installed, so no monitor was written: declare it in packages.yml "
                "(dex init offers the install as a plan) and run `transform deps`, "
                "then declare again"
            )
        if reason == PRODUCTION_DISABLED:
            where = (
                f"the production target '{self.prod_target}'"
                if self.production_reason == TARGET_EXPRESSION
                else "every target"
            )
            return (
                "monitors.vendor is elementary, but dbt_project.yml disables the "
                f"Elementary package on {where}, so a monitor written there would "
                "never run; no monitor was written"
            )
        if reason == PRODUCTION_UNKNOWN:
            if self.production_reason == PROD_TARGET_UNSET:
                why = (
                    "dbt_project.yml enables the Elementary package per target "
                    f"({self.expression}) and no dbt_prod_target is committed"
                )
                fix = "set dbt_prod_target in .dex/config.yml, then declare again"
            elif self.production_reason == EXPRESSION_UNREAD:
                why = (
                    "dex cannot evaluate the Elementary package's +enabled "
                    f"setting in dbt_project.yml ({self.expression})"
                )
                fix = (
                    "dex reads a literal true or false, or a test of target.name "
                    "against literals"
                )
            else:
                why = "dex could not read dbt_project.yml"
                fix = "fix the file, then declare again"
            return (
                f"monitors.vendor is elementary, but {why}, so whether a monitor "
                f"would run in production is unknown and no monitor was written; {fix}"
            )
        return None


def gate(
    project_dir: Path | str,
    edits: Iterable[PlanEdit],
    vendor: str | None,
    prod_target: str | None,
) -> Gate | None:
    """The gate for ``vendor`` (``monitors.vendor``), or ``None`` when unset.

    Read from the repository alone, as the plan's ``edits`` leave it: no
    connection, no dbt call, no production target named to dbt.
    """

    if vendor is None:
        return None
    from .build import _root_text, declared_packages, package_overlay
    from .elementary_install import _is_elementary, detect

    project = Path(project_dir)
    edits = list(edits)
    found = detect(project)
    if found["installed"]:
        package = INSTALLED
    else:
        overlay = package_overlay(edits)
        declared_after = any(
            _is_elementary(p) for p in declared_packages(project, overlay=overlay)
        )
        package = (
            ADDED_BY_PLAN
            if overlay and declared_after and not found["declared"]
            else NOT_INSTALLED
        )
    overlay = package_overlay(edits)
    text = _root_text(project, "dbt_project.yml", overlay or None)
    production, reason, expression = production_enabled(text, prod_target)
    return Gate(
        vendor=vendor,
        package=package,
        release=found["release"],
        production=production,
        production_reason=reason,
        prod_target=prod_target,
        expression=expression,
    )


_COMPARE = re.compile(
    r"""^\{\{-?\s*target\.name\s*(==|!=)\s*(['"])([^'"]*)\2\s*-?\}\}$"""
)
_MEMBERSHIP = re.compile(
    r"""^\{\{-?\s*target\.name\s+(not\s+in|in)\s*[\[\(]([^\]\)]*)[\]\)]\s*-?\}\}$"""
)
_LITERAL_ITEM = re.compile(r"""^\s*(['"])([^'"]*)\1\s*$""")


def production_enabled(
    project_yml: str | None, prod_target: str | None
) -> tuple[str, str, str | None]:
    """Whether ``dbt_project.yml`` (its text) enables the Elementary package on
    the production target: ``(production, reason, expression)``.

    Reads ``models.elementary`` ``+enabled`` (or ``enabled``) at the package's
    root, the setting the package's own models answer to and so the one its
    tests' ``is_elementary_enabled()`` reads, else the same key at the root of
    ``models:``, which dbt applies to a package's models too. Absent at both is
    dbt's default, enabled. A boolean, or the text ``true``/``false``, is read
    as written. A Jinja test of ``target.name`` (``==``, ``!=``, ``in``,
    ``not in`` against literals, the form ``dex init`` writes) is evaluated
    with ``prod_target``; without one it is ``unknown``. Anything else is
    ``unknown``, never guessed.
    """

    if project_yml is None:
        return ENABLED, NOT_SET, None
    try:
        loaded = yaml.safe_load(project_yml)
    except yaml.YAMLError:
        return UNKNOWN, PROJECT_UNREADABLE, None
    if not isinstance(loaded, Mapping):
        return ENABLED, NOT_SET, None
    models = loaded.get("models")
    if not isinstance(models, Mapping):
        return ENABLED, NOT_SET, None
    package = models.get(ELEMENTARY)
    value: Any = None
    # The package's own setting overrides one at the root of `models:`, which
    # dbt applies to every package's models too.
    for level in (package, models):
        if not isinstance(level, Mapping):
            continue
        key = next((k for k in ("+enabled", "enabled") if k in level), None)
        if key is not None:
            value = level[key]
            break
    else:
        return ENABLED, NOT_SET, None
    if isinstance(value, bool):
        return (ENABLED if value else DISABLED), LITERAL, None
    text = str(value).strip()
    if text.lower() in ("true", "false"):
        return (ENABLED if text.lower() == "true" else DISABLED), LITERAL, None
    compare = _COMPARE.match(text)
    membership = _MEMBERSHIP.match(text)
    if compare is None and membership is None:
        return UNKNOWN, EXPRESSION_UNREAD, text
    if prod_target is None:
        return UNKNOWN, PROD_TARGET_UNSET, text
    if compare is not None:
        operator, _, literal = compare.groups()
        holds = (prod_target == literal) == (operator == "==")
        return (ENABLED if holds else DISABLED), TARGET_EXPRESSION, text
    if membership is None:  # pragma: no cover - one of the two matched above
        return UNKNOWN, EXPRESSION_UNREAD, text
    operator, body = membership.groups()
    items = [item for item in body.split(",") if item.strip()]
    literals = [_LITERAL_ITEM.match(item) for item in items]
    if not all(literals):
        return UNKNOWN, EXPRESSION_UNREAD, text
    names = {m.group(2) for m in literals if m is not None}
    holds = (prod_target in names) == (operator.split()[0] == "in")
    return (ENABLED if holds else DISABLED), TARGET_EXPRESSION, text


# --- the population filter as a where_expression (#1483) ---------------------

#: Connectors whose identifiers are quoted with backticks; every other one
#: quotes with double quotes, as its dbt adapter's ``quote`` does.
_BACKTICK_DIALECTS = frozenset({"bigquery", "databricks", "spark", "clickhouse"})

#: Connectors where a backslash starts an escape sequence in a string literal,
#: so a backslash is doubled and a quote written ``\'``: the rule the
#: ``dex_population_filter_literal`` macro applies. ClickHouse doubles the
#: backslash and the quote. Every other connector doubles the quote alone.
_BACKSLASH_DIALECTS = frozenset(
    {"bigquery", "snowflake", "databricks", "spark", "redshift"}
)

_PLAIN_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

#: Words no supported warehouse accepts unquoted as a column name. A column
#: spelled as one was created quoted, so it is written quoted.
_RESERVED = frozenset(
    """
    all and any as asc between both by case cast check collate column
    constraint create cross current_date current_time current_timestamp
    current_user default desc distinct else end except exists false fetch for
    foreign from full grant group having in inner intersect into is join
    lateral leading left like limit natural not null offset on or order outer
    primary qualify references right select some table then to trailing true
    union unique user using when where window with
    """.split()  # noqa: SIM905
)

#: Snowflake's own reserved words, which replace :data:`_RESERVED` there:
#: those its reserved-keywords page reserves outright, those it bars as a
#: column name or a column reference (``case``, ``true``, ``current_date``,
#: ``constraint``, ...), and ``asc`` and ``desc``, which the page's prose
#: reserves. Snowflake folds an unquoted name to upper case, so quoting a word
#: it does not reserve (``user``, ``limit``) would name another column. A
#: column spelled as one of these was created quoted, in a case dex cannot
#: know, so it is written only where the entry marks it ``quote: true``.
_SNOWFLAKE_RESERVED = frozenset(
    """
    all alter and any as asc between by case cast check column connect
    constraint create current current_date current_time current_timestamp
    current_user delete desc distinct drop else exists false following for
    from grant group having ilike in increment insert intersect into is like
    localtime localtimestamp minus not null of on or order qualify regexp
    revoke rlike row rows sample select set some start table tablesample then
    to trigger true try_cast union unique update values when whenever where
    window with
    """.split()  # noqa: SIM905
)

#: BigQuery's reserved keywords, as its lexical-structure reference lists
#: them, which replace :data:`_RESERVED` there: a column spelled as one has
#: to be backtick-quoted (``range``, ``partition``, ``interval``, ``rows``).
#: BigQuery matches a quoted name case-insensitively, so quoting is safe.
_BIGQUERY_RESERVED = frozenset(
    """
    all and any array as asc assert_rows_modified at between by case cast
    collate contains create cross cube current default define desc distinct
    else end enum escape except exclude exists extract false fetch following
    for from full graph_table group grouping groups hash having if ignore in
    inner intersect interval into is join lateral left like limit lookup merge
    natural new no not null nulls of on or order outer over partition preceding
    proto qualify range recursive respect right rollup rows select set some
    struct tablesample then to treat true unbounded union unnest using when
    where window with within
    """.split()  # noqa: SIM905
)

_RESERVED_BY_DIALECT = {
    "snowflake": _SNOWFLAKE_RESERVED,
    "bigquery": _BIGQUERY_RESERVED,
}

_JINJA = ("{{", "{%", "{#")


class FilterUnwritableError(RequestError):
    """A population filter dex cannot write as a ``where_expression``.

    ``reason`` is the ``not_rendered`` reason every monitor of the model takes.
    Never leaves :func:`planned`, which turns it into that reason.
    """

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason


def _identifier(column: str, dialect: str | None, *, quote: bool) -> str:
    """``column`` spelled the way the model's adapter reads it.

    Written as declared, unquoted, which is how dbt and every test it runs
    name a column, unless it has to be quoted: the YAML entry says ``quote:
    true``, or the name is not a plain identifier or is a reserved word. Then
    it is quoted the way the connector's adapter quotes it (backticks on
    BigQuery, Databricks and ClickHouse, double quotes elsewhere), which needs
    the connector to be known. Unquoted, Snowflake folds the name to upper
    case, as it folded the column's own name when dbt created it, so on
    Snowflake only its own reserved words count, and one of those without
    ``quote: true`` is unwritable: its case is unknowable. BigQuery reads its
    own reserved words too, which it matches case-insensitively quoted.
    """

    if any(mark in column for mark in _JINJA):
        raise FilterUnwritableError(
            FILTER_UNWRITABLE,
            f"the filter column '{column}' holds a Jinja delimiter, which dbt "
            "would render",
        )
    reserved = _RESERVED_BY_DIALECT.get(dialect or "", _RESERVED)
    plain = bool(_PLAIN_IDENTIFIER.match(column))
    if not quote and plain and column.lower() not in reserved:
        return column
    if not quote and plain and dialect == "snowflake":
        raise FilterUnwritableError(
            FILTER_UNWRITABLE,
            f"the filter column '{column}' is a Snowflake reserved word, so it was "
            "created quoted, in a case dex cannot know: mark the column "
            "`quote: true` in the model's YAML",
        )
    if dialect is None:
        raise FilterUnwritableError(
            FILTER_UNWRITABLE,
            f"the filter column '{column}' has to be quoted, and no connector is "
            "configured to say how",
        )
    mark = "`" if dialect in _BACKTICK_DIALECTS else '"'
    if mark in column:
        raise FilterUnwritableError(
            FILTER_UNWRITABLE,
            f"the filter column '{column}' holds the quote character {mark}",
        )
    return f"{mark}{column}{mark}"


def _text_literal(text: str, dialect: str | None) -> str:
    """``text`` as a single-quoted string literal on ``dialect``.

    A line break is refused on every connector: BigQuery's single-quoted
    strings cannot hold one even escaped, and one written raw would also break
    the line the YAML holds the expression on.
    """

    if "\n" in text or "\r" in text:
        raise FilterUnwritableError(
            FILTER_UNWRITABLE,
            "a filter value holds a line break, which a single-quoted literal "
            "cannot hold on every connector",
        )
    if "'" not in text and "\\" not in text:
        return f"'{text}'"
    if dialect is None:
        raise FilterUnwritableError(
            FILTER_UNWRITABLE,
            "a filter value holds a quote or a backslash, whose escaping depends on "
            "the connector, and no connector is configured",
        )
    if dialect in _BACKSLASH_DIALECTS:
        return "'" + text.replace("\\", "\\\\").replace("'", "\\'") + "'"
    if dialect == "clickhouse":
        return "'" + text.replace("\\", "\\\\").replace("'", "''") + "'"
    return "'" + text.replace("'", "''") + "'"


def _literals(values: list[Any], dialect: str | None) -> str:
    """The values as SQL literals, comma-joined, typed as declared.

    Text is a quoted string, a number itself and a boolean ``TRUE`` or
    ``FALSE``, with no cast of the column: the literals' type is the column's
    type as the declaration states it. ``maintain verify`` and the
    ``dex_population_filter`` test read the column's type at run time and
    cast it to text where they must; a ``where_expression`` is fixed text
    written at plan time, so declare the values in the column's type. A list
    in another type (``includes: [1, 2]`` on a text column) can fail in the
    warehouse, or, where the warehouse converts implicitly (DuckDB matches
    ``'01'`` in a text column against ``1``), change which rows the monitor
    counts. A list mixing text with numbers or booleans states no one type, so
    it is unwritable.
    """

    import math

    texts = [isinstance(v, str) for v in values]
    if any(texts) and not all(texts):
        raise FilterUnwritableError(
            FILTER_UNWRITABLE,
            "a filter list mixes text with numbers or booleans, so it states no "
            "one type to compare the column with: quote every value, or none",
        )
    out: list[str] = []
    for value in values:
        if isinstance(value, bool):
            out.append("TRUE" if value else "FALSE")
        elif isinstance(value, (int, float)):
            if isinstance(value, float) and not math.isfinite(value):
                raise FilterUnwritableError(
                    FILTER_UNWRITABLE, f"the filter value {value} is not a SQL number"
                )
            out.append(str(value))
        else:
            out.append(_text_literal(str(value), dialect))
    joined = ", ".join(out)
    if any(mark in joined for mark in _JINJA):
        # dbt renders a test argument as Jinja: a value holding a Jinja
        # delimiter would run as code rather than be compared as text.
        raise FilterUnwritableError(
            FILTER_UNWRITABLE,
            "a filter value holds a Jinja delimiter, which dbt would render",
        )
    return joined


def _condition(
    entry: PopulationFilter,
    declaration: Declaration,
    dialect: str | None,
    quoted: frozenset[str],
) -> list[str]:
    column = _identifier(entry.column, dialect, quote=entry.column.lower() in quoted)
    out: list[str] = []
    if entry.includes:
        # `in` drops a NULL row, as the declaration's includes does: a NULL is
        # outside every list.
        out.append(f"{column} in ({_literals(entry.includes, dialect)})")
    if entry.excludes:
        condition = f"{column} not in ({_literals(entry.excludes, dialect)})"
        declared = next(
            (
                c
                for name, c in declaration.columns.items()
                if name.lower() == entry.column.lower()
            ),
            None,
        )
        never_null = (
            declared is not None
            and (declared.null_rule or "").strip().lower() == "never"
        )
        if not never_null:
            # `not in` drops a NULL row, which an excludes keeps: a NULL is no
            # excluded value. The disjunction is parenthesised because
            # Elementary splices the expression after its own time-bucket
            # filter with a bare `and`.
            condition = f"({condition} or {column} is null)"
        out.append(condition)
    return out


def where_expression(
    declaration: Declaration,
    dialect: str | None,
    outputs: frozenset[str] | None = None,
    quoted: frozenset[str] = frozenset(),
) -> str | None:
    """The ``where_expression`` ``declaration``'s ``population.filters`` give,
    or ``None`` where it declares none.

    ``includes: [a, b]`` is ``<column> in ('a', 'b')`` and ``excludes`` is
    ``<column> not in (...)``, written ``(<column> not in (...) or <column> is
    null)`` unless the column declares ``null_rule: never``, since ``not in``
    drops the NULL rows an excludes keeps. Several filters join with ``and``.

    Every part that holds an ``or`` is parenthesised, so the expression reads
    the same wherever it is spliced: Elementary's table monitors read it as
    ``where <expression>``, and, with a ``timestamp_column``, as ``where
    <timestamp> >= <bucket start> and <expression>`` (``table_monitoring_query``
    at the package's e3ab23d8), with no parentheses of its own.

    The values are the declaration's authored text, never read from data (a
    filter on a column that looks like personal data is refused before this
    runs). ``quoted`` names the columns the YAML entry marks ``quote: true``.
    Raises :class:`FilterUnwritableError` where the filter cannot be written: its
    column is not one the model outputs (``outputs``, where known), or it
    needs a quoting or an escaping rule and no connector names one.
    """

    population = declaration.population
    if population is None:
        return None
    parts: list[str] = []
    for entry in population.filters:
        if not (entry.includes or entry.excludes):
            continue
        if outputs is not None and entry.column.lower() not in outputs:
            raise FilterUnwritableError(
                FILTER_COLUMN_NOT_OUTPUT,
                f"the filter column '{entry.column}' is not a column the model outputs",
            )
        parts.extend(_condition(entry, declaration, dialect, quoted))
    return " and ".join(parts) or None


# --- a dimension's domain, read from the exploration cache (#1484) -------------

#: A :class:`DomainReading` that measured a count.
MEASURED = "measured"


@dataclass(frozen=True)
class DomainReading:
    """What the exploration cache says about one column's domain.

    ``status`` is ``measured``, ``not_profiled`` (no cached profile of the
    model, of the column, or no count on it) or ``profile_ambiguous`` (the
    model's name matches several cached relations, which no profile run
    settles). ``pii`` is whether the cached profile flags the column, at any
    confidence, after ``pii_overrides``. ``command`` is the ``explore profile``
    that would supply a count, where one can.
    """

    status: str
    distinct_count: int | None = None
    exact: bool = False
    pii: bool = False
    identifier: str | None = None
    profiled_at: str | None = None
    command: str | None = None
    detail: str | None = None


@dataclass
class Domains:
    """The exploration cache's domains for the models a plan declares.

    Built once per plan by the caller, from the cache with ``pii_overrides``
    already applied (``cache.mask_overridden``). Reads the cache and the
    repository only: it opens no connection and profiles nothing, so a column
    the cache has no count for is named with the command that would supply one
    rather than measured. ``files`` and ``model_paths`` resolve a model's
    literal alias; ``dev_namespace`` names where a project model's dev build
    is read from, as drafting does; ``created`` names the models the plan
    writes SQL for that the project did not have, which have no build to
    profile yet. ``note`` is why the cache could not be read, where it could
    not.
    """

    cache: Any = None
    files: Mapping[str, str] = field(default_factory=dict)
    model_paths: list[str] = field(default_factory=list)
    dev_namespace: str | None = None
    manifest: Any = None
    created: frozenset[str] = frozenset()
    note: str | None = None

    def read(self, model: str, column: str) -> DomainReading:
        """``column`` of ``model`` (a version label reads that version).

        A versioned model's bare name is its shared entry, whose tests every
        version inherits, so each version's relation is read and the column
        qualifies only where it does on all of them: flagged on any is flagged,
        unread on any is unread, and the count is the largest.
        """

        from .. import model_versions as mv

        base, version = mv.split_label(model)
        versions = _version_relations(self.files, base)
        if version is not None:
            relation = (versions or {}).get(str(version)) or f"{base}_v{version}"
            return self._read_relation(model, relation, column)
        if versions:
            readings = [
                self._read_relation(model, relation, column)
                for relation in versions.values()
            ]
            for reading in readings:
                if reading.pii:
                    return reading
            for reading in readings:
                if reading.status != MEASURED:
                    return reading
            return max(readings, key=lambda r: r.distinct_count or 0)
        from .drafting import _alias_of_model

        relation = _alias_of_model(self.files, self.model_paths, base) or base
        return self._read_relation(model, relation, column)

    def _read_relation(self, model: str, relation: str, column: str) -> DomainReading:
        from ..cache import match_identifier
        from .drafting import _column, dataset_for

        dataset = dataset_for(self.cache, relation, manifest=self.manifest)
        if dataset is None and self.cache is not None:
            matched = match_identifier(
                relation, [d.identifier for d in self.cache.datasets]
            )
            if len(matched) > 1:
                # Which relation is the model's build is not settled, so a
                # flag on any of them counts: a column monitor reads no count,
                # only whether the column is personal data.
                candidates = [
                    _column(d, column)
                    for d in self.cache.datasets
                    if d.identifier in matched
                ]
                return DomainReading(
                    PROFILE_AMBIGUOUS,
                    pii=_named_as_pii(column)
                    or any(c is not None and c.pii is not None for c in candidates),
                    detail=(
                        f"'{relation}' matches {len(matched)} relations in the "
                        f"cache ({', '.join(matched)}), so which one is the model's "
                        "build is not settled"
                    ),
                )
        if dataset is None:
            return DomainReading(
                NOT_PROFILED,
                pii=_named_as_pii(column),
                command=self._command(model, relation),
                detail=self.note or "the cache has not profiled the model",
            )
        profile = _column(dataset, column)
        if profile is None or profile.distinct_count is None:
            return DomainReading(
                NOT_PROFILED,
                pii=(
                    _named_as_pii(column)
                    if profile is None
                    else profile.pii is not None
                ),
                identifier=dataset.identifier,
                profiled_at=dataset.profiled_at,
                command=self._profile_again(dataset.identifier),
                detail=(
                    f"the cached profile of {dataset.identifier} holds no column "
                    f"'{column}'"
                    if profile is None
                    else f"the cached profile of {dataset.identifier} holds no "
                    f"distinct count for '{column}'"
                ),
            )
        distinct, exact = profile.distinct_count, profile.distinct_count_exact
        domain = profile.value_domain
        if not exact and domain is not None and domain.elided == 0:
            # A value domain that fit its cap lists every value, so it counts
            # the domain exactly where the profile's own count is an estimate:
            # an approximate count near the bound cannot say which side it is.
            distinct, exact = len(domain.values), True
        return DomainReading(
            MEASURED,
            distinct_count=distinct,
            exact=exact,
            pii=profile.pii is not None,
            identifier=dataset.identifier,
            profiled_at=dataset.profiled_at,
        )

    def _profile_again(self, identifier: str) -> str:
        """The profile that reads ``identifier`` again: with ``--target dev``
        where it is a relation in the dev namespace, which a plain profile,
        reading the source allowlist only, cannot reach."""

        namespace = identifier.rsplit(".", 1)[0].lower() if "." in identifier else ""
        dev = (self.dev_namespace or "").lower()
        in_dev = bool(dev) and (namespace == dev or namespace.endswith(f".{dev}"))
        return f"explore profile {identifier}" + (" --target dev" if in_dev else "")

    def _created(self, model: str) -> bool:
        """Whether the plan creates the SQL file that builds ``model``: for a
        version label, the file the version is defined in (its ``defined_in``,
        else ``<model>_v<N>``), never its relation, which an ``alias`` names."""

        from .. import model_versions as mv

        if model.lower() in self.created:
            return True
        base, version = mv.split_label(model)
        if version is None:
            return False
        found = mv.lookup(mv.cached_version_map(self.files, self.model_paths), base)
        item = found.version(str(version)) if found is not None else None
        stem = item.stem if item is not None else None
        return (stem or f"{base}_v{version}").lower() in self.created

    def _command(self, model: str, relation: str) -> str:
        profile = (
            f"explore profile {relation}"
            if self.dev_namespace is None
            else f"explore profile {self.dev_namespace}.{relation} --target dev"
        )
        if self._created(model):
            # Built by this plan: there is nothing on dev to profile yet.
            return f"transform build --target dev, then {profile}"
        return profile


def _version_relations(files: Mapping[str, str], base: str) -> dict[str, str] | None:
    """Each version of ``base`` and the relation dbt builds it as (its
    ``alias``, else ``<base>_v<N>``), or ``None`` where ``base`` declares no
    ``versions:`` in the project's YAML."""

    for path, content in files.items():
        if not path.endswith((".yml", ".yaml")) or "versions:" not in (content or ""):
            continue
        try:
            loaded = yaml.safe_load(content)
        except yaml.YAMLError:
            continue
        entries = loaded.get("models") if isinstance(loaded, Mapping) else None
        for entry in entries or []:
            if not isinstance(entry, Mapping):
                continue
            if str(entry.get("name", "")).lower() != base.lower():
                continue
            items = entry.get("versions")
            if not isinstance(items, list) or not items:
                continue
            out: dict[str, str] = {}
            for item in items:
                if not isinstance(item, Mapping) or item.get("v") is None:
                    continue
                config = item.get("config")
                alias = (
                    config.get("alias") if isinstance(config, Mapping) else None
                ) or item.get("alias")
                out[str(item["v"])] = str(alias) if alias else f"{base}_v{item['v']}"
            return out or None
    return None


def _named_as_pii(column: str) -> bool:
    """Whether the name detector flags ``column`` with no profile to read: a
    column the cache has never profiled is not asked for a profile it could not
    pass. A generic ``*_name`` match is provisional, refined only against the
    column's values, so it is left to the profile."""

    from ..explore.profile import classify_pii

    detected, provisional = classify_pii(column, "varchar")
    return detected is not None and not provisional


def pii_marked(content: str | None, model: str) -> frozenset[str]:
    """The columns ``content``'s entry for ``model`` marks as personal data
    (``contains_pii`` or any key a reader accepts), lower-cased."""

    from ..guards.pii_meta import entry_meta, says_pii

    node = _entry(content, model)
    columns = node.get("columns") if isinstance(node, Mapping) else None
    return frozenset(
        str(c["name"]).lower()
        for c in columns or []
        if isinstance(c, Mapping)
        and c.get("name") is not None
        and says_pii(entry_meta(c))
    )


@dataclass
class Monitor:
    """One monitor a declaration gives, written or not.

    ``dimension`` is set on a ``dimension_anomalies`` (#1484): the one column
    it groups by. ``domain`` is what the cache said about that column.

    ``column`` is set on a ``column_anomalies`` (#1485, #1591): the column the
    test is listed under, which the package reads as its ``column_name``.
    ``monitors`` is its list, in :data:`COLUMN_MONITOR_ORDER`, and ``sources``
    each declaration field that contributed with the monitors it gave, in
    declaration order: ``field`` is the first of them.
    """

    model: str
    test: str
    timestamp_column: str | None
    field: str | None
    withheld: str | None = None
    #: The ``where_expression`` the population filters give (#1483).
    where_expression: str | None = None
    dimension: str | None = None
    domain: DomainReading | None = None
    column: str | None = None
    monitors: tuple[str, ...] = ()
    sources: tuple[tuple[str, tuple[str, ...]], ...] = ()
    #: A ``data_freshness_sla``'s deadline (#1486): the time of day, its zone,
    #: the days it is checked on as Elementary spells them (``None``: every
    #: day), and the declared basis of its column.
    sla_time: str | None = None
    timezone: str | None = None
    days: tuple[str, ...] | None = None
    basis: str | None = None

    def _arguments(self, with_where: bool) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if self.test == COLUMN:
            out["column_anomalies"] = list(self.monitors)
        if self.dimension is not None:
            out["dimensions"] = [self.dimension]
        if self.timestamp_column is not None:
            out["timestamp_column"] = self.timestamp_column
        if self.test == SLA:
            out["sla_time"] = self.sla_time
            out["timezone"] = self.timezone
            if self.days is not None:
                out["day_of_week"] = list(self.days)
        if with_where and self.where_expression is not None:
            out["where_expression"] = self.where_expression
        return out

    @property
    def arguments(self) -> dict[str, Any]:
        """The arguments as the row reports them: a withheld monitor's carry no
        ``where_expression``, since none was written."""

        return self._arguments(self.withheld is None)

    @property
    def name(self) -> str:
        return f"{ELEMENTARY}.{self.test}"

    def spec(self) -> dict[str, Any]:
        """The test as it renders under the model's entry."""

        from .rewrite import _unsampled

        [(_, rendered)] = _unsampled(self.name).items()
        meta = dict(rendered["config"]["meta"])
        arguments = self._arguments(True)
        marker: dict[str, Any] = {"vendor": ELEMENTARY, "from": self.field}
        if "where_expression" in arguments:
            marker["filter"] = FILTER_FIELD
        if self.test == DIMENSION:
            marker["max_distinct"] = DIMENSION_MAX_DISTINCT
        if self.test == COLUMN:
            # Which declaration field gave which monitor, so a union reads
            # back as the rows it came from (#1485, #1591).
            marker["sources"] = {source: list(given) for source, given in self.sources}
        meta["dex"] = {MARKER: marker}
        return {
            self.name: {
                "arguments": arguments,
                "config": {"tags": [TAG], "meta": meta},
            }
        }


def _dimension_monitors(
    declaration: Declaration,
    times: list[str],
    outputs: frozenset[str] | None,
    domains: Domains,
    where: str | None,
    unwritable: str | None,
    marked: frozenset[str],
) -> list[Monitor]:
    """One ``dimension_anomalies`` per column declared ``role: dimension``
    (#1484), each withheld where it does not qualify. The checks run in this
    order, so the reason given is the first that holds: the column is output,
    there is one time column, the column is not personal data (a flagged
    column is out at any domain, so its count is never read), the cache counts
    its domain, the count is at most :data:`DIMENSION_MAX_DISTINCT`, and the
    population filter can be written."""

    model = declaration.model
    timestamp = times[0] if len(times) == 1 else None
    out: list[Monitor] = []
    for name, column in declaration.columns.items():
        if column.role is not Role.DIMENSION:
            continue
        withheld: str | None = None
        reading: DomainReading | None = None
        if outputs is not None and name.lower() not in outputs:
            withheld = COLUMN_NOT_OUTPUT
        elif not times:
            withheld = NO_TIME_COLUMN
        elif timestamp is None:
            withheld = AMBIGUOUS_TIME_COLUMN
        elif outputs is not None and timestamp.lower() not in outputs:
            # The time column the test would read is not one the model outputs.
            withheld = COLUMN_NOT_OUTPUT
        else:
            reading = domains.read(model, name)
            if reading.pii or name.lower() in marked:
                withheld = PII_FLAGGED
            elif reading.status != MEASURED:
                withheld = reading.status
            elif (reading.distinct_count or 0) > DIMENSION_MAX_DISTINCT:
                withheld = DOMAIN_TOO_LARGE
            else:
                withheld = unwritable
        out.append(
            Monitor(
                model,
                DIMENSION,
                timestamp,
                f"columns.{name}.role: dimension",
                withheld,
                where_expression=where,
                dimension=name,
                domain=reading,
            )
        )
    return out


def column_sources(name: str, column: Any) -> list[tuple[str, tuple[str, ...]]]:
    """The declaration fields of ``column`` (named ``name``) that give it
    column monitors, each with the monitors it gives: ``unit: fraction`` or
    ``unit: percent`` (#1485), and ``role: measure`` with an ``additivity``
    (#1591). A measure with no additivity, an additivity on a column that is
    not a measure, and any other unit give nothing: Dex does not guess."""

    out: list[tuple[str, tuple[str, ...]]] = []
    unit = column.unit.strip().lower() if isinstance(column.unit, str) else None
    if unit in UNIT_MONITORS:
        out.append((f"columns.{name}.unit: {unit}", UNIT_MONITORS[unit]))
    if column.role is Role.MEASURE and column.additivity is not None:
        additivity = Additivity(column.additivity)
        out.append(
            (
                f"columns.{name}.additivity: {additivity.value}",
                ADDITIVITY_MONITORS[additivity],
            )
        )
    return out


def union(sources: Iterable[tuple[str, tuple[str, ...]]]) -> tuple[str, ...]:
    """Every monitor ``sources`` give, once each, in the package's order."""

    given = {m for _, monitors in sources for m in monitors}
    return tuple(m for m in COLUMN_MONITOR_ORDER if m in given)


def _column_monitors(
    declaration: Declaration,
    times: list[str],
    outputs: frozenset[str] | None,
    domains: Domains | None,
    where: str | None,
    unwritable: str | None,
    marked: frozenset[str],
) -> list[Monitor]:
    """One ``column_anomalies`` per column whose unit or additivity gives
    monitors (#1485, #1591; :func:`column_sources`), each withheld where it
    does not qualify. The checks run in this order, so the reason given is
    the first that holds: the column is output, there is one time column and
    the model outputs it, the column is not personal data, and the population
    filter can be written."""

    model = declaration.model
    timestamp = times[0] if len(times) == 1 else None
    out: list[Monitor] = []
    for name, column in declaration.columns.items():
        sources = column_sources(name, column)
        if not sources:
            continue
        withheld: str | None = None
        if outputs is not None and name.lower() not in outputs:
            withheld = COLUMN_NOT_OUTPUT
        elif not times:
            withheld = NO_TIME_COLUMN
        elif timestamp is None:
            withheld = AMBIGUOUS_TIME_COLUMN
        elif outputs is not None and timestamp.lower() not in outputs:
            withheld = COLUMN_NOT_OUTPUT
        elif name.lower() in marked or (
            domains.read(model, name).pii
            if domains is not None
            else _named_as_pii(name)
        ):
            withheld = PII_FLAGGED
        else:
            withheld = unwritable
        out.append(
            Monitor(
                model,
                COLUMN,
                timestamp,
                sources[0][0],
                withheld,
                where_expression=where,
                column=name,
                monitors=union(sources),
                sources=tuple(sources),
            )
        )
    return out


def planned(
    declaration: Declaration,
    outputs: frozenset[str] | None,
    dialect: str | None = None,
    quoted: frozenset[str] = frozenset(),
    domains: Domains | None = None,
    marked: frozenset[str] = frozenset(),
    release: str | None = None,
) -> list[Monitor]:
    """Every monitor ``declaration`` (as the entry states it) gives.

    One time column gives a ``volume_anomalies`` and a
    ``freshness_anomalies`` on it. Several give a ``freshness_anomalies`` on
    each, and a ``volume_anomalies`` withheld as ``ambiguous_time_column``,
    since it takes one. None gives both withheld as ``no_time_column``. A time
    column the model's SQL provably does not output is ``column_not_output``.

    Each column declared ``role: dimension`` gives a ``dimension_anomalies``
    on it (#1484; :func:`_dimension_monitors`), read against ``domains``, the
    exploration cache, and ``marked``, the columns the YAML entry marks as
    personal data. Without ``domains`` no dimension monitor is planned.

    Each column whose unit or additivity gives monitors gives one
    ``column_anomalies`` on it (#1485, #1591; :func:`_column_monitors`), on the
    same time column rule, listed under the column rather than the model.

    Each time column declaring a ``freshness.deadline`` gives a
    ``data_freshness_sla`` on it (#1486; :func:`_sla_monitors`), withheld
    where the installed ``release`` predates the test.

    Each monitor written carries the ``where_expression`` the population
    filters give (:func:`where_expression`), on ``dialect``. Where they cannot
    be written, every monitor is withheld with the reason
    (``filter_column_not_output`` or ``filter_unwritable``) rather than
    written without the filter.
    """

    model = declaration.model
    times = [
        name for name, column in declaration.columns.items() if column.role is Role.TIME
    ]
    unwritable: str | None = None
    where: str | None = None
    if times:
        try:
            where = where_expression(declaration, dialect, outputs, quoted)
        except FilterUnwritableError as exc:
            unwritable = exc.reason
    dimensions = (
        _dimension_monitors(
            declaration, times, outputs, domains, where, unwritable, marked
        )
        if domains is not None
        else []
    ) + _column_monitors(
        declaration, times, outputs, domains, where, unwritable, marked
    )
    if not times:
        return [
            Monitor(model, test, None, None, withheld=NO_TIME_COLUMN)
            for test in (VOLUME, FRESHNESS)
        ] + dimensions

    def field_of(column: str) -> str:
        return f"columns.{column}.role: time"

    def withheld(column: str) -> str | None:
        if outputs is not None and column.lower() not in outputs:
            return COLUMN_NOT_OUTPUT
        return unwritable

    out: list[Monitor] = []
    if len(times) == 1:
        out.append(
            Monitor(
                model,
                VOLUME,
                times[0],
                field_of(times[0]),
                withheld(times[0]),
                where_expression=where,
            )
        )
    else:
        out.append(Monitor(model, VOLUME, None, None, withheld=AMBIGUOUS_TIME_COLUMN))
    out.extend(
        Monitor(
            model,
            FRESHNESS,
            column,
            field_of(column),
            withheld(column),
            where_expression=where,
        )
        for column in times
    )
    out.extend(
        _sla_monitors(
            declaration,
            times,
            withheld,
            where,
            release,
            _sla_breaks_on_quote(declaration, dialect),
        )
    )
    return out + dimensions


def _sla_breaks_on_quote(declaration: Declaration, dialect: str | None) -> bool:
    """Whether the package's ``data_freshness_sla`` would fail to compile with
    the population's ``where_expression`` (#1486). It repeats the expression in
    its result message, inside a single-quoted literal, re-escaping each quote
    (``\\'`` on BigQuery, ``''`` elsewhere: ``get_data_freshness_sla_query``
    at 0.26.1). A quote Dex wrote as ``\\'``, as it must on a connector where a
    backslash escapes, then closes that literal, and the warehouse refuses
    the statement. Doubled quotes (DuckDB, Postgres, ClickHouse) survive it.
    The other monitors do not repeat the expression and are unaffected."""

    if dialect not in _BACKSLASH_DIALECTS:
        return False
    filters = declaration.population.filters if declaration.population else []
    return any(
        isinstance(value, str) and "'" in value
        for entry in filters
        for value in (entry.includes or []) + (entry.excludes or [])
    )


def _sla_monitors(
    declaration: Declaration,
    times: list[str],
    withheld: Any,
    where: str | None,
    release: str | None,
    quote_breaks: bool = False,
) -> list[Monitor]:
    """One ``data_freshness_sla`` per time column declaring a
    ``freshness.deadline`` (#1486), with its ``sla_time``, ``timezone`` and,
    where ``days`` limits it, its ``day_of_week``. A ``max_lag`` alone gives
    none: a lag is not a deadline. Withheld where the time column is not
    output or the population cannot be written (``withheld``), and where the
    installed ``release`` is readable and older than :data:`SLA_SINCE`; an
    unread release is left to dbt's parse. ``quote_breaks`` withholds it as
    ``sla_filter_quote`` (:func:`_sla_breaks_on_quote`)."""

    from .elementary import release_key

    old = release is not None and (
        (key := release_key(release)) is not None and key < release_key(SLA_SINCE)  # type: ignore[operator]
    )
    out: list[Monitor] = []
    for column in times:
        freshness = declaration.columns[column].freshness
        if freshness is None or freshness.deadline is None:
            continue
        out.append(
            Monitor(
                declaration.model,
                SLA,
                column,
                f"columns.{column}.freshness.deadline: {freshness.deadline}",
                withheld(column)
                or (RELEASE_UNSUPPORTED if old else None)
                or (SLA_FILTER_QUOTE if quote_breaks and where else None),
                where_expression=where,
                sla_time=freshness.deadline,
                timezone=freshness.timezone,
                days=tuple(freshness.day_names) if freshness.day_names else None,
                basis=getattr(freshness.basis, "value", str(freshness.basis)),
            )
        )
    return out


def is_monitor(spec: Any) -> bool:
    """Whether a test spec (or a listed test) is a monitor Dex wrote."""

    return marker_of(spec) is not None


def marker_of(test: Any) -> Mapping[str, Any] | None:
    """A listed test's ``config.meta.dex.monitor`` (or top-level ``meta``)."""

    if not isinstance(test, Mapping) or len(test) != 1:
        return None
    body = next(iter(test.values()))
    if not isinstance(body, Mapping):
        return None
    config = body.get("config")
    for holder in (
        config.get("meta") if isinstance(config, Mapping) else None,
        body.get("meta"),
    ):
        if isinstance(holder, Mapping):
            dex = holder.get("dex")
            if isinstance(dex, Mapping) and isinstance(dex.get(MARKER), Mapping):
                return dex[MARKER]
    return None


def _bare(name: str) -> str:
    return name.split(".", 1)[1] if name.lower().startswith(f"{ELEMENTARY}.") else name


def _test_name(test: Any) -> str:
    if isinstance(test, str):
        return test
    if isinstance(test, Mapping) and len(test) == 1:
        return str(next(iter(test)))
    return ""


def _argument_raw(test: Any, name: str) -> Any:
    if not isinstance(test, Mapping) or len(test) != 1:
        return None
    body = next(iter(test.values()))
    if not isinstance(body, Mapping):
        return None
    nested = body.get("arguments")
    return (
        nested.get(name)
        if isinstance(nested, Mapping) and name in nested
        else body.get(name)
    )


def _argument_of(test: Any, name: str) -> str | None:
    value = _argument_raw(test, name)
    return str(value) if value is not None else None


def _timestamp_of(test: Any) -> str | None:
    return _argument_of(test, "timestamp_column")


def _where_of(test: Any) -> str | None:
    return _argument_of(test, "where_expression")


def _dimensions_of(test: Any) -> list[str] | None:
    """A listed test's ``dimensions``, as written (a single string reads as a
    one-column list), or ``None`` where it lists none."""

    value = _argument_raw(test, "dimensions")
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value]
    return [str(value)]


def _dimension_set(test: Any) -> frozenset[str]:
    return frozenset(d.lower() for d in _dimensions_of(test) or [])


def _monitors_of(test: Any) -> list[str] | None:
    """A listed test's ``column_anomalies`` list, as written (a single string
    reads as a one-monitor list), or ``None`` where it lists none and so runs
    the package's defaults."""

    value = _argument_raw(test, "column_anomalies")
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value]
    return [str(value)]


def _monitor_set(test: Any) -> frozenset[str]:
    return frozenset(m.lower() for m in _monitors_of(test) or [])


def _deadline_of(test: Any) -> tuple[str | None, str | None, tuple[str, ...]]:
    """A listed ``data_freshness_sla``'s ``sla_time``, ``timezone`` and
    ``day_of_week`` (lower-cased, as a sorted tuple; empty: every day)."""

    days = _argument_raw(test, "day_of_week")
    if days is None:
        listed: tuple[str, ...] = ()
    elif isinstance(days, (list, tuple)):
        listed = tuple(sorted(str(d).strip().lower() for d in days))
    else:
        listed = (str(days).strip().lower(),)
    return _argument_of(test, "sla_time"), _argument_of(test, "timezone"), listed


def _arguments_of(test: Any) -> dict[str, Any]:
    out: dict[str, Any] = {}
    monitors = _monitors_of(test)
    if monitors is not None:
        out["column_anomalies"] = monitors
    dimensions = _dimensions_of(test)
    if dimensions is not None:
        out["dimensions"] = dimensions
    for name in ("timestamp_column", "sla_time", "timezone", "where_expression"):
        value = _argument_of(test, name)
        if value is not None:
            out[name] = value
    days = _argument_raw(test, "day_of_week")
    if days is not None:
        out["day_of_week"] = (
            [str(d) for d in days] if isinstance(days, (list, tuple)) else [str(days)]
        )
    return out


def matches(spec: Any, listed: Any) -> bool:
    """Whether ``listed`` is already the monitor ``spec`` would write.

    The test's name is compared without the ``elementary.`` namespace. A
    monitor Dex wrote matches only on the same ``timestamp_column`` and the
    same ``where_expression`` (#1483), so one left on a column the declaration
    moved, or on a population it no longer declares, is stale rather than
    current. A
    hand-written ``volume_anomalies`` matches whatever its arguments, since a
    second volume monitor on one model would count its rows twice; a
    hand-written ``freshness_anomalies`` matches on the same
    ``timestamp_column``, since freshness on another column is another
    monitor. A hand-written monitor's ``where_expression`` is its author's
    choice and never compared.

    A ``dimension_anomalies`` (#1484) also matches only on the same
    ``dimensions``, compared as a set without case: a monitor Dex wrote on
    every argument, as above, and a hand-written one on its dimensions alone
    (its time column and filter are its author's). A hand-written one grouping
    by several columns at once is another monitor, since the package groups by
    their combination.

    A ``data_freshness_sla`` (#1486) Dex wrote matches only on the same
    ``sla_time``, ``timezone`` and ``day_of_week`` too, so one written from a
    deadline the declaration no longer states is stale; a hand-written one
    matches on the same ``timestamp_column``, its deadline being its author's.

    A ``column_anomalies`` (#1485, #1591) is compared against the tests its
    own column lists. One Dex wrote matches only on the same monitor list,
    compared as a set without case, as well as the same time column and
    filter, so one written from a unit or an additivity the declaration no
    longer states is stale. A hand-written one matches on the same
    ``timestamp_column``: its monitor list and filter are its author's.
    """

    if _bare(_test_name(spec)).lower() != _bare(_test_name(listed)).lower():
        return False
    ours = _timestamp_of(spec)
    theirs = _timestamp_of(listed)
    same = (ours or "").lower() == (theirs or "").lower()
    if _bare(_test_name(spec)).lower() == DIMENSION:
        if _dimension_set(spec) != _dimension_set(listed):
            return False
        if not is_monitor(listed):
            return True
    if is_monitor(listed):
        if _bare(_test_name(spec)).lower() == COLUMN and _monitor_set(
            spec
        ) != _monitor_set(listed):
            return False
        if _bare(_test_name(spec)).lower() == SLA and _deadline_of(
            spec
        ) != _deadline_of(listed):
            return False
        return same and (_where_of(spec) or "") == (_where_of(listed) or "")
    if _bare(_test_name(spec)).lower() == VOLUME:
        return True
    return same


def entry_tests(content: str | None, model: str) -> list[Any]:
    """The model-level tests ``content`` lists for ``model`` (a version label
    reads that version as dbt builds it)."""

    node = _entry(content, model)
    if not isinstance(node, Mapping):
        return []
    listed = node.get("data_tests") or node.get("tests") or []
    return list(listed) if isinstance(listed, list) else []


def column_tests(content: str | None, model: str, column: str) -> list[Any]:
    """The tests ``content`` lists under ``column`` of ``model`` (a version
    label reads that version as dbt builds it)."""

    node = _entry(content, model)
    columns = node.get("columns") if isinstance(node, Mapping) else None
    entry = next(
        (
            c
            for c in columns or []
            if isinstance(c, Mapping)
            and str(c.get("name", "")).lower() == column.lower()
        ),
        None,
    )
    if not isinstance(entry, Mapping):
        return []
    listed = entry.get("data_tests") or entry.get("tests") or []
    return list(listed) if isinstance(listed, list) else []


def _listed_column_tests(content: str | None, model: str) -> list[tuple[str, Any]]:
    """Every test ``content`` lists under a column of ``model``, with the
    column's name."""

    node = _entry(content, model)
    out: list[tuple[str, Any]] = []
    columns = node.get("columns") if isinstance(node, Mapping) else None
    for entry in columns or []:
        if not isinstance(entry, Mapping) or entry.get("name") is None:
            continue
        listed = entry.get("data_tests") or entry.get("tests") or []
        if isinstance(listed, list):
            out.extend((str(entry["name"]), test) for test in listed)
    return out


def quoted_columns(content: str | None, model: str) -> frozenset[str]:
    """The columns ``content``'s entry for ``model`` marks ``quote: true``,
    lower-cased: dbt quotes those, so the ``where_expression`` does too."""

    node = _entry(content, model)
    columns = node.get("columns") if isinstance(node, Mapping) else None
    return frozenset(
        str(c["name"]).lower()
        for c in columns or []
        if isinstance(c, Mapping)
        and c.get("name") is not None
        and c.get("quote") is True
    )


def _entry(content: str | None, model: str) -> Any:

    from .. import model_versions as mv

    if not content:
        return None
    try:
        loaded = yaml.safe_load(content)
    except yaml.YAMLError:
        return None
    base, version = mv.split_label(model)
    entries = loaded.get("models") if isinstance(loaded, Mapping) else None
    node: Any = next(
        (
            e
            for e in entries or []
            if isinstance(e, Mapping) and str(e.get("name", "")).lower() == base.lower()
        ),
        None,
    )
    if node is not None and version is not None:
        item = next(
            (
                item
                for item in node.get("versions") or []
                if isinstance(item, Mapping) and str(item.get("v")) == str(version)
            ),
            None,
        )
        node = mv.effective_entry(node, item) if item is not None else None
    return node if isinstance(node, Mapping) else None


#: Why a dimension monitor was withheld where the cache could not say whether
#: the column qualifies: a monitor the model already lists stays as listed.
_DOMAIN_UNREAD = (NOT_PROFILED, PROFILE_AMBIGUOUS)

#: Why a dimension column no longer gives its monitor at all, so one Dex wrote
#: on it earlier is stale on its ``dimensions``.
_DIMENSION_GONE = (DOMAIN_TOO_LARGE, PII_FLAGGED, COLUMN_NOT_OUTPUT)


def _domain_fields(monitor: Monitor) -> dict[str, Any]:
    """A dimension monitor's row fields (#1484): the bound, and what the cache
    said. A count is never reported for a column flagged as personal data."""

    out: dict[str, Any] = {"bound": DIMENSION_MAX_DISTINCT}
    reading = monitor.domain
    if reading is None:
        return out
    if (
        monitor.withheld != PII_FLAGGED
        and not reading.pii
        and reading.distinct_count is not None
    ):
        out["distinct_count"] = reading.distinct_count
        out["distinct_count_exact"] = reading.exact
    if reading.identifier is not None:
        out["profile"] = {
            "identifier": reading.identifier,
            "profiled_at": reading.profiled_at,
        }
    if monitor.withheld in _DOMAIN_UNREAD:
        if reading.command is not None:
            out["command"] = reading.command
        if reading.detail is not None:
            out["detail"] = reading.detail
    return out


def decide(
    content: str | None,
    declaration: Declaration,
    the_gate: Gate,
    outputs: frozenset[str] | None,
    dialect: str | None = None,
    domains: Domains | None = None,
) -> tuple[list[Monitor], list[dict[str, Any]]]:
    """The monitors to write into ``content`` for ``declaration`` (the effective
    one), and a row per monitor. ``dialect`` is the configured connector, which
    decides how a ``where_expression`` quotes and escapes (#1483). ``domains``
    reads a dimension column's domain from the exploration cache (#1484);
    without it no ``dimension_anomalies`` is planned."""

    listed = entry_tests(content, declaration.model)
    quoted = quoted_columns(content, declaration.model)
    marked = pii_marked(content, declaration.model)
    closed = the_gate.closed_reason
    to_write: list[Monitor] = []
    rows: list[dict[str, Any]] = []
    gives = planned(
        declaration, outputs, dialect, quoted, domains, marked, the_gate.release
    )
    current_specs = [m.spec() for m in gives if m.withheld is None and m.test != COLUMN]
    for monitor in gives:
        row: dict[str, Any] = {
            "model": monitor.model,
            "test": monitor.name,
            "column": monitor.column or monitor.dimension or monitor.timestamp_column,
            "arguments": dict(monitor.arguments),
            "field": monitor.field,
        }
        if monitor.test == DIMENSION:
            row.update(_domain_fields(monitor))
        if monitor.test == SLA:
            row["basis"] = monitor.basis
        if monitor.test == COLUMN:
            # A column monitor is listed under its column (#1485, #1591).
            row["sources"] = [
                {"field": source, "monitors": list(given)}
                for source, given in monitor.sources
            ]

        here = (
            column_tests(content, declaration.model, monitor.column)
            if monitor.column is not None
            else listed
        )
        if monitor.withheld is not None:
            found = (
                next((t for t in here if matches(monitor.spec(), t)), None)
                if monitor.test == DIMENSION and monitor.withheld in _DOMAIN_UNREAD
                else None
            )
            if found is not None:
                # The cache saying nothing is no reason to call a listed
                # monitor stale: it stays as listed, and the row says why
                # dex could not confirm it.
                row["status"] = ALREADY_LISTED if is_monitor(found) else HAND_WRITTEN
                row["arguments"] = dict(_arguments_of(found))
                current_specs.append(monitor.spec())
            else:
                row["status"], row["reason"] = NOT_RENDERED, monitor.withheld
            rows.append(row)
            continue
        found = next((t for t in here if matches(monitor.spec(), t)), None)
        if found is not None:
            row["status"] = ALREADY_LISTED if is_monitor(found) else HAND_WRITTEN
            if not is_monitor(found) and monitor.test in (COLUMN, SLA):
                # The author's own list stands; the row says what it holds.
                row["arguments"] = dict(_arguments_of(found))
        elif closed is not None:
            row["status"], row["reason"] = NOT_RENDERED, closed
        else:
            row["status"] = ADDED
            to_write.append(monitor)
        rows.append(row)
    for test in listed:
        if not is_monitor(test):
            continue
        if any(matches(spec, test) for spec in current_specs):
            continue
        if _bare(_test_name(test)).lower() == DIMENSION:
            rows.append(_stale_dimension(test, declaration.model, gives, rows))
            continue
        if _bare(_test_name(test)).lower() == COLUMN:
            # Listed on the model rather than a column: not one dex wrote
            # where it writes one, so it is left to its author.
            continue
        marker = marker_of(test) or {}
        # The same test on the same time column is one the population moved
        # (#1483): only its where_expression is out of date. Read from every
        # monitor the declaration gives, withheld ones included, so a filter
        # dex cannot write still reads as the population moving, not the time
        # column, and the note keeps the old monitor.
        same_time = any(
            m.timestamp_column is not None
            and m.test.lower() == _bare(_test_name(test)).lower()
            and m.timestamp_column.lower() == (_timestamp_of(test) or "").lower()
            for m in gives
        )
        # A deadline monitor on the same column whose deadline moved (#1486)
        # is stale on the deadline, whatever its filter.
        # A removed deadline is a moved one too: the column is still the
        # model's time column, so "timestamp_column" would misname it.
        moved_deadline = _bare(_test_name(test)).lower() == SLA and not any(
            m.test == SLA
            and (m.timestamp_column or "").lower()
            == (_timestamp_of(test) or "").lower()
            and _deadline_of(m.spec()) == _deadline_of(test)
            for m in gives
        )
        stale: dict[str, Any] = {
            "model": declaration.model,
            "test": _test_name(test),
            "column": _timestamp_of(test),
            "arguments": _arguments_of(test),
            "field": marker.get("from"),
            "status": STALE,
            "reason": DECLARATION_CHANGED,
            "changed": (
                "deadline"
                if moved_deadline
                else "where_expression"
                if same_time
                else "timestamp_column"
            ),
        }
        if same_time:
            stale["replacement"] = _replacement(rows, stale["test"], stale["column"])
        elif moved_deadline:
            stale["replacement"] = {"status": NOT_RENDERED, "reason": "not_declared"}
        rows.append(stale)
    for column, test in _listed_column_tests(content, declaration.model):
        if not is_monitor(test) or _bare(_test_name(test)).lower() != COLUMN:
            continue
        given = next(
            (
                m
                for m in gives
                if m.test == COLUMN and (m.column or "").lower() == column.lower()
            ),
            None,
        )
        if given is not None and given.withheld is None and matches(given.spec(), test):
            continue
        rows.append(_stale_column(test, declaration.model, column, given, rows))
    return to_write, rows


def _replacement(
    rows: list[dict[str, Any]], test: str, column: str | None
) -> dict[str, Any]:
    """Whether the monitor the declaration now gives in place of a stale one
    (same test, same column) is on the model (written by this plan, already
    listed, or hand-written), or was held back: only then is the stale one
    redundant."""

    current = next(
        (
            r
            for r in rows
            if r.get("status") != STALE
            and _bare(str(r["test"])).lower() == _bare(test).lower()
            and (r.get("column") or "").lower() == (column or "").lower()
        ),
        None,
    )
    return (
        {"status": current["status"], "reason": current.get("reason")}
        if current is not None
        else {"status": NOT_RENDERED, "reason": None}
    )


def _stale_dimension(
    test: Any, model: str, gives: list[Monitor], rows: list[dict[str, Any]]
) -> dict[str, Any]:
    """The ``stale`` row for a ``dimension_anomalies`` Dex wrote that the
    declaration no longer gives (#1484). ``changed`` is ``dimensions`` where
    its column no longer qualifies (no longer declared ``role: dimension``,
    its cached domain grew past the bound, it is now flagged as personal
    data, or the model no longer outputs it), ``timestamp_column`` where the
    model's time column moved, and ``where_expression`` where only the
    population moved."""

    marker = marker_of(test) or {}
    dimensions = _dimensions_of(test) or []
    column = dimensions[0] if len(dimensions) == 1 else None
    given = next(
        (
            m
            for m in gives
            if m.test == DIMENSION
            and column is not None
            and (m.dimension or "").lower() == column.lower()
        ),
        None,
    )
    stale: dict[str, Any] = {
        "model": model,
        "test": _test_name(test),
        "column": column,
        "arguments": _arguments_of(test),
        "field": marker.get("from"),
        "status": STALE,
        "reason": DECLARATION_CHANGED,
    }
    if given is None or given.withheld in _DIMENSION_GONE:
        stale["changed"] = "dimensions"
        stale["cause"] = "not_declared" if given is None else given.withheld
        if given is not None and given.withheld == DOMAIN_TOO_LARGE:
            stale.update(
                {
                    k: v
                    for k, v in _domain_fields(given).items()
                    if k in ("bound", "distinct_count", "distinct_count_exact")
                }
            )
    elif (given.timestamp_column or "").lower() != (_timestamp_of(test) or "").lower():
        stale["changed"] = "timestamp_column"
    else:
        stale["changed"] = "where_expression"
        stale["replacement"] = _replacement(rows, stale["test"], column)
    return stale


#: Why a column no longer gives its column monitor at all, so one Dex wrote
#: on it earlier is stale on the column itself.
_COLUMN_GONE = (PII_FLAGGED, COLUMN_NOT_OUTPUT)


def _stale_column(
    test: Any,
    model: str,
    column: str,
    given: Monitor | None,
    rows: list[dict[str, Any]],
) -> dict[str, Any]:
    """The ``stale`` row for a ``column_anomalies`` Dex wrote on ``column``
    that the declaration no longer gives (#1485, #1591). ``changed`` is
    ``column`` where the column no longer gives one (its unit and additivity
    give no monitor, it is now flagged as personal data, or the model no
    longer outputs it), ``column_anomalies`` where the monitor list moved
    (the unit or the additivity changed), ``timestamp_column`` where the
    model's time column moved, and ``where_expression`` where only the
    population moved."""

    marker = marker_of(test) or {}
    stale: dict[str, Any] = {
        "model": model,
        "test": _test_name(test),
        "column": column,
        "arguments": _arguments_of(test),
        "field": marker.get("from"),
        "status": STALE,
        "reason": DECLARATION_CHANGED,
    }
    if given is None or given.withheld in _COLUMN_GONE:
        stale["changed"] = "column"
        stale["cause"] = "not_declared" if given is None else given.withheld
        return stale
    if _monitor_set(given.spec()) != _monitor_set(test):
        stale["changed"] = "column_anomalies"
        stale["monitors"] = list(given.monitors)
    elif (given.timestamp_column or "").lower() != (_timestamp_of(test) or "").lower():
        stale["changed"] = "timestamp_column"
        return stale
    else:
        stale["changed"] = "where_expression"
    stale["replacement"] = _replacement(rows, stale["test"], column)
    return stale


#: The replacement statuses under which the current monitor is on the model.
_REPLACED = (ADDED, ALREADY_LISTED, HAND_WRITTEN)

#: How a stale dimension monitor's ``cause`` reads in its note.
_DIMENSION_CAUSE = {
    "not_declared": "the column is no longer declared role: dimension",
    DOMAIN_TOO_LARGE: "its cached domain is now larger than the bound",
    PII_FLAGGED: "the column is now flagged as personal data",
    COLUMN_NOT_OUTPUT: "the model no longer outputs the column",
}


def stale_note(row: Mapping[str, Any]) -> str:
    if row.get("changed") == "deadline":
        arguments = row.get("arguments") or {}
        replacement = row.get("replacement") or {}
        lead = (
            f"'{row['model']}' lists {row['test']} on {row.get('column')}, written "
            f"by dex with sla_time {arguments.get('sla_time')} "
            f"{arguments.get('timezone')}, a deadline the declared "
            "freshness.deadline no longer gives"
        )
        if replacement.get("status") in _REPLACED:
            return (
                f"{lead}; the current one is listed beside it and dex leaves this "
                "one in place: remove it, since the old deadline still alerts"
            )
        if replacement.get("reason") == "not_declared":
            return (
                f"{lead}: the column declares no deadline now; dex leaves it in "
                "place: remove it, or keep it as your own by removing its "
                "config.meta.dex marker"
            )
        why = replacement.get("reason") or "it was not written"
        return (
            f"{lead}; dex did not write the current one ({why}), so keep this one "
            "until it can be written, then remove it"
        )
    if row.get("changed") == "where_expression":
        replacement = row.get("replacement") or {}
        lead = (
            f"'{row['model']}' lists {row['test']} on {row.get('column')}, written "
            "by dex with a where_expression the declared population.filters no "
            "longer give"
        )
        if replacement.get("status") in _REPLACED:
            return (
                f"{lead}; the current one is listed beside it and dex leaves this "
                "one in place: remove it, since two such monitors on one model each "
                "count its rows"
            )
        listed_where = str((row.get("arguments") or {}).get("where_expression") or "")
        if replacement.get("reason") == SLA_FILTER_QUOTE and "\\'" in listed_where:
            # The listed one repeats a backslash-escaped quote in its message
            # too (#1486), so it cannot compile here: nothing is gained by
            # keeping it. One without such a quote still runs and is kept.
            return (
                f"'{row['model']}' lists {row['test']} on {row.get('column')}, "
                "written by dex, which fails to compile on this connector: the "
                "package repeats its where_expression, which holds a quote, inside "
                "its result message; remove it, since maintain verify still checks "
                "the deadline"
            )
        why = replacement.get("reason") or "it was not written"
        return (
            f"{lead}; dex did not write the current one ({why}), so keep this one "
            "until it can be written, then remove it"
        )
    if _bare(str(row.get("test", ""))).lower() == COLUMN:
        return _stale_column_note(row)
    if row.get("changed") == "dimensions":
        cause = _DIMENSION_CAUSE.get(str(row.get("cause")), "the declaration changed")
        if row.get("distinct_count") is not None:
            cause += (
                f" ({row['distinct_count']} distinct values, over "
                f"{row.get('bound', DIMENSION_MAX_DISTINCT)})"
            )
        return (
            f"'{row['model']}' lists {row['test']} on {row.get('column')}, written "
            f"by dex from {row.get('field') or 'a declaration'}, and the column no "
            f"longer qualifies: {cause}; dex leaves it in place: remove it, or keep "
            "it as your own by removing its config.meta.dex marker"
        )
    if _bare(str(row.get("test", ""))).lower() == DIMENSION:
        return (
            f"'{row['model']}' lists {row['test']} on {row.get('column')}, written "
            f"by dex with timestamp_column "
            f"{(row.get('arguments') or {}).get('timestamp_column') or 'unset'}, "
            "which the model's declared time column no longer gives; dex leaves it "
            "in place: remove it, or declare the time column again"
        )
    return (
        f"'{row['model']}' lists {row['test']} on "
        f"{row.get('column') or 'no timestamp column'}, written by dex from "
        f"{row.get('field') or 'a declaration'}, which the declaration no longer "
        "gives; dex leaves it in place: remove it, or declare the time column again"
    )


#: How a stale column monitor's ``cause`` reads in its note.
_COLUMN_CAUSE = {
    "not_declared": (
        "neither a fraction or percent unit nor a measure's declared "
        "additivity gives it one any more"
    ),
    PII_FLAGGED: "the column is now flagged as personal data",
    COLUMN_NOT_OUTPUT: "the model no longer outputs the column",
}


def _stale_column_note(row: Mapping[str, Any]) -> str:
    """The note for a stale ``column_anomalies`` (#1485, #1591)."""

    lead = (
        f"'{row['model']}' lists {row['test']} on {row.get('column')}, written by "
        f"dex from {row.get('field') or 'a declaration'}"
    )
    keep = (
        "dex leaves it in place: remove it, or keep it as your own by removing "
        "its config.meta.dex marker"
    )
    changed = row.get("changed")
    if changed == "column":
        cause = _COLUMN_CAUSE.get(str(row.get("cause")), "the declaration changed")
        return f"{lead}, and the column no longer gives one: {cause}; {keep}"
    if changed == "timestamp_column":
        return (
            f"{lead}, with timestamp_column "
            f"{(row.get('arguments') or {}).get('timestamp_column') or 'unset'}, which "
            "the model's declared time column no longer gives; dex leaves it in "
            "place: remove it, or declare the time column again"
        )
    what = (
        "a monitor list the declared unit and additivity no longer give (now "
        f"[{', '.join(row.get('monitors') or [])}])"
        if changed == "column_anomalies"
        else "a where_expression the declared population.filters no longer give"
    )
    replacement = row.get("replacement") or {}
    if replacement.get("status") in _REPLACED:
        return (
            f"{lead}, with {what}; the current one is listed beside it and dex "
            "leaves this one in place: remove it, so the column is watched once"
        )
    why = replacement.get("reason") or "it was not written"
    return (
        f"{lead}, with {what}; dex did not write the current one ({why}), so keep "
        "this one until it can be written, then remove it"
    )


def deadline_note(rows: Iterable[Mapping[str, Any]]) -> str | None:
    """One sentence for the deadlines on a column of ``business`` basis
    (#1486), or ``None``. Elementary's ``data_freshness_sla`` passes only when
    the column holds a value from today in the deadline's zone, so a load of
    the previous day's events, which a business time records, fails it every
    day; a capture time is the column a load deadline belongs on."""

    business = sorted(
        {
            f"{r['model']}.{r.get('column')}"
            for r in rows
            if _bare(str(r.get("test", ""))).lower() == SLA
            and r.get("basis") == "business"
            and r.get("status") in (ADDED, ALREADY_LISTED)
        }
    )
    if not business:
        return None
    return (
        f"{SLA} on {', '.join(business)} reads a business time: it passes only "
        "once that column holds an event from today (in the deadline's time "
        "zone), so a load of the previous day's events fails it every day; where "
        "the deadline is for the load, declare it on the time rows were loaded "
        "(basis: capture)"
    )


def sla_quote_note(rows: Iterable[Mapping[str, Any]]) -> str | None:
    """One sentence for the deadlines withheld as ``sla_filter_quote``
    (#1486), or ``None``."""

    held = sorted(
        {
            f"{r['model']}.{r.get('column')}"
            for r in rows
            if r.get("reason") == SLA_FILTER_QUOTE
        }
    )
    if not held:
        return None
    return (
        f"{SLA} was not written on {', '.join(held)}: the declared "
        "population.filters hold a value with a quote, which this connector "
        "escapes with a backslash, and Elementary repeats the where_expression "
        "inside its own result message, re-escaping each quote, so the test "
        "would fail to compile; the deadline is still checked by maintain verify"
    )


def domain_note(rows: Iterable[Mapping[str, Any]]) -> str | None:
    """One sentence naming the dimension columns left out because the cache
    could not say how many values they hold (#1484), with the command that
    would, or ``None``. Dex never profiles to decide this on its own."""

    unread = [
        r
        for r in rows
        if r.get("status") == NOT_RENDERED and r.get("reason") in _DOMAIN_UNREAD
    ]
    if not unread:
        return None
    # One clause per command, naming every column it would settle.
    grouped: dict[str, list[str]] = {}
    for row in unread:
        how = (
            f"`{row['command']}`"
            if row.get("command")
            else str(row.get("detail") or "no profile run settles it")
        )
        grouped.setdefault(how, []).append(f"{row['model']}.{row['column']}")
    parts = [f"{', '.join(columns)} ({how})" for how, columns in grouped.items()]
    return (
        "no elementary.dimension_anomalies was written for "
        f"{len(unread)} column(s) declared role: dimension, because the exploration "
        "cache holds no distinct count that says whether the domain is at most "
        f"{DIMENSION_MAX_DISTINCT} values; dex does not profile to decide this: "
        + "; ".join(parts)
        + ", then declare again"
    )


def report(the_gate: Gate, rows: list[dict[str, Any]]) -> dict[str, Any]:
    """``data.monitors``: the vendor, what gated it, and one row per monitor."""

    counts: dict[str, int] = {}
    for row in rows:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    production: dict[str, Any] = {
        "status": the_gate.production,
        "reason": the_gate.production_reason,
        "prod_target": the_gate.prod_target,
    }
    if the_gate.expression is not None:
        production["expression"] = the_gate.expression
    return {
        "vendor": the_gate.vendor,
        "package": {"status": the_gate.package, "release": the_gate.release},
        "production": production,
        "counts": counts,
        "monitors": rows,
    }


def decisions(label: str, entry: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The ``data.decisions`` entries for the monitors Dex wrote on ``entry``.

    Read from each listed test's ``config.meta.dex.monitor``, so the record
    is the YAML. An entry names the ``monitor`` and the declaration ``field``
    it came from, and carries no ``column``: it records where a monitor came
    from, not an assumption that settles a change to the model's rows.
    """

    out: list[dict[str, Any]] = []
    listed = entry.get("data_tests") or entry.get("tests") or []
    found: list[tuple[str | None, Any]] = (
        [(None, test) for test in listed] if isinstance(listed, list) else []
    )
    # A column monitor (#1485, #1591) is listed under its column.
    columns = entry.get("columns")
    for column in columns if isinstance(columns, list) else []:
        if not isinstance(column, Mapping) or column.get("name") is None:
            continue
        under = column.get("data_tests") or column.get("tests") or []
        if isinstance(under, list):
            found.extend((str(column["name"]), test) for test in under)
    for column_name, test in found:
        marker = marker_of(test)
        if marker is None:
            continue
        name = _test_name(test)
        timestamp = _timestamp_of(test)
        where = _where_of(test)
        dimensions = _dimensions_of(test)
        monitors = _monitors_of(test)
        source = marker.get("from")
        chosen = [
            f"{key}: {value}"
            for key, value in (
                ("column_name", column_name),
                (
                    "column_anomalies",
                    f"[{', '.join(monitors)}]" if monitors is not None else None,
                ),
                (
                    "dimensions",
                    f"[{', '.join(dimensions)}]" if dimensions is not None else None,
                ),
                ("timestamp_column", timestamp),
                ("sla_time", _argument_of(test, "sla_time")),
                ("timezone", _argument_of(test, "timezone")),
                (
                    "day_of_week",
                    f"[{', '.join(_arguments_of(test)['day_of_week'])}]"
                    if _argument_raw(test, "day_of_week") is not None
                    else None,
                ),
                ("where_expression", where),
            )
            if value is not None
        ]
        given = marker.get("sources")
        if isinstance(given, Mapping) and given:
            # Each field of a column monitor names the monitors it gave, so a
            # union reads back as its rows.
            fields: list[str] = [str(f) for f in given]
            sources = [
                f"{f} ({', '.join(str(m) for m in v)})"
                if isinstance(v, list) and v
                else f
                for f, v in ((str(k), given[k]) for k in given)
            ]
        else:
            fields = [source] if isinstance(source, str) else []
            sources = [source] if isinstance(source, str) else []
        if isinstance(marker.get("filter"), str):
            sources.append(marker["filter"])
        bound = marker.get("max_distinct")
        # A dimension monitor names the bound it was written under (#1484), so
        # a reviewer sees why its column was in.
        bounded = isinstance(bound, int) and not isinstance(bound, bool)
        if bounded and isinstance(source, str):
            sources[0] = (
                f"{source} (a cached domain of at most {bound} distinct values)"
            )
        out.append(
            {
                "model": label,
                "decision": f"Monitor {name}"
                + (f" on {column_name}" if column_name is not None else ""),
                "chosen": "; ".join(chosen) or "the package's defaults",
                "evidence": "contract",
                "detail": (
                    f"written from {' and '.join(sources)}"
                    + (
                        # A deadline check trains nothing (#1486).
                        ""
                        if _bare(name).lower() == SLA
                        else "; training settings are the package's defaults"
                    )
                    if sources
                    else "written by dex from the model's declaration"
                ),
                "monitor": name,
                **({"field": source} if isinstance(source, str) else {}),
                **({"fields": fields} if len(fields) > 1 else {}),
            }
        )
    return out
