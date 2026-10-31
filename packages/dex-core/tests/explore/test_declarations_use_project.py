"""`explore --use-project` reads a model's declaration back (#498).

A declaration (#491) is the project's own statement of what one row is, which
rows are in, and what each column means. These cover the read model, the
declared joins a column's `references` adds, the declared grain beside the
measured one, and the rules the rest of explore already keeps: nothing changes
without the flag, nothing the declaration says is cached, caps bind and are
counted, and values-free mode leaks no population value.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from exmergo_dex_core.cache import ColumnProfile, Dataset, PIIFlag
from exmergo_dex_core.cli import main
from exmergo_dex_core.dbt_project import (
    ModelDeclaration,
    ProjectDefinitions,
    declared_meaning,
    definitions,
)
from exmergo_dex_core.explore.commands import _apply_declarations
from exmergo_dex_core.values_free_conformance import _unknown_paths

_DECLARED_ORDERS = """\
version: 2
models:
  - name: fct_orders
    description: One row per order line, cancelled orders excluded.
    config:
      meta:
        dex:
          grain: [order_id, line_no]
          population:
            rule: Cancelled orders are not revenue.
            filters:
              - column: status
                excludes: [cancelled_zq7]
    columns:
      - name: customer_id
        config:
          meta:
            dex:
              role: foreign_key
              references: customers.customer_id
      - name: amount
        config:
          meta:
            dex:
              role: measure
              unit: currency:EUR
              additivity: additive
"""

_RELATIONSHIPS_TEST = """\
        data_tests:
          - relationships:
              arguments:
                to: ref('customers')
                field: customer_id
