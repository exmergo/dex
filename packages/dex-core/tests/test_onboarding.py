"""Composed setup behavior: real files, bounded writes and truthful readiness."""

from __future__ import annotations

import json

import pytest
import yaml

from exmergo_dex_core import DexEngine, procedure
from exmergo_dex_core.cli import main
from exmergo_dex_core.onboarding import setup_envelope
from exmergo_dex_core.onboarding_docs import ENTERPRISE, README_BEGIN, README_END

pytestmark = pytest.mark.live_open_adapter


def run(root, capsys, *args):
    code = main(["--repo-root", str(root), "init", *args])
    output = capsys.readouterr()
    assert output.out.count("\n") == 1
    return code, json.loads(output.out)


def config(root):
    return yaml.safe_load((root / ".dex/config.yml").read_text(encoding="utf-8"))


def test_noninteractive_empty_setup_is_actionable_without_writes(tmp_path, capsys):
    code, result = run(tmp_path, capsys)
    assert code == 1
    assert result["data"]["missing_inputs"][0]["name"] == "mode"
    assert not list(tmp_path.iterdir())


def test_standalone_semantic_setup_needs_no_warehouse(tmp_path, capsys, monkeypatch):
    (tmp_path / ".dex").mkdir()
    original = "semantic:\n  vendor: ossie\n  ossie:\n    files: [shop.ossie.json]\n"
    (tmp_path / ".dex/config.yml").write_text(original)

    def forbidden(*args, **kwargs):
        pytest.fail("standalone semantics opened a warehouse")

    monkeypatch.setattr(DexEngine, "connect_test", forbidden)
    code, result = run(tmp_path, capsys)
    assert code == 0, result
    assert result["data"]["readiness"]["connection"]["status"] == "not_applicable"
    assert result["data"]["next_steps"][0]["command"] == "dex explore semantic list"
    assert result["cost"]["paradigm"] is None
    assert (tmp_path / ".dex/config.yml").read_text(encoding="utf-8") == original
    assert not list(tmp_path.rglob("dbt_project.yml"))


def test_adoption_honors_existing_nonproduction_default(tmp_path, capsys, duckdb_file):
    (tmp_path / "dbt_project.yml").write_text("name: existing\n")
    (tmp_path / "profiles.yml").write_text(
        yaml.safe_dump(
            {
                "existing": {
                    "target": "development",
                    "outputs": {
                        "development": {"type": "duckdb", "path": str(duckdb_file)}
                    },
                }
            }
        )
    )
    code, result = run(tmp_path, capsys)
    assert code == 0, result
    assert config(tmp_path)["dbt_target"] == "development"
    assert "--target development" in (tmp_path / "README.md").read_text(
        encoding="utf-8"
    )


@pytest.mark.parametrize("name", ["analytics", "."])
def test_rerun_completes_missing_generated_profile_and_directories(
    tmp_path, capsys, name
):
    code, result = run(tmp_path, capsys, name, "--mode", "demo")
    assert code == 0, result
    project = tmp_path / ("analytics" if name == "analytics" else ".")
    original_profile = (project / "profiles.yml").read_bytes()
    (project / "profiles.yml").unlink()
    (project / "models/staging/.gitkeep").unlink()
    (project / "models/staging").rmdir()
    code, repeated = run(tmp_path, capsys)
    assert code == 0, repeated
    assert (project / "profiles.yml").read_bytes() == original_profile
    assert (project / "models/staging/.gitkeep").exists()


def test_resume_keeps_in_place_layout_when_project_file_is_missing(tmp_path, capsys):
    code, result = run(tmp_path, capsys, ".", "--mode", "demo")
    assert code == 0, result
    original = (tmp_path / "dbt_project.yml").read_bytes()
    (tmp_path / "dbt_project.yml").unlink()
    code, result = run(tmp_path, capsys)
    assert code == 0, result
    assert config(tmp_path)["dbt_project_dir"] == "."
    assert (tmp_path / "dbt_project.yml").read_bytes() == original


