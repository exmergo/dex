"""``dex_temporal_contract``: the generated test for a declared history (#1458).

A declaration's ``history`` says how a status-history model reads its
snapshot. With ``declarations.render_tests`` on, each measure it names renders
a model-level test that fails with the number of entities whose column
disagrees with what the snapshot's versions and the policy give: the count
``maintain verify``'s ``temporal_*_mismatch`` finding states for that measure.
A measure verify would leave unchecked renders nothing, with verify's reason.

These are the rendering, the static reading of the snapshot and its lineage,
the gating, and the SQL the macro compiles to, held to verify's own statement
on the same DuckDB tables. Whether a built project agrees end to end is the
parity harness's question (``test_generated_parity.py``).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from exmergo_dex_core import generated_tests as gt
from exmergo_dex_core.cli import main
from exmergo_dex_core.dbt_project import DbtProjectView, SourceFile, content_hash
from exmergo_dex_core.semantic_declarations import History
from exmergo_dex_core.transform import scaffold
from exmergo_dex_core.transform.claims import claim_id
from exmergo_dex_core.transform.declarations import parse_declarations, render
from exmergo_dex_core.transform.history_reading import (
    history_reader,
    snapshot_config,
)
from exmergo_dex_core.transform.plans import EditKind, PlanEdit
from exmergo_dex_core.transform.validate import validate_edit

NAME = gt.TEMPORAL_CONTRACT
MODEL = "host_history"
SNAPSHOT = "hosts_snapshot"


def _history(**overrides) -> dict:
    history = {
        "snapshot": SNAPSHOT,
        "entity": {"host_id": "host_id"},
        "status": {"column": "is_super", "target": True},
        "initial_observation": "baseline",
        "null_status": "ignore",
        "population": "all_observed",
        "time": {"column": "dbt_valid_from", "provenance": "capture"},
        "measures": {
            "transition_count": "changes",
            "latest_target_transition_at": "latest_at",
            "first_target_at": "first_at",
        },
    }
    history.update(overrides)
    return {k: v for k, v in history.items() if v is not None}


def _descends(**fields) -> gt.HistoryReading:
    return gt.HistoryReading(lineage="descends", **fields)


def _render(
    history: dict,
    render_tests=True,
    *,
    reading: gt.HistoryReading | None = None,
    generated=None,
    outputs=None,
):
    [declaration] = parse_declarations([{"model": MODEL, "history": history}])
    stated = reading or _descends()
    return render(
        None,
        declaration,
        render_tests=render_tests,
        generated=generated,
        outputs=outputs,
        history=lambda _model, _history: stated,
    )


def _tests(content: str) -> list[dict]:
    entry = next(e for e in yaml.safe_load(content)["models"] if e["name"] == MODEL)
    return [t[NAME] for t in entry.get("data_tests") or [] if NAME in t]


def _rows(history: dict, **kwargs) -> list[dict]:
    generated: list[dict] = []
    _render(history, generated=generated, **kwargs)
    return [g for g in generated if g["test"] == NAME]


# --- the kind ---------------------------------------------------------------------


def test_it_ships_as_a_scheduled_test_on_every_connector():
    [kind] = [k for k in gt.kinds() if k.name == NAME]
    assert kind.cadence == gt.SCHEDULED
    assert kind.macro == NAME
    assert kind.dialects == gt.ALL_CONNECTORS
    assert kind.reads_history
    assert NAME in scaffold.MACRO_ASSETS
    assert gt.unknown_entries([NAME]) == []


def test_the_policy_table_is_the_one_verify_reads():
    from exmergo_dex_core.maintain import temporal_contracts as temporal_mod

    assert set(temporal_mod._MEASURE_FINDINGS) == set(gt.TEMPORAL_MEASURES)
    history = History.model_validate(
        _history(initial_observation=None, null_status=None, time=None)
    )
    assert gt.policy_owed(history, "transition_count") == [
        "initial_observation",
        "null_status",
    ]
    assert gt.policy_owed(history, "latest_target_transition_at") == [
        "initial_observation",
        "null_status",
        "time.provenance",
    ]
    assert gt.policy_owed(history, "first_target_at") == ["time.provenance"]


def test_each_declared_measure_renders_its_own_model_level_test():
    tests = _tests(_render(_history()))

    assert [t["arguments"]["measure"] for t in tests] == list(gt.TEMPORAL_MEASURES)
    assert tests[0]["arguments"] == {
        "snapshot": f"ref('{SNAPSHOT}')",
        "entity": {"host_id": "host_id"},
        "status_column": "is_super",
        "target": True,
        "measure": "transition_count",
        "measure_column": "changes",
        "initial_observation": "baseline",
        "null_status": "ignore",
        "time_column": "dbt_valid_from",
        "provenance": "capture",
    }
    for test in tests:
        assert test["config"]["tags"] == [gt.SCHEDULED]
        assert test["config"]["meta"]["disable_test_samples"] is True


def test_a_measure_the_history_leaves_out_renders_nothing():
    history = _history(measures={"transition_count": "changes"})
    assert [t["arguments"]["measure"] for t in _tests(_render(history))] == [
        "transition_count"
    ]


def test_the_report_names_each_measure_as_its_variant():
    rows = _rows(_history())
    assert [r["variant"] for r in rows] == list(gt.TEMPORAL_MEASURES)
    assert {r["status"] for r in rows} == {"added"}
    assert {r["cadence"] for r in rows} == {gt.SCHEDULED}


def test_the_tests_are_written_without_yaml_anchors():
    content = _render(_history(entity={"host_id": "host_id", "site": "site_id"}))
    assert "&id" not in content and "*id" not in content


def test_a_renamed_validity_column_is_passed_as_the_order_column():
    [test, *_] = _tests(_render(_history(), reading=_descends(valid_from="valid_at")))
    assert test["arguments"]["order_column"] == "valid_at"
    [plain, *_] = _tests(_render(_history()))
    assert "order_column" not in plain["arguments"]


def test_each_measure_is_its_own_claim_and_a_policy_change_is_another():
    def claims(history: dict) -> list[str]:
        return [t["config"]["meta"]["dex"]["claim"] for t in _tests(_render(history))]

    base = claims(_history())
    assert len(set(base)) == 3
    assert base[0] == claim_id(
        MODEL,
        "history",
        ["host_id", "changes"],
        {
            "check": NAME,
            "measure": "transition_count",
            "snapshot": SNAPSHOT,
            "entity": [["host_id", "host_id"]],
            "status_column": "is_super",
            "target": [True],
            "order_column": "dbt_valid_from",
            "initial_observation": "baseline",
            "null_status": "ignore",
            "time_column": "dbt_valid_from",
            "provenance": "capture",
        },
    )
    assert set(claims(_history(initial_observation="transition"))).isdisjoint(base)
    assert (
        set(claims(_history(status={"column": "is_super", "target": False})))
        & set(base)
        == set()
    )
    # A respelled name states the same fact.
    assert claims(_history(entity={"HOST_ID": "Host_Id"})) == base


# --- what renders nothing ---------------------------------------------------------


@pytest.mark.parametrize(
    ("dropped", "withheld"),
    [
        (
            {"null_status": None},
            {"transition_count", "latest_target_transition_at"},
        ),
        (
            {"initial_observation": None},
            {"transition_count", "latest_target_transition_at"},
        ),
        (
            {"time": {"column": "dbt_valid_from"}},
            {"latest_target_transition_at", "first_target_at"},
        ),
        ({"time": None}, {"latest_target_transition_at", "first_target_at"}),
    ],
)
def test_a_missing_policy_withholds_the_measures_that_depend_on_it(dropped, withheld):
    rows = _rows(_history(**dropped))
    by_measure = {r["variant"]: r for r in rows}
    for measure, row in by_measure.items():
        if measure in withheld:
            assert row["status"] == "not_rendered"
            assert row["reason"] == gt.POLICY_MISSING
        else:
            assert row["status"] == "added"
    content = _render(_history(**dropped))
    assert {t["arguments"]["measure"] for t in _tests(content)} == (
        set(gt.TEMPORAL_MEASURES) - withheld
    )


def test_a_population_left_out_withholds_no_measure():
    # verify reports the population assertion unchecked; no measure reads it.
    rows = _rows(_history(population=None))
    assert {r["status"] for r in rows} == {"added"}


@pytest.mark.parametrize(
    ("reading", "reason"),
    [
        (gt.HistoryReading(lineage="not_ancestor"), gt.SNAPSHOT_NOT_ANCESTOR),
        (gt.HistoryReading(lineage="unknown", why="x"), gt.LINEAGE_UNAVAILABLE),
        (_descends(hard_deletes="new_record"), gt.HARD_DELETE_RECORDS),
        (_descends(config_unread="hard_deletes"), gt.SNAPSHOT_CONFIG_UNREAD),
    ],
)
def test_what_verify_cannot_measure_renders_nothing_and_says_why(reading, reason):
    rows = _rows(_history(), reading=reading)
    assert [r["status"] for r in rows] == ["not_rendered"] * 3
    assert {r["reason"] for r in rows} == {reason}
    assert _tests(_render(_history(), reading=reading)) == []


def test_the_lineage_reason_comes_before_a_missing_policy():
    rows = _rows(
        _history(null_status=None), reading=gt.HistoryReading(lineage="not_ancestor")
    )
    assert {r["reason"] for r in rows} == {gt.SNAPSHOT_NOT_ANCESTOR}


def test_invalidated_hard_deletes_still_render():
    rows = _rows(_history(), reading=_descends(hard_deletes="invalidate"))
    assert {r["status"] for r in rows} == {"added"}


def test_a_measure_column_the_model_does_not_output_renders_nothing():
    rows = _rows(_history(), outputs=frozenset({"host_id", "changes", "first_at"}))
    by = {r["variant"]: r for r in rows}
    assert by["latest_target_transition_at"]["reason"] == gt.COLUMN_NOT_OUTPUT
    assert by["transition_count"]["status"] == "added"


def test_the_switch_off_renders_nothing_and_says_why():
    rows = _rows(_history(), render_tests=False)
    assert {r["reason"] for r in rows} == {"render_tests_off"}


def test_the_every_run_cadence_leaves_it_off():
    rows = _rows(_history(), render_tests=[gt.EVERY_RUN])
    assert {r["reason"] for r in rows} == {"kind_not_enabled"}


@pytest.mark.parametrize("setting", [[NAME], [gt.SCHEDULED]])
def test_a_list_naming_the_kind_or_its_cadence_turns_it_on(setting):
    assert {r["status"] for r in _rows(_history(), render_tests=setting)} == {"added"}


def test_no_reading_renders_nothing():
    [declaration] = parse_declarations([{"model": MODEL, "history": _history()}])
    generated: list[dict] = []
    render(None, declaration, render_tests=True, generated=generated)
    assert [g for g in generated if g["test"] == NAME] == []


# --- reading the snapshot and the lineage, statically ----------------------------


def _view(tmp_path: Path, files: dict[str, str]) -> DbtProjectView:
    files = {"dbt_project.yml": "name: p\nprofile: p\n", **files}
    return DbtProjectView(
        root=str(tmp_path),
        project_name="p",
        profile_name="p",
        files={
            p: SourceFile(path=p, content=c, sha256=content_hash(c))
            for p, c in files.items()
        },
    )


def _read(tmp_path: Path, files: dict[str, str], model: str = MODEL):
    view = _view(tmp_path, files)
    contents = {p: f.content for p, f in view.files.items()}
    return history_reader(view, contents)(model, History.model_validate(_history()))


SNAP_SQL = (
    "{% snapshot hosts_snapshot %}\n{{ config(unique_key='host_id', "
    "strategy='check', check_cols='all'CONFIG) }}\nselect * from {{ ref('hosts') }}\n"
    "{% endsnapshot %}\n"
)


def _snap(config: str = "") -> dict[str, str]:
    return {
        "snapshots/hosts_snapshot.sql": SNAP_SQL.replace("CONFIG", config),
        "models/hosts.sql": "select 1 as host_id, true as is_super\n",
    }


def test_a_model_reading_the_snapshot_through_an_intermediate_descends(tmp_path):
    reading = _read(
        tmp_path,
        {
            **_snap(),
            "models/int_hosts.sql": "select * from {{ ref('hosts_snapshot') }}\n",
            f"models/{MODEL}.sql": "select * from {{ ref('int_hosts') }}\n",
        },
    )
    assert reading.lineage == "descends"
    assert reading.valid_from == "dbt_valid_from"
    assert reading.hard_deletes is None


def test_a_model_that_does_not_reach_the_snapshot_is_not_an_ancestor(tmp_path):
    reading = _read(
        tmp_path,
        {**_snap(), f"models/{MODEL}.sql": "select * from {{ ref('hosts') }}\n"},
    )
    assert reading.lineage == "not_ancestor"


def test_a_model_reading_another_snapshot_names_it(tmp_path):
    other = SNAP_SQL.replace("CONFIG", "").replace("hosts_snapshot", "old_snapshot")
    reading = _read(
        tmp_path,
        {
            **_snap(),
            "snapshots/old_snapshot.sql": other,
            f"models/{MODEL}.sql": "select * from {{ ref('old_snapshot') }}\n",
        },
    )
    assert reading.lineage == "not_ancestor"
    assert reading.other_snapshots == ("old_snapshot",)


def test_a_ref_dex_cannot_resolve_leaves_the_lineage_unknown(tmp_path):
    reading = _read(
        tmp_path,
        {**_snap(), f"models/{MODEL}.sql": "select * from {{ ref(var('src')) }}\n"},
    )
    assert reading.lineage == "unknown"
    assert reading.why


def test_a_macro_that_may_read_a_relation_leaves_the_lineage_unknown(tmp_path):
    reading = _read(
        tmp_path,
        {
            **_snap(),
            f"models/{MODEL}.sql": "select * from {{ my_history_source() }}\n",
        },
    )
    assert reading.lineage == "unknown"
    assert "my_history_source" in (reading.why or "")


@pytest.mark.parametrize(
    ("files", "expected"),
    [
        (_snap(", hard_deletes='new_record'"), "new_record"),
        (
            {
                **_snap(),
                "dbt_project.yml": (
                    "name: p\nprofile: p\nsnapshots:\n  p:\n    +hard_deletes: "
                    "new_record\n"
                ),
            },
            "new_record",
        ),
        (_snap(", invalidate_hard_deletes=true"), "invalidate"),
        (
            {
                "snapshots/hosts.yml": yaml.safe_dump(
                    {
                        "snapshots": [
                            {
                                "name": SNAPSHOT,
                                "relation": "ref('hosts')",
                                "config": {"hard_deletes": "new_record"},
                            }
                        ]
                    }
                )
            },
            "new_record",
        ),
    ],
)
def test_hard_deletes_are_read_from_each_place_dbt_takes_them(
    tmp_path, files, expected
):
    view = _view(tmp_path, files)
    contents = {p: f.content for p, f in view.files.items()}
    assert snapshot_config(contents, view, SNAPSHOT)["hard_deletes"] == expected


def test_the_snapshot_block_wins_over_the_project(tmp_path):
    files = {
        **_snap(", hard_deletes='invalidate'"),
        "dbt_project.yml": (
            "name: p\nprofile: p\nsnapshots:\n  p:\n    +hard_deletes: new_record\n"
        ),
    }
    view = _view(tmp_path, files)
    contents = {p: f.content for p, f in view.files.items()}
    assert snapshot_config(contents, view, SNAPSHOT)["hard_deletes"] == "invalidate"


def test_a_renamed_validity_column_is_read(tmp_path):
    reading = _read(
        tmp_path,
        {
            **_snap(", snapshot_meta_column_names={'dbt_valid_from': 'valid_at'}"),
            f"models/{MODEL}.sql": "select * from {{ ref('hosts_snapshot') }}\n",
        },
    )
    assert reading.valid_from == "valid_at"


def test_a_config_value_written_as_jinja_is_unread(tmp_path):
    reading = _read(
        tmp_path,
        {
            **_snap(", hard_deletes=var('deletes')"),
            f"models/{MODEL}.sql": "select * from {{ ref('hosts_snapshot') }}\n",
        },
    )
    assert reading.config_unread == "hard_deletes"


# --- the plan --------------------------------------------------------------------


def _repo(tmp_path: Path, render_tests: str) -> Path:
    (tmp_path / ".dex").mkdir(exist_ok=True)
    (tmp_path / ".dex" / "config.yml").write_text(
        f"connector: duckdb\nduckdb:\n  path: {tmp_path / 'dev.duckdb'}\n"
        f"declarations:\n  render_tests: {render_tests}\n",
        encoding="utf-8",
    )
    return tmp_path


_PLAN_EDITS = [
    {
        "path": "models/hosts.sql",
        "kind": "model_sql",
        "content": "select 1 as host_id, true as is_super\n",
    },
    {
        "path": "snapshots/hosts_snapshot.sql",
        "kind": "snapshot_sql",
        "content": SNAP_SQL.replace("CONFIG", ""),
    },
    {
        "path": f"models/{MODEL}.sql",
        "kind": "model_sql",
        "content": (
            "select host_id, count(*) as changes, max(dbt_valid_from) as latest_at, "
            "min(dbt_valid_from) as first_at from {{ ref('hosts_snapshot') }} "
            "group by host_id\n"
        ),
    },
]


def _plan(tmp_path: Path, capsys, history: dict, edits=None) -> dict:
    payload = tmp_path / "edits.json"
    payload.write_text(
        json.dumps(
            {
                "edits": edits or _PLAN_EDITS,
                "declarations": [
                    {
                        "model": MODEL,
                        "description": "One row per host.",
                        "history": history,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    main(
        [
            "--repo-root",
            str(tmp_path),
            "transform",
            "plan",
            "host history",
            "--edits-file",
            str(payload),
        ]
    )
    return json.loads(capsys.readouterr().out)


def test_the_plan_that_renders_it_writes_its_macro_as_a_diff(
    dbt_project_dir: Path, tmp_path: Path, capsys
):
    planned = _plan(_repo(tmp_path, "true"), capsys, _history())

    assert planned["status"] == "ok", planned["errors"]
    assert f"macros/{NAME}.sql" in planned["data"]["paths"]
    mine = [t for t in planned["data"]["generated_tests"]["tests"] if t["test"] == NAME]
    assert [t["status"] for t in mine] == ["added"] * 3


def test_a_plan_whose_history_misses_a_policy_says_why(
    dbt_project_dir: Path, tmp_path: Path, capsys
):
    history = _history(null_status=None)
    planned = _plan(_repo(tmp_path, "true"), capsys, history)

    assert planned["status"] == "ok", planned["errors"]
    rows = {
        t["variant"]: t
        for t in planned["data"]["generated_tests"]["tests"]
        if t["test"] == NAME
    }
    assert rows["transition_count"]["status"] == "not_rendered"
    assert rows["transition_count"]["reason"] == gt.POLICY_MISSING
    assert rows["first_target_at"]["status"] == "added"
    assert any("null_status" in w for w in planned["warnings"])


def test_a_plan_whose_model_does_not_read_the_snapshot_renders_nothing(
    dbt_project_dir: Path, tmp_path: Path, capsys
):
    edits = [
        *_PLAN_EDITS[:2],
        {
            "path": f"models/{MODEL}.sql",
            "kind": "model_sql",
            "content": (
                "select host_id, 0 as changes, cast(null as timestamp) as latest_at, "
                "cast(null as timestamp) as first_at from {{ ref('hosts') }}\n"
            ),
        },
    ]
    planned = _plan(_repo(tmp_path, "true"), capsys, _history(), edits)

    assert planned["status"] == "ok", planned["errors"]
    mine = [t for t in planned["data"]["generated_tests"]["tests"] if t["test"] == NAME]
    assert {t["reason"] for t in mine} == {gt.SNAPSHOT_NOT_ANCESTOR}
    assert f"macros/{NAME}.sql" not in planned["data"]["paths"]


# --- the macro ------------------------------------------------------------------


def _asset() -> str:
    return scaffold.macro_edit(NAME, "macros").new_content or ""


def test_the_shipped_macro_passes_the_macro_file_check():
    validate_edit(
        PlanEdit(
            path=f"macros/{NAME}.sql", kind=EditKind.MACRO_SQL, new_content=_asset()
        )
    )


def test_each_warehouse_gets_the_spelling_verify_sends_it():
    asset = _asset()
    assert "clickhouse__dex_temporal_contract_lag(" in asset
    assert "lagInFrame(toNullable(" in asset
    assert "{% macro bigquery__dex_temporal_contract_timestamp() %}datetime" in asset
    for adapter in ("snowflake", "databricks"):
        assert (
            f"{{% macro {adapter}__dex_temporal_contract_timestamp() %}}timestamp_ntz"
        ) in asset
    for adapter in ("bigquery", "snowflake", "databricks", "spark", "redshift"):
        assert (
            f"{{% macro {adapter}__dex_temporal_contract_literal(text) %}}"
            "{{ dex_temporal_contract_backslashed(text) }}"
        ) in asset


D1, D2, D3, D4 = (
    "TIMESTAMP '2026-01-01'",
    "TIMESTAMP '2026-01-02'",
    "TIMESTAMP '2026-01-03'",
    "TIMESTAMP '2026-01-04'",
)


def _select(rows: list[tuple], names: tuple[str, ...], types: tuple[str, ...]) -> str:
    """A SELECT over literal ``rows``, each value cast to its column's type."""

    lines = []
    for index, row in enumerate(rows):
        values = ", ".join(
            f"cast({value} as {kind})" + (f" as {name}" if index == 0 else "")
            for value, name, kind in zip(row, names, types, strict=True)
        )
        lines.append(("select " if index == 0 else "union all select ") + values)
    return "\n".join(lines) + "\n"


