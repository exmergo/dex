"""Authoring Databricks Metric View files through plan/apply (#1258).

What `semantic databricks define|update|plan` promises: whole configured YAML
files in, the set they would leave validated with dex's port of the Metric View
YAML 1.1 rules, a reviewable plan stored with no workspace and no warehouse
connection, every broken set refused naming the file and the element, a write
surface of exactly the files in `semantic.databricks.files`, `transform apply`
writing the authored bytes or nothing at all, and nothing ever deployed.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from exmergo_dex_core.cli import main
from exmergo_dex_core.edits import EditOp, SemanticEditTarget, content_hash
from exmergo_dex_core.edits_conformance import SemanticEditTargetContract
from exmergo_dex_core.metric_views.loader import dump_yaml, read_yaml
from exmergo_dex_core.metric_views.project import MetricViewSemanticLayer
from exmergo_dex_core.semantic_source import SemanticSourceContext
from exmergo_dex_core.transform.plans import EditKind, PlanEdit

from .conftest import view

ORDERS_FILE = "views/orders.yaml"
CUSTOMERS_FILE = "views/customers.yaml"

ORDERS = view(
    comment="One row per placed order.",
    joins=[
        {
            "name": "customer",
            "source": "demo.main.customers",
            "on": "source.customer_id = customer.customer_id",
            "rely": {"at_most_one_match": True},
        }
    ],
    fields=[
        {"name": "status", "expr": "status"},
        {"name": "customer_name", "expr": "customer.name"},
    ],
    measures=[{"name": "revenue", "expr": "SUM(amount)"}],
)
CUSTOMERS = view(
    source="demo.main.customers",
    fields=[{"name": "segment", "expr": "segment"}],
    measures=[{"name": "customers", "expr": "COUNT(customer_id)"}],
)


def put(root: Path, name: str, text: str) -> None:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))


def read(root: Path, name: str) -> str:
    return (root / name).read_bytes().decode("utf-8")


def configure(root: Path, files: list[str], vendor: str = "databricks") -> None:
    (root / ".dex").mkdir(exist_ok=True)
    block = (
        "  databricks:\n    files:\n" + "".join(f"      - {n}\n" for n in files)
        if vendor == "databricks"
        else "  cube:\n    files:\n      - model/orders.yml\n"
    )
    (root / ".dex" / "config.yml").write_text(
        f"connector: duckdb\nsemantic:\n  vendor: {vendor}\n" + block,
        encoding="utf-8",
    )


def layered(root: Path, *extra: str) -> Path:
    """Orders and customers on disk, configured with ``extra`` paths too."""

    configure(root, [ORDERS_FILE, CUSTOMERS_FILE, *extra])
    put(root, ORDERS_FILE, dump_yaml(ORDERS))
    put(root, CUSTOMERS_FILE, dump_yaml(CUSTOMERS))
    return root


def payload(root: Path, edits: dict[str, str], **extra) -> str:
    path = root / "edits.json"
    body = {"edits": [{"path": p, "content": c} for p, c in edits.items()], **extra}
    path.write_text(json.dumps(body), encoding="utf-8")
    return str(path)


def run(capsys, root: Path, *argv: str) -> tuple[int, dict]:
    rc = main(["--repo-root", str(root), *argv])
    return rc, json.loads(capsys.readouterr().out)


def plan(capsys, root: Path, mode: str, edits: dict[str, str]) -> tuple[int, dict]:
    return run(
        capsys,
        root,
        "semantic",
        "databricks",
        mode,
        "author views",
        "--edits-file",
        payload(root, edits),
    )


def errors(envelope: dict) -> str:
    return " ".join(envelope["errors"])


@pytest.fixture
def no_warehouse(monkeypatch):
    """Fail the test if a warehouse connection is opened."""

    def refuse(*_args, **_kwargs):
        raise AssertionError("a warehouse connection was opened")

    monkeypatch.setattr("exmergo_dex_core.connect.open_adapter", refuse)


# --- the shipped contract --------------------------------------------------------


class TestMetricViewSemanticEditing(SemanticEditTargetContract):
    """The editable and placement guarantees, held by the Metric View layer."""

    @pytest.fixture(autouse=True)
    def _root(self, tmp_path: Path) -> None:
        self.root = tmp_path

    def _layer(self) -> MetricViewSemanticLayer:
        put(self.root, ORDERS_FILE, dump_yaml(ORDERS))
        put(self.root, CUSTOMERS_FILE, dump_yaml(CUSTOMERS))
        return MetricViewSemanticLayer.from_context(
            SemanticSourceContext(
                repo_root=str(self.root),
                connector="duckdb",
                options={"files": [ORDERS_FILE, CUSTOMERS_FILE]},
            )
        )

    def make_semantic_edit_target(self):
        return self._layer()

    def an_edit_against_a_changed_semantic_target(self):
        target = self._layer()
        current = target.semantic_edit_view().files[ORDERS_FILE]
        edit = PlanEdit(
            path=ORDERS_FILE,
            kind=EditKind.SEMANTIC_DOCUMENT,
            op=EditOp.UPSERT,
            old_content_hash=current.sha256,
            new_content="# proposed\n" + current.content,
        )
        put(self.root, ORDERS_FILE, "# edited by hand\n" + current.content)
        return target, [edit], lambda: read(self.root, ORDERS_FILE)

    def a_clean_semantic_edit(self, target):
        current = target.semantic_edit_view().files[CUSTOMERS_FILE]
        edit = PlanEdit(
            path=CUSTOMERS_FILE,
            kind=EditKind.SEMANTIC_DOCUMENT,
            op=EditOp.UPSERT,
            old_content_hash=current.sha256,
            new_content="# clean\n" + current.content,
        )
        return edit, lambda: read(self.root, CUSTOMERS_FILE)


def test_the_layer_is_an_edit_target(tmp_path):
    layered(tmp_path)
    layer = MetricViewSemanticLayer(tmp_path, [ORDERS_FILE, CUSTOMERS_FILE])
    assert isinstance(layer, SemanticEditTarget)
    assert layer.semantic_editing_surface() == [ORDERS_FILE, CUSTOMERS_FILE]


# --- define, update, plan --------------------------------------------------------


def test_define_stores_a_plan_and_apply_writes_the_bytes(
    tmp_path, capsys, no_warehouse
):
    layered(tmp_path, "views/lines.yaml")
    authored = (
        "# lines, one row per order line\n"
        "version: 1.1\n"
        "source: demo.main.order_items\n"
        "joins:\n"
        "  - name: orders\n"
        "    source: demo.main.orders\n"
        "    on: source.order_id = orders.order_id\n"
        "fields:\n"
        "  - name: category   # the product category\n"
        "    expr: category\n"
        "measures:\n"
        "  - name: quantity\n"
        "    expr: SUM(quantity)\n"
    )
    put(tmp_path, "views/lines.yaml", "")
    (tmp_path / "views/lines.yaml").unlink()

    rc, envelope = plan(capsys, tmp_path, "define", {"views/lines.yaml": authored})

    assert rc == 0, envelope["errors"]
    data = envelope["data"]
    assert data["defined"] == ["lines"]
    assert any("final word" in note for note in data["notes"])
    assert not (tmp_path / "views/lines.yaml").exists()

    rc, applied = run(capsys, tmp_path, "transform", "apply", data["plan_id"])
    assert rc == 0, applied["errors"]
    assert read(tmp_path, "views/lines.yaml") == authored
    # The `on:` key survives as text: no YAML 1.1 round trip happened.
    assert read_yaml(read(tmp_path, "views/lines.yaml"))["joins"][0]["on"]


def test_update_classifies_changed_and_unchanged_views(tmp_path, capsys):
    layered(tmp_path)
    changed = dict(ORDERS, comment="Placed orders.")
    # Same view, reformatted: flow style and another key order.
    reordered = yaml.safe_dump(
        dict(reversed(list(CUSTOMERS.items()))), default_flow_style=True
    )

    rc, envelope = plan(
        capsys,
        tmp_path,
        "update",
        {ORDERS_FILE: dump_yaml(changed), CUSTOMERS_FILE: reordered},
    )

    assert rc == 0, envelope["errors"]
    assert envelope["data"]["updated"] == ["orders"]
    assert envelope["data"]["unchanged"] == ["customers"]


def test_define_refuses_an_existing_view(tmp_path, capsys):
    layered(tmp_path)
    rc, envelope = plan(
        capsys, tmp_path, "define", {ORDERS_FILE: dump_yaml(dict(ORDERS, comment="x"))}
    )
    assert rc != 0
    assert "already defined: orders" in errors(envelope)


def test_update_refuses_a_new_view(tmp_path, capsys):
    layered(tmp_path, "views/lines.yaml")
    rc, envelope = plan(
        capsys,
        tmp_path,
        "update",
        {
            "views/lines.yaml": dump_yaml(
                view(
                    source="demo.main.order_items",
                    measures=[{"name": "n", "expr": "COUNT(1)"}],
                )
            )
        },
    )
    assert rc != 0
    assert "do not exist yet: lines" in errors(envelope)


def test_plan_accepts_a_mix(tmp_path, capsys):
    layered(tmp_path, "views/lines.yaml")
    rc, envelope = plan(
        capsys,
        tmp_path,
        "plan",
        {
            ORDERS_FILE: dump_yaml(dict(ORDERS, comment="Placed orders.")),
            "views/lines.yaml": dump_yaml(
                view(
                    source="demo.main.order_items",
                    measures=[{"name": "lines", "expr": "COUNT(1)"}],
                )
            ),
        },
    )
    assert rc == 0, envelope["errors"]
    assert envelope["data"]["defined"] == ["lines"]
    assert envelope["data"]["updated"] == ["orders"]


def test_the_subcommand_refuses_another_configured_vendor(tmp_path, capsys):
    configure(tmp_path, [], vendor="cube")
    put(tmp_path, "model/orders.yml", "cubes: []\n")
    rc, envelope = plan(capsys, tmp_path, "plan", {ORDERS_FILE: dump_yaml(ORDERS)})
    assert rc != 0
    assert "semantic.vendor` is 'cube'" in errors(envelope)


# --- the write surface -----------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("views/other.yaml", "is not configured"),
        ("views/orders_ddl.sql", "is SQL"),
        ("views/orders.json", "not a Metric View YAML file"),
        ("../outside.yaml", "outside the repository"),
        ("elsewhere/ORDERS.yml", "already names"),
    ],
)
def test_an_unconfigured_path_is_refused_by_name(tmp_path, capsys, path, expected):
    layered(tmp_path)
    rc, envelope = plan(capsys, tmp_path, "plan", {path: dump_yaml(ORDERS)})
    assert rc != 0
    assert expected in errors(envelope)
    assert "semantic.databricks.files" in errors(envelope)


def test_an_unconfigured_path_names_the_command_that_adopts_it(tmp_path, capsys):
    layered(tmp_path)
    rc, envelope = plan(capsys, tmp_path, "plan", {"views/lines.yaml": "x: 1\n"})
    assert rc != 0
    assert (
        "dex config set semantic.databricks.files "
        f"'[{ORDERS_FILE}, {CUSTOMERS_FILE}, views/lines.yaml]'"
    ) in errors(envelope)


def test_a_delete_is_refused(tmp_path, capsys):
    layered(tmp_path)
    edits = tmp_path / "edits.json"
    edits.write_text(
        json.dumps({"edits": [{"path": CUSTOMERS_FILE, "op": "delete"}]}),
        encoding="utf-8",
    )
    rc, envelope = run(
        capsys,
        tmp_path,
        "semantic",
        "databricks",
        "plan",
        "x",
        "--edits-file",
        str(edits),
    )
    assert rc != 0
    assert "does not delete" in errors(envelope)


# --- the rules, held harder on write than on read ---------------------------------


@pytest.mark.parametrize(
    ("body", "rule"),
    [
        (dict(ORDERS, owner="finance"), "unknown_key"),
        (
            dict(
                ORDERS,
                joins=[
                    dict(
                        ORDERS["joins"][0],
                        rely={"at_most_one_match": True, "not_null": True},
                    )
                ],
            ),
            "unknown_key",
        ),
        (
            dict(ORDERS, dimensions=[{"name": "region", "expr": "region"}]),
            "fields_and_dimensions",
        ),
        (
            dict(
                ORDERS,
                measures=[*ORDERS["measures"], {"name": "STATUS", "expr": "COUNT(1)"}],
            ),
            "duplicate_name",
        ),
        (dict(ORDERS, version=0.1), "version"),
    ],
)
def test_a_rule_the_reader_only_warns_on_refuses_the_write(
    tmp_path, capsys, body, rule
):
    layered(tmp_path)
    rc, envelope = plan(capsys, tmp_path, "update", {ORDERS_FILE: dump_yaml(body)})
    assert rc != 0, envelope["data"]
    assert f"[{rule}]" in errors(envelope)
    assert ORDERS_FILE in errors(envelope)
    assert read(tmp_path, ORDERS_FILE) == dump_yaml(ORDERS)


def test_an_unknown_key_in_a_file_the_plan_does_not_write_only_warns(tmp_path, capsys):
    layered(tmp_path)
    put(tmp_path, CUSTOMERS_FILE, dump_yaml(dict(CUSTOMERS, owner="finance")))

    rc, envelope = plan(
        capsys, tmp_path, "update", {ORDERS_FILE: dump_yaml(dict(ORDERS, comment="x"))}
    )

    assert rc == 0, envelope["errors"]
    assert any("[unknown_key]" in w for w in envelope["warnings"])


def test_a_yaml_1_1_round_trip_is_refused_naming_the_on_key(tmp_path, capsys):
    """PyYAML's default dump of a 1.1-loaded view writes `true:` for `on:`."""

    layered(tmp_path)
    broken = yaml.safe_dump(yaml.safe_load(dump_yaml(ORDERS)), sort_keys=False)
    assert "true:" in broken

    rc, envelope = plan(capsys, tmp_path, "update", {ORDERS_FILE: broken})

    assert rc != 0
    assert "`on:`" in errors(envelope)


