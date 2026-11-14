# Ported from apache/ossie-converters, databricks/java at 82006ba2
# (OssieConverter.java, OssieConverterCommon.java).
# Licensed under the Apache License, Version 2.0.
"""What both directions of the converter share: constants, YAML, accessors,
literal-aware rewriting, and the ``custom_extensions[DATABRICKS]`` stash.

The converter works on parsed YAML as plain maps and lists, and so does this
port: a dict in, a dict and its notices out. Names follow the Java so a reader
can hold the two side by side.
"""

from __future__ import annotations

import enum
import json
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

import yaml

from ...errors import RequestError
from ._java import dumps_stash, get_simple_name, is_whitespace, java_str, trim

OSSIE_VERSION = "0.2.0.dev0"
MV_VERSION = "1.1"
VENDOR = "DATABRICKS"
DIALECT_DATABRICKS = "DATABRICKS"
DIALECT_ANSI = "ANSI_SQL"
DIALECT_OSSIE_SQL = "OSSIE_SQL_2026"
SYNONYM_LIMIT = 10
STASH_VERSION = 1
STASH_SOURCE_KEY = "source_dataset"
CARD_ONE_TO_MANY = "one_to_many"
CARD_MANY_TO_ONE = "many_to_one"
MAX_JOIN_NODES = 200

IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
SELECT_WITH_RE = re.compile(r"^(select|with)\b", re.IGNORECASE | re.ASCII)

# Java's `.` stops at every line terminator, not only `\n`.
JAVA_DOT = "[^\n\r\u0085\u2028\u2029]"


# --- errors and notices ---------------------------------------------------------------


class Kind(str, enum.Enum):
    """The converter's stable failure categories."""

    INVALID_INPUT = "INVALID_INPUT"
    UNSUPPORTED_VERSION = "UNSUPPORTED_VERSION"
    NOT_REPRESENTABLE = "NOT_REPRESENTABLE"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class ConversionError(RequestError):
    """An input the converter refuses to convert (Java ``ConversionException``).

    A :class:`~exmergo_dex_core.errors.RequestError`, so it is a ``DexError``
    and still a ``ValueError``: the input is the caller's to fix.
    """

    def __init__(
        self,
        message: str,
        *,
        kind: Kind = Kind.NOT_REPRESENTABLE,
        reason: str | None = None,
        unsupported_version: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.kind = kind
        self.reason = reason if reason is not None else message
        self.unsupported_version = unsupported_version

    @classmethod
    def invalid_input(cls, message: str, reason: str) -> ConversionError:
        return cls(message, kind=Kind.INVALID_INPUT, reason=reason)

    @classmethod
    def unsupported(cls, message: str, version: str) -> ConversionError:
        return cls(message, kind=Kind.UNSUPPORTED_VERSION, unsupported_version=version)


@dataclass(frozen=True)
class Notice:
    """One drop or rewrite notice. ``origin`` is ``converter`` for a notice the
    converter itself emits, word for word, and ``dex`` for one of dex's own."""

    scope: str
    message: str
    origin: str = "converter"

    def render(self) -> str:
        return f"[{self.scope}] {self.message}"


@dataclass
class Notices:
    """Collects notices during a conversion, in the order they are raised."""

    items: list[Notice] = field(default_factory=list)

    def warn(self, scope: str, message: str, origin: str = "converter") -> None:
        self.items.append(Notice(scope, message, origin))

    def to_list(self) -> list[str]:
        """The converter's ``Notices.toList()``: rendered text, converter's only."""
        return [n.render() for n in self.items if n.origin == "converter"]


# --- YAML ---------------------------------------------------------------------------


class _Loader(yaml.SafeLoader):
    """SnakeYAML's reader as the converter configures it: YAML 1.2 booleans
    (``on``/``off``/``yes``/``no`` stay text), no duplicate keys, and at most 50
    aliases to a collection."""

    _MAX_ALIASES = 50

    def __init__(self, stream: str) -> None:
        super().__init__(stream)
        self._collection_aliases = 0

    def compose_node(self, parent: Any, index: Any) -> Any:
        if self.check_event(yaml.AliasEvent):
            event = self.peek_event()
            node = self.anchors.get(event.anchor)
            if isinstance(node, (yaml.MappingNode, yaml.SequenceNode)):
                self._collection_aliases += 1
                if self._collection_aliases > self._MAX_ALIASES:
                    raise yaml.composer.ComposerError(
                        None,
                        None,
                        "Number of aliases for non-scalar nodes exceeds the "
                        f"specified max={self._MAX_ALIASES}",
                        event.start_mark,
                    )
        return super().compose_node(parent, index)


def _reject_cycles(
    value: Any, stack: set[int] | None = None, done: set[int] | None = None
) -> None:
    """Refuse a value that contains itself. SnakeYAML builds one from a
    recursive alias, and the converter would recurse into it without end. A
    shared node is walked once, so aliases cannot make this exponential."""
    if not isinstance(value, (dict, list)):
        return
    stack = set() if stack is None else stack
    done = set() if done is None else done
    if id(value) in done:
        return
    if id(value) in stack:
        raise yaml.YAMLError("found a recursive alias")
    stack.add(id(value))
    for item in value.values() if isinstance(value, dict) else value:
        _reject_cycles(item, stack, done)
    stack.discard(id(value))
    done.add(id(value))


_Loader.yaml_implicit_resolvers = {
    first: [(tag, rx) for tag, rx in resolvers if tag != "tag:yaml.org,2002:bool"]
    for first, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}
_Loader.add_implicit_resolver(
    "tag:yaml.org,2002:bool",
    re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$"),
    list("tTfF"),
)


def _mapping(loader: _Loader, node: yaml.MappingNode, deep: bool = False) -> Any:
    seen: list[Any] = []
    for key_node, _ in node.value:
        key = loader.construct_object(key_node, deep=True)
        if key in seen:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                f"found duplicate key {java_str(key)}",
                key_node.start_mark,
            )
        seen.append(key)
    return loader.construct_mapping(node, deep=deep)


