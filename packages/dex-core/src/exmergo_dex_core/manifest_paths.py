"""Paths read out of a dbt manifest, spelled the way a plan spells them.

dbt writes ``original_file_path``, ``path`` and ``patch_path`` with the
operating system's separator, so on Windows a model's file reads
``models\\staging\\stg_orders.sql`` while every edit, reference and plan names
it ``models/staging/stg_orders.sql``. ``PurePosixPath(p).as_posix()`` does not
bridge the two (a backslash is an ordinary character to it), so every lookup
keyed by a manifest path goes through here instead.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def posix(path: object) -> str:
    """``path`` with every backslash read as a separator; ``""`` for none."""

    return str(path or "").replace("\\", "/")


def node_file(node: Mapping[str, Any]) -> str:
    """The project-relative file a manifest node was parsed from."""

    return posix(node.get("original_file_path"))


def patch_file(patch_path: object) -> str:
    """The file of a ``patch_path`` (``package://models/schema.yml``), without
    its package, or ``""`` when there is none."""

    if not isinstance(patch_path, str) or not patch_path:
        return ""
    return posix(patch_path.split("://", 1)[-1])