VERSION_COLUMNS = ("host_id", "name", "is_super", "source_at", "dbt_valid_from")
VERSION_TYPES = ("integer", "varchar", "boolean", "timestamp", "timestamp")
ANSWER_COLUMNS = ("host_id", "changes", "latest_at", "first_at")
ANSWER_TYPES = ("integer", "integer", "timestamp", "timestamp")


def _compare(tmp_path: Path, versions, answers, history: dict) -> dict[str, tuple]:
    """Build the versions and the model's answers as DuckDB tables, run the
    shipped test for each measure, and run ``maintain verify``'s own statement
    over the same tables: ``{measure: (test failures, verify's count)}``."""

    import subprocess
    import sys

    duckdb = pytest.importorskip("duckdb")
    from exmergo_dex_core.maintain.temporal_contracts import _statement

    project = tmp_path / "proj"
    (project / "models").mkdir(parents=True)
    (project / "macros").mkdir()
    (project / "dbt_project.yml").write_text(
        "name: p\nversion: '1.0'\nprofile: p\nconfig-version: 2\n"
        "models:\n  p:\n    +materialized: table\n",
        encoding="utf-8",
    )
    warehouse = tmp_path / "w.duckdb"
    (project / "profiles.yml").write_text(
        "p:\n  target: dev\n  outputs:\n    dev:\n      type: duckdb\n"
        f"      path: {warehouse}\n      threads: 1\n",
        encoding="utf-8",
    )
    (project / "macros" / f"{NAME}.sql").write_text(_asset(), encoding="utf-8")
    (project / "models" / "snap.sql").write_text(
        _select(versions, VERSION_COLUMNS, VERSION_TYPES), encoding="utf-8"
    )
    (project / "models" / "m.sql").write_text(
        _select(answers, ANSWER_COLUMNS, ANSWER_TYPES), encoding="utf-8"
    )
    parsed = History.model_validate({**history, "snapshot": "snap"})
    reading = gt.HistoryReading(lineage="descends")
    [declaration] = parse_declarations(
        [{"model": "m", "history": parsed.model_dump(mode="json")}]
    )
    instances = [
        i
        for i in next(k for k in gt.kinds() if k.name == NAME).instances(
            declaration, reading
        )
        if i.withheld is None
    ]
    tests = [
        {NAME: {"name": f"dex_{i.variant}", "arguments": dict(i.arguments)}}
        for i in instances
    ]
    (project / "models" / "schema.yml").write_text(
        yaml.safe_dump({"version": 2, "models": [{"name": "m", "data_tests": tests}]}),
        encoding="utf-8",
    )
    dbt = Path(sys.executable).parent / "dbt"
    if not dbt.is_file():
        pytest.skip("needs dbt beside the interpreter")
    args = ["--project-dir", str(project), "--profiles-dir", str(project)]
    done = subprocess.run(  # noqa: S603 - the venv's own dbt, on a fixture project
        [str(dbt), "build", *args], cwd=project, capture_output=True, check=False
    )
    results = json.loads((project / "target" / "run_results.json").read_text())
    failures: dict[str, int] = {}
    for instance in instances:
        [result] = [
            r
            for r in results["results"]
            if r["unique_id"].startswith(f"test.p.dex_{instance.variant}")
        ]
        assert result["status"] in ("pass", "fail"), (result, done.stdout[-2000:])
        failures[str(instance.variant)] = int(result["failures"] or 0)

    measures = parsed.measures
    columns = {
        "transition_count": measures.transition_count,
        "latest_target_transition_at": measures.latest_target_transition_at,
        "first_target_at": measures.first_target_at,
    }
    time = parsed.time.column if parsed.time else "dbt_valid_from"
    sql = _statement(
        parsed,
        relation="main.snap",
        model_relation="main.m",
        snapshot_keys=list(parsed.entity.values()),
        model_keys=list(parsed.entity),
        status=("is_super", "BOOLEAN"),
        order="dbt_valid_from",
        time=time,
        measures={
            name: (column, "TIMESTAMP")
            for name, column in columns.items()
            if column is not None
        },
    )
    conn = duckdb.connect(str(warehouse), read_only=True)
    try:
        cursor = conn.execute(sql)
        row = dict(
            zip([d[0] for d in cursor.description], cursor.fetchone(), strict=True)
        )
    finally:
        conn.close()
    return {
        name: (failures[name], int(row[f"dex_bad_{name}"] or 0)) for name in failures
    }


