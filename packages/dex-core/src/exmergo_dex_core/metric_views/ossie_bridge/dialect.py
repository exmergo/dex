# Ported from apache/ossie-converters, databricks/java at 82006ba2
# (ExpressionDialect.java). Licensed under the Apache License, Version 2.0.
"""The most portable Ossie dialect a Databricks SQL expression can carry on import.

``OSSIE_SQL_2026`` when it uses only the Ossie expression language, else
``ANSI_SQL`` when it uses only standard SQL, else ``DATABRICKS``. A lexical check
against allowlists, not a parser, and deliberately conservative: anything it does
not recognize stays ``DATABRICKS``, which is always correct because the
expression came from a Metric View.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from ._java import is_digit, is_letter, is_letter_or_digit, is_whitespace
from .common import DIALECT_ANSI, DIALECT_DATABRICKS, DIALECT_OSSIE_SQL

OSSIE_FUNCTIONS = frozenset(
    {
        "SUM", "COUNT", "AVG", "MIN", "MAX", "STDDEV", "STDDEV_POP", "STDDEV_SAMP",
        "VARIANCE", "VAR_POP", "VAR_SAMP", "MEDIAN", "PERCENTILE_CONT",
        "PERCENTILE_DISC", "APPROX_COUNT_DISTINCT", "APPROX_PERCENTILE",
        "CURRENT_DATE", "CURRENT_TIMESTAMP", "YEAR", "QUARTER", "MONTH", "DAY",
        "DAYOFYEAR", "HOUR", "MINUTE", "SECOND", "EXTRACT", "DATE_PART",
        "DATE_TRUNC", "DATEADD", "TO_DATE", "CONCAT", "LENGTH", "LOWER", "UPPER",
        "TRIM", "LTRIM", "RTRIM", "LEFT", "RIGHT", "SUBSTRING", "REPLACE",
        "SPLIT_PART", "POSITION", "CHARINDEX", "CONTAINS", "STARTSWITH",
        "ENDSWITH", "ABS", "ROUND", "FLOOR", "CEIL", "CEILING", "MOD", "SIGN",
        "POWER", "SQRT", "EXP", "LN", "LOG", "LOG10", "SIN", "COS", "TAN", "ASIN",
        "ACOS", "ATAN", "ATAN2", "RADIANS", "DEGREES", "PI", "GREATEST", "LEAST",
        "IF", "IFF", "NULLIF", "COALESCE", "IFNULL", "NVL", "NVL2", "ZEROIFNULL",
        "NULLIFZERO", "CAST", "TRY_CAST",
    }
)  # fmt: skip

ANSI_FUNCTIONS = frozenset(
    {
        "SUM", "COUNT", "AVG", "MIN", "MAX", "STDDEV_POP", "STDDEV_SAMP", "VAR_POP",
        "VAR_SAMP", "CORR", "COVAR_POP", "COVAR_SAMP", "EVERY", "PERCENTILE_CONT",
        "PERCENTILE_DISC", "CURRENT_DATE", "CURRENT_TIMESTAMP", "EXTRACT", "CAST",
        "COALESCE", "NULLIF", "UPPER", "LOWER", "TRIM", "SUBSTRING", "POSITION",
        "CHAR_LENGTH", "CHARACTER_LENGTH", "OCTET_LENGTH", "ABS", "MOD", "LN",
        "EXP", "POWER", "SQRT", "FLOOR", "CEIL", "CEILING",
    }
)  # fmt: skip

AGGREGATES = frozenset(
    {
        "SUM", "COUNT", "AVG", "MIN", "MAX", "STDDEV", "STDDEV_POP", "STDDEV_SAMP",
        "VARIANCE", "VAR_POP", "VAR_SAMP", "MEDIAN", "PERCENTILE_CONT",
        "PERCENTILE_DISC", "APPROX_COUNT_DISTINCT", "APPROX_PERCENTILE", "CORR",
        "COVAR_POP", "COVAR_SAMP", "EVERY", "FILTER", "GROUP",
    }
)  # fmt: skip

ARITY = {
    "COUNT": (1, 1),
    "CEIL": (1, 1),
    "CEILING": (1, 1),
    "FLOOR": (1, 1),
    "DATEADD": (3, 3),
    "LOG": (2, 2),
    "TO_DATE": (1, 1),
    "APPROX_COUNT_DISTINCT": (1, 1),
    "APPROX_PERCENTILE": (2, 2),
    "TRIM": (1, 1),
    "LTRIM": (1, 1),
    "RTRIM": (1, 1),
    "REPLACE": (3, 3),
    "CHARINDEX": (2, 2),
}

OSSIE_TYPES = frozenset(
    {"STRING", "DECIMAL", "NUMERIC", "DOUBLE", "BOOLEAN", "DATE", "TIMESTAMP_NTZ"}
)
ANSI_TYPES = frozenset({"DECIMAL", "NUMERIC", "BOOLEAN", "DATE"})
PARAMETERIZED_TYPES = frozenset({"DECIMAL", "NUMERIC"})

KEYWORDS = frozenset(
    {
        "CASE", "WHEN", "THEN", "ELSE", "END", "AND", "OR", "NOT", "IN", "BETWEEN",
        "LIKE", "IS", "NULL", "TRUE", "FALSE", "DISTINCT", "ORDER", "BY", "ASC",
        "DESC", "WITHIN",
    }
)  # fmt: skip
KEYWORDS_BEFORE_PAREN = frozenset(
    {
        "AND", "OR", "NOT", "WHEN", "THEN", "ELSE", "CASE", "IN", "LIKE", "ILIKE",
        "BETWEEN", "DISTINCT", "BY", "FILTER", "GROUP",
    }
)  # fmt: skip
CONTEXT_KEYWORDS = frozenset(
    {"AS", "FROM", "FOR", "ESCAPE", "WHERE", "NULLS", "BOTH", "LEADING", "TRAILING"}
)
VALUE_KEYWORDS = frozenset({"NULL", "TRUE", "FALSE"})
LITERAL_PREFIXES = frozenset({"DATE", "TIMESTAMP_NTZ"})
REJECTED_WORDS = frozenset(
    {
        "SELECT", "WITH", "UNION", "EXCEPT", "INTERSECT", "HAVING", "JOIN",
        "LATERAL", "INTERVAL", "EXISTS", "ARRAY", "MAP", "STRUCT", "RLIKE",
        "REGEXP", "DIV", "ANY", "SOME", "ALL", "QUALIFY", "LIMIT", "WINDOW",
        "VALUES", "TABLE", "OVER", "FILTER", "GROUP", "CURRENT_USER", "USER",
        "SESSION_USER", "CURRENT_TIME", "LOCALTIME", "GROUPING__ID",
    }
)  # fmt: skip

SYMBOLS = (
    "<>", "<=", ">=", "!=", "||", "<", ">", "=", "+", "-", "*", "/", "%", "(", ")",
    ",", ".",
)  # fmt: skip
REJECTED_SYMBOLS = ("<=>", "==", "->", "=>", "<<", ">>", "--", "/*")

NUMBER = re.compile(r"(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?", re.ASCII)

_Kind = Literal["IDENT", "NUMBER", "STRING", "SYMBOL"]


@dataclass(frozen=True)
class _Token:
    kind: _Kind
    text: str

    def is_(self, symbol: str) -> bool:
        return self.kind == "SYMBOL" and self.text == symbol

    def is_word(self, word: str) -> bool:
        return self.kind == "IDENT" and self.text.upper() == word.upper()

    def upper(self) -> str:
        return self.text.upper()


def classify(
    expr: str | None, datasets: set[str], opaque_names: set[str], measure: bool
) -> str:
    """The most portable dialect ``expr`` can carry.

    ``datasets`` are the model's lower-cased dataset names (``a.b`` is portable
    only when ``a`` is one); ``opaque_names`` are lower-cased names whose meaning
    comes from the Metric View rather than a column; ``measure`` says a name
    outside an aggregate refers to another measure.
    """
    tokens = None if expr is None else _tokenize(expr)
    if not tokens:
        return DIALECT_DATABRICKS
    scan = _Scan(tokens, datasets, opaque_names, measure)
    if not scan.run():
        return DIALECT_DATABRICKS
    if scan.ossie:
        return DIALECT_OSSIE_SQL
    return DIALECT_ANSI if scan.ansi else DIALECT_DATABRICKS


def _is_ident_part(c: str) -> bool:
    return is_letter_or_digit(c) or c == "_"


def _tokenize(s: str) -> list[_Token] | None:
    out: list[_Token] = []
    n = len(s)
    i = 0
    while i < n:
        c = s[i]
        if is_whitespace(c):
            i += 1
        elif c == "'":
            end = s.find("'", i + 1)
            if end < 0:
                return None
            body = s[i + 1 : end]
            if "\\" in body or (end + 1 < n and s[end + 1] == "'"):
                return None
            out.append(_Token("STRING", body))
            i = end + 1
        elif is_letter(c) or c == "_":
            j = i + 1
            while j < n and _is_ident_part(s[j]):
                j += 1
            out.append(_Token("IDENT", s[i:j]))
            i = j
        elif is_digit(c) or (c == "." and i + 1 < n and is_digit(s[i + 1])):
            m = NUMBER.match(s, i)
            if m is None:
                return None
            j = m.end()
            if j < n and _is_ident_part(s[j]):
                return None
            out.append(_Token("NUMBER", s[i:j]))
            i = j
        else:
            sym = _symbol_at(s, i)
            if sym is None:
                return None
            out.append(_Token("SYMBOL", sym))
            i += len(sym)
    return out


def _symbol_at(s: str, i: int) -> str | None:
    if any(s.startswith(rejected, i) for rejected in REJECTED_SYMBOLS):
        return None
    return next((sym for sym in SYMBOLS if s.startswith(sym, i)), None)


def _is_keyword(u: str) -> bool:
    return u in KEYWORDS or u in KEYWORDS_BEFORE_PAREN or u in CONTEXT_KEYWORDS


class _Frame:
    __slots__ = ("aggregate", "commas", "empty", "keyword_form", "opener")

    def __init__(self, opener: str) -> None:
        self.opener = opener
        self.aggregate = opener in AGGREGATES
        self.commas = 0
        self.empty = True
        self.keyword_form = False


class _Scan:
    """One pass over the tokens, narrowing which portable dialects still fit."""

    def __init__(
        self,
        tokens: list[_Token],
        datasets: set[str],
        opaque_names: set[str],
        measure: bool,
    ) -> None:
        self.tokens = tokens
        self.datasets = datasets
        self.opaque_names = opaque_names
        self.measure = measure
        self.frames: list[_Frame] = []  # the last item is the top
        self.pending_opener = ""
        self.ossie = True
        self.ansi = True

    def _top(self) -> _Frame | None:
        return self.frames[-1] if self.frames else None

    def run(self) -> bool:
        tokens = self.tokens
        for i, t in enumerate(tokens):
            prev = tokens[i - 1] if i > 0 else None
            nxt = tokens[i + 1] if i + 1 < len(tokens) else None
            if (
                prev is not None
                and _ends_operand(prev)
                and _starts_operand(t)
                and not _is_typed_literal(prev, t)
            ):
                return False
            top = self._top()
            if top is not None and not t.is_(")"):
                top.empty = False
            if t.kind in ("STRING", "NUMBER"):
                ok = True
            elif t.kind == "SYMBOL":
                ok = self._symbol(t, prev, nxt, top)
            else:
                ok = self._ident(i, t, prev, nxt, top)
            if not ok:
                return False
        return not self.frames

    def _symbol(
        self, t: _Token, prev: _Token | None, nxt: _Token | None, top: _Frame | None
    ) -> bool:
        if t.text == "(":
            self.frames.append(_Frame(self.pending_opener))
            self.pending_opener = ""
            return True
        if t.text == ")":
            return bool(self.frames) and self._close(self.frames.pop())
        if t.text == ",":
            if top is None:
                return False
            top.commas += 1
            return True
        if t.text == ".":
            return (
                prev is not None
                and prev.kind == "IDENT"
                and nxt is not None
                and nxt.kind == "IDENT"
            )
        if t.text in ("%", "!="):
            self.ansi = False
        return True

    def _close(self, f: _Frame) -> bool:
        if f.keyword_form and f.commas > 0:
            return False
        if f.opener in ("POSITION", "FILTER"):
            return f.keyword_form
        if f.opener == "SUBSTRING" and not f.keyword_form:
            self.ansi = False
        arity = ARITY.get(f.opener)
        if arity is None or f.keyword_form:
            return True
        args = 0 if f.empty else f.commas + 1
        return arity[0] <= args <= arity[1]

    def _ident(
        self,
        i: int,
        t: _Token,
        prev: _Token | None,
        nxt: _Token | None,
        top: _Frame | None,
    ) -> bool:
        if prev is not None and prev.is_("."):
            return True
        u = t.upper()
        if nxt is not None and nxt.is_("."):
            return self._qualified_name(i, t)
        if (
            top is not None
            and _is_cast(top)
            and prev is not None
            and prev.is_word("AS")
        ):
            return self._cast_type(u, nxt)
        if nxt is not None and nxt.is_("("):
            return self._call(u, prev)
        if nxt is not None and nxt.kind == "STRING" and not _is_keyword(u):
            return self._typed_literal(u)
        return self._word(t, u, prev, nxt, top)

    def _qualified_name(self, i: int, head: _Token) -> bool:
        tokens = self.tokens
        if head.text.lower() not in self.datasets or not self._is_column_position():
            return False
        two_parts = i + 2 < len(tokens) and tokens[i + 2].kind == "IDENT"
        deeper = i + 3 < len(tokens) and tokens[i + 3].is_(".")
        call = i + 3 < len(tokens) and tokens[i + 3].is_("(")
        return (
            two_parts
            and not deeper
            and not call
            and tokens[i + 2].text.lower() not in self.opaque_names
        )

    def _is_column_position(self) -> bool:
        if not self.measure:
            return True
        return any(f.aggregate for f in self.frames)

    def _call(self, u: str, prev: _Token | None) -> bool:
        in_ossie = u in OSSIE_FUNCTIONS
        in_ansi = u in ANSI_FUNCTIONS
        if in_ossie or in_ansi:
            self.ossie = self.ossie and in_ossie
            self.ansi = self.ansi and in_ansi and not u.startswith("CURRENT_")
            self.pending_opener = u
            return True
        if u not in KEYWORDS_BEFORE_PAREN:
            return False
        if u == "FILTER":
            if prev is None or not prev.is_(")"):
                return False
            self.ossie = False
        elif u == "ILIKE":
            self.ansi = False
        elif u == "GROUP" and (prev is None or not prev.is_word("WITHIN")):
            return False
        self.pending_opener = u
        return True

    def _typed_literal(self, u: str) -> bool:
        if u not in LITERAL_PREFIXES:
            return False
        self.ansi = self.ansi and u == "DATE"
        return True

    def _cast_type(self, u: str, nxt: _Token | None) -> bool:
        params = nxt is not None and nxt.is_("(")
        if (u in PARAMETERIZED_TYPES) != params:
            return False
        if params:
            self.pending_opener = "TYPE"
        in_ossie = u in OSSIE_TYPES
        in_ansi = u in ANSI_TYPES
        self.ossie = self.ossie and in_ossie
        self.ansi = self.ansi and in_ansi
        return in_ossie or in_ansi

    def _word(
        self,
        t: _Token,
        u: str,
        prev: _Token | None,
        nxt: _Token | None,
        top: _Frame | None,
    ) -> bool:
        opener = "" if top is None else top.opener
        if u == "AS":
            return _is_cast(top)
        if u == "FROM":
            if opener == "EXTRACT" and top is not None:
                top.keyword_form = True
                return True
            if prev is not None and prev.is_word("DISTINCT"):
                return True
            return self._standard_keyword_form(top, "SUBSTRING", "TRIM")
        if u == "FOR":
            return self._standard_keyword_form(top, "SUBSTRING")
        if u in ("BOTH", "LEADING", "TRAILING"):
            return self._standard_keyword_form(top, "TRIM")
        if u == "WHERE":
            if (
                opener == "FILTER"
                and top is not None
                and prev is not None
                and prev.is_("(")
            ):
                top.keyword_form = True
                return True
            return False
        if u == "IN":
            if opener == "POSITION" and top is not None:
                top.keyword_form = True
            return True
        if u == "ILIKE":
            self.ansi = False
            return True
        if u == "ESCAPE":
            self.ossie = False
            return True
        if u == "NULLS":
            if nxt is not None and (nxt.is_word("FIRST") or nxt.is_word("LAST")):
                self.ossie = False
                return True
            return False
        if u in REJECTED_WORDS:
            return False
        return _is_keyword(u) or (
            self._is_column_position() and t.text.lower() not in self.opaque_names
        )

    def _standard_keyword_form(self, top: _Frame | None, *openers: str) -> bool:
        if top is None:
            return False
        if top.opener in openers:
            top.keyword_form = True
            self.ossie = False
            return True
        return False


def _ends_operand(t: _Token) -> bool:
    if t.kind == "IDENT":
        u = t.upper()
        return not _is_keyword(u) or u in VALUE_KEYWORDS or u == "END"
    return t.kind in ("NUMBER", "STRING") or t.is_(")")


def _starts_operand(t: _Token) -> bool:
    if t.kind == "IDENT":
        u = t.upper()
        return not _is_keyword(u) or u in VALUE_KEYWORDS
    return t.kind in ("NUMBER", "STRING")


def _is_typed_literal(prev: _Token, t: _Token) -> bool:
    return (
        t.kind == "STRING" and prev.kind == "IDENT" and prev.upper() in LITERAL_PREFIXES
    )


def _is_cast(f: _Frame | None) -> bool:
    return f is not None and f.opener in ("CAST", "TRY_CAST")
