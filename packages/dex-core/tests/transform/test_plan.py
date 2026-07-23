"""`transform plan` end to end: validate, diff, store; never touch the project."""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from exmergo_dex_core.cli import main


def _run(argv: list[str], capsys) -> tuple[int, dict]:
    rc = main(argv)
    out = capsys.readouterr().out
    assert out.count("\n") == 1, "exactly one line on stdout"
    return rc, json.loads(out)


def _write_payload(tmp_path: Path, edits: list[dict]) -> Path:
    payload_file = tmp_path / "edits.json"
    payload_file.write_text(json.dumps({"edits": edits}), encoding="utf-8")
    return payload_file


def test_plan_from_edits_file(dbt_project_dir: Path, tmp_path: Path, capsys):
    payload = _write_payload(
        tmp_path,
        [
            {
                "path": "models/staging/stg_orders.sql",
                "kind": "model_sql",
                "content": "select 1 as id, 2 as customer_id\n",
            },
            {
                "path": "models/staging/stg_orders.yml",
                "kind": "schema_yml",
                "content": "version: 2\nmodels:\n  - name: stg_orders\n",
            },
        ],
    )
    rc, envelope = _run(
        [
            "--repo-root",
            str(tmp_path),
            "transform",
            "plan",
            "add stg_orders",
            "--edits-file",
            str(payload),
        ],
        capsys,
    )
    assert rc == 0
    assert envelope["status"] == "ok"
    plan_id = envelope["data"]["plan_id"]
    assert envelope["data"]["edit_count"] == 2
    assert envelope["diffs"] and envelope["diffs"][0]["op"] == "create"
    assert (tmp_path / ".dex" / "plans" / f"{plan_id}.json").is_file()
    # Propose-don't-impose: planning writes nothing into the dbt project.
    assert not (dbt_project_dir / "models/staging/stg_orders.sql").exists()
    assert not (dbt_project_dir / "models/staging/stg_orders.yml").exists()


def test_plan_packages_yml_at_project_root(
    dbt_project_dir: Path, tmp_path: Path, capsys
):
    payload = _write_payload(
        tmp_path,
        [
            {
                "path": "packages.yml",
                "kind": "packages_yml",
                "content": "packages:\n  - package: dbt-labs/dbt_utils\n"
                "    version: 1.1.1\n",
            }
        ],
    )
    rc, envelope = _run(
        [
            "--repo-root",
            str(tmp_path),
            "transform",
            "plan",
            "add dbt_utils",
            "--edits-file",
            str(payload),
        ],
        capsys,
    )
    assert rc == 0 and envelope["status"] == "ok"
    assert envelope["data"]["paths"] == ["packages.yml"]
    assert envelope["diffs"][0]["op"] == "create"
    # Propose-don't-impose: still nothing written until apply.
    assert not (dbt_project_dir / "packages.yml").exists()


def test_plan_packages_yml_requires_a_packages_key(
    dbt_project_dir: Path, tmp_path: Path, capsys
):
    payload = _write_payload(
        tmp_path,
        [{"path": "packages.yml", "kind": "packages_yml", "content": "nope: true\n"}],
    )
    rc, envelope = _run(
        [
            "--repo-root",
            str(tmp_path),
            "transform",
            "plan",
            "bad packages",
            "--edits-file",
            str(payload),
        ],
        capsys,
    )
    assert rc == 1 and envelope["status"] == "error"
    assert "packages" in envelope["errors"][0]


def test_plan_reads_stdin(dbt_project_dir: Path, tmp_path: Path, capsys, monkeypatch):
    payload = json.dumps(
        {
            "edits": [
                {
                    "path": "models/staging/stg_inline.sql",
                    "kind": "model_sql",
                    "content": "select 1 as id\n",
                }
            ]
        }
    )
    monkeypatch.setattr("sys.stdin", io.StringIO(payload))
    rc, envelope = _run(
        [
            "--repo-root",
            str(tmp_path),
            "transform",
            "plan",
            "inline",
            "--edits-file",
            "-",
        ],
        capsys,
    )
    assert rc == 0
    assert envelope["status"] == "ok"