def _agree(result: dict[str, tuple]) -> None:
    for measure, (test, verify) in result.items():
        assert test == verify, (measure, test, verify)


FIRST_TRUE = [(1, "'ann'", "true", D1, D1)]


def test_an_invented_prior_state_fails_the_count_and_the_latest_time(tmp_path):
    result = _compare(tmp_path, FIRST_TRUE, [(1, 1, D1, D1)], _history())
    assert result == {
        "transition_count": (1, 1),
        "latest_target_transition_at": (1, 1),
        "first_target_at": (0, 0),
    }


def test_the_same_answer_passes_under_the_transition_policy(tmp_path):
    history = _history(initial_observation="transition")
    result = _compare(tmp_path, FIRST_TRUE, [(1, 1, D1, D1)], history)
    assert {m: t for m, (t, _v) in result.items()} == dict.fromkeys(result, 0)
    _agree(result)


def test_many_hosts_with_edits_and_nulls_agree_count_for_count(tmp_path):
    versions = [
        # Host 1: false, true, a name-only edit, false, true: 3 transitions.
        (1, "'a'", "false", D1, D1),
        (1, "'a'", "true", D2, D2),
        (1, "'b'", "true", D3, D3),
        (1, "'b'", "false", "TIMESTAMP '2026-01-05'", "TIMESTAMP '2026-01-05'"),
        (1, "'b'", "true", "TIMESTAMP '2026-01-06'", "TIMESTAMP '2026-01-06'"),
        # Host 2: true, then a NULL status, then true.
        (2, "'c'", "true", D1, D1),
        (2, "'c'", "null", D2, D2),
        (2, "'c'", "true", D3, D3),
        # Host 3: only a NULL status.
        (3, "'d'", "null", D1, D1),
    ]
    answers = [
        (1, 3, "TIMESTAMP '2026-01-06'", D2),  # right
        (2, 2, D3, D1),  # counts the NULL as a state
        (3, 0, "null", "null"),  # right under ignore
    ]
    ignore = _compare(tmp_path / "ignore", versions, answers, _history())
    _agree(ignore)
    assert ignore["transition_count"][0] == 1
    state = _compare(
        tmp_path / "state", versions, answers, _history(null_status="state")
    )
    _agree(state)


