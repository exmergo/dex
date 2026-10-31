"""The values-free result mode: exploration without a cell value in the answer.

Some hosts let a model explore a warehouse (what exists, its shape, its grain, how
tables join, how many rows match a condition) while no value read from a table may
reach the model's context: the model provider sits outside the data boundary, or
the findings are posted somewhere more people can read than can read the
warehouse. Filtering the output afterwards cannot give that guarantee, because a
value the agent has seen once can come back in its prose and no code can tell
which words of a sentence are values. So the engine that produces the result
makes the guarantee, before the agent sees anything (#591).

**Selected by the caller, never by the repository.** ``DexEngine(result_mode=
"values_free")`` pins an engine to the mode, and a call on it that asks for
``"full"`` is refused. On the CLI, ``--values-free`` selects it for one command and
the ``DEX_RESULT_MODE`` environment variable pins it for every command a host runs.
``.dex/config.yml`` is never read for it: an agent may have written that file, so a
switch there would be a switch the agent controls. Neither SQL text nor anything
else the agent supplies can select or deselect it.

**What it guarantees, in one sentence:** no value read from a table appears in a
result, a refusal, an error, a note, a warning, the cache entry a call in the mode
wrote, or the query ledger row it appended. Every result produced in the mode says
so (``data.result_mode``), so a host can assert it.

What it does, by surface:

- Commands outside :data:`ALLOWED_COMMANDS` refuse before opening a connection.
  It is an allowlist, so a command added later is refused until someone reviews
  what it returns.
- ``profile`` (and every command that profiles on the way) drops ``min_value``,
  ``max_value`` (numeric ones too: a maximum salary is a salary) and
  ``value_domain`` from the payload and from any cache entry it writes. The
  temporal counts derived from them (granularity, span in days, periods, gaps)
  stay, since they are counts.
- ``query`` accepts only statements whose every output column is an allowlisted
  aggregate over the rows it reads (see :mod:`.guards.values_free_shape`), and
  refuses any other shape without returning a cell.
- An exception reaching the caller carries a class and a stable code. A
  warehouse's own words, and any exception dex did not author, are replaced by a
  fixed message, because a driver will quote the value a cast failed on.

What it does not claim, and a host has to bound by other means (a cost ceiling, a
query count per run): a filtered count can test a guess about a value
(``count(*) where email = 'a@b.c'``), a count of one identifies a row, and an
aggregate over a single row is that row's value.
"""

from __future__ import annotations

from collections.abc import Callable
from enum import Enum
from typing import TypeVar

from .cache import Dataset
from .errors import ConfigurationError, DexError, WarehouseQueryError

T = TypeVar("T")


class ResultMode(str, Enum):
    """How much of what dex read a result may carry."""

    FULL = "full"
    VALUES_FREE = "values_free"


class ValuesFreeCode(str, Enum):
    """Why a call in the mode was refused or its error withheld.

    A code rather than prose because a host branches on it, and because the code
    is the part of an error that is guaranteed to carry no value.
    """

    # The statement shape the mode refuses (see `guards.values_free_shape`).
    BARE_COLUMN = "bare_column"
    GROUP_BY_COLUMN = "group_by_column"
    WINDOW_FUNCTION = "window_function"
    DISTINCT_PROJECTION = "distinct_projection"
    LIMIT_OFFSET = "limit_offset"
    ORDER_BY = "order_by"
    QUALIFY = "qualify"
    AGGREGATE_NOT_ALLOWED = "aggregate_not_allowed"
    FUNCTION_NOT_ALLOWED = "function_not_allowed"
    LITERAL_PROJECTION = "literal_projection"
    NO_AGGREGATE = "no_aggregate"
    UNAGGREGATED_SUBQUERY = "unaggregated_subquery"
    SET_OPERATION = "set_operation"
    # What came back did not have the shape the statement promised. Backstops
    # behind the shape rule rather than a second rule: they fire only where a
    # dialect returned something the parse could not foresee.
    NON_NUMERIC_RESULT = "non_numeric_result"
    MULTI_ROW_RESULT = "multi_row_result"
    WOULD_TRUNCATE = "would_truncate"
    # The mode itself.
    COMMAND_NOT_VALUES_FREE = "command_not_values_free"
    RESULT_MODE_PINNED = "result_mode_pinned"
    # Errors whose own words are withheld.
    WAREHOUSE_REFUSED = "warehouse_refused"
    UNEXPECTED_ERROR = "unexpected_error"


