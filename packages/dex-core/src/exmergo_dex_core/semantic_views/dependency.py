"""Whether the dialect engine the Semantic View reader needs is installed, asked
without importing it.

Stdlib only. The reader resolves each dimension, fact and metric expression to
the columns and members it reads with sqlglot, which ships with the connector
extras and in ``[semantic-views]``, and not in a base install. Asking here costs
nothing, so ``dex config validate`` and every other command still run where the
extra is absent.
"""

from __future__ import annotations

from importlib.util import find_spec

from ..guards.dialect import DialectDependencyError

__all__ = [
    "SEMANTIC_VIEW_FORMAT_VERSION",
    "SemanticViewDependencyError",
    "ensure_available",
]

#: The identity of the rule set dex ports. Snowflake's Semantic View YAML has no
#: ``version`` key, so the version a fingerprint records is dex's own: the
#: revision of the specification and validation rules this reader was written
#: and qualified against (by ``verify_only`` on a live account). A reader built
#: on a later revision changes it, and ``maintain`` reports the change as itself
#: rather than as every definition changed.
SEMANTIC_VIEW_FORMAT_VERSION = "2026-11"

_INSTALL = 'pip install "exmergo-dex-core[semantic-views]"'


class SemanticViewDependencyError(DialectDependencyError):
    """The ``[semantic-views]`` extra (the dialect engine behind it) is not
    installed.

    A subclass of the dialect refusal, because that is what is missing, with the
    message naming the extra a Semantic View user installs. Raised rather than
    degraded around: a dimension whose column dex cannot resolve is one the PII
    gate would screen by its name alone, with nothing saying so.
    """


def ensure_available() -> None:
    """Refuse by name unless sqlglot is importable."""

    if find_spec("sqlglot") is None:
        raise SemanticViewDependencyError(
            "semantic.vendor: snowflake reads Snowflake Semantic View YAML files, "
            "and resolving each dimension, fact and metric to the columns behind "
            "it needs sqlglot, which this install does not have. Install the "
            f"extra that carries it:\n  {_INSTALL}\nAny connector extra carries "
            "it too."
        )
