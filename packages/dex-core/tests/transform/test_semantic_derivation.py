"""A MetricFlow semantic model derived from a declared mart (#499)."""

from __future__ import annotations

import importlib
import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from exmergo_dex_core.cli import main
from exmergo_dex_core.config import TransformConfig
from exmergo_dex_core.transform import semantic_derivation as sd

# --- the pure derivation ------------------------------------------------------


def _declared(**columns: dict[str, Any]) -> dict[str, Any]:
    return {
        "description": "One row per order.",
        "grain": ["order_id"],
        "columns": {"order_id": {"role": "key"}, **columns},
    }


def _derive(declared: dict[str, Any], types=None, on_ref=None) -> sd.Derivation:
    return sd.derive(
        "fct_orders",
        declared,
        column_types=types or {},
        on_ref=on_ref or {},
    )


ORDERED_AT = {"role": "time", "description": "When the order was placed."}


def test_a_single_column_grain_is_the_primary_entity():
    found = _derive(_declared(status={"role": "dimension"}))

    assert found.body["entities"] == [
        {"name": "order", "type": "primary", "expr": "order_id"}
    ]
    assert found.body["model"] == "ref('fct_orders')"
    assert found.body["description"] == "One row per order."
    assert found.blocked is None


def test_a_composite_grain_names_the_limitation_and_guesses_no_primary_entity():
    declared = _declared(status={"role": "dimension"})
    declared["grain"] = ["order_id", "line_number"]

    found = _derive(declared)

    assert found.status == "failed"
    assert all(e["type"] != "primary" for e in found.body.get("entities", []))
    assert "composite (order_id, line_number)" in found.blocked
    assert "primary_entity" in found.blocked


def test_a_foreign_key_takes_the_entity_name_its_parent_already_declares():
    on_ref = {
        "dim_customers": [
            (
                "models/semantic.yml",
                {
                    "name": "customers",
                    "entities": [
                        {"name": "client", "type": "primary", "expr": "customer_id"}
                    ],
                },
            )
        ]
    }
    found = _derive(
        _declared(
            customer_id={
                "role": "foreign_key",
                "references": "dim_customers.customer_id",
            },
            status={"role": "dimension"},
        ),
        on_ref=on_ref,
    )

    assert {"name": "client", "type": "foreign", "expr": "customer_id"} in found.body[
        "entities"
    ]
    assert any("joins semantic model 'customers'" in n for n in found.notes)


def test_a_foreign_key_without_references_is_named_from_the_column_and_noted():
    found = _derive(
        _declared(customer_id={"role": "foreign_key"}, status={"role": "dimension"})
    )

    assert {"name": "customer", "type": "foreign", "expr": "customer_id"} in found.body[
        "entities"
    ]
    assert any("no `references`" in n for n in found.notes)


@pytest.mark.parametrize(
    ("kind", "truncated", "granularity", "noted"),
    [
        ("date", None, "day", None),
        ("timestamp", None, "day", "a timestamp"),
        (None, None, "day", "could not be read"),
        ("date", "month", "month", "truncates"),
    ],
)
def test_a_time_column_gets_the_granularity_its_type_supports(
    kind, truncated, granularity, noted
):
    found = _derive(
        _declared(ordered_at=ORDERED_AT), types={"ordered_at": (kind, truncated)}
    )

    (dimension,) = found.body["dimensions"]
    assert dimension == {
        "name": "ordered_at",
        "type": "time",
        "type_params": {"time_granularity": granularity},
        "description": "When the order was placed.",
    }
    assert found.body["defaults"] == {"agg_time_dimension": "ordered_at"}
    if noted:
        assert any(n.startswith("ordered_at:") and noted in n for n in found.notes)


def test_a_time_column_whose_type_is_not_temporal_is_not_a_time_dimension():
    found = _derive(
        _declared(ordered_at=ORDERED_AT, status={"role": "dimension"}),
        types={"ordered_at": ("other", None)},
    )

    assert [d["name"] for d in found.body["dimensions"]] == ["status"]
    assert any("not a date or a timestamp" in item for item in found.limitations)


def test_several_time_columns_default_to_the_first_and_say_so():
    found = _derive(
        _declared(ordered_at=ORDERED_AT, shipped_at={"role": "time"}),
    )

    assert found.body["defaults"] == {"agg_time_dimension": "ordered_at"}
    assert any(
        "default time dimension is ordered_at" in n and "shipped_at" in n
        for n in found.notes
    )