_Loader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)


def load_yaml(text: str) -> Any:
    """Parse ``text`` as the converter does. Raises ``yaml.YAMLError``."""
    value = yaml.load(text, Loader=_Loader)  # noqa: S506 - a SafeLoader subclass
    _reject_cycles(value)
    return value


def parse_yaml(text: str) -> Any:
    """``OssieConverter.parseYaml``: ``load_yaml``, with a failure an INVALID_INPUT."""
    try:
        return load_yaml(text)
    except (yaml.YAMLError, TypeError, ValueError) as exc:
        message = "failed to parse YAML: " + _yaml_message(exc)
        raise ConversionError.invalid_input(message, message) from exc


def _yaml_message(exc: BaseException) -> str:
    return " ".join(str(exc).split())


def parse_root(text: str | None, what: str) -> dict[str, Any]:
    """The mapping at the root of ``text``, or the converter's refusal.

    ``what`` is ``Metric View`` or ``Apache Ossie``, as the messages name it.
    """
    if text is None or not trim(text):
        raise ConversionError.invalid_input(
            f"Invalid {what} YAML: expected a mapping at the root", "the input is empty"
        )
    try:
        parsed = load_yaml(text)
    except (yaml.YAMLError, TypeError, ValueError) as exc:
        detail = _yaml_message(exc)
        raise ConversionError.invalid_input(
            f"Invalid {what} YAML: {detail}", f"failed to parse YAML: {detail}"
        ) from exc
    if not isinstance(parsed, dict):
        raise ConversionError.invalid_input(
            f"Invalid {what} YAML: expected a mapping at the root",
            "it is not a mapping at the root",
        )
    return parsed


# --- accessors ------------------------------------------------------------------------


def as_map(x: Any) -> dict[Any, Any]:
    return x if isinstance(x, dict) else {}


def as_list(x: Any) -> list[Any]:
    return x if isinstance(x, list) else []


def str_(x: Any) -> str | None:
    return java_str(x)


def cat(x: Any) -> str:
    """``x`` as Java's string concatenation writes it: ``null`` for null."""
    text = java_str(x)
    return "null" if text is None else text


def str_list(x: Any) -> list[str]:
    return [str(java_str(o)) for o in as_list(x) if o is not None]


def is_simple_identifier(expr: Any) -> bool:
    return isinstance(expr, str) and IDENTIFIER_RE.fullmatch(trim(expr)) is not None


def truthy(v: Any) -> bool:
    """The converter's emptiness test: null, ``""``, an empty list or map, zero
    and ``false`` are absent; everything else is present."""
    if v is None:
        return False
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v != ""
    if isinstance(v, (list, tuple, dict)):
        return len(v) > 0
    if isinstance(v, (int, float)):
        return float(v) != 0.0
    return True


def require(obj: dict[Any, Any], key: str, what: str) -> Any:
    if key not in obj or obj[key] is None:
        raise ConversionError(f"{what} is missing required '{key}'")
    value = obj[key]
    if isinstance(value, str) and not trim(value):
        raise ConversionError(f"{what} has an empty '{key}'")
    return value


def require_str(obj: dict[Any, Any], key: str, what: str) -> str:
    value = require(obj, key, what)
    if isinstance(value, str):
        return value
    raise ConversionError(
        f"{what}: '{key}' must be a string, got {get_simple_name(value)}"
    )


