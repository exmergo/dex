"""Fixture rows that reach a calendar window's boundary (#1112).

A unit test for a trailing seven-day window that feeds it three consecutive days
passes whether the window is six days, seven or eight, and whether it counts
days or rows: every row sits comfortably inside every candidate frame. The
fixture that tells them apart is not hard to write, but it is easy not to: it
needs a row on the last day the window includes, a row on the first day it
excludes, and a gap in the dates so a row-count frame cannot give the same
answer by coincidence.

This module places those rows. For a ``RANGE`` frame with an interval bound in
days or weeks, ordered by a date or timestamp column that traces back to one of
the model's inputs, the scaffold's single example row for that input becomes
four, measured back (or forward, for ``FOLLOWING``) from an anchor date ``A``
with ``N`` the bound in days:

- ``A`` itself, the current row;
- ``A - N``, the last day the window includes;
- ``A - (N + 1)``, the first day it excludes;
- ``A - 2(N + 1)``, one more full window back. Read from ``A - (N + 1)``, it is
  outside a day window and is exactly the row before it, so a ``ROWS N`` frame
  would include it. That is the gap.

The partition columns and every other column keep the scaffold's one example
value, so the rows share a partition and still match whatever the model joins.
The columns the window aggregates are numbered 1, 10, 100, 1000, so the sum the
reader writes into ``expect`` says by itself which rows each window counted.
``expect`` stays empty: dex never invents an expectation.

Anything this cannot place rows for exactly is reported as a note with a reason
code, and the scaffold keeps its single example row for that input: an ordering
expression rather than a column, an ordering column that does not trace to an
input, a unit other than days or weeks, or a non-temporal column.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from sqlglot import exp

from . import window_frames

#: The day every boundary is measured from. Fixed, so a scaffold is
#: reproducible and the reader can do the date arithmetic by eye.
ANCHOR = date(2024, 1, 31)

#: Days per unit this module places rows for. Months and years have no fixed
#: length, and sub-day units need a time-of-day the reader would have to check.
DAYS_PER_UNIT = {"day": 1, "week": 7}

#: The values the aggregated columns take, row by row, oldest first.
MEASURE_VALUES = (1, 10, 100, 1000)

REASONS = {
    "ordering_not_an_input_column": (
        "the window's ORDER BY is not one column that traces back to an input"
    ),
    "unit_not_supported_for_fixture": (
        "the frame's unit is not days or weeks, so dex places no boundary rows"
    ),
    "ordering_not_temporal": "the ORDER BY column is not a date or timestamp",
    "another_frame_on_the_same_input": (
        "an earlier window already placed this input's boundary rows"
    ),
    "numeric_range_offset": (
        "the RANGE bound is a bare number, which counts units of whatever the "
        "ORDER BY computes, so dex cannot tell which dates sit on its boundary"
    ),
    "unsupported_interval_bound": (
        "the RANGE bound is not one constant whole-number interval"
    ),
}


@dataclass
class BoundaryRows:
    """The rows placed per input, what each window was given, and what was not."""

    rows: dict[str, list[dict[str, object]]] = field(default_factory=dict)
    boundaries: list[dict[str, object]] = field(default_factory=list)
    notes: list[dict[str, str]] = field(default_factory=list)


def _unwrap_column(node: exp.Expression | None) -> exp.Column | None:
    while isinstance(node, exp.Paren):
        node = node.this
    if isinstance(node, exp.Cast):
        node = node.this
        while isinstance(node, exp.Paren):
            node = node.this
    if isinstance(node, exp.Column):
        return node
    return None


def _is_constant(node: exp.Expression | None) -> bool:
    if node is None:
        return False
    return len(list(node.find_all(exp.Column))) == 0


def _extract_whole_day_column(expr: exp.Expression | None) -> exp.Column | None:
    """If expr is a whole-day function of one date column, return that column."""
    if expr is None:
        return None
    while isinstance(expr, exp.Paren):
        expr = expr.this

    if isinstance(expr, exp.UnixDate):
        return _unwrap_column(expr.this)

    if (
        isinstance(expr, exp.Anonymous)
        and expr.name.lower() == "unix_date"
        and len(expr.expressions) == 1
    ):
        return _unwrap_column(expr.expressions[0])

    if isinstance(expr, exp.DateDiff):
        unit = expr.args.get("unit")
        unit_str = ""
        if isinstance(unit, (exp.Var, exp.Identifier)):
            unit_str = unit.name.lower()
        elif isinstance(unit, exp.Literal):
            unit_str = str(unit.this).lower()
        elif unit is not None:
            unit_str = str(unit).lower()

        if unit_str in {"day", "days"}:
            col1 = _unwrap_column(expr.this)
            col2 = _unwrap_column(expr.expression)
            if col1 is not None and _is_constant(expr.expression):
                return col1
            if col2 is not None and _is_constant(expr.this):
                return col2

    if isinstance(expr, exp.Anonymous) and expr.name.lower() in {
        "date_diff",
        "datediff",
    }:
        args = list(expr.expressions)
        if len(args) == 3:

            def is_day(u: exp.Expression) -> bool:
                if isinstance(u, (exp.Var, exp.Identifier)):
                    return u.name.lower() in {"day", "days"}
                if isinstance(u, exp.Literal):
                    return str(u.this).lower() in {"day", "days"}
                return str(u).lower() in {"day", "days"}

            dates: list[exp.Expression] = []
            if is_day(args[0]):
                dates = [args[1], args[2]]
            elif is_day(args[2]):
                dates = [args[0], args[1]]

            if dates:
                col1 = _unwrap_column(dates[0])
                col2 = _unwrap_column(dates[1])
                if col1 is not None and _is_constant(dates[1]):
                    return col1
                if col2 is not None and _is_constant(dates[0]):
                    return col2

    return None


def boundary_rows(
    tree: exp.Expression,
    inputs_by_lower: dict[str, str],
    reads: dict[str, set[str]],
    types: dict[str, dict[str, str]],
    example_value,
) -> BoundaryRows:
    """Boundary rows for every calendar window in ``tree`` that dex can reach.

    ``reads`` and ``types`` are the scaffold's own: the columns each input's
    ``given`` block carries and their cached types. ``example_value`` is the
    scaffold's per-type example, so every column not placed here keeps exactly
    the value it would have had.
    """

    out = BoundaryRows()
    for window in tree.find_all(exp.Window):
        spec = window.args.get("spec")
        if (
            not isinstance(spec, exp.WindowSpec)
            or window_frames.frame_kind(spec) != "range"
        ):
            continue
        bounds = window_frames.offsets(spec)
        offset = next(
            (
                o
                for o in bounds
                if isinstance(o, window_frames.FrameOffset) and o.is_interval
            ),
            None,
        )
        frame_text = spec.sql(dialect="duckdb")

        def note(code: str, frame_text: str = frame_text) -> None:
            out.notes.append(
                {"frame": frame_text, "reason_code": code, "reason": REASONS[code]}
            )

        order = window.args.get("order")
        keys = list(order.expressions) if order is not None else []
        order_target = (
            keys[0].this
            if len(keys) == 1 and isinstance(keys[0], exp.Ordered)
            else None
        )
        epoch_day_column = (
            _extract_whole_day_column(order_target)
            if order_target is not None
            else None
        )

        if offset is None:
            numeric_offset = next(
                (
                    o
                    for o in bounds
                    if isinstance(o, window_frames.FrameOffset) and not o.is_interval
                ),
                None,
            )
            if numeric_offset is not None and epoch_day_column is not None:
                offset = window_frames.FrameOffset(
                    spec=numeric_offset.spec,
                    bound=numeric_offset.bound,
                    kind=numeric_offset.kind,
                    side=numeric_offset.side,
                    value=numeric_offset.value,
                    unit="day",
                    unit_text="day",
                )
            else:
                # A RANGE frame with an offset dex cannot place rows around still
                # has a boundary, and a fixture of one example row cannot reach it.
                # Silence would read as "no boundary here".
                if any(isinstance(o, window_frames.FrameOffset) for o in bounds):
                    note("numeric_range_offset")
                elif bounds:
                    note("unsupported_interval_bound")
                continue

        if offset.unit not in DAYS_PER_UNIT:
            note("unit_not_supported_for_fixture")
            continue
        column = epoch_day_column if epoch_day_column is not None else order_target
        traced = (
            _trace(column, window, inputs_by_lower)
            if isinstance(column, exp.Column)
            else None
        )
        if traced is None:
            note("ordering_not_an_input_column")
            continue
        input_name, column_name = traced
        column_type = _type_of(types.get(input_name, {}), column_name)
        if column_type is None or not any(
            hint in column_type.upper() for hint in ("DATE", "TIMESTAMP")
        ):
            note("ordering_not_temporal")
            continue
        if input_name in out.rows:
            note("another_frame_on_the_same_input")
            continue

        days = offset.value * DAYS_PER_UNIT[offset.unit]
        sign = -1 if offset.side == "preceding" else 1
        placed = [
            (
                2 * (days + 1),
                "the gap: read from the first excluded day, this row is outside "
                "the day window but is the row just before it, so a row-count "
                "frame of the same size would include it",
            ),
            (days + 1, "the first day the window excludes"),
            (days, "the last day the window includes"),
            (0, "the current row the window is measured from"),
        ]
        if sign > 0:
            placed.reverse()
        measured = _aggregated_columns(window, inputs_by_lower)
        partition = {
            traced_col[1]
            for p in (window.args.get("partition_by") or [])
            if isinstance(p, exp.Column)
            and (traced_col := _trace(p, window, inputs_by_lower)) is not None
            and traced_col[0] == input_name
        }
        rows: list[dict[str, object]] = []
        described: list[dict[str, object]] = []
        for index, (distance, role) in enumerate(placed):
            day = ANCHOR + timedelta(days=sign * distance)
            row: dict[str, object] = {}
            for name in sorted(reads.get(input_name, set())):
                data_type = _type_of(types.get(input_name, {}), name) or ""
                if name.lower() == column_name.lower():
                    row[name] = _temporal(day, data_type)
                elif (
                    name.lower() in measured
                    and name not in partition
                    and _numeric(data_type)
                ):
                    row[name] = MEASURE_VALUES[index]
                else:
                    row[name] = example_value(data_type)
            rows.append(row)
            described.append(
                {
                    "date": _temporal(day, column_type),
                    "offset_days": sign * distance,
                    "tests": role,
                }
            )
        out.rows[input_name] = rows
        out.boundaries.append(
            {
                "input": input_name,
                "order_by": column_name,
                "frame": frame_text,
                "bound": offset.payload(),
                "anchor": ANCHOR.isoformat(),
                "placed_dates": described,
                "numbered_columns": sorted(
                    c for c in reads.get(input_name, set()) if c.lower() in measured
                ),
            }
        )
    return out


def _trace(
    column: exp.Column, window: exp.Expression, inputs_by_lower: dict[str, str]
) -> tuple[str, str] | None:
    """The ``(input, column)`` a window's column reads, through plain CTEs.

    Follows the column's SELECT to the one relation it can come from: an input
    directly, or a CTE whose projection of that name is the same column
    (aliased or not), recursively. Anything computed, ambiguous across joins,
    or read from a subquery answers ``None``.
    """

    select = window.find_ancestor(exp.Select)
    root = window.root()
    name = column.name
    qualifier = column.table
    seen = 0
    while select is not None and seen < 16:
        seen += 1
        sources = _sources(select)
        if qualifier:
            source = sources.get(qualifier.lower())
        elif len(sources) == 1:
            source = next(iter(sources.values()))
        else:
            return None
        if not isinstance(source, exp.Table):
            return None
        table = source.name.lower()
        if table in inputs_by_lower:
            return inputs_by_lower[table], name
        cte = next(
            (c for c in root.find_all(exp.CTE) if c.alias_or_name.lower() == table),
            None,
        )
        if cte is None or not isinstance(cte.this, exp.Select):
            return None
        select = cte.this
        projected = None
        for projection in select.expressions:
            if isinstance(projection, exp.Star) or (
                isinstance(projection, exp.Column)
                and isinstance(projection.this, exp.Star)
            ):
                projected = projected or exp.column(name)
                continue
            if (projection.alias_or_name or "").lower() != name.lower():
                continue
            inner = projection.this if isinstance(projection, exp.Alias) else projection
            projected = inner if isinstance(inner, exp.Column) else None
            break
        if projected is None:
            return None
        name, qualifier = projected.name, projected.table
    return None


def _sources(select: exp.Select) -> dict[str, exp.Expression]:
    nodes: list[exp.Expression] = []
    from_clause = select.args.get("from_") or select.args.get("from")
    if from_clause is not None:
        nodes.append(from_clause.this)
    nodes.extend(join.this for join in select.args.get("joins") or [])
    return {(node.alias_or_name or "").lower(): node for node in nodes}


def _aggregated_columns(
    window: exp.Window, inputs_by_lower: dict[str, str]
) -> set[str]:
    """The input columns the window's function reads, lower-cased."""

    found: set[str] = set()
    function = window.this
    if function is None:
        return found
    for col in function.find_all(exp.Column):
        traced = _trace(col, window, inputs_by_lower)
        if traced is not None:
            found.add(traced[1].lower())
    return found


def _type_of(types: dict[str, str], column: str) -> str | None:
    for name, data_type in types.items():
        if name.lower() == column.lower():
            return data_type
    return None


def _numeric(data_type: str) -> bool:
    upper = data_type.upper()
    return any(
        hint in upper
        for hint in ("INT", "DECIMAL", "NUMERIC", "DOUBLE", "FLOAT", "REAL", "NUMBER")
    )


def _temporal(day: date, data_type: str) -> str:
    if "TIMESTAMP" in data_type.upper():
        return datetime(day.year, day.month, day.day).strftime("%Y-%m-%d %H:%M:%S")
    return day.isoformat()