# The commands reviewed for the mode, spelled the way the CLI spells them. Every
# other command refuses in the mode before it opens anything: `explore cluster`
# (a small cluster's centroid is a row), `explore semantic values` and `query`
# (their whole output is dimension values or grouped keys), and the transform and
# maintain groups, whose payloads were never audited for values.
ALLOWED_COMMANDS = frozenset(
    {
        "connect test",
        "config get",
        "config validate",
        "explore inventory",
        "explore profile",
        "explore relationships",
        "explore map",
        "explore diagram",
        "explore query",
        "explore semantic list",
        # Package text and marker checks on instruction files; no table is read.
        "procedure",
    }
)

# Keys `.dex/config.yml` might be expected to carry and never will: the mode is the
# caller's to select, and the file is one an agent may have written. A file that
# sets one is told so by `config validate` (and on every envelope), and `config
# set` refuses to write one, so the file can never appear to select the mode.
CALLER_ONLY_KEYS = frozenset({"result_mode", "values_free"})
CALLER_ONLY_MESSAGE = (
    "is selected by the caller (--values-free, DEX_RESULT_MODE, or "
    "DexEngine(result_mode=...)) and never read from the repository, so it has "
    "no effect here"
)

# The same rule for the host's cap on the daily spend ceiling (#787), which a
# file might be expected to carry at the top level or beside the ceiling it
# caps. Keyed by dotted path, so a nested key is named as precisely as a
# top-level one.
SESSION_CEILING_CAP_MESSAGE = (
    "is set by the host (DEX_SESSION_CEILING_CAP or "
    "DexEngine(session_ceiling_cap=...)) and never read from the repository, so it "
    "has no effect here"
)
CALLER_ONLY_SETTINGS: dict[str, str] = {
    **dict.fromkeys(CALLER_ONLY_KEYS, CALLER_ONLY_MESSAGE),
    "session_ceiling_cap": SESSION_CEILING_CAP_MESSAGE,
    "budget.session_ceiling_cap": SESSION_CEILING_CAP_MESSAGE,
}


def caller_only_message(path: str) -> str | None:
    """Why the setting at dotted ``path`` cannot live in ``.dex/config.yml``, or
    None when it can. A key beneath a caller-only one is caller-only too."""

    parts = path.split(".")
    for depth in range(1, len(parts) + 1):
        message = CALLER_ONLY_SETTINGS.get(".".join(parts[:depth]))
        if message is not None:
            return message
    return None


# The column-profile fields that hold a value read from a table, and so are
# absent from a profile produced in the mode and from any cache entry it wrote.
COLUMN_VALUE_FIELDS: tuple[str, ...] = ("min_value", "max_value", "value_domain")

# The words a withheld error carries in place of whatever it said.
_WITHHELD = "detail withheld in values-free mode"