def test_plan_is_idempotent_by_content(dbt_project_dir: Path, tmp_path: Path, capsys):
    payload = _write_payload(
        tmp_path,
        [
            {
                "path": "models/staging/a.sql",
                "kind": "model_sql",
                "content": "select 1\n",
            }
        ],
    )
    argv = [
        "--repo-root",
        str(tmp_path),
        "transform",
        "plan",
        "same",
        "--edits-file",
        str(payload),
    ]
    _rc, first = _run(argv, capsys)
    _rc, second = _run(argv, capsys)
    assert first["data"]["plan_id"] == second["data"]["plan_id"]


def test_plan_rejects_non_select_sql(dbt_project_dir: Path, tmp_path: Path, capsys):
    payload = _write_payload(
        tmp_path,
        [
            {
                "path": "models/staging/evil.sql",
                "kind": "model_sql",
                "content": "drop table customers",
            }
        ],
    )
    rc, envelope = _run(
        [
            "--repo-root",
            str(tmp_path),
            "transform",
            "plan",
            "evil",
            "--edits-file",
            str(payload),
        ],
        capsys,
    )
    assert rc == 1
    assert envelope["status"] == "error"
    assert not (tmp_path / ".dex" / "plans").exists()


def test_plan_accepts_jinja_wrapped_select(
    dbt_project_dir: Path, tmp_path: Path, capsys
):
    content = (
        "{{ config(materialized='view') }}\n\n"
        "with source as (\n"
        "    select * from {{ source('main', 'orders') }}\n"
        ")\n"
        "select id, customer_id from source\n"
    )
    payload = _write_payload(
        tmp_path,
        [{"path": "models/staging/stg_j.sql", "kind": "model_sql", "content": content}],
    )
    rc, envelope = _run(
        [
            "--repo-root",
            str(tmp_path),
            "transform",
            "plan",
            "jinja",
            "--edits-file",
            str(payload),
        ],
        capsys,
    )
    assert rc == 0
    assert envelope["status"] == "ok"


@pytest.mark.parametrize(
    "bad_path",
    ["../outside.sql", "models/../../escape.sql", "seeds/data.yml"],
)
def test_plan_rejects_paths_outside_model_paths(
    dbt_project_dir: Path, tmp_path: Path, capsys, bad_path: str
):
    payload = _write_payload(
        tmp_path, [{"path": bad_path, "kind": "model_sql", "content": "select 1\n"}]
    )
    rc, envelope = _run(
        [
            "--repo-root",
            str(tmp_path),
            "transform",
            "plan",
            "x",
            "--edits-file",
            str(payload),
        ],
        capsys,
    )
    assert rc == 1
    assert envelope["status"] == "error"


def test_plan_rejects_invalid_yaml(dbt_project_dir: Path, tmp_path: Path, capsys):
    payload = _write_payload(
        tmp_path,
        [
            {
                "path": "models/staging/bad.yml",
                "kind": "schema_yml",
                "content": "version: 2\nmodels:\n  - name: [unclosed\n",
            }
        ],
    )
    rc, envelope = _run(
        [
            "--repo-root",
            str(tmp_path),
            "transform",
            "plan",
            "bad",
            "--edits-file",
            str(payload),
        ],
        capsys,
    )
    assert rc == 1
    assert envelope["status"] == "error"


def test_plan_without_content_is_a_clean_error(
    dbt_project_dir: Path, tmp_path: Path, capsys
):
    rc, envelope = _run(
        ["--repo-root", str(tmp_path), "transform", "plan", "no content"], capsys
    )
    assert rc == 1
    assert envelope["status"] == "error"
    assert "edits-file" in envelope["errors"][0]


# --- project config kinds (project_yml / profiles_yml) ------------------------
#
# These exercise the engine directly (`transform.plan`), which does not run dbt
# parse, so containment, the profiles secret-guard, and the path drop-check are
# deterministic without a dbt subprocess. The CLI tests below cover the
# parse-gated path.

from exmergo_dex_core import transform  # noqa: E402


def _cfg_edit(path: str, content: str, kind) -> transform.PlanEdit:
    return transform.PlanEdit(path=path, new_content=content, kind=kind)


