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
    payload: dict, *, content=None, gate=OPEN, rows=None, notes=None, outputs=None
):
    [declaration] = parse_declarations([payload])
    return render(
        content,
        declaration,
        notes=notes,
        outputs=outputs,
        monitors=gate,
        monitor_rows=rows,
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
        "select 1 where false{% endtest %}\n",
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


def test_a_list_holding_text_is_written_as_text_throughout():
    # As maintain verify and dex_population_filter compare it.
    assert (
        _where(_filtered({"column": "code", "includes": ["a", 7, False]}))
        == "code in ('a', '7', 'false')"
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


def test_a_value_holding_a_jinja_delimiter_is_unwritable():
    with pytest.raises(mon.FilterUnwritableError):
        _where(_filtered({"column": "status", "includes": ["{{ var('x') }}"]}))


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
    assert len(notes) == 2


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
