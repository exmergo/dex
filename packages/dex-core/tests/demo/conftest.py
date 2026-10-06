"""Fixtures for the demo on-ramp.

`dex demo` resolves its target against the run directory, which is the process
working directory unless `--repo-root` names another, because the shell is where
a person typing it expects a file to appear. Every test here therefore has to
run somewhere disposable, or the suite would leave warehouses and `.dex/`
directories in the checkout.
"""

from __future__ import annotations

import shlex
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolated_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)


@dataclass(frozen=True)
class RunFrom:
    """Where a demo test stands, and the argv that points the demo at ``root``.

    ``rerun`` is the suffix the printed next steps must carry to resolve the same
    root from the caller's shell, and ``spell`` turns a path under ``root`` into
    the spelling that shell resolves (a live ``--path`` is cwd-relative).
    """

    root: Path
    prefix: tuple[str, ...]
    rerun: str

    def argv(self, *rest: str) -> list[str]:
        return [*self.prefix, *rest]

    def spell(self, relative: str) -> str:
        return relative if not self.prefix else str(self.root / relative)


@pytest.fixture(params=["inside", "outside"])
def run_from(
    request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[RunFrom]:
    """Run the demo from inside its root, or from elsewhere with ``--repo-root``.

    The two have to be indistinguishable on disk and in the envelope, apart from
    the flags the printed commands carry: `dex --repo-root R demo` is `cd R && dex
    demo`. From outside, the caller's own directory must still be empty when the
    test is done, which is the regression this exists to hold.
    """

    root = tmp_path / "project"
    root.mkdir()
    if request.param == "inside":
        monkeypatch.chdir(root)
        yield RunFrom(root=root, prefix=(), rerun="")
        return
    caller = tmp_path / "caller"
    caller.mkdir()
    monkeypatch.chdir(caller)
    yield RunFrom(
        root=root,
        prefix=("--repo-root", str(root)),
        rerun=f" --repo-root {shlex.quote(str(root))}",
    )
    assert sorted(caller.rglob("*")) == [], "dex demo wrote into the caller's cwd"