def test_project_yml_edit_pins_the_existing_file(dbt_project_dir: Path):
    # load() now carries root config into the view, so an edit to the existing
    # dbt_project.yml diffs as an update against a real pinned hash, not a create.
    edit = _cfg_edit(
        "dbt_project.yml",
        'name: dex_test\nversion: "1.0.0"\nprofile: dex_test\n'
        'model-paths: ["models"]\n# tuned\n',
        transform.EditKind.PROJECT_YML,
    )
    plan, diffs, _warnings = transform.plan(
        "tune project", [edit], dbt_project_dir, repo_root=dbt_project_dir.parent
    )
    assert diffs[0]["op"] == "update"
    assert plan.edits[0].old_content_hash is not None


def test_project_yml_kind_must_target_dbt_project(dbt_project_dir: Path):
    edit = _cfg_edit(
        "models/staging/x.yml", "name: x\n", transform.EditKind.PROJECT_YML
    )
    with pytest.raises(transform.PlanError):
        transform.plan(
            "misaimed", [edit], dbt_project_dir, repo_root=dbt_project_dir.parent
        )


def test_config_file_reached_by_the_wrong_kind_is_refused(dbt_project_dir: Path):
    edit = _cfg_edit("dbt_project.yml", "select 1\n", transform.EditKind.MODEL_SQL)
    with pytest.raises(transform.PlanError):
        transform.plan(
            "wrong kind", [edit], dbt_project_dir, repo_root=dbt_project_dir.parent
        )


def test_profiles_yml_refuses_a_secret_already_on_disk(dbt_project_dir: Path):
    # The diff's removed side would leak a pre-existing inlined credential even
    # when the proposed content is clean, so the current file is guarded too.
    (dbt_project_dir / "profiles.yml").write_text(
        "dex_test:\n  target: dev\n  outputs:\n    dev:\n      type: postgres\n"
        "      host: localhost\n      user: u\n      password: hunter2\n"
        "      dbname: d\n      schema: public\n",
        encoding="utf-8",
    )
    safe = (
        "dex_test:\n  target: dev\n  outputs:\n    dev:\n      type: postgres\n"
        "      host: localhost\n      user: u\n"
        "      password: \"{{ env_var('PGPASSWORD') }}\"\n"
        "      dbname: d\n      schema: public\n"
    )
    edit = _cfg_edit("profiles.yml", safe, transform.EditKind.PROFILES_YML)
    with pytest.raises(transform.PlanError) as exc:
        transform.plan(
            "env-var the password",
            [edit],
            dbt_project_dir,
            repo_root=dbt_project_dir.parent,
        )
    assert "hunter2" not in str(exc.value)
    assert "password" in str(exc.value)


def test_project_yml_dropping_a_model_path_warns(dbt_project_dir: Path):
    edit = _cfg_edit(
        "dbt_project.yml",
        'name: dex_test\nprofile: dex_test\nmodel-paths: ["staging"]\n',
        transform.EditKind.PROJECT_YML,
    )
    _plan, _diffs, warnings = transform.plan(
        "restructure", [edit], dbt_project_dir, repo_root=dbt_project_dir.parent
    )
    assert any("model-paths drops" in w and "models" in w for w in warnings)


def test_plan_cli_refuses_an_inlined_profiles_secret(
    dbt_project_dir: Path, tmp_path: Path, capsys
):
    payload = _write_payload(
        tmp_path,
        [
            {
                "path": "profiles.yml",
                "kind": "profiles_yml",
                "content": (
                    "dex_test:\n  target: dev\n  outputs:\n    dev:\n"
                    "      type: postgres\n      host: h\n      user: u\n"
                    "      password: hunter2\n      dbname: d\n      schema: public\n"
                ),
            }
        ],
    )
    rc, envelope = _run(
        [
            "--repo-root",
            str(tmp_path),
            "transform",
            "plan",
            "set profile",
            "--edits-file",
            str(payload),
        ],
        capsys,
    )
    assert rc == 1
    assert envelope["status"] == "error"
    blob = json.dumps(envelope)
    assert "hunter2" not in blob
    assert "env_var" in blob or "credential" in blob
    plans_dir = tmp_path / ".dex" / "plans"
    assert not plans_dir.exists() or not list(plans_dir.glob("*.json"))


