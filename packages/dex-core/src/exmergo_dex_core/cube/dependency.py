"""Whether the dialect engine the Cube reader needs is installed, asked without
importing it.

Stdlib only. The reader resolves a dimension's or a measure's ``sql`` to the
physical column behind it with sqlglot, which ships with the connector extras
and in ``[cube]``, and not in a base install. Asking here costs nothing, so
``dex config validate`` and every non-Cube command still run where the extra is
absent.
"""

from __future__ import annotations

from importlib.util import find_spec

from ..guards.dialect import DialectDependencyError

__all__ = [
    "CUBE_VALIDATOR_RELEASE",
    "CubeDependencyError",
    "ensure_available",
]

#: The Cube release whose model validator (`CubeValidator.ts`, the schema
#: compiler's Joi rules) dex's own rules are ported from. Cube publishes no JSON
#: Schema, so this is the one place its model format is pinned, and the
#: fingerprint records it beside every hash.
CUBE_VALIDATOR_RELEASE = "1.8.14"

_INSTALL = 'pip install "exmergo-dex-core[cube]"'


class CubeDependencyError(DialectDependencyError):
    """The ``[cube]`` extra (the dialect engine behind it) is not installed.

    A subclass of the dialect refusal, because that is what is missing, with
    the message naming the extra a Cube user installs. Raised rather than
    degraded around: a dimension whose column dex cannot resolve is a dimension
    the PII gate would screen by its name alone, with nothing saying so.
    """


def ensure_available() -> None:
    """Refuse by name unless sqlglot is importable."""

    if find_spec("sqlglot") is None:
        raise CubeDependencyError(
            "semantic.vendor: cube reads Cube model files, and resolving each "
            "dimension and measure to the column behind it needs sqlglot, which "
            "this install does not have. Install the extra that carries it:\n"
            f"  {_INSTALL}\n"
            "Any connector extra carries it too."
        )
