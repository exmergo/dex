"""Argument-to-engine bridges shared by the command orchestrators.

These adapt an ``argparse.Namespace`` into the inputs the engine speaks: an open
adapter and a repo root. They live at the command layer, deliberately not in the
engine core, so ``inventory``/``profile``/``rank``/``relationships`` never depend
on argparse. Every ``cmd_*`` module shares them (explore today; transform,
semantic, and maintain as they land).
"""

from __future__ import annotations

import argparse
from pathlib import Path

from .adapters.base import Adapter
from .connect import open_adapter


def repo_root(args: argparse.Namespace) -> str:
    return getattr(args, "repo_root", ".")


def open_from_args(args: argparse.Namespace) -> Adapter:
    group = getattr(args, "group", None)
    subcommand = getattr(args, "subcommand", None)
    command = " ".join(part for part in (group, subcommand) if part) or None
    return open_adapter(
        connector=getattr(args, "connector", None),
        path=getattr(args, "path", None),
        repo_root=repo_root(args),
        budget=getattr(args, "budget", None),
        confirmed=getattr(args, "confirm", False),
        command=command,
    )


def project_dir(args: argparse.Namespace) -> Path:
    """The dbt project directory: the config pin wins, discovery is the default."""

    from .config import load_config
    from .dbt_project import find_project

    root = repo_root(args)
    config = load_config(root)
    if config and config.dbt_project_dir:
        return Path(root) / config.dbt_project_dir
    return find_project(root)