@pytest.mark.parametrize(
    ("declared", "agg"),
    [
        ("sum", "sum"),
        ("avg", "average"),
        ("Mean", "average"),
        ("count distinct", "count_distinct"),
        ("count-distinct", "count_distinct"),
        ("max", "max"),
        ("median", "median"),
    ],
)
def test_a_declared_aggregation_maps_to_its_metricflow_agg(declared, agg):
    found = _derive(
        _declared(
            ordered_at=ORDERED_AT,
            amount={"role": "measure", "aggregation": declared},
        )
    )

    assert found.body["measures"] == [{"name": "amount", "agg": agg, "expr": "amount"}]


@pytest.mark.parametrize(
    ("fields", "named"),
    [
        ({"role": "measure"}, "no declared aggregation"),
        ({"role": "measure", "aggregation": "percentile"}, "no MetricFlow equivalent"),
        (
            {"role": "measure", "aggregation": "sum", "additivity": "non_additive"},
            "the two disagree",
        ),
    ],
)
def test_a_measure_dex_cannot_map_is_named_and_left_out(fields, named):
    found = _derive(
        _declared(ordered_at=ORDERED_AT, amount=fields, status={"role": "dimension"})
    )

    assert "measures" not in found.body
    assert any(
        item.startswith("amount:") and named in item for item in found.limitations
    )


def test_a_non_additive_measure_with_a_non_summing_aggregation_is_derived():
    found = _derive(
        _declared(
            ordered_at=ORDERED_AT,
            share={
                "role": "measure",
                "aggregation": "average",
                "additivity": "non_additive",
            },
        )
    )

    assert found.body["measures"] == [
        {
            "name": "share",
            "agg": "average",
            "expr": "share",
            # MetricFlow reads any measure as one it may add up, so the
            # declaration's word against that travels as metadata.
            "config": {"meta": {"dex": {"additivity": "non_additive"}}},
        }
    ]


def test_a_semi_additive_measure_is_not_added_across_the_time_dimension():
    found = _derive(
        _declared(
            ordered_at=ORDERED_AT,
            season_to_date={
                "role": "measure",
                "aggregation": "sum",
                "additivity": "semi_additive",
                "description": "Points so far this season.",
            },
        )
    )

    assert found.body["measures"] == [
        {
            "name": "season_to_date",
            "agg": "sum",
            "expr": "season_to_date",
            "description": "Points so far this season.",
            "non_additive_dimension": {"name": "ordered_at", "window_choice": "max"},
        }
    ]
    assert any("window_choice: max" in n and "Confirm" in n for n in found.notes)
    assert any("window_groupings" in n for n in found.notes)


def test_a_semi_additive_note_names_the_foreign_entities_it_may_run_per():
    found = _derive(
        _declared(
            ordered_at=ORDERED_AT,
            customer_id={"role": "foreign_key"},
            lifetime_value={
                "role": "measure",
                "aggregation": "sum",
                "additivity": "semi_additive",
            },
        )
    )

    measure = found.body["measures"][0]
    assert "window_groupings" not in measure["non_additive_dimension"]
    assert any("here perhaps: customer" in n for n in found.notes)


def test_a_measure_with_no_time_column_to_aggregate_over_is_left_out():
    found = _derive(
        _declared(
            status={"role": "dimension"},
            total={"role": "measure", "aggregation": "sum", "additivity": "additive"},
        )
    )

    assert "measures" not in found.body
    assert "defaults" not in found.body
    assert any("names no time column" in item for item in found.limitations)


def test_descriptions_are_carried_and_no_metric_is_ever_derived():
    found = _derive(
        _declared(
            ordered_at=ORDERED_AT,
            status={"role": "dimension", "description": "Lifecycle status."},
            total={
                "role": "measure",
                "aggregation": "sum",
                "description": "Order value.",
            },
        )
    )

    assert "metrics" not in found.body
    assert {
        "name": "status",
        "type": "categorical",
        "description": "Lifecycle status.",
    } in (found.body["dimensions"])
    assert found.body["measures"][0]["description"] == "Order value."
    assert any("metrics are not derived" in n for n in found.notes)


def test_a_declaration_with_nothing_a_semantic_model_carries_is_skipped():
    found = _derive(_declared(note={"role": "attribute"}))

    assert found.status == "skipped"
    assert found.body is None


