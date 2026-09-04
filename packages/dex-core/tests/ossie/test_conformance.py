"""The shipped contracts, subclassed for Ossie.

Five bindings rather than hand-written parallel assertions. The contracts are
the standard the other formats and backends are already held to, they are
already written, and each one is a fixture hook or two away. Subclassing them
also means a later change to the contract reaches this format automatically,
which is exactly what a format built as the second implementation of a seam
needs.

The tier contracts stop where the format's own implementation does. Subclassing
a wider contract than the format implements is how you find out you have not
finished, so a narrow class is a feature: `TestOssieProject` now reaches tier 2
(#409), and stops there rather than also mixing in the tier-3 write contract,
which Ossie does not implement.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from exmergo_dex_core.adapters.conformance import (
    DeclaringProjectContract,
    MaintainProjectContract,
    ProjectFactoryContract,
    SemanticCatalogContract,
    SemanticProjectContract,
)
from exmergo_dex_core.adapters.project import ExploreProject, ProjectContext
from exmergo_dex_core.edits import EditOp, content_hash
from exmergo_dex_core.edits_conformance import SemanticEditTargetContract
from exmergo_dex_core.explore.semantic.conformance import SemanticBackendContract
from exmergo_dex_core.explore.semantic.ossie import LocalOssieBackend
from exmergo_dex_core.ossie import OssieProject
from exmergo_dex_core.transform.plans import EditKind, PlanEdit

from .conftest import dataset, document, expression, field, model, write


def _project(root: Path, *names: str) -> OssieProject:
    return OssieProject.from_context(
        ProjectContext(
            repo_root=str(root), connector="duckdb", options={"files": list(names)}
        )
    )


def _declaring(root: Path, name: str, doc) -> OssieProject:
    write(root, name, doc)
    return _project(root, name)


class TestOssieProject(
    ProjectFactoryContract,
    DeclaringProjectContract,
    MaintainProjectContract,
    SemanticProjectContract,
):
    """Tier 1 and 2, the factory, and the declarations dex reads a project *for*."""

    @pytest.fixture(autouse=True)
    def _root(self, tmp_path: Path) -> None:
        # The contracts call their hooks as plain methods rather than through
        # fixtures, so the root is stashed on the instance for them to reach.
        self.root = tmp_path

    def build(self, context: ProjectContext):
        return OssieProject.from_context(context)

    def empty_context(self) -> ProjectContext:
        """A project with nothing declared in it.

        A document declaring one dataset and nothing about it, which is what
        "nothing declared" means for a format whose documents *are* the
        declarations: an empty file is not a valid Ossie document at all, since
        the schema requires at least one dataset per semantic model.
        """

        write(
            self.root,
            "empty.ossie.yaml",
            document(model("empty", dataset("thing", "demo.main.thing"))),
        )
        return ProjectContext(
            repo_root=str(self.root),
            connector="duckdb",
            options={"files": ["empty.ossie.yaml"]},
        )

    def make_unreadable_project(self) -> ExploreProject:
        """A document the format genuinely cannot parse.

        Overridden rather than left to skip, because this is the hook behind the
        contract's most valuable assertion and Ossie has a real unparseable
        state: a YAML file is a file, and files get truncated, merged badly, and
        hand-edited.
        """

        (self.root / "unreadable.ossie.yaml").write_text(
            "version: '0.2.0.dev0'\nsemantic_model: [ {name: x,\n", encoding="utf-8"
        )
        return _project(self.root, "unreadable.ossie.yaml")

    def a_project_declaring_a_unique_key(self):
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

    def a_project_declaring_a_join(self):
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

    def a_project_declaring_a_join_with_differently_named_sides(self):
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

    def a_project_declaring_a_semantic_model(self):
        """One direct field and one computed, so the snapshot's column mapping
        is asserted in both directions, the same reason the catalog hook one
        section over uses the same shape."""

        project = _declaring(
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
            project,
            "snap.orders",
            {"order_id": "order_id", "net_total": None},
            {},
        )

    def a_project_declaring_a_composite_key(self):
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


class TestOssieSemanticCatalog(SemanticCatalogContract):
    """The read catalog keeps what a drift fingerprint would reduce away."""

    @pytest.fixture(autouse=True)
    def _root(self, tmp_path: Path) -> None:
        self.root = tmp_path

    def make_project(self):
        write(
            self.root,
            "catalog.ossie.yaml",
            document(model("c", dataset("thing", "demo.main.thing", field("a")))),
        )
        return _project(self.root, "catalog.ossie.yaml")

    def a_project_declaring_a_semantic_model(self):
        """One direct field and one computed, so the column assertion runs in
        both directions: `None` is the honest answer for an expression, and an
        invented column is what makes the PII gate screen the wrong one."""

        project = _declaring(
            self.root,
            "semantics.ossie.yaml",
            document(
                model(
                    "shop",
                    dataset(
                        "orders",
                        "demo.main.orders",
                        field("order_id"),
                        field("net_total", "order_total - discount"),
                        primary_key=["order_id"],
                    ),
                    metrics=[
                        {
                            "name": "revenue",
                            "expression": expression(ANSI_SQL="SUM(orders.net_total)"),
                        }
                    ],
                )
            ),
        )
        return (
            project,
            "shop.orders",
            {"order_id": "order_id", "net_total": None},
            {},
        )


class TestLocalOssieBackend(SemanticBackendContract):
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
        return LocalOssieBackend(_project(self.root, "backend.ossie.yaml"))


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
        return _project(self.root, first, second), first, second

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
        (self.root / first).write_text(
            "# human edit\n" + original, encoding="utf-8"
        )
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
