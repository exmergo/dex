"""`snapshot_sql` end to end: the block's structure, the config fields each
strategy cannot work without, containment to the snapshot paths, and the two
things a snapshot shares with a model that a macro does not (it builds a
relation, and it is ``ref()``-able, so deleting one is guarded)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from exmergo_dex_core.cli import main
from exmergo_dex_core.dbt_project import DbtProjectError, node_files
from exmergo_dex_core.dbt_project import load as load_project
from exmergo_dex_core.storage import FilesystemStore
from exmergo_dex_core.transform.plans import EditKind, PlanEdit, PlanError
from exmergo_dex_core.transform.plans import plan as make_plan
from exmergo_dex_core.transform.validate import EditValidationError, validate_edit

TIMESTAMP_SNAPSHOT = """{% snapshot snap_customers %}
{{
    config(
        target_schema='snapshots',
        unique_key='id',
        strategy='timestamp',
        updated_at='updated_at',
    )
}}

select id, email, updated_at from {{ ref('stg_customers') }}

{% endsnapshot %}
"""

CHECK_SNAPSHOT = """{% snapshot snap_orders %}
{{ config(unique_key='id', strategy='check', check_cols=['status', 'total']) }}
select id, status, total from {{ ref('stg_customers') }}
{% endsnapshot %}
"""


def _run(argv: list[str], capsys) -> tuple[int, dict]:
    rc = main(argv)
    out = capsys.readouterr().out
    assert out.count("\n") == 1, "exactly one line on stdout"
    return rc, json.loads(out)


def _edits_file(tmp_path: Path, *entries: dict) -> Path:
    payload = tmp_path / "edits.json"
    payload.write_text(json.dumps({"edits": list(entries)}), encoding="utf-8")
    return payload


def _snapshot(content: str, path: str = "snapshots/snap_customers.sql") -> PlanEdit:
    return PlanEdit(path=path, kind=EditKind.SNAPSHOT_SQL, new_content=content)


# --- the structural check --------------------------------------------------------


@pytest.mark.parametrize("content", [TIMESTAMP_SNAPSHOT, CHECK_SNAPSHOT])
def test_a_well_formed_snapshot_validates(content: str):
    assert validate_edit(_snapshot(content)) == []


@pytest.mark.parametrize(
    ("mutation", "fix_named"),
    [
        pytest.param(lambda s: "select 1\n", "{% snapshot", id="no_block"),
        pytest.param(
            lambda s: s.replace("{% endsnapshot %}", ""),
            "endsnapshot",
            id="unclosed",
        ),
        pytest.param(lambda s: s + s, "exactly one snapshot block", id="two_blocks"),
        pytest.param(
            lambda s: s + "\nselect 1\n", "loose content", id="content_outside"
        ),
        pytest.param(
            lambda s: s.replace("        unique_key='id',\n", ""),
            "unique_key",
            id="no_unique_key",
        ),
        pytest.param(
            lambda s: s.replace("strategy='timestamp'", "strategy='scd2'"),
            "unknown snapshot strategy",
            id="unknown_strategy",
        ),
        pytest.param(
            lambda s: s.replace("        strategy='timestamp',\n", ""),
            "needs a strategy",
            id="no_strategy",
        ),
        pytest.param(
            lambda s: s.replace("        updated_at='updated_at',\n", ""),
            "needs updated_at",
            id="timestamp_without_updated_at",
        ),
        pytest.param(
            lambda s: s.replace(
                "select id, email, updated_at from {{ ref('stg_customers') }}",
                "delete from customers",
            ),
            "read-only SELECT",
            id="not_a_select",
        ),
        pytest.param(
            lambda s: s.replace(
                "select id, email, updated_at from {{ ref('stg_customers') }}", ""
            ),
            "no query",
            id="no_query",
        ),
    ],
)
def test_a_broken_snapshot_is_refused_with_the_fix(mutation, fix_named: str):
    with pytest.raises(EditValidationError, match=fix_named):
        validate_edit(_snapshot(mutation(TIMESTAMP_SNAPSHOT)))


def test_a_check_snapshot_without_check_cols_is_refused():
    broken = CHECK_SNAPSHOT.replace(", check_cols=['status', 'total']", "")
    with pytest.raises(EditValidationError, match="needs check_cols"):
        validate_edit(_snapshot(broken, "snapshots/snap_orders.sql"))


# --- containment and kind agreement ----------------------------------------------


def test_a_snapshot_plans_applies_and_lands_as_a_create_diff(
    dbt_project_dir: Path, tmp_path: Path, capsys
):
    payload = _edits_file(
        tmp_path,
        {
            "path": "snapshots/snap_customers.sql",
            "kind": "snapshot_sql",
            "content": TIMESTAMP_SNAPSHOT,
        },
        {
            "path": "snapshots/schema.yml",
            "kind": "schema_yml",
            "content": (
                "version: 2\n"
                "snapshots:\n"
                "  - name: snap_customers\n"
                "    description: customer history\n"
            ),
        },
    )
    rc, envelope = _run(
        [
            "--repo-root",
            str(tmp_path),
            "transform",
            "plan",
            "capture customer history",
            "--edits-file",
            str(payload),
        ],
        capsys,
    )
    assert rc == 0, envelope["errors"]
    assert envelope["data"]["paths"] == [
        "snapshots/snap_customers.sql",
        "snapshots/schema.yml",
    ]
    diffs = {d["path"]: d for d in envelope["diffs"]}
    # A create: nothing on the removed side, the whole file on the added one.
    created = diffs["snapshots/snap_customers.sql"]
    assert created["op"] == "create"
    assert created["deletions"] == 0
    assert "{% snapshot snap_customers %}" in created["unified"]

    rc, envelope = _run(["--repo-root", str(tmp_path), "transform", "apply"], capsys)
    assert rc == 0, envelope["errors"]
    written = dbt_project_dir / "snapshots" / "snap_customers.sql"
    assert written.read_text(encoding="utf-8") == TIMESTAMP_SNAPSHOT


def test_custom_snapshot_paths_are_honored(dbt_project_dir: Path, tmp_path: Path):
    project_yml = dbt_project_dir / "dbt_project.yml"
    project_yml.write_text(
        project_yml.read_text(encoding="utf-8") + 'snapshot-paths: ["history"]\n',
        encoding="utf-8",
    )
    store = FilesystemStore(tmp_path)
    stored, _diffs, _warnings = make_plan(
        "capture",
        [_snapshot(TIMESTAMP_SNAPSHOT, "history/snap_customers.sql")],
        dbt_project_dir,
        tmp_path,
        store=store,
    )
    assert stored.edits[0].path == "history/snap_customers.sql"

    # And the default location is no longer part of the surface once moved:
    # containment refuses it before the kind is ever consulted, and the refusal
    # names where the snapshot family actually is now.
    with pytest.raises(DbtProjectError, match=r"snapshot \(history\)"):
        make_plan(
            "capture",
            [_snapshot(TIMESTAMP_SNAPSHOT)],
            dbt_project_dir,
            tmp_path,
            store=store,
        )


def test_kind_and_surface_must_agree_in_both_directions(
    dbt_project_dir: Path, tmp_path: Path
):
    store = FilesystemStore(tmp_path)
    with pytest.raises(PlanError, match="snapshot paths"):
        make_plan(
            "bad",
            [_snapshot(TIMESTAMP_SNAPSHOT, "models/staging/snap_customers.sql")],
            dbt_project_dir,
            tmp_path,
            store=store,
        )

    model_in_snapshots = PlanEdit(
        path="snapshots/x.sql",
        kind=EditKind.MODEL_SQL,
        new_content="select 1\n",
    )
    with pytest.raises(PlanError, match="snapshot_sql"):
        make_plan("bad", [model_in_snapshots], dbt_project_dir, tmp_path, store=store)


def test_the_misfiled_refusal_comes_from_dex_not_from_dbt(
    dbt_project_dir: Path, tmp_path: Path, capsys
):
    # Through the command layer, where dbt's own parser also runs. dex refuses
    # first, so the caller reads "must live under the project's snapshot paths"
    # rather than dbt's "Encountered unknown tag 'snapshot'", which names no
    # fix at all. Found by dogfooding: the direct-to-`plan` tests above cannot
    # see what the command does before it.
    payload = _edits_file(
        tmp_path,
        {
            "path": "models/staging/snap_customers.sql",
            "kind": "snapshot_sql",
            "content": TIMESTAMP_SNAPSHOT,
        },
    )
    rc, envelope = _run(
        [
            "--repo-root",
            str(tmp_path),
            "transform",
            "plan",
            "misfiled",
            "--edits-file",
            str(payload),
        ],
        capsys,
    )
    assert rc != 0
    assert "snapshot paths" in envelope["errors"][0]
    assert "unknown tag" not in envelope["errors"][0]


def test_a_snapshot_missing_unique_key_is_refused_with_the_fix_not_dbt_s_message(
    dbt_project_dir: Path, tmp_path: Path, capsys
):
    payload = _edits_file(
        tmp_path,
        {
            "path": "snapshots/snap_customers.sql",
            "kind": "snapshot_sql",
            "content": TIMESTAMP_SNAPSHOT.replace("        unique_key='id',\n", ""),
        },
    )
    rc, envelope = _run(
        [
            "--repo-root",
            str(tmp_path),
            "transform",
            "plan",
            "no key",
            "--edits-file",
            str(payload),
        ],
        capsys,
    )
    assert rc != 0
    # dbt says "Snapshots must be configured with a 'strategy' and
    # 'unique_key'", which names neither which one is missing nor what it is
    # for. dex names both.
    assert (
        "needs a unique_key, the column dbt tracks each row by"
        in (envelope["errors"][0])
    )


def test_a_schema_yml_beside_a_snapshot_is_accepted(
    dbt_project_dir: Path, tmp_path: Path
):
    edit = PlanEdit(
        path="snapshots/schema.yml",
        kind=EditKind.SCHEMA_YML,
        new_content="version: 2\nsnapshots:\n  - name: snap_customers\n",
    )
    stored, _diffs, _warnings = make_plan(
        "document the snapshot",
        [edit],
        dbt_project_dir,
        tmp_path,
        store=FilesystemStore(tmp_path),
    )
    assert stored.edits[0].path == "snapshots/schema.yml"


# --- the project view: a snapshot is a node, not just a file ---------------------


def test_a_snapshot_is_loaded_and_counts_as_a_node(dbt_project_dir: Path):
    snapshots = dbt_project_dir / "snapshots"
    snapshots.mkdir()
    (snapshots / "snap_customers.sql").write_text(TIMESTAMP_SNAPSHOT, encoding="utf-8")
    (snapshots / "schema.yml").write_text("version: 2\n", encoding="utf-8")

    view = load_project(dbt_project_dir)
    assert "snapshots/snap_customers.sql" in view.files
    assert "snapshots/schema.yml" in view.files
    # The properties YAML is loaded (so editing it pins a real hash) but builds
    # nothing, so it is not a node.
    assert set(node_files(view)) == {
        "models/staging/stg_customers.sql",
        "snapshots/snap_customers.sql",
    }


def test_deleting_a_snapshot_a_model_still_refs_is_refused(
    dbt_project_dir: Path, tmp_path: Path
):
    snapshots = dbt_project_dir / "snapshots"
    snapshots.mkdir()
    (snapshots / "snap_customers.sql").write_text(TIMESTAMP_SNAPSHOT, encoding="utf-8")
    (dbt_project_dir / "models" / "staging" / "uses_snapshot.sql").write_text(
        "select * from {{ ref('snap_customers') }}\n", encoding="utf-8"
    )
    delete = PlanEdit(
        path="snapshots/snap_customers.sql",
        kind=EditKind.SNAPSHOT_SQL,
        op="delete",
    )
    with pytest.raises(PlanError, match="snap_customers"):
        make_plan(
            "drop the snapshot",
            [delete],
            dbt_project_dir,
            tmp_path,
            store=FilesystemStore(tmp_path),
        )


# --- the build ------------------------------------------------------------------


def test_a_snapshot_is_priced_and_a_seed_is_not():
    from exmergo_dex_core.transform.build import _PRICED_RESOURCE_TYPES

    # A snapshot writes a table, so it scans and is priced; a seed loads a local
    # CSV and issues no billed build statement. Pinned from both sides so a
    # future edit to the set cannot quietly flip either.
    assert "snapshot" in _PRICED_RESOURCE_TYPES
    assert "seed" not in _PRICED_RESOURCE_TYPES


def test_a_dev_build_materializes_an_applied_snapshot(
    dbt_project_dir: Path, tmp_path: Path, capsys
):
    duckdb = pytest.importorskip("duckdb")

    payload = _edits_file(
        tmp_path,
        {
            "path": "snapshots/snap_customers.sql",
            "kind": "snapshot_sql",
            "content": TIMESTAMP_SNAPSHOT.replace(
                "select id, email, updated_at from {{ ref('stg_customers') }}",
                "select 1 as id, 'a' as email, "
                "cast('2026-01-01' as timestamp) as updated_at",
            ).replace("target_schema='snapshots',\n        ", ""),
        },
    )
    rc, envelope = _run(
        [
            "--repo-root",
            str(tmp_path),
            "transform",
            "plan",
            "capture customer history",
            "--edits-file",
            str(payload),
        ],
        capsys,
    )
    assert rc == 0, envelope["errors"]
    rc, _envelope = _run(["--repo-root", str(tmp_path), "transform", "apply"], capsys)
    assert rc == 0

    rc, envelope = _run(
        [
            "--repo-root",
            str(tmp_path),
            "transform",
            "build",
            "--target",
            "dev",
            "--confirm",
        ],
        capsys,
    )
    assert rc == 0, envelope["errors"]
    statuses = {n["name"]: n["status"] for n in envelope["data"]["nodes"]}
    assert statuses["snap_customers"] == "success"

    con = duckdb.connect(str(tmp_path / "dev.duckdb"), read_only=True)
    try:
        rows = con.execute("select id from snap_customers").fetchall()
        assert rows == [(1,)]
    finally:
        con.close()