def test_a_mart_is_a_model_under_a_configured_marts_folder():
    assert sd.in_marts("models/marts/fct_orders.sql", ["models/marts"])
    assert sd.in_marts("models/marts/finance/fct_orders.sql", ["models/marts"])
    assert not sd.in_marts("models/staging/stg_orders.sql", ["models/marts"])
    assert not sd.in_marts("models/marts_old/fct.sql", ["models/marts"])


def test_column_types_read_casts_and_truncations_from_the_sql():
    pytest.importorskip("sqlglot")
    sql = (
        "select order_id, cast(created as timestamp) as created_at,\n"
        "  date_trunc('month', created) as order_month, label as ordered_label,\n"
        "  min(cast(created as date)) as first_day\n"
        "from {{ ref('stg_orders') }}\n"
    )

    found = sd.column_types(
        sql,
        None,
        ["created_at", "order_month", "ordered_label", "first_day"],
        dialect="duckdb",
        vars={},
    )

    assert found["created_at"] == ("timestamp", None)
    assert found["order_month"][1] == "month"
    assert found["ordered_label"] == (None, None)
    assert found["first_day"] == ("date", None)


# --- config -----------------------------------------------------------------------


def test_marts_paths_default_to_the_folder_init_scaffolds():
    assert TransformConfig().marts_paths == ["models/marts"]


@pytest.mark.parametrize("bad", ["/abs/marts", "../elsewhere", ""])
def test_marts_paths_must_stay_inside_the_project(bad):
    with pytest.raises(ValueError, match=r"transform\.marts_paths"):
        TransformConfig(marts_paths=[bad])


# --- through transform plan --------------------------------------------------------

MART_SQL = (
    "select 1 as order_id, 1 as customer_id, 'paid' as status,\n"
    "  cast('2024-01-01' as date) as ordered_at, 10.0 as order_total,\n"
    "  5.0 as balance\n"
)

MART = {
    "model": "fct_orders",
    "description": "One row per order.",
    "grain": ["order_id"],
    "columns": {
        "order_id": {"role": "key"},
        "customer_id": {"role": "foreign_key", "references": "stg_customers.id"},
        "status": {"role": "dimension", "description": "Lifecycle status."},
        "ordered_at": {"role": "time"},
        "order_total": {
            "role": "measure",
            "aggregation": "sum",
            "additivity": "additive",
        },
        "balance": {
            "role": "measure",
            "aggregation": "sum",
            "additivity": "semi_additive",
        },
    },
}

LAYER = (
    "# Hand-written semantic layer; this comment stays.\n"
    "semantic_models:\n"
    "  - name: customers\n"
    "    model: ref('stg_customers')\n"
    "    entities:\n"
    "      - name: customer\n"
    "        type: primary\n"
    "        expr: id\n"
)


def _run(argv: list[str], capsys) -> tuple[int, dict]:
    rc = main(argv)
    out = capsys.readouterr().out
    assert out.count("\n") == 1, "exactly one line on stdout"
    return rc, json.loads(out)


def _payload(tmp_path: Path, body: dict, name: str = "payload.json") -> str:
    path = tmp_path / name
    path.write_text(json.dumps(body), encoding="utf-8")
    return str(path)


def _add_time_spine(project: Path) -> None:
    (project / "models" / "metricflow_time_spine.sql").write_text(
        "select cast(range as date) as date_day\n"
        "from range(date '2020-01-01', date '2030-01-01', interval 1 day)\n",
        encoding="utf-8",
    )
    (project / "models" / "metricflow_time_spine.yml").write_text(
        "version: 2\n"
        "models:\n"
        "  - name: metricflow_time_spine\n"
        "    time_spine:\n"
        "      standard_granularity_column: date_day\n"
        "    columns:\n"
        "      - name: date_day\n"
        "        granularity: day\n",
        encoding="utf-8",
    )


def _add_layer(project: Path, content: str = LAYER) -> Path:
    path = project / "models" / "semantic" / "semantic.yml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _plan(
    tmp_path: Path,
    capsys,
    *,
    declaration: dict | None = None,
    sql_path: str = "models/marts/fct_orders.sql",
) -> tuple[int, dict]:
    body = {
        "edits": [{"path": sql_path, "kind": "model_sql", "content": MART_SQL}],
        "declarations": [declaration or MART],
    }
    return _run(
        [
            "--repo-root",
            str(tmp_path),
            "transform",
            "plan",
            "orders mart",
            "--edits-file",
            _payload(tmp_path, body),
        ],
        capsys,
    )


