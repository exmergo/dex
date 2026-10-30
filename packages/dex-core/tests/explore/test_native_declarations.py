"""A native layer's grain and declarations, read back by `explore --use-project`.

A primary key is the row identity at either arity, and a unique key is not
(#976). What `semantic <vendor> plan` writes under `dex` comes back as
`declaration`, with no dbt project in the repository (#977). Each on a real
DuckDB warehouse through the command surface, the way an agent runs it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from exmergo_dex_core.cli import main
from exmergo_dex_core.semantic_declarations import read_native_declaration

pytest.importorskip("sqlglot")
duckdb = pytest.importorskip("duckdb")

SINGLE = "demo.main.orders"
COMPOSITE = "demo.main.items"
DUPLICATED = "demo.main.events"
UNIQUE_ONLY = "demo.main.lookups"

DECLARATION = {
    "model": "orders",
    "grain": ["order_id"],
    "population": {
        "rule": "Orders that were placed",
        "filters": [{"column": "status", "excludes": ["draft"]}],
    },
    "columns": {
        "amount": {
            "role": "measure",
            "aggregation": "sum",
            "unit": "currency:EUR",
            "additivity": "additive",
        },
        "status": {"role": "dimension"},
    },
}


def _run(root: Path, capsys, *argv: str) -> dict[str, Any]:
    main(["--repo-root", str(root), *argv])
    return json.loads(capsys.readouterr().out)


def _ok(root: Path, capsys, *argv: str) -> dict[str, Any]:
    envelope = _run(root, capsys, *argv)
    assert envelope["status"] == "ok", envelope
    return envelope


def _warehouse(root: Path) -> None:
    conn = duckdb.connect(str(root / "demo.duckdb"))
    conn.execute(
        "CREATE TABLE orders AS SELECT i AS order_id, 'placed' AS status, "
        "i * 1.5 AS amount FROM range(1, 21) t(i)"
    )
    # No single column is unique; the pair is.
    conn.execute(
        "CREATE TABLE items AS SELECT i % 5 AS order_id, i % 4 AS line_no "
        "FROM range(0, 20) t(i)"
    )
    conn.execute(
        "CREATE TABLE events AS SELECT i % 3 AS event_id, i AS seq "
        "FROM range(0, 9) t(i)"
    )
    conn.execute(
        "CREATE TABLE lookups AS SELECT i % 3 AS tenant, i % 4 AS ref, i AS code "
        "FROM range(0, 12) t(i)"
    )
    conn.execute(
        "CREATE TABLE pairs AS SELECT i % 2 AS a, i % 2 AS b FROM range(0, 8) t(i)"
    )
    conn.close()


def _config(root: Path, body: str) -> None:
    (root / ".dex").mkdir(exist_ok=True)
    (root / ".dex" / "config.yml").write_text(
        f"connector: duckdb\nduckdb:\n  path: demo.duckdb\nsemantic:\n{body}",
        encoding="utf-8",
    )


def _ossie_dataset(name: str, columns: list[str], **extra: Any) -> dict[str, Any]:
    return {
        "name": name,
        "source": f"demo.main.{name}",
        **extra,
        "fields": [{"name": c, "expression": {"sql": c}} for c in columns],
    }


def _ossie_repo(root: Path, *datasets: dict[str, Any]) -> Path:
    _warehouse(root)
    (root / "layer.ossie.yaml").write_text(
        yaml.safe_dump(
            {"version": "0.2.0.dev0", "name": "shop", "datasets": list(datasets)},
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    _config(root, "  vendor: ossie\n  ossie:\n    files:\n      - layer.ossie.yaml\n")
    return root


@pytest.fixture
def keyed(tmp_path: Path) -> Path:
    return _ossie_repo(
        tmp_path,
        _ossie_dataset(
            "orders", ["order_id", "status", "amount"], primary_key=["order_id"]
        ),
        _ossie_dataset(
            "items", ["order_id", "line_no"], primary_key=["order_id", "line_no"]
        ),
        _ossie_dataset("events", ["event_id", "seq"], primary_key=["event_id"]),
        _ossie_dataset(
            "lookups",
            ["tenant", "ref", "code"],
            unique_keys=[["code"], ["tenant", "ref"]],
        ),
    )


def _profiled(root: Path, capsys, *objects: str) -> dict[str, dict[str, Any]]:
    envelope = _ok(
        root,
        capsys,
        "explore",
        "profile",
        *objects,
        "--use-project",
        "--columns",
        "all",
    )
    return {d["identifier"]: d for d in envelope["data"]["datasets"]}


# --- #976: a primary key is the grain, at either arity --------------------------------


def test_a_primary_key_of_either_arity_is_the_reported_grain(keyed, capsys):
    datasets = _profiled(keyed, capsys, SINGLE, COMPOSITE)

    assert datasets[SINGLE]["grain"] == ["order_id"]
    assert datasets[COMPOSITE]["grain"] == ["order_id", "line_no"]
    for identifier in (SINGLE, COMPOSITE):
        declaration = datasets[identifier]["declaration"]
        assert declaration["source"] == "ossie"
        assert declaration["grain"] == datasets[identifier]["grain"]
    assert datasets[SINGLE]["declaration"]["grain_status"] == "proven"


def test_a_contradicted_single_primary_key_keeps_the_evidence_beside_the_claim(
    keyed, capsys
):
    dataset = _profiled(keyed, capsys, DUPLICATED)[DUPLICATED]

    assert dataset["grain"] == ["event_id"]
    assert dataset["declaration"]["grain_status"] == "contradicted"
    assert ["event_id"] not in dataset["candidate_keys"]
    assert any("found duplicates" in note for note in dataset["data_quality"])
    assert any(
        "declared primary key" in note and "seq" in note
        for note in dataset["data_quality"]
    )


def test_a_primary_key_beats_a_measured_single_column_key(tmp_path, capsys):
    """Both arities. A composite used to yield to a proven single, and its note
    would then sit beside a declaration saying the opposite."""

    root = _ossie_repo(
        tmp_path,
        _ossie_dataset(
            "lookups", ["tenant", "ref", "code"], primary_key=["tenant", "ref"]
        ),
    )

    dataset = _profiled(root, capsys, UNIQUE_ONLY)[UNIQUE_ONLY]

    assert dataset["grain"] == ["tenant", "ref"]
    assert not any("took precedence" in note for note in dataset["data_quality"])
    assert any("declared primary key" in note for note in dataset["data_quality"])


def test_unique_keys_are_not_a_grain_at_either_arity(keyed, capsys):
    dataset = _profiled(keyed, capsys, UNIQUE_ONLY)[UNIQUE_ONLY]

    assert dataset["grain"] == ["code"]  # Measured, and not because it was declared.
    assert "declaration" not in dataset or dataset["declaration"] is None


def test_a_composite_unique_key_alone_does_not_set_the_grain(tmp_path, capsys):
    root = _ossie_repo(
        tmp_path,
        _ossie_dataset(
            "items", ["order_id", "line_no"], unique_keys=[["order_id", "line_no"]]
        ),
    )

    dataset = _profiled(root, capsys, COMPOSITE)[COMPOSITE]

    assert not any("declared" in note for note in dataset["data_quality"])
    assert not dataset.get("declaration")


def test_a_primary_key_naming_a_gone_column_is_said_and_not_applied(tmp_path, capsys):
    root = _ossie_repo(
        tmp_path,
        _ossie_dataset("orders", ["order_id", "gone"], primary_key=["gone"]),
    )

    dataset = _profiled(root, capsys, SINGLE)[SINGLE]

    assert dataset["grain"] != ["gone"]
    assert dataset["declaration"]["grain_status"] == "missing_columns"


def test_map_reports_both_arities_and_the_grain_clears_with_the_key(keyed, capsys):
    data = _ok(keyed, capsys, "explore", "map", "--use-project")["data"]
    objects = {o["identifier"]: o for o in data["objects"]}

    assert objects[SINGLE]["grain"] == ["order_id"]
    assert objects[COMPOSITE]["grain"] == ["order_id", "line_no"]
    assert objects[SINGLE]["declaration"]["grain_status"] == "proven"
    assert objects[UNIQUE_ONLY]["grain"] == ["code"]

    path = keyed / "layer.ossie.yaml"
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    for entry in document["datasets"]:
        entry.pop("primary_key", None)
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")

    dataset = _profiled(keyed, capsys, COMPOSITE)[COMPOSITE]
    assert not dataset.get("declaration")
    assert not any("primary key" in note for note in dataset["data_quality"])


@pytest.mark.parametrize(
    ("relation", "columns"),
    [(DUPLICATED, ["event_id"]), ("demo.main.pairs", ["a", "b"])],
    ids=["single", "composite"],
)
def test_a_key_that_never_held_stays_a_declaration_to_fix(
    relation, columns, tmp_path, capsys
):
    """Reporting a primary key as the grain must not teach the drift baseline it
    was ever unique, or `maintain grain` calls a wrong declaration lost."""

    name = relation.rpartition(".")[2]
    fields = {"events": ["event_id", "seq"], "pairs": ["a", "b"]}[name]
    root = _ossie_repo(tmp_path, _ossie_dataset(name, fields, primary_key=columns))

    _ok(root, capsys, "explore", "map", "--use-project")
    _ok(root, capsys, "maintain", "snapshot")
    findings = _ok(root, capsys, "maintain", "grain")["data"]["findings"]

    codes = {f["code"] for f in findings if f["identifier"] == relation}
    assert "declared_grain_not_unique" in codes
    assert "key_lost_uniqueness" not in codes


def test_a_dbt_unique_test_is_not_a_grain_claim(tmp_path, capsys):
    _warehouse(tmp_path)
    (tmp_path / "models").mkdir()
    (tmp_path / "dbt_project.yml").write_text(
        "name: shop\nversion: '1.0'\nprofile: shop\nmodel-paths: [models]\n",
        encoding="utf-8",
    )
    (tmp_path / "models" / "schema.yml").write_text(
        yaml.safe_dump(
            {
                "version": 2,
                "models": [
                    {
                        "name": "lookups",
                        "columns": [{"name": "tenant", "data_tests": ["unique"]}],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / ".dex").mkdir()
    (tmp_path / ".dex" / "config.yml").write_text(
        "connector: duckdb\nduckdb:\n  path: demo.duckdb\n", encoding="utf-8"
    )

    dataset = _profiled(tmp_path, capsys, UNIQUE_ONLY)[UNIQUE_ONLY]

    assert dataset["grain"] == ["code"]
    assert any("declared unique" in note for note in dataset["data_quality"])


# --- #977: what a plan writes comes back, on every vendor -----------------------------


def _edits(root: Path, payload: dict[str, Any]) -> str:
    path = root / "edits.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return str(path)


def _ossie_layer(root: Path) -> tuple[str, str]:
    _ossie_repo(root, _ossie_dataset("orders", ["order_id", "status", "amount"]))
    return "ossie", "orders"


def _slayer_layer(root: Path) -> tuple[str, str]:
    pytest.importorskip("slayer")
    from exmergo_dex_core.slayer.loader import FORMAT_VERSION

    _warehouse(root)
    path = root / "semantics" / "models" / "wh" / "orders.yaml"
    path.parent.mkdir(parents=True)
    path.write_text(
        yaml.safe_dump(
            {
                "version": FORMAT_VERSION,
                "name": "orders",
                "sql_table": SINGLE,
                "data_source": "wh",
                "columns": [
                    {"name": "order_id", "type": "INT"},
                    {"name": "status"},
                    {"name": "amount", "type": "DOUBLE"},
                ],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    _config(
        root,
        "  vendor: slayer\n  slayer:\n    base_dir: semantics\n    models:\n"
        "      - models/wh/orders.yaml\n",
    )
    return "slayer", "orders"


def _cube_layer(root: Path) -> tuple[str, str]:
    _warehouse(root)
    path = root / "model" / "orders.yml"
    path.parent.mkdir()
    path.write_text(
        yaml.safe_dump(
            {
                "cubes": [
                    {
                        "name": "orders",
                        "sql_table": SINGLE,
                        "dimensions": [
                            {
                                "name": name,
                                "sql": f"{{CUBE}}.{name}",
                                "type": "number" if name != "status" else "string",
                            }
                            for name in ("order_id", "status", "amount")
                        ],
                    }
                ]
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    _config(root, "  vendor: cube\n  cube:\n    files:\n      - model/orders.yml\n")
    return "cube", "orders"


@pytest.mark.parametrize("layer", [_ossie_layer, _slayer_layer, _cube_layer])
def test_a_planned_declaration_is_read_back_with_no_dbt_project(
    layer, tmp_path, capsys
):
    vendor, model = layer(tmp_path)
    declaration = {**DECLARATION, "model": model}

    planned = _ok(
        tmp_path,
        capsys,
        "semantic",
        vendor,
        "plan",
        "--edits-file",
        _edits(tmp_path, {"declarations": [declaration]}),
    )
    _ok(tmp_path, capsys, "transform", "apply", planned["data"]["plan_id"])

    envelope = _ok(
        tmp_path,
        capsys,
        "explore",
        "profile",
        SINGLE,
        "--use-project",
        "--columns",
        "all",
    )
    (dataset,) = envelope["data"]["datasets"]
    declared = dataset["declaration"]

    assert not (tmp_path / "dbt_project.yml").exists()
    assert declared["source"] == vendor
    assert declared["grain"] == ["order_id"]
    assert dataset["grain"] == ["order_id"]
    assert declared["grain_status"] == "proven"
    assert declared["population"]["rule"] == "Orders that were placed"
    assert declared["population"]["filters"] == [
        {"column": "status", "includes": None, "excludes": ["draft"]}
    ]
    columns = {c["name"]: c for c in declared["columns"]}
    assert columns["amount"]["role"] == "measure"
    assert columns["amount"]["unit"] == "currency:EUR"
    assert columns["amount"]["additivity"] == "additive"
    assert columns["status"]["role"] == "dimension"

    objects = _ok(tmp_path, capsys, "explore", "map", "--use-project")["data"][
        "objects"
    ]
    mapped = next(o for o in objects if o["identifier"] == SINGLE)
    assert mapped["declaration"]["population"]["rule"] == "Orders that were placed"


def test_values_free_withholds_a_native_population(tmp_path, capsys):
    vendor, _model = _ossie_layer(tmp_path)
    planned = _ok(
        tmp_path,
        capsys,
        "semantic",
        vendor,
        "plan",
        "--edits-file",
        _edits(tmp_path, {"declarations": [DECLARATION]}),
    )
    _ok(tmp_path, capsys, "transform", "apply", planned["data"]["plan_id"])

    envelope = _ok(
        tmp_path, capsys, "explore", "profile", SINGLE, "--use-project", "--values-free"
    )
    (dataset,) = envelope["data"]["datasets"]
    population = dataset["declaration"]["population"]

    assert population["rule"] is None
    assert population["filters"][0]["excludes"] is None
    assert "population.rule" in dataset["declaration"]["withheld_fields"]
    assert "Orders that were placed" not in json.dumps(envelope)


# --- untrusted metadata ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("model_meta", "member_meta", "expected"),
    [
        ({"dex": "not a mapping"}, None, "is not a mapping"),
        ({"dex": {"population": "everything"}}, None, "population"),
        ({"dex": {"population": {"filters": [{"column": 3}]}}}, None, "population"),
        ({"dex": {"population": {"rule": "ok", "filters": "x"}}}, None, "population"),
        ({"dex": {"grain": ["nope"]}}, None, "is ignored"),
        (None, {"dex": ["list"]}, "is not a mapping"),
        (None, {"dex": {"role": "ceo"}}, "`role`"),
        (None, {"dex": {"additivity": 7}}, "`additivity`"),
        (None, {"dex": {"references": {"to": "x"}}}, "`references`"),
        (None, {"dex": {"unit": ""}}, "`unit`"),
    ],
)
def test_malformed_metadata_is_a_note_and_never_raised(
    model_meta, member_meta, expected
):
    declaration, notes = read_native_declaration(
        "orders",
        SINGLE,
        ["order_id"],
        model_meta,
        [("amount", "amount", member_meta)],
        source="ossie",
    )

    assert declaration is not None and declaration.grain == ["order_id"]
    assert declaration.columns == {}
    assert any(expected in note for note in notes), notes
    # The note names the field. It does not repeat what the file said.
    assert not any("ceo" in note or "nope" in note for note in notes)


def test_a_partial_block_keeps_what_it_can_read():
    declaration, notes = read_native_declaration(
        "orders",
        SINGLE,
        None,
        {"dex": {"population": {"rule": "Placed", "filters": [{"column": 1}]}}},
        [("amount", "amount", {"dex": {"role": "measure", "additivity": "lots"}})],
        source="ossie",
    )

    assert declaration.population.rule == "Placed"
    assert declaration.columns["amount"].role == "measure"
    assert declaration.columns["amount"].additivity is None
    assert len(notes) == 2


def test_metadata_on_a_field_with_no_column_is_named():
    declaration, notes = read_native_declaration(
        "orders",
        SINGLE,
        None,
        None,
        [("computed", None, {"dex": {"role": "measure"}})],
        source="ossie",
    )

    assert declaration is None
    assert "links no physical column" in notes[0]


def test_a_corrupt_block_reaches_the_envelope_as_a_note(tmp_path, capsys):
    root = _ossie_repo(
        tmp_path,
        _ossie_dataset(
            "orders",
            ["order_id", "status", "amount"],
            primary_key=["order_id"],
            ai_context={"dex": "corrupt"},
        ),
    )

    envelope = _ok(root, capsys, "explore", "profile", SINGLE, "--use-project")

    assert any("not a mapping" in note for note in envelope["data"]["notes"])
    assert envelope["data"]["datasets"][0]["grain"] == ["order_id"]


# --- both a dbt model and a layer -----------------------------------------------------


def test_the_dbt_declaration_wins_and_the_note_says_so(tmp_path, capsys):
    _ossie_repo(
        tmp_path,
        _ossie_dataset(
            "lookups", ["tenant", "ref", "code"], primary_key=["tenant", "ref"]
        ),
    )
    (tmp_path / "models").mkdir()
    (tmp_path / "dbt_project.yml").write_text(
        "name: shop\nversion: '1.0'\nprofile: shop\nmodel-paths: [models]\n",
        encoding="utf-8",
    )
    (tmp_path / "models" / "schema.yml").write_text(
        yaml.safe_dump(
            {
                "version": 2,
                "models": [
                    {
                        "name": "lookups",
                        "config": {"meta": {"dex": {"grain": ["code"]}}},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    envelope = _ok(tmp_path, capsys, "explore", "profile", UNIQUE_ONLY, "--use-project")
    (dataset,) = envelope["data"]["datasets"]

    assert dataset["declaration"]["source"] == "yaml"
    assert dataset["grain"] == ["code"]
    note = next(n for n in envelope["data"]["notes"] if "declared twice" in n)
    assert "the dbt project" in note and "the Ossie layer" in note
    assert "their grains disagree (code against tenant, ref)" in note
