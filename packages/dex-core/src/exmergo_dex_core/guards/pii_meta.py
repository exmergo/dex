"""The PII marks dex stamps into a dbt project's `meta`, and how they are read back.

One module for both sides, so the keys a writer emits and the keys a reader accepts
cannot drift apart.

dbt moved `meta` under `config:`. A top-level `meta` still parses in dbt-core 1.11
but is deprecated, dbt Fusion rejects it, and one model entry carrying both forms is
a parse error. So dex writes only `config.meta`, and reads both locations with
`config.meta` winning, which keeps a project scaffolded by an older dex working
without a rewrite.

The key names are unchanged from what dex has always written, because tools
outside dex read them.
"""

from __future__ import annotations

from typing import Any

#: The flag dex stamps on a model and on each flagged column.
CONTAINS_PII = "contains_pii"
#: The flagged column's category (`email`, `phone`, ...), stamped beside the flag.
PII_CATEGORY = "pii_category"

#: Every key a reader accepts as marking PII: dex's own two, plus the spellings
#: other tools and hand-written projects use.
PII_MARK_KEYS = ("pii", CONTAINS_PII, "is_pii", PII_CATEGORY)


def entry_meta(entry: Any) -> dict[str, Any]:
    """One YAML entry's effective `meta`: top-level, overlaid by `config.meta`.

    The same precedence dbt applies when it merges the two, so a key a human
    moved under `config` is read where they put it.
    """

    if not isinstance(entry, dict):
        return {}
    merged: dict[str, Any] = {}
    top = entry.get("meta")
    if isinstance(top, dict):
        merged.update(top)
    config = entry.get("config")
    nested = config.get("meta") if isinstance(config, dict) else None
    if isinstance(nested, dict):
        merged.update(nested)
    return merged


def says_pii(meta: Any) -> bool:
    """Whether a `meta` mapping marks PII under any accepted key."""

    return isinstance(meta, dict) and any(bool(meta.get(key)) for key in PII_MARK_KEYS)


def stamp_lines(indent: str, category: str | None = None) -> list[str]:
    """The YAML lines that stamp an entry as PII, under `config.meta`.

    ``indent`` is the entry's field indent. A model-level stamp passes no
    category; a column stamp passes the profiler's. Lines carry no trailing
    newline, so a caller joins them in whatever way its file is built.
    """

    lines = [
        f"{indent}config:",
        f"{indent}  meta:",
        f"{indent}    {CONTAINS_PII}: true",
    ]
    if category is not None:
        lines.append(f"{indent}    {PII_CATEGORY}: {category}")
    return lines
