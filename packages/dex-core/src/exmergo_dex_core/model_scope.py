"""The model names a scope argument names.

A scope argument is a list of model names, and the list has more than one
spelling: separate arguments (``a b``), comma-separated (``a,b``), or one
quoted argument with the names separated by spaces (``"a b"``). All three name
the same models, so a caller that quoted the list must not silently narrow the
scope to a model nobody has (#1243).

A model name is a file stem, so it carries no spaces of its own to be confused
with the separator.
"""

from __future__ import annotations

from collections.abc import Iterable


def model_names(values: Iterable[str] | None) -> list[str]:
    """The model names a scope names, in the order they were given.

    Empty when the scope names none, which is how a caller reads "the whole
    project".
    """

    if not values:
        return []
    return [part for raw in values for part in raw.replace(",", " ").split()]