def test_complete_demo_and_repeat_preserve_authored_content(tmp_path, capsys):
    code, result = run(tmp_path, capsys, "analytics", "--mode", "demo")
    assert code == 0, result
    assert result["data"]["readiness"]["connection"]["status"] == "passed"
    assert result["data"]["readiness"]["build"]["status"] == "unchecked"
    assert (tmp_path / "analytics/dbt_project.yml").is_file()
    assert config(tmp_path)["dbt_project_dir"] == "analytics"
    assert result["connection"]["connector"] == "duckdb"
    readme = tmp_path / "README.md"
    assert readme.read_text(encoding="utf-8").startswith("# Dex analytics\n")
    readme.write_text(
        "Authored before\n" + readme.read_text(encoding="utf-8") + "Authored after\n",
        encoding="utf-8",
    )
    agents = tmp_path / "AGENTS.md"
    agents.write_text(
        "Team instructions\n" + agents.read_text(encoding="utf-8"), encoding="utf-8"
    )
    model = tmp_path / "analytics/models/marts/authored.sql"
    model.write_text("select 7 as id\n")
    warehouse_stat = (tmp_path / "dex_demo.duckdb").stat()
    code, repeat = run(tmp_path, capsys)
    assert code == 0, repeat
    assert readme.read_text(encoding="utf-8").count(README_BEGIN) == 1
    assert readme.read_text(encoding="utf-8").startswith("Authored before\n")
    assert readme.read_text(encoding="utf-8").endswith("Authored after\n")
    assert ENTERPRISE in readme.read_text(encoding="utf-8")
    assert agents.read_text(encoding="utf-8").startswith("Team instructions\n")
    assert (
        procedure.check(agents.read_text(encoding="utf-8"))
        is procedure.CopyStatus.CURRENT
    )
    assert model.read_text(encoding="utf-8") == "select 7 as id\n"
    assert (
        tmp_path / "dex_demo.duckdb"
    ).stat().st_mtime_ns == warehouse_stat.st_mtime_ns
    assert repeat["data"]["created"] == []


@pytest.mark.parametrize(
    "name,extra,project",
    [
        ("analytics", [], "analytics"),
        (".", [], "."),
        ("analytics", ["--in-place"], "."),
    ],
)
def test_new_layout_and_missing_credentials_are_explicit(
    tmp_path, capsys, name, extra, project
):
    code, result = run(
        tmp_path,
        capsys,
        name,
        "--connector",
        "duckdb",
        "--path",
        "missing.duckdb",
        *extra,
    )
    assert code == 1
    assert config(tmp_path)["dbt_project_dir"] == project
    assert (tmp_path / project / "dbt_project.yml").exists()
    assert result["data"]["readiness"]["configuration"]["status"] == "passed"
    assert result["data"]["readiness"]["connection"]["status"] == "failed"
    assert "dex connect test" in (tmp_path / "README.md").read_text(encoding="utf-8")
    assert not (tmp_path / "missing.duckdb").exists()


def test_adoption_preserves_authored_dbt_files_and_global_profile(
    tmp_path, capsys, duckdb_file, monkeypatch
):
    project = tmp_path / "authored"
    project.mkdir()
    project_text = (
        "# authored café project\n"
        "name: authored\nprofile: existing\nmodel-paths: [sql]\n"
    )
    (project / "dbt_project.yml").write_text(project_text, encoding="utf-8")
    profiles = tmp_path / "local-profiles"
    profiles.mkdir()
    profile_text = yaml.safe_dump(
        {
            "existing": {
                "target": "dev",
                "outputs": {"dev": {"type": "duckdb", "path": str(duckdb_file)}},
            }
        }
    )
    (profiles / "profiles.yml").write_text(profile_text)
    monkeypatch.setenv("DBT_PROFILES_DIR", str(profiles))
    (tmp_path / "README.md").write_text("# Our project\n\nKeep this byte for byte.\n")
    for agent in ("AGENTS.md", "CLAUDE.md"):
        (tmp_path / agent).write_text("# Team guidance\n")
    code, result = run(tmp_path, capsys)
    assert code == 0, result
    assert result["data"]["mode"] == "adopt"
    assert config(tmp_path)["dbt_project_dir"] == "authored"
    assert (project / "dbt_project.yml").read_text(encoding="utf-8") == project_text
    assert (profiles / "profiles.yml").read_text(encoding="utf-8") == profile_text
    assert not (project / "profiles.yml").exists()
    assert (
        (tmp_path / "README.md")
        .read_text(encoding="utf-8")
        .startswith("# Our project\n")
    )
    for agent in ("AGENTS.md", "CLAUDE.md"):
        assert (
            (tmp_path / agent)
            .read_text(encoding="utf-8")
            .startswith("# Team guidance\n")
        )
        assert (
            procedure.check((tmp_path / agent).read_text(encoding="utf-8"))
            is procedure.CopyStatus.CURRENT
        )


