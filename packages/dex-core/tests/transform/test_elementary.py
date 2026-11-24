"""What an installed Elementary package does for a target (#1305), offline.

Each case builds the few manifest entries the resolver reads, in the shape dbt
1.12.5 writes them (read from the fixture's variants), so nothing here runs dbt.
The fixture's real manifests are read in ``test_elementary_fixture.py``.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

from exmergo_dex_core.transform.elementary import (
    OVERRIDE_FLAG,
    UPLOAD_VARS,
    is_no_results_query,
    monitoring_tests,
    package_model_dependents,
    read,
    unchecked_tests,
)
from exmergo_dex_core.transform.elementary import resolve as _resolve

from .elementary_fixture import (
    CURRENT_VERSION,
    MATRIX,
    POSTGRES_VERSIONS,
    RELEASES,
    VERSIONS,
)

ROOT = "shop"
ELEMENTARY_MAT = "macro.elementary.materialization_test_default"


def resolve(built: dict[str, Any], text: str | None, **kwargs: Any):
    """The resolver at a release the fixture runs, unless a case names another.

    What a release Dex was not run against does to the record is its own
    section below (#1445); every other case is about what it sets itself.
    """

    kwargs.setdefault("release", CURRENT_VERSION)
    return _resolve(built, text, **kwargs)


def _macro(package: str, name: str, *, depends: tuple[str, ...] = (), sql: str = ""):
    return {
        "unique_id": f"macro.{package}.{name}",
        "name": name,
        "package_name": package,
        "macro_sql": sql,
        "depends_on": {"macros": list(depends)},
    }


def _node(package: str, resource_type: str, name: str) -> dict[str, Any]:
    return {
        "unique_id": f"{resource_type}.{package}.{name}",
        "name": name,
        "package_name": package,
        "resource_type": resource_type,
    }


def manifest(
    *,
    adapter: str = "duckdb",
    models: str | None = "enabled",
    hooks: str | None = "enabled",
    hooks_check_enabled: bool = True,
    extra_macros: tuple[dict[str, Any], ...] = (),
    elementary: bool = True,
    upload_vars: tuple[str, ...] = tuple(UPLOAD_VARS.values()),
) -> dict[str, Any]:
    """A manifest for project ``shop`` with the package's pieces as asked.

    ``models`` and ``hooks`` are ``enabled``, ``disabled`` or ``None`` for none.
    dbt's own and the package's ``test`` materializations are always there, as
    they are in every manifest of a project that installs the package.
    ``upload_vars`` are the ``disable_*`` vars the package's ``on_run_end`` reads.
    """

    macros = [
        _macro("dbt", "materialization_test_default"),
        _macro("dbt", "statement"),
        _macro(f"dbt_{adapter}", f"{adapter}__get_columns"),
    ]
    nodes = [_node(ROOT, "model", "fct")]
    disabled: dict[str, list[dict[str, Any]]] = {}
    if elementary:
        check = "{% if not elementary.is_elementary_enabled() %}{% endif %}"
        reads = "".join(f"{{{{ get_config_var('{v}') }}}}" for v in upload_vars)
        macros += [
            _macro(
                "elementary",
                "materialization_test_default",
                depends=("macro.elementary.materialize_test",),
            ),
            _macro("elementary", "materialization_test_snowflake"),
            _macro(
                "elementary", "on_run_start", sql=check if hooks_check_enabled else ""
            ),
            _macro(
                "elementary",
                "on_run_end",
                sql=(check if hooks_check_enabled else "") + reads,
            ),
        ]
        package_nodes = []
        if models:
            package_nodes.append(("model", "dbt_invocations", models))
        if hooks:
            package_nodes.extend(
                ("operation", name, hooks)
                for name in ("elementary-on-run-start-0", "elementary-on-run-end-0")
            )
        for resource_type, name, state in package_nodes:
            node = _node("elementary", resource_type, name)
            if state == "enabled":
                nodes.append(node)
            else:
                disabled[node["unique_id"]] = [node]
    macros += list(extra_macros)
    return {
        "metadata": {"adapter_type": adapter, "project_name": ROOT},
        "nodes": {n["unique_id"]: n for n in nodes},
        "disabled": disabled,
        "macros": {m["unique_id"]: m for m in macros},
    }


def project_yml(*, flag: Any = None, vars_: dict[str, Any] | None = None) -> str:
    lines = [f"name: {ROOT}", "version: '1.0.0'", "profile: shop"]
    if flag is not None:
        lines += ["flags:", f"  {OVERRIDE_FLAG}: {flag}"]
    if vars_:
        import yaml

        lines.append(yaml.safe_dump({"vars": vars_}, sort_keys=False).rstrip())
    return "\n".join(lines) + "\n"


# --- installed -------------------------------------------------------------------


def test_a_project_without_the_package_reads_not_installed():
    record = resolve(manifest(elementary=False), project_yml())

    assert not record.installed
    assert record.hooks == "absent"
    assert record.materialization == "dbt"
    assert not any(record.uploads.values())


def test_a_package_disabled_whole_still_reads_installed():
    # Only its macros and its disabled nodes are left, as on variant d for dev.
    record = resolve(manifest(models="disabled", hooks="disabled"), project_yml())

    assert record.installed
    assert not record.enabled


# --- enabled ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("models", "enabled"), [("enabled", True), ("disabled", False)]
)
def test_enabled_reads_whether_a_package_model_is_in_the_graph(models, enabled):
    assert resolve(manifest(models=models), project_yml()).enabled is enabled


# --- materialization -------------------------------------------------------------


@pytest.mark.parametrize(
    ("flag", "expected", "macro"),
    [
        ("false", "elementary", ELEMENTARY_MAT),
        ("true", "dbt", "macro.dbt.materialization_test_default"),
        # dbt's default since 1.8.0 is true.
        (None, "dbt", "macro.dbt.materialization_test_default"),
        # dbt refuses a flag that is not a YAML boolean and keeps its default.
        ("'false'", "dbt", "macro.dbt.materialization_test_default"),
        (
            "\"{{ env_var('X', 'false') }}\"",
            "dbt",
            "macro.dbt.materialization_test_default",
        ),
    ],
)
def test_the_flag_decides_between_the_package_and_dbt(flag, expected, macro):
    """Variants a and b: with the flag false dbt takes the imported package's
    materialization over its own, and with it true (or unset) it does not."""

    record = resolve(manifest(), project_yml(flag=flag))

    assert record.materialization == expected
    assert record.materialization_macro == macro


def test_a_root_shim_that_calls_the_package_is_the_package_s():
    """Variant c: dbt never passes over the root project, and the root's macro
    hands the test to the package's."""

    shim = _macro(ROOT, "materialization_test_default", depends=(ELEMENTARY_MAT,))
    record = resolve(manifest(extra_macros=(shim,)), project_yml(flag="true"))

    assert record.materialization_macro == f"macro.{ROOT}.materialization_test_default"
    assert record.materialization == "elementary"


def test_a_root_shim_that_reaches_the_package_through_a_helper_is_the_package_s():
    """dbt records each call on the macro that makes it, so a shim that hands
    the test on through a macro of its own reaches the package's only by
    following the calls. A cycle among them ends the walk."""

    shim = _macro(
        ROOT,
        "materialization_test_default",
        depends=(f"macro.{ROOT}.forward_test",),
    )
    forward = _macro(
        ROOT,
        "forward_test",
        depends=(f"macro.{ROOT}.materialization_test_default", ELEMENTARY_MAT),
    )
    record = resolve(manifest(extra_macros=(shim, forward)), project_yml(flag="true"))

    assert record.materialization_macro == f"macro.{ROOT}.materialization_test_default"
    assert record.materialization == "elementary"


def test_a_root_shim_whose_helpers_loop_without_the_package_is_not_the_package_s():
    shim = _macro(
        ROOT,
        "materialization_test_default",
        depends=(f"macro.{ROOT}.forward_test",),
    )
    forward = _macro(
        ROOT,
        "forward_test",
        depends=(f"macro.{ROOT}.materialization_test_default", "macro.dbt.statement"),
    )
    record = resolve(manifest(extra_macros=(shim, forward)), project_yml(flag="true"))

    assert record.materialization == "dbt"


def test_a_root_materialization_of_its_own_is_not_the_package_s():
    own = _macro(ROOT, "materialization_test_default", depends=("macro.dbt.statement",))
    record = resolve(manifest(extra_macros=(own,)), project_yml(flag="false"))

    assert record.materialization_macro == f"macro.{ROOT}.materialization_test_default"
    assert record.materialization == "dbt"


def test_a_materialization_for_the_adapter_beats_a_default_one():
    """dbt weighs specificity before locality: an adapter's own ``test``
    materialization wins over the package's ``default`` one even with the flag
    false, and the package's Snowflake one is no candidate on DuckDB."""

    adapter_own = _macro("dbt_duckdb", "materialization_test_duckdb")
    record = resolve(manifest(extra_macros=(adapter_own,)), project_yml(flag="false"))

    assert (
        record.materialization_macro == "macro.dbt_duckdb.materialization_test_duckdb"
    )
    assert record.materialization == "dbt"


def test_the_flag_passes_over_a_more_specific_package_materialization():
    """With the flag true dbt drops an imported winner whenever dbt has any
    candidate of its own, however less specific."""

    specific = _macro(
        "elementary",
        "materialization_test_duckdb",
        depends=("macro.elementary.materialize_test",),
    )
    on = resolve(manifest(extra_macros=(specific,)), project_yml(flag="true"))
    off = resolve(manifest(extra_macros=(specific,)), project_yml(flag="false"))

    assert on.materialization_macro == "macro.dbt.materialization_test_default"
    assert off.materialization_macro == "macro.elementary.materialization_test_duckdb"
    assert (on.materialization, off.materialization) == ("dbt", "elementary")


def _flags_with_a_bad_sibling() -> str:
    return project_yml(flag="false") + "  send_anonymous_usage_stats: 'nope'\n"


def test_an_invalid_sibling_flag_leaves_the_override_flag_true():
    """dbt validates the whole ``flags`` block: one invalid value anywhere and
    every flag keeps its default, so dbt's materialization is picked even with
    this flag false (``read_project_flags``, dbt-core 1.12.5)."""

    pytest.importorskip("dbt.contracts.project")

    record = resolve(manifest(), _flags_with_a_bad_sibling())

    assert record.materialization_macro == "macro.dbt.materialization_test_default"


def test_without_dbt_only_the_override_flag_itself_is_checked(monkeypatch):
    # The other arm: with no dbt to validate the block, the sibling is unseen.
    monkeypatch.setitem(sys.modules, "dbt.contracts.project", None)

    record = resolve(manifest(), _flags_with_a_bad_sibling())

    assert record.materialization_macro == ELEMENTARY_MAT


def test_the_package_s_materialization_is_inert_where_it_is_not_enabled():
    """Variant d for dev: dbt still picks the package's materialization, which
    hands each test straight to dbt's because the package is disabled."""

    record = resolve(
        manifest(models="disabled", hooks="disabled"), project_yml(flag="false")
    )

    assert record.materialization_macro == ELEMENTARY_MAT
    assert record.materialization == "dbt"


def test_the_flag_falls_back_to_a_root_shim_over_a_more_specific_package_one():
    """On Snowflake the package's own ``materialization_test_snowflake`` is the
    most specific candidate. With the flag true dbt passes over it, and the
    best of what is left is the root project's shim, not dbt's default."""

    snowflake = _macro(
        "elementary",
        "materialization_test_snowflake",
        depends=("macro.elementary.materialize_test",),
    )
    shim = _macro(ROOT, "materialization_test_default", depends=(ELEMENTARY_MAT,))
    record = resolve(
        manifest(adapter="snowflake", extra_macros=(snowflake, shim)),
        project_yml(flag="true"),
    )

    assert record.materialization_macro == f"macro.{ROOT}.materialization_test_default"
    assert record.materialization == "elementary"


# --- hooks and uploads -----------------------------------------------------------


def test_hooks_run_and_every_upload_is_on_by_default():
    record = resolve(manifest(), project_yml())

    assert record.hooks == "run"
    assert record.uploads == dict.fromkeys(UPLOAD_VARS, True)


def test_disabling_the_package_s_models_disables_its_hooks():
    """Variant d for dev: dbt gives the package's ``on-run-*`` operations the
    package's model config, so they are disabled with its models and never
    run."""

    record = resolve(manifest(models="disabled", hooks="disabled"), project_yml())

    assert record.hooks == "disabled"
    assert not any(record.uploads.values())


def test_a_manifest_without_the_package_s_hooks_reads_absent():
    record = resolve(manifest(hooks=None), project_yml())

    assert record.hooks == "absent"
    assert not any(record.uploads.values())


@pytest.mark.parametrize(
    ("vars_", "cli_vars", "hooks"),
    [
        ({"edr_cli_run": True}, None, "return_early"),
        ({"edr_cli_run": "true"}, None, "return_early"),
        ({"edr_cli_run": "FALSE"}, None, "run"),
        # Not the exact string, so a truthy one to the package's ``if``.
        ({"edr_cli_run": " false"}, None, "return_early"),
        ({"edr_cli_run": 0}, None, "run"),
        # The package's scope wins over the global value for its own nodes.
        ({"edr_cli_run": True, "elementary": {"edr_cli_run": False}}, None, "run"),
        # --vars wins over both.
        ({"elementary": {"edr_cli_run": False}}, {"edr_cli_run": True}, "return_early"),
        ({"other_package": {"edr_cli_run": True}}, None, "run"),
    ],
)
def test_edr_cli_run_makes_the_hooks_return_early(vars_, cli_vars, hooks):
    record = resolve(manifest(), project_yml(vars_=vars_), cli_vars=cli_vars)

    assert record.hooks == hooks
    assert any(record.uploads.values()) is (hooks == "run")


def test_an_edr_cli_run_dbt_must_render_is_unknown():
    record = resolve(
        manifest(), project_yml(vars_={"edr_cli_run": "{{ env_var('EDR') }}"})
    )

    assert record.hooks == "unknown"
    assert set(record.uploads.values()) == {None}
    assert any("edr_cli_run" in note for note in record.notes)


@pytest.mark.parametrize(("checks", "hooks"), [(True, "return_early"), (False, "run")])
def test_hooks_of_a_package_not_enabled_return_early_where_they_check(checks, hooks):
    """From package 0.20.0 both hooks begin by checking that the package is
    enabled; up to 0.19.0 they do not."""

    record = resolve(
        manifest(models="disabled", hooks_check_enabled=checks), project_yml()
    )

    assert record.hooks == hooks


@pytest.mark.parametrize(
    ("checks", "hooks"), [(True, "return_early"), (False, "unknown")]
)
def test_an_unreadable_edr_cli_run_cannot_matter_where_the_hooks_return_first(
    checks, hooks
):
    """From 0.20.0 the hooks check that the package is enabled before they read
    ``edr_cli_run``, so with every package model disabled and the hooks left on
    (``models: elementary: edr: +enabled: false``) a Jinja value for it cannot
    change what they do (#1307; CodeRabbit on #1382). Up to 0.19.0 it still
    can."""

    record = resolve(
        manifest(models="disabled", hooks_check_enabled=checks),
        project_yml(vars_={"edr_cli_run": "{{ env_var('EDR') }}"}),
    )

    assert record.hooks == hooks


# --- what a mutation run keeps out of the copy (#1307) ----------------------------


def _reading(node: dict[str, Any], *nodes: str, macros: tuple[str, ...] = ()):
    return {**node, "depends_on": {"nodes": list(nodes), "macros": list(macros)}}


def test_a_root_node_that_refers_to_a_package_model_is_a_dependent():
    """Variant e: dbt refuses the parse when a model, or an exposure, refers to a
    disabled model. For a test it only warns and disables the test, which would
    drop out of a mutation run unseen, so a test of the project's own counts too.
    A test of the package's own does not."""

    built = manifest()
    invocations = "model.elementary.dbt_invocations"
    runs = _reading(_node(ROOT, "model", "elementary_runs"), invocations)
    test = _reading(
        _node(ROOT, "test", "relationships_fct_invocation_id"),
        "model.shop.fct",
        invocations,
    )
    own = _reading(_node("elementary", "test", "unique_invocations_id"), invocations)
    exposure = _reading(_node(ROOT, "exposure", "dashboard"), invocations)
    unrelated = _reading(_node(ROOT, "model", "fct2"), "model.shop.fct")
    built["nodes"].update({n["unique_id"]: n for n in (runs, test, own, unrelated)})
    built["exposures"] = {exposure["unique_id"]: exposure}

    assert package_model_dependents(built) == [
        "exposure.shop.dashboard",
        "model.shop.elementary_runs",
        "test.shop.relationships_fct_invocation_id",
    ]


def test_a_reference_to_a_package_model_already_disabled_still_counts():
    built = manifest(models="disabled")
    runs = _reading(
        _node(ROOT, "model", "elementary_runs"), "model.elementary.dbt_invocations"
    )
    built["nodes"][runs["unique_id"]] = runs

    assert package_model_dependents(built) == ["model.shop.elementary_runs"]


def test_a_project_without_a_reference_to_the_package_has_no_dependent():
    assert package_model_dependents(manifest()) == []
    assert package_model_dependents(manifest(elementary=False)) == []


def test_monitoring_tests_are_those_whose_package_test_checks_it_is_enabled():
    """``volume_anomalies`` makes the check through ``test_table_anomalies``;
    a ``*_with_context`` test and dbt's own make none."""

    check = "{% if elementary.is_elementary_enabled() %}{% endif %}"
    macros = (
        _macro(
            "elementary",
            "test_volume_anomalies",
            depends=("macro.elementary.test_table_anomalies",),
        ),
        _macro("elementary", "test_table_anomalies", sql=check),
        _macro("elementary", "test_schema_changes", sql=check),
        _macro("elementary", "test_not_null_with_context"),
        # The check outside a test macro does not make the test a monitoring one.
        _macro("elementary", "get_context_select_clause", sql=check),
    )
    built = manifest(extra_macros=macros)
    fct = "model.shop.fct"
    tests = [
        _reading(
            _node(ROOT, "test", "volume"),
            fct,
            macros=("macro.elementary.test_volume_anomalies",),
        ),
        _reading(
            _node(ROOT, "test", "changes"),
            fct,
            macros=("macro.elementary.test_schema_changes",),
        ),
        _reading(
            _node(ROOT, "test", "with_context"),
            fct,
            macros=(
                "macro.elementary.test_not_null_with_context",
                "macro.elementary.get_context_select_clause",
            ),
        ),
        _reading(
            _node(ROOT, "test", "not_null_fct_id"),
            fct,
            macros=("macro.dbt.test_not_null",),
        ),
    ]
    built["nodes"].update({t["unique_id"]: t for t in tests})

    assert monitoring_tests(built) == {"changes": [fct], "volume": [fct]}


def test_a_project_test_that_wraps_a_monitoring_test_is_one():
    """dbt records the call to the package's test on the project's own test
    macro, not on the test node, so the wrapper is followed through, at any
    depth. A project macro that only calls dbt's is not one."""

    check = "{% if elementary.is_elementary_enabled() %}{% endif %}"
    macros = (
        _macro(
            ROOT,
            "test_my_volume",
            depends=(f"macro.{ROOT}.my_volume_body",),
        ),
        _macro(
            ROOT,
            "my_volume_body",
            depends=(f"macro.{ROOT}.test_my_volume", "macro.elementary.test_volume"),
        ),
        _macro("elementary", "test_volume", sql=check),
        _macro(ROOT, "test_positive", depends=("macro.dbt.test_not_null",)),
    )
    built = manifest(extra_macros=macros)
    fct = "model.shop.fct"
    tests = [
        _reading(
            _node(ROOT, "test", "my_volume_fct"),
            fct,
            macros=(f"macro.{ROOT}.test_my_volume", "macro.dbt.get_where_subquery"),
        ),
        _reading(
            _node(ROOT, "test", "positive_fct"),
            fct,
            macros=(f"macro.{ROOT}.test_positive",),
        ),
    ]
    built["nodes"].update({t["unique_id"]: t for t in tests})

    assert monitoring_tests(built) == {"my_volume_fct": [fct]}


def test_each_disable_var_turns_off_its_own_upload():
    record = resolve(
        manifest(),
        project_yml(
            vars_={"disable_tests_results": True, "disable_run_results": "True"}
        ),
    )

    assert record.uploads == {
        "artifacts": True,
        "run_results": False,
        "test_results": False,
        "freshness_results": True,
        "invocations": True,
    }


def test_an_upload_the_installed_release_lacks_is_off():
    """0.15.2's ``on_run_end`` has no freshness upload and reads no
    ``disable_freshness_results``; 0.16.4's reads all five vars."""

    without = tuple(v for v in UPLOAD_VARS.values() if v != "disable_freshness_results")
    record = resolve(manifest(upload_vars=without), project_yml())

    assert record.uploads["freshness_results"] is False
    assert all(on for name, on in record.uploads.items() if name != "freshness_results")


# --- read: the project's own files -----------------------------------------------


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.mark.parametrize("install", ["dbt_packages", "vendor/packages"])
def test_read_takes_the_release_from_the_installed_package(tmp_path: Path, install):
    header = "" if install == "dbt_packages" else f"packages-install-path: {install}\n"
    _write(tmp_path, "dbt_project.yml", header + project_yml(flag="false"))
    _write(
        tmp_path,
        f"{install}/elementary/dbt_project.yml",
        'name: elementary\nversion: "0.16.4"\n',
    )

    record = read(tmp_path, manifest())

    assert record.release == "0.16.4"
    assert record.materialization == "elementary"


def test_read_without_the_package_files_has_no_release(tmp_path: Path):
    _write(tmp_path, "dbt_project.yml", project_yml())

    record = read(tmp_path, manifest())

    assert record.installed
    assert record.release is None


# --- the work a build's estimate leaves out (#1309) ----------------------------------

_CHECK = "{% if elementary.is_elementary_enabled() %}{% endif %}"
_MONITORING_MACROS = (
    _macro(
        "elementary",
        "test_volume_anomalies",
        depends=("macro.elementary.test_table_anomalies",),
    ),
    _macro("elementary", "test_table_anomalies", sql=_CHECK),
    _macro("elementary", "test_schema_changes", sql=_CHECK),
    _macro("dbt", "test_not_null"),
)


def _test(name: str, macro: str, *, namespace=None, test_name="", kwargs=None):
    node = _reading(
        _node(ROOT, "test", name), "model.shop.fct", macros=(f"macro.{macro}",)
    )
    node["test_metadata"] = {
        "namespace": namespace,
        "name": test_name,
        "kwargs": kwargs or {},
    }
    return node


def _built(models: str | None = "enabled", hooks: str | None = "enabled"):
    """A manifest with an anomaly test, a schema-change test and dbt's not_null."""

    built = manifest(models=models, hooks=hooks, extra_macros=_MONITORING_MACROS)
    tests = [
        _test(
            "volume",
            "elementary.test_volume_anomalies",
            namespace="elementary",
            test_name="volume_anomalies",
            kwargs={"timestamp_column": "ordered_at"},
        ),
        _test(
            "changes",
            "elementary.test_schema_changes",
            namespace="elementary",
            test_name="schema_changes",
        ),
        _test("not_null_fct_id", "dbt.test_not_null", test_name="not_null"),
    ]
    built["nodes"].update({t["unique_id"]: t for t in tests})
    return built


def _work(built, flag=False, vars_=None, selected=None, release=CURRENT_VERSION):
    from exmergo_dex_core.transform.elementary import unpriced_work

    text = project_yml(flag=flag, vars_=vars_)
    record = resolve(built, text, release=release)
    return unpriced_work(record, built, text, selected=selected)


def test_elementary_s_materialization_owes_all_three_kinds_of_work():
    """Variant a: hooks run, the package is enabled and dbt picks its
    materialization."""

    work = _work(_built())

    assert work.codes == [
        "elementary_hooks",
        "elementary_monitoring_tests",
        "elementary_test_extras",
    ]
    assert [t.name for t in work.gated_tests] == ["changes", "volume"]
    # Only dbt's own test gets the failed-row count and the sample query: the
    # package's anomaly and schema-change tests collect their own rows.
    assert work.extras_tests == ("not_null_fct_id",)
    assert len(work.warnings()) == 3
    assert all("the estimate is a floor" in w for w in work.warnings())


def test_dbt_s_materialization_owes_the_hooks_and_the_monitoring_tests():
    """Variant b: the flag keeps dbt's materialization, so no extras."""

    work = _work(_built(), flag=True)

    assert work.codes == ["elementary_hooks", "elementary_monitoring_tests"]
    assert work.extras_tests == ()


def test_a_package_disabled_for_the_target_owes_nothing():
    """Variant d: the models, and with them the hooks, are disabled."""

    work = _work(_built(models="disabled", hooks="disabled"))

    assert work.codes == []
    assert work.warnings() == []


def test_hooks_that_return_early_are_not_work():
    work = _work(_built(), vars_={"edr_cli_run": True})

    assert "elementary_hooks" not in work.codes
    assert "elementary_monitoring_tests" in work.codes


def test_a_project_without_the_package_owes_nothing():
    from exmergo_dex_core.transform.elementary import unpriced_work

    built = manifest(elementary=False)

    assert unpriced_work(resolve(built, project_yml()), built).codes == []


def test_only_the_selected_tests_are_counted():
    work = _work(_built(), selected={"test.shop.changes"})

    assert [t.name for t in work.gated_tests] == ["changes"]
    assert work.extras_tests == ()
    # The hooks run whatever the selection.
    assert work.codes == ["elementary_hooks", "elementary_monitoring_tests"]


@pytest.mark.parametrize(
    ("kwargs", "vars_", "expected"),
    [
        ({}, None, (14, "default")),
        ({"days_back": 30}, None, (30, "test")),
        ({}, {"days_back": "7"}, (7, "project")),
        ({}, {"elementary": {"days_back": 21}}, (21, "project")),
        ({"days_back": "{{ var('window') }}"}, None, (None, "unknown")),
        ({"training_period": {"period": "day", "count": 30}}, None, (None, "unread")),
        ({"seasonality": "day_of_week"}, None, (None, "unread")),
    ],
)
def test_an_anomaly_test_names_the_window_it_reads(kwargs, vars_, expected):
    built = _built()
    built["nodes"]["test.shop.volume"]["test_metadata"]["kwargs"].update(kwargs)

    [volume] = [t for t in _work(built, vars_=vars_).gated_tests if t.name == "volume"]

    assert (volume.days_back, volume.days_back_source) == expected


def test_the_warning_names_each_anomaly_test_s_window():
    [_, gated, _] = _work(_built()).warnings()

    assert "volume over 14 days" in gated
    assert "2 Elementary monitoring tests" in gated


@pytest.mark.parametrize(
    ("model_config", "expected"),
    [
        ({"config": {"elementary": {"days_back": 30}}}, (30, "model")),
        ({"config": {"meta": {"elementary": {"days_back": 45}}}}, (45, "model")),
        ({"meta": {"elementary": {"days_back": 60}}}, (60, "model")),
        ({"config": {"elementary": {"seasonality": "day_of_week"}}}, (None, "unread")),
    ],
)
def test_the_tested_model_s_elementary_config_comes_before_the_vars(
    model_config, expected
):
    built = _built()
    built["nodes"]["model.shop.fct"].update(model_config)

    [volume] = [
        t
        for t in _work(built, vars_={"days_back": 7}).gated_tests
        if t.name == "volume"
    ]

    assert (volume.days_back, volume.days_back_source) == expected


def test_a_test_argument_beats_the_model_s_config():
    built = _built()
    built["nodes"]["model.shop.fct"]["config"] = {"elementary": {"days_back": 30}}
    built["nodes"]["test.shop.volume"]["test_metadata"]["kwargs"]["days_back"] = 3

    [volume] = [t for t in _work(built).gated_tests if t.name == "volume"]

    assert (volume.days_back, volume.days_back_source) == (3, "test")


# --- tests that checked nothing (#1308) -------------------------------------------

#: What ``no_results_query()`` compiles to at 0.16.4 and 0.26.1.
NO_RESULTS = (
    "\n    with nothing as (select 1 as num)\n    select * from nothing where num = 2\n"
)


def test_the_no_results_query_is_matched_and_nothing_else_is():
    assert is_no_results_query(NO_RESULTS)
    assert is_no_results_query(
        "WITH nothing AS (SELECT 1 AS num) SELECT * FROM nothing WHERE num = 0;"
    )
    assert not is_no_results_query(None)
    assert not is_no_results_query("")
    assert not is_no_results_query(
        f"{NO_RESULTS} union all select 1 from fct where amount < 0"  # noqa: S608
    )
    assert not is_no_results_query("select * from fct where id is null")


def _elementary_test_manifest(*, models: str) -> dict[str, Any]:
    """Every kind of test the classifier tells apart, on ``fct``.

    ``compiled`` maps a test's name to the SQL the build recorded for it, with
    ``None`` for a test the manifest holds no compiled SQL for.
    """

    check = "{% if elementary.is_elementary_enabled() %}{% endif %}"
    macros = (
        _macro(
            "elementary",
            "test_volume_anomalies",
            depends=("macro.elementary.test_table_anomalies",),
        ),
        _macro("elementary", "test_table_anomalies", sql=check),
        _macro("elementary", "test_schema_changes", sql=check),
        _macro("elementary", "test_volume_threshold", sql=check),
        _macro("elementary", "test_json_schema", sql=check),
        _macro("elementary", "test_collect_metrics", sql=check),
        _macro("elementary", "test_not_null_with_context"),
        _macro(
            ROOT, "test_my_volume", depends=("macro.elementary.test_volume_anomalies",)
        ),
    )
    return manifest(models=models, extra_macros=macros)


def _with_tests(built: dict[str, Any], compiled: dict[str, str | None]) -> dict:
    macro_of = {
        "volume": "macro.elementary.test_volume_anomalies",
        "changes": "macro.elementary.test_schema_changes",
        "threshold": "macro.elementary.test_volume_threshold",
        "json": "macro.elementary.test_json_schema",
        "collect": "macro.elementary.test_collect_metrics",
        "with_context": "macro.elementary.test_not_null_with_context",
        "wrapped_volume": f"macro.{ROOT}.test_my_volume",
        "not_null_fct_id": "macro.dbt.test_not_null",
    }
    for name, sql in compiled.items():
        node = _reading(
            _node(ROOT, "test", name), "model.shop.fct", macros=(macro_of[name],)
        )
        if sql is not None:
            node["compiled_code"] = sql
        built["nodes"][node["unique_id"]] = node
    return built


def _classified(built: dict[str, Any], **kwargs: Any) -> dict[str, str]:
    record = resolve(built, project_yml(), **kwargs)
    return {
        uid.split(".")[2]: reason
        for uid, reason in unchecked_tests(built, record).items()
    }


def test_with_the_package_disabled_every_test_compiled_to_no_results_is_unchecked():
    """Acceptance, first half: whatever the name, a test that compiled to the
    no-results query is listed; a dbt test, and an Elementary test that
    compiled to a real query, are not."""

    built = _with_tests(
        _elementary_test_manifest(models="disabled"),
        {
            "volume": NO_RESULTS,
            "changes": NO_RESULTS,
            "json": NO_RESULTS,
            "wrapped_volume": NO_RESULTS,
            "with_context": "select * from fct where id is null",
            "not_null_fct_id": "select id from fct where id is null",
        },
    )
    assert _classified(built) == dict.fromkeys(
        ("volume", "changes", "json", "wrapped_volume"), "package_disabled"
    )


def test_with_the_package_disabled_and_no_compiled_sql_the_enabled_check_decides():
    built = _with_tests(
        _elementary_test_manifest(models="disabled"),
        {"volume": None, "with_context": None, "not_null_fct_id": None},
    )
    assert _classified(built) == {"volume": "package_disabled"}


def test_with_the_package_enabled_the_history_tests_are_unchecked():
    """Acceptance, second half: the anomaly tests (through a project's wrapper
    too), ``schema_changes`` and ``volume_threshold`` rest on history; a
    ``*_with_context`` test, ``json_schema`` and dbt's own do not. A test that
    still compiled to the no-results query checked nothing for that reason."""

    real = "select * from elementary_test_results_tmp where is_anomalous"
    built = _with_tests(
        _elementary_test_manifest(models="enabled"),
        {
            "volume": real,
            "changes": real,
            "threshold": None,
            "wrapped_volume": real,
            "json": "select * from fct where not valid",
            "collect": NO_RESULTS,
            "with_context": "select * from fct where id is null",
            "not_null_fct_id": "select id from fct where id is null",
        },
    )
    assert _classified(built) == {
        "volume": "history_unmeasured",
        "changes": "history_unmeasured",
        "threshold": "history_unmeasured",
        "wrapped_volume": "history_unmeasured",
        "collect": "compiled_to_no_results",
    }


def test_a_project_without_the_package_has_no_unchecked_tests():
    built = manifest(elementary=False)
    built = _with_tests(built, {"not_null_fct_id": NO_RESULTS})
    assert unchecked_tests(built, resolve(built, project_yml())) == {}


# --- the releases Dex was run against (#1445) ------------------------------------

#: Every release the Elementary fixture installs: the releases its matrix names.
FIXTURE_RELEASES = set(RELEASES)


def _parts(release: str) -> list[int]:
    return [int(part) for part in release.split(".")]


def _newest() -> list[int]:
    return max(_parts(r) for r in FIXTURE_RELEASES)


def _oldest() -> list[int]:
    return min(_parts(r) for r in FIXTURE_RELEASES)


#: A patch above the newest release the fixture runs, a minor above it, and a
#: minor below the oldest, each derived from the fixture so none goes stale.
_NEWEST, _OLDEST = _newest(), _oldest()
PATCH_ABOVE = f"{_NEWEST[0]}.{_NEWEST[1]}.{_NEWEST[2] + 1}"
MINOR_ABOVE = f"{_NEWEST[0]}.{_NEWEST[1] + 1}.0"
MINOR_BELOW = f"{_OLDEST[0]}.{_OLDEST[1] - 1}.0"
UNTESTED = (PATCH_ABOVE, MINOR_ABOVE, MINOR_BELOW)


def test_the_tested_releases_are_the_ones_the_fixture_runs():
    """The range is derived from the fixture, so the two cannot drift apart: a
    release added to the fixture, or dropped from it, fails here until the
    engine's list says the same."""

    from exmergo_dex_core.transform import elementary

    assert sorted(elementary.TESTED_RELEASES) == sorted(FIXTURE_RELEASES)
    assert _parts(elementary.NEWEST_TESTED) == _newest()
    assert _parts(elementary.OLDEST_TESTED) == _oldest()
    assert elementary.NEWEST_TESTED == CURRENT_VERSION


def test_the_matrix_names_each_release_the_fixture_builds_once():
    """#1446: the matrix is the fixture's release lists, slot by slot. A release
    added to ``VERSIONS`` or ``POSTGRES_VERSIONS`` without a slot, or a slot
    whose release neither list builds, fails here; so does a release on the
    wrong warehouse, since DuckDB builds no release before 0.23.0."""

    assert len(RELEASES) == len(set(RELEASES)), "a release fills two slots"
    on = {
        "DuckDB": {s.release for s in MATRIX if s.warehouse == "DuckDB"},
        "Postgres": {s.release for s in MATRIX if s.warehouse == "Postgres"},
    }
    assert {s.warehouse for s in MATRIX} == set(on)
    assert on["DuckDB"] == set(VERSIONS)
    assert on["Postgres"] == set(POSTGRES_VERSIONS)
    newest = max(MATRIX, key=lambda s: _parts(s.release))
    oldest = min(MATRIX, key=lambda s: _parts(s.release))
    assert (newest.slot, newest.release) == ("the newest release", CURRENT_VERSION)
    assert oldest.slot == "the floor"


def _contributing_matrix() -> list[tuple[str, str, str]]:
    """The rows of the release-matrix table in CONTRIBUTING.md: slot, release,
    warehouse."""

    text = (Path(__file__).resolve().parents[4] / "CONTRIBUTING.md").read_text(
        encoding="utf-8"
    )
    section = text.split("### The release matrix", 1)[1].split("\n#", 1)[0]
    rows = []
    for line in section.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 3 or set(cells[0]) <= {"-", " "} or cells[0] == "Slot":
            continue
        rows.append((cells[0], cells[1].strip("`"), cells[2]))
    return rows


def test_the_policy_in_contributing_states_the_matrix_the_fixture_runs():
    """The table a contributor reads is the matrix the fixture builds, row for
    row and in order, so the policy cannot describe releases nothing runs."""

    assert _contributing_matrix() == [(s.slot, s.release, s.warehouse) for s in MATRIX]


@pytest.mark.parametrize(
    ("release", "status"),
    [
        *[(r, "supported") for r in sorted(FIXTURE_RELEASES)],
        # Inside the range and in no slot: supported, with #1381's warning
        # below 0.20.0.
        ("0.21.0", "supported"),
        ("0.18.1", "supported"),
        ("v0.20.0", "supported"),
        # A patch above the newest is untested too, and so is a prerelease of it.
        (PATCH_ABOVE, "untested"),
        (f"{PATCH_ABOVE}-rc.1", "untested"),
        (MINOR_ABOVE, "untested"),
        ("1.0.0", "untested"),
        (MINOR_BELOW, "untested"),
        (f"{'.'.join(map(str, _oldest()))}-rc.1", "untested"),
        (None, "unknown"),
        ("", "unknown"),
        ("0.26", "unknown"),
        ("unknown-build-42", "unknown"),
        ("{{ var('elementary_version') }}", "unknown"),
    ],
)
def test_a_release_is_supported_only_inside_the_tested_range(release, status):
    from exmergo_dex_core.transform.elementary import release_status

    assert release_status(release) == status


@pytest.mark.parametrize("release", sorted(FIXTURE_RELEASES))
def test_a_tested_release_carries_no_release_reason_code(release):
    record = resolve(manifest(), project_yml(), release=release)

    assert record.release_status == "supported"
    assert record.release_reason_code is None
    assert record.release_warning() is None
    assert record.hooks == "run"


@pytest.mark.parametrize("release", UNTESTED)
def test_on_an_untested_release_what_the_hooks_do_is_unknown(release):
    """Hooks that would run, or return early on the package's own enabled
    check, are ``unknown``, and so is every upload they would perform."""

    running = resolve(manifest(), project_yml(), release=release)
    early = resolve(manifest(models="disabled"), project_yml(), release=release)

    for record in (running, early):
        assert record.release_status == "untested"
        assert record.release_reason_code == "elementary_release_untested"
        assert record.hooks == "unknown"
        assert record.uploads == dict.fromkeys(UPLOAD_VARS, None)
        assert any(release in note or "untested" in note for note in record.notes)


def test_a_release_that_cannot_be_read_is_unknown():
    record = resolve(manifest(), project_yml(), release=None)

    assert record.release_status == "unknown"
    assert record.release_reason_code == "elementary_release_unknown"
    assert record.hooks == "unknown"


@pytest.mark.parametrize("release", [*UNTESTED, None])
def test_what_dbt_settles_is_read_on_any_release(release):
    """Whether the package's models and hooks are enabled, and which ``test``
    materialization dbt picks, are dbt's to say, not the release's."""

    from exmergo_dex_core.transform.elementary import release_status

    disabled = resolve(
        manifest(models="disabled", hooks="disabled"), project_yml(), release=release
    )
    absent = resolve(manifest(hooks=None), project_yml(), release=release)
    picked = resolve(manifest(), project_yml(flag="false"), release=release)
    passed_over = resolve(manifest(), project_yml(flag="true"), release=release)

    assert (disabled.enabled, disabled.hooks) == (False, "disabled")
    assert not any(disabled.uploads.values())
    assert absent.hooks == "absent"
    assert (picked.materialization, picked.materialization_macro) == (
        "elementary",
        ELEMENTARY_MAT,
    )
    assert passed_over.materialization == "dbt"
    assert disabled.release_status == release_status(release)


@pytest.mark.parametrize("release", [*UNTESTED, None])
def test_the_package_s_materialization_off_its_tested_releases_is_unknown(release):
    """Where the package is disabled, its materialization hands each test to
    dbt's on the releases Dex read; on another, nothing says it does."""

    record = resolve(
        manifest(models="disabled", hooks="disabled"),
        project_yml(flag="false"),
        release=release,
    )

    assert record.materialization_macro == ELEMENTARY_MAT
    assert record.materialization == "unknown"


def test_the_warning_names_the_release_read_and_the_newest_tested():
    untested = resolve(manifest(), project_yml(), release=PATCH_ABOVE)
    unknown = resolve(manifest(), project_yml(), release=None)

    said = untested.release_warning()
    assert said is not None
    assert PATCH_ABOVE in said
    assert CURRENT_VERSION in said
    assert "elementary_release_untested" in said
    said = unknown.release_warning()
    assert said is not None
    assert CURRENT_VERSION in said
    assert "elementary_release_unknown" in said


@pytest.mark.parametrize(
    "own",
    [
        b'name: elementary\nversion: "0.26.1\n  - [',
        b"\xff\xfename: elementary\n",
        b"name: elementary\n",
        b"name: elementary\nversion: \"{{ var('v') }}\"\n",
    ],
    ids=["invalid_yaml", "not_utf8", "no_version", "jinja_version"],
)
def test_read_reports_an_installed_project_file_it_cannot_read_as_unknown(
    tmp_path: Path, own: bytes
):
    _write(tmp_path, "dbt_project.yml", project_yml())
    path = tmp_path / "dbt_packages" / "elementary" / "dbt_project.yml"
    path.parent.mkdir(parents=True)
    path.write_bytes(own)

    record = read(tmp_path, manifest())

    assert record.installed
    assert record.release_status == "unknown"
    assert record.release_reason_code == "elementary_release_unknown"


def test_read_reports_a_release_above_the_newest_tested_as_untested(tmp_path: Path):
    _write(tmp_path, "dbt_project.yml", project_yml())
    _write(
        tmp_path,
        "dbt_packages/elementary/dbt_project.yml",
        f'name: elementary\nversion: "{PATCH_ABOVE}"\n',
    )

    record = read(tmp_path, manifest())

    assert record.release == PATCH_ABOVE
    assert record.release_reason_code == "elementary_release_untested"


_EVERY_KIND = {
    "volume": "select * from elementary_test_results_tmp where is_anomalous",
    "changes": "select * from elementary_test_results_tmp",
    "threshold": None,
    "wrapped_volume": "select * from elementary_test_results_tmp",
    "json": "select * from fct where not valid",
    "collect": NO_RESULTS,
    "with_context": "select * from fct where id is null",
    "not_null_fct_id": "select id from fct where id is null",
}


@pytest.mark.parametrize(
    ("release", "code"),
    [
        (PATCH_ABOVE, "elementary_release_untested"),
        (MINOR_BELOW, "elementary_release_untested"),
        (None, "elementary_release_unknown"),
    ],
)
def test_off_its_tested_releases_no_elementary_test_is_read_as_checking(release, code):
    """Every test that reaches the package's tests carries the release code,
    the ones #1308's rule would let count included; a test whose compiled SQL
    is the no-results query keeps that reason, which is read off its SQL, and
    dbt's own test is never listed."""

    built = _with_tests(_elementary_test_manifest(models="enabled"), _EVERY_KIND)

    assert _classified(built, release=release) == {
        **dict.fromkeys(
            ("volume", "changes", "threshold", "wrapped_volume", "json"), code
        ),
        "with_context": code,
        "collect": "compiled_to_no_results",
    }


def test_off_its_tested_releases_a_disabled_package_s_tests_carry_the_code():
    built = _with_tests(
        _elementary_test_manifest(models="disabled"),
        {"volume": NO_RESULTS, "changes": None, "not_null_fct_id": None},
    )

    assert _classified(built, release=PATCH_ABOVE) == {
        "volume": "package_disabled",
        "changes": "elementary_release_untested",
    }


def test_off_its_tested_releases_the_work_is_counted_wherever_it_may_run():
    """Variant a at an untested release: the hooks may run, the anomaly test's
    default window is not taken from the releases Dex read, and every selected
    test counts toward the extras, the package's own included."""

    work = _work(_built(), release=PATCH_ABOVE)

    assert work.codes == [
        "elementary_hooks",
        "elementary_monitoring_tests",
        "elementary_test_extras",
    ]
    assert work.hooks_state == "unknown"
    assert [(t.name, t.days_back, t.days_back_source) for t in work.gated_tests] == [
        ("changes", None, None),
        ("volume", None, "unknown"),
    ]
    assert work.extras_tests == ("changes", "not_null_fct_id", "volume")
    record = work.as_dict()
    assert (record["release"], record["release_reason_code"]) == (
        PATCH_ABOVE,
        "elementary_release_untested",
    )
    assert len(work.warnings()) == 4
    assert PATCH_ABOVE in work.warnings()[-1]
    assert CURRENT_VERSION in work.warnings()[-1]


def test_off_its_tested_releases_a_disabled_package_still_owes_its_tests():
    """Variant d at an untested release: dbt disabled the hooks, so they are no
    work; that the monitoring tests and the materialization do nothing where
    the package is disabled is the tested releases' behaviour, so both count."""

    tested = _work(_built(models="disabled", hooks="disabled"))
    untested = _work(_built(models="disabled", hooks="disabled"), release=PATCH_ABOVE)

    assert tested.codes == []
    assert untested.codes == ["elementary_monitoring_tests", "elementary_test_extras"]
    assert not untested.hooks


def test_a_build_names_the_untested_release_and_lists_its_tests(tmp_path: Path):
    """The build's own reading (#1308's path): the record from the installed
    files, its tests under the release code, and the warning beside them."""

    from exmergo_dex_core.transform.commands import (
        _elementary_release_warning,
        _unchecked_tests,
    )

    _write(tmp_path, "dbt_project.yml", project_yml())
    _write(
        tmp_path,
        "dbt_packages/elementary/dbt_project.yml",
        f'name: elementary\nversion: "{MINOR_ABOVE}"\n',
    )
    built = _with_tests(
        _elementary_test_manifest(models="enabled"),
        {"volume": "select 1", "not_null_fct_id": "select 1"},
    )

    inputs = {"manifest": built, "project": tmp_path}
    unchecked = _unchecked_tests(inputs)
    warning = _elementary_release_warning(inputs)

    assert unchecked == {"test.shop.volume": "elementary_release_untested"}
    assert warning is not None
    assert MINOR_ABOVE in warning


class _Copy:
    """A stand-in for the mutation run's project copy: ``parse`` answers with
    the manifest the copy's edits would give, and the override flag is written
    into the copy's own ``dbt_project.yml``, where the resolver reads it."""

    def __init__(self, root: Path) -> None:
        self.shadow = root
        self.models_off = False
        _write(root, "dbt_project.yml", project_yml(flag="false"))

    def parse(self) -> dict[str, Any]:
        state = "disabled" if self.models_off else "enabled"
        return manifest(models=state, hooks=state)

    def disable_package_models(self, package: str):
        self.models_off = True

        def undo() -> None:
            self.models_off = False

        return undo

    def set_project_flag(self, name: str, value: Any):
        _write(self.shadow, "dbt_project.yml", project_yml(flag=str(value).lower()))
        return lambda: _write(self.shadow, "dbt_project.yml", project_yml())


@pytest.mark.parametrize(("release", "pinned"), [("0.26.1", False), ("0.26.2", True)])
def test_the_mutation_copy_pins_dbt_s_materialization_off_the_tested_releases(
    tmp_path: Path, release: str, pinned: bool
):
    """With the package's models disabled, a tested release's materialization
    hands each test to dbt's, so the copy needs nothing more. On an untested
    one nothing says so, and the copy sets dbt's override flag as well, so
    dbt's own materialization runs whatever the release does (#1445)."""

    from exmergo_dex_core.transform.commands import _quiet_elementary

    copy = _Copy(tmp_path)
    _write(
        tmp_path,
        "dbt_packages/elementary/dbt_project.yml",
        f'name: elementary\nversion: "{release}"\n',
    )

    _left_out, warnings = _quiet_elementary(copy)

    text = (tmp_path / "dbt_project.yml").read_text(encoding="utf-8")
    assert (f"{OVERRIDE_FLAG}: true" in text) is pinned
    assert read(tmp_path, copy.parse()).materialization == "dbt"
    assert any("elementary_release_untested" in w for w in warnings) is pinned
