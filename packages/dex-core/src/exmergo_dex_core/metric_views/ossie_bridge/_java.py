# Ported from apache/ossie-converters, databricks/java at 82006ba2.
# Licensed under the Apache License, Version 2.0.
"""The few Java behaviours the converter's output depends on, restated in Python.

The converter is Java, and some of what it emits follows from how Java treats a
string, a number or a set rather than from anything the converter says. Each
helper here restates one of those, so the port gives the converter's answer
rather than Python's:

- ``trim`` strips code units up to U+0020, not Unicode whitespace;
- ``Character.isWhitespace``, ``isLetter`` and ``isDigit`` (the Nd category);
  a code point above U+FFFF is two surrogate units in Java, neither a letter
  nor a digit;
- ``Object.toString`` of a parsed YAML value, which error messages quote;
- ``Double.toString``, which the stash JSON writes;
- ``HashSet`` iteration order, which decides which dropped name a cascade
  notice cites when a column reads two of them;
- the stash JSON writer: Jackson with ``ESCAPE_NON_ASCII``, ``": "`` and
  ``", "`` separators, and lowercase ``\\u`` escapes.
"""

from __future__ import annotations

import datetime as _dt
import math
import unicodedata
from collections.abc import Iterable, Iterator
from typing import Any

__all__ = [
    "JavaHashSet",
    "dumps_stash",
    "get_simple_name",
    "is_digit",
    "is_letter",
    "is_letter_or_digit",
    "is_whitespace",
    "java_double",
    "java_str",
    "split_dropping_trailing",
    "trim",
]

# Character.isWhitespace: the Unicode space separators except the no-break
# ones, plus these controls.
_WHITESPACE_CONTROLS = frozenset("\t\n\u000b\f\r\u001c\u001d\u001e\u001f")
_NO_BREAK = frozenset(("\u00a0", "\u2007", "\u202f"))
_LETTERS = frozenset(("Lu", "Ll", "Lt", "Lm", "Lo"))


def trim(text: str) -> str:
    """``String.trim()``: every leading and trailing code unit up to U+0020."""
    start, end = 0, len(text)
    while start < end and text[start] <= " ":
        start += 1
    while end > start and text[end - 1] <= " ":
        end -= 1
    return text[start:end]


def is_whitespace(c: str) -> bool:
    """``Character.isWhitespace(char)``."""
    if c in _WHITESPACE_CONTROLS:
        return True
    if c in _NO_BREAK:
        return False
    return unicodedata.category(c) in ("Zs", "Zl", "Zp")


def _bmp(c: str) -> bool:
    # A code point above U+FFFF is a surrogate pair in Java, and each half is
    # neither a letter nor a digit.
    return ord(c) <= 0xFFFF


def is_letter(c: str) -> bool:
    """``Character.isLetter(char)``."""
    return _bmp(c) and unicodedata.category(c) in _LETTERS


def is_digit(c: str) -> bool:
    """``Character.isDigit(char)``: the Nd category, which is ``isdecimal``."""
    return _bmp(c) and unicodedata.category(c) == "Nd"


def is_letter_or_digit(c: str) -> bool:
    """``Character.isLetterOrDigit(char)``."""
    return is_letter(c) or is_digit(c)


def split_dropping_trailing(parts: list[str]) -> list[str]:
    """``String.split(regex)`` with no limit drops trailing empty strings."""
    out = list(parts)
    while out and out[-1] == "":
        out.pop()
    return out


def java_double(value: float) -> str:
    """``Double.toString(double)``: shortest digits, ``E`` notation outside
    ``[1e-3, 1e7)``, and always a digit after the point."""
    if math.isnan(value):
        return "NaN"
    if math.isinf(value):
        return "Infinity" if value > 0 else "-Infinity"
    if value == 0:
        return "-0.0" if math.copysign(1.0, value) < 0 else "0.0"
    sign = "-" if value < 0 else ""
    magnitude = abs(value)
    digits, exponent = _shortest(magnitude)
    if 1e-3 <= magnitude < 1e7:
        point = exponent + 1
        if point <= 0:
            text = "0." + "0" * (-point) + digits
        elif point >= len(digits):
            text = digits + "0" * (point - len(digits)) + ".0"
        else:
            text = digits[:point] + "." + digits[point:]
        return sign + text
    mantissa = digits[0] + "." + (digits[1:] or "0")
    return f"{sign}{mantissa}E{exponent}"


def _shortest(magnitude: float) -> tuple[str, int]:
    """The shortest round-tripping digits of ``magnitude`` and the decimal
    exponent of the first one (``123.4`` is ``("1234", 2)``).

    ``repr`` gives the shortest round-tripping digits, as Java 19 and later do.
    """
    body, _, power = repr(magnitude).partition("e")
    whole, _, frac = body.partition(".")
    raw = whole + frac
    lead = len(raw) - len(raw.lstrip("0"))
    digits = raw.lstrip("0").rstrip("0") or "0"
    exponent = (int(power) if power else 0) + len(whole) - 1 - lead
    return digits, exponent


