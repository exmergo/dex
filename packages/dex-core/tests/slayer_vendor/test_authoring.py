"""Authoring SLayer models through plan/apply (#599).

What `semantic slayer define|update|plan` promises: whole configured model files
in, the set they would leave validated with SLayer's own model, a reviewable
plan stored with no warehouse connection and no SLayer storage opened, every
broken set refused naming the file and the element, a write surface of exactly
`semantic.slayer.models`, and `transform apply` writing the authored bytes or
nothing at all.

Needs the real `[slayer]` extra, so it skips without it; the `tests` CI job
is where it runs.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from exmergo_dex_core.slayer import dependency
from exmergo_dex_core.slayer.dependency import SLAYER_VERSION

if dependency.installed_version() != SLAYER_VERSION:
    pytest.skip(
        f"the [slayer] extra (SLayer {SLAYER_VERSION}) is not installed",
        allow_module_level=True,
    )

from exmergo_dex_core.adapters.project import EditableProject
from exmergo_dex_core.cli import main
from exmergo_dex_core.edits import EditOp, SemanticEditTarget, content_hash
from exmergo_dex_core.edits_conformance import SemanticEditTargetContract
from exmergo_dex_core.semantic_source import SemanticSourceContext
from exmergo_dex_core.slayer.loader import FORMAT_VERSION
from exmergo_dex_core.slayer.project import SlayerSemanticLayer
from exmergo_dex_core.transform.plans import EditKind, PlanEdit

BASE = "semantics/slayer"
ORDERS = "models/wh/orders.yaml"
CUSTOMERS = "models/wh/customers.yaml"


def model(name: str, *, data_source: str = "wh", **fields: Any) -> dict[str, Any]:
    return {
        "version": FORMAT_VERSION,
        "name": name,
        "sql_table": f"demo.main.{name}",
        "data_source": data_source,
        **fields,
    }


def orders(**overrides: Any) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "default_time_dimension": "ordered_at",
        "columns": [
            {"name": "order_id", "type": "INT", "primary_key": True},
            {"name": "customer_id", "type": "INT"},
            {"name": "amount", "type": "DOUBLE"},
            {"name": "ordered_at", "type": "TIMESTAMP"},
        ],
        "measures": [{"name": "revenue", "formula": "sum(amount)"}],
        "joins": [
            {
                "target_model": "customers",
                "join_pairs": [["customer_id", "id"]],
                "cardinality": "many_to_one",
            }
        ],
    }
    fields.update(overrides)
    return model("orders", **fields)


def customers(**overrides: Any) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "columns": [
            {"name": "id", "type": "INT", "primary_key": True},
            {"name": "region"},
        ]
    }
    fields.update(overrides)
    return model("customers", **fields)


def dump(entry: dict[str, Any], comment: str = "") -> str:
    return comment + yaml.safe_dump(entry, sort_keys=False)


def put(root: Path, name: str, text: str) -> Path:
    path = root / BASE / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))
    return path


def read(root: Path, name: str) -> str:
    return (root / BASE / name).read_bytes().decode("utf-8")


def configure(root: Path, models: list[str], vendor: str = "slayer") -> None:
    (root / ".dex").mkdir(exist_ok=True)
    (root / ".dex" / "config.yml").write_text(
        f"connector: duckdb\nsemantic:\n  vendor: {vendor}\n  slayer:\n"
        f"    base_dir: {BASE}\n    models:\n"
        + "".join(f"      - {name}\n" for name in models),
        encoding="utf-8",
    )


def repo(root: Path, models: list[str], **files: str) -> Path:
    """Configure ``models`` and write ``files`` (keyed by attribute-safe name)."""

    configure(root, models)
    for name, text in files.items():
        put(root, name, text)
    return root


def payload(root: Path, edits: dict[str, str]) -> str:
    path = root / "edits.json"
    path.write_text(
        json.dumps({"edits": [{"path": p, "content": c} for p, c in edits.items()]}),
        encoding="utf-8",
    )
    return str(path)


def run(capsys, root: Path, *argv: str) -> tuple[int, dict]:
    rc = main(["--repo-root", str(root), *argv])
    return rc, json.loads(capsys.readouterr().out)


def plan(capsys, root: Path, mode: str, edits: dict[str, str]) -> tuple[int, dict]:
    return run(
        capsys,
        root,
        "semantic",
        "slayer",
        mode,
        "author slayer models",
        "--edits-file",
        payload(root, edits),
    )


def errors(envelope: dict) -> str:
    return " ".join(envelope["errors"])


@pytest.fixture
def no_storage_or_warehouse(monkeypatch):
    """Fail the test if SLayer storage is opened or a warehouse is reached."""

    from slayer.storage import yaml_storage

    def refuse(*_args, **_kwargs):
        raise AssertionError("SLayer storage was opened")

    monkeypatch.setattr(yaml_storage.YAMLStorage, "__init__", refuse)

    def no_connection(*_args, **_kwargs):
        raise AssertionError("a warehouse connection was opened")

    monkeypatch.setattr("exmergo_dex_core.connect.open_adapter", no_connection)


# --- the shipped contract --------------------------------------------------------


class TestSlayerSemanticEditing(SemanticEditTargetContract):
    """The write guarantees, without claiming a transformation-project tier."""

    @pytest.fixture(autouse=True)
    def _root(self, tmp_path: Path) -> None:
        self.root = tmp_path

    def _layer(self) -> SlayerSemanticLayer:
        put(self.root, ORDERS, dump(orders()))
        put(self.root, CUSTOMERS, dump(customers()))
        return SlayerSemanticLayer.from_context(
            SemanticSourceContext(
                repo_root=str(self.root),
                connector="duckdb",
                options={"base_dir": BASE, "models": [ORDERS, CUSTOMERS]},
            )
        )

    def make_semantic_edit_target(self):
        return self._layer()

    def an_edit_against_a_changed_semantic_target(self):
        target = self._layer()
        current = target.semantic_edit_view().files[ORDERS]
        edit = PlanEdit(
            path=ORDERS,
            kind=EditKind.SEMANTIC_DOCUMENT,
            op=EditOp.UPSERT,
            old_content_hash=current.sha256,
            new_content="# proposed\n" + current.content,
        )
        put(self.root, ORDERS, "# rewritten by SLayer\n" + current.content)
        return target, [edit], lambda: read(self.root, ORDERS)

    def a_clean_semantic_edit(self, target):
        current = read(self.root, CUSTOMERS)
        edit = PlanEdit(
            path=CUSTOMERS,
            kind=EditKind.SEMANTIC_DOCUMENT,
            op=EditOp.UPSERT,
            old_content_hash=content_hash(current),
            new_content="# proposed clean\n" + current,
        )
        return edit, lambda: read(self.root, CUSTOMERS)


def test_slayer_claims_the_semantic_edit_capability_not_the_project_tier(tmp_path):
    layer = SlayerSemanticLayer(tmp_path, BASE, [ORDERS])

    assert isinstance(layer, SemanticEditTarget)
    assert not isinstance(layer, EditableProject)
    assert layer.semantic_editing_surface() == [ORDERS]


# --- define, update, plan --------------------------------------------------------


def test_define_plans_a_new_model_and_apply_writes_the_authored_bytes(
    tmp_path, capsys, no_storage_or_warehouse
):
    root = repo(tmp_path, [ORDERS, CUSTOMERS], **{CUSTOMERS: dump(customers())})
    # A comment, a key order no dumper would choose, and CRLF line endings: all
    # of it has to land exactly as authored.
    authored = "# owned by dex\r\n" + dump(orders()).replace("\n", "\r\n")

    rc, planned = plan(capsys, root, "define", {ORDERS: authored})

    assert rc == 0, planned["errors"]
    assert planned["data"]["defined"] == ["orders"]
    assert planned["data"]["updated"] == []
    assert planned["diffs"] and planned["diffs"][0]["path"] == ORDERS
    assert not (root / BASE / ORDERS).exists()
    assert any("byte-for-byte" in w for w in planned["warnings"])
    assert any("dex owns" in n for n in planned["data"]["notes"])

    rc, applied = run(capsys, root, "transform", "apply")

    assert rc == 0, applied["errors"]
    assert applied["data"]["written"] == [ORDERS]
    assert (root / BASE / ORDERS).read_bytes() == authored.encode("utf-8")


def test_update_classifies_an_existing_model_and_leaves_the_others_alone(
    tmp_path, capsys, no_storage_or_warehouse
):
    before_customers = dump(customers())
    root = repo(
        tmp_path,
        [ORDERS, CUSTOMERS],
        **{ORDERS: dump(orders()), CUSTOMERS: before_customers},
    )
    revised = dump(
        orders(measures=[{"name": "revenue", "formula": "sum(amount) / 100"}])
    )

    rc, planned = plan(capsys, root, "update", {ORDERS: revised})
    assert rc == 0, planned["errors"]
    assert planned["data"]["updated"] == ["orders"]
    assert planned["data"]["defined"] == []

    rc, applied = run(capsys, root, "transform", "apply")
    assert rc == 0, applied["errors"]
    assert read(root, ORDERS) == revised
    assert read(root, CUSTOMERS) == before_customers


def test_define_refuses_an_existing_model_and_update_a_new_one(tmp_path, capsys):
    root = repo(tmp_path, [ORDERS, CUSTOMERS], **{CUSTOMERS: dump(customers())})

    rc, refused_update = plan(capsys, root, "update", {ORDERS: dump(orders())})
    assert rc != 0
    assert "semantic slayer define" in errors(refused_update)

    put(root, ORDERS, dump(orders()))
    rc, refused_define = plan(
        capsys, root, "define", {CUSTOMERS: dump(customers(), "# again\n")}
    )
    assert rc != 0
    assert "semantic slayer update" in errors(refused_define)


def test_a_subcommand_naming_another_vendor_is_refused(tmp_path, capsys):
    configure(tmp_path, [ORDERS], vendor="ossie")
    (tmp_path / ".dex" / "config.yml").write_text(
        "connector: duckdb\nsemantic:\n  vendor: ossie\n  ossie:\n"
        "    files: [layer.ossie.yaml]\n",
        encoding="utf-8",
    )

    rc, refused = plan(capsys, tmp_path, "plan", {ORDERS: dump(orders())})

    assert rc != 0
    assert "use `semantic ossie`" in errors(refused)


# --- broken sets are refused, naming the file and the element ----------------------


@pytest.mark.parametrize(
    ("edits", "named"),
    [
        pytest.param(
            {
                ORDERS: dump(
                    orders(
                        joins=[
                            {
                                "target_model": "nowhere",
                                "join_pairs": [["customer_id", "id"]],
                            }
                        ]
                    )
                )
            },
            [ORDERS, "target_model", "nowhere"],
            id="dangling-join-target",
        ),
        pytest.param(
            {
                ORDERS: dump(
                    orders(
                        joins=[
                            {
                                "target_model": "customers",
                                "join_pairs": [["customer_id", "missing_col"]],
                            }
                        ]
                    )
                )
            },
            [ORDERS, "join_pairs", "missing_col"],
            id="join-pair-missing-column",
        ),
        pytest.param(
            {"models/wh/orders.yaml": dump({**orders(), "name": "orders_v2"})},
            [ORDERS, "orders_v2", "models/<data_source>/<name>.yaml"],
            id="path-name-mismatch",
        ),
        pytest.param(
            {ORDERS: dump({**orders(), "version": FORMAT_VERSION - 1})},
            [ORDERS, "version", "stale", str(FORMAT_VERSION)],
            id="stale-version",
        ),
        pytest.param(
            {ORDERS: dump(orders(default_time_dimension="placed_at"))},
            [ORDERS, "default_time_dimension", "placed_at"],
            id="time-dimension-not-a-column",
        ),
        pytest.param(
            {
                ORDERS: dump(
                    orders(measures=[{"name": "revenue", "formula": "sum(amount"}])
                )
            },
            [ORDERS, "measures['revenue'].formula"],
            id="formula-does-not-parse",
        ),
        pytest.param(
            {
                ORDERS: dump(
                    orders(measures=[{"name": "aov", "formula": "gross / count(*)"}])
                )
            },
            [ORDERS, "measures['aov'].formula", "gross"],
            id="formula-names-no-saved-measure",
        ),
    ],
)
def test_a_broken_set_is_refused_naming_file_and_element(
    tmp_path, capsys, edits, named
):
    root = repo(
        tmp_path,
        [ORDERS, CUSTOMERS],
        **{ORDERS: dump(orders()), CUSTOMERS: dump(customers())},
    )

    rc, refused = plan(capsys, root, "plan", edits)

    assert rc != 0
    message = errors(refused)
    assert "no plan was stored" in message
    for fragment in named:
        assert fragment in message, (fragment, message)
    assert not list((root / ".dex").glob("plans/*"))


# --- keys the pinned release does not declare (#620) ------------------------------


def _with_column(column: dict[str, Any]) -> dict[str, Any]:
    base = orders()
    return orders(columns=[*base["columns"][:-1], column])


@pytest.mark.parametrize(
    ("authored", "element"),
    [
        pytest.param(orders(owner="analytics"), "owner", id="model"),
        pytest.param(
            _with_column({"name": "ordered_at", "type": "TIMESTAMP", "nullable": True}),
            "columns['ordered_at'].nullable",
            id="column",
        ),
        pytest.param(
            _with_column(
                {"name": "ordered_at", "type": "TIMESTAMP", "format": {"style": "x"}}
            ),
            "columns['ordered_at'].format.style",
            id="column-format",
        ),
        pytest.param(
            orders(
                measures=[{"name": "revenue", "formula": "sum(amount)", "unit": "$"}]
            ),
            "measures['revenue'].unit",
            id="measure",
        ),
        pytest.param(
            orders(
                aggregations=[
                    {
                        "name": "wavg",
                        "formula": "sum({value} * {weight}) / sum({weight})",
                        "params": [{"name": "weight", "sql": "amount", "default": 1}],
                        "kind": "weighted",
                    }
                ]
            ),
            "aggregations['wavg'].kind",
            id="aggregation",
        ),
        pytest.param(
            orders(
                aggregations=[
                    {
                        "name": "wavg",
                        "formula": "sum({value} * {weight}) / sum({weight})",
                        "params": [{"name": "weight", "sql": "amount", "default": 1}],
                    }
                ]
            ),
            "aggregations['wavg'].params['weight'].default",
            id="aggregation-param",
        ),
        pytest.param(
            orders(
                joins=[
                    {
                        "target_model": "customers",
                        "join_pairs": [["customer_id", "id"]],
                        "cardinality": "many_to_one",
                        "relationship": "foreign",
                    }
                ]
            ),
            "joins[0].relationship",
            id="join",
        ),
    ],
)
def test_a_key_slayer_does_not_declare_is_refused_at_any_level(
    tmp_path, capsys, authored, element
):
    """SLayer's own models ignore an unknown key, so it would validate, be
    written, and do nothing."""

    root = repo(
        tmp_path,
        [ORDERS, CUSTOMERS],
        **{ORDERS: dump(orders()), CUSTOMERS: dump(customers())},
    )

    rc, refused = plan(capsys, root, "update", {ORDERS: dump(authored)})

    assert rc != 0
    message = errors(refused)
    assert "no plan was stored" in message
    assert f"{ORDERS}: {element}: [unknown_key]" in message, message
    assert f"SLayer {SLAYER_VERSION}" in message
    assert not list((root / ".dex").glob("plans/*"))


def _text_amount() -> dict[str, Any]:
    """orders with `amount` declared TEXT, which its `revenue` measure sums."""

    authored = orders()
    for column in authored["columns"]:
        if column["name"] == "amount":
            column["type"] = "TEXT"
    return authored


def test_a_measure_its_column_type_cannot_aggregate_is_refused(tmp_path, capsys):
    """SLayer checks an aggregation against its column's type only when it binds
    a query, so `sum` over a TEXT column would validate, be listed, and fail on
    every query. Authoring refuses it by file and element instead."""

    root = repo(
        tmp_path,
        [ORDERS, CUSTOMERS],
        **{ORDERS: dump(orders()), CUSTOMERS: dump(customers())},
    )

    rc, refused = plan(capsys, root, "update", {ORDERS: dump(_text_amount())})

    assert rc != 0
    message = errors(refused)
    assert "no plan was stored" in message
    assert (
        f"{ORDERS}: measures['revenue'].formula: [aggregation_not_applicable]"
        in message
    ), message
    assert "'sum' is not applicable to a TEXT column" in message
    assert not list((root / ".dex").glob("plans/*"))


def test_a_count_over_a_text_column_is_not_refused(tmp_path, capsys):
    authored = _text_amount()
    authored["measures"] = [{"name": "amounts", "formula": "amount:count"}]
    root = repo(
        tmp_path,
        [ORDERS, CUSTOMERS],
        **{ORDERS: dump(orders()), CUSTOMERS: dump(customers())},
    )

    rc, planned = plan(capsys, root, "update", {ORDERS: dump(authored)})

    assert rc == 0, planned["errors"]


@pytest.mark.parametrize(
    ("column", "formula"),
    [
        ({"name": "amount", "type": "TEXT"}, "amount:sum"),
        ({"name": "amount", "type": "TEXT"}, "amount:count_distinct"),
        ({"name": "amount", "type": "DOUBLE"}, "amount:avg"),
        ({"name": "amount", "type": "DATE"}, "amount:max"),
        ({"name": "amount", "type": "DATE"}, "amount:avg"),
        ({"name": "amount", "type": "BOOLEAN"}, "amount:sum"),
        (
            {"name": "amount", "type": "DOUBLE", "allowed_aggregations": ["sum"]},
            "amount:avg",
        ),
        ({"name": "order_id", "type": "INT", "primary_key": True}, "order_id:sum"),
        ({"name": "order_id", "type": "INT", "primary_key": True}, "order_id:count"),
    ],
)
def test_the_aggregation_check_agrees_with_slayer_s_own_binding_gate(column, formula):
    """dex restates SLayer's query-time gate from its public pieces; this holds
    the two equal, so a release that moves the rule fails here first."""

    from slayer.core.enums import normalize_aggregation_name
    from slayer.core.models import SlayerModel
    from slayer.engine.binding import _column_agg_refusal

    from exmergo_dex_core.slayer.loader import LoadedModel, unaggregatable_measures

    columns = [column]
    if column["name"] != "order_id":
        columns.insert(0, {"name": "order_id", "type": "INT", "primary_key": True})
    slayer_model = SlayerModel(
        **model("orders", columns=columns, measures=[{"name": "m", "formula": formula}])
    )
    found = unaggregatable_measures(LoadedModel(file=ORDERS, model=slayer_model))
    target = slayer_model.get_column(column["name"])
    agg = normalize_aggregation_name(formula.split(":")[1])
    refused = _column_agg_refusal(
        col=target, columns=slayer_model.columns, agg=agg, custom=False
    )

    assert bool(found) == (refused is not None), (found, refused)


def test_metricflow_habits_are_refused_with_what_slayer_uses_instead(tmp_path, capsys):
    """The reproduction in #620: the keys an agent brings from MetricFlow."""

    root = repo(
        tmp_path,
        [ORDERS, CUSTOMERS],
        **{ORDERS: dump(orders()), CUSTOMERS: dump(customers())},
    )
    authored = orders(
        entities=[{"name": "order", "type": "primary", "expr": "order_id"}],
        measures=[
            {
                "name": "revenue",
                "formula": "amount:sum",
                "agg": "sum",
                "agg_time_dimension": "ordered_at",
            }
        ],
    )

    rc, refused = plan(capsys, root, "update", {ORDERS: dump(authored)})

    assert rc != 0
    message = errors(refused)
    for element, instead in [
        ("entities", "primary_key: true"),
        ("measures['revenue'].agg", "amount:sum"),
        ("measures['revenue'].agg_time_dimension", "default_time_dimension"),
    ]:
        assert f"{ORDERS}: {element}: [unknown_key]" in message, message
        assert instead in message
    assert not list((root / ".dex").glob("plans/*"))