def test_plan_cli_project_yml_pins_an_update(
    dbt_project_dir: Path, tmp_path: Path, capsys
):
    payload = _write_payload(
        tmp_path,
        [
            {
                "path": "dbt_project.yml",
                "kind": "project_yml",
                "content": (
                    'name: dex_test\nversion: "1.0.0"\nprofile: dex_test\n'
                    'model-paths: ["models"]\n# tuned by dex\n'
                ),
            }
        ],
    )
    rc, envelope = _run(
        [
            "--repo-root",
            str(tmp_path),
            "transform",
            "plan",
            "tune project",
            "--edits-file",
            str(payload),
        ],
        capsys,
    )
    assert rc == 0
    assert envelope["status"] == "ok"
    assert envelope["diffs"][0]["op"] == "update"


# --- Deletes: first-class, guarded, atomic across the plan --------------------


def _stub_parse_ok(monkeypatch) -> None:
    """Make the shadow ``dbt parse`` a deterministic success.

    A clean delete plan runs the authoritative parse; stubbing it keeps the
    guard/diff/warning assertions independent of a real dbt subprocess (the real
    parse has its own coverage in test_parse.py).
    """

    import importlib
    import subprocess

    build_module = importlib.import_module("exmergo_dex_core.transform.build")

    def fake(timeout, cwd, env=None):
        def run(argv):
            return subprocess.CompletedProcess(
                args=argv, returncode=0, stdout="", stderr=""
            )

        return run

    monkeypatch.setattr(build_module, "_default_runner", fake)


def _add_referrer(project: Path, name: str, refs: str) -> None:
    (project / "models" / "marts").mkdir(parents=True, exist_ok=True)
    (project / "models" / "marts" / f"{name}.sql").write_text(
        f"select * from {{{{ ref('{refs}') }}}}\n",  # noqa: S608 (test dbt SQL)
        encoding="utf-8",
    )


def test_plan_delete_emits_delete_diff_and_pins_hash(
    dbt_project_dir: Path, tmp_path: Path, capsys, monkeypatch
):
    _stub_parse_ok(monkeypatch)
    payload = _write_payload(
        tmp_path,
        [
            {
                "path": "models/staging/stg_customers.sql",
                "kind": "model_sql",
                "op": "delete",
            },
            {
                "path": "models/staging/schema.yml",
                "kind": "schema_yml",
                "content": "version: 2\n",
            },
        ],
    )
    rc, envelope = _run(
        [
            "--repo-root",
            str(tmp_path),
            "transform",
            "plan",
            "drop stg_customers",
            "--edits-file",
            str(payload),
        ],
        capsys,
    )
    assert rc == 0, envelope
    assert envelope["status"] == "ok"
    ops = {d["path"]: d["op"] for d in envelope["diffs"]}
    assert ops["models/staging/stg_customers.sql"] == "delete"
    # The plan is stored, the project untouched, and the delete pins the hash it
    # was planned against so a later human edit surfaces as a conflict.
    plan_id = envelope["data"]["plan_id"]
    plan = json.loads((tmp_path / ".dex" / "plans" / f"{plan_id}.json").read_text())
    deletion = next(e for e in plan["edits"] if e["op"] == "delete")
    assert deletion["old_content_hash"]
    assert deletion["new_content"] is None
    assert (dbt_project_dir / "models/staging/stg_customers.sql").exists()


def test_plan_delete_of_a_missing_file_is_refused(
    dbt_project_dir: Path, tmp_path: Path, capsys
):
    payload = _write_payload(
        tmp_path,
        [{"path": "models/staging/ghost.sql", "kind": "model_sql", "op": "delete"}],
    )
    rc, envelope = _run(
        [
            "--repo-root",
            str(tmp_path),
            "transform",
            "plan",
            "drop ghost",
            "--edits-file",
            str(payload),
        ],
        capsys,
    )
    assert rc == 1
    assert "nothing to delete" in envelope["errors"][0]


