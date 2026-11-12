"""Authoring Snowflake Semantic View files through plan/apply (#1292).

What `semantic snowflake define|update|plan` promises: whole configured YAML
files in, the set they would leave validated with dex's port of Snowflake's
specification and validation rules, a reviewable plan stored with no Snowflake
and no warehouse connection, every broken set refused naming the file and the
element, a write surface of exactly the files in `semantic.snowflake.files`,
each logical table and derived metric classified, `transform apply` writing the
authored bytes or nothing at all, and nothing ever deployed.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

pytest.importorskip("sqlglot")

from exmergo_dex_core.cli import main
from exmergo_dex_core.edits import EditOp, SemanticEditTarget, content_hash
from exmergo_dex_core.edits_conformance import SemanticEditTargetContract
from exmergo_dex_core.errors import ProjectError
from exmergo_dex_core.semantic_source import SemanticSourceContext
from exmergo_dex_core.semantic_views.loader import dump_yaml, read_yaml
from exmergo_dex_core.semantic_views.project import SemanticViewSemanticLayer
from exmergo_dex_core.transform.plans import EditKind, PlanEdit

from .conftest import relationship, sv, table

SHOP_FILE = "views/shop.yaml"
STOCK_FILE = "views/stock.yaml"

SHOP = sv(
    "shop",
    table(
        "orders",
        "demo.main.orders",
        ["order_id"],
        dimensions=[{"name": "status", "expr": "status"}],
        facts=[{"name": "amount", "expr": "amount"}],
        metrics=[{"name": "revenue", "expr": "SUM(orders.amount)"}],
    ),
    table(
        "customers",
        "demo.main.customers",
        ["customer_id"],
        dimensions=[{"name": "region", "expr": "region"}],
        metrics=[{"name": "customer_count", "expr": "COUNT(customer_id)"}],
    ),
    relationships=[
        relationship(
            "orders_customers", "orders", "customers", ("customer_id", "customer_id")
        )
    ],
    metrics=[
        {
            "name": "revenue_per_customer",
            "expr": "orders.revenue / customers.customer_count",
        }
    ],
)
STOCK = sv(
    "stock",
    table(
        "items",
        "demo.main.items",
        ["item_id"],
        dimensions=[{"name": "category", "expr": "category"}],
    ),
)


def put(root: Path, name: str, text: str) -> None:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))


def read(root: Path, name: str) -> str:
    return (root / name).read_bytes().decode("utf-8")


def configure(root: Path, files: list[str], vendor: str = "snowflake") -> None:
    (root / ".dex").mkdir(exist_ok=True)
    block = (
        "  snowflake:\n    files:\n" + "".join(f"      - {n}\n" for n in files)
        if vendor == "snowflake"
        else "  cube:\n    files:\n      - model/orders.yml\n"
    )
    (root / ".dex" / "config.yml").write_text(
        f"connector: duckdb\nsemantic:\n  vendor: {vendor}\n" + block,
        encoding="utf-8",
    )


def layered(root: Path, *extra: str) -> Path:
    """Shop and stock on disk, configured with ``extra`` paths too."""

    configure(root, [SHOP_FILE, STOCK_FILE, *extra])
    put(root, SHOP_FILE, dump_yaml(SHOP))
    put(root, STOCK_FILE, dump_yaml(STOCK))
    return root


def with_table(view: dict, name: str, **changes) -> dict:
    """``view`` with one logical table changed."""
    tables = [dict(t, **changes) if t["name"] == name else t for t in view["tables"]]
    return dict(view, tables=tables)


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
        "snowflake",
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


class TestSemanticViewSemanticEditing(SemanticEditTargetContract):
    """The editable and placement guarantees, held by the Semantic View layer."""

    @pytest.fixture(autouse=True)
    def _root(self, tmp_path: Path) -> None:
        self.root = tmp_path

    def _layer(self) -> SemanticViewSemanticLayer:
        put(self.root, SHOP_FILE, dump_yaml(SHOP))
        put(self.root, STOCK_FILE, dump_yaml(STOCK))
        return SemanticViewSemanticLayer.from_context(
            SemanticSourceContext(
                repo_root=str(self.root),
                connector="duckdb",
                options={"files": [SHOP_FILE, STOCK_FILE]},
            )
        )

    def make_semantic_edit_target(self):
        return self._layer()

    def an_edit_against_a_changed_semantic_target(self):
        target = self._layer()
        current = target.semantic_edit_view().files[SHOP_FILE]
        edit = PlanEdit(
            path=SHOP_FILE,
            kind=EditKind.SEMANTIC_DOCUMENT,
            op=EditOp.UPSERT,
            old_content_hash=current.sha256,
            new_content="# proposed\n" + current.content,
        )
        put(self.root, SHOP_FILE, "# edited by hand\n" + current.content)
        return target, [edit], lambda: read(self.root, SHOP_FILE)

    def a_clean_semantic_edit(self, target):
        current = target.semantic_edit_view().files[STOCK_FILE]
        edit = PlanEdit(
            path=STOCK_FILE,
            kind=EditKind.SEMANTIC_DOCUMENT,
            op=EditOp.UPSERT,
            old_content_hash=current.sha256,
            new_content="# clean\n" + current.content,
        )
        return edit, lambda: read(self.root, STOCK_FILE)


def test_the_layer_is_an_edit_target(tmp_path):
    layered(tmp_path)
    layer = SemanticViewSemanticLayer(tmp_path, [SHOP_FILE, STOCK_FILE])
    assert isinstance(layer, SemanticEditTarget)
    assert layer.semantic_editing_surface() == [SHOP_FILE, STOCK_FILE]


# --- define, update, plan --------------------------------------------------------


def test_define_stores_a_plan_and_apply_writes_the_bytes(
    tmp_path, capsys, no_warehouse
):
    layered(tmp_path, "views/lines.yaml")
    authored = (
        "# lines, one row per order line\n"
        "name: lines\n"
        "tables:\n"
        "  - name: lines   # the order lines\n"
        "    base_table: {database: demo, schema: main, table: order_items}\n"
        "    primary_key: {columns: [order_id, line_no]}\n"
        "    facts:\n"
        "      - name: quantity\n"
        "        expr: quantity\n"
        "    metrics:\n"
        "      - name: units\n"
        "        expr: SUM(lines.quantity)\n"
    )

    rc, envelope = plan(capsys, tmp_path, "define", {"views/lines.yaml": authored})

    assert rc == 0, envelope["errors"]
    data = envelope["data"]
    assert data["defined"] == ["lines", "lines.lines"]
    assert any("final word" in note for note in data["notes"])
    assert not (tmp_path / "views/lines.yaml").exists()

    rc, applied = run(capsys, tmp_path, "transform", "apply", data["plan_id"])
    assert rc == 0, applied["errors"]
    assert read(tmp_path, "views/lines.yaml") == authored


def test_update_classifies_tables_and_derived_metrics(tmp_path, capsys):
    layered(tmp_path)
    changed = with_table(SHOP, "orders", description="Placed orders.")
    # Same view, reformatted: flow style and another key order.
    reordered = yaml.safe_dump(
        dict(reversed(list(STOCK.items()))), default_flow_style=True
    )

    rc, envelope = plan(
        capsys,
        tmp_path,
        "update",
        {SHOP_FILE: dump_yaml(changed), STOCK_FILE: reordered},
    )

    assert rc == 0, envelope["errors"]
    data = envelope["data"]
    assert data["updated"] == ["shop.orders"]
    assert data["unchanged"] == [
        "shop",
        "shop.customers",
        "shop.revenue_per_customer",
        "stock",
        "stock.items",
    ]


def test_a_relationship_change_reports_the_view_updated(tmp_path, capsys):
    layered(tmp_path)
    rc, envelope = plan(
        capsys,
        tmp_path,
        "update",
        {SHOP_FILE: dump_yaml(dict(SHOP, description="The shop."))},
    )
    assert rc == 0, envelope["errors"]
    assert envelope["data"]["updated"] == ["shop"]


def test_update_adds_a_table_to_an_existing_view(tmp_path, capsys):
    layered(tmp_path)
    grown = dict(
        STOCK,
        tables=[
            *STOCK["tables"],
            table(
                "bins",
                "demo.main.bins",
                ["bin_id"],
                dimensions=[{"name": "aisle", "expr": "aisle"}],
            ),
        ],
    )
    rc, envelope = plan(capsys, tmp_path, "update", {STOCK_FILE: dump_yaml(grown)})
    assert rc == 0, envelope["errors"]
    assert envelope["data"]["defined"] == ["stock.bins"]


def test_define_refuses_an_existing_view(tmp_path, capsys):
    layered(tmp_path)
    rc, envelope = plan(
        capsys,
        tmp_path,
        "define",
        {SHOP_FILE: dump_yaml(with_table(SHOP, "orders", description="x"))},
    )
    assert rc != 0
    assert "already defined: shop" in errors(envelope)


def test_define_refuses_a_removal(tmp_path, capsys):
    layered(tmp_path)
    shrunk = dict(SHOP, metrics=[])
    rc, envelope = plan(capsys, tmp_path, "define", {SHOP_FILE: dump_yaml(shrunk)})
    assert rc != 0
    assert "removes: shop.revenue_per_customer" in errors(envelope)


def test_update_refuses_a_new_view(tmp_path, capsys):
    layered(tmp_path, "views/lines.yaml")
    rc, envelope = plan(
        capsys,
        tmp_path,
        "update",
        {
            "views/lines.yaml": dump_yaml(
                sv(
                    "lines",
                    table(
                        "lines",
                        "demo.main.order_items",
                        metrics=[{"name": "n", "expr": "COUNT(1)"}],
                    ),
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
            SHOP_FILE: dump_yaml(with_table(SHOP, "orders", description="Placed.")),
            "views/lines.yaml": dump_yaml(
                sv(
                    "lines",
                    table(
                        "lines",
                        "demo.main.order_items",
                        metrics=[{"name": "n", "expr": "COUNT(1)"}],
                    ),
                )
            ),
        },
    )
    assert rc == 0, envelope["errors"]
    assert envelope["data"]["defined"] == ["lines", "lines.lines"]
    assert envelope["data"]["updated"] == ["shop.orders"]


def test_the_subcommand_refuses_another_configured_vendor(tmp_path, capsys):
    configure(tmp_path, [], vendor="cube")
    put(tmp_path, "model/orders.yml", "cubes: []\n")
    rc, envelope = plan(capsys, tmp_path, "plan", {SHOP_FILE: dump_yaml(SHOP)})
    assert rc != 0
    assert "semantic.vendor` is 'cube'" in errors(envelope)


# --- the write surface -----------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("views/other.yaml", "is not configured"),
        ("views/shop_ddl.sql", "is SQL"),
        ("views/shop.json", "not a Semantic View YAML file"),
        ("../outside.yaml", "outside the repository"),
    ],
)
def test_an_unconfigured_path_is_refused_by_name(tmp_path, capsys, path, expected):
    layered(tmp_path)
    rc, envelope = plan(capsys, tmp_path, "plan", {path: dump_yaml(SHOP)})
    assert rc != 0
    assert expected in errors(envelope)
    assert "semantic.snowflake.files" in errors(envelope)


def test_an_unconfigured_path_names_the_command_that_adopts_it(tmp_path, capsys):
    layered(tmp_path)
    rc, envelope = plan(capsys, tmp_path, "plan", {"views/lines.yaml": "x: 1\n"})
    assert rc != 0
    assert (
        "dex config set semantic.snowflake.files "
        f"'[{SHOP_FILE}, {STOCK_FILE}, views/lines.yaml]'"
    ) in errors(envelope)


def test_a_delete_is_refused(tmp_path, capsys):
    layered(tmp_path)
    edits = tmp_path / "edits.json"
    edits.write_text(
        json.dumps({"edits": [{"path": STOCK_FILE, "op": "delete"}]}),
        encoding="utf-8",
    )
    rc, envelope = run(
        capsys,
        tmp_path,
        "semantic",
        "snowflake",
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
        (dict(SHOP, owner="finance"), "unknown_key"),
        (with_table(SHOP, "orders", owner="finance"), "unknown_key"),
        (
            with_table(
                SHOP,
                "orders",
                dimensions=[
                    {
                        "name": "status",
                        "expr": "status",
                        "access_modifier": "private_access",
                    }
                ],
            ),
            "dimension_access",
        ),
        (
            with_table(
                SHOP,
                "orders",
                metrics=[{"name": "revenue", "expr": "SUM(orders.total_price)"}],
            ),
            "unresolved_member",
        ),
        (
            with_table(
                SHOP,
                "orders",
                facts=[{"name": "STATUS", "expr": "amount"}],
            ),
            "duplicate_name",
        ),
    ],
)
def test_a_rule_the_reader_only_warns_on_refuses_the_write(
    tmp_path, capsys, body, rule
):
    layered(tmp_path)
    rc, envelope = plan(capsys, tmp_path, "update", {SHOP_FILE: dump_yaml(body)})
    assert rc != 0, envelope["data"]
    assert f"[{rule}]" in errors(envelope)
    assert SHOP_FILE in errors(envelope)
    assert read(tmp_path, SHOP_FILE) == dump_yaml(SHOP)


def test_a_non_text_key_is_refused(tmp_path, capsys):
    layered(tmp_path)
    broken = dump_yaml(SHOP) + "1: one\n"
    rc, envelope = plan(capsys, tmp_path, "update", {SHOP_FILE: broken})
    assert rc != 0
    assert "[malformed]" in errors(envelope)


def test_a_strict_rule_in_a_file_the_plan_does_not_write_only_warns(tmp_path, capsys):
    layered(tmp_path)
    put(
        tmp_path,
        STOCK_FILE,
        dump_yaml(
            with_table(
                STOCK,
                "items",
                owner="ops",
                metrics=[{"name": "n", "expr": "COUNT(items.missing)"}],
            )
        ),
    )

    rc, envelope = plan(
        capsys,
        tmp_path,
        "update",
        {SHOP_FILE: dump_yaml(with_table(SHOP, "orders", description="x"))},
    )

    assert rc == 0, envelope["errors"]
    warned = " ".join(envelope["warnings"])
    assert "[unknown_key]" in warned
    assert "[unresolved_member]" in warned


def test_an_error_anywhere_in_the_set_refuses_the_plan(tmp_path, capsys):
    layered(tmp_path)
    put(tmp_path, STOCK_FILE, "name: stock\ntables: []\n")
    rc, envelope = plan(
        capsys,
        tmp_path,
        "update",
        {SHOP_FILE: dump_yaml(with_table(SHOP, "orders", description="x"))},
    )
    assert rc != 0
    assert STOCK_FILE in errors(envelope)


def test_a_missing_configured_file_must_be_authored(tmp_path, capsys):
    layered(tmp_path, "views/lines.yaml")
    rc, envelope = plan(
        capsys,
        tmp_path,
        "update",
        {SHOP_FILE: dump_yaml(with_table(SHOP, "orders", description="x"))},
    )
    assert rc != 0
    assert "missing and this plan does not supply them: views/lines.yaml" in errors(
        envelope
    )


# --- apply -----------------------------------------------------------------------


def test_apply_writes_crlf_and_comments_exactly(tmp_path, capsys):
    layered(tmp_path)
    authored = dump_yaml(with_table(SHOP, "orders", description="Placed.")).replace(
        "\n", "\r\n"
    )
    authored = "# kept as written\r\n" + authored
    rc, envelope = plan(capsys, tmp_path, "update", {SHOP_FILE: authored})
    assert rc == 0, envelope["errors"]
    rc, applied = run(
        capsys, tmp_path, "transform", "apply", envelope["data"]["plan_id"]
    )
    assert rc == 0, applied["errors"]
    assert (tmp_path / SHOP_FILE).read_bytes() == authored.encode("utf-8")
    # Read back as authored.
    assert read_yaml(read(tmp_path, SHOP_FILE))["tables"][0]["description"] == "Placed."


def test_a_file_changed_after_planning_is_a_conflict(tmp_path, capsys):
    layered(tmp_path)
    rc, envelope = plan(
        capsys,
        tmp_path,
        "update",
        {SHOP_FILE: dump_yaml(with_table(SHOP, "orders", description="x"))},
    )
    assert rc == 0, envelope["errors"]
    by_hand = "# by hand\n" + dump_yaml(SHOP)
    put(tmp_path, SHOP_FILE, by_hand)

    rc, applied = run(
        capsys, tmp_path, "transform", "apply", envelope["data"]["plan_id"]
    )

    assert applied["status"] == "needs_confirmation", applied
    assert "semantic snowflake plan" in json.dumps(applied)
    assert read(tmp_path, SHOP_FILE) == by_hand


def test_apply_revalidates_the_set_it_would_leave(tmp_path):
    layered(tmp_path)
    layer = SemanticViewSemanticLayer(tmp_path, [SHOP_FILE, STOCK_FILE])
    current = layer.semantic_edit_view().files[SHOP_FILE]
    # The neighbour broke after planning: the set apply would leave is invalid.
    put(tmp_path, STOCK_FILE, "name: stock\ntables: []\n")
    edit = PlanEdit(
        path=SHOP_FILE,
        kind=EditKind.SEMANTIC_DOCUMENT,
        old_content_hash=content_hash(current.content),
        new_content=dump_yaml(with_table(SHOP, "orders", description="x")),
    )

    with pytest.raises(ProjectError, match="nothing was written"):
        layer.write_semantic_edits([edit])
    assert read(tmp_path, SHOP_FILE) == current.content


def test_apply_holds_a_written_file_to_the_strict_rules(tmp_path):
    """A plan document applied elsewhere never ran planning's checks."""

    layered(tmp_path)
    layer = SemanticViewSemanticLayer(tmp_path, [SHOP_FILE, STOCK_FILE])
    current = layer.semantic_edit_view().files[SHOP_FILE]
    edit = PlanEdit(
        path=SHOP_FILE,
        kind=EditKind.SEMANTIC_DOCUMENT,
        old_content_hash=current.sha256,
        new_content=dump_yaml(dict(SHOP, owner="finance")),
    )

    with pytest.raises(ProjectError, match=r"\[unknown_key\]"):
        layer.write_semantic_edits([edit])
    assert read(tmp_path, SHOP_FILE) == current.content


