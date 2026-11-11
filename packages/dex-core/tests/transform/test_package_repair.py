"""The pieces `transform repair` drafts an override from (#1229), without dbt."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
import yaml

from exmergo_dex_core.explore.profile import _epoch_note, _temporal_note, content_shape
from exmergo_dex_core.transform import package_repair as repair
from exmergo_dex_core.transform.completion import _repair_obligations
from exmergo_dex_core.transform.evidence import BuildEvidence, BuildNode
from exmergo_dex_core.transform.prerequisites import diagnose
from exmergo_dex_core.transform.refactor_compare import BlockedInput, repair_first

# --- reading the error -----------------------------------------------------------


@pytest.mark.parametrize(
    ("message", "function", "offending", "target"),
    [
        (
            "Binder Error: Cannot mix values of type TIMESTAMP and INTEGER in "
            "COALESCE operator - an explicit cast is required",
            "coalesce",
            "integer",
            "timestamp",
        ),
        (
            "Binder Error: No function matches the given name and argument types "
            "'date_trunc(STRING_LITERAL, INTEGER)'. You might need to add explicit "
            "type casts.",
            "date_trunc",
            "integer",
            "timestamp",
        ),
        (
            "No matching signature for function COALESCE for argument types: "
            "TIMESTAMP, INT64. Supported signature: COALESCE([ANY, ...]) at [5:5]",
            "coalesce",
            "integer",
            "timestamp",
        ),
        (
            "No matching signature for function TIMESTAMP_TRUNC for argument "
            "types: INT64, DATE_TIME_PART. Supported signatures: ...",
            "date_trunc",
            "integer",
            "timestamp",
        ),
        (
            "Invalid cast from INT64 to TIMESTAMP at [7:10]",
            "cast",
            "integer",
            "timestamp",
        ),
        (
            "Database Error in model stg (models/stg.sql)\n  No matching signature "
            "for function COALESCE\n    Argument types: TIMESTAMP, INT64\n    "
            "Signature: COALESCE([T1, ...])",
            "coalesce",
            "integer",
            "timestamp",
        ),
        (
            "Cannot mix values of type DATE and VARCHAR in COALESCE",
            "coalesce",
            "string",
            "date",
        ),
    ],
)
def test_the_typing_errors_dex_reads(message, function, offending, target):
    """Each DuckDB and BigQuery typing error names its function and types."""

    failure = repair.parse_type_failure(message)
    assert failure is not None
    assert (failure.function, failure.offending, failure.target) == (
        function,
        offending,
        target,
    )


@pytest.mark.parametrize(
    "message",
    [
        None,
        "",
        "Catalog Error: Table with name task does not exist!",
        "Conversion Error: Could not convert string 'x' to INT32",
        "No matching signature for function SUBSTR for argument types: INT64",
    ],
)
def test_anything_else_is_not_read(message):
    """A message that is not a typing error Dex converts reads as nothing."""

    assert repair.parse_type_failure(message) is None


def test_a_coalesce_of_two_non_temporal_types_has_no_target():
    """A coalesce of two non-temporal types has no target to convert to."""

    failure = repair.parse_type_failure(
        "Cannot mix values of type INTEGER and VARCHAR in COALESCE"
    )
    assert failure is not None and failure.target is None


@pytest.mark.parametrize(
    ("data_type", "expected"),
    [
        ("INTEGER", "integer"),
        ("BIGINT", "integer"),
        ("INT64", "integer"),
        ("VARCHAR", "string"),
        ("STRING", "string"),
        ("TIMESTAMP WITH TIME ZONE", "timestamp"),
        ("DATETIME", "timestamp"),
        ("DATE", "date"),
        ("INTERVAL", None),
        ("STRING_LITERAL", None),
        ("DOUBLE", None),
        (None, None),
    ],
)
def test_type_families(data_type, expected):
    """Warehouse types fold into the families conversions are chosen by."""

    assert repair.family(data_type) == expected


# --- conversions -----------------------------------------------------------------


def test_each_connector_spells_its_conversions_without_a_quote():
    """Every spelling can sit inside a quoted macro argument."""

    for connector in ("duckdb", "bigquery", "snowflake"):
        for kind in ("null_cast", "epoch_seconds", "epoch_millis", "iso_parse"):
            for target in ("timestamp", "date"):
                sql = repair.conversion_sql(kind, target, connector, "c")
                assert sql is not None and "'" not in sql and '"' not in sql
                assert "c" in sql
    assert repair.conversion_sql("null_cast", "timestamp", "postgres", "c") is None
    assert (
        repair.conversion_sql("epoch_seconds", "date", "bigquery", "c")
        == "cast(timestamp_seconds(cast(c as int64)) as date)"
    )


def _profile(name="legacy_ts", data_type="INTEGER", null_fraction=1.0, pii=None):
    """A cached column profile with only the fields settling reads."""

    return SimpleNamespace(
        name=name, data_type=data_type, null_fraction=null_fraction, pii=pii
    )


def _dataset(columns, notes=(), rows=3):
    """A cached dataset holding ``columns`` and the profile's ``notes``."""

    return SimpleNamespace(
        identifier="dev.main.raw_task",
        row_count=rows,
        profiled_at="2026-11-10T00:00:00+00:00",
        columns=list(columns),
        data_quality=list(notes),
    )