def _stored_content(tmp_path: Path, plan_id: str, path: str) -> str:
    stored = json.loads(
        (tmp_path / ".dex" / "plans" / f"{plan_id}.json").read_text(encoding="utf-8")
    )
    (edit,) = [e for e in stored["edits"] if e["path"] == path]
    return edit["new_content"]


@pytest.fixture
def real_dbt():
    pytest.importorskip("dbt.cli.main")


def test_without_a_semantic_layer_the_derivation_is_offered_and_nothing_is_added(
    dbt_project_dir: Path, tmp_path: Path, capsys, real_dbt
):
    rc, envelope = _plan(tmp_path, capsys)

    assert rc == 0, envelope
    proposal = envelope["data"]["semantic_proposal"]
    assert proposal["mode"] == "proposed"
    assert proposal["time_spine"] == "missing"
    assert envelope["data"]["paths"] == [
        "models/marts/fct_orders.sql",
        "models/marts/fct_orders.yml",
    ]
    model = proposal["models"]["fct_orders"]
    assert model["status"] == "proposed"
    assert model["validation"]["parse"]["result"] == "passed"
    assert "throwaway time spine" in model["validation"]["parse"]["detail"]
    (definition,) = proposal["definitions"]
    assert definition["path"] == "models/marts/fct_orders_semantic.yml"
    body = yaml.safe_load(definition["content"])
    assert body["name"] == "fct_orders"
    assert "metrics" not in body
    assert any("time spine" in n for n in proposal["notes"])
    assert "semantic plan --definitions-file" in proposal["next"]
    # Nothing was written to the project: no spine, no semantic file.
    assert not (dbt_project_dir / "models" / "dex_shadow_time_spine.sql").exists()
    assert not (dbt_project_dir / "models" / "marts").exists()


def test_the_proposal_passes_to_semantic_plan_as_it_stands(
    dbt_project_dir: Path, tmp_path: Path, capsys, real_dbt
):
    rc, envelope = _plan(tmp_path, capsys)
    assert rc == 0, envelope
    rc, applied = _run(["--repo-root", str(tmp_path), "transform", "apply"], capsys)
    assert rc == 0, applied
    _add_time_spine(dbt_project_dir)

    proposal = envelope["data"]["semantic_proposal"]
    rc, planned = _run(
        [
            "--repo-root",
            str(tmp_path),
            "semantic",
            "plan",
            "adopt the derived model",
            "--definitions-file",
            _payload(tmp_path, proposal, "proposal.json"),
        ],
        capsys,
    )

    assert rc == 0, planned
    assert planned["data"]["defined"] == ["fct_orders"]
    assert planned["data"]["paths"] == ["models/marts/fct_orders_semantic.yml"]


def test_with_a_semantic_layer_the_derivation_lands_in_the_same_plan(
    dbt_project_dir: Path, tmp_path: Path, capsys, real_dbt
):
    _add_time_spine(dbt_project_dir)
    _add_layer(dbt_project_dir)

    rc, envelope = _plan(tmp_path, capsys)

    assert rc == 0, envelope
    data = envelope["data"]
    proposal = data["semantic_proposal"]
    assert proposal["mode"] == "included"
    assert proposal["definitions"] == []
    model = proposal["models"]["fct_orders"]
    assert model["status"] == "included"
    assert {v["result"] for v in model["validation"].values()} == {"passed"}
    assert "models/semantic/semantic.yml" in data["paths"]
    assert any("added to this plan" in w for w in envelope["warnings"])
    written = _stored_content(tmp_path, data["plan_id"], "models/semantic/semantic.yml")
    assert written.startswith(LAYER)
    names = [m["name"] for m in yaml.safe_load(written)["semantic_models"]]
    assert names == ["customers", "fct_orders"]
    derived = yaml.safe_load(written)["semantic_models"][1]
    assert {"name": "customer", "type": "foreign", "expr": "customer_id"} in derived[
        "entities"
    ]


def test_a_layer_with_no_time_spine_is_offered_not_added(
    dbt_project_dir: Path, tmp_path: Path, capsys, real_dbt
):
    _add_layer(dbt_project_dir)

    rc, envelope = _plan(tmp_path, capsys)

    assert rc == 0, envelope
    proposal = envelope["data"]["semantic_proposal"]
    assert proposal["mode"] == "proposed"
    assert proposal["time_spine"] == "missing"
    assert "models/semantic/semantic.yml" not in envelope["data"]["paths"]


