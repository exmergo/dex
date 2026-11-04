"""Reading the offset bounds of a window frame, for the mutator and the scaffold.

A window frame's boundary is where its off-by-one defects live: ``27 PRECEDING``
written for a 28-day window, or a 28-day window that should have been 27. The
mutator shifts a bound to plant that defect, and the unit-test scaffold places
fixture rows on either side of it, so both need the same answer to one question:
is this bound a constant offset dex can read exactly, and if not, why not.

Two spellings are read. A bare integer (``6 PRECEDING``) counts rows in a
``ROWS`` frame and units of the ordering value in a ``RANGE`` frame. An interval
with one integer quantity and one unit (``INTERVAL '6 day' PRECEDING``,
``INTERVAL 6 DAYS``) is a calendar span over a temporal ordering. Every other
offset (a column, a function, a fraction, a compound interval such as
``'1 day 2 hours'``, a cast) is reported as :class:`Unsupported` with a stable
reason code, so a caller can say the bound exists and was not tested rather than
saying nothing and implying that it was. ``UNBOUNDED`` and ``CURRENT ROW`` are
not offsets at all: there is no boundary to move, and :func:`read_bound` returns
``None`` for them.

Pure: nothing here runs SQL. A bound is replaced on whatever tree the caller
hands in, so the caller decides whether that is a fresh copy.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlglot import expressions as exp

#: The units an interval quantity may carry, by every spelling sqlglot keeps,
#: mapped to the singular a sentence uses. A unit outside this map is a
#: dialect-specific spelling dex has not reviewed, so it is not shifted.
UNITS = {
    "SECOND": "second",
    "SECONDS": "second",
    "MINUTE": "minute",
    "MINUTES": "minute",
    "HOUR": "hour",
    "HOURS": "hour",
    "DAY": "day",
    "DAYS": "day",
    "WEEK": "week",
    "WEEKS": "week",
    "MONTH": "month",
    "MONTHS": "month",
    "QUARTER": "quarter",
    "QUARTERS": "quarter",
    "YEAR": "year",
    "YEARS": "year",
}

#: Why an offset bound is not one dex can shift or place rows around.
REASONS = {
    "non_literal_offset": "the offset is a column or an expression, not a constant",
    "fractional_offset": "the offset is not a whole number",
    "negative_offset": "the offset is negative",
    "string_offset": "the offset is a quoted string rather than a number",
    "compound_interval": "the interval carries more than one unit",
    "unknown_unit": "the interval's unit is not one dex has reviewed",
}

BOUNDS = ("start", "end")


@dataclass(frozen=True)
class FrameOffset:
    """One constant offset bound of a frame, read exactly.

    ``unit`` is ``None`` for a bare number. ``unit_text`` is the unit exactly as
    the SQL spelled it (``DAYS`` stays plural), so a rewritten bound changes the
    quantity and nothing else.
    """

    spec: exp.WindowSpec
    bound: str
    kind: str
    side: str
    value: int
    unit: str | None = None
    unit_text: str | None = None

    @property
    def is_interval(self) -> bool:
        return self.unit is not None

    def payload(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "bound": self.bound,
            "side": self.side,
            "value": self.value,
            "unit": self.unit,
        }


@dataclass(frozen=True)
class Unsupported:
    """An offset bound dex will not shift, and the code saying why."""

    spec: exp.WindowSpec
    bound: str
    side: str
    reason_code: str

    @property
    def reason(self) -> str:
        return REASONS[self.reason_code]


def frame_kind(spec: exp.WindowSpec) -> str:
    return str(spec.args.get("kind") or "").lower() or "rows"


def read_bound(spec: exp.WindowSpec, bound: str) -> FrameOffset | Unsupported | None:
    """The offset at ``bound`` (``start`` or ``end``), or why it cannot be read.

    ``None`` means there is no offset there: no bound, ``UNBOUNDED`` or
    ``CURRENT ROW``. Those have no boundary to shift, so they are neither a site
    nor an unsupported one.
    """

    node = spec.args.get(bound)
    if node is None or isinstance(node, str):
        return None
    if isinstance(node, (exp.Var, exp.Identifier)) or (
        isinstance(node, exp.Column) and node.name.upper() in {"UNBOUNDED", "CURRENT"}
    ):
        return None
    side = str(spec.args.get(f"{bound}_side") or "").lower()
    kind = frame_kind(spec)

    def unsupported(code: str) -> Unsupported:
        return Unsupported(spec=spec, bound=bound, side=side, reason_code=code)

    if isinstance(node, exp.Literal):
        if node.args.get("is_string"):
            return unsupported("string_offset")
        value = _whole(node.this)
        if value is None:
            return unsupported("fractional_offset")
        if value < 0:
            return unsupported("negative_offset")
        return FrameOffset(spec=spec, bound=bound, kind=kind, side=side, value=value)

    if isinstance(node, exp.Interval):
        unit = node.args.get("unit")
        if unit is None:
            return unsupported("compound_interval")
        unit_text = unit.name if isinstance(unit, (exp.Var, exp.Identifier)) else None
        if unit_text is None or unit_text.upper() not in UNITS:
            return unsupported("unknown_unit")
        quantity = node.this
        while isinstance(quantity, exp.Paren):
            quantity = quantity.this
        if not isinstance(quantity, exp.Literal):
            return unsupported("non_literal_offset")
        value = _whole(quantity.this)
        if value is None:
            return unsupported("fractional_offset")
        if value < 0:
            return unsupported("negative_offset")
        return FrameOffset(
            spec=spec,
            bound=bound,
            kind=kind,
            side=side,
            value=value,
            unit=UNITS[unit_text.upper()],
            unit_text=unit_text,
        )

    return unsupported("non_literal_offset")


def offsets(spec: exp.WindowSpec) -> list[FrameOffset | Unsupported]:
    """Every offset bound of one frame, start first."""

    found = (read_bound(spec, bound) for bound in BOUNDS)
    return [entry for entry in found if entry is not None]


def with_value(offset: FrameOffset, value: int) -> None:
    """Rewrite ``offset``'s bound in place to ``value``, keeping its spelling.

    Only the quantity changes: an interval keeps its unit as written and its
    quoting, the side (``PRECEDING``/``FOLLOWING``) and everything else in the
    frame stay as they were.
    """

    node = offset.spec.args[offset.bound]
    if not offset.is_interval:
        offset.spec.set(offset.bound, exp.Literal.number(value))
        return
    quantity = node.this
    while isinstance(quantity, exp.Paren):
        quantity = quantity.this
    replacement = (
        exp.Literal.string(str(value))
        if quantity.args.get("is_string")
        else exp.Literal.number(value)
    )
    quantity.replace(replacement)


def plural(unit: str | None, value: int) -> str:
    """``6 days``, ``1 day``, ``6 rows`` for a bare ROWS bound."""

    word = unit or "row"
    return f"{value} {word}" if value == 1 else f"{value} {word}s"


def _whole(text: object) -> int | None:
    try:
        number = float(str(text).strip())
    except (TypeError, ValueError):
        return None
    if not number.is_integer():
        return None
    return int(number)
