"""`maintain coverage`'s inventory, over hand-built manifests (#1280).

Each manifest here is shaped like dbt 1.12's parse output; the real-dbt
round trip is in `test_coverage_dbt.py`.
"""

from __future__ import annotations

import json
from typing import Any

from exmergo_dex_core.maintain import coverage as coverage_mod

PROJECT = "shop"


def _model(name: str, *, columns=(), materialized="table", reads=(), **extra) -> dict:
    node = {
        "unique_id": None,
        "resource_type": "model",
        "name": name,
        "package_name": extra.pop("package", PROJECT),
        "original_file_path": f"models/{name}.sql",
        "config": {"materialized": materialized, "enabled": True},
        "columns": {c: {"name": c} for c in columns},
        "depends_on": {"nodes": list(reads)},
    }
    node.update(extra)
    return node


def _generic(
    test: str,
    on: str | None,
    column: str | None = None,
    *,
    reads=None,
    severity="ERROR",
    enabled=True,
    package=PROJECT,
    namespace=None,
    name=None,
) -> dict:
    return {
        "resource_type": "test",
        "name": name or f"{test}_{column}",
        "package_name": package,
        "attached_node": on,
        "column_name": column,
        "test_metadata": {"name": test, "namespace": namespace, "kwargs": {}},
        "config": {"severity": severity, "enabled": enabled},
        "depends_on": {"nodes": list(reads if reads is not None else [on])},
    }


def _singular(name: str, reads, *, severity="ERROR") -> dict:
    return {
        "resource_type": "test",
        "name": name,
        "package_name": PROJECT,
        "attached_node": None,
        "column_name": None,
        "config": {"severity": severity, "enabled": True},
        "depends_on": {"nodes": list(reads)},
    }


def _unit(name: str, model: str, *, incremental=None, version=None) -> dict:
    overrides: dict[str, Any] = {"macros": {}}
    if incremental is not None:
        overrides["macros"]["is_incremental"] = incremental
    uid = f"model.{PROJECT}.{model}" + (f".v{version}" if version else "")
    return {
        "name": name,
        "model": model,
        "version": version,
        "package_name": PROJECT,
        "overrides": overrides,
        "depends_on": {"nodes": [uid]},
    }


def _source(name: str, *, columns=(), freshness=None) -> dict:
    return {
        "resource_type": "source",
        "name": name,
        "source_name": "raw",
        "identifier": name,
        "package_name": PROJECT,
        "original_file_path": "models/sources.yml",
        "config": {"enabled": True, "freshness": freshness},
        "freshness": freshness,
        "columns": {c: {"name": c} for c in columns},
    }


def _manifest(nodes=None, sources=None, units=None, disabled=None) -> dict:
    return {
        "metadata": {"project_name": PROJECT},
        "nodes": nodes or {},
        "sources": sources or {},
        "unit_tests": units or {},
        "disabled": disabled or {},
    }


def _record(inventory, section: str, name: str) -> dict:
    return next(r for r in inventory.sections[section] if r["name"] == name)


M = f"model.{PROJECT}"


def test_each_test_kind_counts_apart_with_its_severity():
    manifest = _manifest(
        nodes={
            f"{M}.orders": _model("orders", columns=["id", "status"]),
            "test.a": _generic("unique", f"{M}.orders", "id"),
            "test.b": _generic("not_null", f"{M}.orders", "id", severity="warn"),
            "test.c": _singular("assert_ok", [f"{M}.orders"]),
        },
        units={"unit_test.a": _unit("ut_orders", "orders")},
    )
    inv = coverage_mod.inventory(manifest)
    orders = _record(inv, "models", "orders")
    assert orders["own_tests"] == {"generic": 2, "singular": 1, "unit": 1}
    assert orders["severity"] == {"error": 2, "warn": 1}
    assert inv.summary["tests"]["by_kind"] == {
        "generic": 2,
        "singular": 1,
        "unit": 1,
        "freshness": 0,
    }
    assert inv.summary["tests"]["by_severity"] == {"error": 2, "warn": 1}