def test_every_field_the_pinned_release_declares_is_accepted(
    tmp_path, capsys, no_storage_or_warehouse
):
    """Including `meta`, and the values SLayer caches on a column."""

    root = repo(
        tmp_path,
        [ORDERS, CUSTOMERS],
        **{ORDERS: dump(orders()), CUSTOMERS: dump(customers())},
    )
    authored = orders(
        description="One row per order.",
        meta={"owner": "analytics"},
        columns=[
            {"name": "order_id", "type": "INT", "primary_key": True},
            {"name": "customer_id", "type": "INT", "meta": {"pii": False}},
            {"name": "amount", "type": "DOUBLE", "label": "Amount"},
            {
                "name": "ordered_at",
                "type": "TIMESTAMP",
                "sampled": "2026-10-01",
                "sampled_values": ["2026-10-01"],
                "distinct_count": 31,
            },
        ],
        measures=[{"name": "revenue", "formula": "sum(amount)", "meta": {"k": 1}}],
        joins=[
            {
                "target_model": "customers",
                "join_pairs": [["customer_id", "id"]],
                "cardinality": "many_to_one",
                "meta": {"k": 1},
            }
        ],
    )

    rc, planned = plan(capsys, root, "update", {ORDERS: dump(authored)})

    assert rc == 0, planned["errors"]
    assert not any("unknown_key" in w for w in planned["warnings"])