def _coalesce():
    """The failure DuckDB reports for a TIMESTAMP coalesced with an INTEGER."""

    return repair.TypeFailure(
        "coalesce", ("TIMESTAMP", "INTEGER"), "integer", "timestamp"
    )


def test_an_all_null_column_is_a_plain_cast_that_changes_no_value():
    """All NULL settles a cast through text, which changes no value."""

    conversion = repair.Conversion(column="legacy_ts")
    profile = _profile()
    repair.settle(conversion, profile, _dataset([profile]), _coalesce(), "duckdb")
    assert conversion.status == "resolved"
    assert conversion.kind == "null_cast"
    assert conversion.meaning_changing is False
    assert conversion.evidence == "data"
    assert conversion.expression == "cast(cast({c} as varchar) as timestamp)"


def test_epoch_seconds_are_read_as_such_and_flagged_as_meaning_changing():
    """Epoch seconds are read as such, and flagged as changing the value's meaning."""

    profile = _profile(name="due_epoch", null_fraction=0.0)
    note = (
        "due_epoch is declared INTEGER but 100% of values fall in the plausible "
        "unix-epoch-seconds range"
    )
    conversion = repair.Conversion(column="due_epoch")
    repair.settle(
        conversion, profile, _dataset([profile], [note]), _coalesce(), "bigquery"
    )
    assert conversion.kind == "epoch_seconds"
    assert conversion.meaning_changing is True
    assert conversion.expression == "timestamp_seconds(cast({c} as int64))"


def test_iso_text_is_parsed_when_the_error_names_a_string():
    """ISO text is parsed when the error names a string column."""

    profile = _profile(name="due_on", data_type="VARCHAR", null_fraction=0.1)
    note = (
        "due_on is declared VARCHAR but 99% of values match the %Y-%m-%d date "
        "format; consider casting with that format"
    )
    failure = repair.TypeFailure(
        "coalesce", ("TIMESTAMP", "VARCHAR"), "string", "timestamp"
    )
    conversion = repair.Conversion(column="due_on")
    repair.settle(conversion, profile, _dataset([profile], [note]), failure, "duckdb")
    assert (conversion.kind, conversion.meaning_changing) == ("iso_parse", False)


@pytest.mark.parametrize(
    ("profile", "notes", "reason"),
    [
        # Some values, no shape: never guessed.
        (_profile(null_fraction=0.5), [], "ambiguous_content"),
        # An ISO note does not settle an integer.
        (
            _profile(null_fraction=0.5),
            [
                "legacy_ts is declared INTEGER but 99% of values match the "
                "%Y-%m-%d date format"
            ],
            "ambiguous_content",
        ),
        (_profile(null_fraction=0.5, pii=object()), [], "pii_flagged"),
        (_profile(data_type="TIMESTAMP", null_fraction=0.5), [], "stale_profile"),
    ],
)
def test_what_the_evidence_does_not_settle_stays_unresolved(profile, notes, reason):
    """Evidence that settles nothing leaves the column unresolved, never guessed."""

    conversion = repair.Conversion(column="legacy_ts")
    repair.settle(
        conversion, profile, _dataset([profile], notes), _coalesce(), "duckdb"
    )
    assert conversion.status == "unresolved"
    assert conversion.reason_code == reason
    assert conversion.expression is None