_MESSAGES: dict[ValuesFreeCode, str] = {
    ValuesFreeCode.BARE_COLUMN: (
        "a column is projected outside an aggregate, so the result would carry "
        "its values"
    ),
    ValuesFreeCode.GROUP_BY_COLUMN: (
        "GROUP BY keyed by a column returns that column's values as group keys"
    ),
    ValuesFreeCode.WINDOW_FUNCTION: (
        "a window function returns one value per row rather than one aggregate"
    ),
    ValuesFreeCode.DISTINCT_PROJECTION: (
        "SELECT DISTINCT returns rows rather than one aggregate"
    ),
    ValuesFreeCode.LIMIT_OFFSET: (
        "LIMIT, OFFSET and FETCH have nothing to bound in a one-row aggregate"
    ),
    ValuesFreeCode.ORDER_BY: ("ORDER BY has nothing to order in a one-row aggregate"),
    ValuesFreeCode.QUALIFY: "QUALIFY filters on a window function",
    ValuesFreeCode.AGGREGATE_NOT_ALLOWED: (
        "the aggregate is not on the values-free allowlist: it returns a value "
        "from a row (MIN, MAX, ANY_VALUE, ARG_MAX, MODE, a quantile) or a "
        "collection of them (ARRAY_AGG, STRING_AGG, LIST, APPROX_TOP_K)"
    ),
    ValuesFreeCode.FUNCTION_NOT_ALLOWED: (
        "the function is not on the values-free allowlist of arithmetic over aggregates"
    ),
    ValuesFreeCode.LITERAL_PROJECTION: (
        "a non-numeric literal is projected; only numbers and aggregates may be"
    ),
    ValuesFreeCode.NO_AGGREGATE: (
        "an output column contains no aggregate, so it is not computed over "
        "the rows the statement reads"
    ),
    ValuesFreeCode.UNAGGREGATED_SUBQUERY: (
        "a subquery in the select list is not itself a values-free aggregate"
    ),
    ValuesFreeCode.SET_OPERATION: (
        "UNION, INTERSECT and EXCEPT are refused in values-free mode; run each "
        "aggregate as its own statement"
    ),
    ValuesFreeCode.NON_NUMERIC_RESULT: (
        "the statement returned something other than a number, so its result "
        "was discarded"
    ),
    ValuesFreeCode.MULTI_ROW_RESULT: (
        "the statement returned more than one row, so its result was discarded"
    ),
    ValuesFreeCode.WOULD_TRUNCATE: (
        "the result would have been truncated to fit the payload budget, and "
        "values-free mode refuses rather than truncates"
    ),
    ValuesFreeCode.COMMAND_NOT_VALUES_FREE: (
        "this command is not available in values-free mode"
    ),
    ValuesFreeCode.RESULT_MODE_PINNED: (
        "this engine is pinned to values-free mode, and a call cannot ask for "
        "full results"
    ),
    ValuesFreeCode.WAREHOUSE_REFUSED: (
        f"the warehouse refused a statement dex built; {_WITHHELD}"
    ),
    ValuesFreeCode.UNEXPECTED_ERROR: (
        f"the command failed with an unexpected error; {_WITHHELD}"
    ),
}

# The envelope `reason` each code reads as (a `Reason` value, spelled out so this
# module needs nothing from the envelope). A refusal of the shape or the command is
# a policy saying no; the two withheld classes keep the bucket their original
# belonged to.
_REASONS: dict[ValuesFreeCode, str] = {
    ValuesFreeCode.WAREHOUSE_REFUSED: "execution_failure",
    ValuesFreeCode.UNEXPECTED_ERROR: "internal",
}


class ValuesFreeError(DexError):
    """A call in values-free mode was refused, or its error's words withheld.

    The message is always one of a fixed set, never built from anything a
    warehouse said, so ``str()`` of this exception is safe to hand to the model.
    ``column`` and ``function`` name what the shape rule refused (an identifier,
    never a value); ``reason`` is the envelope bucket, read by
    :func:`~.envelope.reason_for`.
    """

    def __init__(
        self,
        code: ValuesFreeCode,
        *,
        column: str | None = None,
        function: str | None = None,
        command: str | None = None,
        stage=None,
    ):
        self.code = code
        self.column = column
        self.function = function
        self.command = command
        self.stage = stage
        self.reason = _REASONS.get(code, "guard")
        super().__init__(self._message())

    def _message(self) -> str:
        text = f"values-free mode: {_MESSAGES[self.code]} ({self.code.value})"
        if self.column:
            text += f"; column: {self.column}"
        if self.function:
            text += f"; function: {self.function}"
        if self.command:
            text += f"; command: {self.command}"
        return text


def parse_result_mode(value: str | ResultMode | None) -> ResultMode | None:
    """``value`` as a mode, ``None`` left alone, anything else refused by name."""

    if value is None or isinstance(value, ResultMode):
        return value
    try:
        return ResultMode(str(value).strip().lower())
    except ValueError:
        allowed = ", ".join(m.value for m in ResultMode)
        raise ConfigurationError(
            f"unknown result mode '{value}'; expected one of: {allowed}"
        ) from None