def test_a_disabled_test_counts_nowhere_but_excluded():
    manifest = _manifest(
        nodes={
            f"{M}.orders": _model("orders", columns=["id"]),
            "test.a": _generic("unique", f"{M}.orders", "id", enabled=False),
        },
        disabled={"test.b": [_generic("not_null", f"{M}.orders", "id", enabled=False)]},
    )
    inv = coverage_mod.inventory(manifest)
    orders = _record(inv, "models", "orders")
    assert orders["tested"] is False
    assert orders["columns"]["asserted"] == 0
    assert inv.excluded["tests"]["disabled"] == 2


def test_package_and_disabled_resources_leave_every_denominator():
    manifest = _manifest(
        nodes={
            f"{M}.orders": _model("orders"),
            "model.utils.helper": _model("helper", package="utils"),
            "test.p": _generic("not_null", "model.utils.helper", "x", package="utils"),
            "test.q": _generic("not_null", "model.utils.helper", "x"),
        },
        disabled={f"{M}.old": [_model("old", config={"enabled": False})]},
    )
    inv = coverage_mod.inventory(manifest)
    assert [r["name"] for r in inv.sections["models"]] == ["orders"]
    assert inv.summary["models"]["with_own_tests"] == {
        "numerator": 0,
        "denominator": 1,
        "of": "enabled project models",
    }
    reasons = {(e["name"], e["reason"]) for e in inv.excluded["resources"]}
    assert reasons == {("helper", "package"), ("old", "disabled")}
    assert inv.excluded["by_reason"] == {"package": 1, "disabled": 1}
    assert inv.excluded["tests"]["package"] == 1
    assert inv.excluded["tests"]["on_excluded_resource"] == 1


def test_an_ephemeral_model_is_counted_and_marked():
    manifest = _manifest(
        nodes={
            f"{M}.stg": _model("stg", columns=["id"], materialized="ephemeral"),
            "test.a": _generic("not_null", f"{M}.stg", "id"),
        }
    )
    inv = coverage_mod.inventory(manifest)
    stg = _record(inv, "models", "stg")
    assert stg["ephemeral"] is True
    assert stg["tested"] is True
    assert inv.summary["models"]["ephemeral"] == 1
    assert any("ephemeral" in note for note in inv.notes)


def test_downstream_tests_are_listed_apart_and_never_own_coverage():
    manifest = _manifest(
        nodes={
            f"{M}.stg": _model("stg", columns=["id"]),
            f"{M}.fct": _model("fct", columns=["id"], reads=[f"{M}.stg"]),
            f"{M}.agg": _model("agg", reads=[f"{M}.fct"]),
            "test.a": _generic("unique", f"{M}.fct", "id"),
            "test.b": _singular("assert_agg", [f"{M}.agg"]),
        },
        units={"unit_test.a": _unit("ut_fct", "fct")},
    )
    inv = coverage_mod.inventory(manifest)
    stg = _record(inv, "models", "stg")
    assert stg["tested"] is False
    assert stg["own_tests"] == {"generic": 0, "singular": 0, "unit": 0}
    # The unit test on fct reads fixtures, not stg, so it protects nothing here.
    assert stg["downstream_tests"] == {"count": 2, "on": ["agg", "fct"]}
    assert inv.summary["models"]["downstream_only"]["numerator"] == 1


def test_a_relationships_test_is_its_childs_own_and_its_parents_downstream():
    manifest = _manifest(
        nodes={
            f"{M}.customers": _model("customers", columns=["id"]),
            f"{M}.orders": _model("orders", columns=["customer_id"]),
            "test.r": _generic(
                "relationships",
                f"{M}.orders",
                "customer_id",
                reads=[f"{M}.customers", f"{M}.orders"],
            ),
        }
    )
    inv = coverage_mod.inventory(manifest)
    assert _record(inv, "models", "orders")["own_tests"]["generic"] == 1
    customers = _record(inv, "models", "customers")
    assert customers["tested"] is False
    assert customers["downstream_tests"]["count"] == 1