def test_an_unreviewed_connector_is_unresolved_rather_than_guessed():
    """A connector with no reviewed spelling is unresolved."""

    conversion = repair.Conversion(column="legacy_ts")
    profile = _profile()
    repair.settle(conversion, profile, _dataset([profile]), _coalesce(), "databricks")
    assert conversion.reason_code == "unsupported_on_connector"


def test_content_shape_reads_back_what_the_profile_writes():
    """The parser and the note writers stay in step."""

    agg = SimpleNamespace(
        epoch_seconds_fraction=1.0,
        epoch_millis_fraction=0.0,
        epoch_seconds_min_value=1,
        epoch_seconds_max_value=2,
        epoch_millis_min_value=None,
        epoch_millis_max_value=None,
    )
    assert content_shape([_epoch_note("d", "INTEGER", agg)], "d") == "epoch_seconds"
    agg.epoch_seconds_fraction, agg.epoch_millis_fraction = 0.0, 1.0
    agg.epoch_millis_min_value, agg.epoch_millis_max_value = 1, 2
    assert content_shape([_epoch_note("d", "BIGINT", agg)], "d") == "epoch_millis"
    for fmt, shape in (("%Y-%m-%d", "iso_date"), ("%Y-%m-%d %H:%M:%S", "iso_datetime")):
        assert content_shape([_temporal_note("d", "VARCHAR", 0.99, fmt)], "d") == shape
    # Another column's note says nothing about this one.
    assert (
        content_shape([_temporal_note("dd", "VARCHAR", 0.99, "%Y-%m-%d")], "d") is None
    )


# --- rewriting -------------------------------------------------------------------

SQL = """\
-- coalesce(legacy_ts) in a comment is not a site
select
    id,
    legacy_ts as legacy_raw,
    coalesce(base.created_at, base.legacy_ts) as created_ts,
    cast({{ dbt.date_trunc('day', 'due_epoch') }} as date) as due_date
from base
"""


def test_call_sites_include_macro_arguments_and_skip_comments():
    """Call sites include macro arguments and skip comments."""

    assert repair.columns_in_sites(SQL, "coalesce") == ["created_at", "legacy_ts"]
    assert "due_epoch" in repair.columns_in_sites(SQL, "date_trunc")


def test_conversions_land_only_inside_the_failing_calls():
    """A conversion is written only inside the calls the error names."""

    legacy = repair.Conversion(
        column="legacy_ts", status="resolved", expression="cast({c} as timestamp)"
    )
    out = repair.apply_conversions(SQL, "coalesce", [legacy])
    assert "coalesce(base.created_at, cast(base.legacy_ts as timestamp))" in out
    # The pass-through select and the comment are untouched.
    assert "    legacy_ts as legacy_raw," in out
    assert "-- coalesce(legacy_ts) in a comment" in out
    assert legacy.sites == 1
    assert legacy.expression == "cast(legacy_ts as timestamp)"

    epoch = repair.Conversion(
        column="due_epoch", status="resolved", expression="epoch_ms({c} * 1000)"
    )
    out = repair.apply_conversions(SQL, "date_trunc", [epoch])
    assert "dbt.date_trunc('day', 'epoch_ms(due_epoch * 1000)')" in out


def test_a_conversion_no_site_reads_is_unresolved():
    """A conversion no call site reads becomes unresolved and changes nothing."""

    missing = repair.Conversion(column="nowhere", status="resolved", expression="{c}")
    assert repair.apply_conversions(SQL, "coalesce", [missing]) == SQL
    assert (missing.status, missing.reason_code) == ("unresolved", "site_not_found")


def test_only_the_packages_own_unqualified_macros_are_qualified():
    """Only the package's own macros, called unqualified, are qualified."""

    sql = (
        "select {{ task_columns() }}, {{ tiny_pkg.other() }}, "
        "{{ fivetran_utils.fill() }}, {{ dbt.type_string() }} from x"
    )
    out, changed = repair.qualify_macros(sql, "tiny_pkg", {"task_columns", "other"})
    assert changed == ["task_columns"]
    assert "{{ tiny_pkg.task_columns() }}" in out
    assert "{{ tiny_pkg.other() }}" in out
    assert "{{ fivetran_utils.fill() }}" in out


