"""`transform build --cold-deps` and the repair routes for a failed package (#860).

Real dbt on DuckDB, with one tiny package installed from a local git repository:
no network, and a real copy, unlike a `local:` package, which dbt links. The
package coalesces a timestamp with an all-NULL column typed integer, the
benchmark's own failure, and a report reads it.
"""

from __future__ import annotations

import importlib
import json
import shutil
import subprocess
from pathlib import Path

import duckdb
import pytest
import yaml

from exmergo_dex_core.cli import main

pytest.importorskip("dbt.adapters.duckdb")
pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")

COALESCE = (
    "select id, coalesce(created_at, legacy_ts) as created_ts "
    "from {{ ref('raw_task') }}\n"
)
REPAIRED = (
    "select id, coalesce(created_at, cast(cast(legacy_ts as varchar) as timestamp)) "
    "as created_ts from {{ ref('raw_task') }}\n"
)
REPORT = "select id, created_ts from {{ ref('pkg_task') }}\n"


def _git(cwd: Path, *argv: str) -> str:
    return subprocess.run(  # noqa: S603
        [  # noqa: S607
            "git",
            "-c",
            "user.email=t@example.com",
            "-c",
            "user.name=t",
            *argv,
        ],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _package(root: Path, *, two_arg_reader: bool = False) -> Path:
    package = root / "tiny_pkg"
    (package / "models").mkdir(parents=True)
    (package / "dbt_project.yml").write_text(
        "name: tiny_pkg\nversion: '1.0'\nconfig-version: 2\nmodel-paths: [models]\n"
    )
    (package / "models" / "pkg_task.sql").write_text(COALESCE)
    if two_arg_reader:
        (package / "models" / "pkg_rollup.sql").write_text(
            "select count(*) as n from {{ ref('tiny_pkg', 'pkg_task') }}\n"
        )
    _git(package, "init", "-q", ".")
    _git(package, "add", "-A")
    _git(package, "commit", "-qm", "init")
    return package


@pytest.fixture
def project(tmp_path, monkeypatch):
    return _project(tmp_path, monkeypatch)


def _project(tmp_path, monkeypatch, **package_options) -> Path:
    package = _package(tmp_path, **package_options)
    root = tmp_path / "proj"
    (root / "models").mkdir(parents=True)
    (root / "seeds").mkdir()
    (root / ".dex").mkdir()
    (root / ".git").mkdir()
    (root / "dbt_project.yml").write_text(
        yaml.safe_dump(
            {
                "name": "proj",
                "version": "1.0",
                "config-version": 2,
                "profile": "proj",
                "model-paths": ["models"],
                "seed-paths": ["seeds"],
                "seeds": {
                    "proj": {
                        "raw_task": {
                            "+column_types": {
                                "id": "integer",
                                "created_at": "timestamp",
                                "legacy_ts": "integer",
                            }
                        }
                    }
                },
            },
            sort_keys=False,
        )
    )
    # Relative on purpose: a cold build runs from a copy and must still reach
    # this database.
    (root / "profiles.yml").write_text(
        "proj:\n  target: dev\n  outputs:\n    dev:\n      type: duckdb\n"
        "      path: ./dev.duckdb\n"
    )
    (root / "packages.yml").write_text(
        yaml.safe_dump(
            {
                "packages": [
                    {
                        "git": f"file://{package}",
                        "revision": _git(package, "rev-parse", "HEAD"),
                    }
                ]
            }
        )
    )
    (root / "seeds" / "raw_task.csv").write_text(
        "id,created_at,legacy_ts\n1,2024-01-01 00:00:00,\n2,,\n3,2024-02-01 00:00:00,\n"
    )
    (root / "models" / "rpt.sql").write_text(REPORT)
    (root / ".dex" / "config.yml").write_text(
        "connector: duckdb\nduckdb:\n  path: dev.duckdb\n"
    )
    monkeypatch.chdir(root)
    monkeypatch.setenv("DEX_ALLOW_UNSERIALIZED_LEDGER", "1")
    return root


def _dex(capsys, *argv: str) -> dict:
    main(list(argv))
    return json.loads(capsys.readouterr().out)


def _build(capsys, *flags: str) -> dict:
    return _dex(capsys, "transform", "build", "--target", "dev", *flags)


def _override(root: Path, capsys) -> str:
    """The supported repair, through a plan: a project model plus the disable."""

    assert _dex(capsys, "transform", "deps")["status"] == "ok"
    config = yaml.safe_load((root / "dbt_project.yml").read_text())
    config["models"] = {"tiny_pkg": {"pkg_task": {"+enabled": False}}}
    payload = {
        "edits": [
            {"path": "models/pkg_task.sql", "kind": "model_sql", "content": REPAIRED},
            {
                "path": "dbt_project.yml",
                "kind": "project_yml",
                "content": yaml.safe_dump(config, sort_keys=False),
            },
        ]
    }
    edits = root / "edits.json"
    edits.write_text(json.dumps(payload))
    planned = _dex(
        capsys, "transform", "plan", "Repair pkg_task", "--edits-file", str(edits)
    )
    assert planned["status"] == "ok", planned["errors"]
    plan_id = planned["data"]["plan_id"]
    applied = _dex(capsys, "transform", "apply", plan_id)
    assert applied["status"] == "ok", applied["errors"]
    return plan_id


def _report_rows(root: Path) -> list[tuple]:
    connection = duckdb.connect(str(root / "dev.duckdb"), read_only=True)
    try:
        return connection.execute("select * from main.rpt order by 1").fetchall()
    finally:
        connection.close()


def _column_type(root: Path, relation: str, column: str) -> str:
    connection = duckdb.connect(str(root / "dev.duckdb"), read_only=True)
    try:
        return connection.execute(
            "select data_type from information_schema.columns "
            "where table_name = ? and column_name = ?",
            [relation, column],
        ).fetchone()[0]
    finally:
        connection.close()


def test_a_failed_package_model_is_diagnosed_as_package_owned(project, capsys):
    envelope = _build(capsys)

    assert envelope["status"] == "error"
    [entry] = envelope["data"]["prerequisites"]
    assert entry["node"] == "pkg_task"
    assert entry["owner"] == "package"
    assert entry["package"] == "tiny_pkg"
    # No plan named, so nothing can be claimed about its relation to one.
    assert entry["plan_relation"] == "undetermined"
    kinds = {o["kind"]: o for o in entry["options"]}
    assert kinds["project_override"]["supported"] is True
    assert "+enabled: false" in kinds["project_override"]["how"]
    assert kinds["macro_dispatch"]["supported"] is False
    assert kinds["pinned_dependency"]["supported"] is True
    [blocker] = envelope["data"]["completion"]["blockers"]
    assert blocker["owner"] == "package"


def test_an_edit_under_dbt_packages_passes_warm_and_is_lost_cold(project, capsys):
    assert _build(capsys)["status"] == "error"  # installs the package, then fails
    installed = project / "dbt_packages" / "tiny_pkg" / "models" / "pkg_task.sql"
    installed.write_text(REPAIRED)

    warm = _build(capsys)
    assert warm["status"] == "ok"
    assert "cold_verification" not in warm["data"]

    cold = _build(capsys, "--cold-deps")
    assert cold["status"] == "error"
    report = cold["data"]["cold_verification"]
    assert report["reinstalled"] is True
    assert report["installed_edits"]["changed"]["paths"] == [
        "tiny_pkg/models/pkg_task.sql"
    ]
    assert report["packages"][0]["source"] == "git"
    assert report["packages"][0]["pin"]
    assert report["installed_tree_digest"]
    assert report["dbt_runtime"]
    assert cold["data"]["completion"]["cold"]["installed_edits_lost"] == 1
    # Nothing points into the copy, which is gone by the time anyone reads this.
    assert "dex-shadow" not in json.dumps(cold)
    assert report["artifact_directory"].endswith("/cold")
    assert cold["data"]["artifact_directory"] == report["artifact_directory"]
    assert not Path(report["artifact_directory"]).is_absolute()
    # The real project keeps what it had; the proof ran in the copy.
    assert installed.read_text() == REPAIRED


def test_a_project_override_survives_cold_and_rebuilds_the_report(project, capsys):
    plan_id = _override(project, capsys)

    built = _build(capsys, "--cold-deps", "--for-plan", plan_id, "--verify")

    assert built["status"] == "ok", built["errors"]
    assert built["data"]["outcome"] == "validated"
    report = built["data"]["cold_verification"]
    assert report["reinstalled"] is True
    assert report["rebuilt_dev_relations"] == 3
    # Every row kept, and the column is a timestamp rather than a text repair.
    assert [row[0] for row in _report_rows(project)] == [1, 2, 3]
    assert _column_type(project, "rpt", "created_ts").startswith("TIMESTAMP")


def test_a_skipped_report_with_a_stale_relation_is_not_built(project, capsys):
    _override(project, capsys)
    assert _build(capsys)["status"] == "ok"
    assert _report_rows(project)

    # The repair is reverted, so the prerequisite fails on a dev target that
    # still holds the report from the run above.
    (project / "models" / "pkg_task.sql").write_text(COALESCE)
    payload = {
        "edits": [
            {"path": "models/rpt.sql", "kind": "model_sql", "content": REPORT + "\n"}
        ]
    }
    edits = project / "edits.json"
    edits.write_text(json.dumps(payload))
    plan_id = _dex(
        capsys, "transform", "plan", "Touch the report", "--edits-file", str(edits)
    )["data"]["plan_id"]
    assert _dex(capsys, "transform", "apply", plan_id)["status"] == "ok"

    built = _build(capsys, "--for-plan", plan_id)

    assert built["status"] == "error"
    completion = built["data"]["completion"]
    assert completion["deliverables"]["skipped"]["names"] == ["rpt"]
    assert completion["deliverables"]["built"]["count"] == 0
    assert "predates" in completion["deliverables"]["note"]
    assert built["data"]["outcome"] != "validated"
    [entry] = built["data"]["prerequisites"]
    # The failing model is not the one this plan edited, nor behind it.
    assert entry["node"] == "pkg_task"
    assert entry["plan_relation"] == "independent"
    assert entry["blocks"] == ["rpt"]
    assert _report_rows(project)  # the stale relation is still there


def test_a_plan_that_breaks_a_model_is_not_called_preexisting(project, capsys):
    plan_id = _override(project, capsys)
    assert _build(capsys, "--for-plan", plan_id)["status"] == "ok"
    payload = {
        "edits": [
            {
                "path": "models/pkg_task.sql",
                "kind": "model_sql",
                "content": "select id, no_such_column as created_ts "
                "from {{ ref('raw_task') }}\n",
            }
        ]
    }
    edits = project / "edits.json"
    edits.write_text(json.dumps(payload))
    planned = _dex(capsys, "transform", "plan", "Break it", "--edits-file", str(edits))
    plan_id = planned["data"]["plan_id"]
    _dex(capsys, "transform", "apply", plan_id)

    built = _build(capsys, "--for-plan", plan_id)

    [entry] = built["data"]["prerequisites"]
    assert entry["owner"] == "project"
    assert entry["plan_relation"] == "edited"
    assert entry["options"][0]["kind"] == "project_edit"


def test_a_two_argument_reader_rules_the_override_out(tmp_path, monkeypatch, capsys):
    _project(tmp_path, monkeypatch, two_arg_reader=True)

    [entry] = _build(capsys)["data"]["prerequisites"]

    option = next(o for o in entry["options"] if o["kind"] == "project_override")
    assert option["supported"] is False
    assert "pkg_rollup" in option["reason"]
    assert "ref('tiny_pkg', 'pkg_task')" in option["reason"]


def test_a_failed_reinstall_builds_nothing(project, capsys):
    (project / "packages.yml").write_text(
        yaml.safe_dump(
            {"packages": [{"git": f"file://{project}/absent", "revision": "abc"}]}
        )
    )
    built = _build(capsys, "--cold-deps")

    assert built["status"] == "error"
    assert built["data"]["cold_verification"]["reinstalled"] is False
    assert "nodes" not in built["data"]
    # The bounded lead is still there, and says why nothing was built.
    assert built["data"]["completion"]["outcome"] == "not_run"
    assert built["data"]["completion"]["cold"]["reinstalled"] is False
    assert "rebuilt_dev_relations" not in built["data"]["cold_verification"]
    assert not (project / "dev.duckdb").exists()


def test_no_package_spec_says_so_and_builds(project, capsys):
    (project / "packages.yml").unlink()
    (project / "models" / "rpt.sql").write_text("select 1 as id\n")

    built = _build(capsys, "--cold-deps")

    assert built["status"] == "ok"
    assert built["data"]["cold_verification"]["ran"] is False
    assert built["data"]["completion"]["cold"]["ran"] is False


def test_no_package_spec_is_still_reported_when_the_build_fails(project, capsys):
    (project / "packages.yml").unlink()
    (project / "models" / "rpt.sql").write_text("select no_such from nowhere\n")

    built = _build(capsys, "--cold-deps")

    assert built["status"] == "error"
    assert built["data"]["cold_verification"]["ran"] is False


def test_cold_deps_conflicts_with_no_install_deps(project, capsys):
    refused = _build(capsys, "--cold-deps", "--no-install-deps")
    assert refused["status"] == "error"
    assert "--no-install-deps" in refused["errors"][0]


def test_an_ordinary_build_never_reinstalls(project, capsys, monkeypatch):
    _build(capsys)  # the first build installs what is absent
    # By path: the package re-exports the build function under the module's name.
    build_module = importlib.import_module("exmergo_dex_core.transform.build")

    def refuse(*args, **kwargs):
        raise AssertionError("an ordinary build reinstalled the packages")

    monkeypatch.setattr(build_module, "deps", refuse)
    assert _build(capsys)["status"] == "error"  # the package's own failure


def test_a_local_package_is_named_as_not_comparable(tmp_path, monkeypatch, capsys):
    root = _project(tmp_path, monkeypatch)
    (root / "packages.yml").write_text(
        yaml.safe_dump({"packages": [{"local": "../tiny_pkg"}]})
    )
    (root / "models" / "pkg_task.sql").write_text(REPAIRED)
    config = yaml.safe_load((root / "dbt_project.yml").read_text())
    config["models"] = {"tiny_pkg": {"pkg_task": {"+enabled": False}}}
    (root / "dbt_project.yml").write_text(yaml.safe_dump(config, sort_keys=False))

    built = _build(capsys, "--cold-deps")

    assert built["status"] == "ok", built["errors"]
    report = built["data"]["cold_verification"]
    assert report["installed_edits"]["not_compared"] == ["tiny_pkg"]
    assert any("cannot detect" in note for note in report["notes"])


def test_cold_artifacts_stay_apart_from_the_last_ordinary_build(project, capsys):
    from exmergo_dex_core.transform.artifact_runtime import readable_artifact_dir

    _override(project, capsys)
    assert _build(capsys)["status"] == "ok"
    ordinary = readable_artifact_dir(project)

    built = _build(capsys, "--cold-deps")

    assert built["status"] == "ok", built["errors"]
    assert readable_artifact_dir(project) == ordinary
    label = built["data"]["cold_verification"]["artifact_directory"]
    assert Path(label).name == "cold"
    assert (project / label / "manifest.json").is_file()
    assert (project / label / "logs" / "dbt.log").is_file()


def test_a_cold_build_refuses_a_prod_target_before_reinstalling(project, capsys):
    refused = _dex(capsys, "transform", "build", "--target", "prod", "--cold-deps")
    assert refused["status"] == "error"
    assert not (project / "dbt_packages").exists()


def test_a_prose_population_is_named_apart_from_a_measured_one(project, capsys):
    _override(project, capsys)
    payload = {
        "edits": [
            {"path": "models/rpt.sql", "kind": "model_sql", "content": REPORT + "\n"}
        ],
        "declarations": [
            {
                "model": "rpt",
                "description": "One row per task.",
                "grain": ["id"],
                "population": {"rule": "One row per task that has a timestamp."},
            }
        ],
    }
    edits = project / "edits.json"
    edits.write_text(json.dumps(payload))
    planned = _dex(capsys, "transform", "plan", "Declare", "--edits-file", str(edits))
    assert planned["data"]["declarations"]["rpt"]["population_contract"] == "prose"
    plan_id = planned["data"]["plan_id"]
    assert _dex(capsys, "transform", "apply", plan_id)["status"] == "ok"

    built = _build(capsys, "--for-plan", plan_id, "--verify")

    obligations = built["data"]["completion"]["obligations"]
    assert obligations["prose_population"]["names"] == ["rpt"]


def test_a_folder_named_logs_under_models_is_built_cold(project, capsys):
    _override(project, capsys)
    folder = project / "models" / "logs"
    folder.mkdir()
    (folder / "stg_logs.sql").write_text("select 1 as id\n")

    built = _build(capsys, "--cold-deps")

    assert built["status"] == "ok", built["errors"]
    assert "stg_logs" in {node["name"] for node in built["data"]["nodes"]}


def test_an_edit_in_a_packages_logs_folder_is_seen(project, capsys):
    assert _dex(capsys, "transform", "deps")["status"] == "ok"
    extra = project / "dbt_packages" / "tiny_pkg" / "models" / "logs" / "extra.sql"
    extra.parent.mkdir(parents=True)
    extra.write_text("select 1 as id\n")

    report = _build(capsys, "--cold-deps")["data"]["cold_verification"]

    assert report["installed_edits"]["added"]["paths"] == [
        "tiny_pkg/models/logs/extra.sql"
    ]


def test_a_custom_install_path_is_compared_and_not_copied(project, capsys):
    config = yaml.safe_load((project / "dbt_project.yml").read_text())
    config["packages-install-path"] = "vendor/dbt"
    (project / "dbt_project.yml").write_text(yaml.safe_dump(config, sort_keys=False))
    assert _dex(capsys, "transform", "deps")["status"] == "ok"
    installed = project / "vendor" / "dbt" / "tiny_pkg" / "models" / "pkg_task.sql"
    installed.write_text(REPAIRED)

    built = _build(capsys, "--cold-deps")

    # The hand edit stayed out of the copy, so the package's own failure shows.
    assert built["status"] == "error"
    assert built["data"]["cold_verification"]["installed_edits"]["changed"][
        "paths"
    ] == ["tiny_pkg/models/pkg_task.sql"]


@pytest.mark.parametrize("spelling", ["absolute", "climbing"])
def test_an_install_path_outside_the_project_is_refused_untouched(
    project, capsys, spelling
):
    outside = project.parent / "shared_pkgs"
    (outside / "tiny_pkg").mkdir(parents=True)
    kept = outside / "tiny_pkg" / "kept.sql"
    kept.write_text("hand edit\n")
    config = yaml.safe_load((project / "dbt_project.yml").read_text())
    config["packages-install-path"] = (
        str(outside)
        if spelling == "absolute"
        else f"{'../' * 40}{outside.relative_to('/')}"
    )
    (project / "dbt_project.yml").write_text(yaml.safe_dump(config, sort_keys=False))

    built = _build(capsys, "--cold-deps")

    assert built["status"] == "error"
    cold = built["data"]["cold_verification"]
    assert cold["reinstalled"] is False
    assert "packages-install-path" in cold["reason"]
    assert kept.read_text() == "hand edit\n"
    assert sorted(p.name for p in outside.iterdir()) == ["tiny_pkg"]
    assert not (project / "dev.duckdb").exists()


def test_a_templated_install_path_is_refused_untouched(project, capsys, monkeypatch):
    outside = project.parent / "rendered_pkgs"
    (outside / "tiny_pkg").mkdir(parents=True)
    kept = outside / "tiny_pkg" / "kept.sql"
    kept.write_text("hand edit\n")
    monkeypatch.setenv("DEX_TEST_PKGS", str(outside))
    config = yaml.safe_load((project / "dbt_project.yml").read_text())
    config["packages-install-path"] = "{{ env_var('DEX_TEST_PKGS') }}"
    (project / "dbt_project.yml").write_text(yaml.safe_dump(config, sort_keys=False))

    built = _build(capsys, "--cold-deps")

    assert built["status"] == "error"
    cold = built["data"]["cold_verification"]
    assert cold["reinstalled"] is False
    assert "templated" in cold["reason"]
    assert kept.read_text() == "hand edit\n"
    assert sorted(p.name for p in outside.iterdir()) == ["tiny_pkg"]
    assert not (project / "dev.duckdb").exists()


def test_a_relative_local_package_keeps_the_lockfile_of_the_others(
    tmp_path, monkeypatch, capsys
):
    root = _project(tmp_path, monkeypatch)
    spec = yaml.safe_load((root / "packages.yml").read_text())
    shared = tmp_path / "shared_pkg"
    (shared / "models").mkdir(parents=True)
    (shared / "dbt_project.yml").write_text(
        "name: shared_pkg\nversion: '1.0'\nconfig-version: 2\nmodel-paths: [models]\n"
    )
    spec["packages"].append({"local": "../shared_pkg"})
    (root / "packages.yml").write_text(yaml.safe_dump(spec))
    assert _dex(capsys, "transform", "deps")["status"] == "ok"
    lock = (root / "package-lock.yml").read_bytes()
    _override(root, capsys)

    report = _build(capsys, "--cold-deps")["data"]["cold_verification"]

    assert report["reinstalled"] is True
    assert report["installed_edits"]["not_compared"] == ["shared_pkg"]
    assert not any("afresh" in note for note in report.get("notes", []))
    assert (root / "package-lock.yml").read_bytes() == lock


def test_a_failed_reinstall_names_the_required_deliverables(project, capsys):
    plan_id = _override(project, capsys)
    (project / "packages.yml").write_text(
        yaml.safe_dump(
            {"packages": [{"git": f"file://{project}/absent", "revision": "abc"}]}
        )
    )
    (project / "package-lock.yml").unlink()

    built = _build(capsys, "--cold-deps", "--for-plan", plan_id)

    deliverables = built["data"]["completion"]["deliverables"]
    assert "pkg_task" in deliverables["missing"]["names"]
