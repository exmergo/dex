"""`transform apply` end to end: hash-checked writes, conflicts, confirmation."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from exmergo_dex_core.cli import main


def _run(argv: list[str], capsys) -> tuple[int, dict]:
    rc = main(argv)
    out = capsys.readouterr().out
    assert out.count("\n") == 1, "exactly one line on stdout"
    return rc, json.loads(out)


def _make_plan(
    tmp_path: Path, capsys, path: str, content: str, intent: str = "test plan"
) -> str:
    payload_file = tmp_path / f"edits-{abs(hash((path, content)))}.json"
    payload_file.write_text(
        json.dumps(
            {"edits": [{"path": path, "kind": "model_sql", "content": content}]}
        ),
        encoding="utf-8",
    )
    rc, envelope = _run(
        [
            "--repo-root",
            str(tmp_path),
            "transform",
            "plan",
            intent,
            "--edits-file",
            str(payload_file),
        ],
        capsys,
    )
    assert rc == 0 and envelope["status"] == "ok"
    return envelope["data"]["plan_id"]


def test_apply_round_trip(dbt_project_dir: Path, tmp_path: Path, capsys):
    plan_id = _make_plan(
        tmp_path, capsys, "models/marts/fct_orders.sql", "select 1 as id\n"
    )
    rc, envelope = _run(
        ["--repo-root", str(tmp_path), "transform", "apply", plan_id], capsys
    )
    assert rc == 0
    assert envelope["status"] == "ok"
    assert envelope["data"]["written"] == ["models/marts/fct_orders.sql"]
    assert envelope["diffs"]
    written = dbt_project_dir / "models/marts/fct_orders.sql"
    assert written.read_text(encoding="utf-8") == "select 1 as id\n"
    # The stored plan is marked applied.
    plan_file = tmp_path / ".dex" / "plans" / f"{plan_id}.json"
    assert json.loads(plan_file.read_text())["applied_at"] is not None


def test_apply_writes_packages_yml_at_project_root(
    dbt_project_dir: Path, tmp_path: Path, capsys
):
    payload_file = tmp_path / "packages-edit.json"
    payload_file.write_text(
        json.dumps(
            {
                "edits": [
                    {
                        "path": "packages.yml",
                        "kind": "packages_yml",
                        "content": "packages:\n  - package: dbt-labs/dbt_utils\n"
                        "    version: 1.1.1\n",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    rc, envelope = _run(
        [
            "--repo-root",
            str(tmp_path),
            "transform",
            "plan",
            "add dbt_utils",
            "--edits-file",
            str(payload_file),
        ],
        capsys,
    )
    assert rc == 0 and envelope["status"] == "ok"
    plan_id = envelope["data"]["plan_id"]

    rc, envelope = _run(
        ["--repo-root", str(tmp_path), "transform", "apply", plan_id], capsys
    )
    assert rc == 0 and envelope["status"] == "ok"
    assert envelope["data"]["written"] == ["packages.yml"]
    manifest = dbt_project_dir / "packages.yml"
    assert manifest.is_file()
    assert "dbt_utils" in manifest.read_text(encoding="utf-8")


def test_apply_unknown_plan_is_a_clean_error(
    dbt_project_dir: Path, tmp_path: Path, capsys
):
    rc, envelope = _run(
        ["--repo-root", str(tmp_path), "transform", "apply", "p0000000000"], capsys
    )
    assert rc == 1
    assert envelope["status"] == "error"


def test_apply_without_plan_id_and_no_plans_is_a_clean_error(
    dbt_project_dir: Path, tmp_path: Path, capsys
):
    rc, envelope = _run(["--repo-root", str(tmp_path), "transform", "apply"], capsys)
    assert rc == 1
    assert envelope["status"] == "error"
    assert "no unapplied plan" in envelope["errors"][0]


def test_apply_without_id_takes_the_latest_unapplied_plan(
    dbt_project_dir: Path, tmp_path: Path, capsys
):
    _make_plan(tmp_path, capsys, "models/marts/first.sql", "select 1 as id\n", "one")
    latest_id = _make_plan(
        tmp_path, capsys, "models/marts/second.sql", "select 2 as id\n", "two"
    )
    rc, envelope = _run(["--repo-root", str(tmp_path), "transform", "apply"], capsys)
    assert rc == 0, envelope
    assert envelope["data"]["plan_id"] == latest_id
    assert envelope["data"]["written"] == ["models/marts/second.sql"]


def test_apply_accepts_a_semantic_plan_id(
    dbt_project_dir: Path, tmp_path: Path, capsys
):
    """`transform apply` writes any plan kind through one path: a semantic plan
    id applies exactly like a model plan id."""

    semantic_yaml = (
        "semantic_models:\n"
        "  - name: customers\n"
        "    model: ref('stg_customers')\n"
        "    entities:\n"
        "      - name: customer\n"
        "        type: primary\n"
        "        expr: id\n"
        "    measures:\n"
        "      - name: customer_count\n"
        "        agg: count\n"
        "        expr: id\n"
    )
    payload_file = tmp_path / "semantic-edits.json"
    payload_file.write_text(
        json.dumps(
            {
                "edits": [
                    {"path": "models/semantic/customers.yml", "content": semantic_yaml}
                ]
            }
        ),
        encoding="utf-8",
    )
    rc, envelope = _run(
        [
            "--repo-root",
            str(tmp_path),
            "semantic",
            "define",
            "customers",
            "--edits-file",
            str(payload_file),
        ],
        capsys,
    )
    assert rc == 0, envelope
    plan_id = envelope["data"]["plan_id"]

    rc, envelope = _run(
        ["--repo-root", str(tmp_path), "transform", "apply", plan_id], capsys
    )
    assert rc == 0, envelope
    assert envelope["data"]["written"] == ["models/semantic/customers.yml"]
    assert (dbt_project_dir / "models/semantic/customers.yml").is_file()


def test_transform_plans_lists_pending_and_applied(
    dbt_project_dir: Path, tmp_path: Path, capsys
):
    older = _make_plan(tmp_path, capsys, "models/marts/a.sql", "select 1 as a\n", "a")
    newer = _make_plan(tmp_path, capsys, "models/marts/b.sql", "select 1 as b\n", "b")
    _run(["--repo-root", str(tmp_path), "transform", "apply", older], capsys)

    rc, envelope = _run(["--repo-root", str(tmp_path), "transform", "plans"], capsys)
    assert rc == 0, envelope
    assert envelope["data"]["count"] == 2
    listed = envelope["data"]["plans"]
    assert [p["plan_id"] for p in listed] == [newer, older], "newest first"
    by_id = {p["plan_id"]: p for p in listed}
    assert by_id[newer]["pending"] is True and by_id[newer]["applied_at"] is None
    assert by_id[older]["pending"] is False and by_id[older]["applied_at"] is not None
    assert by_id[newer]["kinds"] == ["model_sql"]
    assert by_id[newer]["paths"] == ["models/marts/b.sql"]


def test_transform_plans_with_no_store_is_ok_and_empty(tmp_path: Path, capsys):
    rc, envelope = _run(["--repo-root", str(tmp_path), "transform", "plans"], capsys)
    assert rc == 0
    assert envelope["data"] == {"plans": [], "count": 0}


def test_reapply_is_a_noop(dbt_project_dir: Path, tmp_path: Path, capsys):
    plan_id = _make_plan(tmp_path, capsys, "models/marts/fct_x.sql", "select 1\n")
    _run(["--repo-root", str(tmp_path), "transform", "apply", plan_id], capsys)
    rc, envelope = _run(
        ["--repo-root", str(tmp_path), "transform", "apply", plan_id], capsys
    )
    assert rc == 0
    assert envelope["status"] == "ok"
    assert envelope["data"]["written"] == []


def test_apply_conflict_needs_confirmation_then_confirm_overrides(
    dbt_project_dir: Path, tmp_path: Path, capsys
):
    target = dbt_project_dir / "models/staging/stg_customers.sql"
    plan_id = _make_plan(
        tmp_path, capsys, "models/staging/stg_customers.sql", "select 1 as id\n"
    )
    # A human edits the file between plan and apply.
    target.write_text("select 99 as id -- hand-tuned\n", encoding="utf-8")

    rc, envelope = _run(
        ["--repo-root", str(tmp_path), "transform", "apply", plan_id], capsys
    )
    assert rc == 0
    assert envelope["status"] == "needs_confirmation"
    assert (
        envelope["data"]["conflicts"][0]["path"] == "models/staging/stg_customers.sql"
    )
    assert envelope["diffs"], "the divergence is surfaced as a diff"
    assert target.read_text(encoding="utf-8") == "select 99 as id -- hand-tuned\n"

    rc, envelope = _run(
        ["--repo-root", str(tmp_path), "transform", "apply", plan_id, "--confirm"],
        capsys,
    )
    assert rc == 0
    assert envelope["status"] == "ok"
    assert envelope["data"]["written"] == ["models/staging/stg_customers.sql"]
    assert envelope["data"]["conflicts_overridden"] == [
        "models/staging/stg_customers.sql"
    ]
    assert target.read_text(encoding="utf-8") == "select 1 as id\n"


# --- Deletes end to end, and identical across every connector ----------------


def _stub_parse_ok(monkeypatch) -> None:
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


def _plan(
    tmp_path: Path, capsys, edits: list[dict], intent: str = "plan"
) -> tuple[int, dict]:
    payload = tmp_path / f"edits-{abs(hash(json.dumps(edits)))}.json"
    payload.write_text(json.dumps({"edits": edits}), encoding="utf-8")
    return _run(
        [
            "--repo-root",
            str(tmp_path),
            "transform",
            "plan",
            intent,
            "--edits-file",
            str(payload),
        ],
        capsys,
    )


def test_apply_deletes_a_model_and_marks_applied(
    dbt_project_dir: Path, tmp_path: Path, capsys, monkeypatch
):
    _stub_parse_ok(monkeypatch)
    rc, envelope = _plan(
        tmp_path,
        capsys,
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
        "drop stg_customers",
    )
    assert rc == 0, envelope
    plan_id = envelope["data"]["plan_id"]

    rc, envelope = _run(
        ["--repo-root", str(tmp_path), "transform", "apply", plan_id], capsys
    )
    assert rc == 0, envelope
    assert "models/staging/stg_customers.sql" in envelope["data"]["written"]
    assert not (dbt_project_dir / "models/staging/stg_customers.sql").exists()
    plan_file = tmp_path / ".dex" / "plans" / f"{plan_id}.json"
    assert json.loads(plan_file.read_text())["applied_at"] is not None


def test_apply_delete_needs_confirmation_when_the_file_diverged(
    dbt_project_dir: Path, tmp_path: Path, capsys, monkeypatch
):
    _stub_parse_ok(monkeypatch)
    rc, envelope = _plan(
        tmp_path,
        capsys,
        [
            {
                "path": "models/staging/stg_customers.sql",
                "kind": "model_sql",
                "op": "delete",
            }
        ],
        "drop stg_customers",
    )
    assert rc == 0, envelope
    plan_id = envelope["data"]["plan_id"]

    # A human edits the model after planning the delete: it must not vanish.
    target = dbt_project_dir / "models/staging/stg_customers.sql"
    target.write_text("select 7 as id -- do not delete\n", encoding="utf-8")

    rc, envelope = _run(
        ["--repo-root", str(tmp_path), "transform", "apply", plan_id], capsys
    )
    assert envelope["status"] == "needs_confirmation"
    assert target.exists()

    # With --confirm the deletion is carried out deliberately.
    rc, envelope = _run(
        ["--repo-root", str(tmp_path), "--confirm", "transform", "apply", plan_id],
        capsys,
    )
    assert rc == 0, envelope
    assert not target.exists()


_CONNECTOR_PROFILES = {
    "duckdb": (
        "dex_test:\n  target: dev\n  outputs:\n    dev:\n      type: duckdb\n"
        "      path: /tmp/dex_dogfood_dev.duckdb\n"
    ),
    "bigquery": (
        "dex_test:\n  target: dev\n  outputs:\n    dev:\n      type: bigquery\n"
        "      method: oauth\n      project: dex-dogfood\n      dataset: dbt_dev\n"
        "      location: EU\n"
    ),
    "snowflake": (
        "dex_test:\n  target: dev\n  outputs:\n    dev:\n      type: snowflake\n"
        "      account: dogfood\n      user: dex\n"
        "      authenticator: externalbrowser\n"
        "      database: analytics\n      warehouse: wh\n      schema: dbt_dev\n"
    ),
    "databricks": (
        "dex_test:\n  target: dev\n  outputs:\n    dev:\n      type: databricks\n"
        "      host: dogfood.databricks.com\n      http_path: /sql/1.0/warehouses/abc\n"
        "      catalog: main\n      schema: dbt_dev\n"
    ),
}


@pytest.mark.parametrize("connector", sorted(_CONNECTOR_PROFILES))
def test_guarded_delete_is_identical_across_connectors(
    connector: str, dbt_project_dir: Path, tmp_path: Path, capsys, monkeypatch
):
    """Dogfood: the guarded-delete loop behaves the same on every warehouse.

    The delete path is file-level and never touches an adapter, so retyping the
    project's dev target to BigQuery, Snowflake, Databricks, or DuckDB must not
    change a thing: an orphaning delete is refused, and a delete plus its
    ref-removing update applies cleanly.
    """

    (dbt_project_dir / "profiles.yml").write_text(
        _CONNECTOR_PROFILES[connector], encoding="utf-8"
    )
    (dbt_project_dir / "models" / "marts").mkdir(parents=True, exist_ok=True)
    (dbt_project_dir / "models" / "marts" / "mart_customers.sql").write_text(
        "select * from {{ ref('stg_customers') }}\n", encoding="utf-8"
    )

    # An orphaning delete is refused identically, without ever reaching dbt.
    rc, envelope = _plan(
        tmp_path,
        capsys,
        [
            {
                "path": "models/staging/stg_customers.sql",
                "kind": "model_sql",
                "op": "delete",
            }
        ],
        "drop stg_customers",
    )
    assert rc == 1, connector
    assert "mart_customers" in envelope["errors"][0]

    # Delete plus the update that removes the ref applies cleanly, everywhere.
    _stub_parse_ok(monkeypatch)
    rc, envelope = _plan(
        tmp_path,
        capsys,
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
            {
                "path": "models/marts/mart_customers.sql",
                "kind": "model_sql",
                "content": "select 1 as id\n",
            },
        ],
        "reclassify",
    )
    assert rc == 0, (connector, envelope)
    plan_id = envelope["data"]["plan_id"]
    rc, envelope = _run(
        ["--repo-root", str(tmp_path), "transform", "apply", plan_id], capsys
    )
    assert rc == 0, (connector, envelope)
    assert not (dbt_project_dir / "models/staging/stg_customers.sql").exists()
