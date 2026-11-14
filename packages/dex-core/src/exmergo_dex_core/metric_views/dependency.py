"""Whether the dialect engine the Metric View reader needs is installed, asked
without importing it.

Stdlib only. The reader resolves a field's or a measure's ``expr`` to the
physical column behind it with sqlglot, which ships with the connector extras
and in ``[metric-views]``, and not in a base install. Asking here costs nothing,
so ``dex config validate`` and every other command still run where the extra is
absent.
"""

from __future__ import annotations

from importlib.util import find_spec

from ..guards.dialect import DialectDependencyError

__all__ = [
    "CONVERTER_COMMIT",
    "METRIC_VIEW_SPEC_VERSION",
    "MetricViewDependencyError",
    "ensure_available",
]

#: The Metric View YAML specification version dex reads. The only one: a
#: definition in another version is refused by name, because the rules a
#: version is checked against are the version's.
METRIC_VIEW_SPEC_VERSION = "1.1"

#: The commit of apache/ossie-converters whose `databricks` spoke the catalog
#: mapping is ported from (its last change under `databricks/`). Recorded beside
#: every fingerprint hash, so a change of mapping is reported as itself.
CONVERTER_COMMIT = "82006ba2"

_INSTALL = 'pip install "exmergo-dex-core[metric-views]"'


class MetricViewDependencyError(DialectDependencyError):
    """The ``[metric-views]`` extra (the dialect engine behind it) is not
    installed.

    A subclass of the dialect refusal, because that is what is missing, with the
    message naming the extra a Metric View user installs. Raised rather than
    degraded around: a field whose column dex cannot resolve is a field the PII
    gate would screen by its name alone, with nothing saying so.
    """


def ensure_available() -> None:
    """Refuse by name unless sqlglot is importable."""

    if find_spec("sqlglot") is None:
        raise MetricViewDependencyError(
            "semantic.vendor: databricks reads Metric View YAML files, and "
            "resolving each field and measure to the column behind it needs "
            "sqlglot, which this install does not have. Install the extra that "
            f"carries it:\n  {_INSTALL}\nAny connector extra carries it too."
        )