"""


def _run(argv: list[str], capsys) -> dict:
    rc = main(argv)
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0, payload
    assert payload["status"] == "ok", payload
    return payload


@pytest.fixture
def warehouse(tmp_path: Path) -> Path:
    duckdb = pytest.importorskip("duckdb")
    db = tmp_path / "wh.duckdb"
    conn = duckdb.connect(str(db))
    conn.execute("CREATE TABLE customers (customer_id INTEGER, email VARCHAR)")
    conn.executemany(
        "INSERT INTO customers VALUES (?, ?)",
        [(i, f"c{i}@example.com") for i in range(1, 21)],
    )
    conn.execute(
        "CREATE TABLE fct_orders (order_id INTEGER, line_no INTEGER, "
        "customer_id INTEGER, status VARCHAR, amount DOUBLE)"
    )
    conn.executemany(
        "INSERT INTO fct_orders VALUES (?, ?, ?, ?, ?)",
        [
            (order, line, (order % 20) + 1, "paid", float(order + line))
            for order in range(1, 41)
            for line in (1, 2)
        ],
    )
    conn.close()
    return db


def _repo(tmp_path: Path, schema: str = _DECLARED_ORDERS) -> Path:
    repo = tmp_path / "repo"
    (repo / "models").mkdir(parents=True, exist_ok=True)
    (repo / "dbt_project.yml").write_text(
        'name: dex_test\nversion: "1.0.0"\nmodel-paths: ["models"]\n',
        encoding="utf-8",
    )
    (repo / "models" / "schema.yml").write_text(schema, encoding="utf-8")
    return repo


def _args(command: str, db: Path, repo: Path, *extra: str) -> list[str]:
    return [
        "explore",
        command,
        *extra,
        "--path",
        str(db),
        "--repo-root",
        str(repo),
    ]


def _orders(payload: dict, key: str = "datasets") -> dict:
    return next(
        d for d in payload["data"][key] if d["identifier"].endswith(".fct_orders")
    )


# --- the read model ---------------------------------------------------------------


def test_declared_meaning_reads_a_yaml_entry_and_a_manifest_node_alike():
    yaml_entry = {
        "name": "m",
        "config": {"meta": {"dex": {"grain": ["id"]}}},
        "columns": [
            {"name": "amt", "meta": {"dex": {"unit": "count", "role": "measure"}}},
        ],
    }
    node = {
        "name": "m",
        "config": {"meta": {"dex": {"grain": ["id"]}}},
        "columns": {
            "amt": {
                "name": "amt",
                "meta": {"dex": {"unit": "count", "role": "measure"}},
            }
        },
    }
    a = declared_meaning(yaml_entry, "m", relation=None, source="yaml")
    b = declared_meaning(node, "m", relation="db.s.m", source="manifest")
    assert a is not None and b is not None
    assert a.grain == b.grain == ["id"]
    assert a.columns == b.columns
    assert a.columns["amt"].unit == "count"
    assert b.relation == "db.s.m"


def test_declared_meaning_ignores_malformed_fields_and_empty_declarations():
    entry = {
        "name": "m",
        "meta": {
            "dex": {
                "grain": "id",
                "population": {"filters": [{"column": "s"}, "junk"], "rule": "  "},
            }
        },
        "columns": [{"name": "x", "meta": {"dex": {"role": 3, "unit": ""}}}, "junk"],
    }
    assert declared_meaning(entry, "m", relation=None, source="yaml") is None
    assert declared_meaning({"name": "m"}, "m", relation=None, source="yaml") is None


def test_project_definitions_default_carries_no_declarations():
    # Every format that never reads declarations (Ossie, SLayer, a host's own)
    # returns this default and is unaffected.
    assert ProjectDefinitions().model_declarations == {}


def test_a_stale_manifest_yields_to_the_yaml_declaration(tmp_path: Path):
    repo = _repo(tmp_path)
    target = repo / "target"
    target.mkdir()
    node = {
        "resource_type": "model",
        "name": "fct_orders",
        "relation_name": '"wh"."main"."fct_orders"',
        "config": {"meta": {"dex": {"grain": ["order_id"]}}},
        "columns": {},
    }
    manifest = {
        "metadata": {"generated_at": "2000-01-01T00:00:00Z"},
        "nodes": {"model.dex_test.fct_orders": node},
    }
    (target / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    defs = definitions(repo)
    declaration = defs.model_declarations["fct_orders"]
    assert defs.manifest_stale
    assert declaration.grain == ["order_id", "line_no"]
    assert declaration.source == "yaml"
    assert declaration.relation == "wh.main.fct_orders"

    manifest["metadata"]["generated_at"] = "2999-01-01T00:00:00Z"
    (target / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    fresh = definitions(repo).model_declarations["fct_orders"]
    assert fresh.grain == ["order_id"]
    assert fresh.source == "manifest"


# --- the declared grain beside the measured one -----------------------------------


def _dataset(**columns: bool | None) -> Dataset:
    return Dataset(
        identifier="db.main.t",
        columns=[
            ColumnProfile(name=name, data_type="INTEGER", is_unique=unique)
            for name, unique in columns.items()
        ],
    )


def _declare(dataset: Dataset, grain: list[str]) -> Dataset:
    defs = ProjectDefinitions(
        model_declarations={
            "t": ModelDeclaration(model="t", grain=grain, source="yaml"),
        }
    )
    (out,), _notes = _apply_declarations(
        [dataset], defs, high_null_fraction=0.95, values_free=False
    )
    return out


def test_declared_grain_status_proven_when_a_measured_key_is_the_grain():
    ds = _dataset(id=True, name=False)
    ds.candidate_keys = [["id"]]
    ds.grain = ["id"]
    out = _declare(ds, ["ID"])
    assert out.declaration.grain_status == "proven"
    assert out.grain == ["id"]
    assert not any("declaration" in n for n in out.data_quality)


def test_declared_grain_contradicted_keeps_the_evidence_beside_the_claim():
    ds = _dataset(id=True, code=False)
    ds.candidate_keys = [["id"]]
    ds.grain = ["id"]
    out = _declare(ds, ["code"])
    assert out.declaration.grain_status == "contradicted"
    assert out.grain == ["code"]
    # The claim is reported as grain; the measurement survives untouched.
    assert out.candidate_keys == [["id"]]
    assert any("profiling found duplicates" in n for n in out.data_quality)
    assert any(
        "comes from the model's declaration (measured grain was id)" in n
        for n in out.data_quality
    )
    # A copy: the dataset passed in is not touched.
    assert ds.grain == ["id"] and ds.declaration is None


def test_declared_composite_grain_unmeasured_and_missing_columns():
    out = _declare(_dataset(a=False, b=False), ["a", "b"])
    assert out.declaration.grain_status == "unmeasured"
    assert out.grain == ["a", "b"]
    assert any("measurement found no key" in n for n in out.data_quality)

    missing = _declare(_dataset(a=False), ["a", "gone"])
    assert missing.declaration.grain_status == "missing_columns"
    assert missing.grain is None
    assert any("(gone); not applied" in n for n in missing.data_quality)

    assert _declare(_dataset(), ["a"]).declaration.grain_status == "unprofiled"


@pytest.mark.parametrize("confidence", [0.2, 0.9])
def test_population_values_on_a_pii_column_are_withheld_in_full_mode(confidence):
    ds = _dataset(id=True)
    ds.columns.append(
        ColumnProfile(
            name="email",
            data_type="VARCHAR",
            pii=PIIFlag(category="email", confidence=confidence),
        )
    )
    defs = ProjectDefinitions(
        model_declarations={
            "t": ModelDeclaration.model_validate(
                {
                    "model": "t",
                    "source": "yaml",
                    "population": {
                        "rule": "staff excluded",
                        "filters": [
                            {"column": "EMAIL", "excludes": ["a@b.c"]},
                            {"column": "id", "includes": [1]},
                        ],
                    },
                }
            )
        }
    )
    (out,), _ = _apply_declarations(
        [ds], defs, high_null_fraction=0.95, values_free=False
    )
    email, ident = out.declaration.population.filters
    assert email.excludes is None and email.includes is None
    assert ident.includes == [1]
    assert out.declaration.population.rule is None
    assert out.declaration.withheld_fields == [
        "population.rule",
        "population.filters[].values",
    ]


# --- end to end -------------------------------------------------------------------


def test_profile_carries_the_declaration_only_under_use_project(
    warehouse: Path, tmp_path: Path, capsys
):
    repo = _repo(tmp_path)
    bare = _orders(_run(_args("profile", warehouse, repo, "fct_orders"), capsys))
    assert "declaration" not in bare
    assert bare["grain"] != ["order_id", "line_no"]

    payload = _run(
        _args("profile", warehouse, repo, "fct_orders", "--use-project"), capsys
    )
    orders = _orders(payload)
    # Served from the cache the bare run wrote, and still annotated.
    assert payload["data"]["cache_hit_count"] == 1
    declaration = orders["declaration"]
    assert declaration["model"] == "fct_orders"
    assert declaration["grain"] == ["order_id", "line_no"]
    assert declaration["grain_status"] in {"proven", "unmeasured"}
    assert orders["grain"] == ["order_id", "line_no"]
    assert declaration["population"] == {
        "rule": "Cancelled orders are not revenue.",
        "filters": [
            {"column": "status", "includes": None, "excludes": ["cancelled_zq7"]}
        ],
    }
    by_name = {c["name"]: c for c in declaration["columns"]}
    assert by_name["amount"] == {
        "name": "amount",
        "role": "measure",
        "unit": "currency:EUR",
        "additivity": "additive",
        "references": None,
    }
    assert by_name["customer_id"]["references"] == "customers.customer_id"
    assert any("carry their model's declaration" in n for n in payload["data"]["notes"])

    # Nothing a declaration says reaches the cache, the grain included.
    cache_text = (repo / ".dex" / "cache.json").read_text(encoding="utf-8")
    assert "declaration" not in cache_text
    assert "cancelled_zq7" not in cache_text
    cached = json.loads(cache_text)
    (entry,) = [d for d in cached["datasets"] if d["identifier"].endswith("fct_orders")]
    assert entry["grain"] == bare["grain"]


def test_a_removed_declaration_clears_on_the_next_run(
    warehouse: Path, tmp_path: Path, capsys
):
    repo = _repo(tmp_path)
    first = _orders(
        _run(_args("profile", warehouse, repo, "fct_orders", "--use-project"), capsys)
    )
    assert "declaration" in first
    _repo(tmp_path, "version: 2\nmodels:\n  - name: fct_orders\n")
    again = _orders(
        _run(_args("profile", warehouse, repo, "fct_orders", "--use-project"), capsys)
    )
    assert "declaration" not in again
    assert again["grain"] != ["order_id", "line_no"]
    assert not any("model's declaration" in n for n in again["data_quality"])


def test_map_object_carries_the_declaration_and_the_declared_join(
    warehouse: Path, tmp_path: Path, capsys
):
    repo = _repo(tmp_path)
    bare = _run(_args("map", warehouse, repo), capsys)
    assert all("declaration" not in o for o in bare["data"]["objects"])
    assert all(
        "declaration" not in (e.get("declared_by") or "") for e in bare["data"]["edges"]
    )

    payload = _run(_args("map", warehouse, repo, "--use-project"), capsys)
    orders = _orders(payload, "objects")
    assert orders["declaration"]["grain"] == ["order_id", "line_no"]
    assert orders["grain"] == ["order_id", "line_no"]
    roles = {c["name"]: c["role"] for c in orders["columns"]}
    assert roles["order_id"] == roles["line_no"] == "grain"
    (edge,) = [
        e
        for e in payload["data"]["edges"]
        if e["from_dataset"].endswith(".fct_orders")
        and e["to_dataset"].endswith(".customers")
    ]
    assert edge["kind"] == "declared"
    assert edge["confidence"] == 1.0
    assert edge["declared_by"] == "declaration fct_orders.customer_id"
    assert edge["declaration_sources"] == ["declaration"]
    assert any("come from model declarations" in n for n in payload["data"]["notes"])


def test_a_declared_join_also_tested_is_one_edge_with_both_sources(
    warehouse: Path, tmp_path: Path, capsys
):
    schema = _DECLARED_ORDERS.replace(
        "              references: customers.customer_id\n",
        "              references: customers.customer_id\n" + _RELATIONSHIPS_TEST,
    )
    repo = _repo(tmp_path, schema)
    payload = _run(_args("relationships", warehouse, repo, "--use-project"), capsys)
    edges = [
        r
        for r in payload["data"]["relationships"]
        if r["from_dataset"].endswith(".fct_orders")
        and r["to_dataset"].endswith(".customers")
    ]
    assert len(edges) == 1
    (edge,) = edges
    assert edge["from_columns"] == ["customer_id"]
    assert edge["to_columns"] == ["customer_id"]
    assert edge["declared_by"] == "declaration fct_orders.customer_id"
    assert sorted(edge["declaration_sources"]) == ["declaration", "yaml"]
    assert any(
        "also declared by a relationships test" in n for n in payload["data"]["notes"]
    )
    assert payload["data"]["conflicts"] == []


def test_a_declaration_disagreeing_with_a_test_is_a_conflict(
    warehouse: Path, tmp_path: Path, capsys
):
    schema = _DECLARED_ORDERS.replace(
        "              references: customers.customer_id\n",
        "              references: customers.customer_id\n"
        + _RELATIONSHIPS_TEST.replace("field: customer_id", "field: email"),
    )
    repo = _repo(tmp_path, schema)
    payload = _run(_args("relationships", warehouse, repo, "--use-project"), capsys)
    (conflict,) = payload["data"]["conflicts"]
    sources = sorted(d["source"] for d in conflict["declarations"])
    assert sources == ["declaration", "yaml"]


def test_an_unresolvable_reference_is_a_note_not_a_guess(
    warehouse: Path, tmp_path: Path, capsys
):
    schema = _DECLARED_ORDERS.replace(
        "references: customers.customer_id", "references: dim_nowhere.customer_id"
    )
    repo = _repo(tmp_path, schema)
    payload = _run(_args("map", warehouse, repo, "--use-project"), capsys)
    assert not any(
        (e.get("declared_by") or "").startswith("declaration")
        for e in payload["data"]["edges"]
    )
    assert any(
        "declared reference fct_orders.customer_id -> dim_nowhere.customer_id" in n
        and "skipped rather than guessed" in n
        for n in payload["data"]["notes"]
    )
    notes = payload["data"]["notes"]
    assert "no declared relationships resolved against this connection" in notes
    assert not any("declares no foreign keys" in n for n in notes)


def _wide_schema(count: int) -> str:
    columns = "".join(
        f"      - name: c{i}\n        config:\n          meta:\n"
        f"            dex:\n              role: attribute\n"
        for i in range(count)
    )
    return "version: 2\nmodels:\n  - name: fct_orders\n    columns:\n" + columns


def test_declared_columns_are_capped_and_counted(
    warehouse: Path, tmp_path: Path, capsys
):
    repo = _repo(tmp_path, _wide_schema(14))
    mapped = _run(_args("map", warehouse, repo, "--use-project"), capsys)
    declaration = _orders(mapped, "objects")["declaration"]
    assert len(declaration["columns"]) == 12
    assert declaration["elided_column_count"] == 2
    assert any(
        "2 declared column(s) are not listed" in n for n in mapped["data"]["notes"]
    )
    # --detail widens eligibility, never a cap.
    detailed = _run(_args("map", warehouse, repo, "--use-project", "--detail"), capsys)
    assert len(_orders(detailed, "objects")["declaration"]["columns"]) == 12

    profiled = _run(
        _args("profile", warehouse, repo, "fct_orders", "--use-project"), capsys
    )
    assert _orders(profiled)["declaration"]["elided_column_count"] == 2
    assert any("--columns all lists every one" in n for n in profiled["data"]["notes"])
    every = _run(
        _args(
            "profile",
            warehouse,
            repo,
            "fct_orders",
            "--use-project",
            "--columns",
            "all",
        ),
        capsys,
    )
    full = _orders(every)["declaration"]
    assert len(full["columns"]) == 14
    assert full["elided_column_count"] == 0


@pytest.mark.parametrize("command", ["profile", "map"])
def test_values_free_withholds_every_population_value(
    command: str, warehouse: Path, tmp_path: Path, capsys
):
    repo = _repo(tmp_path)
    extra = ["fct_orders"] if command == "profile" else []
    rc = main(_args(command, warehouse, repo, *extra, "--use-project", "--values-free"))
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "cancelled_zq7" not in out
    assert "Cancelled orders are not revenue" not in out
    payload = json.loads(out)
    assert payload["data"]["result_mode"] == "values_free"
    key = "datasets" if command == "profile" else "objects"
    declaration = _orders(payload, key)["declaration"]
    assert declaration["population"] == {
        "rule": None,
        "filters": [{"column": "status", "includes": None, "excludes": None}],
    }
    assert declaration["withheld_fields"] == [
        "population.rule",
        "population.filters[].values",
    ]
    # Every field the declaration adds was reviewed for the mode.
    assert _unknown_paths(command, payload["data"]) == []
    # The rest of the declaration holds no table value and is kept.
    assert declaration["grain"] == ["order_id", "line_no"]
    cache_text = (repo / ".dex" / "cache.json").read_text(encoding="utf-8")
    assert "cancelled_zq7" not in cache_text
