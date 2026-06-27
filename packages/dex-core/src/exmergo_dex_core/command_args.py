"""Argument-to-engine bridges shared by the command orchestrators.

These adapt an ``argparse.Namespace`` into the inputs the engine speaks: an open
adapter and a repo root. They live at the command layer, deliberately not in the
engine core, so ``inventory``/``profile``/``rank``/``relationships`` never depend
on argparse. Every ``cmd_*`` module shares them (explore today; transform, model,
and reconcile as they land).
"""

from __future__ import annotations

import argparse

from .adapters.base import Adapter
from .connect import open_adapter


def repo_root(args: argparse.Namespace) -> str:
    return getattr(args, "repo_root", ".")


def open_from_args(args: argparse.Namespace) -> Adapter:
    return open_adapter(
        connector=getattr(args, "connector", None),
        path=getattr(args, "path", None),
        repo_root=repo_root(args),
    )
