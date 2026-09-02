# ruff: noqa: E501
"""Catalog-only backend for native Apache Ossie documents."""

from __future__ import annotations

from ...ossie import catalog
from .backend import EXECUTION_DEX, BackendDescriptor, SemanticBackendError
from .catalog import SemanticCatalog


class LocalOssieBackend:
    name = "local"
    vendor = "ossie"
    deployment = "local"
    execution = EXECUTION_DEX
    descriptor = BackendDescriptor(
        name=name,
        vendor=vendor,
        deployment=deployment,
        execution=execution,
        catalog_gaps={
            "query": [
                "Ossie specifies interchange metadata, not a portable query runtime"
            ],
            "values": ["Ossie specifies no distinct-values API"],
            "measures": ["Ossie metrics are expression based"],
            "entities": [
                "Ossie declares explicit relationships rather than MetricFlow entities"
            ],
        },
    )

    def __init__(self, engine) -> None:
        self._engine = engine

    @classmethod
    def from_engine(cls, engine):
        if engine.repo_root is None:
            raise SemanticBackendError(
                "the local Ossie backend needs a repository containing the configured Ossie files"
            )
        return cls(engine)

    def list_definitions(self):
        semantic = self._engine.config.semantic
        view = catalog(
            self._engine.repo_root,
            semantic.ossie.files,
            self._engine.connector or self._engine.config.connector,
        )
        return SemanticCatalog.from_view(view, self)

    def query(self, _q):
        raise SemanticBackendError(
            "Ossie is a semantic interchange format and has no portable query runtime; use explore semantic list, or configure a vendor runtime"
        )

    def values(self, _dimension, _metrics):
        raise SemanticBackendError(
            "Ossie specifies no portable dimension-values API; profile the physical relation instead"
        )

    def filter_refs(self, _clauses):
        return None