def _scope(root=None, package_vars=None):
    """The vars each side sees, for a package ``pkg`` in a project ``proj``."""

    return repair.VarScope(
        root_project="proj",
        package="pkg",
        root=root or {},
        package_vars=package_vars or {},
        scopes={"proj", "pkg", "other_pkg"},
    )


def test_a_relation_var_that_would_cycle_is_inlined():
    """A relation var the project points at the overridden model is inlined."""

    scope = _scope(
        root={"proj": {"task": "{{ ref('stg_task') }}"}},
        package_vars={"pkg": {"task": "{{ source('raw', 'task') }}"}},
    )
    out, changed, defaults, warnings = repair.resolve_vars(
        "select * from {{ var('task') }}", scope
    )
    assert out == "select * from {{ source('raw', 'task') }}"
    assert changed == [{"var": "task", "change": "inlined"}]
    assert defaults == {} and warnings == []


def test_a_value_the_project_lacks_becomes_the_reads_default():
    """A value only the package defines becomes the read's default."""

    scope = _scope(package_vars={"using_x": True, "pkg": {"cols": ["a"]}})
    out, changed, _defaults, _warnings = repair.resolve_vars(
        "{{ config(enabled=var('using_x')) }} {% for c in var('cols') %}{% endfor %}",
        scope,
    )
    assert 'var("using_x", true)' in out
    assert 'var("cols", ["a"])' in out
    assert {c["change"] for c in changed} == {"defaulted"}


def test_a_value_the_project_sets_differently_becomes_the_packages_literal():
    """A value the project sets differently is written as the package's literal."""

    scope = _scope(root={"limit_n": 5, "pkg": {"limit_n": 10}})
    out, changed, _d, _w = repair.resolve_vars("select {{ var('limit_n') }}", scope)
    # Only the package's scoped value differs from what the project sees.
    assert out == "select {{ 10 }}"
    assert changed == [{"var": "limit_n", "change": "literal"}]


def test_a_var_seen_the_same_way_on_both_sides_is_left_alone():
    """A var both sides read the same way is left alone."""

    scope = _scope(root={"shared": 1})
    sql = "select {{ var('shared') }}"
    assert repair.resolve_vars(sql, scope)[0] == sql


def test_a_var_a_macro_reads_by_name_is_set_for_the_project():
    """A var a macro reads by name is set under the project's vars."""

    scope = _scope(package_vars={"pkg": {"task_pass_through_columns": []}})
    out, changed, defaults, _w = repair.resolve_vars(
        "select {{ fivetran_utils.fill_pass_through_columns("
        "'task_pass_through_columns') }}",
        scope,
    )
    assert defaults == {"task_pass_through_columns": []}
    assert changed == [
        {"var": "task_pass_through_columns", "change": "project_default"}
    ]
    assert "fill_pass_through_columns('task_pass_through_columns')" in out


def test_the_packages_resolved_config_is_restated_unless_the_sql_sets_it():
    """The package's resolved config is restated unless its SQL sets it."""

    sql, keys = repair.restate_config(
        "select 1", {"materialized": "table", "schema": "stg", "alias": None}
    )
    assert keys == ["materialized", "schema"]
    assert sql.startswith('{{ config(materialized="table", schema="stg") }}\n')
    sql, keys = repair.restate_config(
        "{{ config(materialized='view') }}\nselect 1", {"materialized": "table"}
    )
    assert keys == []


# --- dbt_project.yml in place ------------------------------------------------------

PROJECT = """\
name: proj  # the project
version: '1.0'

# packages we override
models:
  proj:
    +materialized: view
  other_pkg:
    +schema: other

seeds:
  proj: {}
"""


def test_set_nested_inserts_under_the_existing_block_and_keeps_comments():
    """A nested key joins its existing block, and comments survive."""

    out = repair.set_nested(PROJECT, ["models", "pkg", "stg", "+enabled"], False)
    assert "# packages we override" in out and "name: proj  # the project" in out
    loaded = yaml.safe_load(out)
    assert loaded["models"]["pkg"]["stg"]["+enabled"] is False
    assert loaded["models"]["other_pkg"] == {"+schema": "other"}
    assert loaded["seeds"] == {"proj": {}}
    assert out.index("  pkg:") < out.index("seeds:")
    # Idempotent.
    assert repair.set_nested(out, ["models", "pkg", "stg", "+enabled"], False) == out