def java_str(value: Any) -> str | None:
    """``Object.toString()`` of a value SnakeYAML parsed (``None`` for null)."""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return java_double(value)
    if isinstance(value, dict):
        inner = ", ".join(f"{java_str(k)}={java_str(v)}" for k, v in value.items())
        return "{" + inner + "}"
    if isinstance(value, list):
        return "[" + ", ".join(str(java_str(v)) for v in value) + "]"
    if isinstance(value, (_dt.date, _dt.datetime)):
        # SnakeYAML builds a java.util.Date, whose text depends on the JVM's
        # time zone; ISO text is the honest stand-in.
        return value.isoformat()
    return str(value)


def get_simple_name(value: Any) -> str:
    """``getClass().getSimpleName()`` of a value SnakeYAML parsed."""
    if isinstance(value, bool):
        return "Boolean"
    if isinstance(value, int):
        if -(2**31) <= value < 2**31:
            return "Integer"
        if -(2**63) <= value < 2**63:
            return "Long"
        return "BigInteger"
    if isinstance(value, float):
        return "Double"
    if isinstance(value, dict):
        return "LinkedHashMap"
    if isinstance(value, list):
        return "ArrayList"
    if isinstance(value, (_dt.date, _dt.datetime)):
        return "Date"
    if isinstance(value, bytes):
        return "byte[]"
    return type(value).__name__


# --- HashSet<String> iteration order --------------------------------------------------


def _string_hash(text: str) -> int:
    """``String.hashCode()`` over UTF-16 code units, as a signed 32-bit int."""
    h = 0
    for unit in _utf16_units(text):
        h = (31 * h + unit) & 0xFFFFFFFF
    return h - (1 << 32) if h & 0x80000000 else h


def _utf16_units(text: str) -> Iterator[int]:
    for ch in text:
        point = ord(ch)
        if point > 0xFFFF:
            point -= 0x10000
            yield 0xD800 + (point >> 10)
            yield 0xDC00 + (point & 0x3FF)
        else:
            yield point


def _spread(h: int) -> int:
    unsigned = h & 0xFFFFFFFF
    return (unsigned ^ (unsigned >> 16)) & 0xFFFFFFFF


class JavaHashSet:
    """A set of strings that iterates in ``java.util.HashSet``'s order.

    HashMap keeps a table of ``capacity`` buckets (16, doubling once the size
    passes three quarters of it); iteration walks the buckets in index order
    and each bucket in insertion order. A resize splits each bucket keeping that
    relative order, so iterating by (bucket, insertion) reproduces it. ``None``
    hashes to bucket 0, as HashMap's null key does.
    """

    def __init__(self, items: Iterable[str | None] = ()) -> None:
        self._items: dict[str | None, int] = {}
        self._counter = 0
        self._capacity = 16
        for item in items:
            self.add(item)

    def add(self, item: str | None) -> bool:
        if item in self._items:
            return False
        self._items[item] = self._counter
        self._counter += 1
        while len(self._items) > self._capacity * 0.75:
            self._capacity *= 2
        return True

    def __contains__(self, item: object) -> bool:
        return item in self._items

    def __len__(self) -> int:
        return len(self._items)

    def __iter__(self) -> Iterator[str | None]:
        mask = self._capacity - 1

        def bucket(item: str | None) -> int:
            return 0 if item is None else _spread(_string_hash(item)) & mask

        return iter(sorted(self._items, key=lambda i: (bucket(i), self._items[i])))


# --- the stash JSON writer ------------------------------------------------------------

_ESCAPES = {
    '"': '\\"',
    "\\": "\\\\",
    "\b": "\\b",
    "\f": "\\f",
    "\n": "\\n",
    "\r": "\\r",
    "\t": "\\t",
}


def _json_string(text: str) -> str:
    out = ['"']
    for unit_text in _utf16_chars(text):
        if unit_text in _ESCAPES:
            out.append(_ESCAPES[unit_text])
            continue
        unit = ord(unit_text)
        # Jackson escapes the controls below U+0020 and, with ESCAPE_NON_ASCII,
        # every unit above U+007F; U+007F itself is written raw.
        if unit < 0x20 or unit > 0x7F:
            out.append(f"\\u{unit:04x}")
        else:
            out.append(unit_text)
    out.append('"')
    return "".join(out)


def _utf16_chars(text: str) -> Iterator[str]:
    for unit in _utf16_units(text):
        yield chr(unit)


def dumps_stash(value: Any) -> str:
    """``value`` as the converter's stash blob writes it."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            # Jackson writes a non-finite double as a quoted string.
            return '"' + java_double(value) + '"'
        return java_double(value)
    if isinstance(value, str):
        return _json_string(value)
    if isinstance(value, dict):
        inner = ", ".join(
            f"{_json_string(str(java_str(k)))}: {dumps_stash(v)}"
            for k, v in value.items()
        )
        return "{" + inner + "}"
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(dumps_stash(v) for v in value) + "]"
    if isinstance(value, _dt.datetime):
        # Jackson writes a java.util.Date as epoch milliseconds.
        moment = value if value.tzinfo else value.replace(tzinfo=_dt.UTC)
        return str(int(moment.timestamp() * 1000))
    if isinstance(value, _dt.date):
        moment = _dt.datetime(value.year, value.month, value.day, tzinfo=_dt.UTC)
        return str(int(moment.timestamp() * 1000))
    return _json_string(str(value))
