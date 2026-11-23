"""Vendor monitors written from a model's declaration (#1444, #1482).

A declaration already says which column is a model's time. An observability
vendor's monitors need the same fact, and a project that writes it twice, by
hand, in two places, lets the two drift. With ``monitors.vendor`` set in
``.dex/config.yml``, a plan that renders a declaration also writes the
vendor's own tests into the same YAML entry, in the same plan and diff, parsed
by dbt before the plan is stored.

One vendor today, the Elementary dbt package, and one mapping row: a column
declared with role ``time`` becomes the ``timestamp_column`` of
``elementary.volume_anomalies`` and ``elementary.freshness_anomalies``.
``freshness_anomalies`` cannot run without one (the package lists it under
``mandatory_params``), and ``volume_anomalies`` without one counts the whole
table once per run, trained only on run history, which is a different monitor
from the one a time column gives, so neither is written without it.

What this module never does:

* write a monitor where it will not run. The package has to be installed (or
  added by the same plan), and the repository's ``dbt_project.yml`` has to show
  its models enabled on the production target, read with ``target.name``
  replaced by the committed ``dbt_prod_target`` (the #1481 decision). Disabled
  there, or not readable, and nothing is written; the report says which;
* choose a statistic. Sensitivity, ``days_back``, seasonality and every other
  training setting stay the package's defaults;
* touch a monitor the project wrote by hand. A listed test of the same name
  (and, for a freshness monitor, the same ``timestamp_column``) is reported
  ``hand_written`` and left exactly as written;
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

from ..semantic_declarations import Declaration, Role
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


@dataclass
class Monitor:
    """One monitor a declaration gives, written or not."""

    model: str
    test: str
    timestamp_column: str | None
    field: str | None
    withheld: str | None = None
    arguments: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.arguments and self.timestamp_column is not None:
            self.arguments = {"timestamp_column": self.timestamp_column}

    @property
    def name(self) -> str:
        return f"{ELEMENTARY}.{self.test}"

    def spec(self) -> dict[str, Any]:
        """The test as it renders under the model's entry."""

        from .rewrite import _unsampled

        [(_, rendered)] = _unsampled(self.name).items()
        meta = dict(rendered["config"]["meta"])
        meta["dex"] = {MARKER: {"vendor": ELEMENTARY, "from": self.field}}
        return {
            self.name: {
                "arguments": dict(self.arguments),
                "config": {"tags": [TAG], "meta": meta},
            }
        }


