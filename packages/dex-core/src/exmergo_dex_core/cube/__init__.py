"""Cube YAML model files as dex's semantic layer (`semantic.vendor: cube`).

Importing this package imports neither sqlglot nor anything Cube ships: dex
reads plain YAML cubes and views statically, validated by its own port of
Cube's model rules, and needs no Node runtime and no Cube server.
"""

from __future__ import annotations

from .dependency import CUBE_VALIDATOR_RELEASE, CubeDependencyError, ensure_available

__all__ = [
    "CUBE_VALIDATOR_RELEASE",
    "CubeDependencyError",
    "ensure_available",
]
