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
# letter and does not close it. A value can hold a line break (#1412), so the
# close may sit on a later line.
_OPEN = rf"{_NOT_MASKED}(?<!\w)"
_CLOSE = r"(?=$|[\s,;:.)\]])"
# After a phrase that names a value, the quote that opens it opens a value, so
# the close is looked for to the end of the message, and a value that never
# closes is masked to the end: a value too long to close fails closed.
_QUOTED = rf"{_OPEN}(?:(?s:'.*?'|\".*?\"){_CLOSE}|(?s:['\"].*)\Z)"
# Anywhere else a quote may be stray, so its close is looked for within
# _SPAN_MAX characters, on its line or a later one; unbounded, every stray quote
# in a long message would scan to its end.
_SPAN_MAX = 2000
_INSIDE = rf"(?s:.{{0,{_SPAN_MAX}}}?)"
_QUOTED_NEAR = rf"{_OPEN}(?:'{_INSIDE}'|\"{_INSIDE}\"){_CLOSE}"
# An unquoted value runs to the end of its clause.
_TO_CLAUSE_END = rf"{_NOT_MASKED}[^;\n]+"

# Each pattern has a ``value`` group; everything around it is kept. The whole
# message is matched at once, so ``$`` is the end of a line (MULTILINE) and only
# a quoted value crosses one.
_FLAGS = re.IGNORECASE | re.MULTILINE
# A shape whose phrase follows the value opens on the quote, so it would be
# tried at every quote in the message: it is listed with its phrase and runs
# only when the message holds that phrase.
_SHAPES: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (phrase, re.compile(p, _FLAGS))
    for phrase, p in (
        shape if isinstance(shape, tuple) else ("", shape)
        for shape in (
            # DuckDB: `Could not convert string 'x' to INT32`
            rf"could not convert string (?P<value>{_QUOTED})",
            # DuckDB JSON: `Failed to cast value to numerical: {"k":"x"}`; the value is
            # printed unquoted, so it is masked whole, up to the `when casting from
            # source column <name>` clause that names the column (kept) or the end.
            # The clause counts only where it ends the line with one name, so a
            # value that itself contains the phrase is still masked whole.
            rf"failed to cast value to [\w ()]+?: (?P<value>{_NOT_MASKED}.+?)"
            r"(?=\s+when casting from source column [^\s\"',{}\[\]]+$|$)",
            # DuckDB: `invalid date field format: "x", expected format is ...`
            rf"field format: (?P<value>{_QUOTED})",
            # DuckDB: `Type INT32 with value 300 can't be cast ...`
            rf"\bwith value (?P<value>{_QUOTED}|{_NOT_MASKED}[^\s,;]+)",
            # DuckDB JSON: `Malformed JSON ... Input: "x"`. DuckDB prints the input
            # in double quotes without escaping, so a quote inside JSON does not end
            # it: it runs, over line breaks too, to DuckDB's own words after it
            # (the column clause, the LINE block) or to the end.
            rf"(?-i:\bInput): (?P<value>{_NOT_MASKED}(?s:.+?))"
            r"(?= when casting from source column [^\s\"',{}\[\]]+$|\n\nLINE \d+:|\Z)",
            # Postgres / Redshift: `invalid input syntax for type integer: "x"`
            rf"invalid input syntax for [^:\n]*: (?P<value>{_QUOTED})",
            # Postgres: `invalid input value for enum mood: "x"`
            rf"invalid input value for [^:\n]*: (?P<value>{_QUOTED})",
            # Postgres: `value "x" is out of range for type integer`
            (
                " is out of range",
                rf"\bvalue (?P<value>{_QUOTED_NEAR}) is out of range",
            ),
            # Postgres: `date/time field value out of range: "x"`
            rf"out of range: (?P<value>{_QUOTED})",
            # Snowflake: `Numeric value 'x' is not recognized`. Before `\bValue`,
            # whose value would end at a quote inside it, before a line break.
            (" is not recognized", rf"(?P<value>{_QUOTED_NEAR}) is not recognized"),
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
            # Snowflake: `Can't parse 'x' as timestamp`
            rf"can't parse (?P<value>{_QUOTED})",
            # Databricks: `[CAST_INVALID_INPUT] The value 'x' of the type "STRING" ...`
            rf"\bThe value (?P<value>{_QUOTED})",
            # ClickHouse: `Cannot parse string 'x' as Int32`; `... before: 'x'`
            rf"cannot parse string (?P<value>{_QUOTED})",
            rf"\bbefore: (?P<value>{_QUOTED}|{_TO_CLAUSE_END})",
            # Trino: `Cannot cast 'x' to INT`, `Cannot cast VARCHAR 'x' to INTEGER`
            rf"cannot cast (?:\w+ )?(?P<value>{_QUOTED})",
            # Trino: `Value cannot be cast to date: x`; `Invalid format: "x"`
            rf"value cannot be cast to [^:\n]+: (?P<value>{_TO_CLAUSE_END})",
            rf"invalid format: (?P<value>{_QUOTED})",
        )
    )
)