def test_exploration_without_dbt_and_optional_instructions(
    tmp_path, capsys, duckdb_file
):
    code, result = run(
        tmp_path,
        capsys,
        "--mode",
        "explore",
        "--connector",
        "duckdb",
        "--path",
        str(duckdb_file),
        "--no-agent-instructions",
    )
    assert code == 0, result
    assert result["data"]["project_dir"] is None
    assert result["data"]["readiness"]["build"]["status"] == "not_applicable"
    assert not list(tmp_path.rglob("dbt_project.yml"))
    assert not (tmp_path / "AGENTS.md").exists()


def test_standalone_semantics_are_preserved_without_dbt(tmp_path, capsys):
    # Discovery does not need to parse or rewrite the semantic definition.
    semantic = tmp_path / "shop.ossie.json"
    semantic.write_text('{"authored": true}\n')
    code, result = run(
        tmp_path, capsys, "--connector", "duckdb", "--path", "not-yet.duckdb"
    )
    assert code == 1
    assert result["data"]["mode"] == "explore"
    assert semantic.read_text(encoding="utf-8") == '{"authored": true}\n'
    assert not list(tmp_path.rglob("dbt_project.yml"))


def test_existing_config_requires_confirmation_before_any_write(tmp_path, capsys):
    (tmp_path / ".dex").mkdir()
    original = (
        "# Preserve my choices\nconnector: duckdb\nduckdb:\n"
        "  path: old.duckdb\nbudget:\n  ceiling: 123456\n"
    )
    (tmp_path / ".dex/config.yml").write_text(original)
    code, result = run(tmp_path, capsys, "analytics", "--path", "new.duckdb")
    assert code == 0
    assert result["status"] == "needs_confirmation"
    assert (tmp_path / ".dex/config.yml").read_text(encoding="utf-8") == original
    assert not (tmp_path / "analytics").exists()
    assert not (tmp_path / "README.md").exists()
    code, result = run(
        tmp_path, capsys, "analytics", "--path", "new.duckdb", "--confirm"
    )
    assert code == 1  # No warehouse exists; configuration is still created.
    assert config(tmp_path)["duckdb"]["path"] == "new.duckdb"
    assert config(tmp_path)["budget"] == {"ceiling": 123456}
    assert "# Preserve my choices" in (tmp_path / ".dex/config.yml").read_text(
        encoding="utf-8"
    )


@pytest.mark.parametrize(
    "document", [README_BEGIN, README_END + README_BEGIN, README_BEGIN * 2 + README_END]
)
def test_malformed_readme_is_refused_before_writes(tmp_path, capsys, document):
    (tmp_path / "README.md").write_text(document, encoding="utf-8")
    code, result = run(tmp_path, capsys, "analytics", "--mode", "demo")
    assert code == 1
    assert "malformed" in result["errors"][0]
    assert not (tmp_path / "analytics").exists()
    assert not (tmp_path / "dex_demo.duckdb").exists()
    assert (tmp_path / "README.md").read_text(encoding="utf-8") == document


def test_ambiguous_projects_require_selection(tmp_path, capsys):
    for name in ("one", "two"):
        project = tmp_path / name
        project.mkdir()
        (project / "dbt_project.yml").write_text(f"name: {name}\n")
    code, result = run(tmp_path, capsys)
    assert code == 1
    assert result["data"]["missing_inputs"][0]["choices"] == ["one", "two"]
    assert not (tmp_path / ".dex/config.yml").exists()


@pytest.mark.parametrize(
    "flag",
    [["--session-ceiling", "100"], ["--no-session-ceiling"], ["--budget", "100"]],
)
def test_setup_never_answers_spending_questions(tmp_path, capsys, flag):
    code, result = run(tmp_path, capsys, "analytics", "--mode", "demo", *flag)
    assert code == 1
    assert "spending" in result["errors"][0]
    assert not list(tmp_path.iterdir())


