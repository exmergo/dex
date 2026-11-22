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


def test_a_column_named_for_a_time_granularity_fails_with_the_fix_named():
    found = _derive(_declared(year={"role": "dimension"}))

    assert found.status == "failed"
    assert found.blocked.startswith("year: the dimension would be named 'year'")
    assert "`expr: year`" in found.blocked
    assert "'fct_orders_year'" in found.blocked
    (warning,) = sd._warnings([found])
    assert "year: the dimension would be named 'year'" in warning
    assert "Encountered an error" not in warning


def test_a_reserved_time_column_also_names_the_references_to_repoint():
    found = _derive(_declared(month={"role": "time"}))

    assert found.status == "failed"
    assert found.blocked.startswith("month: the dimension would be named 'month'")
    assert "`defaults.agg_time_dimension`" in found.blocked
    assert "non_additive_dimension" not in found.blocked

    semi = _derive(
        _declared(
            month={"role": "time"},
            balance={
                "role": "measure",
                "aggregation": "sum",
                "additivity": "semi_additive",
            },
        )
    )

    assert semi.status == "failed"
    assert "`defaults.agg_time_dimension`" in semi.blocked
    assert "`non_additive_dimension.name`" in semi.blocked


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
    sql: str = MART_SQL,
    flags: tuple[str, ...] = (),
) -> tuple[int, dict]:
    body = {
        "edits": [{"path": sql_path, "kind": "model_sql", "content": sql}],
        "declarations": [declaration or MART],
    }
    return _run(
        [
            "--repo-root",
            str(tmp_path),
            "transform",
            "plan",
            "orders mart",
            *flags,
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


def test_a_no_parse_transform_plan_leaves_the_derivation_unvalidated(
    dbt_project_dir: Path, tmp_path: Path, capsys, monkeypatch
):
    # #1517: a host that may not run dbt where it authors still gets the
    # derivation as a proposal, never parsed and never added to the plan.
    _add_time_spine(dbt_project_dir)
    _add_layer(dbt_project_dir)
    calls = _fake_parse(monkeypatch)

    rc, envelope = _plan(tmp_path, capsys, flags=("--no-parse",))

    assert rc == 0, envelope
    assert calls == [], "a no-parse transform plan started dbt"
    proposal = envelope["data"]["semantic_proposal"]
    model = proposal["models"]["fct_orders"]
    assert model["status"] == "unvalidated"
    assert model["validation"]["parse"]["result"] == "not_run"
    assert "no_parse" in model["validation"]["parse"]["detail"]
    assert "models/semantic/semantic.yml" not in envelope["data"]["paths"]
    assert any("dbt parse not run" in w for w in envelope["warnings"])
    # Nothing was added, so it is offered for the side that parses.
    assert proposal["mode"] == "proposed"
    assert [d["path"] for d in proposal["definitions"]] == [
        "models/semantic/semantic.yml"
    ]
    assert "fct_orders" in proposal["definitions"][0]["content"]
    assert "where dbt may parse it" in proposal["next"]


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


def test_a_mart_named_with_double_underscore_derives_single_underscore_name():
    """Fivetran convention `intercom__threads` derives `intercom_threads`
    with model ref intact (#1207)."""
    found = sd.derive(
        "intercom__threads",
        _declared(
            status={"role": "dimension"},
            thread_count={"role": "measure", "aggregation": "sum"},
        ),
        column_types={},
        on_ref={},
    )
    assert found.status == "proposed"
    assert found.body["name"] == "intercom_threads"
    assert found.body["model"] == "ref('intercom__threads')"
    assert any(
        "intercom_threads" in n and "intercom__threads" in n for n in found.notes
    )
    assert any("MetricFlow does not allow '__'" in n for n in found.notes)


def test_a_double_underscore_mart_gets_proposal_passing_metricflow_validation(
    dbt_project_dir: Path, tmp_path: Path, capsys, real_dbt
):
    """End-to-end plan with a double-underscore mart proposes valid MetricFlow
    YAML (#1207)."""
    fivetran_mart = {
        "model": "intercom__threads",
        "description": "One row per conversation thread.",
        "grain": ["order_id"],
        "columns": {
            "order_id": {"role": "key"},
            "status": {"role": "dimension", "description": "Thread status."},
            "order_total": {
                "role": "measure",
                "aggregation": "sum",
            },
        },
    }
    rc, envelope = _plan(
        tmp_path,
        capsys,
        declaration=fivetran_mart,
        sql_path="models/marts/intercom__threads.sql",
    )
    assert rc == 0 and envelope["status"] == "ok", envelope
    proposal = envelope["data"]["semantic_proposal"]
    assert "intercom__threads" in proposal["models"]
    model_data = proposal["models"]["intercom__threads"]
    assert model_data["semantic_model"] == "intercom_threads"
    assert model_data["status"] == "proposed"
    assert "ref('intercom__threads')" in model_data["content"]
    assert "name: intercom_threads" in model_data["content"]
    assert not any("regex" in w.lower() for w in envelope.get("warnings", []))


def test_two_marts_that_normalize_to_one_name_cannot_both_derive(
    dbt_project_dir: Path, tmp_path: Path, capsys
):
    """`intercom__threads` and `intercom_threads` derive one MetricFlow name, so
    the second is refused rather than spliced over the first (#1207)."""
    thread_mart = {
        "description": "One row per thread.",
        "grain": ["order_id"],
        "columns": {
            "order_id": {"role": "key"},
            "status": {"role": "dimension"},
            "order_total": {"role": "measure", "aggregation": "sum"},
        },
    }
    body = {
        "edits": [
            {
                "path": "models/marts/intercom__threads.sql",
                "kind": "model_sql",
                "content": MART_SQL,
            },
            {
                "path": "models/marts/intercom_threads.sql",
                "kind": "model_sql",
                "content": MART_SQL,
            },
        ],
        "declarations": [
            {**thread_mart, "model": "intercom__threads"},
            {**thread_mart, "model": "intercom_threads"},
        ],
    }
    rc, envelope = _run(
        [
            "--repo-root",
            str(tmp_path),
            "transform",
            "plan",
            "threads",
            "--edits-file",
            _payload(tmp_path, body),
        ],
        capsys,
    )

    assert rc == 0 and envelope["status"] == "ok", json.dumps(envelope)[:1500]
    models = envelope["data"]["semantic_proposal"]["models"]
    first = models["intercom__threads"]
    second = models["intercom_threads"]
    assert first["status"] in ("proposed", "unvalidated")
    assert second["status"] == "failed"
    assert "already derived for intercom__threads" in second["reason"]
    assert "semantic model name 'intercom_threads'" in second["reason"]


def test_a_derivation_that_fails_validation_does_not_reserve_its_name(
    dbt_project_dir: Path, tmp_path: Path, capsys, monkeypatch
):
    """A refused first mart must not hold the name: the second mart normalizes to
    `intercom_threads` too, and if the first never reaches the plan the second is
    the one that owns the name."""
    real_validate = sd.validate_static
    calls = {"count": 0}

    def first_fails(derivation, view, payload_documents):
        calls["count"] += 1
        if calls["count"] == 1:
            derivation.validation["schema"] = {
                "result": "failed",
                "detail": "synthetic failure",
            }
            return False
        return real_validate(derivation, view, payload_documents)

    monkeypatch.setattr(sd, "validate_static", first_fails)

    thread_mart = {
        "description": "One row per thread.",
        "grain": ["order_id"],
        "columns": {
            "order_id": {"role": "key"},
            "status": {"role": "dimension"},
            "order_total": {"role": "measure", "aggregation": "sum"},
        },
    }
    body = {
        "edits": [
            {
                "path": "models/marts/intercom__threads.sql",
                "kind": "model_sql",
                "content": MART_SQL,
            },
            {
                "path": "models/marts/intercom_threads.sql",
                "kind": "model_sql",
                "content": MART_SQL,
            },
        ],
        "declarations": [
            {**thread_mart, "model": "intercom__threads"},
            {**thread_mart, "model": "intercom_threads"},
        ],
    }
    rc, envelope = _run(
        [
            "--repo-root",
            str(tmp_path),
            "transform",
            "plan",
            "threads",
            "--edits-file",
            _payload(tmp_path, body),
        ],
        capsys,
    )

    assert rc == 0 and envelope["status"] == "ok", json.dumps(envelope)[:1500]
    models = envelope["data"]["semantic_proposal"]["models"]
    assert models["intercom__threads"]["status"] == "failed"
    second = models["intercom_threads"]
    assert second["status"] in ("proposed", "unvalidated"), json.dumps(second)[:800]
    assert "already derived" not in second.get("reason", "")
    assert calls["count"] >= 2


DAILY_MART = {
    "model": "fct_world_output_daily",
    "description": "One row per day.",
    "grain": ["output_day"],
    "columns": {
        "output_day": {"role": "time"},
        "output_total": {
            "role": "measure",
            "aggregation": "sum",
            "additivity": "additive",
        },
    },
}


def test_a_time_column_grain_names_its_entity_apart_from_the_dimension():
    found = sd.derive(
        "fct_world_output_daily",
        DAILY_MART,
        column_types={"output_day": ("date", None)},
        on_ref={},
    )

    assert found.status == "proposed"
    assert found.body["entities"] == [
        {
            "name": "world_output_daily_output_day",
            "type": "primary",
            "expr": "output_day",
        }
    ]
    assert [d["name"] for d in found.body["dimensions"]] == ["output_day"]


# dbt validates the semantic manifest only when the project has a metric.
METRIC_LAYER = (
    LAYER
    + "    defaults:\n"
    + "      agg_time_dimension: signed_up_on\n"
    + "    dimensions:\n"
    + "      - name: signed_up_on\n"
    + "        type: time\n"
    + "        expr: current_date\n"
    + "        type_params:\n"
    + "          time_granularity: day\n"
    + "    measures:\n"
    + "      - name: customer_count\n"
    + "        agg: count\n"
    + "        expr: id\n"
    + "metrics:\n"
    + "  - name: customers\n"
    + "    label: Customers\n"
    + "    type: simple\n"
    + "    type_params:\n"
    + "      measure: customer_count\n"
)


def test_dbt_parses_a_mart_whose_grain_is_a_single_time_column(
    dbt_project_dir: Path, tmp_path: Path, capsys, real_dbt
):
    """The entity and the time dimension may not share a name (#1416)."""
    _add_time_spine(dbt_project_dir)
    _add_layer(dbt_project_dir, METRIC_LAYER)

    rc, envelope = _plan(
        tmp_path,
        capsys,
        declaration=DAILY_MART,
        sql_path="models/marts/fct_world_output_daily.sql",
        sql="select cast('2024-01-01' as date) as output_day, 10.0 as output_total\n",
    )

    assert rc == 0, envelope
    model = envelope["data"]["semantic_proposal"]["models"]["fct_world_output_daily"]
    assert model["status"] == "included", model
    assert model["validation"]["parse"]["result"] == "passed", model


def test_a_time_grain_entity_never_takes_a_name_a_dimension_already_has():
    """`fct_daily` + `output_day` would name the entity `daily_output_day`, which a
    categorical dimension already holds, so the entity takes a numbered name."""
    declared = {
        "grain": ["output_day"],
        "columns": {
            "output_day": {"role": "time"},
            "daily_output_day": {"role": "dimension"},
        },
    }

    found = sd.derive(
        "fct_daily",
        declared,
        column_types={"output_day": ("date", None)},
        on_ref={},
    )

    assert found.body["entities"] == [
        {"name": "daily_output_day_2", "type": "primary", "expr": "output_day"}
    ]
    assert {d["name"] for d in found.body["dimensions"]} == {
        "output_day",
        "daily_output_day",
    }


def test_a_dimension_never_takes_the_name_of_a_foreign_entity():
    declared = {
        "grain": ["reading_id"],
        "columns": {
            "reading_id": {"role": "key"},
            "read_at": {"role": "time"},
            "output_day_id": {
                "role": "foreign_key",
                "references": "fct_daily.output_day",
            },
            "daily_output_day": {"role": "dimension"},
            "reading_total": {"role": "measure", "aggregation": "sum"},
        },
    }
    parent = {
        "name": "daily",
        "entities": [
            {"name": "daily_output_day", "type": "primary", "expr": "output_day"}
        ],
    }

    found = sd.derive(
        "fct_readings",
        declared,
        column_types={"read_at": ("date", None)},
        on_ref={"fct_daily": [("models/marts/daily.yml", parent)]},
    )

    assert {"name": "daily_output_day", "type": "foreign", "expr": "output_day_id"} in (
        found.body["entities"]
    )
    assert [d["name"] for d in found.body["dimensions"]] == [
        "read_at",
        "readings_daily_output_day",
    ]
    assert found.body["dimensions"][1]["expr"] == "daily_output_day"


def test_a_replacement_name_never_takes_a_measure_name():
    """The dimension moved aside for the foreign entity would be named
    `readings_daily_output_day`, which a measure holds, so it takes a numbered name."""
    declared = {
        "grain": ["reading_id"],
        "columns": {
            "reading_id": {"role": "key"},
            "read_at": {"role": "time"},
            "output_day_id": {
                "role": "foreign_key",
                "references": "fct_daily.output_day",
            },
            "daily_output_day": {"role": "dimension"},
            "readings_daily_output_day": {"role": "measure", "aggregation": "sum"},
        },
    }
    parent = {
        "name": "daily",
        "entities": [
            {"name": "daily_output_day", "type": "primary", "expr": "output_day"}
        ],
    }

    found = sd.derive(
        "fct_readings",
        declared,
        column_types={"read_at": ("date", None)},
        on_ref={"fct_daily": [("models/marts/daily.yml", parent)]},
    )

    assert found.body["dimensions"][1] == {
        "name": "readings_daily_output_day_2",
        "type": "categorical",
        "expr": "daily_output_day",
    }
    assert [m["name"] for m in found.body["measures"]] == ["readings_daily_output_day"]


def test_a_time_grain_entity_never_takes_a_dimension_name_from_another_model():
    """MetricFlow checks one element type per name across every semantic model, so
    a dimension elsewhere in the layer holds the name as firmly as one here."""
    other = {
        "name": "calendar",
        "dimensions": [{"name": "daily_output_day", "type": "categorical"}],
    }

    found = sd.derive(
        "fct_daily",
        {"grain": ["output_day"], "columns": {"output_day": {"role": "time"}}},
        column_types={"output_day": ("date", None)},
        on_ref={"dim_calendar": [("models/marts/calendar.yml", other)]},
    )

    assert found.body["entities"] == [
        {"name": "daily_output_day_2", "type": "primary", "expr": "output_day"}
    ]


def test_a_child_joins_the_renamed_entity_of_a_parent_derived_in_the_same_plan(
    dbt_project_dir: Path, tmp_path: Path, capsys
):
    child = {
        "description": "One row per hourly reading.",
        "grain": ["reading_id"],
        "columns": {
            "reading_id": {"role": "key"},
            "read_at": {"role": "time"},
            "output_day_id": {
                "role": "foreign_key",
                "references": "fct_world_output_daily.output_day",
            },
            "world_output_daily_output_day": {"role": "dimension"},
            "reading_total": {"role": "measure", "aggregation": "sum"},
        },
    }
    body = {
        "edits": [
            {
                "path": "models/marts/fct_world_output_daily.sql",
                "kind": "model_sql",
                "content": (
                    "select cast('2024-01-01' as date) as output_day, "
                    "1.0 as output_total\n"
                ),
            },
            {
                "path": "models/marts/fct_readings.sql",
                "kind": "model_sql",
                "content": (
                    "select 1 as reading_id, cast('2024-01-01' as date) as read_at, "
                    "cast('2024-01-01' as date) as output_day_id, "
                    "'x' as world_output_daily_output_day, 1.0 as reading_total\n"
                ),
            },
        ],
        "declarations": [
            DAILY_MART,
            {**child, "model": "fct_readings"},
        ],
    }
    rc, envelope = _run(
        [
            "--repo-root",
            str(tmp_path),
            "transform",
            "plan",
            "readings",
            "--edits-file",
            _payload(tmp_path, body),
        ],
        capsys,
    )

    assert rc == 0 and envelope["status"] == "ok", json.dumps(envelope)[:1500]
    models = envelope["data"]["semantic_proposal"]["models"]
    assert models["fct_world_output_daily"]["status"] in ("proposed", "unvalidated")
    # the child's dimension holds the parent's first-choice name, so the parent's
    # entity is numbered and the child joins by that name, its dimension untouched
    assert (
        "name: world_output_daily_output_day_2\n"
        in models["fct_world_output_daily"]["content"]
    )
    content = models["fct_readings"]["content"]
    assert "name: world_output_daily_output_day_2\n" in content
    assert "expr: output_day_id\n" in content
    assert "name: output_day\n" not in content
    assert content.count("name: world_output_daily_output_day\n") == 1
    assert "readings_world_output_daily_output_day" not in content


def test_a_foreign_key_joins_by_a_name_the_primary_entity_gave_up():
    """The primary entity's first-choice name `output_day` is the calendar's entity:
    the primary takes its replacement first, so the foreign key still joins."""
    calendar = {
        "name": "calendar",
        "entities": [{"name": "output_day", "type": "primary", "expr": "calendar_key"}],
    }
    declared = {
        "grain": ["output_day"],
        "columns": {
            "output_day": {"role": "time"},
            "output_day_id": {
                "role": "foreign_key",
                "references": "fct_calendar.calendar_key",
            },
        },
    }

    found = sd.derive(
        "fct_daily",
        declared,
        column_types={"output_day": ("date", None)},
        on_ref={"fct_calendar": [("models/marts/calendar.yml", calendar)]},
    )

    assert found.body["entities"] == [
        {"name": "daily_output_day", "type": "primary", "expr": "output_day"},
        {"name": "output_day", "type": "foreign", "expr": "output_day_id"},
    ]
    (dimension,) = found.body["dimensions"]
    assert (dimension["name"], dimension["expr"]) == (
        "daily_output_day_2",
        "output_day",
    )
    assert found.body["defaults"] == {"agg_time_dimension": "daily_output_day_2"}


# The plan derives marts in name order, so one name sorts before the daily mart
# and one after: both declaration orders.
@pytest.mark.parametrize("other", ["fct_calendar", "fct_zz_calendar"])
def test_a_replacement_name_stays_clear_of_another_mart_in_the_plan(
    dbt_project_dir: Path, tmp_path: Path, capsys, other: str
):
    """An unrelated mart in the same plan has a dimension named the way the daily
    mart's entity would be; the entity is numbered whichever mart derives first."""
    calendar = {
        "model": other,
        "description": "One row per calendar key.",
        "grain": ["day_key"],
        "columns": {
            "day_key": {"role": "key"},
            "world_output_daily_output_day": {"role": "dimension"},
        },
    }
    body = {
        "edits": [
            {
                "path": "models/marts/fct_world_output_daily.sql",
                "kind": "model_sql",
                "content": (
                    "select cast('2024-01-01' as date) as output_day, "
                    "1.0 as output_total\n"
                ),
            },
            {
                "path": f"models/marts/{other}.sql",
                "kind": "model_sql",
                "content": (
                    "select 1 as day_key, 'x' as world_output_daily_output_day\n"
                ),
            },
        ],
        "declarations": [DAILY_MART, calendar],
    }
    rc, envelope = _run(
        [
            "--repo-root",
            str(tmp_path),
            "transform",
            "plan",
            "calendar",
            "--edits-file",
            _payload(tmp_path, body),
        ],
        capsys,
    )

    assert rc == 0 and envelope["status"] == "ok", json.dumps(envelope)[:1500]
    models = envelope["data"]["semantic_proposal"]["models"]
    daily = models["fct_world_output_daily"]["content"]
    assert "name: world_output_daily_output_day_2\n" in daily
    assert "name: world_output_daily_output_day\n" not in daily
    assert "name: world_output_daily_output_day\n" in models[other]["content"]


def test_a_fallback_entity_name_never_takes_a_dimension_name_the_plan_holds():
    """The foreign key's join name `record` is the primary entity's, so it falls
    back to `readings`, a dimension another mart in the plan declares: numbered."""
    records = {
        "name": "records",
        "entities": [{"name": "record", "type": "primary", "expr": "record_id"}],
    }
    declared = {
        "grain": ["record_id"],
        "columns": {
            "record_id": {"role": "key"},
            "id": {"role": "foreign_key", "references": "fct_records.record_id"},
            "read_at": {"role": "time"},
        },
    }

    found = sd.derive(
        "fct_readings",
        declared,
        column_types={"read_at": ("date", None)},
        on_ref={"fct_records": [("models/marts/records.yml", records)]},
        reserved={"dimension": {"readings"}},
    )

    assert found.body["entities"][1] == {
        "name": "readings_2",
        "type": "foreign",
        "expr": "id",
    }
    free = sd.derive(
        "fct_readings",
        declared,
        column_types={"read_at": ("date", None)},
        on_ref={"fct_records": [("models/marts/records.yml", records)]},
    )
    assert free.body["entities"][1]["name"] == "readings"


# Name order picks which mart of the cycle derives first: both orders.
@pytest.mark.parametrize("other", ["fct_snapshots", "fct_zz_snapshots"])
def test_marts_that_reference_each_other_join_by_final_entity_names(
    dbt_project_dir: Path, tmp_path: Path, capsys, other: str
):
    """Whichever mart of the cycle derives first, each foreign key joins the other
    mart's final entity name; the snapshots mart never takes the daily mart's time
    dimension name `output_day`."""
    daily = {
        **DAILY_MART,
        "columns": {
            **DAILY_MART["columns"],
            "latest_snapshot_id": {
                "role": "foreign_key",
                "references": f"{other}.snapshot_id",
            },
        },
    }
    snapshots = {
        "model": other,
        "description": "One row per snapshot.",
        "grain": ["snapshot_id"],
        "columns": {
            "snapshot_id": {"role": "key"},
            "taken_at": {"role": "time"},
            "output_day_id": {
                "role": "foreign_key",
                "references": "fct_world_output_daily.output_day",
            },
            "snapshot_total": {"role": "measure", "aggregation": "sum"},
        },
    }
    body = {
        "edits": [
            {
                "path": "models/marts/fct_world_output_daily.sql",
                "kind": "model_sql",
                "content": (
                    "select cast('2024-01-01' as date) as output_day, "
                    "1.0 as output_total, 1 as latest_snapshot_id\n"
                ),
            },
            {
                "path": f"models/marts/{other}.sql",
                "kind": "model_sql",
                "content": (
                    "select 1 as snapshot_id, cast('2024-01-01' as date) as "
                    "taken_at, cast('2024-01-01' as date) as output_day_id, "
                    "1.0 as snapshot_total\n"
                ),
            },
        ],
        "declarations": [daily, snapshots],
    }
    rc, envelope = _run(
        [
            "--repo-root",
            str(tmp_path),
            "transform",
            "plan",
            "cycle",
            "--edits-file",
            _payload(tmp_path, body),
        ],
        capsys,
    )

    assert rc == 0 and envelope["status"] == "ok", json.dumps(envelope)[:1500]
    offered = _offered(envelope)
    daily_entities = offered["fct_world_output_daily"]["entities"]
    snapshot_entities = offered[other]["entities"]
    assert {
        "name": "world_output_daily_output_day",
        "type": "primary",
        "expr": "output_day",
    } in daily_entities
    assert {"name": "snapshot", "type": "foreign", "expr": "latest_snapshot_id"} in (
        daily_entities
    )
    assert {"name": "snapshot", "type": "primary", "expr": "snapshot_id"} in (
        snapshot_entities
    )
    assert {
        "name": "world_output_daily_output_day",
        "type": "foreign",
        "expr": "output_day_id",
    } in snapshot_entities
    assert "output_day" not in {e["name"] for e in snapshot_entities}


def _offered(envelope: dict) -> dict[str, dict]:
    """The semantic models the plan offers for adoption, by name."""

    bodies = (
        yaml.safe_load(d["content"])
        for d in envelope["data"]["semantic_proposal"]["definitions"]
    )
    return {body["name"]: body for body in bodies}


@pytest.mark.parametrize("other", ["fct_a_snapshots", "fct_snapshots"])
def test_a_settled_primary_name_holds_whichever_mart_derives_first(
    dbt_project_dir: Path, tmp_path: Path, capsys, other: str
):
    """The snapshots mart's grain column is `daily_output_day`, the daily mart's
    first-choice entity name, and the two reference each other. The daily entity
    takes `daily_output_day_2` in the plan's first pass and keeps it, so each
    foreign key joins the other mart in either order."""
    daily = {
        "model": "fct_daily",
        "description": "One row per day.",
        "grain": ["output_day"],
        "columns": {
            "output_day": {"role": "time"},
            "b_id": {
                "role": "foreign_key",
                "references": f"{other}.daily_output_day",
            },
            "output_total": {"role": "measure", "aggregation": "sum"},
        },
    }
    snapshots = {
        "model": other,
        "description": "One row per snapshot.",
        "grain": ["daily_output_day"],
        "columns": {
            "daily_output_day": {"role": "key"},
            "taken_at": {"role": "time"},
            "output_day_id": {
                "role": "foreign_key",
                "references": "fct_daily.output_day",
            },
            "snapshot_total": {"role": "measure", "aggregation": "sum"},
        },
    }
    body = {
        "edits": [
            {
                "path": "models/marts/fct_daily.sql",
                "kind": "model_sql",
                "content": (
                    "select cast('2024-01-01' as date) as output_day, "
                    "1 as b_id, 1.0 as output_total\n"
                ),
            },
            {
                "path": f"models/marts/{other}.sql",
                "kind": "model_sql",
                "content": (
                    "select 1 as daily_output_day, cast('2024-01-01' as date) as "
                    "taken_at, cast('2024-01-01' as date) as output_day_id, "
                    "1.0 as snapshot_total\n"
                ),
            },
        ],
        "declarations": [daily, snapshots],
    }
    rc, envelope = _run(
        [
            "--repo-root",
            str(tmp_path),
            "transform",
            "plan",
            "settled",
            "--edits-file",
            _payload(tmp_path, body),
        ],
        capsys,
    )

    assert rc == 0 and envelope["status"] == "ok", json.dumps(envelope)[:1500]
    models = envelope["data"]["semantic_proposal"]["models"]
    daily_body = yaml.safe_load(models["fct_daily"]["content"])
    snapshots_body = yaml.safe_load(models[other]["content"])
    daily_entities = {e["name"]: e for e in daily_body["entities"]}
    snapshot_entities = {e["name"]: e for e in snapshots_body["entities"]}
    assert daily_entities["daily_output_day_2"]["type"] == "primary"
    assert snapshot_entities["daily_output_day"]["type"] == "primary"
    # each foreign key joins the other mart's primary entity by name
    assert snapshot_entities["daily_output_day_2"] == {
        "name": "daily_output_day_2",
        "type": "foreign",
        "expr": "output_day_id",
    }
    assert daily_entities["daily_output_day"] == {
        "name": "daily_output_day",
        "type": "foreign",
        "expr": "b_id",
    }


def test_two_settled_primary_names_never_meet(
    dbt_project_dir: Path, tmp_path: Path, capsys
):
    """`fct_a_b` (grain `cd`) and `fct_a` (grain `b_cd`) both reach `a_b_cd` for a
    replacement primary name. The one settled second is numbered, so the child's
    foreign key still joins the parent."""
    parent = {
        "model": "fct_a_b",
        "description": "One row per cd.",
        "grain": ["cd"],
        "columns": {
            "cd": {"role": "time"},
            "parent_total": {"role": "measure", "aggregation": "sum"},
        },
    }
    child = {
        "model": "fct_a",
        "description": "One row per b_cd.",
        "grain": ["b_cd"],
        "columns": {
            "b_cd": {"role": "time"},
            "parent_id": {"role": "foreign_key", "references": "fct_a_b.cd"},
            "child_total": {"role": "measure", "aggregation": "sum"},
        },
    }
    body = {
        "edits": [
            {
                "path": "models/marts/fct_a_b.sql",
                "kind": "model_sql",
                "content": (
                    "select cast('2024-01-01' as date) as cd, 1.0 as parent_total\n"
                ),
            },
            {
                "path": "models/marts/fct_a.sql",
                "kind": "model_sql",
                "content": (
                    "select cast('2024-01-01' as date) as b_cd, "
                    "cast('2024-01-01' as date) as parent_id, 1.0 as child_total\n"
                ),
            },
        ],
        "declarations": [parent, child],
    }
    rc, envelope = _run(
        [
            "--repo-root",
            str(tmp_path),
            "transform",
            "plan",
            "settled",
            "--edits-file",
            _payload(tmp_path, body),
        ],
        capsys,
    )

    assert rc == 0 and envelope["status"] == "ok", json.dumps(envelope)[:1500]
    models = envelope["data"]["semantic_proposal"]["models"]
    for model in ("fct_a_b", "fct_a"):
        assert models[model]["status"] in ("proposed", "unvalidated"), json.dumps(
            models[model]
        )[:1500]
    parent_entities = yaml.safe_load(models["fct_a_b"]["content"])["entities"]
    child_entities = yaml.safe_load(models["fct_a"]["content"])["entities"]
    (parent_primary,) = [e["name"] for e in parent_entities if e["type"] == "primary"]
    (child_primary,) = [e["name"] for e in child_entities if e["type"] == "primary"]
    assert {parent_primary, child_primary} == {"a_b_cd", "a_b_cd_2"}
    assert {"name": parent_primary, "type": "foreign", "expr": "parent_id"} in (
        child_entities
    )


@pytest.mark.parametrize("other", ["fct_a_snapshots", "fct_snapshots"])
def test_a_reference_to_a_non_grain_column_joins_the_same_name_in_a_cycle(
    dbt_project_dir: Path, tmp_path: Path, capsys, other: str
):
    """`buyer_id` references the snapshots mart's `account_id`, which joins the
    authored `customer` entity; the daily mart joins `customer` whichever mart of
    the cycle derives first."""
    _add_layer(dbt_project_dir)
    daily = {
        "model": "fct_daily",
        "description": "One row per day.",
        "grain": ["output_day"],
        "columns": {
            "output_day": {"role": "time"},
            "buyer_id": {"role": "foreign_key", "references": f"{other}.account_id"},
            "output_total": {"role": "measure", "aggregation": "sum"},
        },
    }
    snapshots = {
        "model": other,
        "description": "One row per snapshot.",
        "grain": ["snapshot_id"],
        "columns": {
            "snapshot_id": {"role": "key"},
            "taken_at": {"role": "time"},
            "account_id": {"role": "foreign_key", "references": "stg_customers.id"},
            "output_day_id": {
                "role": "foreign_key",
                "references": "fct_daily.output_day",
            },
            "snapshot_total": {"role": "measure", "aggregation": "sum"},
        },
    }
    body = {
        "edits": [
            {
                "path": "models/marts/fct_daily.sql",
                "kind": "model_sql",
                "content": (
                    "select cast('2024-01-01' as date) as output_day, "
                    "1 as buyer_id, 1.0 as output_total\n"
                ),
            },
            {
                "path": f"models/marts/{other}.sql",
                "kind": "model_sql",
                "content": (
                    "select 1 as snapshot_id, cast('2024-01-01' as date) as "
                    "taken_at, 1 as account_id, cast('2024-01-01' as date) as "
                    "output_day_id, 1.0 as snapshot_total\n"
                ),
            },
        ],
        "declarations": [daily, snapshots],
    }
    rc, envelope = _run(
        [
            "--repo-root",
            str(tmp_path),
            "transform",
            "plan",
            "cycle",
            "--edits-file",
            _payload(tmp_path, body),
        ],
        capsys,
    )

    assert rc == 0 and envelope["status"] == "ok", json.dumps(envelope)[:1500]
    offered = _offered(envelope)
    assert {"name": "customer", "type": "foreign", "expr": "buyer_id"} in (
        offered["fct_daily"]["entities"]
    )
    assert {"name": "customer", "type": "foreign", "expr": "account_id"} in (
        offered[other]["entities"]
    )


def test_a_numbered_entity_never_takes_a_parent_join_name(
    dbt_project_dir: Path, tmp_path: Path, capsys
):
    """`fct_output` settles `output_day_2`. In the child, `output_day_id` names a
    dimension's name and is numbered before `parent_day_id`, which references
    `fct_output.day_2`: the number skips `output_day_2`, which stays the join."""
    parent = {
        "model": "fct_output",
        "description": "One row per day.",
        "grain": ["day_2"],
        "columns": {
            "day_2": {"role": "time"},
            "output_total": {"role": "measure", "aggregation": "sum"},
        },
    }
    child = {
        "model": "fct_child",
        "description": "One row per child.",
        "grain": ["child_id"],
        "columns": {
            "child_id": {"role": "key"},
            "read_at": {"role": "time"},
            "output_day": {"role": "dimension"},
            "output_day_id": {"role": "foreign_key"},
            "parent_day_id": {"role": "foreign_key", "references": "fct_output.day_2"},
            "child_total": {"role": "measure", "aggregation": "sum"},
        },
    }
    body = {
        "edits": [
            {
                "path": "models/marts/fct_output.sql",
                "kind": "model_sql",
                "content": (
                    "select cast('2024-01-01' as date) as day_2, 1.0 as output_total\n"
                ),
            },
            {
                "path": "models/marts/fct_child.sql",
                "kind": "model_sql",
                "content": (
                    "select 1 as child_id, cast('2024-01-01' as date) as read_at, "
                    "'x' as output_day, 1 as output_day_id, "
                    "cast('2024-01-01' as date) as parent_day_id, 1.0 as child_total\n"
                ),
            },
        ],
        "declarations": [parent, child],
    }
    rc, envelope = _run(
        [
            "--repo-root",
            str(tmp_path),
            "transform",
            "plan",
            "numbered",
            "--edits-file",
            _payload(tmp_path, body),
        ],
        capsys,
    )

    assert rc == 0 and envelope["status"] == "ok", json.dumps(envelope)[:1500]
    offered = _offered(envelope)
    assert {"name": "output_day_2", "type": "primary", "expr": "day_2"} in (
        offered["fct_output"]["entities"]
    )
    child_entities = offered["fct_child"]["entities"]
    assert {"name": "output_day_2", "type": "foreign", "expr": "parent_day_id"} in (
        child_entities
    )
    (numbered,) = [e for e in child_entities if e["expr"] == "output_day_id"]
    assert numbered["name"] not in {"output_day", "output_day_2"}


def test_a_moved_dimension_never_takes_a_dimension_name_from_another_model():
    """The time dimension moves aside for the foreign entity `output_day`; another
    model's categorical dimension holds `readings_output_day`, so it is numbered and
    the default time dimension follows it."""
    parent = {
        "name": "daily",
        "entities": [{"name": "output_day", "type": "primary", "expr": "output_day"}],
    }
    other = {
        "name": "calendar",
        "dimensions": [{"name": "readings_output_day", "type": "categorical"}],
    }
    declared = {
        "grain": ["reading_id"],
        "columns": {
            "reading_id": {"role": "key"},
            "output_day": {"role": "time"},
            "output_day_id": {
                "role": "foreign_key",
                "references": "fct_daily.output_day",
            },
            "reading_total": {"role": "measure", "aggregation": "sum"},
        },
    }

    found = sd.derive(
        "fct_readings",
        declared,
        column_types={"output_day": ("date", None)},
        on_ref={
            "fct_daily": [("models/marts/daily.yml", parent)],
            "dim_calendar": [("models/marts/calendar.yml", other)],
        },
    )

    (dimension,) = found.body["dimensions"]
    assert (dimension["name"], dimension["expr"]) == (
        "readings_output_day_2",
        "output_day",
    )
    assert found.body["defaults"] == {"agg_time_dimension": "readings_output_day_2"}


def test_a_foreign_key_joins_a_parent_column_that_is_itself_a_planned_foreign_key(
    dbt_project_dir: Path, tmp_path: Path, capsys
):
    """`fct_returns.buyer_id` references `fct_orders.customer_id`, which joins the
    planned `fct_customers` entity `customer`: the returns mart joins `customer`
    too, not a `buyer` fallback that joins nothing."""

    def mart(model: str, grain: str, **foreign: str) -> dict:
        return {
            "model": model,
            "description": f"One row per {grain}.",
            "grain": [grain],
            "columns": {
                grain: {"role": "key"},
                "happened_at": {"role": "time"},
                **{
                    column: {"role": "foreign_key", "references": ref}
                    for column, ref in foreign.items()
                },
                "amount": {"role": "measure", "aggregation": "sum"},
            },
        }

    marts = [
        mart("fct_customers", "customer_id"),
        mart("fct_orders", "order_id", customer_id="fct_customers.customer_id"),
        mart("fct_returns", "return_id", buyer_id="fct_orders.customer_id"),
    ]
    body = {
        "edits": [
            {
                "path": f"models/marts/{m['model']}.sql",
                "kind": "model_sql",
                "content": (
                    "select "
                    + ", ".join(
                        f"1 as {c}"
                        if c != "happened_at"
                        else "current_date as happened_at"
                        for c in m["columns"]
                    )
                    + "\n"
                ),
            }
            for m in marts
        ],
        "declarations": marts,
    }
    rc, envelope = _run(
        [
            "--repo-root",
            str(tmp_path),
            "transform",
            "plan",
            "chain",
            "--edits-file",
            _payload(tmp_path, body),
        ],
        capsys,
    )

    assert rc == 0 and envelope["status"] == "ok", json.dumps(envelope)[:1500]
    offered = _offered(envelope)
    assert {"name": "customer", "type": "foreign", "expr": "customer_id"} in (
        offered["fct_orders"]["entities"]
    )
    assert {"name": "customer", "type": "foreign", "expr": "buyer_id"} in (
        offered["fct_returns"]["entities"]
    )