def test_a_missing_configured_file_must_be_authored(tmp_path, capsys):
    layered(tmp_path, "views/lines.yaml")
    rc, envelope = plan(
        capsys, tmp_path, "update", {ORDERS_FILE: dump_yaml(dict(ORDERS, comment="x"))}
    )
    assert rc != 0
    assert "missing and this plan does not supply them: views/lines.yaml" in errors(
        envelope
    )


# --- apply -----------------------------------------------------------------------


def test_apply_writes_crlf_and_comments_exactly(tmp_path, capsys):
    layered(tmp_path)
    authored = dump_yaml(dict(ORDERS, comment="Placed.")).replace("\n", "\r\n")
    authored = "# kept as written\r\n" + authored
    rc, envelope = plan(capsys, tmp_path, "update", {ORDERS_FILE: authored})
    assert rc == 0, envelope["errors"]
    rc, applied = run(
        capsys, tmp_path, "transform", "apply", envelope["data"]["plan_id"]
    )
    assert rc == 0, applied["errors"]
    assert (tmp_path / ORDERS_FILE).read_bytes() == authored.encode("utf-8")


def test_a_file_changed_after_planning_is_a_conflict(tmp_path, capsys):
    layered(tmp_path)
    rc, envelope = plan(
        capsys, tmp_path, "update", {ORDERS_FILE: dump_yaml(dict(ORDERS, comment="x"))}
    )
    assert rc == 0, envelope["errors"]
    by_hand = "# by hand\n" + dump_yaml(ORDERS)
    put(tmp_path, ORDERS_FILE, by_hand)

    rc, applied = run(
        capsys, tmp_path, "transform", "apply", envelope["data"]["plan_id"]
    )

    assert applied["status"] == "needs_confirmation", applied
    assert "semantic databricks plan" in json.dumps(applied)
    assert read(tmp_path, ORDERS_FILE) == by_hand