def test_setup_never_scans_builds_or_accepts_a_snapshot(tmp_path, capsys, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("setup ran an analytics workflow")

    for method in ("map", "profile", "query", "build", "snapshot"):
        monkeypatch.setattr(DexEngine, method, forbidden)
    code, result = run(tmp_path, capsys, "analytics", "--mode", "demo")
    assert code == 0, result


def test_library_and_cli_share_setup(tmp_path, capsys):
    cli_root, library_root = tmp_path / "cli", tmp_path / "library"
    cli_root.mkdir()
    library_root.mkdir()
    code, result = run(cli_root, capsys, "analytics", "--mode", "demo")
    assert code == 0, result
    with DexEngine.from_repo(library_root) as engine:
        actual = setup_envelope(engine.initialize("analytics", mode="demo"))
    for field in ("mode", "project_dir", "connector", "readiness", "created"):
        assert actual.data[field] == result["data"][field]


def test_tty_wizard_and_completion_stay_off_stdout(tmp_path, capsys, monkeypatch):
    import sys

    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stderr, "isatty", lambda: True)
    answers = iter(["2", "analytics"])
    monkeypatch.setattr("builtins.input", lambda: next(answers))
    code = main(["--repo-root", str(tmp_path), "init"])
    output = capsys.readouterr()
    assert code == 0, output.out
    assert json.loads(output.out)["data"]["mode"] == "demo"
    assert output.out.count("\n") == 1
    assert "Create a Dex project (recommended)" in output.err
    assert ENTERPRISE in output.err


def test_terminal_cancellation_is_clean(tmp_path, capsys, monkeypatch):
    import sys

    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stderr, "isatty", lambda: True)

    def cancel():
        raise KeyboardInterrupt

    monkeypatch.setattr("builtins.input", cancel)
    code, result = run(tmp_path, capsys)
    assert code == 1
    assert result["data"]["cancelled"] is True
    assert not list(tmp_path.iterdir())


def test_instruction_opt_out_and_missing_setup_artifacts_survive_reruns(
    tmp_path, capsys
):
    code, result = run(
        tmp_path, capsys, "analytics", "--mode", "demo", "--no-agent-instructions"
    )
    assert code == 0, result
    (tmp_path / "README.md").unlink()
    (tmp_path / "analytics/dbt_project.yml").unlink()
    profile = tmp_path / "analytics/profiles.yml"
    authored_profile = profile.read_text(encoding="utf-8") + "# preserved\n"
    profile.write_text(authored_profile)
    code, repeat = run(tmp_path, capsys)
    assert code == 0, repeat
    assert profile.read_text(encoding="utf-8") == authored_profile
    assert not (tmp_path / "AGENTS.md").exists()
    assert (tmp_path / "README.md").exists()
    assert (tmp_path / "analytics/dbt_project.yml").exists()


def test_invalid_project_selection_does_not_write(tmp_path, capsys):
    code, result = run(
        tmp_path,
        capsys,
        "--mode",
        "adopt",
        "--project-dir",
        "missing",
        "--connector",
        "duckdb",
        "--path",
        "missing.duckdb",
    )
    assert code == 1
    assert "existing dbt project" in result["errors"][0]
    assert not list(tmp_path.iterdir())


def test_connector_conflict_is_refused_before_adoption(tmp_path, capsys):
    (tmp_path / "dbt_project.yml").write_text("name: authored\n")
    (tmp_path / "profiles.yml").write_text(
        "authored:\n  outputs:\n    dev:\n"
        "      type: duckdb\n      path: warehouse.duckdb\n"
    )
    code, result = run(tmp_path, capsys, "--connector", "bigquery")
    assert code == 1
    assert "conflicts" in result["errors"][0]
    assert not (tmp_path / ".dex/config.yml").exists()


def test_readme_changed_during_connection_check_is_preserved(
    tmp_path, capsys, monkeypatch
):
    from exmergo_dex_core import onboarding

    original = onboarding._verify

    def edit_during_check(engine, cfg, root, result):
        original(engine, cfg, root, result)
        (root / "README.md").write_text("# Concurrent human edit\n")

    monkeypatch.setattr(onboarding, "_verify", edit_during_check)
    code, result = run(tmp_path, capsys, "analytics", "--mode", "demo")
    assert code == 1
    assert (tmp_path / "README.md").read_text(
        encoding="utf-8"
    ) == "# Concurrent human edit\n"
    assert any(
        "changed during setup" in problem for problem in result["data"]["problems"]
    )


