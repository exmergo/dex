"""YAML as a native semantic layer's own reader reads it, shared by the readers
of formats dex parses statically (Databricks Metric Views, Snowflake Semantic
Views).

Three things PyYAML's ``SafeLoader`` does that those readers must not:

- **YAML 1.1 booleans.** ``on``, ``yes`` and ``off`` become ``True`` and
  ``False``. YAML 1.2 (what the vendors' parsers read) keeps them as text, so a
  bare ``on:`` key stays the text ``on``.
- **A key written twice** keeps the last one silently. Which one a deployment
  reads is not something dex will guess, so it is refused.
- **A key that is not text** (``true:``, ``1:``) is accepted. Every key in these
  formats is a name, so it is refused, with a hint where it is what a YAML 1.1
  writer leaves of an ``on:``.

The writer's half (:class:`StrictDumper`) uses the same resolvers, so whatever
dex writes reads back through :func:`read_strict_yaml` unchanged: read and
write with one loader.

:func:`expansion` sizes a parsed document with its aliases followed, linearly,
so a reader can refuse a "billion laughs" file or a recursive alias before it
walks the document.

Pure PyYAML: no vendor package and no dialect engine.
"""

from __future__ import annotations

import re
from typing import Any

import yaml

from .yaml_items import IndentedDumper

__all__ = [
    "MAX_EXPANDED_VALUES",
    "StrictDumper",
    "StrictLoader",
    "dump_strict_yaml",
    "expansion",
    "read_strict_yaml",
]


class StrictLoader(yaml.SafeLoader):
    """SafeLoader with YAML 1.2's booleans, no duplicate keys, text keys only.

    ``document`` names what a refusal says every key of is (a reader subclasses
    this and sets it to its format's name).
    """

    document = "semantic layer document"


StrictLoader.yaml_implicit_resolvers = {
    first: [(tag, rx) for tag, rx in resolvers if tag != "tag:yaml.org,2002:bool"]
    for first, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}
StrictLoader.add_implicit_resolver(
    "tag:yaml.org,2002:bool",
    re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$"),
    list("tTfF"),
)


def _mapping(loader: StrictLoader, node: yaml.MappingNode, deep: bool = False) -> Any:
    """Construct a mapping, refusing a duplicate key and a key that is not text."""
    seen: set[Any] = set()
    for key_node, _ in node.value:
        key = loader.construct_object(key_node, deep=True)
        if not isinstance(key, str):
            hint = (
                ". A YAML 1.1 reader or writer (PyYAML's default, for one) turns a "
                "bare `on:` into `true:`; write the join condition's key as `on:` "
                "again"
                if key is True
                else ""
            )
            raise yaml.constructor.ConstructorError(
                None,
                None,
                f"the key `{key}` is not text, and every {loader.document} key "
                f"is{hint}",
                key_node.start_mark,
            )
        if key in seen:
            raise yaml.constructor.ConstructorError(
                None,
                None,
                f"the key `{key}` is written twice in one mapping",
                key_node.start_mark,
            )
        seen.add(key)
    return loader.construct_mapping(node, deep=deep)


StrictLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)


def read_strict_yaml(text: str, loader: type[StrictLoader] = StrictLoader) -> Any:
    """``text`` parsed with ``loader``: YAML 1.2 booleans, and a duplicate or
    non-text key refused (``yaml.YAMLError``)."""

    return yaml.load(text, Loader=loader)  # noqa: S506 - a SafeLoader subclass


class StrictDumper(IndentedDumper):
    """The writer's half of :class:`StrictLoader`: YAML 1.2 booleans, block
    lists indented under their key.

    PyYAML's own dumper quotes any text a YAML 1.1 reader would take for a
    boolean (``'on'``). With 1.2's resolvers such text is written plain, as the
    vendors' own examples write it, and reads back through
    :func:`read_strict_yaml` as the same text.
    """


StrictDumper.yaml_implicit_resolvers = dict(StrictLoader.yaml_implicit_resolvers)


def dump_strict_yaml(value: Any, dumper: type[StrictDumper] = StrictDumper) -> str:
    """``value`` in block style, keys in the order given, YAML 1.2 scalars, so it
    reads back through :func:`read_strict_yaml` unchanged."""

    return yaml.dump(value, Dumper=dumper, sort_keys=False, allow_unicode=True)


#: How many values a file may expand to once its YAML aliases are followed. A
#: semantic layer document is a few hundred; past this a file is a "billion
#: laughs" shape that would stall every later read of it (a fingerprint
#: serializes it whole).
MAX_EXPANDED_VALUES = 100_000


def expansion(value: Any) -> int | None:
    """How many values ``value`` expands to with aliases followed, or ``None``
    where it contains itself (a recursive alias).

    Each container is visited once: a shared alias is sized once and its size
    reused, so the check is linear in the parsed document, not in its expansion.
    A size saturates at ``MAX_EXPANDED_VALUES + 1``, so a count never grows past
    what the caller compares it with, and cycles are still looked for after it
    saturates.
    """

    sizes: dict[int, int] = {}
    visiting: set[int] = set()

    def size(node: Any) -> int | None:
        if not isinstance(node, (dict, list)):
            return 1
        key = id(node)
        if key in sizes:
            return sizes[key]
        if key in visiting:
            return None
        visiting.add(key)
        total = 1
        for child in node.values() if isinstance(node, dict) else node:
            inner = size(child)
            if inner is None:
                return None
            total = min(total + inner, MAX_EXPANDED_VALUES + 1)
        visiting.discard(key)
        sizes[key] = total
        return total

    return size(value)