def test_an_incremental_model_reports_whether_any_unit_test_takes_the_branch():
    manifest = _manifest(
        nodes={
            f"{M}.full_only": _model("full_only", materialized="incremental"),
            f"{M}.both": _model("both", materialized="incremental"),
        },
        units={
            "unit_test.a": _unit("ut_full", "full_only", incremental=False),
            "unit_test.b": _unit("ut_b_full", "both", incremental=False),
            "unit_test.c": _unit("ut_b_incr", "both", incremental=True),
        },
    )
    inv = coverage_mod.inventory(manifest)
    assert _record(inv, "models", "full_only")["incremental"] == {
        "unit_tests": 1,
        "incremental_branch_tests": [],
        "incremental_branch_tested": False,
    }
    assert _record(inv, "models", "both")["incremental"][
        "incremental_branch_tests"
    ] == ["ut_b_incr"]
    assert inv.summary["models"]["incremental_branch_tested"] == {
        "numerator": 1,
        "denominator": 2,
        "of": "enabled incremental project models",
    }


def test_a_unit_test_binds_to_the_version_it_tests():
    manifest = _manifest(
        nodes={
            f"{M}.dim.v1": _model("dim", version=1),
            f"{M}.dim.v2": _model("dim", version=2),
        },
        units={"unit_test.a": _unit("ut_dim", "dim", version=2)},
    )
    inv = coverage_mod.inventory(manifest)
    assert _record(inv, "models", "dim.v2")["own_tests"]["unit"] == 1
    assert _record(inv, "models", "dim.v1")["tested"] is False


def test_source_tests_and_freshness_attach_through_their_dependency():
    fresh = {
        "warn_after": {"count": 12, "period": "hour"},
        "error_after": {"count": None, "period": None},
    }
    manifest = _manifest(
        nodes={
            # A source's generic test carries no attached_node.
            "test.s": _generic(
                "not_null", None, "id", reads=[f"source.{PROJECT}.raw.orders"]
            )
        },
        sources={
            f"source.{PROJECT}.raw.orders": _source(
                "orders", columns=["id"], freshness=fresh
            ),
            f"source.{PROJECT}.raw.events": _source("events"),
        },
    )
    inv = coverage_mod.inventory(manifest)
    orders = _record(inv, "sources", "raw.orders")
    assert orders["own_tests"] == {"generic": 1, "singular": 0, "freshness": 1}
    assert {"kind": "freshness", "severity": "warn"} in orders["tests"]
    assert inv.summary["sources"]["with_freshness"] == {
        "numerator": 1,
        "denominator": 2,
        "of": "enabled project sources",
    }
    assert inv.summary["sources"]["columns_asserted"]["numerator"] == 1


def test_tests_on_snapshots_and_seeds_count_in_their_own_sections():
    snap = _model("snap", columns=["id"], resource_type="snapshot")
    seed = _model("countries", columns=["code"], resource_type="seed")
    manifest = _manifest(
        nodes={
            f"snapshot.{PROJECT}.snap": snap,
            f"seed.{PROJECT}.countries": seed,
            "test.a": _generic("not_null", f"snapshot.{PROJECT}.snap", "id"),
            "test.b": _generic("unique", f"seed.{PROJECT}.countries", "code"),
        }
    )
    inv = coverage_mod.inventory(manifest)
    assert _record(inv, "snapshots", "snap")["own_tests"] == {
        "generic": 1,
        "singular": 0,
    }
    assert inv.summary["seeds"]["columns_asserted"] == {
        "numerator": 1,
        "denominator": 1,
        "of": "documented columns of enabled project seeds",
    }
    assert inv.summary["models"]["count"] == 0