def test_values_free_library_and_cli_refuse_setup_before_writes(tmp_path, capsys):
    code, result = run(tmp_path, capsys, "analytics", "--mode", "demo", "--values-free")
    assert code == 1
    assert result["data"]["result_mode"] == "values_free"
    with (
        DexEngine.from_repo(tmp_path, result_mode="values_free") as engine,
        pytest.raises(Exception, match=r"values.free"),
    ):
        engine.initialize("analytics", mode="demo")
    assert not list(tmp_path.iterdir())


def test_tty_adoption_from_relative_root(tmp_path, capsys, monkeypatch, duckdb_file):
    import sys

    (tmp_path / "dbt_project.yml").write_text("name: authored\n")
    (tmp_path / "profiles.yml").write_text(
        yaml.safe_dump(
            {
                "authored": {
                    "target": "dev",
                    "outputs": {"dev": {"type": "duckdb", "path": str(duckdb_file)}},
                }
            }
        )
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stderr, "isatty", lambda: True)
    answers = iter(["no"])
    monkeypatch.setattr("builtins.input", lambda: next(answers))
    code = main(["--repo-root", ".", "init"])
    output = capsys.readouterr()
    result = json.loads(output.out)
    assert code == 0, result
    assert result["data"]["mode"] == "adopt"
    # The one question an adoption asks for a known path is the Elementary
    # install (#1464); declining it stores no plan.
    assert output.err.count("(yes/no)") == 1
    assert "Elementary package" in output.err
    assert result["data"]["elementary"]["install"] == "declined"
    assert not list((tmp_path / ".dex").glob("plans/*"))


def test_discovery_resolves_a_symlinked_root(tmp_path, symlinks):
    from exmergo_dex_core.onboarding import discover

    actual = tmp_path / "actual"
    actual.mkdir()
    (actual / "dbt_project.yml").write_text("name: authored\n")
    linked = tmp_path / "linked"
    linked.symlink_to(actual, target_is_directory=True)
    assert discover(linked)["projects"] == ["."]


def test_tty_configuration_review_prints_unified_diff(tmp_path, capsys, monkeypatch):
    import sys

    (tmp_path / ".dex").mkdir()
    original = "connector: duckdb\nduckdb:\n  path: old.duckdb\n"
    (tmp_path / ".dex/config.yml").write_text(original)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stderr, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda: "no")
    code = main(
        ["--repo-root", str(tmp_path), "init", "analytics", "--path", "new.duckdb"]
    )
    output = capsys.readouterr()
    assert code == 0
    assert json.loads(output.out)["status"] == "needs_confirmation"
    assert "--- a/.dex/config.yml\n+++ b/.dex/config.yml\n" in output.err
    assert "{'path':" not in output.err
    assert (tmp_path / ".dex/config.yml").read_text(encoding="utf-8") == original
    assert not (tmp_path / "analytics").exists()


@pytest.mark.parametrize("metadata", ["", "[]\n", "name: null\n", "name: [broken\n"])
def test_invalid_existing_project_metadata_is_actionable_before_writes(
    tmp_path, capsys, metadata
):
    (tmp_path / "dbt_project.yml").write_text(metadata)
    code, result = run(
        tmp_path, capsys, "--connector", "duckdb", "--path", "missing.duckdb"
    )
    assert code == 1
    assert "dbt_project.yml" in result["errors"][0]
    assert not (tmp_path / ".dex/config.yml").exists()
    assert not (tmp_path / "README.md").exists()


# --- the Elementary install (#1464) --------------------------------------------


def _adoptable(root, duckdb_file, *, packages=None, installed=None):
    (root / "dbt_project.yml").write_text(
        "name: authored\nprofile: authored\n# kept as written\n"
    )
    (root / "profiles.yml").write_text(
        yaml.safe_dump(
            {
                "authored": {
                    "target": "dev",
                    "outputs": {"dev": {"type": "duckdb", "path": str(duckdb_file)}},
                }
            }
        )
    )
    if packages is not None:
        (root / "packages.yml").write_text(packages)
    if installed is not None:
        package = root / "dbt_packages/elementary"
        package.mkdir(parents=True)
        (package / "dbt_project.yml").write_text(
            f"name: elementary\nversion: {installed}\n"
        )