def test_an_authored_semantic_model_on_the_mart_is_never_overwritten(
    dbt_project_dir: Path, tmp_path: Path, capsys, real_dbt
):
    _add_time_spine(dbt_project_dir)
    authored = LAYER + (
        "  - name: orders\n"
        "    model: ref('fct_orders')\n"
        "    entities:\n"
        "      - name: order\n"
        "        type: primary\n"
        "        expr: order_id\n"
    )
    _add_layer(dbt_project_dir, authored)

    rc, envelope = _plan(tmp_path, capsys)

    assert rc == 0, envelope
    model = envelope["data"]["semantic_proposal"]["models"]["fct_orders"]
    assert model["status"] == "skipped"
    assert "'orders' in models/semantic/semantic.yml" in model["reason"]
    assert "models/semantic/semantic.yml" not in envelope["data"]["paths"]


def test_a_measure_name_another_model_holds_fails_rather_than_being_renamed(
    dbt_project_dir: Path, tmp_path: Path, capsys, real_dbt
):
    _add_time_spine(dbt_project_dir)
    _add_layer(
        dbt_project_dir,
        LAYER + "    measures:\n"
        "      - name: order_total\n"
        "        agg: count\n"
        "        expr: id\n",
    )

    rc, envelope = _plan(tmp_path, capsys)

    assert rc == 0, envelope
    model = envelope["data"]["semantic_proposal"]["models"]["fct_orders"]
    assert model["status"] == "failed"
    assert "measure 'order_total'" in model["reason"]
    assert "models/semantic/semantic.yml" not in envelope["data"]["paths"]
    assert any("not offered (failed)" in w for w in envelope["warnings"])


def _fake_parse(monkeypatch, *, fails_with_derivation=False, available=True):
    calls: list[list[str]] = []

    def shadow_parse(project, edits, *, target=None, **_kw):
        paths = [e.path for e in edits]
        calls.append(paths)
        if not available:
            return {
                "available": False,
                "reason": "dbt is not installed",
                "success": None,
                "messages": [],
            }
        derived = any(
            p.endswith("semantic.yml") and "fct_orders" in (e.new_content or "")
            for p, e in zip(paths, edits, strict=True)
        )
        if fails_with_derivation and derived:
            return {
                "available": True,
                "reason": None,
                "success": False,
                "messages": ["Parsing Error: a derived element collides"],
            }
        return {"available": True, "reason": None, "success": True, "messages": []}

    # `transform.build` the attribute is a function; the module is the target.
    monkeypatch.setattr(
        importlib.import_module("exmergo_dex_core.transform.build"),
        "shadow_parse",
        shadow_parse,
    )
    return calls


def test_a_derivation_dbt_cannot_parse_is_reported_and_the_plan_still_stores(
    dbt_project_dir: Path, tmp_path: Path, capsys, monkeypatch
):
    _add_time_spine(dbt_project_dir)
    _add_layer(dbt_project_dir)
    _fake_parse(monkeypatch, fails_with_derivation=True)

    rc, envelope = _plan(tmp_path, capsys)

    assert rc == 0, envelope
    model = envelope["data"]["semantic_proposal"]["models"]["fct_orders"]
    assert model["status"] == "failed"
    assert model["validation"]["parse"]["result"] == "failed"
    assert "collides" in model["validation"]["parse"]["detail"]
    assert "models/semantic/semantic.yml" not in envelope["data"]["paths"]


def test_without_dbt_a_derivation_is_never_included(
    dbt_project_dir: Path, tmp_path: Path, capsys, monkeypatch
):
    _add_time_spine(dbt_project_dir)
    _add_layer(dbt_project_dir)
    _fake_parse(monkeypatch, available=False)

    rc, envelope = _plan(tmp_path, capsys)

    assert rc == 0, envelope
    proposal = envelope["data"]["semantic_proposal"]
    model = proposal["models"]["fct_orders"]
    assert model["status"] == "unvalidated"
    assert model["validation"]["parse"]["result"] == "not_run"
    assert proposal["definitions"] == []
    assert "models/semantic/semantic.yml" not in envelope["data"]["paths"]


