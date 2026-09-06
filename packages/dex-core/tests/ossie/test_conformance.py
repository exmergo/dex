"""The shipped contracts, subclassed for Ossie.

Bindings rather than hand-written parallel assertions. The contracts are the
standard the other sources and layers are already held to, they are already
written, and each one is a hook or two away. Subclassing them also means a later
change to a contract reaches this implementation automatically, which is what a
second implementation of a seam needs.

**Semantic-source contracts, not project ones.** Ossie is a semantic layer: it
owns no model graph, no compilation, no targets, and no dbt write surface, so
the project tiers are the wrong standard for it and satisfying them would be
claiming capabilities it does not have. What it does own is a set of
declarations, a read catalog, a drift fingerprint, and a write surface over its
own configured documents, and there is a contract for each. The assertions are
the same ones the project contracts run, extracted rather than copied, so dbt and
Ossie cannot drift apart on behaviour they share.

Which contracts are absent is the other half of the statement.
`SemanticCatalogContract` from `explore.semantic.conformance` is not here: it
asserts the content of a dbt-shaped reference layer, and Ossie has no entities,
no measures, and no metric groupability to answer it with. Manufacturing them to
pass would be inventing a layer the document's author never wrote. The catalog
*shape* rules that do apply reach Ossie through `SemanticBackendContract` and the
source contracts below, and the applicability boundary is written down in
`references/ossie-compatibility.md` rather than left as a gap someone rediscovers.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from exmergo_dex_core.edits import EditOp, content_hash
from exmergo_dex_core.edits_conformance import SemanticEditTargetContract
from exmergo_dex_core.explore.semantic.conformance import SemanticBackendContract
from exmergo_dex_core.explore.semantic.ossie import LocalOssieBackend
from exmergo_dex_core.ossie import OssieSemanticLayer
from exmergo_dex_core.semantic_source import SemanticSourceContext
from exmergo_dex_core.semantic_source_conformance import (
    SemanticCatalogSourceContract,
    SemanticDeclarationContract,
    SemanticFingerprintContract,
    SemanticSourceFactoryContract,
)
from exmergo_dex_core.transform.plans import EditKind, PlanEdit

from .conftest import dataset, document, expression, field, model, write


def _source(root: Path, *names: str) -> OssieSemanticLayer:
    return OssieSemanticLayer.from_context(
        SemanticSourceContext(
            repo_root=str(root), connector="duckdb", options={"files": list(names)}
        )
    )


def _declaring(root: Path, name: str, doc) -> OssieSemanticLayer:
    write(root, name, doc)
    return _source(root, name)


class TestOssieSemanticSource(
    SemanticSourceFactoryContract,
    SemanticDeclarationContract,
    SemanticFingerprintContract,
    SemanticCatalogSourceContract,
):
    """Construction, declarations, the fingerprint, and the read catalog."""

    @pytest.fixture(autouse=True)
    def _root(self, tmp_path: Path) -> None:
        # The contracts call their hooks as plain methods rather than through
        # fixtures, so the root is stashed on the instance for them to reach.
        self.root = tmp_path

    def build_source(self, context: SemanticSourceContext):
        return OssieSemanticLayer.from_context(context)

    def empty_source_context(self) -> SemanticSourceContext:
        """A layer with nothing declared in it.

        A document declaring one dataset and nothing about it, which is what
        "nothing declared" means for a source whose documents *are* the
        declarations: an empty file is not a valid Ossie document at all, since
        the schema requires at least one dataset per semantic model.
        """

        write(
            self.root,
            "empty.ossie.yaml",
            document(model("empty", dataset("thing", "demo.main.thing"))),
        )
        return SemanticSourceContext(
            repo_root=str(self.root),
            connector="duckdb",
            options={"files": ["empty.ossie.yaml"]},
        )

    def absent_document_context(self) -> SemanticSourceContext:
        """A configured document that is not on disk.

        Overridden rather than left to skip: this is the ordinary state between
        committing a path to config and authoring the file, and refusing it at
        construction would make `explore map` on a raw warehouse fail over a typo
        in a semantic-layer path.
        """

        return SemanticSourceContext(
            repo_root=str(self.root),
            connector="duckdb",
            options={"files": ["absent.ossie.yaml"]},
        )

    def make_semantic_source(self):
        return self.build_source(self.empty_source_context())

    def an_unreadable_semantic_source(self) -> OssieSemanticLayer:
        """A document the source genuinely cannot parse.

        Overridden rather than left to skip, because Ossie has a real unparseable
        state: a YAML file is a file, and files get truncated, merged badly, and
        hand-edited.
        """

        (self.root / "unreadable.ossie.yaml").write_text(
            "version: '0.2.0.dev0'\nsemantic_model: [ {name: x,\n", encoding="utf-8"
        )
        return _source(self.root, "unreadable.ossie.yaml")

    def test_the_declaration_channel_never_raises_on_an_unreadable_document(
        self,
    ) -> None:
        """The asymmetry the catalog contract's own assertion implies, asserted
        from the other side.

        A caller reading the catalog asked what the layer contains, so refusing
        is the answer to their question. A caller on the declaration channel
        asked about a *warehouse* and happens to have a layer beside it, and
        exploration runs against raw warehouses where a semantic layer is absent
        or broken. Raising there turns an ordinary condition into an outage.
        """

        definitions = self.an_unreadable_semantic_source().declared_definitions()

        assert definitions.declared_keys == []
        assert definitions.notes, (
            "an empty result with no note is indistinguishable from a layer that "
            "genuinely declares nothing"
        )

    def a_source_declaring_a_unique_key(self):
        return (
            _declaring(
                self.root,
                "key.ossie.yaml",
                document(
                    model(
                        "k",
                        dataset(
                            "orders",
                            "demo.main.orders",
                            field("order_id"),
                            primary_key=["order_id"],
                        ),
                    )
                ),
            ),
            "k.orders",
            "order_id",
        )

    def a_source_declaring_a_join(self):
        return (
            _declaring(
                self.root,
                "join.ossie.yaml",
                document(
                    model(
                        "j",
                        dataset(
                            "orders",
                            "demo.main.orders",
                            field("customer_id"),
                        ),
                        dataset(
                            "customers",
                            "demo.main.customers",
                            field("customer_id"),
                            primary_key=["customer_id"],
                        ),
                        relationships=[
                            {
                                "name": "r",
                                "from": "orders",
                                "to": "customers",
                                "from_columns": ["customer_id"],
                                "to_columns": ["customer_id"],
                            }
                        ],
                    )
                ),
            ),
            "j.orders",
            "customer_id",
            "j.customers",
            "customer_id",
        )

    def a_source_declaring_a_join_with_differently_named_sides(self):
        """The ordinary case, not the exotic one.

        A mirrored fixture cannot fail for the right reason: an implementation
        that reads one side and copies it onto the other satisfies it exactly.
        """

        return (
            _declaring(
                self.root,
                "sides.ossie.yaml",
                document(
                    model(
                        "s",
                        dataset("orders", "demo.main.orders", field("buyer_ref")),
                        dataset(
                            "customers",
                            "demo.main.customers",
                            field("customer_id"),
                            primary_key=["customer_id"],
                        ),
                        relationships=[
                            {
                                "name": "r",
                                "from": "orders",
                                "to": "customers",
                                "from_columns": ["buyer_ref"],
                                "to_columns": ["customer_id"],
                            }
                        ],
                    )
                ),
            ),
            "s.orders",
            "buyer_ref",
            "s.customers",
            "customer_id",
        )

    def a_source_declaring_a_semantic_model(self):
        """One direct field and one computed, so the snapshot's column mapping
        is asserted in both directions, the same reason the catalog hook one
        section over uses the same shape."""

        source = _declaring(
            self.root,
            "snapshot.ossie.yaml",
            document(
                model(
                    "snap",
                    dataset(
                        "orders",
                        "demo.main.orders",
                        field("order_id"),
                        field("net_total", "order_total - discount"),
                        primary_key=["order_id"],
                    ),
                )
            ),
        )
        return (
            source,
            "snap.orders",
            {"order_id": "order_id", "net_total": None},
            {},
        )

    def a_source_declaring_a_composite_key(self):
        """Three columns, because a pair cannot tell you what you came to find
        out: an implementation that special-cases the pair passes a two-column
        fixture and fails a four-column one."""

        return (
            _declaring(
                self.root,
                "composite.ossie.yaml",
                document(
                    model(
                        "c",
                        dataset(
                            "allocations",
                            "demo.main.allocations",
                            field("order_id"),
                            field("line_no"),
                            field("warehouse_id"),
                            primary_key=["order_id", "line_no", "warehouse_id"],
                        ),
                    )
                ),
            ),
            "c.allocations",
            ("order_id", "line_no", "warehouse_id"),
        )


class TestLocalOssieLayer(SemanticBackendContract):
    """Provenance, idempotency, declared scope, and the payload rules."""

    @pytest.fixture(autouse=True)
    def _root(self, tmp_path: Path) -> None:
        self.root = tmp_path

    def make_backend(self):
        write(
            self.root,
            "backend.ossie.yaml",
            document(
                model(
                    "b",
                    dataset(
                        "orders",
                        "demo.main.orders",
                        field("order_id"),
                        primary_key=["order_id"],
                    ),
                    # Two metrics, because the contract's cap assertion needs
                    # something to cut before it can check that the cut was counted.
                    metrics=[
                        {
                            "name": "order_count",
                            "expression": expression(ANSI_SQL="COUNT(orders.order_id)"),
                        },
                        {
                            "name": "max_order_id",
                            "expression": expression(ANSI_SQL="MAX(orders.order_id)"),
                        },
                    ],
                )
            ),
        )
        return LocalOssieBackend(_source(self.root, "backend.ossie.yaml"))


class TestOssieSemanticEditing(SemanticEditTargetContract):
    """The write guarantees, without claiming a transformation-project tier."""

    @pytest.fixture(autouse=True)
    def _root(self, tmp_path: Path) -> None:
        self.root = tmp_path

    def _documents(self):
        first = "first.ossie.yaml"
        second = "second.ossie.yaml"
        write(
            self.root,
            first,
            document(model("first", dataset("things", "demo.main.things"))),
        )
        write(
            self.root,
            second,
            document(model("second", dataset("things", "demo.main.things"))),
        )
        return _source(self.root, first, second), first, second

    def make_semantic_edit_target(self):
        target, _first, _second = self._documents()
        return target

    def an_edit_against_a_changed_semantic_target(self):
        target, first, _second = self._documents()
        view = target.semantic_edit_view()
        original = view.files[first].content
        edit = PlanEdit(
            path=first,
            kind=EditKind.SEMANTIC_DOCUMENT,
            op=EditOp.UPSERT,
            old_content_hash=view.files[first].sha256,
            new_content="# proposed\n" + original,
        )
        (self.root / first).write_text("# human edit\n" + original, encoding="utf-8")
        return target, [edit], lambda: (self.root / first).read_text("utf-8")

    def a_clean_semantic_edit(self, target):
        second = "second.ossie.yaml"
        current = (self.root / second).read_text("utf-8")
        edit = PlanEdit(
            path=second,
            kind=EditKind.SEMANTIC_DOCUMENT,
            op=EditOp.UPSERT,
            old_content_hash=content_hash(current),
            new_content="# proposed clean\n" + current,
        )
        return edit, lambda: (self.root / second).read_text("utf-8")