def test_versions_sharing_one_validity_time_are_excluded_alike(tmp_path):
    versions = [(1, "'a'", "false", D1, D1), (1, "'a'", "true", D2, D1)]
    result = _compare(tmp_path, versions, [(1, 5, D4, D4)], _history())
    _agree(result)
    assert result["transition_count"] == (0, 0)


def test_a_business_time_running_backwards_is_excluded_alike(tmp_path):
    history = _history(time={"column": "source_at", "provenance": "business"})
    versions = [
        (1, "'a'", "false", D3, D1),
        (1, "'a'", "true", D2, D2),
        (2, "'a'", "false", D1, D1),
        (2, "'a'", "true", D1, D2),  # a frozen business time
    ]
    answers = [(1, 9, D4, D4), (2, 9, D4, D4)]
    result = _compare(tmp_path, versions, answers, history)
    _agree(result)
    # Host 1 is excluded; host 2's count is compared, its times are not.
    assert result["transition_count"] == (1, 1)
    assert result["latest_target_transition_at"] == (0, 0)


def test_a_host_missing_from_the_model_is_no_mismatch(tmp_path):
    versions = [(1, "'a'", "true", D1, D1), (2, "'a'", "true", D1, D1)]
    result = _compare(tmp_path, versions, [(1, 0, "null", D1)], _history())
    _agree(result)
    assert result["transition_count"] == (0, 0)