def test_no_declared_field_reads_as_unknown_at_any_level():
    """Generated from the pinned models themselves, so a release that adds a
    field cannot be refused for it."""

    from pydantic import BaseModel
    from slayer.core.models import SlayerModel

    from exmergo_dex_core.slayer.loader import _unknown_keys

    def nested(annotation: Any) -> list[type[BaseModel]]:
        if isinstance(annotation, type) and issubclass(annotation, BaseModel):
            return [annotation]
        return [m for arg in getattr(annotation, "__args__", ()) for m in nested(arg)]

    def every_field(owner: type[BaseModel], depth: int = 0) -> dict[str, Any]:
        raw: dict[str, Any] = {}
        for name, info in owner.model_fields.items():
            models = nested(info.annotation) if depth < 4 else []
            raw[name] = [every_field(models[0], depth + 1)] if models else None
        return raw

    assert list(_unknown_keys(every_field(SlayerModel), SlayerModel, ())) == []


def test_an_unknown_key_in_a_file_the_plan_does_not_edit_only_warns(tmp_path, capsys):
    """dex refuses to write the defect; it does not refuse to write next to one."""

    root = repo(
        tmp_path,
        [ORDERS, CUSTOMERS],
        **{ORDERS: dump(orders()), CUSTOMERS: dump(customers(owner="crm"))},
    )
    revised = dump(
        orders(measures=[{"name": "revenue", "formula": "sum(amount) / 100"}])
    )

    rc, planned = plan(capsys, root, "update", {ORDERS: revised})

    assert rc == 0, planned["errors"]
    assert any(f"{CUSTOMERS}: owner: [unknown_key]" in w for w in planned["warnings"])


