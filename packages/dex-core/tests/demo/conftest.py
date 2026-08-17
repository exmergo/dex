"""Fixtures for the demo on-ramp.

`dex demo` resolves its target against the process working directory, because
that is where a person typing it expects a file to appear. Every test here
therefore has to run somewhere disposable, or the suite would leave warehouses
and `.dex/` directories in the checkout.
"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolated_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
