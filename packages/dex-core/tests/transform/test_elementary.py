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
    read,
    resolve,
)

ROOT = "shop"
ELEMENTARY_MAT = "macro.elementary.materialization_test_default"


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