def validate_source(source: Any, dataset_name: str) -> str:
    s = "" if source is None else trim(str(java_str(source)))
    if not s:
        raise ConversionError(f"Dataset '{dataset_name}': missing/empty 'source'")
    if SELECT_WITH_RE.search(s):
        return s
    parts = s.split(".")
    ok = len(parts) == 3 and all(
        p and not any(is_whitespace(c) for c in p) for p in parts
    )
    if ok:
        return s
    raise ConversionError(
        f"Dataset '{dataset_name}': source '{java_str(source)}' must be a 3-part "
        "catalog.schema.table identifier or a SELECT/WITH subquery"
    )


def pick_expression(osi_expression: Any, scope: str) -> str | None:
    """The first truthy of the DATABRICKS, ANSI_SQL and OSSIE_SQL_2026 entries
    of ``expression.dialects``, or ``None`` when there is none."""
    if osi_expression is None:
        return None
    if not isinstance(osi_expression, dict):
        reason = f"{scope}: 'expression' must be a mapping"
        raise ConversionError.invalid_input(reason, reason)
    dialect_values = osi_expression.get("dialects")
    if dialect_values is None:
        return None
    if not isinstance(dialect_values, list):
        reason = f"{scope}: 'expression.dialects' must be a list"
        raise ConversionError.invalid_input(reason, reason)
    dialects: dict[Any, Any] = {}
    for index, d in enumerate(dialect_values):
        if not isinstance(d, dict):
            reason = f"{scope}: 'expression.dialects[{index}]' must be a mapping"
            raise ConversionError.invalid_input(reason, reason)
        dialects[java_str(d.get("dialect"))] = d.get("expression")
    if truthy(dialects.get(DIALECT_DATABRICKS)):
        chosen = dialects.get(DIALECT_DATABRICKS)
    elif truthy(dialects.get(DIALECT_ANSI)):
        chosen = dialects.get(DIALECT_ANSI)
    else:
        chosen = dialects.get(DIALECT_OSSIE_SQL)
    if chosen is not None and not isinstance(chosen, str):
        raise ConversionError(
            f"expression must be a string, got {get_simple_name(chosen)}"
        )
    return chosen


def synonyms_of(ai_context: Any) -> list[str]:
    if isinstance(ai_context, dict):
        return str_list(ai_context.get("synonyms"))
    return []


def merge_description(description: Any, ai_context: Any) -> str | None:
    desc = java_str(description)
    if isinstance(ai_context, str) and trim(ai_context):
        return f"{desc}\n{ai_context}" if desc else ai_context
    return desc


def last_identifier(source: Any) -> str | None:
    if source is None:
        return None
    s = trim(str(java_str(source)))
    dot = s.rfind(".")
    last = s[dot + 1 :] if dot >= 0 else s
    return last.strip("`")


# --- literal-aware rewriting ----------------------------------------------------------


def _end_of_quoted(sql: str, start: int, quote: str) -> int:
    i = start + 1
    n = len(sql)
    while i < n:
        c = sql[i]
        if c == "\\" and quote != "`" and i + 1 < n:
            i += 2
            continue
        if c == quote:
            if i + 1 < n and sql[i + 1] == quote:
                i += 2
                continue
            return i + 1
        i += 1
    return n


def literal_spans(sql: str) -> list[tuple[int, int]]:
    """Every span that is not SQL code: string literals, backquoted identifiers,
    ``--`` line comments and ``/* */`` block comments."""
    spans: list[tuple[int, int]] = []
    i = 0
    n = len(sql)
    while i < n:
        c = sql[i]
        skip_to = -1
        if c in ("'", '"', "`"):
            skip_to = _end_of_quoted(sql, i, c)
        elif c == "-" and i + 1 < n and sql[i + 1] == "-":
            nl = sql.find("\n", i)
            skip_to = n if nl < 0 else nl
        elif c == "/" and i + 1 < n and sql[i + 1] == "*":
            end = sql.find("*/", i + 2)
            skip_to = n if end < 0 else end + 2
        if skip_to < 0:
            i += 1
            continue
        spans.append((i, skip_to))
        i = skip_to
    return spans


def replace_outside_literals(
    sql: str, pattern: re.Pattern[str], replacer: str | Callable[[re.Match[str]], str]
) -> str:
    """``pattern`` replaced in the code spans of ``sql`` only. The replacement
    is literal text, never a template."""
    fn = (lambda _m: replacer) if isinstance(replacer, str) else replacer
    out: list[str] = []
    code_start = 0
    for start, end in literal_spans(sql):
        out.append(pattern.sub(fn, sql[code_start:start]))
        out.append(sql[start:end])
        code_start = end
    out.append(pattern.sub(fn, sql[code_start:]))
    return "".join(out)


