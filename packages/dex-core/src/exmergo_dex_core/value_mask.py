"""Mask the data value a warehouse error quotes, and keep the rest of its words.

A warehouse refuses a statement in its own words, and those words are what make a
failure diagnosable: the column it could not find, the type it expected, the
object that is gone. One family of refusals also quotes a value: a conversion,
parse or range failure names the input it could not handle (DuckDB's
``Could not convert string 'org_cedar' to INT32``, BigQuery's
``Bad int64 value: org_cedar``). That input comes from a row, possibly from a
PII-flagged column, and an error is not a result column, so neither the query
firewall nor values-free mode's result shape ever sees it (#1070).

:func:`mask_values` replaces that value with :data:`VALUE_WITHHELD` and leaves
every other word, so ``Could not convert string <value withheld> to INT32`` still
says what failed and what type was expected. It is applied where every adapter
builds its refusal (``adapters.base.warehouse_refusal``) and by
``envelope.failure_text`` wherever a caught failure is folded into a note, a
warning or a record.

It is a catalogue of the shapes each connector's conversion errors take, not a
proof that no other message can carry a value: a shape not listed here passes
through, and finding one is a bug to fix by adding it. Two layers keep that gap
small. The explicit shapes come first, each masking exactly the value it names,
quoted or not. Then any message that still reads as a conversion failure has
every remaining quoted segment masked, which over-masks an occasional quoted type
name rather than letting an unlisted shape through.

This module imports nothing from the engine, so the adapters and ``errors`` can
use it from any tier.
"""

from __future__ import annotations

import re

VALUE_WITHHELD = "<value withheld>"

# Never re-mask the placeholder, so masking is idempotent.
_NOT_MASKED = rf"(?!{re.escape(VALUE_WITHHELD)})"
# A quoted value opens at a quote no word character precedes (so the apostrophe
# in `Can't` opens nothing) and closes at the first matching quote that a
# delimiter or the end follows. Warehouses print the value as stored, without
# escaping, so `'O'Brien'` is one value: its inner quote is followed by a
# letter and does not close it.
_QUOTED = rf"{_NOT_MASKED}(?<!\w)(?:'[^\n]*?'|\"[^\n]*?\")(?=$|[\s,;:.)\]])"
# An unquoted value runs to the end of its clause.
_TO_CLAUSE_END = rf"{_NOT_MASKED}[^;\n]+"

# Each pattern has a ``value`` group; everything around it is kept.
_SHAPES: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        # DuckDB: `Could not convert string 'x' to INT32`
        rf"could not convert string (?P<value>{_QUOTED})",
        # DuckDB: `invalid date field format: "x", expected format is ...`
        rf"field format: (?P<value>{_QUOTED})",
        # DuckDB: `Type INT32 with value 300 can't be cast ...`
        rf"\bwith value (?P<value>{_QUOTED}|{_NOT_MASKED}[^\s,;]+)",
        # DuckDB JSON: `Malformed JSON ... Input: "x"` (the input runs to the end)
        rf"(?-i:\bInput): (?P<value>{_NOT_MASKED}.+)$",
        # Postgres / Redshift: `invalid input syntax for type integer: "x"`
        rf"invalid input syntax for [^:]*: (?P<value>{_QUOTED})",
        # Postgres: `invalid input value for enum mood: "x"`
        rf"invalid input value for [^:]*: (?P<value>{_QUOTED})",
        # Postgres: `value "x" is out of range for type integer`
        rf"\bvalue (?P<value>{_QUOTED}) is out of range",
        # Postgres: `date/time field value out of range: "x"`
        rf"out of range: (?P<value>{_QUOTED})",
        # Redshift: `Invalid digit, Value 'p', Pos 0`;
        # ClickHouse: `Value 'x' cannot be parsed`
        rf"\bValue (?P<value>{_QUOTED})",
        # BigQuery: `Bad int64 value: x` (quoted or not)
        rf"\bBad [\w ]+? value: (?P<value>{_QUOTED}|{_TO_CLAUSE_END})",
        # BigQuery: `Could not cast literal "x" to type DATE`
        rf"could not cast literal (?P<value>{_QUOTED})",
        # BigQuery: `Invalid date: 'x'`, `Invalid timestamp value: x`
        rf"\bInvalid (?:date|datetime|time|timestamp|numeric|bignumeric|interval)"
        rf"(?: value)?: (?P<value>{_QUOTED}|{_TO_CLAUSE_END})",
        # BigQuery PARSE_*: `Failed to parse input string "x"`,
        # `Could not parse 'x' as ...`
        rf"failed to parse input string (?P<value>{_QUOTED})",
        rf"could not parse (?P<value>{_QUOTED})",
        # Snowflake: `Numeric value 'x' is not recognized`,
        # `Can't parse 'x' as timestamp`
        rf"(?P<value>{_QUOTED}) is not recognized",
        rf"can't parse (?P<value>{_QUOTED})",
        # Databricks: `[CAST_INVALID_INPUT] The value 'x' of the type "STRING" ...`
        rf"\bThe value (?P<value>{_QUOTED})",
        # ClickHouse: `Cannot parse string 'x' as Int32`; `... before: 'x'`
        rf"cannot parse string (?P<value>{_QUOTED})",
        rf"\bbefore: (?P<value>{_QUOTED}|{_TO_CLAUSE_END})",
        # Trino: `Cannot cast 'x' to INT`, `Cannot cast VARCHAR 'x' to INTEGER`
        rf"cannot cast (?:\w+ )?(?P<value>{_QUOTED})",
        # Trino: `Value cannot be cast to date: x`; `Invalid format: "x"`
        rf"value cannot be cast to [^:]+: (?P<value>{_TO_CLAUSE_END})",
        rf"invalid format: (?P<value>{_QUOTED})",
    )
)

# Words that mark a message as a conversion, parse or range failure, the family
# that quotes its input. A message carrying one has every remaining quoted
# segment masked.
# Only phrases a value-quoting failure uses: a bare `parse` or `cast` also
# appears in dbt's `Parsing Error` and in a binder's "explicit cast required",
# whose quoted names are identifiers, not values.
_CONVERSION_MARKER = re.compile(
    r"conversion error|could not convert|(?:could not|cannot|can't|unable to) "
    r"cast|cannot be cast|(?:could not|cannot|can't|failed to|unable to) parse|"
    r"not recognized|invalid input|malformed|out of range|\bbad [\w ]+? value|"
    r"invalid (?:digit|date|datetime|time|timestamp|numeric|boolean|format)",
    re.IGNORECASE,
)
_ANY_QUOTED = re.compile(_QUOTED)


def _mask_group(match: re.Match[str]) -> str:
    start, end = match.span("value")
    whole_start = match.start()
    text = match.group(0)
    return text[: start - whole_start] + VALUE_WITHHELD + text[end - whole_start :]


def mask_values(text: str) -> str:
    """``text`` with any data value a conversion error quotes replaced.

    Idempotent, and a message that is not a conversion failure is returned
    unchanged, so it is safe to apply to any warehouse or dbt message.
    """

    if not text:
        return text
    lines = text.split("\n")
    masked: list[str] = []
    for line in lines:
        for shape in _SHAPES:
            line = shape.sub(_mask_group, line)
        if _CONVERSION_MARKER.search(line):
            line = _ANY_QUOTED.sub(VALUE_WITHHELD, line)
        masked.append(line)
    return "\n".join(masked)