def test_apply_revalidates_the_set_it_would_leave(tmp_path):
    layered(tmp_path)
    layer = MetricViewSemanticLayer(tmp_path, [ORDERS_FILE, CUSTOMERS_FILE])
    current = layer.semantic_edit_view().files[ORDERS_FILE]
    # The neighbour broke after planning: the set apply would leave is invalid.
    put(tmp_path, CUSTOMERS_FILE, "version: 9\nsource: x\n")
    edit = PlanEdit(
        path=ORDERS_FILE,
        kind=EditKind.SEMANTIC_DOCUMENT,
        old_content_hash=content_hash(current.content),
        new_content=dump_yaml(dict(ORDERS, comment="x")),
    )
    from exmergo_dex_core.errors import ProjectError

    with pytest.raises(ProjectError, match="nothing was written"):
        layer.write_semantic_edits([edit])
    assert read(tmp_path, ORDERS_FILE) == current.content


def test_authoring_opens_no_connection(tmp_path, capsys, no_warehouse):
    layered(tmp_path)
    rc, envelope = plan(
        capsys, tmp_path, "update", {ORDERS_FILE: dump_yaml(dict(ORDERS, comment="x"))}
    )
    assert rc == 0, envelope["errors"]
    rc, applied = run(
        capsys, tmp_path, "transform", "apply", envelope["data"]["plan_id"]
    )
    assert rc == 0, applied["errors"]


