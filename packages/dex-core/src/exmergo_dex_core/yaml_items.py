"""Where one named list item sits in a YAML document, by its YAML marks.

Every surgical writer of a semantic-layer document reads this: Cube's
derivation annotate, which rewrites one cube's list item and leaves every other
byte alone, Cube's `maintain reconcile` removal, which cuts list items out of
one cube or view, and Ossie's derivation, which rewrites one dataset or appends
one beside the others. A file is found by ``yaml.compose`` marks plus the
indentation rule in :mod:`..yaml_spans`, never by re-serializing the file.

Only block style is located. A flow-style list of elements shares its lines
with neighbours, so nothing here can bound one entry in it, and ``None`` says so.
"""

from __future__ import annotations

from typing import Any

import yaml

from .yaml_spans import before_trailing_blanks, block_end

__all__ = ["append_item", "dump_items", "element_node", "locate"]


class _IndentedDumper(yaml.SafeDumper):
    """Block lists indented under their key, the way hand-written YAML is."""

    def increase_indent(self, flow: bool = False, indentless: bool = False):
        return super().increase_indent(flow, False)


def dump_items(value: Any, indent: int = 0) -> str:
    """``value`` as block YAML, every line padded by ``indent`` spaces."""

    dumped = yaml.dump(
        value, Dumper=_IndentedDumper, sort_keys=False, allow_unicode=True
    )
    pad = " " * indent
    return "".join(
        pad + line if line.strip() else line for line in dumped.splitlines(True)
    )


def element_node(
    root: yaml.Node, name: str, section: str = "cubes"
) -> yaml.MappingNode | None:
    """The block-style mapping of the element ``name`` under ``section``."""

    if not isinstance(root, yaml.MappingNode):
        return None
    for key, value in root.value:
        if key.value != section or not isinstance(value, yaml.SequenceNode):
            continue
        if value.flow_style:
            return None
        for item in value.value:
            if not isinstance(item, yaml.MappingNode) or item.flow_style:
                continue
            names = [v.value for k, v in item.value if k.value == "name"]
            if names == [name]:
                return item
    return None


def locate(
    text: str, name: str, section: str = "cubes"
) -> tuple[int, int, int, dict[str, Any]] | None:
    """Where the element ``name``'s list item starts and ends, its dash column,
    and its parsed body; ``None`` where it is not a block-style item."""

    try:
        root = yaml.compose(text)
    except yaml.YAMLError:
        return None
    item = element_node(root, name, section)
    if item is None:
        return None
    line_start = text.rfind("\n", 0, item.start_mark.index) + 1
    dash = text.find("-", line_start, item.start_mark.index)
    if dash < 0 or text[line_start:dash].strip():
        return None
    indent = dash - line_start
    end = before_trailing_blanks(text, block_end(text, line_start, indent))
    # The item's own lines parse as a one-item list, which keeps its
    # indentation consistent where a slice from the first key would not.
    parsed = yaml.safe_load(text[line_start:end])
    if not isinstance(parsed, list) or not isinstance(parsed[0], dict):
        return None
    return line_start, end, indent, parsed[0]


def append_item(text: str, section: str, item: Any) -> str | None:
    """``text`` with ``item`` appended to the root block list ``section``.

    The new item takes the indentation of the list's own items and lands after
    the last one, so every existing byte is kept. Where the root has no
    ``section`` at all, it is added at the end of the document. ``None`` where
    the list is not block style, or the root is not a mapping, since nothing
    here can place an item in a flow-style list without rewriting its line.
    """

    try:
        root = yaml.compose(text)
    except yaml.YAMLError:
        return None
    if not isinstance(root, yaml.MappingNode):
        return None
    sequence = next((value for key, value in root.value if key.value == section), None)
    if sequence is None:
        body = dump_items({section: [item]})
        prefix = text if text.endswith("\n") or not text else text + "\n"
        return prefix + body
    if not isinstance(sequence, yaml.SequenceNode) or sequence.flow_style:
        return None
    if not sequence.value:
        return None
    last = sequence.value[-1]
    line_start = text.rfind("\n", 0, last.start_mark.index) + 1
    dash = text.find("-", line_start, last.start_mark.index)
    if dash < 0 or text[line_start:dash].strip():
        return None
    indent = dash - line_start
    end = before_trailing_blanks(text, block_end(text, line_start, indent))
    rendered = dump_items([item], indent)
    head = text[:end]
    if head and not head.endswith("\n"):
        head += "\n"
    return head + rendered + text[end:]