def test_plan_guard_refuses_a_delete_that_orphans_a_ref(
    dbt_project_dir: Path, tmp_path: Path, capsys
):
    _add_referrer(dbt_project_dir, "mart_customers", "stg_customers")
    payload = _write_payload(
        tmp_path,
        [
            {
                "path": "models/staging/stg_customers.sql",
                "kind": "model_sql",
                "op": "delete",
            }
        ],
    )
    rc, envelope = _run(
        [
            "--repo-root",
            str(tmp_path),
            "transform",
            "plan",
            "drop stg_customers",
            "--edits-file",
            str(payload),
        ],
        capsys,
    )
    assert rc == 1
    msg = envelope["errors"][0]
    assert "stg_customers" in msg and "mart_customers" in msg
    # Nothing was stored: an orphaning delete never becomes a plan.
    assert not (tmp_path / ".dex" / "plans").exists() or not list(
        (tmp_path / ".dex" / "plans").glob("*.json")
    )


def test_plan_accepts_a_delete_plus_the_update_that_removes_the_ref(
    dbt_project_dir: Path, tmp_path: Path, capsys, monkeypatch
):
    _stub_parse_ok(monkeypatch)
    _add_referrer(dbt_project_dir, "mart_customers", "stg_customers")
    payload = _write_payload(
        tmp_path,
        [
            {
                "path": "models/staging/stg_customers.sql",
                "kind": "model_sql",
                "op": "delete",
            },
            {
                "path": "models/staging/schema.yml",
                "kind": "schema_yml",
                "content": "version: 2\n",
            },
            # The referrer is rewritten to stand on its own, in the same plan.
            {
                "path": "models/marts/mart_customers.sql",
                "kind": "model_sql",
                "content": "select 1 as id\n",
            },
        ],
    )
    rc, envelope = _run(
        [
            "--repo-root",
            str(tmp_path),
            "transform",
            "plan",
            "reclassify",
            "--edits-file",
            str(payload),
        ],
        capsys,
    )
    assert rc == 0, envelope
    assert envelope["status"] == "ok"
    ops = {d["path"]: d["op"] for d in envelope["diffs"]}
    assert ops["models/staging/stg_customers.sql"] == "delete"
    assert ops["models/marts/mart_customers.sql"] == "update"


def test_plan_delete_warns_about_a_lingering_schema_entry(
    dbt_project_dir: Path, tmp_path: Path, capsys, monkeypatch
):
    _stub_parse_ok(monkeypatch)
    # Delete the model but leave its schema.yml doc entry behind.
    payload = _write_payload(
        tmp_path,
        [
            {
                "path": "models/staging/stg_customers.sql",
                "kind": "model_sql",
                "op": "delete",
            }
        ],
    )
    rc, envelope = _run(
        [
            "--repo-root",
            str(tmp_path),
            "transform",
            "plan",
            "drop model",
            "--edits-file",
            str(payload),
        ],
        capsys,
    )
    assert rc == 0, envelope
    assert any("still documents deleted model" in w for w in envelope["warnings"])


def test_plan_delete_with_content_is_rejected(
    dbt_project_dir: Path, tmp_path: Path, capsys
):
    payload = _write_payload(
        tmp_path,
        [
            {
                "path": "models/staging/stg_customers.sql",
                "kind": "model_sql",
                "op": "delete",
                "content": "select 1\n",
            }
        ],
    )
    rc, envelope = _run(
        [
            "--repo-root",
            str(tmp_path),
            "transform",
            "plan",
            "bad",
            "--edits-file",
            str(payload),
        ],
        capsys,
    )
    assert rc == 1
    assert "must not carry content" in envelope["errors"][0]


def test_plan_unknown_op_is_rejected(dbt_project_dir: Path, tmp_path: Path, capsys):
    payload = _write_payload(
        tmp_path,
        [
            {
                "path": "models/staging/stg_customers.sql",
                "kind": "model_sql",
                "op": "frobnicate",
            }
        ],
    )
    rc, envelope = _run(
        [
            "--repo-root",
            str(tmp_path),
            "transform",
            "plan",
            "bad",
            "--edits-file",
            str(payload),
        ],
        capsys,
    )
    assert rc == 1
    assert "unknown op" in envelope["errors"][0]
