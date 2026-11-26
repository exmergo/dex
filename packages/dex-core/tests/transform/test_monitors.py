"""Vendor monitors written from a declaration (#1482).

With ``monitors.vendor: elementary``, a column declared with role ``time``
becomes the ``timestamp_column`` of ``elementary.volume_anomalies`` and
``elementary.freshness_anomalies``, written into the model's YAML entry in the
same plan as the declaration. A monitor is written only where the package is
installed and the repository shows it enabled on the production target; a
hand-written monitor is never touched, and ``data.decisions`` is read back from
the tests Dex wrote.

The package here is a stand-in: an ``elementary`` project under
``dbt_packages`` whose two test macros select nothing, so dbt parses the plan
the way it parses one against the real package without installing it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from exmergo_dex_core.cli import main
from exmergo_dex_core.config import DexConfig, MonitorsConfig, load_config, save_config
from exmergo_dex_core.transform import change_scope
from exmergo_dex_core.transform import monitors as mon
from exmergo_dex_core.transform.declarations import (
    decisions,
    parse_declarations,
    render,
)

VOLUME = "elementary.volume_anomalies"
FRESHNESS = "elementary.freshness_anomalies"

OPEN = mon.Gate(
    vendor="elementary",
    package=mon.INSTALLED,
    release="0.26.1",
    production=mon.ENABLED,
    production_reason=mon.NOT_SET,
    prod_target="prod",
)


def _render(
    payload: dict,
    *,
    content=None,
    gate=OPEN,
    rows=None,
    notes=None,
    outputs=None,
    dialect=None,
    domains=None,
):
    [declaration] = parse_declarations([payload])
    return render(
        content,
        declaration,
        notes=notes,
        outputs=outputs,
        dialect=dialect,
        monitors=gate,
        monitor_rows=rows,
        monitor_domains=domains,
    )


def _tests(content: str, version: int | None = None) -> list:
    [entry] = yaml.safe_load(content)["models"]
    if version is not None:
        entry = next(v for v in entry["versions"] if v["v"] == version)
    return entry.get("data_tests") or entry.get("tests") or []


def _named(tests: list) -> list[tuple[str, str | None]]:
    out = []
    for test in tests:
        name = test if isinstance(test, str) else next(iter(test))
        body = {} if isinstance(test, str) else next(iter(test.values())) or {}
        arguments = body.get("arguments") or body
        out.append((name, arguments.get("timestamp_column")))
    return out


def _time(*columns: str, model: str = "fct_orders") -> dict:
    return {"model": model, "columns": {c: {"role": "time"} for c in columns}}


# --- reading production ---------------------------------------------------------


def _project(enabled: str | None) -> str:
    text = "name: shop\n"
    if enabled is not None:
        text += f"models:\n  elementary:\n    +enabled: {enabled}\n"
    return text


@pytest.mark.parametrize(
    ("enabled", "prod", "expected"),
    [
        (None, None, (mon.ENABLED, mon.NOT_SET)),
        ("true", None, (mon.ENABLED, mon.LITERAL)),
        ("false", "prod", (mon.DISABLED, mon.LITERAL)),
        ('"false"', None, (mon.DISABLED, mon.LITERAL)),
        (
            "\"{{ target.name != 'dev' }}\"",
            "prod",
            (mon.ENABLED, mon.TARGET_EXPRESSION),
        ),
        (
            "\"{{ target.name != 'dev' }}\"",
            "dev",
            (mon.DISABLED, mon.TARGET_EXPRESSION),
        ),
        ("\"{{target.name=='prod'}}\"", "prod", (mon.ENABLED, mon.TARGET_EXPRESSION)),
        (
            "\"{{ target.name in ['prod', 'ci'] }}\"",
            "ci",
            (mon.ENABLED, mon.TARGET_EXPRESSION),
        ),
        (
            "\"{{ target.name not in ('dev', 'local') }}\"",
            "local",
            (mon.DISABLED, mon.TARGET_EXPRESSION),
        ),
        ("\"{{ target.name != 'dev' }}\"", None, (mon.UNKNOWN, mon.PROD_TARGET_UNSET)),
        (
            "\"{{ env_var('ELEMENTARY_ON', 'true') }}\"",
            "prod",
            (mon.UNKNOWN, mon.EXPRESSION_UNREAD),
        ),
        (
            "\"{{ target.name in var('envs') }}\"",
            "prod",
            (mon.UNKNOWN, mon.EXPRESSION_UNREAD),
        ),
    ],
)
def test_production_is_read_from_the_repository_with_the_prod_target(
    enabled, prod, expected
):
    status, reason, _ = mon.production_enabled(_project(enabled), prod)
    assert (status, reason) == expected


def test_a_root_models_setting_reaches_the_package_unless_it_overrides_it():
    # dbt applies a root `models: +enabled` to every package's models too.
    root_off = "models:\n  +enabled: false\n  shop:\n    +enabled: true\n"
    assert mon.production_enabled(root_off, "prod")[:2] == (mon.DISABLED, mon.LITERAL)
    overridden = root_off + "  elementary:\n    +enabled: true\n"
    assert mon.production_enabled(overridden, "prod")[:2] == (
        mon.ENABLED,
        mon.LITERAL,
    )


def test_a_setting_on_a_package_folder_does_not_decide_the_package():
    # Only the package root decides whether its models (and so its tests'
    # `is_elementary_enabled()`) run; a folder below it is not read.
    text = "models:\n  elementary:\n    edr:\n      +enabled: false\n"
    assert mon.production_enabled(text, "prod")[:2] == (mon.ENABLED, mon.NOT_SET)


# --- rendering ------------------------------------------------------------------


def test_one_time_column_writes_both_monitors_with_its_timestamp_column():
    rows: list = []

    out = _render(_time("ordered_at"), rows=rows)

    assert _named(_tests(out)) == [(VOLUME, "ordered_at"), (FRESHNESS, "ordered_at")]
    [volume, _] = _tests(out)
    config = volume[VOLUME]["config"]
    assert config["tags"] == [mon.TAG]
    assert config["meta"]["disable_test_samples"] is True
    assert config["meta"]["dex"]["monitor"] == {
        "vendor": "elementary",
        "from": "columns.ordered_at.role: time",
    }
    # Dex chooses no statistic: only the timestamp column is written.
    assert volume[VOLUME]["arguments"] == {"timestamp_column": "ordered_at"}
    assert [(r["test"], r["status"]) for r in rows] == [
        (VOLUME, mon.ADDED),
        (FRESHNESS, mon.ADDED),
    ]


def test_several_time_columns_watch_freshness_on_each_and_withhold_volume():
    rows: list = []

    out = _render(_time("ordered_at", "loaded_at"), rows=rows)

    assert _named(_tests(out)) == [
        (FRESHNESS, "ordered_at"),
        (FRESHNESS, "loaded_at"),
    ]
    assert [(r["test"], r["status"], r.get("reason")) for r in rows] == [
        (VOLUME, mon.NOT_RENDERED, mon.AMBIGUOUS_TIME_COLUMN),
        (FRESHNESS, mon.ADDED, None),
        (FRESHNESS, mon.ADDED, None),
    ]


def test_no_time_column_writes_no_monitor():
    rows: list = []

    out = _render(
        {"model": "fct_orders", "description": "One row per order."}, rows=rows
    )

    assert _tests(out) == []
    assert {(r["test"], r["reason"]) for r in rows} == {
        (VOLUME, mon.NO_TIME_COLUMN),
        (FRESHNESS, mon.NO_TIME_COLUMN),
    }


def test_a_time_column_the_model_does_not_output_writes_nothing():
    rows: list = []

    out = _render(_time("ordered_at"), rows=rows, outputs=frozenset({"order_id"}))

    assert _tests(out) == []
    assert {r["reason"] for r in rows} == {mon.COLUMN_NOT_OUTPUT}


def test_a_hand_written_volume_monitor_is_kept_exactly_as_written():
    content = (
        "version: 2\n"
        "models:\n"
        "  - name: fct_orders\n"
        "    data_tests:\n"
        "      - elementary.volume_anomalies:\n"
        "          sensitivity: 4  # tuned by hand\n"
    )
    rows: list = []

    out = _render(_time("ordered_at"), content=content, rows=rows)

    assert "sensitivity: 4  # tuned by hand" in out
    assert _named(_tests(out)) == [(VOLUME, None), (FRESHNESS, "ordered_at")]
    assert [(r["test"], r["status"]) for r in rows] == [
        (VOLUME, mon.HAND_WRITTEN),
        (FRESHNESS, mon.ADDED),
    ]


def test_a_bare_hand_written_monitor_counts_as_the_same_test():
    content = (
        "version: 2\n"
        "models:\n"
        "  - name: fct_orders\n"
        "    tests:\n"
        "      - freshness_anomalies:\n"
        "          timestamp_column: ordered_at\n"
    )
    rows: list = []

    out = _render(_time("ordered_at"), content=content, rows=rows)

    assert ("freshness_anomalies", "ordered_at") in _named(_tests(out))
    assert (FRESHNESS, "ordered_at") not in _named(_tests(out))
    assert {r["test"]: r["status"] for r in rows}[FRESHNESS] == mon.HAND_WRITTEN


def test_a_hand_written_freshness_monitor_on_another_column_is_another_monitor():
    content = (
        "version: 2\n"
        "models:\n"
        "  - name: fct_orders\n"
        "    data_tests:\n"
        "      - elementary.freshness_anomalies:\n"
        "          arguments:\n"
        "            timestamp_column: loaded_at\n"
    )
    rows: list = []

    out = _render(_time("ordered_at"), content=content, rows=rows)

    assert (FRESHNESS, "loaded_at") in _named(_tests(out))
    assert (FRESHNESS, "ordered_at") in _named(_tests(out))
    assert {r["test"]: r["status"] for r in rows}[FRESHNESS] == mon.ADDED


def test_declaring_again_lists_each_monitor_once():
    first = _render(_time("ordered_at"))
    rows: list = []

    again = _render(
        {"model": "fct_orders", "description": "x"}, content=first, rows=rows
    )

    assert _named(_tests(again)) == [(VOLUME, "ordered_at"), (FRESHNESS, "ordered_at")]
    assert {r["status"] for r in rows} == {mon.ALREADY_LISTED}


def test_a_monitor_on_a_moved_time_column_is_reported_stale_and_kept():
    first = _render(_time("ordered_at"))
    moved = _time("placed_at")
    moved["columns"]["ordered_at"] = {"role": "attribute"}
    rows: list = []
    notes: list = []

    out = _render(moved, content=first, rows=rows, notes=notes)

    named = _named(_tests(out))
    assert (VOLUME, "ordered_at") in named and (FRESHNESS, "ordered_at") in named
    assert (VOLUME, "placed_at") in named and (FRESHNESS, "placed_at") in named
    stale = [r for r in rows if r["status"] == mon.STALE]
    assert {(r["test"], r["column"], r["reason"]) for r in stale} == {
        (VOLUME, "ordered_at", mon.DECLARATION_CHANGED),
        (FRESHNESS, "ordered_at", mon.DECLARATION_CHANGED),
    }
    assert any("no longer gives" in note for note in notes)


def test_a_versioned_model_takes_its_monitors_on_the_version():
    content = (
        "version: 2\n"
        "models:\n"
        "  - name: fct_orders\n"
        "    latest_version: 1\n"
        "    versions:\n"
        "      - v: 1\n"
        "      - v: 2\n"
    )

    out = _render(_time("ordered_at", model="fct_orders.v2"), content=content)

    assert _tests(out, version=1) == []
    assert _named(_tests(out, version=2)) == [
        (VOLUME, "ordered_at"),
        (FRESHNESS, "ordered_at"),
    ]
    [entry] = yaml.safe_load(out)["models"]
    assert "data_tests" not in entry


@pytest.mark.parametrize(
    ("gate", "reason"),
    [
        (
            mon.Gate(
                "elementary", mon.NOT_INSTALLED, None, mon.ENABLED, mon.NOT_SET, "prod"
            ),
            mon.PACKAGE_NOT_INSTALLED,
        ),
        (
            mon.Gate(
                "elementary", mon.INSTALLED, "0.26.1", mon.DISABLED, mon.LITERAL, "prod"
            ),
            mon.PRODUCTION_DISABLED,
        ),
        (
            mon.Gate(
                "elementary",
                mon.INSTALLED,
                "0.26.1",
                mon.UNKNOWN,
                mon.EXPRESSION_UNREAD,
                "prod",
            ),
            mon.PRODUCTION_UNKNOWN,
        ),
    ],
)
def test_a_closed_gate_writes_nothing_and_says_why(gate, reason):
    rows: list = []

    out = _render(_time("ordered_at"), gate=gate, rows=rows)

    assert _tests(out) == []
    assert {(r["status"], r["reason"]) for r in rows} == {(mon.NOT_RENDERED, reason)}
    assert gate.warning() is not None


def test_no_gate_writes_no_monitor_and_reports_nothing():
    rows: list = []

    out = _render(_time("ordered_at"), gate=None, rows=rows)

    assert _tests(out) == []
    assert rows == []


# --- decisions ------------------------------------------------------------------


def test_decisions_are_read_back_from_the_monitors_in_the_yaml():
    out = _render(_time("ordered_at"))

    found = decisions({"models/schema.yml": out}, ["fct_orders"])

    assert [(d["monitor"], d["field"], d["chosen"]) for d in found] == [
        (VOLUME, "columns.ordered_at.role: time", "timestamp_column: ordered_at"),
        (FRESHNESS, "columns.ordered_at.role: time", "timestamp_column: ordered_at"),
    ]
    # A monitor is not an assumption about which rows the model holds.
    assert all("column" not in d for d in found)


def test_a_hand_written_monitor_is_no_decision():
    content = (
        "version: 2\n"
        "models:\n"
        "  - name: fct_orders\n"
        "    data_tests:\n"
        "      - elementary.volume_anomalies:\n"
        "          timestamp_column: ordered_at\n"
    )
    assert decisions({"models/schema.yml": content}, ["fct_orders"]) == []


def test_a_monitor_decision_settles_no_population_change():
    decision = {
        "model": "fct_orders",
        "decision": "Monitor elementary.volume_anomalies",
        "chosen": "timestamp_column: ordered_at",
        "evidence": "contract",
        "monitor": VOLUME,
        "field": "columns.ordered_at.role: time",
    }
    report = change_scope.analyze(
        [
            (
                "models/marts/fct_orders.sql",
                "select order_id, ordered_at from orders\n",
                "select order_id, ordered_at from orders"
                " where ordered_at > '2024-01-01'\n",
            )
        ],
        [decision],
        "duckdb",
    )

    assert report is not None
    [scope] = report["models"]
    assert scope["population"] == "changed"
    assert all(c["status"] == "undeclared" for c in scope["changes"] if "status" in c)


# --- config ---------------------------------------------------------------------


def test_the_monitors_block_round_trips_and_defaults_to_unset(tmp_path: Path):
    assert DexConfig().monitors.vendor is None

    save_config(DexConfig(monitors=MonitorsConfig(vendor="elementary")), tmp_path)

    assert load_config(tmp_path).monitors.vendor == "elementary"


def test_an_unknown_vendor_is_refused_by_name():
    with pytest.raises(ValueError, match=r"monitors\.vendor must be one of elementary"):
        MonitorsConfig(vendor="monte_carlo")


# --- the plan -------------------------------------------------------------------


def _install(project: Path, enabled: str | None = "\"{{ target.name != 'dev' }}\""):
    package = project / "dbt_packages" / "elementary"
    (package / "macros").mkdir(parents=True)
    (package / "dbt_project.yml").write_text(
        'name: elementary\nversion: "0.26.1"\nconfig-version: 2\n', encoding="utf-8"
    )
    (package / "macros" / "tests.sql").write_text(
        "{% test volume_anomalies(model, timestamp_column, where_expression=none) %}"
        "select 1 where false{% endtest %}\n"
        "{% test freshness_anomalies(model, timestamp_column, where_expression=none) %}"
        "select 1 where false{% endtest %}\n"
        "{% test dimension_anomalies(model, dimensions, timestamp_column=none,"
        " where_expression=none) %}select 1 where false{% endtest %}\n",
        encoding="utf-8",
    )
    (project / "packages.yml").write_text(
        "packages:\n  - package: elementary-data/elementary\n    version: 0.26.1\n",
        encoding="utf-8",
    )
    if enabled is not None:
        project_yml = project / "dbt_project.yml"
        project_yml.write_text(
            project_yml.read_text(encoding="utf-8")
            + f"models:\n  elementary:\n    +enabled: {enabled}\n",
            encoding="utf-8",
        )


def _repo(
    tmp_path: Path, *, vendor: str | None = "elementary", prod: str | None = "prod"
):
    (tmp_path / ".dex").mkdir(exist_ok=True)
    text = (
        f"connector: duckdb\nduckdb:\n  path: {tmp_path / 'dev.duckdb'}\n"
        "dbt_project_dir: analytics\n"
    )
    if prod is not None:
        text += f"dbt_prod_target: {prod}\n"
    if vendor is not None:
        text += f"monitors:\n  vendor: {vendor}\n"
    (tmp_path / ".dex" / "config.yml").write_text(text, encoding="utf-8")


ORDERS = "select 1 as order_id, current_timestamp as ordered_at, 'placed' as status\n"
DECLARED = {
    "model": "fct_orders",
    "description": "One row per order.",
    "grain": ["order_id"],
    "columns": {"order_id": {"role": "key"}, "ordered_at": {"role": "time"}},
}


def _dex(argv: list[str], capsys) -> dict:
    main(argv)
    return json.loads(capsys.readouterr().out)


def _plan(tmp_path: Path, capsys, declaration: dict = DECLARED) -> dict:
    payload = tmp_path / "edits.json"
    payload.write_text(
        json.dumps(
            {
                "edits": [
                    {
                        "path": "models/marts/fct_orders.sql",
                        "kind": "model_sql",
                        "content": ORDERS,
                    }
                ],
                "declarations": [declaration],
            }
        ),
        encoding="utf-8",
    )
    return _dex(
        [
            "--repo-root",
            str(tmp_path),
            "transform",
            "plan",
            "orders",
            "--edits-file",
            str(payload),
        ],
        capsys,
    )


def _schema(planned: dict) -> str:
    [diff] = [d for d in planned["diffs"] if "fct_orders" in json.dumps(d)]
    return json.dumps(diff)


def test_the_plan_writes_both_monitors_and_reports_where_they_came_from(
    dbt_project_dir: Path, tmp_path: Path, capsys
):
    _install(dbt_project_dir)
    _repo(tmp_path)

    planned = _plan(tmp_path, capsys)

    assert planned["status"] == "ok", planned["errors"]
    report = planned["data"]["monitors"]
    assert report["vendor"] == "elementary"
    assert report["package"] == {"status": mon.INSTALLED, "release": "0.26.1"}
    assert report["production"]["status"] == mon.ENABLED
    assert report["production"]["reason"] == mon.TARGET_EXPRESSION
    assert report["counts"] == {mon.ADDED: 2}
    assert {d.get("field") for d in planned["data"]["decisions"] if "monitor" in d} == {
        "columns.ordered_at.role: time"
    }
    # The monitors are no generated tests: nothing prices or counts them.
    assert "generated_tests" not in planned["data"]

    applied = _dex(["--repo-root", str(tmp_path), "transform", "apply"], capsys)
    assert applied["status"] == "ok", applied["errors"]
    assert [d["monitor"] for d in applied["data"]["decisions"] if "monitor" in d] == [
        VOLUME,
        FRESHNESS,
    ]
    written = (dbt_project_dir / "models" / "marts" / "fct_orders.yml").read_text(
        encoding="utf-8"
    )
    assert _named(_tests(written)) == [
        (VOLUME, "ordered_at"),
        (FRESHNESS, "ordered_at"),
    ]


def test_with_the_setting_unset_the_plan_writes_no_monitor(
    dbt_project_dir: Path, tmp_path: Path, capsys
):
    _install(dbt_project_dir)
    _repo(tmp_path, vendor=None)

    planned = _plan(tmp_path, capsys)

    assert planned["status"] == "ok", planned["errors"]
    assert "monitors" not in planned["data"]
    assert "anomalies" not in json.dumps(planned["diffs"])


def test_without_the_package_the_plan_writes_no_monitor_and_says_so(
    dbt_project_dir: Path, tmp_path: Path, capsys
):
    _repo(tmp_path)

    planned = _plan(tmp_path, capsys)

    assert planned["status"] == "ok", planned["errors"]
    assert planned["data"]["monitors"]["package"]["status"] == mon.NOT_INSTALLED
    assert planned["data"]["monitors"]["counts"] == {mon.NOT_RENDERED: 2}
    assert "anomalies" not in json.dumps(planned["diffs"])
    assert any("not installed" in w for w in planned["warnings"])


@pytest.mark.parametrize(
    ("enabled", "prod", "status", "reason", "says"),
    [
        ("false", "prod", mon.DISABLED, mon.LITERAL, "never run"),
        (
            "\"{{ target.name == 'dev' }}\"",
            "prod",
            mon.DISABLED,
            mon.TARGET_EXPRESSION,
            "'prod'",
        ),
        (
            "\"{{ target.name != 'dev' }}\"",
            None,
            mon.UNKNOWN,
            mon.PROD_TARGET_UNSET,
            "dbt_prod_target",
        ),
        (
            "\"{{ var('on', true) }}\"",
            "prod",
            mon.UNKNOWN,
            mon.EXPRESSION_UNREAD,
            "+enabled",
        ),
    ],
)
def test_where_production_does_not_run_the_package_no_monitor_is_written(
    dbt_project_dir: Path, tmp_path: Path, capsys, enabled, prod, status, reason, says
):
    _install(dbt_project_dir, enabled=enabled)
    _repo(tmp_path, prod=prod)

    planned = _plan(tmp_path, capsys)

    assert planned["status"] == "ok", planned["errors"]
    production = planned["data"]["monitors"]["production"]
    assert (production["status"], production["reason"]) == (status, reason)
    assert "anomalies" not in json.dumps(planned["diffs"])
    assert any("no monitor was written" in w and says in w for w in planned["warnings"])


def test_transform_declare_writes_the_monitors_an_earlier_declaration_owes(
    dbt_project_dir: Path, tmp_path: Path, capsys
):
    _install(dbt_project_dir)
    sql = dbt_project_dir / "models" / "staging" / "stg_customers.sql"
    sql.write_text(
        "select 1 as id, current_timestamp as signed_up_at\n", encoding="utf-8"
    )
    (dbt_project_dir / "models" / "staging" / "schema.yml").write_text(
        "version: 2\n"
        "models:\n"
        "  - name: stg_customers\n"
        "    description: One row per customer.\n"
        "    config:\n"
        "      meta:\n"
        "        dex:\n"
        "          grain: [id]\n"
        "    columns:\n"
        "      - name: id\n"
        "        data_tests: [unique, not_null]\n"
        "        config:\n"
        "          meta:\n"
        "            dex:\n"
        "              role: key\n"
        "      - name: signed_up_at\n"
        "        config:\n"
        "          meta:\n"
        "            dex:\n"
        "              role: time\n",
        encoding="utf-8",
    )

    def declare() -> dict:
        return _dex(
            ["--repo-root", str(tmp_path), "transform", "declare", "stg_customers"],
            capsys,
        )

    _repo(tmp_path, vendor=None)
    assert declare()["data"]["plan_id"] is None

    _repo(tmp_path)
    proposed = declare()

    assert proposed["status"] == "ok", proposed["errors"]
    [proposal] = proposed["data"]["proposed"]
    assert [m["test"] for m in proposal["fields"]["monitors"]["value"]] == [
        VOLUME,
        FRESHNESS,
    ]
    applied = _dex(["--repo-root", str(tmp_path), "transform", "apply"], capsys)
    assert applied["status"] == "ok", applied["errors"]
    assert declare()["data"]["plan_id"] is None


def test_transform_declare_says_why_an_owed_monitor_was_not_written(
    dbt_project_dir: Path, tmp_path: Path, capsys
):
    _install(dbt_project_dir, enabled="false")
    (dbt_project_dir / "models" / "staging" / "stg_customers.sql").write_text(
        "select 1 as id, current_timestamp as signed_up_at\n", encoding="utf-8"
    )
    (dbt_project_dir / "models" / "staging" / "schema.yml").write_text(
        "version: 2\n"
        "models:\n"
        "  - name: stg_customers\n"
        "    description: One row per customer.\n"
        "    config:\n"
        "      meta:\n"
        "        dex:\n"
        "          grain: [id]\n"
        "    columns:\n"
        "      - name: id\n"
        "        data_tests: [unique, not_null]\n"
        "        config:\n"
        "          meta:\n"
        "            dex:\n"
        "              role: key\n"
        "      - name: signed_up_at\n"
        "        config:\n"
        "          meta:\n"
        "            dex:\n"
        "              role: time\n",
        encoding="utf-8",
    )
    _repo(tmp_path)

    declared = _dex(
        ["--repo-root", str(tmp_path), "transform", "declare", "stg_customers"],
        capsys,
    )

    assert declared["status"] == "ok", declared["errors"]
    assert declared["data"]["plan_id"] is None
    said = json.dumps(declared)
    assert "disables the Elementary package" in said
    assert "no monitor was written" in said


# --- the population filter as a where_expression (#1483) --------------------------


def _filtered(*filters: dict, columns: dict | None = None) -> dict:
    return {
        "model": "fct_orders",
        "population": {"filters": list(filters)},
        "columns": {"ordered_at": {"role": "time"}, **(columns or {})},
    }


def _where(payload: dict, dialect: str | None = "duckdb", **kwargs) -> str | None:
    [declaration] = parse_declarations([payload])
    return mon.where_expression(declaration, dialect, **kwargs)


def _arguments(content: str) -> list[dict]:
    return [next(iter(t.values()))["arguments"] for t in _tests(content)]


def test_includes_is_written_as_in():
    assert (
        _where(_filtered({"column": "status", "includes": ["placed", "shipped"]}))
        == "status in ('placed', 'shipped')"
    )


def test_excludes_on_a_nullable_column_keeps_its_null_rows():
    assert (
        _where(_filtered({"column": "status", "excludes": ["cancelled"]}))
        == "(status not in ('cancelled') or status is null)"
    )


def test_excludes_on_a_column_that_is_never_null_takes_no_null_arm():
    assert (
        _where(
            _filtered(
                {"column": "status", "excludes": ["cancelled"]},
                columns={"status": {"null_rule": "never"}},
            )
        )
        == "status not in ('cancelled')"
    )


def test_several_filters_join_with_and():
    assert _where(
        _filtered(
            {"column": "status", "includes": ["placed"]},
            {"column": "region", "excludes": ["test"]},
        )
    ) == ("status in ('placed') and (region not in ('test') or region is null)")


@pytest.mark.parametrize(
    ("dialect", "literal"),
    [
        ("duckdb", "'o''brien'"),
        ("postgres", "'o''brien'"),
        ("trino", "'o''brien'"),
        ("bigquery", "'o\\'brien'"),
        ("snowflake", "'o\\'brien'"),
        ("databricks", "'o\\'brien'"),
        ("redshift", "'o\\'brien'"),
        ("clickhouse", "'o''brien'"),
    ],
)
def test_an_embedded_quote_is_escaped_as_the_connector_reads_it(dialect, literal):
    assert (
        _where(_filtered({"column": "status", "includes": ["o'brien"]}), dialect)
        == f"status in ({literal})"
    )


def test_a_backslash_is_doubled_where_it_starts_an_escape():
    payload = _filtered({"column": "status", "includes": ["a\\b"]})
    assert _where(payload, "duckdb") == "status in ('a\\b')"
    assert _where(payload, "snowflake") == "status in ('a\\\\b')"


def test_numbers_and_booleans_are_written_as_literals():
    assert (
        _where(_filtered({"column": "tier", "includes": [1, 2.5]}))
        == "tier in (1, 2.5)"
    )
    assert (
        _where(_filtered({"column": "is_test", "excludes": [True]}))
        == "(is_test not in (TRUE) or is_test is null)"
    )


def test_a_list_mixing_text_with_numbers_or_booleans_is_unwritable():
    # It states no one type for the column, and a where_expression cannot read
    # the column's type the way maintain verify does at run time.
    for values in (["a", 7], ["1", 2], ["a", False]):
        with pytest.raises(mon.FilterUnwritableError) as raised:
            _where(_filtered({"column": "code", "includes": values}))
        assert raised.value.reason == mon.FILTER_UNWRITABLE


def test_values_are_never_cast_to_another_type():
    # Typed as declared: a numeric list is numbers, a text list is text.
    assert _where(_filtered({"column": "code", "includes": ["1", "2"]})) == (
        "code in ('1', '2')"
    )
    assert _where(_filtered({"column": "code", "includes": [1, 2]})) == (
        "code in (1, 2)"
    )


@pytest.mark.parametrize(
    ("column", "dialect", "written"),
    [
        ("status", "snowflake", "status"),
        ("Status", "bigquery", "Status"),
        ("order status", "snowflake", '"order status"'),
        ("order status", "postgres", '"order status"'),
        ("order status", "bigquery", "`order status`"),
        ("order status", "databricks", "`order status`"),
        ("order status", "clickhouse", "`order status`"),
        ("order", "duckdb", '"order"'),
        ("user", "postgres", '"user"'),
        # Snowflake reads only its own reserved words: `user` is not one, and
        # quoting it would name a lower-case column dbt never created.
        ("user", "snowflake", "user"),
        ("limit", "snowflake", "limit"),
    ],
)
def test_the_column_is_quoted_the_way_the_adapter_quotes_it(column, dialect, written):
    assert (
        _where(_filtered({"column": column, "includes": ["x"]}), dialect)
        == f"{written} in ('x')"
    )


def test_a_column_the_entry_marks_quote_true_is_quoted():
    assert (
        _where(
            _filtered({"column": "status", "includes": ["x"]}),
            "snowflake",
            quoted=frozenset({"status"}),
        )
        == "\"status\" in ('x')"
    )


def test_a_filter_that_needs_a_connector_rule_with_none_configured_is_unwritable():
    for payload in (
        _filtered({"column": "order status", "includes": ["x"]}),
        _filtered({"column": "status", "includes": ["o'brien"]}),
    ):
        with pytest.raises(mon.FilterUnwritableError) as raised:
            _where(payload, None)
        assert raised.value.reason == mon.FILTER_UNWRITABLE
    # Nothing to quote or escape: the connector decides nothing.
    assert _where(_filtered({"column": "status", "includes": ["x"]}), None) == (
        "status in ('x')"
    )


def test_a_snowflake_reserved_word_is_written_only_with_quote_true():
    payload = _filtered({"column": "order", "includes": ["x"]})
    with pytest.raises(mon.FilterUnwritableError) as raised:
        _where(payload, "snowflake")
    assert "quote: true" in str(raised.value)
    assert _where(payload, "snowflake", quoted=frozenset({"order"})) == (
        "\"order\" in ('x')"
    )


def test_a_value_holding_a_jinja_delimiter_is_unwritable():
    with pytest.raises(mon.FilterUnwritableError):
        _where(_filtered({"column": "status", "includes": ["{{ var('x') }}"]}))


def test_a_column_name_holding_a_jinja_delimiter_is_unwritable():
    with pytest.raises(mon.FilterUnwritableError):
        _where(_filtered({"column": "a{{b}}", "includes": ["x"]}))


def test_no_filters_give_no_where_expression():
    assert _where(_time("ordered_at")) is None
    assert _where(_filtered({"column": "status"})) is None


def test_a_model_declaring_includes_writes_it_on_every_monitor():
    out = _render(
        _filtered({"column": "status", "includes": ["placed"]}), notes=[], rows=[]
    )

    assert _arguments(out) == [
        {"timestamp_column": "ordered_at", "where_expression": "status in ('placed')"},
        {"timestamp_column": "ordered_at", "where_expression": "status in ('placed')"},
    ]
    marker = next(iter(_tests(out)[0].values()))["config"]["meta"]["dex"]["monitor"]
    assert marker["filter"] == "population.filters"


def test_a_model_with_no_filters_writes_monitors_with_no_where_expression():
    out = _render(_time("ordered_at"))

    assert _arguments(out) == [
        {"timestamp_column": "ordered_at"},
        {"timestamp_column": "ordered_at"},
    ]


def test_a_filter_on_a_column_the_model_does_not_output_writes_no_monitor():
    rows: list = []
    out = _render(
        _filtered({"column": "status", "includes": ["placed"]}),
        rows=rows,
        outputs=frozenset({"ordered_at"}),
    )

    assert "anomalies" not in out
    assert {(r["status"], r["reason"]) for r in rows} == {
        (mon.NOT_RENDERED, mon.FILTER_COLUMN_NOT_OUTPUT)
    }


def test_a_monitor_written_before_the_filter_was_declared_is_stale_and_kept():
    before = _render(_time("ordered_at"))
    rows: list = []
    notes: list = []

    after = _render(
        _filtered({"column": "status", "includes": ["placed"]}),
        content=before,
        rows=rows,
        notes=notes,
    )

    statuses = sorted((r["test"], r["status"]) for r in rows)
    assert statuses == [
        (FRESHNESS, mon.ADDED),
        (FRESHNESS, mon.STALE),
        (VOLUME, mon.ADDED),
        (VOLUME, mon.STALE),
    ]
    # Never removed: the unfiltered pair is still listed beside the new one.
    assert len(_tests(after)) == 4
    stale = [r for r in rows if r["status"] == mon.STALE]
    assert {r["changed"] for r in stale} == {"where_expression"}
    # The note names the filter, not a time column the declaration still gives.
    assert len(notes) == 2
    assert all("where_expression" in n and "time column" not in n for n in notes)


def test_declaring_the_filter_again_lists_each_monitor_once():
    payload = _filtered({"column": "status", "includes": ["placed"]})
    once = _render(payload)
    rows: list = []

    twice = _render(payload, content=once, rows=rows)

    assert twice == once
    assert {r["status"] for r in rows} == {mon.ALREADY_LISTED}


def test_a_hand_written_monitor_keeps_its_own_where_expression():
    content = (
        "version: 2\n"
        "models:\n"
        "  - name: fct_orders\n"
        "    data_tests:\n"
        "      - elementary.volume_anomalies:\n"
        "          arguments:\n"
        "            timestamp_column: ordered_at\n"
        "            where_expression: \"region = 'emea'\"\n"
    )
    rows: list = []

    out = _render(
        _filtered({"column": "status", "includes": ["placed"]}),
        content=content,
        rows=rows,
    )

    assert out.startswith(content.split("      - elementary")[0])
    assert "where_expression: \"region = 'emea'\"" in out
    volume = next(r for r in rows if r["test"] == VOLUME)
    assert volume["status"] == mon.HAND_WRITTEN


def test_a_decision_names_the_where_expression_and_the_filter_field():
    out = _render(_filtered({"column": "status", "includes": ["placed"]}))

    [volume, _] = decisions({"models/schema.yml": out}, ["fct_orders"])

    assert volume["chosen"] == (
        "timestamp_column: ordered_at; where_expression: status in ('placed')"
    )
    assert "population.filters" in volume["detail"]
    assert volume["field"] == "columns.ordered_at.role: time"
    assert "column" not in volume


# The two places Elementary's table monitors splice a where_expression, read in
# macros/edr/data_monitoring/monitors_query/table_monitoring_query.sql at the
# package's e3ab23d8: on its own, and after the time-bucket filter with a bare
# `and`, which is what the anomaly tests run with a timestamp_column.
_SPLICES = (
    "select count(*) from orders where {where}",
    "select count(*) from orders where ordered_at >= date '2024-01-01' and {where}",
)


def _orders():
    duckdb = pytest.importorskip("duckdb")
    connection = duckdb.connect()
    connection.execute(
        "create table orders as select * from (values"
        " (1, 'placed', 'emea', date '2024-02-01'),"
        " (2, 'shipped', 'emea', date '2024-02-01'),"
        " (3, 'cancelled', 'test', date '2024-02-01'),"
        " (4, 'placed', null, date '2024-02-01'),"
        " (5, null, null, date '2024-02-01'),"
        " (6, 'placed', null, date '2023-01-01')"
        ") as t(order_id, status, region, ordered_at)"
    )
    return connection


def _monitored(connection, where: str) -> list[int]:
    counts = [connection.execute(s.format(where=where)).fetchone()[0] for s in _SPLICES]
    return counts


def test_the_written_includes_selects_exactly_the_declared_rows():
    connection = _orders()
    out = _render(_filtered({"column": "status", "includes": ["placed", "shipped"]}))
    [where] = {a["where_expression"] for a in _arguments(out)}

    # Rows 1, 2, 4 and 6 are placed or shipped; 3 is cancelled and 5 is NULL,
    # both outside the population. Row 6 is before the time bucket.
    assert _monitored(connection, where) == [4, 3]


def test_the_written_excludes_keeps_null_rows_inside_the_time_bucket():
    connection = _orders()
    out = _render(_filtered({"column": "region", "excludes": ["test"]}))
    [where] = {a["where_expression"] for a in _arguments(out)}

    # Every row but 3 is in the population, NULL regions (4, 5, 6) included;
    # behind the time bucket, row 6 stays out.
    assert _monitored(connection, where) == [5, 4]


def test_the_plan_writes_the_filter_as_the_monitors_where_expression(
    dbt_project_dir: Path, tmp_path: Path, capsys
):
    _install(dbt_project_dir)
    _repo(tmp_path)
    declared = {
        **DECLARED,
        "population": {
            "rule": "Placed orders.",
            "filters": [{"column": "status", "includes": ["placed"]}],
        },
    }

    planned = _plan(tmp_path, capsys, declared)

    assert planned["status"] == "ok", planned["errors"]
    rows = planned["data"]["monitors"]["monitors"]
    assert {r["arguments"].get("where_expression") for r in rows} == {
        "status in ('placed')"
    }
    chosen = [d["chosen"] for d in planned["data"]["decisions"] if "monitor" in d]
    assert (
        chosen
        == ["timestamp_column: ordered_at; where_expression: status in ('placed')"] * 2
    )


def test_transform_declare_writes_owed_monitors_with_the_declared_filter(
    dbt_project_dir: Path, tmp_path: Path, capsys
):
    _install(dbt_project_dir)
    (dbt_project_dir / "models" / "staging" / "stg_customers.sql").write_text(
        "select 1 as id, current_timestamp as signed_up_at, 'paid' as plan\n",
        encoding="utf-8",
    )
    (dbt_project_dir / "models" / "staging" / "schema.yml").write_text(
        "version: 2\n"
        "models:\n"
        "  - name: stg_customers\n"
        "    description: One row per customer.\n"
        "    config:\n"
        "      meta:\n"
        "        dex:\n"
        "          grain: [id]\n"
        "          population:\n"
        "            rule: Customers not on the internal plan.\n"
        "            filters:\n"
        "              - column: plan\n"
        "                excludes: [internal]\n"
        "    columns:\n"
        "      - name: id\n"
        "        data_tests: [unique, not_null]\n"
        "        config:\n"
        "          meta:\n"
        "            dex:\n"
        "              role: key\n"
        "      - name: signed_up_at\n"
        "        config:\n"
        "          meta:\n"
        "            dex:\n"
        "              role: time\n",
        encoding="utf-8",
    )
    _repo(tmp_path)

    proposed = _dex(
        ["--repo-root", str(tmp_path), "transform", "declare", "stg_customers"],
        capsys,
    )

    assert proposed["status"] == "ok", proposed["errors"]
    [proposal] = proposed["data"]["proposed"]
    wheres = {
        m["arguments"].get("where_expression")
        for m in proposal["fields"]["monitors"]["value"]
    }
    assert wheres == {"(plan not in ('internal') or plan is null)"}
    applied = _dex(["--repo-root", str(tmp_path), "transform", "apply"], capsys)
    assert applied["status"] == "ok", applied["errors"]
    written = (dbt_project_dir / "models" / "staging" / "schema.yml").read_text(
        encoding="utf-8"
    )
    assert written.count("where_expression:") == 2


# --- the final pass (#1483 review) ----------------------------------------------


CLOSED = mon.Gate(
    vendor="elementary",
    package=mon.INSTALLED,
    release="0.26.1",
    production=mon.DISABLED,
    production_reason=mon.LITERAL,
    prod_target="prod",
)


def test_a_stale_monitor_is_kept_while_its_replacement_cannot_be_written():
    before = _render(_time("ordered_at"))
    rows: list = []
    notes: list = []

    after = _render(
        _filtered({"column": "status", "includes": ["placed"]}),
        content=before,
        gate=CLOSED,
        rows=rows,
        notes=notes,
    )

    # Nothing new is written beside the old pair.
    assert len(_tests(after)) == 2
    stale = [r for r in rows if r["status"] == mon.STALE]
    assert {r["replacement"]["status"] for r in stale} == {mon.NOT_RENDERED}
    assert {r["replacement"]["reason"] for r in stale} == {mon.PRODUCTION_DISABLED}
    assert len(notes) == 2
    for note in notes:
        assert "keep this one" in note
        assert mon.PRODUCTION_DISABLED in note
        assert "remove it, since" not in note


def test_a_stale_monitor_whose_replacement_is_written_asks_for_removal():
    before = _render(_time("ordered_at"))
    rows: list = []
    notes: list = []

    _render(
        _filtered({"column": "status", "includes": ["placed"]}),
        content=before,
        rows=rows,
        notes=notes,
    )

    stale = [r for r in rows if r["status"] == mon.STALE]
    assert {r["replacement"]["status"] for r in stale} == {mon.ADDED}
    assert all("remove it, since" in n for n in notes)


@pytest.mark.parametrize(
    ("column", "written"),
    [
        ("range", "`range`"),
        ("partition", "`partition`"),
        ("interval", "`interval`"),
        ("rows", "`rows`"),
        ("status", "status"),
    ],
)
def test_a_bigquery_reserved_word_is_backtick_quoted(column, written):
    # No `quote: true`: BigQuery requires the keyword quoted.
    assert (
        _where(_filtered({"column": column, "includes": ["x"]}), "bigquery")
        == f"{written} in ('x')"
    )


@pytest.mark.parametrize("dialect", ["bigquery", "duckdb", "snowflake", None])
@pytest.mark.parametrize("value", ["a\nb", "a\rb"])
def test_a_value_holding_a_line_break_is_unwritable(dialect, value):
    with pytest.raises(mon.FilterUnwritableError) as raised:
        _where(_filtered({"column": "status", "includes": [value]}), dialect)
    assert raised.value.reason == mon.FILTER_UNWRITABLE
    # A single-line value is written unchanged.
    assert _where(_filtered({"column": "status", "includes": ["a b"]}), dialect) == (
        "status in ('a b')"
    )


def test_a_non_finite_number_is_unwritable():
    with pytest.raises(mon.FilterUnwritableError):
        _where(_filtered({"column": "rate", "includes": [float("inf")]}))


def test_a_quoted_column_holding_its_quote_character_is_unwritable():
    with pytest.raises(mon.FilterUnwritableError):
        _where(_filtered({"column": 'a"b', "includes": ["x"]}), "duckdb")


def test_a_column_the_yaml_entry_marks_quote_true_is_quoted_through_decide():
    content = (
        "version: 2\n"
        "models:\n"
        "  - name: fct_orders\n"
        "    columns:\n"
        "      - name: status\n"
        "        quote: true\n"
    )

    out = _render(
        _filtered({"column": "status", "includes": ["placed"]}),
        content=content,
        dialect="duckdb",
    )

    assert {a["where_expression"] for a in _arguments(out)} == {
        "\"status\" in ('placed')"
    }


def test_the_plan_escapes_the_filter_for_the_configured_connector(
    dbt_project_dir: Path, tmp_path: Path, capsys
):
    # A quote in a value needs the connector's escaping, so the configured
    # dialect has to reach the monitors the plan writes.
    _install(dbt_project_dir)
    _repo(tmp_path)
    declared = {
        **DECLARED,
        "population": {"filters": [{"column": "status", "includes": ["o'k"]}]},
    }

    planned = _plan(tmp_path, capsys, declared)

    assert planned["status"] == "ok", planned["errors"]
    rows = planned["data"]["monitors"]["monitors"]
    assert {r["status"] for r in rows} == {mon.ADDED}
    applied = _dex(["--repo-root", str(tmp_path), "transform", "apply"], capsys)
    assert applied["status"] == "ok", applied["errors"]
    written = (dbt_project_dir / "models" / "marts" / "fct_orders.yml").read_text(
        encoding="utf-8"
    )
    wheres = {a["where_expression"] for a in _arguments(written)}
    assert wheres == {"status in ('o''k')"}


def test_a_stale_monitor_is_kept_when_its_new_filter_is_unwritable():
    # Written with filter A on ordered_at; the declaration now gives a filter
    # dex cannot write on the same time column. The time column did not move,
    # so the old monitor is the model's only one and must be kept.
    before = _render(_filtered({"column": "status", "includes": ["placed"]}))
    rows: list = []
    notes: list = []

    after = _render(
        _filtered({"column": "status", "includes": ["a\nb"]}),
        content=before,
        rows=rows,
        notes=notes,
    )

    # The old monitors stay as written, and nothing is added beside them.
    assert _tests(after) == _tests(before)
    stale = [r for r in rows if r["status"] == mon.STALE]
    assert len(stale) == 2
    assert {r["changed"] for r in stale} == {"where_expression"}
    assert {
        (r["replacement"]["status"], r["replacement"]["reason"]) for r in stale
    } == {(mon.NOT_RENDERED, mon.FILTER_UNWRITABLE)}
    assert len(notes) == 2
    for note in notes:
        assert "keep this one until it can be written" in note
        assert mon.FILTER_UNWRITABLE in note
        assert "remove it, since" not in note
        assert "declare the time column again" not in note


# --- small-domain dimensions as dimension_anomalies (#1484) ------------------------

DIMENSION = "elementary.dimension_anomalies"


def _profile(name: str, distinct: int | None, *, exact: bool = True, pii=None):
    from exmergo_dex_core.cache import ColumnProfile

    return ColumnProfile(
        name=name,
        data_type="varchar",
        distinct_count=distinct,
        distinct_count_exact=exact,
        pii=pii,
    )


def _cache(*columns, identifier: str = "dev.main.fct_orders"):
    from exmergo_dex_core.cache import Dataset, DexCache

    return DexCache(
        datasets=[
            Dataset(
                identifier=identifier,
                columns=[_profile("ordered_at", 400), *columns],
                profiled_at="2026-11-26T00:00:00Z",
            )
        ]
    )


def _domains(*columns, cache=None, **kwargs) -> mon.Domains:
    return mon.Domains(
        cache=cache if cache is not None else _cache(*columns),
        dev_namespace="dev.main",
        **kwargs,
    )


def _dims(*dimensions: str, times: tuple[str, ...] = ("ordered_at",)) -> dict:
    return {
        "model": "fct_orders",
        "columns": {
            **{t: {"role": "time"} for t in times},
            **{d: {"role": "dimension"} for d in dimensions},
        },
    }


def _dimension_tests(content: str) -> list[dict]:
    return [
        next(iter(t.values()))
        for t in _tests(content)
        if isinstance(t, dict) and next(iter(t)) == DIMENSION
    ]


def _dimension_rows(rows: list) -> dict:
    return {r["column"]: r for r in rows if r["test"] == DIMENSION}


def test_a_small_domain_dimension_is_written_with_the_time_column():
    rows: list = []

    out = _render(_dims("status"), rows=rows, domains=_domains(_profile("status", 4)))

    [test] = _dimension_tests(out)
    assert test["arguments"] == {
        "dimensions": ["status"],
        "timestamp_column": "ordered_at",
    }
    marker = test["config"]["meta"]["dex"]["monitor"]
    assert marker == {
        "vendor": "elementary",
        "from": "columns.status.role: dimension",
        "max_distinct": mon.DIMENSION_MAX_DISTINCT,
    }
    assert "dex_monitor" in test["config"]["tags"]
    row = _dimension_rows(rows)["status"]
    assert row["status"] == mon.ADDED
    assert (row["distinct_count"], row["distinct_count_exact"], row["bound"]) == (
        4,
        True,
        20,
    )
    assert row["profile"]["identifier"] == "dev.main.fct_orders"
    # The time monitors are unchanged beside it.
    assert {r["test"] for r in rows} == {VOLUME, FRESHNESS, DIMENSION}


@pytest.mark.parametrize(("distinct", "written"), [(20, True), (21, False)])
def test_the_bound_is_inclusive(distinct, written):
    rows: list = []

    out = _render(
        _dims("channel"), rows=rows, domains=_domains(_profile("channel", distinct))
    )

    assert bool(_dimension_tests(out)) is written
    row = _dimension_rows(rows)["channel"]
    if written:
        assert row["status"] == mon.ADDED
    else:
        assert (row["status"], row["reason"]) == (
            mon.NOT_RENDERED,
            mon.DOMAIN_TOO_LARGE,
        )
        assert row["distinct_count"] == 21


def test_each_qualifying_dimension_gets_its_own_test():
    # The package groups a `dimensions` list by the columns' combination, so
    # two columns in one test would be up to 4 x 3 series, not 4 + 3.
    out = _render(
        _dims("status", "channel", "city"),
        domains=_domains(
            _profile("status", 4), _profile("channel", 3), _profile("city", 812)
        ),
    )

    assert [t["arguments"]["dimensions"] for t in _dimension_tests(out)] == [
        ["status"],
        ["channel"],
    ]


@pytest.mark.parametrize(
    ("cache", "reason", "command"),
    [
        (None, mon.NOT_PROFILED, "explore profile dev.main.fct_orders --target dev"),
        (
            "no_column",
            mon.NOT_PROFILED,
            "explore profile dev.main.fct_orders --target dev",
        ),
        (
            "no_count",
            mon.NOT_PROFILED,
            "explore profile dev.main.fct_orders --target dev",
        ),
    ],
)
def test_a_dimension_with_no_cached_count_is_left_out_and_named(cache, reason, command):
    from exmergo_dex_core.cache import DexCache

    caches = {
        None: DexCache(),
        "no_column": _cache(),
        "no_count": _cache(_profile("status", None)),
    }
    rows: list = []
    notes: list = []

    out = _render(
        _dims("status"),
        rows=rows,
        notes=notes,
        domains=_domains(cache=caches[cache]),
    )

    assert _dimension_tests(out) == []
    row = _dimension_rows(rows)["status"]
    assert (row["status"], row["reason"]) == (mon.NOT_RENDERED, reason)
    assert row["command"] == command
    assert "distinct_count" not in row
    [note] = [n for n in notes if "dimension_anomalies" in n]
    assert f"fct_orders.status (`{command}`)" in note
    assert "does not profile" in note


def test_a_model_the_plan_creates_is_built_before_it_is_profiled():
    from exmergo_dex_core.cache import DexCache

    rows: list = []

    _render(
        _dims("status"),
        rows=rows,
        domains=_domains(cache=DexCache(), created=frozenset({"fct_orders"})),
    )

    assert _dimension_rows(rows)["status"]["command"] == (
        "transform build --target dev, then "
        "explore profile dev.main.fct_orders --target dev"
    )


def test_a_name_matching_several_profiles_is_ambiguous():
    from exmergo_dex_core.cache import Dataset, DexCache

    cache = DexCache(
        datasets=[
            Dataset(identifier=i, columns=[_profile("status", 4)])
            for i in ("dev.main.fct_orders", "prod.main.fct_orders")
        ]
    )
    rows: list = []

    out = _render(_dims("status"), rows=rows, domains=_domains(cache=cache))

    assert _dimension_tests(out) == []
    row = _dimension_rows(rows)["status"]
    assert row["reason"] == mon.PROFILE_AMBIGUOUS
    assert "2 relations" in row["detail"]


def test_a_column_flagged_as_personal_data_is_never_a_dimension():
    from exmergo_dex_core.cache import PIICategory, PIIFlag

    rows: list = []
    flag = PIIFlag(category=PIICategory.EMAIL, confidence=0.1)

    out = _render(
        _dims("contact"),
        rows=rows,
        domains=_domains(_profile("contact", 3, pii=flag)),
    )

    assert _dimension_tests(out) == []
    row = _dimension_rows(rows)["contact"]
    assert (row["status"], row["reason"]) == (mon.NOT_RENDERED, mon.PII_FLAGGED)
    # Its domain is never reported.
    assert "distinct_count" not in row


def test_a_flagged_column_with_no_count_reads_as_flagged_not_unprofiled():
    from exmergo_dex_core.cache import PIICategory, PIIFlag

    rows: list = []
    flag = PIIFlag(category=PIICategory.EMAIL, confidence=0.9)

    _render(
        _dims("contact"),
        rows=rows,
        domains=_domains(_profile("contact", None, pii=flag)),
    )

    assert _dimension_rows(rows)["contact"]["reason"] == mon.PII_FLAGGED


def test_a_column_the_yaml_marks_as_personal_data_is_never_a_dimension():
    content = (
        "version: 2\n"
        "models:\n"
        "  - name: fct_orders\n"
        "    columns:\n"
        "      - name: contact\n"
        "        config:\n"
        "          meta:\n"
        "            contains_pii: true\n"
    )
    rows: list = []

    out = _render(
        _dims("contact"),
        content=content,
        rows=rows,
        domains=_domains(_profile("contact", 3)),
    )

    assert _dimension_tests(out) == []
    assert _dimension_rows(rows)["contact"]["reason"] == mon.PII_FLAGGED


def test_a_pii_override_clears_a_cached_flag(tmp_path: Path):
    from exmergo_dex_core.cache import PIICategory, PIIFlag, mask_overridden
    from exmergo_dex_core.config import PIIOverride, pii_override_paths

    flag = PIIFlag(category=PIICategory.NAME, confidence=0.4)
    cache = mask_overridden(
        _cache(_profile("plan_name", 3, pii=flag)),
        pii_override_paths([PIIOverride(column="dev.main.fct_orders.plan_name")]),
    )

    out = _render(_dims("plan_name"), domains=_domains(cache=cache))

    assert [t["arguments"]["dimensions"] for t in _dimension_tests(out)] == [
        ["plan_name"]
    ]


@pytest.mark.parametrize(
    ("times", "reason"),
    [
        ((), mon.NO_TIME_COLUMN),
        (("ordered_at", "shipped_at"), mon.AMBIGUOUS_TIME_COLUMN),
    ],
)
def test_a_dimension_follows_the_time_column_rule(times, reason):
    rows: list = []

    out = _render(
        _dims("status", times=times),
        rows=rows,
        domains=_domains(_profile("status", 4)),
    )

    assert _dimension_tests(out) == []
    row = _dimension_rows(rows)["status"]
    assert (row["status"], row["reason"]) == (mon.NOT_RENDERED, reason)
    # Whether it qualifies is never read where it could not be written anyway.
    assert "distinct_count" not in row
    assert "command" not in row


def test_a_dimension_the_model_does_not_output_is_not_written():
    rows: list = []

    _render(
        _dims("status"),
        rows=rows,
        outputs=frozenset({"ordered_at"}),
        domains=_domains(_profile("status", 4)),
    )

    assert _dimension_rows(rows)["status"]["reason"] == mon.COLUMN_NOT_OUTPUT


def test_without_a_domain_reader_no_dimension_monitor_is_planned():
    rows: list = []

    _render(_dims("status"), rows=rows)

    assert DIMENSION not in {r["test"] for r in rows}


def test_a_dimension_monitor_carries_the_population_filter():
    payload = _dims("status")
    payload["population"] = {"filters": [{"column": "region", "excludes": ["test"]}]}
    payload["columns"]["region"] = {"role": "attribute"}

    out = _render(payload, domains=_domains(_profile("status", 4)), dialect="duckdb")

    [test] = _dimension_tests(out)
    assert test["arguments"]["where_expression"] == (
        "(region not in ('test') or region is null)"
    )
    assert test["config"]["meta"]["dex"]["monitor"]["filter"] == "population.filters"


def test_an_unwritable_filter_withholds_the_dimension_monitor_too():
    payload = _dims("status")
    payload["population"] = {"filters": [{"column": "region", "includes": ["a\nb"]}]}
    rows: list = []

    out = _render(payload, rows=rows, domains=_domains(_profile("status", 4)))

    assert _dimension_tests(out) == []
    assert _dimension_rows(rows)["status"]["reason"] == mon.FILTER_UNWRITABLE


def test_a_hand_written_dimension_monitor_on_the_column_is_kept():
    content = (
        "version: 2\n"
        "models:\n"
        "  - name: fct_orders\n"
        "    data_tests:\n"
        "      - elementary.dimension_anomalies:\n"
        "          dimensions: [STATUS]\n"
        "          anomaly_sensitivity: 2\n"
    )
    rows: list = []

    out = _render(
        _dims("status"),
        content=content,
        rows=rows,
        domains=_domains(_profile("status", 4)),
    )

    assert _dimension_tests(out) == [
        {"dimensions": ["STATUS"], "anomaly_sensitivity": 2}
    ]
    assert _dimension_rows(rows)["status"]["status"] == mon.HAND_WRITTEN


def test_a_hand_written_combined_dimension_monitor_is_another_monitor():
    content = (
        "version: 2\n"
        "models:\n"
        "  - name: fct_orders\n"
        "    data_tests:\n"
        "      - elementary.dimension_anomalies:\n"
        "          dimensions: [status, channel]\n"
    )
    rows: list = []

    out = _render(
        _dims("status"),
        content=content,
        rows=rows,
        domains=_domains(_profile("status", 4)),
    )

    dims = [t.get("arguments", t)["dimensions"] for t in _dimension_tests(out)]
    assert dims == [["status", "channel"], ["status"]]
    assert _dimension_rows(rows)["status"]["status"] == mon.ADDED


def test_declaring_again_lists_each_dimension_monitor_once():
    domains = _domains(_profile("status", 4))
    before = _render(_dims("status"), domains=domains)
    rows: list = []

    after = _render(_dims("status"), content=before, rows=rows, domains=domains)

    assert after == before
    assert _dimension_rows(rows)["status"]["status"] == mon.ALREADY_LISTED


def test_a_listed_monitor_whose_profile_is_gone_stays_listed():
    from exmergo_dex_core.cache import DexCache

    before = _render(_dims("status"), domains=_domains(_profile("status", 4)))
    rows: list = []
    notes: list = []

    after = _render(
        _dims("status"),
        content=before,
        rows=rows,
        notes=notes,
        domains=_domains(cache=DexCache()),
    )

    assert after == before
    row = _dimension_rows(rows)["status"]
    assert row["status"] == mon.ALREADY_LISTED
    assert not any("stale" in n or "no longer" in n for n in notes)


@pytest.mark.parametrize(
    ("after_payload", "domains", "cause"),
    [
        (_dims("status"), ("status", 25, None), mon.DOMAIN_TOO_LARGE),
        (_dims(), ("status", 4, None), "not_declared"),
        (_dims("status"), ("status", 4, "email"), mon.PII_FLAGGED),
    ],
)
def test_a_dimension_that_no_longer_qualifies_is_stale_and_kept(
    after_payload, domains, cause
):
    from exmergo_dex_core.cache import PIICategory, PIIFlag

    before = _render(_dims("status"), domains=_domains(_profile("status", 4)))
    name, distinct, pii = domains
    flag = PIIFlag(category=PIICategory(pii), confidence=0.5) if pii else None
    if after_payload["columns"].get("status") is None:
        # Declared again as an attribute: no longer a dimension.
        after_payload = {
            **after_payload,
            "columns": {**after_payload["columns"], "status": {"role": "attribute"}},
        }
    rows: list = []
    notes: list = []

    after = _render(
        after_payload,
        content=before,
        rows=rows,
        notes=notes,
        domains=_domains(_profile(name, distinct, pii=flag)),
    )

    assert _dimension_tests(after) == _dimension_tests(before)
    [stale] = [r for r in rows if r["status"] == mon.STALE]
    assert (stale["test"], stale["column"], stale["changed"], stale["cause"]) == (
        DIMENSION,
        "status",
        "dimensions",
        cause,
    )
    if cause == mon.DOMAIN_TOO_LARGE:
        assert stale["distinct_count"] == 25
    else:
        assert "distinct_count" not in stale
    [note] = [n for n in notes if "no longer qualifies" in n]
    assert "dex leaves it in place" in note


def test_a_dimension_monitor_whose_filter_moved_is_stale_with_its_replacement():
    domains = _domains(_profile("status", 4))
    before = _render(_dims("status"), domains=domains)
    payload = _dims("status")
    payload["population"] = {"filters": [{"column": "status", "includes": ["placed"]}]}
    rows: list = []

    _render(payload, content=before, rows=rows, domains=domains)

    [stale] = [r for r in rows if r["status"] == mon.STALE and r["test"] == DIMENSION]
    assert stale["changed"] == "where_expression"
    assert stale["replacement"]["status"] == mon.ADDED


def test_a_dimension_monitor_on_a_moved_time_column_is_stale():
    before = _render(_dims("status"), domains=_domains(_profile("status", 4)))
    rows: list = []
    notes: list = []

    _render(
        _dims("status", times=("placed_at",)),
        content=before,
        rows=rows,
        notes=notes,
        domains=_domains(_profile("status", 4)),
    )

    [stale] = [r for r in rows if r["status"] == mon.STALE and r["test"] == DIMENSION]
    assert stale["changed"] == "timestamp_column"
    assert any("timestamp_column ordered_at" in n for n in notes)


def test_a_versioned_model_takes_its_dimension_monitor_on_the_version():
    content = (
        "version: 2\n"
        "models:\n"
        "  - name: fct_orders\n"
        "    latest_version: 1\n"
        "    versions:\n"
        "      - v: 1\n"
        "      - v: 2\n"
    )
    payload = {**_dims("status"), "model": "fct_orders.v2"}

    out = _render(
        payload,
        content=content,
        domains=_domains(
            cache=_cache(_profile("status", 4), identifier="dev.main.fct_orders_v2")
        ),
    )

    tests = _tests(out, version=2)
    assert any(isinstance(t, dict) and next(iter(t)) == DIMENSION for t in tests)
    assert _tests(out, version=1) == []


def test_a_dimension_decision_names_its_dimensions_and_the_bound():
    out = _render(_dims("status"), domains=_domains(_profile("status", 4)))

    found = decisions({"models/schema.yml": out}, ["fct_orders"])
    [dimension] = [d for d in found if d["monitor"] == DIMENSION]

    assert dimension["chosen"] == "dimensions: [status]; timestamp_column: ordered_at"
    assert dimension["field"] == "columns.status.role: dimension"
    assert "at most 20 distinct values" in dimension["detail"]
    assert "column" not in dimension


def test_a_dimension_decision_settles_no_population_change():
    decision = {
        "model": "fct_orders",
        "decision": "Monitor elementary.dimension_anomalies",
        "chosen": "dimensions: [status]; timestamp_column: ordered_at",
        "evidence": "contract",
        "monitor": DIMENSION,
        "field": "columns.status.role: dimension",
    }
    report = change_scope.analyze(
        [
            (
                "models/marts/fct_orders.sql",
                "select order_id, status from orders\n",
                "select order_id, status from orders where status = 'placed'\n",
            )
        ],
        [decision],
        "duckdb",
    )

    assert report is not None
    [scope] = report["models"]
    assert scope["population"] == "changed"
    assert all(c["status"] == "undeclared" for c in scope["changes"] if "status" in c)


def test_a_closed_gate_writes_no_dimension_monitor():
    rows: list = []
    gate = mon.Gate(
        "elementary", mon.INSTALLED, "0.26.1", mon.DISABLED, mon.LITERAL, "prod"
    )

    out = _render(
        _dims("status"), gate=gate, rows=rows, domains=_domains(_profile("status", 4))
    )

    assert _dimension_tests(out) == []
    assert _dimension_rows(rows)["status"]["reason"] == mon.PRODUCTION_DISABLED


# --- the plan, end to end ---------------------------------------------------------

DIMENSIONED = {
    **DECLARED,
    "columns": {**DECLARED["columns"], "status": {"role": "dimension"}},
}


def _save(tmp_path: Path, cache) -> None:
    from exmergo_dex_core.storage import FilesystemStore

    FilesystemStore(tmp_path).save_cache(cache)


def test_the_plan_writes_a_dimension_monitor_from_the_cached_domain(
    dbt_project_dir: Path, tmp_path: Path, capsys
):
    _install(dbt_project_dir)
    _repo(tmp_path)
    _save(tmp_path, _cache(_profile("status", 3)))

    planned = _plan(tmp_path, capsys, DIMENSIONED)

    assert planned["status"] == "ok", planned["errors"]
    rows = _dimension_rows(planned["data"]["monitors"]["monitors"])
    assert rows["status"]["status"] == mon.ADDED
    assert rows["status"]["distinct_count"] == 3
    chosen = [
        d["chosen"]
        for d in planned["data"]["decisions"]
        if d.get("monitor") == DIMENSION
    ]
    assert chosen == ["dimensions: [status]; timestamp_column: ordered_at"]
    applied = _dex(["--repo-root", str(tmp_path), "transform", "apply"], capsys)
    assert applied["status"] == "ok", applied["errors"]
    written = (dbt_project_dir / "models" / "marts" / "fct_orders.yml").read_text(
        encoding="utf-8"
    )
    assert [t["arguments"]["dimensions"] for t in _dimension_tests(written)] == [
        ["status"]
    ]


def test_the_plan_names_the_profile_a_dimension_needs(
    dbt_project_dir: Path, tmp_path: Path, capsys
):
    _install(dbt_project_dir)
    _repo(tmp_path)

    planned = _plan(tmp_path, capsys, DIMENSIONED)

    assert planned["status"] == "ok", planned["errors"]
    row = _dimension_rows(planned["data"]["monitors"]["monitors"])["status"]
    assert (row["status"], row["reason"]) == (mon.NOT_RENDERED, mon.NOT_PROFILED)
    assert row["command"].startswith(
        "transform build --target dev, then explore profile"
    )
    assert any(
        "dimension_anomalies" in w and "does not profile" in w
        for w in planned["warnings"]
    )
    assert DIMENSION not in json.dumps(planned["diffs"])


def test_transform_declare_writes_an_owed_dimension_monitor(
    dbt_project_dir: Path, tmp_path: Path, capsys
):
    _install(dbt_project_dir)
    (dbt_project_dir / "models" / "staging" / "stg_customers.sql").write_text(
        "select 1 as id, current_timestamp as signed_up_at, 'pro' as tier\n",
        encoding="utf-8",
    )
    (dbt_project_dir / "models" / "staging" / "schema.yml").write_text(
        "version: 2\n"
        "models:\n"
        "  - name: stg_customers\n"
        "    description: One row per customer.\n"
        "    config:\n"
        "      meta:\n"
        "        dex:\n"
        "          grain: [id]\n"
        "    columns:\n"
        "      - name: id\n"
        "        data_tests: [unique, not_null]\n"
        "        config:\n"
        "          meta:\n"
        "            dex:\n"
        "              role: key\n"
        "      - name: signed_up_at\n"
        "        config:\n"
        "          meta:\n"
        "            dex:\n"
        "              role: time\n"
        "      - name: tier\n"
        "        config:\n"
        "          meta:\n"
        "            dex:\n"
        "              role: dimension\n",
        encoding="utf-8",
    )
    _repo(tmp_path)
    from exmergo_dex_core.cache import Dataset, DexCache

    _save(
        tmp_path,
        DexCache(
            datasets=[
                Dataset(
                    identifier="dev.main.stg_customers",
                    columns=[_profile("id", 1), _profile("tier", 3)],
                )
            ]
        ),
    )

    def declare() -> dict:
        return _dex(
            ["--repo-root", str(tmp_path), "transform", "declare", "stg_customers"],
            capsys,
        )

    proposed = declare()

    assert proposed["status"] == "ok", proposed["errors"]
    [proposal] = proposed["data"]["proposed"]
    assert [m["test"] for m in proposal["fields"]["monitors"]["value"]] == [
        VOLUME,
        FRESHNESS,
        DIMENSION,
    ]
    # The stored plan's monitor report rides on the declare envelope too.
    assert _dimension_rows(proposed["data"]["monitors"]["monitors"])["tier"][
        "status"
    ] == (mon.ADDED)
    applied = _dex(["--repo-root", str(tmp_path), "transform", "apply"], capsys)
    assert applied["status"] == "ok", applied["errors"]
    assert declare()["data"]["plan_id"] is None


@pytest.mark.parametrize(
    ("column", "reason"),
    [("customer_email", mon.PII_FLAGGED), ("plan_name", mon.NOT_PROFILED)],
)
def test_an_unprofiled_column_named_as_personal_data_is_not_sent_to_profile(
    column, reason
):
    from exmergo_dex_core.cache import DexCache

    rows: list = []
    notes: list = []

    _render(_dims(column), rows=rows, notes=notes, domains=_domains(cache=DexCache()))

    # A name the detector flags outright is out before any profile; a generic
    # `*_name` match is provisional, left to the profile's value-shape check.
    assert _dimension_rows(rows)[column]["reason"] == reason
    assert any(column in n for n in notes) is (reason == mon.NOT_PROFILED)


def test_the_profile_note_names_each_command_once():
    from exmergo_dex_core.cache import DexCache

    notes: list = []

    _render(_dims("status", "channel"), notes=notes, domains=_domains(cache=DexCache()))

    [note] = [n for n in notes if "dimension_anomalies" in n]
    assert note.count("explore profile") == 1
    assert "fct_orders.status, fct_orders.channel (`explore profile" in note


def test_a_complete_value_domain_counts_the_domain_exactly():
    from exmergo_dex_core.cache import ValueCount, ValueDomain

    profile = _profile("status", 22, exact=False)
    profile.value_domain = ValueDomain(
        values=[ValueCount(value=f"s{i}", count=1) for i in range(19)], elided=0
    )
    rows: list = []

    out = _render(_dims("status"), rows=rows, domains=_domains(profile))

    # The estimate says 22; the domain the profile listed in full says 19.
    row = _dimension_rows(rows)["status"]
    assert (row["distinct_count"], row["distinct_count_exact"]) == (19, True)
    assert _dimension_tests(out)


def test_an_approximate_count_with_no_complete_domain_is_read_as_counted():
    rows: list = []

    _render(_dims("sku"), rows=rows, domains=_domains(_profile("sku", 30, exact=False)))

    row = _dimension_rows(rows)["sku"]
    assert (row["reason"], row["distinct_count_exact"]) == (mon.DOMAIN_TOO_LARGE, False)


# --- review follow-ups ------------------------------------------------------------

_VERSIONED = (
    "version: 2\n"
    "models:\n"
    "  - name: fct_orders\n"
    "    latest_version: 2\n"
    "    versions:\n"
    "      - v: 1\n"
    "        config:\n"
    "          alias: fct_orders\n"
    "      - v: 2\n"
)


def _versioned_cache(*datasets):
    from exmergo_dex_core.cache import Dataset, DexCache

    return DexCache(
        datasets=[
            Dataset(
                identifier=f"dev.main.{name}", columns=[_profile("ordered_at", 9), c]
            )
            for name, c in datasets
        ]
    )


def test_a_version_with_an_alias_reads_the_relation_dbt_builds():
    files = {"models/marts/fct_orders.yml": _VERSIONED}
    domains = mon.Domains(
        cache=_versioned_cache(("fct_orders", _profile("status", 4))),
        files=files,
        dev_namespace="dev.main",
    )

    reading = domains.read("fct_orders.v1", "status")

    assert (reading.status, reading.distinct_count) == (mon.MEASURED, 4)
    assert reading.identifier == "dev.main.fct_orders"


def test_a_shared_versioned_entry_screens_every_version():
    from exmergo_dex_core.cache import PIICategory, PIIFlag

    files = {"models/marts/fct_orders.yml": _VERSIONED}
    flag = PIIFlag(category=PIICategory.EMAIL, confidence=0.3)
    flagged = mon.Domains(
        cache=_versioned_cache(
            ("fct_orders", _profile("status", 4)),
            ("fct_orders_v2", _profile("status", 4, pii=flag)),
        ),
        files=files,
    )
    clean = mon.Domains(
        cache=_versioned_cache(
            ("fct_orders", _profile("status", 4)),
            ("fct_orders_v2", _profile("status", 12)),
        ),
        files=files,
    )

    # Every version inherits a test on the shared entry, so a flag on any
    # version keeps the column out, and the count is the largest.
    assert flagged.read("fct_orders", "status").pii is True
    assert clean.read("fct_orders", "status").distinct_count == 12


def test_a_reprofile_of_a_dev_relation_names_target_dev():
    rows: list = []

    _render(
        _dims("status"),
        rows=rows,
        domains=_domains(cache=_cache(_profile("status", None))),
    )

    assert _dimension_rows(rows)["status"]["command"] == (
        "explore profile dev.main.fct_orders --target dev"
    )


def test_a_reprofile_outside_the_dev_namespace_needs_no_target():
    rows: list = []

    _render(
        _dims("status"),
        rows=rows,
        domains=_domains(
            cache=_cache(_profile("status", None), identifier="raw.app.fct_orders")
        ),
    )

    assert _dimension_rows(rows)["status"]["command"] == (
        "explore profile raw.app.fct_orders"
    )


def test_a_dimension_whose_time_column_is_not_output_is_not_written():
    rows: list = []

    out = _render(
        _dims("status"),
        rows=rows,
        outputs=frozenset({"status"}),
        domains=_domains(_profile("status", 4)),
    )

    assert _dimension_tests(out) == []
    row = _dimension_rows(rows)["status"]
    assert row["reason"] == mon.COLUMN_NOT_OUTPUT
    assert "distinct_count" not in row


def test_transform_declare_with_nothing_to_propose_names_the_profile_owed(
    dbt_project_dir: Path, tmp_path: Path, capsys
):
    _install(dbt_project_dir)
    (dbt_project_dir / "models" / "staging" / "stg_customers.sql").write_text(
        "select 1 as id, current_timestamp as signed_up_at, 'pro' as tier\n",
        encoding="utf-8",
    )
    (dbt_project_dir / "models" / "staging" / "schema.yml").write_text(
        "version: 2\n"
        "models:\n"
        "  - name: stg_customers\n"
        "    description: One row per customer.\n"
        "    config:\n"
        "      meta:\n"
        "        dex:\n"
        "          grain: [id]\n"
        "    columns:\n"
        "      - name: id\n"
        "        data_tests: [unique, not_null]\n"
        "        config:\n"
        "          meta:\n"
        "            dex:\n"
        "              role: key\n"
        "      - name: signed_up_at\n"
        "        config:\n"
        "          meta:\n"
        "            dex:\n"
        "              role: time\n"
        "      - name: tier\n"
        "        config:\n"
        "          meta:\n"
        "            dex:\n"
        "              role: dimension\n",
        encoding="utf-8",
    )
    _repo(tmp_path)

    def declare() -> dict:
        return _dex(
            ["--repo-root", str(tmp_path), "transform", "declare", "stg_customers"],
            capsys,
        )

    # The time monitors are owed and written; the dimension waits on a profile.
    first = declare()
    assert first["data"]["plan_id"] is not None, first["errors"]
    applied = _dex(["--repo-root", str(tmp_path), "transform", "apply"], capsys)
    assert applied["status"] == "ok", applied["errors"]

    again = declare()

    assert again["data"]["plan_id"] is None
    said = json.dumps(again["data"]["notes"])
    assert "stg_customers.tier" in said
    assert "explore profile" in said