def test_authoring_opens_no_connection(tmp_path, capsys, no_warehouse):
    layered(tmp_path)
    rc, envelope = plan(
        capsys,
        tmp_path,
        "update",
        {SHOP_FILE: dump_yaml(with_table(SHOP, "orders", description="x"))},
    )
    assert rc == 0, envelope["errors"]
    rc, applied = run(
        capsys, tmp_path, "transform", "apply", envelope["data"]["plan_id"]
    )
    assert rc == 0, applied["errors"]


def test_apply_keeps_the_file_mode(tmp_path):
    layered(tmp_path)
    target = tmp_path / SHOP_FILE
    target.chmod(0o640)
    layer = SemanticViewSemanticLayer(tmp_path, [SHOP_FILE, STOCK_FILE])
    current = layer.semantic_edit_view().files[SHOP_FILE]
    result = layer.write_semantic_edits(
        [
            PlanEdit(
                path=SHOP_FILE,
                kind=EditKind.SEMANTIC_DOCUMENT,
                old_content_hash=current.sha256,
                new_content=dump_yaml(with_table(SHOP, "orders", description="x")),
            )
        ]
    )
    assert result.written == [SHOP_FILE]
    assert target.stat().st_mode & 0o777 == 0o640


def test_a_malformed_file_can_be_repaired(tmp_path, capsys):
    """Classification reads the old file as declaring nothing, never as a crash."""
    layered(tmp_path)
    put(tmp_path, STOCK_FILE, "name: stock\ntables: 7\n")
    rc, envelope = plan(capsys, tmp_path, "plan", {STOCK_FILE: dump_yaml(STOCK)})
    assert rc == 0, envelope["errors"]
    assert "stock.items" in envelope["data"]["defined"]


