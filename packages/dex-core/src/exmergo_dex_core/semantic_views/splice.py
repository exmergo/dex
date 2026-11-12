"""Byte-keeping edits inside one logical table of a Semantic View file (#1292).

Annotation and derivation change a Semantic View the way a person would: one
member rewritten, one appended, the table's description set, and nothing else in
the file touched, comments included. Everything is found by ``yaml.compose``
marks and the indentation rule in :mod:`..yaml_spans`, never by re-serializing
the file. A Semantic View nests its members a level deeper than the root lists
:mod:`..yaml_items` handles (``tables[i].dimensions[j]``), which is why this
module exists rather than a re-render of the whole table, which would drop the
comments written inside it.

Names are matched ignoring case, as Snowflake folds unquoted identifiers. Only
block style is edited: a flow-style list or mapping shares its lines with
neighbours, so each function raises :class:`UnwritableError` rather than rewrite
lines it cannot bound.
"""

from __future__ import annotations

from typing import Any

import yaml

from ..errors import DexError
from ..yaml_items import dump_items
from ..yaml_spans import before_trailing_blanks, block_end
from .loader import Dumper

__all__ = [
    "UnwritableError",
    "append_member",
    "replace_member",
    "set_member_scalar",
    "set_table_scalar",
    "table_names",
]

#: The keys a new table-level scalar lands after, in order of preference, so a
#: description sits near the table's identity as authors write it.
_ANCHORS = ("synonyms", "base_table", "name")


class UnwritableError(DexError):
    """The file cannot be edited in place; the message says why. Turned into a
    definition's ``failed`` reason by :mod:`.derivation`, never shown raw."""


def table_names(text: str) -> list[str]:
    """The names of the block-style logical tables ``text`` declares."""
    try:
        root = yaml.compose(text)
    except yaml.YAMLError:
        return []
    sequence = _section(root, "tables")
    if sequence is None:
        return []
    return [n for item in sequence.value if (n := _name(item)) is not None]


def set_table_scalar(text: str, table: str, key: str, value: str) -> str:
    """``text`` with the table's ``key`` set to ``value``, every other byte kept.

    Where the key is written, its lines are replaced; where it is not, it is
    added after the table's ``synonyms``, ``base_table`` or ``name``.
    """

    return _set_scalar(
        text, _table(text, table), key, value, f"tables['{table}']", _ANCHORS
    )


def set_member_scalar(
    text: str, table: str, section: str, name: str, key: str, value: str
) -> str:
    """``text`` with one member's ``key`` set to ``value``, every other byte of
    the member kept too (a flow-style ``synonyms: [a]`` stays as written).

    Where the key is written, its lines are replaced; where it is not, it is
    added at the end of the member.
    """

    node = _table(text, table)
    sequence = _section(node, section)
    where = f"tables['{table}'].{section}['{name}']"
    if sequence is not None:
        for item in sequence.value:
            if (_name(item) or "").lower() != name.lower():
                continue
            if (
                isinstance(item, yaml.MappingNode)
                and not item.flow_style
                and _inside(item, sequence)
            ):
                return _set_scalar(text, item, key, value, where, ())
            break
    raise UnwritableError(
        f"{where} is not a block-style list item dex can rewrite on its own"
    )


def _set_scalar(
    text: str,
    node: yaml.MappingNode,
    key: str,
    value: str,
    where: str,
    anchors: tuple[str, ...],
) -> str:
    """``text`` with the list item ``node``'s ``key`` set to ``value``.

    A new key lands after the first of ``anchors`` the item writes, else at the
    item's end.
    """

    indent = _key_indent(node)
    rendered = dump_items({key: value}, indent, Dumper)
    anchor_end: int | None = None
    anchor_rank = len(anchors)
    for key_node, _value in node.value:
        line_start = _line_start(text, key_node.start_mark.index)
        end = before_trailing_blanks(text, block_end(text, line_start, indent))
        lowered = str(key_node.value).lower()
        if lowered == key.lower():
            if text[line_start : key_node.start_mark.index].strip():
                raise UnwritableError(
                    f"{where}.{key} shares its line with the list dash, so dex "
                    "cannot rewrite it alone"
                )
            return text[:line_start] + rendered + text[end:]
        if lowered in anchors and anchors.index(lowered) < anchor_rank:
            anchor_rank, anchor_end = anchors.index(lowered), end
    if anchor_end is None:
        anchor_end = _item_end(text, node)
    return _insert(text, anchor_end, rendered)