def test_set_nested_adds_a_missing_top_level_block_and_fills_an_empty_one():
    """A missing block is added, and an empty one is filled."""

    out = repair.set_nested("name: proj\n", ["vars", "proj", "cols"], [])
    assert yaml.safe_load(out) == {"name": "proj", "vars": {"proj": {"cols": []}}}
    out = repair.set_nested(
        "name: proj\nmodels:\nseeds: {}\n", ["models", "p", "+enabled"], False
    )
    assert yaml.safe_load(out)["models"] == {"p": {"+enabled": False}}


def test_set_nested_replaces_a_scalar_leaf():
    """An existing scalar is replaced in place, its comment kept."""

    out = repair.set_nested(
        "models:\n  pkg:\n    stg:\n      +enabled: true  # on\n",
        ["models", "pkg", "stg", "+enabled"],
        False,
    )
    assert out == "models:\n  pkg:\n    stg:\n      +enabled: false  # on\n"


def test_set_nested_refuses_a_flow_mapping_it_would_reformat():
    """A flow mapping on the path is refused rather than reformatted."""

    with pytest.raises(repair.RepairRefusedError, match="flow mapping"):
        repair.set_nested("models: {pkg: {}}\n", ["models", "pkg", "+enabled"], False)


# --- the bind check -----------------------------------------------------------------


class _Adapter:
    def __init__(self, error=None):
        """A fake adapter raising ``error`` from every statement, if one is given."""

        self.error = error
        self.sql: list[str] = []

    def run_query(self, sql, *, max_rows, timeout_seconds):
        """Record the statement and raise the configured error."""

        self.sql.append(sql)
        if self.error:
            raise RuntimeError(self.error)

    def query_estimate(self, sql):
        """Record the statement, raise any configured error, and price it at zero."""

        self.sql.append(sql)
        if self.error:
            raise RuntimeError(self.error)
        return 0.0


def test_duckdb_binds_the_draft_returning_no_row():
    """DuckDB binds the draft with ``limit 0``."""

    adapter = _Adapter()
    assert repair.bind_check(adapter, "duckdb", "select 1;") == ("bound", None, None)
    assert adapter.sql[0].endswith("as dex_bind_check limit 0")


def test_bigquery_dry_runs_the_draft_and_a_failure_is_masked():
    """BigQuery dry-runs the draft, and a refusal's values are masked."""

    adapter = _Adapter(error="Could not cast literal 'secret-value' to type DATE")
    status, detail, text = repair.bind_check(adapter, "bigquery", "select 1")
    assert status == "failed"
    assert "secret-value" not in detail and "secret-value" not in text
    assert adapter.sql == ["select 1"]


def test_elsewhere_no_bind_check_runs():
    """A connector with no free, row-free check runs none."""

    adapter = _Adapter()
    status, reason, _text = repair.bind_check(adapter, "postgres", "select 1")
    assert status == "not_checked" and "postgres" in reason
    assert adapter.sql == []


# --- pointers that survive a cut -----------------------------------------------------


def _manifest(**config):
    """A manifest with one package model and the project model reading it."""

    return {
        "metadata": {"project_name": "proj"},
        "nodes": {
            "model.pkg.stg_task": {
                "name": "stg_task",
                "resource_type": "model",
                "package_name": "pkg",
                "original_file_path": "models/stg_task.sql",
                "depends_on": {"nodes": []},
                **config,
            },
            "model.proj.mine": {
                "name": "mine",
                "resource_type": "model",
                "package_name": "proj",
                "original_file_path": "models/mine.sql",
                "depends_on": {"nodes": ["model.pkg.stg_task"]},
            },
        },
    }


def test_a_package_failure_names_the_repair_command():
    """A failed package model names `dex transform repair` in `next`."""

    [entry] = diagnose([("model.pkg.stg_task", "error", "boom")], _manifest())
    assert entry["next"] == "repair: dex transform repair stg_task"
    [override] = [o for o in entry["options"] if o["kind"] == "project_override"]
    assert override["repair"] == "dex transform repair stg_task"


