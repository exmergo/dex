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


@pytest.mark.parametrize(
    "content",
    [
        "{{ config(materialized='view') }} select 1 as id\n",
        "{{ config(materialized='view') }}\nselect 1 as id\n",
    ],
    ids=["inline", "separate-line"],
)
def test_plan_accepts_inline_and_separate_line_config_headers(
    dbt_project_dir: Path, tmp_path: Path, capsys, content: str
):
    """#1003: Inline and separate-line config() headers both plan successfully."""
    payload = _write_payload(
        tmp_path,
        [
            {
                "path": "models/config_probe.sql",
                "kind": "model_sql",
                "content": content,
            },
        ],
    )
    rc, envelope = _run(
        [
            "--repo-root",
            str(tmp_path),
            "transform",
            "plan",
            "probe config",
            "--edits-file",
            str(payload),
        ],
        capsys,
    )
    assert rc == 0, envelope
    assert envelope["status"] == "ok"
    assert envelope["data"]["edit_count"] == 1
    assert envelope["diffs"] and envelope["diffs"][0]["op"] == "create"
    plan_id = envelope["data"]["plan_id"]
    plan_file = tmp_path / ".dex" / "plans" / f"{plan_id}.json"
    assert plan_file.is_file()
    saved_plan = json.loads(plan_file.read_text(encoding="utf-8"))
    assert saved_plan["edits"][0]["new_content"] == content


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


