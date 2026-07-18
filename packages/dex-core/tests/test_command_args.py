"""Config-root resolution: the walk-up that lets `.dex/config.yml` be referenced
from anywhere inside a project, and its boundaries (git-root ceiling, config-file
anchor). The safety-spine tests own the "no silent default" property; these cover
the resolution edge cases directly."""

from __future__ import annotations

import argparse
from pathlib import Path

from exmergo_dex_core import command_args
from exmergo_dex_core.config import DexConfig, save_config


def _root(path: Path | str) -> str:
    return command_args.repo_root(argparse.Namespace(repo_root=str(path)))


def test_walks_up_to_the_owning_config(tmp_path: Path):
    (tmp_path / ".git").mkdir()
    save_config(DexConfig(connector="bigquery"), tmp_path)
    sub = tmp_path / "a" / "b" / "c"
    sub.mkdir(parents=True)
    assert _root(sub) == str(tmp_path.resolve())


def test_config_at_the_run_directory_is_found(tmp_path: Path):
    save_config(DexConfig(connector="bigquery"), tmp_path)
    assert _root(tmp_path) == str(tmp_path.resolve())


def test_cache_only_dex_does_not_shadow_the_real_config(tmp_path: Path):
    # A subdirectory holding only a .dex/ cache (no config.yml) must not stop the
    # walk: the real config higher up owns the tree.
    (tmp_path / ".git").mkdir()
    save_config(DexConfig(connector="bigquery"), tmp_path)
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / ".dex").mkdir()
    (sub / ".dex" / "cache.json").write_text("{}", encoding="utf-8")
    assert _root(sub) == str(tmp_path.resolve())


def test_git_root_is_the_ceiling(tmp_path: Path):
    # config sits ABOVE the git root; the walk must not cross the repo boundary,
    # so it is not adopted and the raw run directory is returned unchanged.
    save_config(DexConfig(connector="bigquery"), tmp_path)
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    sub = repo / "models"
    sub.mkdir()
    assert _root(sub) == str(sub)


def test_no_git_repo_does_not_walk_above_the_run_directory(tmp_path: Path):
    # Without an enclosing git repo the ceiling is the run directory itself, so a
    # config in a parent is never silently adopted.
    save_config(DexConfig(connector="bigquery"), tmp_path)
    sub = tmp_path / "models"
    sub.mkdir()
    assert _root(sub) == str(sub)


def test_not_found_returns_the_raw_root_unchanged(tmp_path: Path):
    (tmp_path / ".git").mkdir()
    assert _root(tmp_path) == str(tmp_path)