def test_apply_holds_a_written_file_to_the_strict_rules(tmp_path):
    """A plan document applied elsewhere never ran planning's checks."""

    layered(tmp_path)
    layer = MetricViewSemanticLayer(tmp_path, [ORDERS_FILE, CUSTOMERS_FILE])
    current = layer.semantic_edit_view().files[ORDERS_FILE]
    edit = PlanEdit(
        path=ORDERS_FILE,
        kind=EditKind.SEMANTIC_DOCUMENT,
        old_content_hash=current.sha256,
        new_content=dump_yaml(dict(ORDERS, owner="finance")),
    )
    from exmergo_dex_core.errors import ProjectError

    with pytest.raises(ProjectError, match=r"\[unknown_key\]"):
        layer.write_semantic_edits([edit])
    assert read(tmp_path, ORDERS_FILE) == current.content


def test_apply_keeps_the_file_mode(tmp_path):
    layered(tmp_path)
    target = tmp_path / ORDERS_FILE
    target.chmod(0o644)
    layer = MetricViewSemanticLayer(tmp_path, [ORDERS_FILE, CUSTOMERS_FILE])
    current = layer.semantic_edit_view().files[ORDERS_FILE]
    result = layer.write_semantic_edits(
        [
            PlanEdit(
                path=ORDERS_FILE,
                kind=EditKind.SEMANTIC_DOCUMENT,
                old_content_hash=current.sha256,
                new_content=dump_yaml(dict(ORDERS, comment="x")),
            )
        ]
    )
    assert result.written == [ORDERS_FILE]
    assert target.stat().st_mode & 0o777 == 0o644