def test_a_duplicate_model_name_refuses_both_files(tmp_path, capsys):
    other = "models/other/orders.yaml"
    root = repo(
        tmp_path,
        [ORDERS, CUSTOMERS, other],
        **{ORDERS: dump(orders()), CUSTOMERS: dump(customers())},
    )

    rc, refused = plan(
        capsys,
        root,
        "define",
        {other: dump(orders(data_source="other", joins=[]))},
    )

    assert rc != 0
    message = errors(refused)
    assert "duplicate_name" in message
    assert ORDERS in message and other in message


@pytest.mark.parametrize(
    ("path", "says"),
    [
        ("datasources/wh.yaml", "connection credentials"),
        ("priority.yaml", "priority.yaml"),
        ("embeddings.db", "search index"),
        ("models/wh/unlisted.yaml", "not configured"),
        ("../escape.yaml", "outside semantic.slayer.base_dir"),
    ],
)
def test_a_path_outside_the_configured_models_is_refused(tmp_path, capsys, path, says):
    root = repo(
        tmp_path,
        [ORDERS, CUSTOMERS],
        **{ORDERS: dump(orders()), CUSTOMERS: dump(customers())},
    )

    rc, refused = plan(capsys, root, "plan", {path: "type: duckdb\n"})

    assert rc != 0
    assert says in errors(refused)
    assert not (root / BASE / path).exists()


