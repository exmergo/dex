"""Where a block of YAML starts and stops, measured by indentation.

The rule every surgical YAML edit in the engine shares: the schema.yml and
semantic rewrites under ``transform/`` and the ``.dex/config.yml`` editor. It
lives on its own, with nothing but the standard library, because the config
editor has to run on a base install that carries no dialect engine, and the
rewrite module that first grew these helpers imports one at the top.

PyYAML's ``end_mark`` on a block collection is where the parser stopped reading,
which is inside the *next* entry, so it cannot bound an entry. Indentation can:
an entry owns every following line indented deeper than the line it starts on.
"""

from __future__ import annotations


def indent_of(content: str, line_start: int) -> int:
    """The number of leading spaces on the line that starts at ``line_start``."""

    rest = content[line_start:]
    return len(rest) - len(rest.lstrip(" "))


def block_end(content: str, line_start: int, indent: int) -> int:
    """Where the block starting at ``line_start`` with ``indent`` stops.

    The first following line that carries content at ``indent`` or shallower. A
    comment counts as content, so a comment at the block's own column belongs to
    what follows it. Blank lines belong to whatever comes next too, so a removal
    does not eat the separator a human put between two entries.
    """

    position = content.find("\n", line_start)
    while position >= 0:
        line_begin = position + 1
        if line_begin >= len(content):
            return len(content)
        line_end = content.find("\n", line_begin)
        line = content[line_begin : len(content) if line_end < 0 else line_end]
        if line.strip():
            depth = len(line) - len(line.lstrip(" "))
            if depth <= indent:
                return line_begin
        position = line_end
    return len(content)


def before_trailing_blanks(content: str, end: int) -> int:
    """``end`` backed up over the blank lines a block span swept up.

    :func:`block_end` walks past blank lines looking for the next line with
    content, so an entry's span reaches past the separator a human wrote after it.
    An insertion at the raw end lands on the far side of that gap, and a removal
    eats it.
    """

    while end > 0 and content[end - 1] == "\n":
        line_start = content.rfind("\n", 0, end - 1) + 1
        if content[line_start : end - 1].strip():
            break
        end = line_start
    return end


def with_leading_comments(content: str, start: int) -> int:
    """``start`` moved up over the comments written directly above its entry.

    The other half of :func:`block_end`'s rule: a comment at a block's own column
    belongs to what follows it, so removing an entry has to take the comment lines
    above it too, or they end up describing its neighbour. Only full-line comments
    at exactly the entry's indent count, with nothing between them and the entry.
    A blank line stops the walk, because a comment set apart by one is a header
    for whatever follows it rather than a note on this entry.
    """

    line_start = content.rfind("\n", 0, start) + 1
    indent = indent_of(content, line_start)
    moved = start
    while line_start > 0:
        above = content.rfind("\n", 0, line_start - 1) + 1
        line = content[above : line_start - 1]
        if not line.lstrip(" ").startswith("#") or indent_of(content, above) != indent:
            break
        line_start = moved = above
    return moved


def sequence_item_span(content: str, item_start: int) -> tuple[int, int] | None:
    """Where one block-sequence item starts and stops, dash line to last line.

    ``item_start`` is where the item's node begins (PyYAML's ``start_mark``).
    Only an item whose dash opens its own line: anything else shares a line with
    something that is not being removed, so ``None``.
    """

    line_start = content.rfind("\n", 0, item_start) + 1
    lead = content[line_start:item_start]
    if lead.strip() != "-" or lead.rstrip().count("-") != 1:
        return None
    return line_start, block_end(content, line_start, indent_of(content, line_start))