def test_columns_asserted_against_documented_and_undocumented_listed_apart():
    manifest = _manifest(
        nodes={
            f"{M}.orders": _model("orders", columns=["id", "Amount", "status"]),
            "test.a": _generic("not_null", f"{M}.orders", "id"),
            "test.b": _generic("not_null", f"{M}.orders", "amount"),
            "test.c": _generic("not_null", f"{M}.orders", "ghost"),
            "test.d": _generic(
                "unique_combination_of_columns",
                f"{M}.orders",
                None,
                namespace="dbt_utils",
            ),
        }
    )
    inv = coverage_mod.inventory(manifest)
    columns = _record(inv, "models", "orders")["columns"]
    assert columns == {
        "documented": 3,
        "asserted": 2,
        "unasserted": ["status"],
        "undocumented_tested": ["ghost"],
    }
    assert inv.summary["models"]["columns_asserted"] == {
        "numerator": 2,
        "denominator": 3,
        "of": "documented columns of enabled project models",
    }
    tests = _record(inv, "models", "orders")["tests"]
    assert {
        "kind": "generic",
        "severity": "error",
        "test": ("dbt_utils.unique_combination_of_columns"),
    } in tests


def test_a_generic_tests_node_name_never_reaches_the_payload():
    manifest = _manifest(
        nodes={
            f"{M}.orders": _model("orders", columns=["status"]),
            "test.a": _generic(
                "accepted_values",
                f"{M}.orders",
                "status",
                name="accepted_values_orders_status__SENTINEL_VALUE",
            ),
        }
    )
    inv = coverage_mod.inventory(manifest)
    payload = json.dumps(
        [inv.summary, inv.sections, inv.excluded, inv.notes, inv.warnings]
    )
    assert "SENTINEL_VALUE" not in payload
    assert "accepted_values" in payload


def test_the_selector_narrows_every_section_and_its_denominators():
    manifest = _manifest(
        nodes={
            f"{M}.orders": _model("orders"),
            f"{M}.customers": _model("customers"),
            f"{M}.dim.v2": _model(
                "dim", version=2, original_file_path="models/dim_v2.sql"
            ),
        },
        sources={f"source.{PROJECT}.raw.events": _source("events")},
    )
    inv = coverage_mod.inventory(
        manifest, selector=["orders", "raw.events", "dim_v2", "nope"]
    )
    assert inv.scope == {"selected": ["dim.v2", "orders", "raw.events"]}
    assert inv.summary["models"]["with_own_tests"] == {
        "numerator": 0,
        "denominator": 2,
        "of": "selected enabled project models",
    }
    assert inv.summary["sources"]["count"] == 1
    assert any("'nope'" in warning for warning in inv.warnings)


def test_a_selected_models_downstream_tests_come_from_unselected_models():
    manifest = _manifest(
        nodes={
            f"{M}.stg": _model("stg"),
            f"{M}.fct": _model("fct", columns=["id"], reads=[f"{M}.stg"]),
            "test.a": _generic("unique", f"{M}.fct", "id"),
        }
    )
    inv = coverage_mod.inventory(manifest, selector=["stg"])
    assert _record(inv, "models", "stg")["downstream_tests"]["count"] == 1
    assert inv.summary["tests"]["by_kind"]["generic"] == 0