def test_plan_refuses_uninstalled_declared_packages_with_the_dex_verb(
    dbt_project_dir: Path, tmp_path: Path, capsys
):
    (dbt_project_dir / "packages.yml").write_text(
        'packages:\n  - package: dbt-labs/dbt_utils\n    version: "1.3.0"\n',
        encoding="utf-8",
    )
    payload = _write_payload(
        tmp_path,
        [
            {
                "path": "models/staging/schema.yml",
                "kind": "schema_yml",
                "content": (
                    "version: 2\n"
                    "models:\n"
                    "  - name: stg_customers\n"
                    "    columns:\n"
                    "      - name: id\n"
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
            "add a column",
            "--edits-file",
            str(payload),
        ],
        capsys,
    )
    assert rc == 1 and envelope["status"] == "error"
    message = envelope["errors"][0]
    assert "transform deps" in message
    assert "dbt deps" not in message
    # Planning never installs: the package directory is still absent.
    assert not (dbt_project_dir / "dbt_packages").exists()


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


@pytest.mark.parametrize("existing", [False, True], ids=["create", "update"])
def test_plan_rejects_invalid_yaml(
    dbt_project_dir: Path, tmp_path: Path, capsys, existing: bool
):
    target = dbt_project_dir / "models/staging/bad.yml"
    if existing:
        target.write_text("version: 2\nmodels: []\n", encoding="utf-8")
    before = {
        path.relative_to(dbt_project_dir): path.read_bytes()
        for path in dbt_project_dir.rglob("*")
        if path.is_file()
    }
    payload = _write_payload(
        tmp_path,
        [
            {
                "path": "models/staging/bad.yml",
                "kind": "schema_yml",
                "content": "version: 2\ndescription: Collected signal: more\n",
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

    message = "\n".join(envelope["errors"])
    assert "models/staging/bad.yml: invalid YAML:" in message
    assert "line 2, column 30" in message
    after = {
        path.relative_to(dbt_project_dir): path.read_bytes()
        for path in dbt_project_dir.rglob("*")
        if path.is_file()
    }
    assert after == before
    assert not (tmp_path / ".dex" / "plans").exists()


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
from exmergo_dex_core.storage import FilesystemStore  # noqa: E402


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
        "tune project",
        [edit],
        dbt_project_dir,
        repo_root=dbt_project_dir.parent,
        store=FilesystemStore(dbt_project_dir.parent),
    )
    assert diffs[0]["op"] == "update"
    assert plan.edits[0].old_content_hash is not None


def test_project_yml_kind_must_target_dbt_project(dbt_project_dir: Path):
    edit = _cfg_edit(
        "models/staging/x.yml", "name: x\n", transform.EditKind.PROJECT_YML
    )
    with pytest.raises(transform.PlanError):
        transform.plan(
            "misaimed",
            [edit],
            dbt_project_dir,
            repo_root=dbt_project_dir.parent,
            store=FilesystemStore(dbt_project_dir.parent),
        )


def test_config_file_reached_by_the_wrong_kind_is_refused(dbt_project_dir: Path):
    edit = _cfg_edit("dbt_project.yml", "select 1\n", transform.EditKind.MODEL_SQL)
    with pytest.raises(transform.PlanError):
        transform.plan(
            "wrong kind",
            [edit],
            dbt_project_dir,
            repo_root=dbt_project_dir.parent,
            store=FilesystemStore(dbt_project_dir.parent),
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
            store=FilesystemStore(dbt_project_dir.parent),
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
        "restructure",
        [edit],
        dbt_project_dir,
        repo_root=dbt_project_dir.parent,
        store=FilesystemStore(dbt_project_dir.parent),
    )
    assert any("model-paths drops" in w and "models" in w for w in warnings)


def test_project_yml_dropping_any_authored_path_family_warns(dbt_project_dir: Path):
    """Every path key dex authors into, not the two that happened to be first.

    Dropping a family orphans every file under it, and which family a caller
    restructures is not something the warning gets to have an opinion about.
    Only `model-paths` and `macro-paths` were covered before, so a
    dbt_project.yml that dropped `seed-paths` silently orphaned every seed.
    """

    original = (
        "name: dex_test\nprofile: dex_test\n"
        'model-paths: ["models"]\n'
        'macro-paths: ["macros"]\n'
        'snapshot-paths: ["snapshots"]\n'
        'seed-paths: ["seeds"]\n'
        'test-paths: ["tests"]\n'
        'analysis-paths: ["analyses"]\n'
    )
    (dbt_project_dir / "dbt_project.yml").write_text(original, encoding="utf-8")

    edit = _cfg_edit(
        "dbt_project.yml",
        "name: dex_test\nprofile: dex_test\n",
        transform.EditKind.PROJECT_YML,
    )
    _plan, _diffs, warnings = transform.plan(
        "flatten the project",
        [edit],
        dbt_project_dir,
        repo_root=dbt_project_dir.parent,
        store=FilesystemStore(dbt_project_dir.parent),
    )
    for key, directory in (
        ("model-paths", "models"),
        ("macro-paths", "macros"),
        ("snapshot-paths", "snapshots"),
        ("seed-paths", "seeds"),
        ("test-paths", "tests"),
        ("analysis-paths", "analyses"),
    ):
        assert any(f"{key} drops" in w and directory in w for w in warnings), key


# --- column contract: authored SELECT list vs. declared schema.yml (#214) -----


def test_column_contract_warns_when_a_declared_column_is_missing(
    dbt_project_dir: Path,
):
    """Declaring both `id` and `email` (in the same plan) but authoring a
    SELECT that only produces `id` warns, naming the missing column."""

    schema_edit = _cfg_edit(
        "models/staging/schema.yml",
        "version: 2\nmodels:\n  - name: stg_customers\n    columns:\n"
        "      - name: id\n      - name: email\n",
        transform.EditKind.SCHEMA_YML,
    )
    model_edit = _cfg_edit(
        "models/staging/stg_customers.sql",
        "select 1 as id\n",
        transform.EditKind.MODEL_SQL,
    )
    plan, _diffs, warnings = transform.plan(
        "drop email from the select",
        [schema_edit, model_edit],
        dbt_project_dir,
        repo_root=dbt_project_dir.parent,
        store=FilesystemStore(dbt_project_dir.parent),
    )
    assert plan.plan_id  # the plan still validates and stores
    assert any("email" in w and "does not produce" in w for w in warnings), warnings


def test_column_contract_warns_when_select_adds_an_undeclared_column(
    dbt_project_dir: Path,
):
    """`stg_customers`' schema.yml declares only `id`; authoring a SELECT that
    also produces `email` warns, naming the undeclared column."""

    model_edit = _cfg_edit(
        "models/staging/stg_customers.sql",
        "select 1 as id, 'a@example.com' as email\n",
        transform.EditKind.MODEL_SQL,
    )
    plan, _diffs, warnings = transform.plan(
        "add email to the select",
        [model_edit],
        dbt_project_dir,
        repo_root=dbt_project_dir.parent,
        store=FilesystemStore(dbt_project_dir.parent),
    )
    assert plan.plan_id
    assert any("email" in w and "not declared" in w for w in warnings), warnings


def test_column_contract_is_silent_when_no_columns_are_declared(
    dbt_project_dir: Path,
):
    """A model whose schema.yml entry declares no `columns:` list has no
    contract to compare against, and the whole-project scan must not reach
    into an unrelated model (`stg_customers`) this plan never touched."""

    schema_edit = _cfg_edit(
        "models/staging/stg_orders.yml",
        "version: 2\nmodels:\n  - name: stg_orders\n",
        transform.EditKind.SCHEMA_YML,
    )
    model_edit = _cfg_edit(
        "models/staging/stg_orders.sql",
        "select 1 as id, 2 as customer_id\n",
        transform.EditKind.MODEL_SQL,
    )
    plan, _diffs, warnings = transform.plan(
        "add stg_orders",
        [schema_edit, model_edit],
        dbt_project_dir,
        repo_root=dbt_project_dir.parent,
        store=FilesystemStore(dbt_project_dir.parent),
    )
    assert plan.plan_id
    assert not any("stg_orders" in w for w in warnings), warnings
    # stg_customers' own on-disk mismatch (declares `id`, selects `id, email`)
    # must stay silent too: this plan never authored that model.
    assert not any("stg_customers" in w for w in warnings), warnings


def test_column_contract_reports_a_select_star_as_unresolvable(
    dbt_project_dir: Path,
):
    schema_edit = _cfg_edit(
        "models/staging/schema.yml",
        "version: 2\nmodels:\n  - name: stg_customers\n    columns:\n"
        "      - name: id\n",
        transform.EditKind.SCHEMA_YML,
    )
    model_edit = _cfg_edit(
        "models/staging/stg_customers.sql",
        "select * from raw_customers\n",
        transform.EditKind.MODEL_SQL,
    )
    plan, _diffs, warnings = transform.plan(
        "star it",
        [schema_edit, model_edit],
        dbt_project_dir,
        repo_root=dbt_project_dir.parent,
        store=FilesystemStore(dbt_project_dir.parent),
    )
    assert plan.plan_id
    assert any("could not be resolved statically" in w for w in warnings), warnings


# --- column contract through a star (#643) -------------------------------------

_DECLARES_ID_AND_NAME = (
    "version: 2\nmodels:\n  - name: mart\n    columns:\n"
    "      - name: id\n      - name: name\n"
)


def _contract_plan(project: Path, *edits, store=None) -> list[str]:
    _plan, _diffs, warnings = transform.plan(
        "contract",
        list(edits),
        project,
        repo_root=project.parent,
        store=store or FilesystemStore(project.parent),
    )
    return warnings


def _schema(content: str = _DECLARES_ID_AND_NAME):
    return _cfg_edit("models/marts/schema.yml", content, transform.EditKind.SCHEMA_YML)


def _sql(path: str, content: str):
    return _cfg_edit(path, content, transform.EditKind.MODEL_SQL)


def _store_with_profile(project: Path, identifier: str, columns: list[str]):
    from exmergo_dex_core.cache import ColumnProfile, Dataset, DexCache

    store = FilesystemStore(project.parent)
    store.save_cache(
        DexCache(
            datasets=[
                Dataset(
                    identifier=identifier,
                    columns=[
                        ColumnProfile(name=name, data_type="VARCHAR")
                        for name in columns
                    ],
                )
            ]
        )
    )
    return store


def test_the_final_cte_shape_is_compared_in_both_directions(dbt_project_dir: Path):
    warnings = _contract_plan(
        dbt_project_dir,
        _schema(),
        _sql(
            "models/marts/mart.sql",
            "with src as (select * from {{ ref('stg_customers') }}),\n"
            "final as (select id, surname from src)\nselect * from final\n",
        ),
    )
    assert not any("could not be resolved" in w for w in warnings), warnings
    assert any("name for mart that the SELECT list" in w for w in warnings), warnings
    assert any("surname not declared" in w for w in warnings), warnings


def test_a_star_over_a_ref_reads_the_project_model_as_this_plan_leaves_it(
    dbt_project_dir: Path,
):
    store = _store_with_profile(dbt_project_dir, "main.up", ["id", "stale"])
    warnings = _contract_plan(
        dbt_project_dir,
        _schema(),
        _sql("models/marts/up.sql", "select 1 as id, 'x' as name\n"),
        _sql("models/marts/mart.sql", "select * from {{ ref('up') }}\n"),
        store=store,
    )
    assert not any("mart" in w and "stale" in w for w in warnings), warnings
    assert not any("could not be resolved" in w for w in warnings), warnings
    assert not any("cached profile" in w for w in warnings), warnings


def test_a_star_over_a_source_reads_the_profile_through_its_declared_schema(
    dbt_project_dir: Path,
):
    store = _store_with_profile(dbt_project_dir, "raw_zone.people", ["id", "email"])
    sources = _cfg_edit(
        "models/marts/sources.yml",
        "version: 2\nsources:\n  - name: crm\n    schema: raw_zone\n"
        "    tables:\n      - name: customers\n        identifier: people\n",
        transform.EditKind.SCHEMA_YML,
    )
    warnings = _contract_plan(
        dbt_project_dir,
        _schema(),
        sources,
        _sql(
            "models/marts/mart.sql", "select * from {{ source('crm', 'customers') }}\n"
        ),
        store=store,
    )
    named = [w for w in warnings if "name for mart" in w or "email not declared" in w]
    assert len(named) == 2, warnings
    assert all("cached profile of raw_zone.people" in w for w in named)


def test_a_star_with_no_profile_says_so_and_names_the_fix(dbt_project_dir: Path):
    warnings = _contract_plan(
        dbt_project_dir,
        _schema(),
        _sql(
            "models/marts/mart.sql", "select * from {{ source('crm', 'customers') }}\n"
        ),
    )
    skipped = [w for w in warnings if "could not be resolved statically" in w]
    assert len(skipped) == 1, warnings
    assert "source('crm', 'customers')" in skipped[0]
    assert "dex explore profile customers" in skipped[0]


def test_an_upstream_this_plan_changes_never_falls_back_to_its_profile(
    dbt_project_dir: Path,
):
    store = _store_with_profile(dbt_project_dir, "main.up", ["id", "name"])
    warnings = _contract_plan(
        dbt_project_dir,
        _schema(),
        _sql("models/marts/up.sql", "select * from {{ source('crm', 'unknown') }}\n"),
        _sql("models/marts/mart.sql", "select * from {{ ref('up') }}\n"),
        store=store,
    )
    skipped = [w for w in warnings if "for mart" in w and "could not be resolved" in w]
    assert len(skipped) == 1, warnings
    assert "this plan changes" in skipped[0]


def test_a_ref_cycle_is_named(dbt_project_dir: Path):
    warnings = _contract_plan(
        dbt_project_dir,
        _schema(),
        _sql("models/marts/mart.sql", "select * from {{ ref('mart') }}\n"),
    )
    assert any("cycle" in w for w in warnings), warnings


def test_a_declaration_against_the_final_cte_is_refused_for_an_absent_column(
    dbt_project_dir: Path, tmp_path: Path, capsys
):
    payload = tmp_path / "edits.json"
    payload.write_text(
        json.dumps(
            {
                "edits": [
                    {
                        "path": "models/marts/mart.sql",
                        "kind": "model_sql",
                        "content": (
                            "with final as (select 1 as id) select * from final\n"
                        ),
                    }
                ],
                "declarations": [
                    {
                        "model": "mart",
                        "description": "One row per thing.",
                        "grain": ["id"],
                        "columns": {"absent": {"role": "attribute"}},
                    }
                ],
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
            "x",
            "--edits-file",
            str(payload),
        ],
        capsys,
    )
    assert rc != 0
    assert "absent" in json.dumps(envelope["errors"])


def test_column_contract_reports_an_unaliased_macro_column_as_unresolvable(
    dbt_project_dir: Path,
):
    """A macro standing in for a whole column (no alias) can expand to any
    number of real columns once dbt renders it, so it is unresolvable exactly
    like `select *` -- not a guessable single column named `__dex_jinja__`."""

    model_edit = _cfg_edit(
        "models/staging/stg_customers.sql",
        "select {{ dbt_utils.star(ref('raw_customers')) }} from raw_customers\n",
        transform.EditKind.MODEL_SQL,
    )
    plan, _diffs, warnings = transform.plan(
        "splat it",
        [model_edit],
        dbt_project_dir,
        repo_root=dbt_project_dir.parent,
        store=FilesystemStore(dbt_project_dir.parent),
    )
    assert plan.plan_id
    assert any("could not be resolved statically" in w for w in warnings), warnings


def test_column_contract_resolves_an_aliased_macro_column(dbt_project_dir: Path):
    """A macro call given an explicit alias has a known output name regardless
    of what expression computed it, so it is resolvable, unlike a bare macro
    standing in for an unknown number of columns."""

    schema_edit = _cfg_edit(
        "models/staging/schema.yml",
        "version: 2\nmodels:\n  - name: stg_customers\n    columns:\n"
        "      - name: id\n      - name: total\n",
        transform.EditKind.SCHEMA_YML,
    )
    model_edit = _cfg_edit(
        "models/staging/stg_customers.sql",
        "select 1 as id, {{ some_macro(x) }} as total\n",
        transform.EditKind.MODEL_SQL,
    )
    plan, _diffs, warnings = transform.plan(
        "compute total",
        [schema_edit, model_edit],
        dbt_project_dir,
        repo_root=dbt_project_dir.parent,
        store=FilesystemStore(dbt_project_dir.parent),
    )
    assert plan.plan_id
    assert not any(
        "could not be resolved statically" in w or "declares column" in w
        for w in warnings
    ), warnings


def test_column_contract_resolves_a_union_model(dbt_project_dir: Path):
    """#1035: A model ending in a UNION resolves columns from its first branch."""
    schema_edit = _cfg_edit(
        "models/staging/schema.yml",
        "version: 2\nmodels:\n  - name: stg_customers\n    columns:\n"
        "      - name: id\n      - name: name\n",
        transform.EditKind.SCHEMA_YML,
    )
    model_edit = _cfg_edit(
        "models/staging/stg_customers.sql",
        "select 1 as id, 'alice' as name from t1\nunion all\nselect 2, 'bob' from t2\n",
        transform.EditKind.MODEL_SQL,
    )
    plan, _diffs, warnings = transform.plan(
        "union customers",
        [schema_edit, model_edit],
        dbt_project_dir,
        repo_root=dbt_project_dir.parent,
        store=FilesystemStore(dbt_project_dir.parent),
    )
    assert plan.plan_id
    assert not any(
        "could not be resolved statically" in w or "declares column" in w
        for w in warnings
    ), warnings


def test_column_contract_warns_on_union_model_divergence(dbt_project_dir: Path):
    """#1035: A union model whose first branch omits a declared column warns."""
    schema_edit = _cfg_edit(
        "models/staging/schema.yml",
        "version: 2\nmodels:\n  - name: stg_customers\n    columns:\n"
        "      - name: id\n      - name: missing_col\n",
        transform.EditKind.SCHEMA_YML,
    )
    model_edit = _cfg_edit(
        "models/staging/stg_customers.sql",
        "select 1 as id from t1 union all select 2 as id from t2\n",
        transform.EditKind.MODEL_SQL,
    )
    plan, _diffs, warnings = transform.plan(
        "union missing col",
        [schema_edit, model_edit],
        dbt_project_dir,
        repo_root=dbt_project_dir.parent,
        store=FilesystemStore(dbt_project_dir.parent),
    )
    assert plan.plan_id
    assert any(
        "declares column(s) missing_col" in w and "does not produce" in w
        for w in warnings
    ), warnings


def test_a_declaration_against_a_union_model_is_refused_for_an_absent_column(
    dbt_project_dir: Path, tmp_path: Path, capsys
):
    """#1035: Declarations against a union model refuse an undeclared column."""
    payload = tmp_path / "edits.json"
    payload.write_text(
        json.dumps(
            {
                "edits": [
                    {
                        "path": "models/marts/mart.sql",
                        "kind": "model_sql",
                        "content": (
                            "select 1 as id from t1\n"
                            "union all\n"
                            "select 2 as id from t2\n"
                        ),
                    }
                ],
                "declarations": [
                    {
                        "model": "mart",
                        "description": "One row per thing.",
                        "grain": ["id"],
                        "columns": {"absent": {"role": "attribute"}},
                    }
                ],
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
            "x",
            "--edits-file",
            str(payload),
        ],
        capsys,
    )
    assert rc != 0
    assert "absent" in json.dumps(envelope["errors"])


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


def test_plan_guard_refuses_a_delete_that_orphans_a_python_ref(
    dbt_project_dir: Path, tmp_path: Path, capsys
):
    """#657: a surviving dbt.ref() in a Python model blocks deletion."""

    py_path = dbt_project_dir / "models" / "marts" / "py_customers.py"
    py_path.parent.mkdir(parents=True, exist_ok=True)
    py_path.write_text(
        'def model(dbt, session):\n    return dbt.ref("stg_customers")\n',
        encoding="utf-8",
    )
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
    assert "stg_customers" in msg and "py_customers" in msg


def test_plan_guard_refuses_a_delete_that_orphans_a_yaml_snapshot_relation(
    dbt_project_dir: Path, tmp_path: Path, capsys
):
    """#657: a surviving snapshot relation: blocks deletion."""

    snap = dbt_project_dir / "snapshots" / "snapshots.yml"
    snap.parent.mkdir(parents=True, exist_ok=True)
    snap.write_text(
        "snapshots:\n"
        "  - name: customers_snapshot\n"
        "    relation: ref('stg_customers')\n",
        encoding="utf-8",
    )
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
    assert "stg_customers" in msg and "snapshots.yml" in msg


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


# --- what the delete guard sees now ----------------------------------------------
#
# The guard reads the reference index rather than a regex over raw file text.
# Three things change, and each is a case that used to be answered wrongly.


def test_a_seed_row_that_merely_reads_like_a_ref_no_longer_blocks_a_delete(
    dbt_project_dir: Path, tmp_path: Path, capsys, monkeypatch
):
    _stub_parse_ok(monkeypatch)
    (dbt_project_dir / "seeds").mkdir(parents=True, exist_ok=True)
    (dbt_project_dir / "seeds" / "notes.csv").write_text(
        "id,note\n1,\"see ref('stg_customers') for the old shape\"\n",
        encoding="utf-8",
    )
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
    # A seed's rows are data. The text-scanning guard read them as source and
    # refused a delete that breaks nothing.
    assert rc == 0, envelope


def test_a_two_argument_ref_to_a_deleted_model_is_now_caught(
    dbt_project_dir: Path, tmp_path: Path, capsys, monkeypatch
):
    _stub_parse_ok(monkeypatch)
    (dbt_project_dir / "models" / "marts").mkdir(parents=True, exist_ok=True)
    (dbt_project_dir / "models" / "marts" / "mart_customers.sql").write_text(
        "select * from {{ ref('some_package', 'stg_customers') }}\n",
        encoding="utf-8",
    )
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
    # The regex captured the first argument, so this dangling reference used to
    # register as a reference to a model called `some_package`, and passed.
    assert rc == 1
    assert "still references" in envelope["errors"][0]
    assert "mart_customers.sql" in envelope["errors"][0]


def test_a_reference_dex_cannot_resolve_warns_instead_of_refusing(
    dbt_project_dir: Path, tmp_path: Path, capsys, monkeypatch
):
    _stub_parse_ok(monkeypatch)
    (dbt_project_dir / "models" / "marts").mkdir(parents=True, exist_ok=True)
    (dbt_project_dir / "models" / "marts" / "dynamic.sql").write_text(
        "select * from {{ ref(var('which_model')) }}\n", encoding="utf-8"
    )
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
    # It might name the deleted model and dex cannot tell. Refusing would be
    # unsatisfiable: no edit the caller could make would make it resolvable.
    assert rc == 0, envelope
    assert any("could not resolve" in warning for warning in envelope["warnings"])
    assert any("dynamic.sql:1" in warning for warning in envelope["warnings"])


def test_plan_deletes_empty_scaffold_gitkeep_when_first_model_lands(tmp_path: Path):
    """Issue #653: the first real file into a Dex-scaffolded folder drops `.gitkeep`."""

    from exmergo_dex_core.edits import EditOp
    from exmergo_dex_core.storage import MemoryStore
    from exmergo_dex_core.transform.plans import EditKind, PlanEdit
    from exmergo_dex_core.transform.plans import plan as make_plan

    project = tmp_path / "analytics"
    staging = project / "models" / "staging"
    staging.mkdir(parents=True)
    (project / "dbt_project.yml").write_text(
        "name: analytics\nversion: '1.0'\nprofile: analytics\n"
        "model-paths: ['models']\n",
        encoding="utf-8",
    )
    (project / "profiles.yml").write_text(
        "analytics:\n  target: dev\n  outputs:\n    dev:\n      type: duckdb\n"
        "      path: ../warehouse.duckdb\n",
        encoding="utf-8",
    )
    (staging / ".gitkeep").write_text("", encoding="utf-8")
    # A non-empty keeper in marts must survive.
    marts = project / "models" / "marts"
    marts.mkdir(parents=True)
    (marts / ".gitkeep").write_text("keep me\n", encoding="utf-8")

    store = MemoryStore()
    edits = [
        PlanEdit(
            path="models/staging/stg_orders.sql",
            new_content="select 1 as id\n",
            kind=EditKind.MODEL_SQL,
        )
    ]
    new_plan, diffs, _warnings = make_plan(
        "scaffold stg_orders",
        edits,
        project,
        tmp_path,
        store=store,
    )
    by_path = {e.path: e for e in new_plan.edits}
    assert "models/staging/.gitkeep" in by_path
    assert by_path["models/staging/.gitkeep"].op is EditOp.DELETE
    assert "models/marts/.gitkeep" not in by_path
    assert any(d["path"] == "models/staging/.gitkeep" for d in diffs)


# --- reserved-word column names need `quote: true` for their tests (#651) ----


def _reserved_plan(dbt_project_dir: Path, schema: str) -> list[str]:
    edit = _cfg_edit("models/staging/schema.yml", schema, transform.EditKind.SCHEMA_YML)
    _plan, _diffs, warnings = transform.plan(
        "test a reserved-word column",
        [edit],
        dbt_project_dir,
        repo_root=dbt_project_dir.parent,
        store=FilesystemStore(dbt_project_dir.parent),
        dialect="duckdb",
    )
    return [w for w in warnings if "reserved word" in w]


@pytest.mark.parametrize(
    ("schema", "named"),
    [
        (
            "version: 2\nmodels:\n  - name: stg_events\n    columns:\n"
            "      - name: window\n        data_tests: [not_null]\n",
            "window of stg_events",
        ),
        (
            "version: 2\nsources:\n  - name: raw\n    tables:\n"
            "      - name: events\n        columns:\n          - name: window\n"
            "            tests:\n              - accepted_values:\n"
            "                  values: [day]\n",
            "window of raw.events",
        ),
    ],
)
def test_reserved_column_with_tests_and_no_quote_warns(
    dbt_project_dir: Path, schema: str, named: str
):
    warnings = _reserved_plan(dbt_project_dir, schema)
    assert len(warnings) == 1 and named in warnings[0], warnings
    assert "quote: true" in warnings[0]


@pytest.mark.parametrize(
    "schema",
    [
        # Quoted: dbt writes it quoted, so the test compiles.
        "version: 2\nsources:\n  - name: raw\n    tables:\n"
        "      - name: events\n        columns:\n          - name: window\n"
        "            quote: true\n            tests: [not_null]\n",
        # No tests: nothing writes the column into SQL at plan time.
        "version: 2\nmodels:\n  - name: stg_events\n    columns:\n"
        "      - name: window\n        description: the window\n",
    ],
)
def test_reserved_column_quoted_or_untested_is_silent(
    dbt_project_dir: Path, schema: str
):
    assert _reserved_plan(dbt_project_dir, schema) == []


# --- #1166: the package check reads the post-edit tree --------------------------

_DBT_UTILS = 'packages:\n  - package: dbt-labs/dbt_utils\n    version: "1.3.0"\n'
_LOCAL_PKG = "packages:\n  - local: ../shared_pkg\n"
_SCHEMA_EDIT = {
    "path": "models/staging/schema.yml",
    "kind": "schema_yml",
    "content": (
        "version: 2\nmodels:\n  - name: stg_customers\n    columns:\n      - name: id\n"
    ),
}


def _plan(
    tmp_path: Path, edits: list[dict], capsys, declarations: list[dict] | None = None
) -> tuple[int, dict]:
    """Plan ``edits`` (and any ``declarations``) through the CLI."""

    payload = tmp_path / "edits.json"
    payload.write_text(
        json.dumps({"edits": edits, "declarations": declarations or []}),
        encoding="utf-8",
    )
    return _run(
        [
            "--repo-root",
            str(tmp_path),
            "transform",
            "plan",
            "declare and use a package",
            "--edits-file",
            str(payload),
        ],
        capsys,
    )


def _install(project: Path, directory: str, name: str) -> None:
    """Lay down a package directory the way `dbt deps` would leave it."""

    package = project / "dbt_packages" / directory
    package.mkdir(parents=True)
    (package / "dbt_project.yml").write_text(
        f"name: {name}\nversion: '1.0.0'\nconfig-version: 2\n", encoding="utf-8"
    )


def _parse_passes(monkeypatch) -> list[list]:
    """Stand in for dbt's parse, recording each call, so a test can say the
    package gate let the plan through without depending on a real install."""

    import importlib

    build_module = importlib.import_module("exmergo_dex_core.transform.build")
    calls: list[list] = []

    def fake(project_dir, edits, **_kwargs):
        calls.append(list(edits))
        return {"available": True, "reason": None, "success": True, "messages": []}

    monkeypatch.setattr(build_module, "shadow_parse", fake)
    return calls


@pytest.mark.parametrize(
    ("spec", "named"),
    [(_DBT_UTILS, "dbt-labs/dbt_utils@1.3.0"), (_LOCAL_PKG, "../shared_pkg")],
    ids=["hub", "local"],
)
def test_plan_refuses_a_package_its_own_edit_declares_before_dbt_parses(
    dbt_project_dir: Path, tmp_path: Path, capsys, monkeypatch, spec: str, named: str
):
    calls = _parse_passes(monkeypatch)
    rc, envelope = _plan(
        tmp_path,
        [
            {"path": "packages.yml", "kind": "packages_yml", "content": spec},
            _SCHEMA_EDIT,
        ],
        capsys,
    )
    assert rc == 1 and envelope["status"] == "error"
    message = envelope["errors"][0]
    assert "this plan declares dbt packages that are not installed" in message
    assert named in message
    assert "transform deps" in message
    assert "dbt deps" not in message
    assert "packages.yml change on its own" in message
    # Refused before dbt ran, and planning installed nothing.
    assert calls == []
    assert not (dbt_project_dir / "dbt_packages").exists()


@pytest.mark.parametrize(
    ("spec", "directory", "name"),
    [(_DBT_UTILS, "dbt_utils", "dbt_utils"), (_LOCAL_PKG, "shared_pkg", "shared")],
    ids=["hub", "local"],
)
def test_plan_declaring_an_installed_package_reaches_the_parse(
    dbt_project_dir: Path,
    tmp_path: Path,
    capsys,
    monkeypatch,
    spec: str,
    directory: str,
    name: str,
):
    _install(dbt_project_dir, directory, name)
    calls = _parse_passes(monkeypatch)
    rc, envelope = _plan(
        tmp_path,
        [
            {"path": "packages.yml", "kind": "packages_yml", "content": spec},
            _SCHEMA_EDIT,
        ],
        capsys,
    )
    assert rc == 0 and envelope["status"] == "ok", envelope["errors"]
    assert len(calls) == 1


def test_a_packages_only_plan_is_stored_so_the_two_step_route_works(
    dbt_project_dir: Path, tmp_path: Path, capsys, monkeypatch
):
    """The refusal's own advice has to be followable: a plan carrying only the
    `packages_yml` edit needs no parse, so it is stored even though the package
    is not installed yet."""

    calls = _parse_passes(monkeypatch)
    rc, envelope = _plan(
        tmp_path,
        [{"path": "packages.yml", "kind": "packages_yml", "content": _DBT_UTILS}],
        capsys,
    )
    assert rc == 0 and envelope["status"] == "ok", envelope["errors"]
    assert envelope["data"]["paths"] == ["packages.yml"]
    assert calls == []


def test_a_plan_that_stops_declaring_an_uninstalled_package_is_not_refused(
    dbt_project_dir: Path, tmp_path: Path, capsys, monkeypatch
):
    (dbt_project_dir / "packages.yml").write_text(_DBT_UTILS, encoding="utf-8")
    _install(dbt_project_dir, "shared_pkg", "shared")
    calls = _parse_passes(monkeypatch)
    rc, envelope = _plan(
        tmp_path,
        [
            {"path": "packages.yml", "kind": "packages_yml", "content": _LOCAL_PKG},
            _SCHEMA_EDIT,
        ],
        capsys,
    )
    assert rc == 0 and envelope["status"] == "ok", envelope["errors"]
    assert len(calls) == 1


def test_a_plan_adding_a_package_names_the_already_missing_ones_apart(
    dbt_project_dir: Path, tmp_path: Path, capsys, monkeypatch
):
    (dbt_project_dir / "packages.yml").write_text(_LOCAL_PKG, encoding="utf-8")
    _parse_passes(monkeypatch)
    rc, envelope = _plan(
        tmp_path,
        [
            {
                "path": "packages.yml",
                "kind": "packages_yml",
                "content": _LOCAL_PKG
                + '  - package: dbt-labs/dbt_utils\n    version: "1.3.0"\n',
            },
            _SCHEMA_EDIT,
        ],
        capsys,
    )
    assert rc == 1
    message = envelope["errors"][0]
    declared, _, already = message.partition("already declares")
    assert "dbt-labs/dbt_utils@1.3.0" in declared
    assert "../shared_pkg" not in declared
    assert "../shared_pkg" in already


def test_a_control_flow_model_with_a_new_package_is_refused_before_compile(
    dbt_project_dir: Path, tmp_path: Path, capsys, monkeypatch
):
    import importlib

    compile_module = importlib.import_module("exmergo_dex_core.transform.compile")

    def must_not_compile(*_args, **_kwargs):
        raise AssertionError("dbt compile ran before the package check")

    monkeypatch.setattr(compile_module, "compile_models", must_not_compile)
    rc, envelope = _plan(
        tmp_path,
        [
            {"path": "packages.yml", "kind": "packages_yml", "content": _DBT_UTILS},
            {
                "path": "models/staging/stg_flagged.sql",
                "kind": "model_sql",
                "content": "{% if true %}select 1 as id{% else %}select 2 as id"
                "{% endif %}\n",
            },
        ],
        capsys,
    )
    assert rc == 1 and envelope["status"] == "error"
    message = envelope["errors"][0]
    assert "dbt-labs/dbt_utils@1.3.0" in message
    assert "transform deps" in message
    assert "dbt deps" not in message


def test_a_mart_declaration_with_a_new_package_is_refused_before_any_parse(
    dbt_project_dir: Path, tmp_path: Path, capsys, monkeypatch
):
    """The derivation of a declared mart runs dbt's parse too; the package check
    comes before it, so no dbt run is spent on a plan that cannot parse."""

    calls = _parse_passes(monkeypatch)
    rc, envelope = _plan(
        tmp_path,
        [
            {"path": "packages.yml", "kind": "packages_yml", "content": _DBT_UTILS},
            {
                "path": "models/marts/fct_orders.sql",
                "kind": "model_sql",
                "content": "select 1 as order_id, 2.0 as amount\n",
            },
        ],
        capsys,
        declarations=[
            {
                "model": "fct_orders",
                "description": "One row per order.",
                "grain": ["order_id"],
                "columns": {
                    "order_id": {"role": "key"},
                    "amount": {"role": "measure", "aggregation": "sum"},
                },
            }
        ],
    )
    assert rc == 1 and envelope["status"] == "error", envelope
    message = envelope["errors"][0]
    assert "this plan declares dbt packages that are not installed" in message
    assert "dbt deps" not in message
    assert calls == []


def test_a_sql_only_mart_edit_with_packages_missing_skips_the_derivation_parse(
    dbt_project_dir: Path, tmp_path: Path, capsys, monkeypatch
):
    """A SQL-only edit to a declared mart needs no dbt parse of its own, so the
    plan is stored; the derivation it triggers is left unvalidated in Dex's words
    instead of spending two parses that dbt would refuse."""

    (dbt_project_dir / "models" / "marts").mkdir(parents=True)
    (dbt_project_dir / "models" / "marts" / "fct_orders.sql").write_text(
        "select 1 as order_id, current_date as ordered_at, 2.0 as amount\n",
        encoding="utf-8",
    )
    (dbt_project_dir / "models" / "marts" / "fct_orders.yml").write_text(
        "version: 2\n"
        "models:\n"
        "  - name: fct_orders\n"
        "    description: One row per order.\n"
        "    config:\n"
        "      meta:\n"
        "        dex:\n"
        "          grain: [order_id]\n"
        "    columns:\n"
        "      - name: order_id\n"
        "        config: {meta: {dex: {role: key}}}\n"
        "      - name: ordered_at\n"
        "        config: {meta: {dex: {role: time}}}\n"
        "      - name: amount\n"
        "        config: {meta: {dex: {role: measure, aggregation: sum}}}\n",
        encoding="utf-8",
    )
    (dbt_project_dir / "packages.yml").write_text(_DBT_UTILS, encoding="utf-8")
    calls = _parse_passes(monkeypatch)
    rc, envelope = _plan(
        tmp_path,
        [
            {
                "path": "models/marts/fct_orders.sql",
                "kind": "model_sql",
                "content": "select 1 as order_id, current_date as ordered_at, "
                "3.0 as amount\n",
            }
        ],
        capsys,
    )
    assert rc == 0 and envelope["status"] == "ok", envelope["errors"]
    assert calls == []
    derived = envelope["data"]["semantic_proposal"]["models"]["fct_orders"]
    assert derived["status"] == "unvalidated"
    parse = derived["validation"]["parse"]
    assert parse["result"] == "not_run"
    assert "transform deps" in parse["detail"]
    assert "dbt deps" not in parse["detail"]
    assert envelope["data"]["paths"] == ["models/marts/fct_orders.sql"]
