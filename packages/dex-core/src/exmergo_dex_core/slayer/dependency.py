"""Whether the pinned SLayer release is installed, answered without importing it.

Stdlib only, and that is the point of this module being separate: a base install
has no SLayer, and a base SLayer pulls FastAPI, uvicorn, mcp, duckdb, jafgen and
tantivy. The check reads installed distribution metadata and the import system's
finder, so asking "is SLayer here" costs none of that, and nothing that merely
asks can break on an install without it.
"""

from __future__ import annotations

from importlib import metadata
from importlib.util import find_spec

from ..errors import DexError

__all__ = [
    "SLAYER_DISTRIBUTION",
    "SLAYER_VERSION",
    "SlayerDependencyError",
    "ensure_available",
    "installed_version",
]

#: The distribution the `[slayer]` extra installs. It imports as `slayer`, and
#: the distribution named `slayer` on PyPI is an unrelated project, which is why
#: the check below asks for this name rather than for whatever answers to
#: `import slayer`.
SLAYER_DISTRIBUTION = "motley-slayer"

#: The one SLayer release dex reads. Exact, and held equal to the `[slayer]` pin
#: in pyproject.toml by a test: SLayer's stored model format is versioned and
#: moves between releases, so a release dex was not built against is refused
#: rather than read.
SLAYER_VERSION = "1.2.0"

_INSTALL = 'pip install "exmergo-dex-core[slayer]"'


class SlayerDependencyError(DexError):
    """The `[slayer]` extra is not installed, or holds a SLayer release dex does
    not read.

    Raised rather than degraded around, for the reason
    `ossie.loader.OssieDependencyError` is: a model dex cannot validate is a model
    dex cannot promise is well formed. A refusal the caller fixes by installing
    something, so the envelope reports it as a prerequisite.
    """


def installed_version() -> str | None:
    """The installed SLayer release, or None when there is none."""

    try:
        return metadata.version(SLAYER_DISTRIBUTION)
    except metadata.PackageNotFoundError:
        return None


def ensure_available() -> None:
    """Refuse by name unless the pinned SLayer release is importable.

    Two conditions, each with its own fix. No `motley-slayer` at all is the
    missing extra. A different release is an environment that resolved SLayer
    some other way (installed by hand, or by another tool sharing the
    environment), and reading its format as though it were the pinned one is the
    failure the pin exists to prevent.
    """

    version = installed_version()
    if version is None or find_spec("slayer") is None:
        raise SlayerDependencyError(
            "semantic.vendor: slayer reads SLayer model files, and SLayer is not "
            "installed. Install the extra that carries it:\n"
            f"  {_INSTALL}\n"
            "dex validates each model against SLayer's own model format before "
            "reading it, and there is no weaker check to fall back to."
        )
    if version != SLAYER_VERSION:
        raise SlayerDependencyError(
            f"semantic.vendor: slayer needs SLayer {SLAYER_VERSION}, and this "
            f"environment has {SLAYER_DISTRIBUTION} {version}. SLayer's model "
            "format moves between releases, so dex reads only the release it "
            "pins. Reinstall the extra to get it:\n"
            f"  {_INSTALL}"
        )