def test_a_package_model_that_cannot_be_overridden_names_no_repair():
    """A package model whose override route is refused names no repair."""

    [entry] = diagnose(
        [("model.pkg.stg_task", "error", "boom")], _manifest(language="python")
    )
    assert "next" not in entry


def test_a_project_failure_names_its_file():
    """A failed project model points at `transform plan` on its file."""

    [entry] = diagnose([("model.proj.mine", "error", "boom")], _manifest())
    assert entry["next"] == "repair: transform plan on models/mine.sql"


def test_the_refactor_offer_names_the_repair_command_not_a_key_plans_lack():
    """The refactor offer names the command, not a key plans never emit."""

    text = repair_first(
        [
            BlockedInput("stg_task", "error", "pkg"),
            BlockedInput("int_x", "skipped", "pkg"),
        ]
    )
    assert "`dex transform repair stg_task`" in text
    assert "dex transform repair int_x" not in text
    text = repair_first(
        [
            BlockedInput("stg_task", "error", "pkg"),
            BlockedInput("stg_note", "error", "pkg"),
        ]
    )
    assert "`dex transform repair stg_note stg_task` drafts" in text
    assert "data.prerequisites" not in text


# --- the build's obligations ----------------------------------------------------------


def _evidence(statuses: dict[str, str]) -> BuildEvidence:
    """Build evidence holding one node per ``statuses`` entry."""

    return BuildEvidence.model_construct(
        nodes=[
            BuildNode(unique_id=uid, name=uid.split(".")[-1], status=status)
            for uid, status in statuses.items()
        ]
    )


RECORD = {
    "node": "stg_task",
    "package": "pkg",
    "status": "resolved",
    "population": "kept",
}
GRAPH = {"test.proj.unique_stg_task_id.abc": ["model.proj.stg_task"]}


def _kinds(obligations):
    """Each obligation's kind, mapped to its status and reason or finding."""

    return {o.kind: (o.status, o.reason_code or o.finding) for o in obligations}


def test_a_cold_verified_repair_satisfies_every_obligation():
    """A cold build that built and tested the override satisfies every check."""

    evidence = _evidence(
        {"model.proj.stg_task": "success", "test.proj.unique_stg_task_id.abc": "pass"}
    )
    cold = {"reinstalled": True, "installed_edits": {"changed": {"count": 0}}}
    out = _repair_obligations(
        RECORD, evidence, GRAPH, {"ran": True, "findings": []}, cold
    )
    assert _kinds(out) == {
        "repair_built": ("satisfied", None),
        "repair_tests": ("satisfied", None),
        "repair_cold_deps": ("satisfied", None),
        "repair_row_count": ("satisfied", None),
    }


def test_a_failed_test_a_lost_edit_and_a_row_finding_contradict_it():
    """A failed test, a lost installed edit and a row finding each contradict it."""

    evidence = _evidence(
        {"model.proj.stg_task": "success", "test.proj.unique_stg_task_id.abc": "fail"}
    )
    cold = {"reinstalled": True, "installed_edits": {"changed": {"count": 2}}}
    verification = {
        "ran": True,
        "findings": [{"code": "row_loss", "identifier": "stg_task"}],
    }
    out = _repair_obligations(RECORD, evidence, GRAPH, verification, cold)
    assert _kinds(out)["repair_tests"] == ("contradicted", "package_test_failed")
    assert _kinds(out)["repair_cold_deps"] == ("contradicted", "installed_edits")
    assert _kinds(out)["repair_row_count"] == ("contradicted", "row_loss")


def test_an_unbuilt_unresolved_repair_stays_open():
    """An unbuilt, unresolved repair leaves every check open."""

    evidence = _evidence({"model.pkg.stg_task": "error"})
    out = _repair_obligations(
        {**RECORD, "status": "unresolved"}, evidence, GRAPH, None, None
    )
    assert _kinds(out) == {
        "repair_built": ("unchecked", "not_built"),
        "repair_tests": ("unchecked", "no_tests"),
        "repair_cold_deps": ("unchecked", "not_cold"),
        "repair_row_count": ("unchecked", "verification_not_run"),
        "repair_conversion": ("unchecked", "unresolved"),
    }


