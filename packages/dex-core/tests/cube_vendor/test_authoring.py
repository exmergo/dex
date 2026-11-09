"""Authoring Cube model files through plan/apply (#911).

What `semantic cube define|update|plan` promises: whole configured YAML files in,
the set they would leave validated with dex's port of Cube's model rules, a
reviewable plan stored with no Cube server and no warehouse connection, every
broken set refused naming the file and the element, a write surface of exactly
the plain YAML files in `semantic.cube.files`, and `transform apply` writing the
authored bytes or nothing at all.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from exmergo_dex_core.adapters.project import EditableProject
from exmergo_dex_core.cli import main
from exmergo_dex_core.cube.loader import EXTENDS_REASON, JINJA_REASON, JS_REASON
from exmergo_dex_core.cube.project import CubeSemanticLayer
from exmergo_dex_core.edits import EditOp, SemanticEditTarget, content_hash
from exmergo_dex_core.edits_conformance import SemanticEditTargetContract
from exmergo_dex_core.semantic_source import SemanticSourceContext
from exmergo_dex_core.transform.plans import EditKind, PlanEdit

from .conftest import CUSTOMERS, ORDERS, cube, view

ORDERS_FILE = "model/orders.yml"
CUSTOMERS_FILE = "model/customers.yml"
VIEWS_FILE = "model/views.yml"


def dump(*cubes: dict[str, Any], views: tuple = (), comment: str = "") -> str:
    body: dict[str, Any] = {}
    if cubes:
        body["cubes"] = list(cubes)
    if views:
        body["views"] = list(views)
    return comment + yaml.safe_dump(body, sort_keys=False)


def put(root: Path, name: str, text: str) -> None:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))


def read(root: Path, name: str) -> str:
    return (root / name).read_bytes().decode("utf-8")


def configure(root: Path, files: list[str], vendor: str = "cube") -> None:
    (root / ".dex").mkdir(exist_ok=True)
    (root / ".dex" / "config.yml").write_text(
        f"connector: duckdb\nsemantic:\n  vendor: {vendor}\n  cube:\n    files:\n"
        + "".join(f"      - {name}\n" for name in files),
        encoding="utf-8",
    )


def repo(root: Path, files: list[str], **contents: str) -> Path:
    configure(root, files)
    for name, text in contents.items():
        put(root, name, text)
    return root


def layered(root: Path, *extra: str) -> Path:
    """Orders and customers on disk, configured with ``extra`` paths too."""

    return repo(
        root,
        [ORDERS_FILE, CUSTOMERS_FILE, *extra],
        **{ORDERS_FILE: dump(ORDERS), CUSTOMERS_FILE: dump(CUSTOMERS)},
    )


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
        "cube",
        mode,
        "author cubes",
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


class TestCubeSemanticEditing(SemanticEditTargetContract):
    """The write guarantees, without claiming a transformation-project tier."""

    @pytest.fixture(autouse=True)
    def _root(self, tmp_path: Path) -> None:
        self.root = tmp_path

    def _layer(self) -> CubeSemanticLayer:
        put(self.root, ORDERS_FILE, dump(ORDERS))
        put(self.root, CUSTOMERS_FILE, dump(CUSTOMERS))
        return CubeSemanticLayer.from_context(
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
        current = read(self.root, CUSTOMERS_FILE)
        edit = PlanEdit(
            path=CUSTOMERS_FILE,
            kind=EditKind.SEMANTIC_DOCUMENT,
            op=EditOp.UPSERT,
            old_content_hash=content_hash(current),
            new_content="# proposed clean\n" + current,
        )
        return edit, lambda: read(self.root, CUSTOMERS_FILE)


def test_cube_claims_the_semantic_edit_capability_not_the_project_tier(tmp_path):
    layer = CubeSemanticLayer(tmp_path, [ORDERS_FILE, "model/legacy.js"])

    assert isinstance(layer, SemanticEditTarget)
    assert not isinstance(layer, EditableProject)
    # A JavaScript model is part of the layer, and never on the write surface.
    assert layer.semantic_editing_surface() == [ORDERS_FILE]


# --- define, update, plan --------------------------------------------------------


def test_define_plans_a_new_file_and_apply_writes_the_authored_bytes(
    tmp_path, capsys, no_warehouse
):
    root = repo(
        tmp_path, [ORDERS_FILE, CUSTOMERS_FILE], **{CUSTOMERS_FILE: dump(CUSTOMERS)}
    )
    # A comment, camelCase keys, and CRLF line endings: all of it lands as is.
    authored = "# owned by dex\r\n" + dump(ORDERS).replace(
        "sql_table", "sqlTable"
    ).replace("\n", "\r\n")

    rc, planned = plan(capsys, root, "define", {ORDERS_FILE: authored})

    assert rc == 0, planned["errors"]
    assert planned["data"]["defined"] == ["orders"]
    assert planned["data"]["updated"] == []
    assert "unchanged" not in planned["data"]
    assert planned["diffs"] and planned["diffs"][0]["path"] == ORDERS_FILE
    assert not (root / ORDERS_FILE).exists()
    assert any("byte-for-byte" in w for w in planned["warnings"])

    rc, applied = run(capsys, root, "transform", "apply")

    assert rc == 0, applied["errors"]
    assert applied["data"]["written"] == [ORDERS_FILE]
    assert (root / ORDERS_FILE).read_bytes() == authored.encode("utf-8")


def test_update_classifies_each_element_and_leaves_other_files_alone(
    tmp_path, capsys, no_warehouse
):
    shared = "model/shared.yml"
    second = cube(
        "refunds",
        dimensions=[{"name": "id", "sql": "id", "type": "number", "primary_key": True}],
    )
    root = repo(
        tmp_path,
        [shared, CUSTOMERS_FILE],
        **{shared: dump(ORDERS, second), CUSTOMERS_FILE: dump(CUSTOMERS)},
    )
    before_customers = read(root, CUSTOMERS_FILE)
    changed = dict(ORDERS, description="One row per order, revised.")
    # `refunds` respelled in camelCase only: the same definition to Cube.
    respelled = dump(changed, second).replace("primary_key", "primaryKey")

    rc, planned = plan(capsys, root, "update", {shared: respelled})

    assert rc == 0, planned["errors"]
    assert planned["data"]["updated"] == ["orders"]
    assert planned["data"]["unchanged"] == ["refunds"]
    assert planned["data"]["defined"] == []

    rc, applied = run(capsys, root, "transform", "apply")
    assert rc == 0, applied["errors"]
    assert read(root, shared) == respelled
    assert read(root, CUSTOMERS_FILE) == before_customers


def test_plan_takes_a_mix_and_define_and_update_hold_their_promise(tmp_path, capsys):
    root = layered(tmp_path, VIEWS_FILE)
    a_view = view("sales", {"join_path": "orders", "includes": ["status"]})

    rc, refused_update = plan(
        capsys, root, "update", {VIEWS_FILE: dump(views=(a_view,))}
    )
    assert rc != 0
    assert "semantic cube define" in errors(refused_update)

    rc, refused_define = plan(
        capsys,
        root,
        "define",
        {VIEWS_FILE: dump(views=(a_view,)), ORDERS_FILE: dump(dict(ORDERS, title="O"))},
    )
    assert rc != 0
    assert "semantic cube update" in errors(refused_define)

    rc, planned = plan(
        capsys,
        root,
        "plan",
        {VIEWS_FILE: dump(views=(a_view,)), ORDERS_FILE: dump(dict(ORDERS, title="O"))},
    )
    assert rc == 0, planned["errors"]
    assert planned["data"]["defined"] == ["sales"]
    assert planned["data"]["updated"] == ["orders"]


def test_define_refuses_a_removal_and_update_names_it(tmp_path, capsys):
    shared = "model/shared.yml"
    extra = cube(
        "refunds",
        dimensions=[{"name": "id", "sql": "id", "type": "number", "primary_key": True}],
    )
    root = repo(
        tmp_path,
        [shared, CUSTOMERS_FILE],
        **{shared: dump(ORDERS, extra), CUSTOMERS_FILE: dump(CUSTOMERS)},
    )

    rc, refused = plan(capsys, root, "define", {shared: dump(ORDERS)})
    assert rc != 0
    assert "removes: refunds" in errors(refused)

    rc, planned = plan(capsys, root, "update", {shared: dump(ORDERS)})
    assert rc == 0, planned["errors"]
    assert any(
        "removes Cube definitions" in w and "refunds" in w for w in planned["warnings"]
    )


def test_a_subcommand_naming_another_vendor_is_refused(tmp_path, capsys):
    (tmp_path / ".dex").mkdir()
    (tmp_path / ".dex" / "config.yml").write_text(
        "connector: duckdb\nsemantic:\n  vendor: ossie\n  ossie:\n"
        "    files: [layer.ossie.yaml]\n",
        encoding="utf-8",
    )
    rc, refused = plan(capsys, tmp_path, "define", {ORDERS_FILE: dump(ORDERS)})
    assert rc != 0
    assert "use `semantic ossie`" in errors(refused)


# --- the write surface -------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "says"),
    [
        ("model/legacy.js", JS_REASON),
        ("model/globals.py", "deployment configuration"),
        ("cube.js", "deployment configuration"),
        ("model/other.yml", "dex config set semantic.cube.files"),
        ("../outside.yml", "outside the repository"),
        ("model/notes.txt", "not a Cube model file"),
    ],
)
def test_a_path_off_the_surface_is_refused_by_name(tmp_path, capsys, path, says):
    root = layered(tmp_path, "model/legacy.js")
    put(root, "model/legacy.js", "cube(`legacy`, {});\n")

    rc, refused = plan(capsys, root, "plan", {path: dump(ORDERS)})

    assert rc != 0
    assert path in errors(refused)
    assert says in errors(refused)


def test_a_configured_template_on_disk_is_not_replaced(tmp_path, capsys):
    templated = "model/generated.yml"
    root = layered(tmp_path, templated)
    put(
        root,
        templated,
        "cubes:\n{% for t in tables %}\n  - name: {{ t }}\n{% endfor %}\n",
    )

    rc, refused = plan(capsys, root, "plan", {templated: dump(cube("plain"))})

    assert rc != 0
    message = errors(refused)
    assert f"{templated}: (root)" in message
    assert JINJA_REASON in message
    assert "the file on disk is compiled-only" in message


@pytest.mark.parametrize(
    ("content", "element", "reason"),
    [
        ("cubes:\n  - name: {{ name }}\n    sql_table: t\n", "(root)", JINJA_REASON),
        (
            dump(cube("child", extends="orders")),
            "cubes['child']",
            EXTENDS_REASON,
        ),
    ],
)
def test_compiled_only_content_is_refused_with_the_catalog_reason(
    tmp_path, capsys, content, element, reason
):
    root = layered(tmp_path, VIEWS_FILE)

    rc, refused = plan(capsys, root, "plan", {VIEWS_FILE: content})

    assert rc != 0
    message = errors(refused)
    assert f"{VIEWS_FILE}: {element}" in message
    assert reason in message


# --- a broken set is refused naming file and element -------------------------------


@pytest.mark.parametrize(
    ("edits", "file", "element", "rule"),
    [
        # A name declared twice, across files.
        (
            {VIEWS_FILE: dump(cube("orders", sql_table="demo.main.other"))},
            VIEWS_FILE,
            "cubes['orders']",
            "duplicate",
        ),
        # A join to a cube nobody declares.
        (
            {
                ORDERS_FILE: dump(
                    dict(
                        ORDERS,
                        joins=[
                            {
                                "name": "nowhere",
                                "sql": "{CUBE}.x = {nowhere}.x",
                                "relationship": "many_to_one",
                            }
                        ],
                    )
                )
            },
            ORDERS_FILE,
            "cubes['orders'].joins['nowhere']",
            "join_target",
        ),
        # A member reference that resolves to nothing.
        (
            {
                ORDERS_FILE: dump(
                    dict(
                        ORDERS,
                        measures=[
                            {"name": "ratio", "sql": "{missing} / 2", "type": "number"}
                        ],
                    )
                )
            },
            ORDERS_FILE,
            "cubes['orders'].measures['ratio']",
            "unresolved_reference",
        ),
        # Both `sql` and `sql_table`.
        (
            {ORDERS_FILE: dump(dict(ORDERS, sql="select 1"))},
            ORDERS_FILE,
            "cubes['orders']",
            "sql_xor_sql_table",
        ),
        # A view walking a join nobody declared.
        (
            {
                VIEWS_FILE: dump(
                    views=(
                        view("v", {"join_path": "customers.orders", "includes": "*"}),
                    )
                )
            },
            VIEWS_FILE,
            "views['v']",
            "join_path",
        ),
        # Two joins between the same pair of cubes.
        (
            {
                ORDERS_FILE: dump(
                    dict(ORDERS, joins=[ORDERS["joins"][0], ORDERS["joins"][0]])
                )
            },
            ORDERS_FILE,
            "cubes['orders'].joins['customers']",
            "duplicate_join",
        ),
        # A key Cube does not declare, in a file this plan writes.
        (
            {ORDERS_FILE: dump(dict(ORDERS, owner="data team"))},
            ORDERS_FILE,
            "cubes['orders']",
            "unknown_key",
        ),
    ],
)
def test_a_broken_set_is_refused_naming_file_and_element(
    tmp_path, capsys, edits, file, element, rule
):
    root = layered(tmp_path, VIEWS_FILE)
    put(root, VIEWS_FILE, "# views go here\n")

    rc, refused = plan(capsys, root, "plan", edits)

    assert rc != 0
    message = errors(refused)
    assert "no plan was stored" in message
    assert f"{file}: {element}" in message
    assert f"[{rule}]" in message
    # A refusal never says dex would read past what it refused.
    assert "dex ignores it" not in message


def test_an_unknown_key_in_a_file_the_plan_does_not_edit_only_warns(tmp_path, capsys):
    root = repo(
        tmp_path,
        [ORDERS_FILE, CUSTOMERS_FILE, VIEWS_FILE],
        **{
            ORDERS_FILE: dump(dict(ORDERS, owner="data team")),
            CUSTOMERS_FILE: dump(CUSTOMERS),
        },
    )
    a_view = view("sales", {"join_path": "orders", "includes": ["status"]})

    rc, planned = plan(capsys, root, "define", {VIEWS_FILE: dump(views=(a_view,))})

    assert rc == 0, planned["errors"]
    assert any("[unknown_key]" in w and "owner" in w for w in planned["warnings"])


def test_a_summing_cube_on_the_one_side_with_no_key_warns_of_fan_out(tmp_path, capsys):
    keyless = cube(
        "customers",
        dimensions=[{"name": "id", "sql": "id", "type": "number"}],
        measures=[{"name": "lifetime_value", "sql": "ltv", "type": "sum"}],
    )
    root = layered(tmp_path)

    rc, planned = plan(capsys, root, "update", {CUSTOMERS_FILE: dump(keyless)})

    assert rc == 0, planned["errors"]
    fan_out = [w for w in planned["warnings"] if "[fan_out]" in w]
    assert len(fan_out) == 1
    assert fan_out[0].startswith(f"{CUSTOMERS_FILE}: cubes['customers']")
    assert "lifetime_value" in fan_out[0]


def test_missing_configured_files_must_be_supplied(tmp_path, capsys):
    root = repo(tmp_path, [ORDERS_FILE, CUSTOMERS_FILE], **{ORDERS_FILE: dump(ORDERS)})

    rc, refused = plan(
        capsys, root, "update", {ORDERS_FILE: dump(dict(ORDERS, title="x"))}
    )

    assert rc != 0
    assert CUSTOMERS_FILE in errors(refused)
    assert "missing" in errors(refused)


# --- apply ---------------------------------------------------------------------------


def test_a_file_changed_after_planning_refuses_the_whole_plan(tmp_path, capsys):
    root = layered(tmp_path)
    rc, planned = plan(
        capsys,
        root,
        "update",
        {
            ORDERS_FILE: dump(dict(ORDERS, title="Orders, revised")),
            CUSTOMERS_FILE: dump(dict(CUSTOMERS, title="Customers, revised")),
        },
    )
    assert rc == 0, planned["errors"]
    put(root, CUSTOMERS_FILE, "# edited by hand\n" + read(root, CUSTOMERS_FILE))
    before = read(root, ORDERS_FILE)

    rc, applied = run(capsys, root, "transform", "apply")

    assert applied["status"] == "needs_confirmation"
    assert read(root, ORDERS_FILE) == before
    assert "semantic cube plan" in json.dumps(applied)


def test_authoring_opens_no_connection_and_no_cube_server(
    tmp_path, capsys, no_warehouse
):
    root = layered(tmp_path)
    import socket

    real = socket.socket.connect

    def no_network(self, *args, **kwargs):  # pragma: no cover - fails the test
        raise AssertionError("a network connection was attempted")

    socket.socket.connect = no_network  # type: ignore[method-assign]
    try:
        rc, planned = plan(
            capsys, root, "update", {ORDERS_FILE: dump(dict(ORDERS, title="O"))}
        )
        assert rc == 0, planned["errors"]
        rc, applied = run(capsys, root, "transform", "apply")
        assert rc == 0, applied["errors"]
    finally:
        socket.socket.connect = real  # type: ignore[method-assign]