# Words that mark a message as a conversion, parse or range failure, the family
# that quotes its input. A quoted segment that opens on a line carrying one is
# masked.
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
_ANY_QUOTED = re.compile(_QUOTED_NEAR)
_OPENING = re.compile(r"(?<!\w)['\"]")
# A quote that could close a value of its kind; a cheap scan for one tells when
# the bounded match is bound to fail.
_CLOSING = {q: re.compile(q + _CLOSE, re.MULTILINE) for q in "'\""}


def _mask_group(match: re.Match[str]) -> str:
    start, end = match.span("value")
    whole_start = match.start()
    text = match.group(0)
    return text[: start - whole_start] + VALUE_WITHHELD + text[end - whole_start :]


def _mask_on_conversion_lines(text: str) -> str:
    """``text`` with each quoted segment that opens on a conversion line masked.

    Only the lines that carry a marker are searched for an opening quote, and
    each opening looks at most _SPAN_MAX characters ahead, so the cost is linear
    in the message. A segment that closes on a later line joins that line to the
    marker line, so the search goes on to the end of the joined line, as a
    second call would; that keeps the mask idempotent. An opening that does not
    close within the bound is masked to the end of the line: a value too long to
    find its close fails closed.
    """

    out: list[str] = []
    done = 0
    searched = 0  # the end of the last marker line, joined lines included
    for marker in _CONVERSION_MARKER.finditer(text):
        if marker.start() < searched:
            continue  # this line was searched already
        line_end = text.find("\n", marker.end())
        if line_end < 0:
            line_end = len(text)
        pos = max(text.rfind("\n", 0, marker.start()) + 1, done)
        while (opening := _OPENING.search(text, pos, line_end)) is not None:
            start = opening.start()
            # No quote in reach that could close it: the bounded match would fail.
            closing = _CLOSING[text[start]]
            if closing.search(text, start + 1, start + _SPAN_MAX + 2) is None:
                quoted = None
            else:
                quoted = _ANY_QUOTED.match(text, start)
            if quoted is None:
                out += [text[done : opening.start()], VALUE_WITHHELD]
                done = line_end
                break
            out += [text[done : quoted.start()], VALUE_WITHHELD]
            done = pos = quoted.end()
            if done > line_end:
                line_end = text.find("\n", done)
                if line_end < 0:
                    line_end = len(text)
        searched = line_end
    return "".join(out) + text[done:]


def mask_values(text: str) -> str:
    """``text`` with any data value a conversion error quotes replaced.

    Idempotent, and a message that is not a conversion failure is returned
    unchanged, so it is safe to apply to any warehouse or dbt message. Apply it
    to the whole message, before any split or cut: a value can span a line
    break, and no single line holds it whole (#1412).
    """

    if not text:
        return text
    lowered = text.lower()
    for phrase, shape in _SHAPES:
        if phrase in lowered:
            text = shape.sub(_mask_group, text)
    return _mask_on_conversion_lines(text)