def test_no_repair_record_adds_no_obligation():
    """A plan with no repair record adds no repair obligation."""

    assert _repair_obligations(None, _evidence({}), {}, None, None) == []


def test_a_var_read_twice_is_reported_once():
    """A var read twice is reported once."""

    scope = _scope(package_vars={"pkg": {"cols": []}})
    _out, changed, _d, _w = repair.resolve_vars(
        "{% if var('cols') %}{{ var('cols') | join(', ') }}{% endif %}", scope
    )
    assert changed == [{"var": "cols", "change": "defaulted"}]


def test_the_plan_hint_names_the_command_that_drafts_a_blocked_repair():
    """The plan hint names one repair call covering every failed package model."""

    from exmergo_dex_core.transform.native_semantic import plan_hint
    from exmergo_dex_core.transform.results import PlanResult

    result = PlanResult(plan_id="p1")
    result.refactor_comparison_offer = {
        "per_model": [
            {
                "command": "transform compare p1 --model m",
                "blocked_inputs": [
                    {
                        "model": "stg_task",
                        "status": "error",
                        "owner": "package",
                        "package": "pkg",
                        "repair": "dex transform repair stg_task",
                    },
                    {"model": "int_x", "status": "skipped", "owner": "project"},
                ],
            }
        ]
    }
    step = plan_hint(result)["next"]
    assert "(`dex transform repair stg_task` drafts it)" in step
    assert step.startswith("the baseline does not build (int_x, stg_task)")

    # Two failed package models: one call, since the overrides share
    # dbt_project.yml and separate plans would conflict on apply.
    result.refactor_comparison_offer["per_model"][0]["blocked_inputs"].append(
        {
            "model": "stg_note",
            "status": "error",
            "owner": "package",
            "package": "pkg",
            "repair": "dex transform repair stg_note",
        }
    )
    step = plan_hint(result)["next"]
    assert "(`dex transform repair stg_note stg_task` drafts it)" in step


def test_a_column_list_macro_inside_a_cte_passes_the_static_check():
    """A Fivetran staging model with no control flow plans like any other."""

    from exmergo_dex_core.transform.validate import _assert_query_only, strip_jinja

    sql = (
        "with base as (select * from {{ ref('tmp') }}),\n"
        "fields as (\n"
        "    select\n"
        "        /* the macro lists the columns */\n"
        "        {{ fivetran_utils.fill_staging_columns(source_columns=x) }}\n"
        "\n"
        "        {{ fivetran_utils.source_relation() }}\n"
        "    from base\n"
        ")\n"
        "select * from fields\n"
    )
    assert _assert_query_only("m.sql", sql, "model") == []
    # A macro rendering a whole query in parentheses is still a subquery.
    assert "select __dex_jinja__" in strip_jinja("select * from (\n  {{ q() }}\n) t")


def test_a_plan_repairing_several_models_holds_each_to_its_obligations():
    """Several overrides in one plan are each held to their checks."""

    evidence = _evidence(
        {"model.proj.stg_task": "success", "model.proj.stg_note": "error"}
    )
    summary = {
        "status": "resolved",
        "nodes": [RECORD, {**RECORD, "node": "stg_note"}],
    }
    cold = {"reinstalled": True, "installed_edits": {}}
    graph = {
        "test.proj.unique_stg_task_id.abc": ["model.proj.stg_task"],
        "test.proj.unique_stg_note_id.abc": ["model.proj.stg_note"],
    }
    evidence.nodes.append(
        BuildNode(unique_id="test.proj.unique_stg_task_id.abc", name="t", status="pass")
    )
    out = _repair_obligations(summary, evidence, graph, {"ran": True}, cold)
    # A model whose every check held is one entry; an open one lists what is open.
    assert [(o.model, o.kind, o.status) for o in out] == [
        ("stg_task", "repair", "satisfied"),
        ("stg_note", "repair_built", "contradicted"),
        ("stg_note", "repair_tests", "unchecked"),
        ("stg_note", "repair_row_count", "unchecked"),
    ]