def replace_member(
    text: str, table: str, section: str, name: str, entry: dict[str, Any]
) -> str:
    """``text`` with the member ``name`` of ``section`` re-rendered as ``entry``."""

    node = _table(text, table)
    sequence = _section(node, section)
    if sequence is None:
        raise UnwritableError(
            f"tables['{table}'].{section} is not a block-style list dex can edit"
        )
    for item in sequence.value:
        if (_name(item) or "").lower() != name.lower():
            continue
        if not _inside(item, sequence):
            break
        start, end, indent = _span(text, item)
        return text[:start] + dump_items([entry], indent, Dumper) + text[end:]
    raise UnwritableError(
        f"tables['{table}'].{section}['{name}'] is not a block-style list item dex "
        "can rewrite on its own"
    )


def append_member(text: str, table: str, section: str, entry: dict[str, Any]) -> str:
    """``text`` with ``entry`` appended to the table's ``section``.

    The new item takes the indentation of the list's own items and lands after
    the last one. Where the table has no ``section``, the list is added at the
    end of the table.
    """

    node = _table(text, table)
    present = any(str(k.value).lower() == section.lower() for k, _v in node.value)
    sequence = _section(node, section)
    if not present:
        rendered = dump_items({section: [entry]}, _key_indent(node), Dumper)
        return _insert(text, _item_end(text, node), rendered)
    if (
        sequence is None
        or not sequence.value
        or not _inside(sequence.value[-1], sequence)
    ):
        raise UnwritableError(
            f"tables['{table}'].{section} is not a non-empty block-style list dex "
            "can append to"
        )
    _start, end, indent = _span(text, sequence.value[-1])
    return _insert(text, end, dump_items([entry], indent, Dumper))


# --- nodes and spans -------------------------------------------------------------


def _table(text: str, table: str) -> yaml.MappingNode:
    """The block-style mapping of the logical table ``table``."""
    try:
        root = yaml.compose(text)
    except yaml.YAMLError as exc:
        raise UnwritableError(f"the file is not valid YAML: {exc}") from exc
    sequence = _section(root, "tables")
    if sequence is None:
        raise UnwritableError("the file's `tables` is not a block-style list")
    for item in sequence.value:
        if (_name(item) or "").lower() == table.lower():
            if (
                not isinstance(item, yaml.MappingNode)
                or item.flow_style
                or not _inside(item, sequence)
            ):
                break
            return item
    raise UnwritableError(
        f"tables['{table}'] is not a block-style list item dex can edit in place"
    )


def _section(node: Any, section: str) -> yaml.SequenceNode | None:
    """The block-style sequence under ``section`` of a block mapping, or None."""
    if not isinstance(node, yaml.MappingNode) or node.flow_style:
        return None
    for key, value in node.value:
        if str(key.value).lower() != section.lower():
            continue
        if (
            isinstance(value, yaml.SequenceNode)
            and not value.flow_style
            and _inside(value, node)
        ):
            return value
        return None
    return None


def _inside(node: yaml.Node, container: yaml.Node) -> bool:
    """Whether ``node`` is written inside ``container``.

    A YAML alias composes to its anchor's node, whose marks point at the anchor.
    Editing through an alias would rewrite the anchor, and with it every other
    place that reads it, so an aliased node is never edited in place.
    """

    return (
        container.start_mark.index <= node.start_mark.index
        and node.end_mark.index <= container.end_mark.index
    )


def _name(item: Any) -> str | None:
    """A list item's ``name``, where it is a mapping with one."""
    if not isinstance(item, yaml.MappingNode):
        return None
    for key, value in item.value:
        if key.value == "name" and isinstance(value, yaml.ScalarNode):
            return str(value.value)
    return None


def _line_start(text: str, index: int) -> int:
    return text.rfind("\n", 0, index) + 1


def _span(text: str, item: yaml.Node) -> tuple[int, int, int]:
    """A list item's dash line, its end, and its dash column."""
    if isinstance(item, yaml.MappingNode) and item.flow_style:
        raise UnwritableError("a flow-style list item cannot be edited alone")
    line_start = _line_start(text, item.start_mark.index)
    dash = text.find("-", line_start, item.start_mark.index)
    if dash < 0 or text[line_start:dash].strip():
        raise UnwritableError("a list item does not open its own line")
    indent = dash - line_start
    end = before_trailing_blanks(text, block_end(text, line_start, indent))
    return line_start, end, indent


def _item_end(text: str, node: yaml.MappingNode) -> int:
    """Where the table's list item ends."""
    return _span(text, node)[1]


def _key_indent(node: yaml.MappingNode) -> int:
    """The column the table's keys are written at."""
    if not node.value:
        raise UnwritableError("an empty table mapping has no key column to follow")
    return int(node.value[0][0].start_mark.column)


def _insert(text: str, at: int, rendered: str) -> str:
    """``rendered`` placed at ``at``, on a line of its own."""
    head = text[:at]
    if head and not head.endswith("\n"):
        head += "\n"
    return head + rendered + text[at:]