def test_caps_elide_untested_last_and_count_every_cut():
    nodes: dict[str, dict] = {}
    for index in range(coverage_mod.RESOURCE_CAP + 5):
        name = f"m{index:03d}"
        columns = [f"c{c}" for c in range(coverage_mod.COLUMN_CAP + 3)]
        nodes[f"{M}.{name}"] = _model(name, columns=columns)
    for index in range(coverage_mod.TEST_CAP + 2):
        nodes[f"test.t{index}"] = _generic("not_null", f"{M}.m000", f"c{index}")
    inv = coverage_mod.inventory(_manifest(nodes=nodes))
    models = inv.sections["models"]
    assert len(models) == coverage_mod.RESOURCE_CAP
    # Untested resources sort first, so the cut drops tested ones.
    assert "m000" not in {r["name"] for r in models}
    assert inv.elided["models"] == 5
    assert inv.summary["models"]["count"] == coverage_mod.RESOURCE_CAP + 5
    assert inv.elided["columns"] == 3 * coverage_mod.RESOURCE_CAP
    assert any("are not listed" in note for note in inv.notes)

    full = coverage_mod.inventory(_manifest(nodes=nodes), full=True)
    assert len(full.sections["models"]) == coverage_mod.RESOURCE_CAP + 5
    assert all(count == 0 for count in full.elided.values())
    m000 = _record(full, "models", "m000")
    assert len(m000["tests"]) == coverage_mod.TEST_CAP + 2


def test_a_per_resource_test_cut_is_counted():
    nodes = {f"{M}.orders": _model("orders")}
    for index in range(coverage_mod.TEST_CAP + 4):
        nodes[f"test.t{index}"] = _singular(f"s{index}", [f"{M}.orders"])
    inv = coverage_mod.inventory(_manifest(nodes=nodes))
    orders = _record(inv, "models", "orders")
    assert len(orders["tests"]) == coverage_mod.TEST_CAP
    assert orders["own_tests"]["singular"] == coverage_mod.TEST_CAP + 4
    assert inv.elided["tests"] == 4


def test_a_source_relationships_test_is_the_sources_own():
    source = f"source.{PROJECT}.raw.orders"
    manifest = _manifest(
        nodes={
            f"{M}.customers": _model("customers", columns=["id"]),
            # No attached_node, and it reads both the source and the model.
            "test.r": _generic(
                "relationships",
                None,
                "customer_id",
                reads=[f"{M}.customers", source],
            ),
        },
        sources={source: _source("orders", columns=["customer_id"])},
    )
    inv = coverage_mod.inventory(manifest)
    orders = _record(inv, "sources", "raw.orders")
    assert orders["own_tests"]["generic"] == 1
    assert orders["columns"]["asserted"] == 1
    assert inv.excluded["tests"]["on_excluded_resource"] == 0
    assert _record(inv, "models", "customers")["downstream_tests"]["count"] == 1


def test_the_incremental_branch_test_names_are_capped_and_counted():
    extra = 3
    units = {
        f"unit_test.{index}": _unit(f"ut_{index:02d}", "fct", incremental=True)
        for index in range(coverage_mod.TEST_CAP + extra)
    }
    manifest = _manifest(
        nodes={f"{M}.fct": _model("fct", materialized="incremental")}, units=units
    )
    inv = coverage_mod.inventory(manifest)
    incremental = _record(inv, "models", "fct")["incremental"]
    assert len(incremental["incremental_branch_tests"]) == coverage_mod.TEST_CAP
    assert incremental["unit_tests"] == coverage_mod.TEST_CAP + extra
    assert incremental["incremental_branch_tested"] is True
    # The branch names and the tests list are each cut by `extra`.
    assert inv.elided["tests"] == 2 * extra
    full = coverage_mod.inventory(manifest, full=True)
    assert (
        len(_record(full, "models", "fct")["incremental"]["incremental_branch_tests"])
        == coverage_mod.TEST_CAP + extra
    )