def test_a_one_line_refusal_is_read_through_its_cause():
    """The adapter keeps the first line; the types are on the cause."""

    class _Refusing(_Adapter):
        def query_estimate(self, sql):
            """Refuse with a one-line error whose cause carries the argument types."""

            try:
                raise RuntimeError(
                    "No matching signature for function TIMESTAMP_TRUNC\n"
                    "  Argument types: INT64, DATE_TIME_PART"
                )
            except RuntimeError as exc:
                raise ValueError(
                    "No matching signature for function TIMESTAMP_TRUNC"
                ) from exc

    status, detail, text = repair.bind_check(_Refusing(), "bigquery", "select 1")
    assert status == "failed" and "Argument types" not in detail
    failure = repair.parse_type_failure(text)
    assert failure is not None and failure.function == "date_trunc"


def test_a_later_pass_never_reads_a_written_conversion_as_a_new_site():
    """BigQuery's second failure is a cast; the first pass inserted one."""

    profile_null = _profile(name="legacy_ts")
    profile_epoch = _profile(name="due_epoch", null_fraction=0.0)
    note = (
        "due_epoch is declared INTEGER but 100% of values fall in the plausible "
        "unix-epoch-seconds range"
    )
    dataset = _dataset([profile_null, profile_epoch], [note])
    drafted = repair.Draft(
        name="stg",
        package="pkg",
        unique_id="model.pkg.stg",
        override_path="models/overrides/stg.sql",
        body=(
            "select coalesce(created_at, legacy_ts) as created_ts,\n"
            "  cast({{ dbt.date_trunc('day', 'due_epoch') }} as date) as due_date\n"
            "from base\n"
        ),
        project_yml="",
        disabled_at="models.pkg.stg.+enabled",
        restated={},
    )
    manifest = {
        "nodes": {
            "model.pkg.stg": {"depends_on": {"nodes": ["source.pkg.v.t"]}},
        },
        "sources": {"source.pkg.v.t": {"relation_name": "dev.main.raw_task"}},
    }
    cache = SimpleNamespace(datasets=[dataset])
    kwargs = {"manifest": manifest, "cache": cache, "connector": "bigquery"}
    assert repair.add_failure(
        drafted,
        "No matching signature for function COALESCE\n"
        "  Argument types: TIMESTAMP, INT64",
        source="build",
        **kwargs,
    )
    assert repair.add_failure(
        drafted, "Invalid cast from INT64 to TIMESTAMP", source="bind_check", **kwargs
    )
    assert [(c.column, c.call) for c in drafted.conversions] == [
        ("legacy_ts", "coalesce"),
        ("due_epoch", "cast"),
    ]
    assert drafted.body.count("legacy_ts") == 1
    assert "cast(cast(legacy_ts as string) as timestamp)" in drafted.body
    assert "timestamp_seconds(cast(due_epoch as int64))" in drafted.body


def _converted_draft(bind: dict) -> repair.Draft:
    """A draft whose one failure was converted, with ``bind`` as its bind check."""

    drafted = repair.Draft(
        name="stg",
        package="pkg",
        unique_id="model.pkg.stg",
        override_path="models/overrides/stg.sql",
        body="select 1",
        project_yml="",
        disabled_at="models.pkg.stg.+enabled",
        restated={},
        errors=[{"recognized": True, "source": "build"}],
        conversions=[
            repair.Conversion(column="c", status="resolved", kind="null_cast")
        ],
    )
    drafted.bind_check = bind
    return drafted


@pytest.mark.parametrize(
    ("bind", "status"),
    [
        ({"status": "bound"}, "resolved"),
        ({"status": "not_checked", "reason": "dbt is not installed"}, "unbound"),
        ({"status": "not_run"}, "unbound"),
        ({"status": "failed", "detail": "x"}, "unresolved"),
    ],
)
def test_a_node_is_resolved_only_when_its_draft_binds(bind, status):
    """A node is resolved only when its draft binds on dev."""

    assert repair.finish(_converted_draft(bind))["status"] == status


def test_an_unbound_draft_is_checked_by_the_build_that_builds_it():
    """An unbound draft's conversion stays open until the override builds."""

    record = {**RECORD, "status": "unbound"}
    built = _evidence({"model.proj.stg_task": "success"})
    failed = _evidence({"model.proj.stg_task": "error"})
    kinds = lambda e: _kinds(_repair_obligations(record, e, {}, None, None))  # noqa: E731
    assert "repair_conversion" not in kinds(built)
    assert kinds(failed)["repair_conversion"] == ("unchecked", "unbound")