def test_no_parse_never_skips_the_parse_of_an_included_derivation(
    dbt_project_dir: Path, tmp_path: Path, capsys, monkeypatch
):
    _add_time_spine(dbt_project_dir)
    _add_layer(dbt_project_dir)
    (dbt_project_dir / "models" / "marts").mkdir()
    (dbt_project_dir / "models" / "marts" / "fct_orders.sql").write_text(
        MART_SQL, encoding="utf-8"
    )
    calls = _fake_parse(monkeypatch)

    rc, envelope = _run(
        [
            "--repo-root",
            str(tmp_path),
            "semantic",
            "plan",
            "declare the mart",
            "--no-parse",
            "--edits-file",
            _payload(
                tmp_path,
                {
                    "edits": [
                        {"path": "models/semantic/semantic.yml", "content": LAYER}
                    ],
                    "declarations": [MART],
                },
            ),
        ],
        capsys,
    )

    assert rc == 0, envelope
    model = envelope["data"]["semantic_proposal"]["models"]["fct_orders"]
    assert model["status"] == "included"
    assert calls, "the derivation was parsed despite --no-parse"
    assert "models/semantic/semantic.yml" in envelope["data"]["paths"]


def test_a_model_outside_the_marts_folders_derives_nothing(
    dbt_project_dir: Path, tmp_path: Path, capsys, monkeypatch
):
    _fake_parse(monkeypatch)

    rc, envelope = _plan(
        tmp_path,
        capsys,
        declaration={**MART, "model": "int_orders"},
        sql_path="models/intermediate/int_orders.sql",
    )

    assert rc == 0, envelope
    assert "semantic_proposal" not in envelope["data"]


def test_marts_paths_in_config_move_where_a_mart_is(
    dbt_project_dir: Path, tmp_path: Path, capsys, monkeypatch
):
    _fake_parse(monkeypatch)
    (tmp_path / ".dex").mkdir(exist_ok=True)
    (tmp_path / ".dex" / "config.yml").write_text(
        "connector: duckdb\ntransform:\n  marts_paths: [models/gold]\n",
        encoding="utf-8",
    )

    rc, envelope = _plan(tmp_path, capsys, sql_path="models/gold/fct_orders.sql")

    assert rc == 0, envelope
    assert envelope["data"]["semantic_proposal"]["models"]["fct_orders"]["status"] == (
        "proposed"
    )


def test_a_non_dbt_semantic_vendor_gets_its_own_derivation_and_no_metricflow(
    dbt_project_dir: Path, tmp_path: Path, capsys, monkeypatch
):
    """Under `vendor: ossie` the Ossie hook answers (#500): nothing
    MetricFlow-shaped is derived and dbt never parses a semantic file."""

    calls = _fake_parse(monkeypatch)
    (tmp_path / ".dex").mkdir(exist_ok=True)
    (tmp_path / ".dex" / "config.yml").write_text(
        "connector: duckdb\n"
        "semantic:\n"
        "  vendor: ossie\n"
        "  ossie:\n"
        "    files: [semantic/orders.ossie.yaml]\n",
        encoding="utf-8",
    )

    rc, envelope = _plan(tmp_path, capsys)

    assert rc == 0, envelope
    proposal = envelope["data"]["semantic_proposal"]
    assert proposal["vendor"] == "ossie"
    assert "semantic_models" not in json.dumps(proposal)
    assert not any(any("semantic" in p for p in c) for c in calls)


# --- population and the other declared metadata (#927) ------------------------------

POPULATED = {
    **MART,
    "population": {
        "rule": "Paid and shipped orders",
        "filters": [{"column": "status", "includes": ["paid", "shipped"]}],
    },
    "assumptions": [
        {"decision": "what counts", "chosen": "paid or shipped", "evidence": "request"}
    ],
    "columns": {
        **MART["columns"],
        "status": {**MART["columns"]["status"], "null_rule": "never"},
        "order_total": {**MART["columns"]["order_total"], "unit": "currency:EUR"},
    },
}


def _filters_anywhere(node: Any) -> list[Any]:
    """Every filter the definition applies; metadata (``config``) states, it
    does not apply, so the population's own ``filters`` there are not one."""

    if isinstance(node, dict):
        found = [v for k, v in node.items() if k in ("filter", "filters")]
        return found + [
            f for k, v in node.items() if k != "config" for f in _filters_anywhere(v)
        ]
    if isinstance(node, list):
        return [f for v in node for f in _filters_anywhere(v)]
    return []