def test_an_alias_bomb_in_the_file_replaced_is_never_expanded(tmp_path, capsys):
    layered(tmp_path)
    bomb = "name: stock\na: &a [x, x, x, x, x, x, x, x, x, x]\n"
    for level in "bcdefgh":
        previous = chr(ord(level) - 1)
        bomb += f"{level}: &{level} [{', '.join([f'*{previous}'] * 10)}]\n"
    bomb += "tables: [*h]\n"
    put(tmp_path, STOCK_FILE, bomb)
    rc, envelope = plan(capsys, tmp_path, "plan", {STOCK_FILE: dump_yaml(STOCK)})
    assert rc == 0, envelope["errors"]


def test_a_failed_move_puts_back_what_was_already_written(tmp_path, monkeypatch):
    layered(tmp_path)
    layer = SemanticViewSemanticLayer(tmp_path, [SHOP_FILE, STOCK_FILE])
    view = layer.semantic_edit_view().files
    before = {name: read(tmp_path, name) for name in (SHOP_FILE, STOCK_FILE)}
    edits = [
        PlanEdit(
            path=name,
            kind=EditKind.SEMANTIC_DOCUMENT,
            old_content_hash=view[name].sha256,
            new_content="# changed\n" + view[name].content,
        )
        for name in (SHOP_FILE, STOCK_FILE)
    ]
    real = Path.replace
    calls = {"n": 0}

    def flaky(self, target):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("disk full")
        return real(self, target)

    monkeypatch.setattr(Path, "replace", flaky)
    with pytest.raises(OSError, match="disk full"):
        layer.write_semantic_edits(edits)
    monkeypatch.setattr(Path, "replace", real)
    assert {name: read(tmp_path, name) for name in (SHOP_FILE, STOCK_FILE)} == before
    # No staged write and no backup copy is left behind.
    assert not list((tmp_path / "views").glob(".*.dex*"))


def test_a_successful_apply_leaves_no_backup_copy(tmp_path):
    layered(tmp_path)
    layer = SemanticViewSemanticLayer(tmp_path, [SHOP_FILE, STOCK_FILE])
    current = layer.semantic_edit_view().files[SHOP_FILE]
    layer.write_semantic_edits(
        [
            PlanEdit(
                path=SHOP_FILE,
                kind=EditKind.SEMANTIC_DOCUMENT,
                old_content_hash=current.sha256,
                new_content="# changed\n" + current.content,
            )
        ]
    )
    assert read(tmp_path, SHOP_FILE).startswith("# changed\n")
    assert not list((tmp_path / "views").glob(".*.dex*"))