def test_a_status_compared_as_text_agrees(tmp_path):
    versions = [(1, "'a'", "true", D1, D1), (1, "'a'", "false", D2, D2)]
    history = _history(status={"column": "is_super", "target": "true"})
    result = _compare(tmp_path, versions, [(1, 2, D1, D1)], history)
    _agree(result)


def test_transform_declare_renders_the_tests_a_history_declared_earlier_owes(
    dbt_project_dir: Path, tmp_path: Path, capsys
):
    for edit in _PLAN_EDITS:
        target = dbt_project_dir / edit["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(edit["content"], encoding="utf-8")
    (dbt_project_dir / "models" / f"{MODEL}.yml").write_text(
        yaml.safe_dump(
            {
                "version": 2,
                "models": [
                    {
                        "name": MODEL,
                        "description": "One row per host.",
                        "config": {"meta": {"dex": {"history": _history()}}},
                    }
                ],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    _repo(tmp_path, "true")
    main(["--repo-root", str(tmp_path), "transform", "declare", MODEL])
    declared = json.loads(capsys.readouterr().out)

    assert declared["status"] == "ok", declared["errors"]
    assert declared["data"]["plan_id"] is not None, declared["data"]["skipped"]
    assert f"models/{MODEL}.yml" in declared["data"]["paths"]
    assert f"macros/{NAME}.sql" in declared["data"]["paths"]


def _rerender(
    content: str, history: dict | None, reading=None
) -> tuple[str, list, list]:
    payload: dict = {"model": MODEL}
    if history is not None:
        payload["history"] = history
    [declaration] = parse_declarations([payload])
    generated: list[dict] = []
    notes: list[str] = []
    stated = reading or _descends()
    text = render(
        content,
        declaration,
        render_tests=True,
        generated=generated,
        notes=notes,
        history=lambda _model, _history: stated,
    )
    return text, [g for g in generated if g["test"] == NAME], notes


def test_a_changed_policy_reports_the_tests_it_no_longer_gives_as_stale():
    before = _render(_history())
    _text, rows, notes = _rerender(before, _history(null_status="state"))

    stale = [r for r in rows if r["status"] == "stale"]
    assert len(stale) == 3
    assert {r["reason"] for r in stale} == {"history_changed"}
    assert sorted(r["variant"] for r in stale) == sorted(gt.TEMPORAL_MEASURES)
    assert sorted(r["status"] for r in rows if r["status"] != "stale") == ["added"] * 3
    assert sum(gt.TEMPORAL_CONTRACT in n and "no longer gives" in n for n in notes) == 3


def test_an_unchanged_history_reports_nothing_stale():
    before = _render(_history())
    _text, rows, notes = _rerender(before, _history())
    assert {r["status"] for r in rows} == {"already_listed"}
    assert not any("no longer gives" in n for n in notes)


def test_a_withheld_measure_already_listed_is_not_stale():
    # The claim is the same; what changed is whether verify can measure it.
    before = _render(_history())
    reading = gt.HistoryReading(lineage="not_ancestor")
    _text, rows, _notes = _rerender(before, _history(), reading)
    assert {r["status"] for r in rows} == {"listed_while_off"}


def _config(tmp_path: Path, files: dict[str, str]) -> dict:
    view = _view(tmp_path, files)
    contents = {p: f.content for p, f in view.files.items()}
    return snapshot_config(contents, view, SNAPSHOT)


def _properties(config: dict) -> dict[str, str]:
    return {
        "snapshots/schema.yml": yaml.safe_dump(
            {"version": 2, "snapshots": [{"name": SNAPSHOT, "config": config}]}
        )
    }


def test_a_sql_snapshot_s_yaml_properties_are_read_under_its_config_call(tmp_path):
    files = {**_snap(), **_properties({"hard_deletes": "new_record"})}
    assert _config(tmp_path, files)["hard_deletes"] == "new_record"
    # The SQL block's own config() is the later layer, and wins.
    files = {
        **_snap(", hard_deletes='invalidate'"),
        **_properties({"hard_deletes": "new_record"}),
    }
    assert _config(tmp_path, files)["hard_deletes"] == "invalidate"


def test_a_templated_yaml_value_is_unread(tmp_path):
    files = {**_snap(), **_properties({"hard_deletes": "{{ var('mode') }}"})}
    assert _config(tmp_path, files)["_unread"] == "hard_deletes"
    renamed = {"snapshot_meta_column_names": {"dbt_valid_from": "{{ var('c') }}"}}
    files = {**_snap(), **_properties(renamed)}
    assert _config(tmp_path, files)["_unread"] == "snapshot_meta_column_names"


def test_a_literal_at_a_later_layer_settles_an_unread_one(tmp_path):
    files = {
        **_snap(", hard_deletes='invalidate'"),
        "dbt_project.yml": (
            "name: p\nprofile: p\nsnapshots:\n  p:\n"
            "    +hard_deletes: \"{{ var('mode') }}\"\n"
        ),
    }
    config = _config(tmp_path, files)
    assert config["hard_deletes"] == "invalidate"
    assert "_unread" not in config


def test_every_config_call_in_the_block_is_read_the_later_winning(tmp_path):
    sql = SNAP_SQL.replace("CONFIG", "").replace(
        "{% endsnapshot %}",
        "{{ config(hard_deletes='new_record') }}\n{% endsnapshot %}",
    )
    files = {"snapshots/hosts_snapshot.sql": sql, "models/hosts.sql": "select 1\n"}
    assert _config(tmp_path, files)["hard_deletes"] == "new_record"


def test_the_project_config_is_walked_through_the_sql_file_s_stem(tmp_path):
    sql = SNAP_SQL.replace("CONFIG", "")
    files = {
        "snapshots/legacy.sql": sql,
        "models/hosts.sql": "select 1\n",
        "dbt_project.yml": (
            "name: p\nprofile: p\nsnapshots:\n  p:\n    legacy:\n"
            "      +hard_deletes: new_record\n"
        ),
    }
    assert _config(tmp_path, files)["hard_deletes"] == "new_record"


def test_each_test_is_named_by_its_claim_not_its_arguments():
    for test in _tests(_render(_history())):
        assert test["name"] == f"{NAME}_{test['config']['meta']['dex']['claim']}"
    # A target of 1 and of "1" stringify alike: dbt would give both one name.
    one = _tests(_render(_history(status={"column": "is_super", "target": 1})))
    text = _tests(_render(_history(status={"column": "is_super", "target": "1"})))
    assert {t["name"] for t in one}.isdisjoint({t["name"] for t in text})


def test_a_declared_name_in_another_case_resolves_to_the_column(tmp_path):
    # The macro quotes the physical spelling it finds, as verify does, so a
    # declaration written in another case still reads the column.
    history = _history(
        entity={"HOST_ID": "Host_Id"},
        status={"column": "IS_SUPER", "target": True},
        measures={"transition_count": "Changes"},
    )
    result = _compare(tmp_path, FIRST_TRUE, [(1, 1, D1, D1)], history)
    assert result == {"transition_count": (1, 1)}


def test_a_column_the_relation_lacks_errors_the_test_rather_than_passing(tmp_path):
    # A measure column the model does not have (named NULL, the worst case:
    # written bare it would compare as NULL and pass).
    history = _history(measures={"first_target_at": "null"})
    with pytest.raises(AssertionError, match="error"):
        _compare(tmp_path, FIRST_TRUE, [(1, 0, "NULL", D1)], history)


def test_a_snapshot_s_own_name_is_a_project_config_level(tmp_path):
    files = {
        "snapshots/hosts.yml": yaml.safe_dump(
            {"snapshots": [{"name": SNAPSHOT, "relation": "ref('hosts')"}]}
        ),
        "dbt_project.yml": (
            f"name: p\nprofile: p\nsnapshots:\n  p:\n    {SNAPSHOT}:\n"
            "      +hard_deletes: new_record\n"
        ),
    }
    assert _config(tmp_path, files)["hard_deletes"] == "new_record"


def test_a_config_call_in_a_jinja_comment_is_not_read(tmp_path):
    sql = SNAP_SQL.replace("CONFIG", ", hard_deletes='new_record'").replace(
        "{% endsnapshot %}",
        "{# {{ config(hard_deletes='invalidate') }} #}\n{% endsnapshot %}",
    )
    files = {"snapshots/hosts_snapshot.sql": sql, "models/hosts.sql": "select 1\n"}
    assert _config(tmp_path, files)["hard_deletes"] == "new_record"


def test_a_positional_config_mapping_is_read(tmp_path):
    sql = SNAP_SQL.replace("CONFIG", "").replace(
        "{% endsnapshot %}",
        "{{ config({'hard_deletes': 'new_record'}) }}\n{% endsnapshot %}",
    )
    files = {"snapshots/hosts_snapshot.sql": sql, "models/hosts.sql": "select 1\n"}
    assert _config(tmp_path, files)["hard_deletes"] == "new_record"
    unread = SNAP_SQL.replace("CONFIG", "").replace(
        "{% endsnapshot %}", "{{ config(var('cfg')) }}\n{% endsnapshot %}"
    )
    files["snapshots/hosts_snapshot.sql"] = unread
    assert "_unread" in _config(tmp_path, files)