def test_source_freshness_carries_last_run_status_and_not_run_when_unreached():
    warn_only = {
        "warn_after": {"count": 12, "period": "hour"},
        "error_after": {"count": None, "period": None},
    }
    err_policy = {
        "warn_after": {"count": 12, "period": "hour"},
        "error_after": {"count": 24, "period": "hour"},
    }
    manifest = _manifest(
        nodes={
            "test.s": _generic(
                "not_null", None, "id", reads=[f"source.{PROJECT}.raw.orders"]
            )
        },
        sources={
            f"source.{PROJECT}.raw.orders": _source(
                "orders", columns=["id"], freshness=err_policy
            ),
            f"source.{PROJECT}.raw.customers": _source(
                "customers", freshness=warn_only
            ),
            f"source.{PROJECT}.raw.payments": _source("payments", freshness=err_policy),
            f"source.{PROJECT}.raw.unreached": _source(
                "unreached", freshness=warn_only
            ),
            f"source.{PROJECT}.raw.unchecked": _source("unchecked", freshness=None),
        },
    )
    inv = coverage_mod.inventory(
        manifest,
        statuses={"test.s": "pass"},
        freshness_statuses={
            f"source.{PROJECT}.raw.orders": "pass",
            f"source.{PROJECT}.raw.customers": "warn",
            f"source.{PROJECT}.raw.payments": "error",
            # An old sources.json row for a source that no longer declares freshness.
            f"source.{PROJECT}.raw.unchecked": "error",
        },
    )

    orders = _record(inv, "sources", "raw.orders")
    assert {
        "kind": "freshness",
        "severity": "error",
        "last_run": "pass",
    } in orders["tests"]
    assert orders["last_run"] == {
        "pass": 2,
        "fail": 0,
        "warn": 0,
        "error": 0,
        "skipped": 0,
        "not_run": 0,
    }

    assert _record(inv, "sources", "raw.customers")["tests"] == [
        {"kind": "freshness", "severity": "warn", "last_run": "warn"}
    ]
    assert _record(inv, "sources", "raw.payments")["tests"] == [
        {"kind": "freshness", "severity": "error", "last_run": "error"}
    ]
    assert _record(inv, "sources", "raw.unreached")["tests"] == [
        {"kind": "freshness", "severity": "warn", "last_run": "not_run"}
    ]
    assert _record(inv, "sources", "raw.unchecked")["tests"] == []
    assert inv.summary["tests"]["by_last_run"] == {
        "pass": 2,
        "fail": 0,
        "warn": 1,
        "error": 1,
        "skipped": 0,
        "not_run": 1,
    }