def find_outside_literals(sql: str, pattern: re.Pattern[str]) -> bool:
    spans = literal_spans(sql)
    if not spans:
        return pattern.search(sql) is not None
    code = list(sql)
    for start, end in spans:
        for i in range(start, end):
            code[i] = " "
    return pattern.search("".join(code)) is not None


def qualifier_chain_pattern(names: Iterable[str]) -> re.Pattern[str] | None:
    """A maximal dotted run of ``names`` qualifying a column (``parent.child.``),
    group 1 holding the run without its trailing dot."""
    ordered = sorted(names, key=len, reverse=True)
    if not ordered:
        return None
    one = "(?:" + "|".join(re.escape(n) for n in ordered) + ")"
    return re.compile(r"\b(" + one + r"(?:\." + one + r")*)\.", re.ASCII)


def rewrite_qualifiers(
    expr: str, chain: re.Pattern[str] | None, resolve: Callable[[str], str | None]
) -> str:
    """Each qualifier run rewritten from its last name through ``resolve``; a run
    whose leaf ``resolve`` does not know is left alone."""
    if chain is None:
        return expr

    def replace(m: re.Match[str]) -> str:
        leaf = last_identifier(m.group(1))
        resolved = None if leaf is None else resolve(leaf)
        return m.group(0) if resolved is None else resolved + "."

    return replace_outside_literals(expr, chain, replace)


# --- the custom_extensions[DATABRICKS] stash ------------------------------------------


def _custom_extensions(obj: dict[Any, Any]) -> list[dict[Any, Any]]:
    value = obj.get("custom_extensions")
    if value is None:
        return []
    if not isinstance(value, list):
        reason = "'custom_extensions' must be a list"
        raise ConversionError.invalid_input(reason, reason)
    out: list[dict[Any, Any]] = []
    for index, extension in enumerate(value):
        if not isinstance(extension, dict):
            reason = f"'custom_extensions[{index}]' must be a mapping"
            raise ConversionError.invalid_input(reason, reason)
        out.append(extension)
    return out


def _strict_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise ValueError(f"Duplicate field '{key}'")
        out[key] = value
    return out


def _no_constant(name: str) -> Any:
    raise ValueError(f"Non-standard token '{name}'")


def loads_stash(data: str) -> Any:
    """The stash blob parsed as Jackson's strict reader does: duplicate keys,
    ``NaN`` and trailing tokens refused."""
    return json.loads(
        data, object_pairs_hook=_strict_pairs, parse_constant=_no_constant
    )


def read_stash(obj: dict[Any, Any], notices: Notices, scope: str) -> dict[str, Any]:
    """The first ``custom_extensions`` entry for DATABRICKS, parsed, without ``_v``."""
    stash: dict[str, Any] | None = None
    saw_extra = False
    for ext in _custom_extensions(obj):
        if java_str(ext.get("vendor_name")) != VENDOR:
            continue
        if stash is not None:
            saw_extra = True
            continue
        data_value = ext.get("data")
        if data_value is not None and not isinstance(data_value, str):
            reason = "DATABRICKS custom_extensions data must be a string"
            raise ConversionError.invalid_input(reason, reason)
        data = data_value or "{}"
        try:
            parsed = loads_stash(data)
        except ValueError as exc:
            reason = f"DATABRICKS custom_extensions data is not valid JSON: {exc}"
            raise ConversionError.invalid_input(reason, reason) from exc
        if not isinstance(parsed, dict):
            reason = "DATABRICKS custom_extensions data must be a JSON object"
            raise ConversionError.invalid_input(reason, reason)
        parsed.pop("_v", None)
        stash = parsed
    if saw_extra:
        notices.warn(
            scope,
            "found more than one DATABRICKS custom_extensions entry; "
            "using the first and ignoring the rest",
        )
    return stash if stash is not None else {}


def foreign_vendor_extensions(obj: dict[Any, Any]) -> list[dict[Any, Any]]:
    return [
        e for e in _custom_extensions(obj) if java_str(e.get("vendor_name")) != VENDOR
    ]


def stash_blob(data: dict[str, Any]) -> str:
    """The stash blob for ``data``: ``{"_v": 1, ...}`` in the pinned bytes."""
    return dumps_stash({"_v": STASH_VERSION, **data})


def write_stash(obj: dict[str, Any], data: dict[str, Any]) -> None:
    """Attach (or replace) the DATABRICKS entry holding ``data``; a no-op when empty."""
    if not data:
        return
    blob = stash_blob(data)
    exts = obj.setdefault("custom_extensions", [])
    for ext in exts:
        if isinstance(ext, dict) and java_str(ext.get("vendor_name")) == VENDOR:
            ext["data"] = blob
            return
    exts.append({"vendor_name": VENDOR, "data": blob})