def require_allowed(mode: ResultMode, command: str) -> None:
    """Refuse ``command`` in the mode unless it was reviewed for it."""

    if mode is ResultMode.VALUES_FREE and command not in ALLOWED_COMMANDS:
        raise ValuesFreeError(ValuesFreeCode.COMMAND_NOT_VALUES_FREE, command=command)


def withhold_values(dataset: Dataset) -> Dataset:
    """Drop every value-bearing field from ``dataset``, in place, and mark it.

    The mark is what keeps a withheld entry from ever being served to a caller
    outside the mode as though it were complete: a full profile treats a marked
    entry as stale and scans again (see ``_split_fresh_stale``).
    """

    for column in dataset.columns:
        column.min_value = None
        column.max_value = None
        column.value_domain = None
    dataset.values_withheld = True
    return dataset


def withheld_copy(dataset: Dataset) -> Dataset:
    """A deep copy of ``dataset`` with its values withheld; the original is kept."""

    return withhold_values(dataset.model_copy(deep=True))


def withhold_error(exc: BaseException) -> BaseException:
    """``exc`` as it may reach a caller in the mode.

    Three cases. A :class:`ValuesFreeError` is already safe. A refusal dex
    authored is safe too: its words are dex's own and name objects, columns,
    numbers and fixes, never a value; it keeps its type, so a host's ``except``
    clauses still match, and gains a code. Anything carrying someone else's words
    is replaced: a :class:`~.errors.WarehouseQueryError` keeps its type, the object
    it was reading and its stage but loses the server's text, and any exception
    dex did not raise on purpose (a driver error that escaped translation) becomes
    a :class:`ValuesFreeError` with no text of its own.

    The replacement is built from nothing but those fields, so it does not chain
    the original through ``__cause__``.
    """

    if isinstance(exc, ValuesFreeError):
        return exc
    if isinstance(exc, WarehouseQueryError):
        withheld = WarehouseQueryError(
            _WITHHELD,
            reading=exc.reading,
            stage=exc.stage,
            authored_by=exc.authored_by,
        )
        withheld.values_free_code = ValuesFreeCode.WAREHOUSE_REFUSED.value
        return withheld
    if isinstance(exc, DexError):
        if getattr(exc, "values_free_code", None) is None:
            exc.values_free_code = _authored_code(exc)
        return exc
    return ValuesFreeError(ValuesFreeCode.UNEXPECTED_ERROR)


def error_code(exc: BaseException) -> str | None:
    """The stable code an exception carries in the mode, if it carries one."""

    if isinstance(exc, ValuesFreeError):
        return exc.code.value
    return getattr(exc, "values_free_code", None)


def _authored_code(exc: DexError) -> str:
    # The firewall's and the guard's own codes where they have them; the class
    # name otherwise, snake-cased and without its `Error` suffix, which is stable
    # for as long as the class is.
    reason = getattr(exc, "reason", None)
    if isinstance(reason, Enum):
        return str(reason.value)
    name = type(exc).__name__.removesuffix("Error")
    return "".join(
        f"_{ch.lower()}" if ch.isupper() and i else ch.lower()
        for i, ch in enumerate(name)
    )


def call_withholding(mode: ResultMode, call: Callable[..., T], *args, **kwargs) -> T:
    """``call(*args, **kwargs)``, with anything it raises rendered by
    :func:`withhold_error` when ``mode`` is values-free.

    A function rather than a context manager on purpose: a replacement raised
    from inside a ``with`` block's exit is raised while the original is still
    being handled, so Python attaches the original as its ``__context__`` and
    every traceback prints the words the replacement exists to drop. Raised here,
    after the ``except`` clause has finished, it carries nothing.
    """

    if mode is not ResultMode.VALUES_FREE:
        return call(*args, **kwargs)
    try:
        return call(*args, **kwargs)
    except Exception as exc:  # every exception is re-raised, rendered or as is
        replacement = withhold_error(exc)
        if replacement is exc:
            raise
    raise replacement