def _plans(root):
    return sorted((root / ".dex/plans").glob("*.json"))


@pytest.mark.parametrize("mode", ["new", "demo"])
def test_a_new_project_declares_elementary_in_its_own_diff(
    tmp_path, capsys, duckdb_file, mode
):
    from exmergo_dex_core.transform.elementary_install import INSTALL_RELEASE

    args = ["analytics", "--mode", mode]
    if mode == "new":
        args += ["--connector", "duckdb", "--path", str(duckdb_file)]
    code, result = run(tmp_path, capsys, *args)
    assert code == 0, result
    packages = yaml.safe_load(
        (tmp_path / "analytics/packages.yml").read_text(encoding="utf-8")
    )
    assert packages == {
        "packages": [
            {"package": "elementary-data/elementary", "version": INSTALL_RELEASE}
        ]
    }
    project = yaml.safe_load(
        (tmp_path / "analytics/dbt_project.yml").read_text(encoding="utf-8")
    )
    assert project["models"]["elementary"] == {
        "+schema": "elementary",
        "+enabled": "{{ target.name != 'dev' }}",
    }
    assert project["vars"] == {"test_sample_row_count": 0}
    assert {d["path"] for d in result["diffs"]} >= {
        "analytics/packages.yml",
        "analytics/dbt_project.yml",
    }
    elementary = result["data"]["elementary"]
    assert elementary["install"] == "written"
    assert elementary["declared"] and not elementary["installed"]
    commands = [step["command"] for step in result["data"]["next_steps"]]
    assert "dex transform deps" in commands
    assert "dbt run --select elementary --target <production target>" in commands
    assert not _plans(tmp_path)
    # A resumed setup reproduces the choice rather than revisiting it.
    assert json.loads((tmp_path / ".dex/setup.json").read_text(encoding="utf-8"))[
        "elementary"
    ]


def test_no_elementary_leaves_a_new_project_without_the_package(
    tmp_path, capsys, duckdb_file
):
    code, result = run(
        tmp_path,
        capsys,
        "analytics",
        "--mode",
        "new",
        "--connector",
        "duckdb",
        "--path",
        str(duckdb_file),
        "--no-elementary",
    )
    assert code == 0, result
    assert not (tmp_path / "analytics/packages.yml").exists()
    project = yaml.safe_load(
        (tmp_path / "analytics/dbt_project.yml").read_text(encoding="utf-8")
    )
    assert "models" not in project and "vars" not in project
    assert result["data"]["elementary"]["install"] == "declined"
    assert not any(
        "elementary" in step["command"] for step in result["data"]["next_steps"]
    )


def test_adopting_a_project_without_elementary_stores_the_install_plan(
    tmp_path, capsys, duckdb_file
):
    _adoptable(tmp_path, duckdb_file)
    before = (tmp_path / "dbt_project.yml").read_text(encoding="utf-8")
    code, result = run(tmp_path, capsys)
    assert code == 0, result
    elementary = result["data"]["elementary"]
    assert elementary["declared"] is False and elementary["installed"] is False
    assert elementary["install"] == "planned"
    assert [d["path"] for d in elementary["plan_diffs"]] == [
        "packages.yml",
        "dbt_project.yml",
    ]
    # Init changed neither file; the plan holds them.
    assert (tmp_path / "dbt_project.yml").read_text(encoding="utf-8") == before
    assert not (tmp_path / "packages.yml").exists()
    [stored] = _plans(tmp_path)
    plan = json.loads(stored.read_text(encoding="utf-8"))
    assert plan["plan_id"] == elementary["plan_id"]
    assert [(e["path"], e["kind"]) for e in plan["edits"]] == [
        ("packages.yml", "packages_yml"),
        ("dbt_project.yml", "project_yml"),
    ]
    commands = [step["command"] for step in result["data"]["next_steps"]]
    assert f"dex transform apply {elementary['plan_id']}" in commands

    # A second init finds the plan it stored rather than storing another.
    code, again = run(tmp_path, capsys)
    assert code == 0, again
    assert again["data"]["elementary"]["plan_id"] == elementary["plan_id"]
    assert len(again["data"]["elementary"]["plan_diffs"]) == 2
    assert len(_plans(tmp_path)) == 1

    # The plan is one that changes what runs.
    code = main(
        ["--repo-root", str(tmp_path), "transform", "classify", elementary["plan_id"]]
    )
    classified = json.loads(capsys.readouterr().out)
    assert code == 0, classified
    by_path = {c["path"]: c for c in classified["data"]["classifications"]}
    assert by_path["packages.yml"]["artifact_class"] == "executable"
    assert [s["signal"] for s in by_path["packages.yml"]["signals"]] == [
        "dependency_hub"
    ]

    # Applied, the project declares the package and keeps what was written.
    code = main(
        ["--repo-root", str(tmp_path), "transform", "apply", elementary["plan_id"]]
    )
    applied = json.loads(capsys.readouterr().out)
    assert code == 0, applied
    assert "# kept as written" in (tmp_path / "dbt_project.yml").read_text(
        encoding="utf-8"
    )
    code, after = run(tmp_path, capsys)
    assert code == 0, after
    assert after["data"]["elementary"]["install"] == "present"
    assert after["data"]["elementary"]["declared"] is True
    assert "dex transform deps" in [
        step["command"] for step in after["data"]["next_steps"]
    ]