def test_coverage_keeps_last_run_and_freshness_run_apart_and_opens_no_connection(
    tmp_path, monkeypatch
):
    from exmergo_dex_core import connect
    from exmergo_dex_core.config import DexConfig
    from exmergo_dex_core.engine import DexEngine
    from exmergo_dex_core.results import to_envelope

    for name in ("DBT_ENGINE_TARGET_PATH", "DBT_TARGET_PATH", "DBT_LOG_PATH"):
        monkeypatch.delenv(name, raising=False)

    project = tmp_path / "analytics"
    target = project / "target"
    logs = project / "logs"
    target.mkdir(parents=True)
    logs.mkdir(parents=True)
    (project / "dbt_project.yml").write_text("name: shop\n", encoding="utf-8")

    err_policy = {
        "warn_after": {"count": 12, "period": "hour"},
        "error_after": {"count": 24, "period": "hour"},
    }
    manifest = _manifest(
        nodes={
            f"{M}.orders": _model("orders", columns=["id"]),
            "test.shop.not_null_orders_id": _generic("not_null", f"{M}.orders", "id"),
        },
        sources={
            f"source.{PROJECT}.raw.orders": _source("orders", freshness=err_policy),
            f"source.{PROJECT}.raw.events": _source("events", freshness=err_policy),
            f"source.{PROJECT}.raw.disabled": {
                **_source("disabled", freshness=err_policy),
                "config": {"enabled": False, "freshness": err_policy},
            },
        },
    )
    manifest["metadata"]["generated_at"] = "2026-11-15T10:00:00Z"
    (target / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    test_inv = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    fresh_inv = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    (target / "run_results.json").write_text(
        json.dumps(
            {
                "metadata": {
                    "generated_at": "2026-11-15T10:10:00Z",
                    "invocation_id": test_inv,
                },
                "results": [
                    {
                        "unique_id": "test.shop.not_null_orders_id",
                        "status": "fail",
                        "message": "secret_test_failure",
                    }
                ],
                "args": {"which": "test"},
            }
        ),
        encoding="utf-8",
    )
    (target / "sources.json").write_text(
        json.dumps(
            {
                "metadata": {
                    "generated_at": "2026-11-15T10:20:00Z",
                    "invocation_id": fresh_inv,
                },
                "results": [
                    {
                        "unique_id": f"source.{PROJECT}.raw.orders",
                        "status": "runtime error",
                        "max_loaded_at": "2026-11-14T01:02:03Z",
                        "message": "Database Error: secret_freshness_message",
                    },
                    {
                        "unique_id": f"source.{PROJECT}.raw.gone",
                        "status": "pass",
                        "max_loaded_at": "2026-11-15T09:59:59Z",
                    },
                    {
                        "unique_id": f"source.{PROJECT}.raw.disabled",
                        "status": "pass",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    (logs / "dbt.log").write_text(
        "\n".join(
            json.dumps({"info": {"invocation_id": inv}})
            for inv in (test_inv, fresh_inv)
        )
        + "\n",
        encoding="utf-8",
    )

    import importlib

    build_mod = importlib.import_module("exmergo_dex_core.transform.build")
    monkeypatch.setattr(
        build_mod,
        "shadow_parse",
        lambda *a, **k: {
            "available": False,
            "reason": "dbt is not installed",
            "success": None,
            "messages": [],
        },
    )

    def _forbid_connection(*args, **kwargs):
        raise AssertionError("maintain coverage opened a connection")

    monkeypatch.setattr(connect, "open_adapter", _forbid_connection)

    with DexEngine(
        config=DexConfig(dbt_project_dir="analytics"), repo_root=tmp_path
    ) as engine:
        envelope = to_envelope(engine.coverage()).model_dump(mode="json")
        assert engine._adapter_instance is None
        data = envelope["data"]

        assert data["last_run"]["invocation_id"] == test_inv
        assert data["last_run"]["generated_at"] == "2026-11-15T10:10:00Z"
        assert data["last_run"]["command"] == "test"
        assert isinstance(data["last_run"]["age_seconds"], int)

        assert data["freshness_run"]["invocation_id"] == fresh_inv
        assert data["freshness_run"]["generated_at"] == "2026-11-15T10:20:00Z"
        assert data["freshness_run"]["artifact"] == "project"
        assert data["freshness_run"]["cold"] is False
        assert isinstance(data["freshness_run"]["age_seconds"], int)

        by_source = {s["name"]: s["tests"] for s in data["sources"]}
        assert by_source["raw.orders"] == [
            {"kind": "freshness", "severity": "error", "last_run": "error"}
        ]
        assert by_source["raw.events"] == [
            {"kind": "freshness", "severity": "error", "last_run": "not_run"}
        ]
        assert "raw.gone" not in by_source
        assert "raw.disabled" not in by_source

        rendered = json.dumps(envelope)
        for forbidden in (
            "max_loaded_at",
            "2026-11-14T01:02:03Z",
            "2026-11-15T09:59:59Z",
            "secret_freshness_message",
            "secret_test_failure",
            "current",
            "recent",
        ):
            assert forbidden not in rendered

        # Removing sources.json is a note, not an error, and leaves last_run intact.
        (target / "sources.json").unlink()
        without_sources = to_envelope(engine.coverage()).model_dump(mode="json")
        assert engine._adapter_instance is None
        assert without_sources["status"] == "ok"
        assert without_sources["data"]["last_run"]["invocation_id"] == test_inv
        assert without_sources["data"]["freshness_run"] is None
        assert any("sources.json" in note for note in without_sources["data"]["notes"])
        assert {
            s["name"]: s["tests"][0]["last_run"]
            for s in without_sources["data"]["sources"]
        } == {"raw.events": "not_run", "raw.orders": "not_run"}
