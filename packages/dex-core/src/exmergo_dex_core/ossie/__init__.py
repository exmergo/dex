"""Native Apache Ossie support: the document reader, validator, and format.

Apache Ossie (incubating) is a portable interchange format for semantic models.
dex reads it as a **semantic source** behind the seam in
:mod:`..semantic_source`, so no command has to know which vendor answered, and it
depends neither on MetricFlow nor on the upstream Ossie package: the schema it
validates against is vendored and pinned by content hash, and nothing here
requires a checkout of Apache Ossie at runtime.

It is not a project format and cannot be selected as one. A transformation
project owns a model graph, compilation, packages, targets, and a write surface;
Ossie documents own semantic metadata. The two axes sit beside each other, and
`project.format: ossie` is refused rather than translated.

What Ossie is and is not shapes what this package does. It specifies interchange
metadata, not a portable query runtime, so the semantic catalog is read and
generic metric queries are refused rather than invented. Where the specification
is silent, dex records what the document says and declines to infer the rest.

Imports here stay light: the schema validator lives behind the `[ossie]` extra
and the dialect engine behind `[sql]`, and both are imported at the point of use
so `import exmergo_dex_core` costs neither.
"""

from __future__ import annotations

from .dialects import NON_SQL_DIALECTS, PORTABLE_DIALECT, SQL_DIALECTS
from .loader import (
    DOCUMENT_SUFFIXES,
    SCHEMA_SHA256,
    Diagnostic,
    LoadedDocument,
    LoadResult,
    OssieDependencyError,
    load_documents,
)
from .project import VENDOR_NAME, OssieSemanticLayer

__all__ = [
    "DOCUMENT_SUFFIXES",
    "NON_SQL_DIALECTS",
    "PORTABLE_DIALECT",
    "SCHEMA_SHA256",
    "SQL_DIALECTS",
    "VENDOR_NAME",
    "Diagnostic",
    "LoadResult",
    "LoadedDocument",
    "OssieDependencyError",
    "OssieSemanticLayer",
    "load_documents",
]