def test_declining_on_an_existing_project_stores_no_plan(tmp_path, capsys, duckdb_file):
    _adoptable(tmp_path, duckdb_file)
    code, result = run(tmp_path, capsys, "--no-elementary")
    assert code == 0, result
    assert result["data"]["elementary"]["install"] == "declined"
    assert result["data"]["elementary"]["plan_id"] is None
    assert not _plans(tmp_path)
    assert any(
        step["command"] == "dex init" and "--no-elementary" in step["purpose"]
        for step in result["data"]["next_steps"]
    )


def test_a_project_with_elementary_installed_reports_its_release(
    tmp_path, capsys, duckdb_file
):
    _adoptable(
        tmp_path,
        duckdb_file,
        packages="packages:\n  - package: elementary-data/elementary\n"
        "    version: 0.23.0\n",
        installed="0.23.0",
    )
    code, result = run(tmp_path, capsys)
    assert code == 0, result
    elementary = result["data"]["elementary"]
    assert elementary["install"] == "present"
    assert elementary["installed"] is True
    assert elementary["release"] == "0.23.0"
    assert not _plans(tmp_path)
    assert not any(
        "elementary" in step["command"] or step["command"] == "dex transform deps"
        for step in result["data"]["next_steps"]
    )


def test_an_unwritable_packages_file_offers_nothing_and_says_why(
    tmp_path, capsys, duckdb_file
):
    _adoptable(
        tmp_path,
        duckdb_file,
        packages="packages: [{package: dbt-labs/dbt_utils, version: 1.3.0}]\n",
    )
    code, result = run(tmp_path, capsys)
    assert code == 0, result
    elementary = result["data"]["elementary"]
    assert elementary["install"] == "not_offered"
    assert "by hand" in elementary["reason"]
    assert not _plans(tmp_path)


def test_the_production_steps_name_the_configured_production_target(
    tmp_path, capsys, duckdb_file
):
    _adoptable(tmp_path, duckdb_file)
    (tmp_path / ".dex").mkdir()
    (tmp_path / ".dex/config.yml").write_text(
        f"connector: duckdb\nduckdb:\n  path: {duckdb_file}\ndbt_prod_target: prod\n"
    )
    code, result = run(tmp_path, capsys, "--confirm")
    assert code == 0, result
    assert result["data"]["elementary"]["install"] == "planned"
    assert "dbt run --select elementary --target prod" in [
        step["command"] for step in result["data"]["next_steps"]
    ]


def test_no_elementary_wins_over_a_recorded_choice_when_recovering(
    tmp_path, capsys, duckdb_file
):
    code, result = run(
        tmp_path,
        capsys,
        "analytics",
        "--mode",
        "new",
        "--connector",
        "duckdb",
        "--path",
        str(duckdb_file),
    )
    assert code == 0, result
    (tmp_path / "analytics/packages.yml").unlink()
    code, result = run(tmp_path, capsys, "--no-elementary")
    assert code == 0, result
    assert not (tmp_path / "analytics/packages.yml").exists()