def test_a_population_is_carried_as_metadata_and_never_as_a_filter():
    found = _derive(POPULATED)

    body = found.body
    assert body["config"]["meta"]["dex"]["population"] == POPULATED["population"]
    assert body["config"]["meta"]["dex"]["assumptions"] == POPULATED["assumptions"]
    assert body["description"] == (
        "One row per order. Population: Paid and shipped orders; status is one "
        "of 'paid' or 'shipped'."
    )
    # Never a filter: the mart's SQL already enforces the population.
    assert _filters_anywhere(body) == []
    (status,) = [d for d in body["dimensions"] if d["name"] == "status"]
    assert status["config"] == {"meta": {"dex": {"null_rule": "never"}}}
    (total,) = [m for m in body["measures"] if m["name"] == "order_total"]
    assert total["config"] == {"meta": {"dex": {"unit": "currency:EUR"}}}
    assert {"population", "assumptions", "columns.order_total.unit"} <= set(
        found.carried
    )


def test_every_declared_field_is_either_carried_or_named_with_why():
    declared = _declared(
        ordered_at=ORDERED_AT,
        note={"role": "attribute", "description": "Free text."},
    )
    declared["grain"] = ["order_id", "ordered_at"]

    found = _derive(declared)

    assert found.not_carried["grain"].startswith("composite")
    assert "no element for" in found.not_carried["columns.note.description"]
    stated = {"description", "grain", "columns.order_id.role"}
    assert stated <= set(found.carried) | set(found.not_carried)


def test_dbt_parses_the_metadata_a_derivation_carries(
    dbt_project_dir: Path, tmp_path: Path, capsys, real_dbt
):
    _add_time_spine(dbt_project_dir)
    _add_layer(dbt_project_dir)

    rc, envelope = _plan(tmp_path, capsys, declaration=POPULATED)

    assert rc == 0, envelope
    model = envelope["data"]["semantic_proposal"]["models"]["fct_orders"]
    assert model["status"] == "included", model
    assert model["validation"]["parse"]["result"] == "passed"


def test_the_local_catalog_omits_the_population_sentence_a_build_compiles(
    dbt_project_dir: Path, tmp_path: Path, capsys, real_dbt
):
    """#1005: the compiled manifest keeps the generated sentence and the structured
    population; the local catalog reads both and shows neither value."""

    _add_time_spine(dbt_project_dir)
    _add_layer(dbt_project_dir)
    (dbt_project_dir / "models" / "semantic" / "metrics.yml").write_text(
        "metrics:\n"
        "  - name: order_revenue\n"
        "    type: simple\n"
        "    label: Order revenue\n"
        "    type_params:\n"
        "      measure: order_total\n",
        encoding="utf-8",
    )
    rc, planned = _plan(tmp_path, capsys, declaration=POPULATED)
    assert rc == 0, planned
    root = ["--repo-root", str(tmp_path)]
    rc, applied = _run([*root, "transform", "apply"], capsys)
    assert rc == 0, applied
    rc, built = _run([*root, "transform", "build", "--target", "dev"], capsys)
    assert rc == 0, built

    # Where the build wrote it: Dex's isolated artifact directory (#1014).
    from exmergo_dex_core.dbt_project import semantic_manifest_file

    compiled = json.loads(semantic_manifest_file(dbt_project_dir).read_text("utf-8"))
    (entry,) = [m for m in compiled["semantic_models"] if m["name"] == "fct_orders"]
    assert "Population: Paid and shipped orders" in entry["description"]
    assert entry["config"]["meta"]["dex"]["population"] == POPULATED["population"]

    for mode, flags in (("full", []), ("values_free", ["--values-free"])):
        listing = [*root, "explore", "semantic", "list", "--local", *flags]
        rc, listed = _run(listing, capsys)
        assert rc == 0, listed
        assert listed["data"].get("result_mode", "full") == mode
        models = {m["name"]: m for m in listed["data"]["semantic_models"]}
        assert models["fct_orders"]["description"] == "One row per order."
        assert "Paid and shipped" not in json.dumps(listed)
        assert "'shipped'" not in json.dumps(listed)

    rc, searched = _run(
        [*root, "explore", "semantic", "list", "--local", "--search", "shipped"],
        capsys,
    )
    assert rc == 0, searched
    assert searched["data"]["metrics"] == []