def planned(declaration: Declaration, outputs: frozenset[str] | None) -> list[Monitor]:
    """Every monitor ``declaration`` (as the entry states it) gives.

    One time column gives a ``volume_anomalies`` and a
    ``freshness_anomalies`` on it. Several give a ``freshness_anomalies`` on
    each, and a ``volume_anomalies`` withheld as ``ambiguous_time_column``,
    since it takes one. None gives both withheld as ``no_time_column``. A time
    column the model's SQL provably does not output is ``column_not_output``.
    """

    model = declaration.model
    times = [
        name for name, column in declaration.columns.items() if column.role is Role.TIME
    ]
    if not times:
        return [
            Monitor(model, test, None, None, withheld=NO_TIME_COLUMN)
            for test in (VOLUME, FRESHNESS)
        ]

    def field_of(column: str) -> str:
        return f"columns.{column}.role: time"

    def withheld(column: str) -> str | None:
        if outputs is not None and column.lower() not in outputs:
            return COLUMN_NOT_OUTPUT
        return None

    out: list[Monitor] = []
    if len(times) == 1:
        out.append(
            Monitor(model, VOLUME, times[0], field_of(times[0]), withheld(times[0]))
        )
    else:
        out.append(Monitor(model, VOLUME, None, None, withheld=AMBIGUOUS_TIME_COLUMN))
    out.extend(
        Monitor(model, FRESHNESS, column, field_of(column), withheld(column))
        for column in times
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


def _timestamp_of(test: Any) -> str | None:
    if not isinstance(test, Mapping) or len(test) != 1:
        return None
    body = next(iter(test.values()))
    if not isinstance(body, Mapping):
        return None
    nested = body.get("arguments")
    value = (
        nested.get("timestamp_column")
        if isinstance(nested, Mapping) and "timestamp_column" in nested
        else body.get("timestamp_column")
    )
    return str(value) if value is not None else None


def matches(spec: Any, listed: Any) -> bool:
    """Whether ``listed`` is already the monitor ``spec`` would write.

    The test's name is compared without the ``elementary.`` namespace. A
    monitor Dex wrote matches only on the same ``timestamp_column``, so one
    left on a column the declaration moved is stale rather than current. A
    hand-written ``volume_anomalies`` matches whatever its arguments, since a
    second volume monitor on one model would count its rows twice; a
    hand-written ``freshness_anomalies`` matches on the same
    ``timestamp_column``, since freshness on another column is another
    monitor.
    """

    if _bare(_test_name(spec)).lower() != _bare(_test_name(listed)).lower():
        return False
    ours = _timestamp_of(spec)
    theirs = _timestamp_of(listed)
    same = (ours or "").lower() == (theirs or "").lower()
    if is_monitor(listed):
        return same
    if _bare(_test_name(spec)).lower() == VOLUME:
        return True
    return same


def entry_tests(content: str | None, model: str) -> list[Any]:
    """The model-level tests ``content`` lists for ``model`` (a version label
    reads that version as dbt builds it)."""

    from .. import model_versions as mv

    if not content:
        return []
    try:
        loaded = yaml.safe_load(content)
    except yaml.YAMLError:
        return []
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
    if not isinstance(node, Mapping):
        return []
    listed = node.get("data_tests") or node.get("tests") or []
    return list(listed) if isinstance(listed, list) else []


def decide(
    content: str | None,
    declaration: Declaration,
    the_gate: Gate,
    outputs: frozenset[str] | None,
) -> tuple[list[Monitor], list[dict[str, Any]]]:
    """The monitors to write into ``content`` for ``declaration`` (the effective
    one), and a provisional row per monitor; :func:`finish` settles ``added``
    against the rendered text."""

    listed = entry_tests(content, declaration.model)
    closed = the_gate.closed_reason
    to_write: list[Monitor] = []
    rows: list[dict[str, Any]] = []
    for monitor in planned(declaration, outputs):
        row: dict[str, Any] = {
            "model": monitor.model,
            "test": monitor.name,
            "column": monitor.timestamp_column,
            "arguments": dict(monitor.arguments),
            "field": monitor.field,
        }
        if monitor.withheld is not None:
            row["status"], row["reason"] = NOT_RENDERED, monitor.withheld
            rows.append(row)
            continue
        found = next((t for t in listed if matches(monitor.spec(), t)), None)
        if found is not None:
            row["status"] = ALREADY_LISTED if is_monitor(found) else HAND_WRITTEN
        elif closed is not None:
            row["status"], row["reason"] = NOT_RENDERED, closed
        else:
            row["status"] = ADDED
            to_write.append(monitor)
        rows.append(row)
    planned_specs = [
        m.spec() for m in planned(declaration, outputs) if m.withheld is None
    ]
    for test in listed:
        if not is_monitor(test):
            continue
        if any(matches(spec, test) for spec in planned_specs):
            continue
        marker = marker_of(test) or {}
        rows.append(
            {
                "model": declaration.model,
                "test": _test_name(test),
                "column": _timestamp_of(test),
                "arguments": (
                    {"timestamp_column": _timestamp_of(test)}
                    if _timestamp_of(test) is not None
                    else {}
                ),
                "field": marker.get("from"),
                "status": STALE,
                "reason": DECLARATION_CHANGED,
            }
        )
    return to_write, rows


def stale_note(row: Mapping[str, Any]) -> str:
    return (
        f"'{row['model']}' lists {row['test']} on "
        f"{row.get('column') or 'no timestamp column'}, written by dex from "
        f"{row.get('field') or 'a declaration'}, which the declaration no longer "
        "gives; dex leaves it in place: remove it, or declare the time column again"
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
    if not isinstance(listed, list):
        return out
    for test in listed:
        marker = marker_of(test)
        if marker is None:
            continue
        name = _test_name(test)
        timestamp = _timestamp_of(test)
        source = marker.get("from")
        out.append(
            {
                "model": label,
                "decision": f"Monitor {name}",
                "chosen": (
                    f"timestamp_column: {timestamp}"
                    if timestamp is not None
                    else "the package's defaults"
                ),
                "evidence": "contract",
                "detail": (
                    f"written from {source}; training settings are the "
                    "package's defaults"
                    if source
                    else "written by dex from the model's declaration"
                ),
                "monitor": name,
                **({"field": source} if isinstance(source, str) else {}),
            }
        )
    return out