@pytest.mark.parametrize(
    "secret_at",
    [f"{BASE}/datasources/wh.yaml", "outside-storage/wh.yaml"],
    ids=["datasource-in-storage", "outside-storage"],
)
def test_a_configured_model_symlinked_away_from_models_is_refused(
    tmp_path, capsys, secret_at
):
    root = repo(tmp_path, [ORDERS, CUSTOMERS], **{CUSTOMERS: dump(customers())})
    secret = tmp_path / secret_at
    secret.parent.mkdir(parents=True)
    secret.write_text("password: hunter2\n", encoding="utf-8")
    link = root / BASE / ORDERS
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(secret)

    rc, refused = plan(capsys, root, "plan", {ORDERS: dump(orders())})

    assert rc != 0
    assert "models/ directory" in errors(refused) or "outside" in errors(refused)
    assert "hunter2" not in json.dumps(refused)
    assert secret.read_text(encoding="utf-8") == "password: hunter2\n"


# --- apply -----------------------------------------------------------------------


def test_a_file_changed_after_planning_refuses_the_whole_multi_file_plan(
    tmp_path, capsys
):
    root = repo(
        tmp_path,
        [ORDERS, CUSTOMERS],
        **{ORDERS: dump(orders()), CUSTOMERS: dump(customers())},
    )
    rc, planned = plan(
        capsys,
        root,
        "update",
        {
            ORDERS: dump(orders(), "# authored orders\n"),
            CUSTOMERS: dump(customers(), "# authored customers\n"),
        },
    )
    assert rc == 0, planned["errors"]
    # What SLayer's storage does on save: rewrite the file in its own layout.
    rewritten = dump(customers(), "# rewritten by slayer serve\n")
    put(root, CUSTOMERS, rewritten)
    orders_before = read(root, ORDERS)

    rc, applied = run(capsys, root, "transform", "apply")

    assert applied["status"] == "needs_confirmation", applied
    assert [c["path"] for c in applied["data"]["conflicts"]] == [CUSTOMERS]
    hint = json.dumps(applied)
    assert "SLayer's own storage" in hint and "semantic slayer plan" in hint
    assert read(root, ORDERS) == orders_before
    assert read(root, CUSTOMERS) == rewritten


def test_formulas_are_skipped_by_name_without_the_sql_extra(
    tmp_path, capsys, monkeypatch
):
    from exmergo_dex_core.slayer import loader

    monkeypatch.setattr(loader, "find_spec", lambda name: None)
    root = repo(
        tmp_path,
        [ORDERS, CUSTOMERS],
        **{ORDERS: dump(orders()), CUSTOMERS: dump(customers())},
    )
    unparseable = orders(measures=[{"name": "revenue", "formula": "sum(amount"}])

    rc, planned = plan(capsys, root, "update", {ORDERS: dump(unparseable)})

    assert rc == 0, planned["errors"]
    assert any(
        "formulas were not parsed" in note and "[sql]" in note
        for note in planned["data"]["notes"]
    )