def test_an_installed_package_declared_in_an_unread_form_is_not_offered_again(
    tmp_path, capsys, duckdb_file
):
    _adoptable(
        tmp_path,
        duckdb_file,
        packages="packages:\n  - tarball: https://example.com/elementary.tgz\n",
        installed="0.26.1",
    )
    code, result = run(tmp_path, capsys)
    assert code == 0, result
    assert result["data"]["elementary"]["install"] == "present"
    assert not _plans(tmp_path)


def test_a_partly_applied_install_is_reused_by_its_effective_edits(
    tmp_path, capsys, duckdb_file
):
    from exmergo_dex_core.transform.elementary_install import project_yml

    _adoptable(tmp_path, duckdb_file)
    project = tmp_path / "dbt_project.yml"
    project.write_text(
        project_yml(project.read_text(encoding="utf-8"), "dev"), encoding="utf-8"
    )
    code, first = run(tmp_path, capsys)
    assert code == 0, first
    assert [d["path"] for d in first["data"]["elementary"]["plan_diffs"]] == [
        "packages.yml"
    ]
    code, again = run(tmp_path, capsys)
    assert code == 0, again
    assert (
        again["data"]["elementary"]["plan_id"] == first["data"]["elementary"]["plan_id"]
    )
    assert len(_plans(tmp_path)) == 1


def test_an_install_plan_for_one_project_is_not_reused_for_another(
    tmp_path, capsys, duckdb_file
):
    for name in ("one", "two"):
        (tmp_path / name).mkdir()
        _adoptable(tmp_path / name, duckdb_file)
    code, first = run(tmp_path, capsys, "--project-dir", "one")
    assert code == 0, first
    code, second = run(tmp_path, capsys, "--project-dir", "two", "--confirm")
    assert code == 0, second
    assert second["data"]["project_dir"] == "two"
    assert second["data"]["elementary"]["install"] == "planned"
    assert (
        second["data"]["elementary"]["plan_id"]
        != first["data"]["elementary"]["plan_id"]
    )
    plans = [json.loads(p.read_text(encoding="utf-8")) for p in _plans(tmp_path)]
    assert sorted(p["project_dir"] for p in plans) == ["one", "two"]


def test_recovery_does_not_recreate_packages_yml_beside_dependencies_yml(
    tmp_path, capsys, duckdb_file
):
    code, result = run(
        tmp_path,
        capsys,
        "analytics",
        "--mode",
        "new",
        "--connector",
        "duckdb",
        "--path",
        str(duckdb_file),
    )
    assert code == 0, result
    # The project moved its package list: dbt refuses `packages:` in both.
    (tmp_path / "analytics/packages.yml").rename(
        tmp_path / "analytics/dependencies.yml"
    )
    code, result = run(tmp_path, capsys)
    assert code == 0, result
    assert not (tmp_path / "analytics/packages.yml").exists()
    assert "packages" in yaml.safe_load(
        (tmp_path / "analytics/dependencies.yml").read_text(encoding="utf-8")
    )


def test_an_install_plan_pinned_to_an_older_file_is_not_reused(
    tmp_path, capsys, duckdb_file
):
    _adoptable(tmp_path, duckdb_file)
    project = tmp_path / "dbt_project.yml"
    project.write_text(
        project.read_text(encoding="utf-8") + "vars:\n  test_sample_row_count: 1\n",
        encoding="utf-8",
    )
    code, first = run(tmp_path, capsys)
    assert code == 0, first
    # Both versions of the file propose the same `0`, but the stored plan's
    # pin is the earlier file, which apply would refuse.
    project.write_text(
        project.read_text(encoding="utf-8").replace("count: 1", "count: 2"),
        encoding="utf-8",
    )
    code, again = run(tmp_path, capsys)
    assert code == 0, again
    plan_id = again["data"]["elementary"]["plan_id"]
    assert plan_id != first["data"]["elementary"]["plan_id"]
    assert main(["--repo-root", str(tmp_path), "transform", "apply", plan_id]) == 0
    capsys.readouterr()
    assert (
        yaml.safe_load(project.read_text(encoding="utf-8"))["vars"][
            "test_sample_row_count"
        ]
        == 0
    )
