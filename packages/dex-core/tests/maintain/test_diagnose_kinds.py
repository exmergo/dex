"""``dex maintain diagnose``: the cause kinds ``dex demo`` does not seed (#1476).

Each fixture is a shop project in a real git repository: a source
(``raw.orders``), a seed (``status_codes``), three models (``stg_orders`` from
the source, ``dim_status`` from the seed, ``fct_orders`` joining the two), and
three tests (``not_null`` on ``stg_orders.amount``, a ``relationships`` test from
``fct_orders`` to ``dim_status``, and ``dbt_utils.unique_combination_of_columns``
on ``fct_orders``). Elementary's tables in a DuckDB file hold four runs of one
failing test: the first two pass at commit ``c1``, the last two fail at commit
``c2``, so the break's commit range is ``c1..c2``.

Each kind has a pair: the warehouse with that defect, where diagnosis names the
cause and its evidence, and the same warehouse without it, where diagnosis
reports no cause and lists the candidate as ruled out.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from exmergo_dex_core import DexEngine
from exmergo_dex_core.cache import ColumnProfile, Dataset
from exmergo_dex_core.maintain.results import DiagnoseResult
from exmergo_dex_core.maintain.snapshot import Snapshot, WarehouseBaseline
from exmergo_dex_core.results import to_envelope

from .test_diagnose import _git
from .test_incident_sources import RELEASE, SCHEMA, Result, Run, _create, _warehouse

duckdb = pytest.importorskip("duckdb")

PROJECT = "shop"
SOURCE = "source.shop.raw.orders"
SEED = "seed.shop.status_codes"
STG = "model.shop.stg_orders"
DIM = "model.shop.dim_status"
FCT = "model.shop.fct_orders"
NOT_NULL = "test.shop.not_null_stg_orders_amount.1a2b3c"
RELATIONSHIPS = "test.shop.relationships_fct_orders_status_code__dim_status.4d5e6f"
COMBO = "test.shop.dbt_utils_unique_combination_of_columns_fct_orders.7a8b9c"

LOCK_V1 = """packages:
  - package: dbt-labs/dbt_utils
    version: 1.1.1
    name: dbt_utils
  - package: dbt-labs/codegen
    version: 0.12.1
    name: codegen
sha1_hash: 0000000000000000000000000000000000000000
"""
PACKAGES_YML = """packages:
  - package: dbt-labs/dbt_utils
    version: [">=1.0.0", "<2.0.0"]
  - package: dbt-labs/codegen
    version: [">=0.12.0", "<1.0.0"]
"""


def _model(uid: str, sql: str, deps: Sequence[str], columns: Sequence[str]) -> dict:
    name = uid.split(".")[-1]
    return {
        "unique_id": uid,
        "resource_type": "model",
        "package_name": PROJECT,
        "name": name,
        "database": "dev",
        "schema": "main",
        "alias": name,
        "relation_name": f'"dev"."main"."{name}"',
        "original_file_path": f"models/{name}.sql",
        "patch_path": f"{PROJECT}://models/schema.yml",
        "raw_code": sql,
        "config": {"enabled": True, "materialized": "table"},
        "columns": {c: {"name": c, "data_type": None} for c in columns},
        "depends_on": {"nodes": list(deps), "macros": []},
    }


def _test(
    uid: str,
    name: str,
    model: str,
    deps: Sequence[str],
    kwargs: dict[str, Any],
    *,
    column: str | None = None,
    namespace: str | None = None,
    macros: Sequence[str] = (),
) -> dict:
    meta: dict[str, Any] = {"name": name, "kwargs": kwargs}
    if namespace:
        meta["namespace"] = namespace
    return {
        "unique_id": uid,
        "resource_type": "test",
        "package_name": PROJECT,
        "name": uid.split(".")[2],
        "column_name": column,
        "original_file_path": "models/schema.yml",
        "config": {"enabled": True, "severity": "ERROR"},
        "test_metadata": meta,
        "depends_on": {"nodes": list(deps), "macros": list(macros)},
        "attached_node": model,
    }


def _manifest() -> dict[str, Any]:
    nodes: dict[str, Any] = {
        SEED: {
            "unique_id": SEED,
            "resource_type": "seed",
            "package_name": PROJECT,
            "name": "status_codes",
            "database": "dev",
            "schema": "main",
            "original_file_path": "seeds/status_codes.csv",
            "checksum": {"name": "sha256", "checksum": "x"},
            "config": {"enabled": True},
            "depends_on": {"nodes": [], "macros": []},
        },
        STG: _model(
            STG,
            "select order_id, status_code, amount from {{ source('raw', 'orders') }}",
            [SOURCE],
            ["order_id", "status_code", "amount"],
        ),
        DIM: _model(
            DIM,
            "select status_code, label from {{ ref('status_codes') }}",
            [SEED],
            ["status_code", "label"],
        ),
        FCT: _model(
            FCT,
            "select o.order_id, o.status_code, o.amount, d.label "
            "from {{ ref('stg_orders') }} o left join {{ ref('dim_status') }} d "
            "using (status_code)",
            [STG, DIM],
            ["order_id", "status_code", "amount", "label"],
        ),
        NOT_NULL: _test(
            NOT_NULL,
            "not_null",
            STG,
            [STG],
            {"column_name": "amount"},
            column="amount",
        ),
        RELATIONSHIPS: _test(
            RELATIONSHIPS,
            "relationships",
            FCT,
            [DIM, FCT],
            {"column_name": "status_code", "to": "ref('dim_status')"},
            column="status_code",
        ),
        COMBO: _test(
            COMBO,
            "unique_combination_of_columns",
            FCT,
            [FCT],
            {"combination_of_columns": ["order_id", "status_code"]},
            namespace="dbt_utils",
            macros=["macro.dbt_utils.test_unique_combination_of_columns"],
        ),
    }
    for table in ("dbt_invocations", "elementary_test_results", "dbt_run_results"):
        uid = f"model.elementary.{table}"
        nodes[uid] = {
            "unique_id": uid,
            "resource_type": "model",
            "package_name": "elementary",
            "name": table,
            "database": "dev",
            "schema": SCHEMA,
            "config": {"enabled": True},
        }
    for hook in ("on-run-start", "on-run-end"):
        uid = f"operation.elementary.elementary-{hook}-0"
        nodes[uid] = {
            "unique_id": uid,
            "resource_type": "operation",
            "package_name": "elementary",
        }
    return {
        "metadata": {"project_name": PROJECT, "adapter_type": "duckdb"},
        "nodes": nodes,
        "sources": {
            SOURCE: {
                "unique_id": SOURCE,
                "resource_type": "source",
                "package_name": PROJECT,
                "source_name": "raw",
                "name": "orders",
                "identifier": "orders",
                "database": "dev",
                "schema": "raw",
                "original_file_path": "models/sources.yml",
            }
        },
        "macros": {
            "macro.dbt_utils.test_unique_combination_of_columns": {
                "unique_id": "macro.dbt_utils.test_unique_combination_of_columns",
                "package_name": "dbt_utils",
                "original_file_path": "macros/generic_tests/unique_combo.sql",
            }
        },
        "disabled": {},
    }


#: The project's files at the first commit. Each fixture edits some at ``c2``.
BASE_FILES: dict[str, str] = {
    "dbt_project.yml": "name: shop\nversion: '1.0'\nprofile: shop\n",
    "packages.yml": PACKAGES_YML,
    "package-lock.yml": LOCK_V1,
    "seeds/status_codes.csv": "status_code,label\n1,placed\n2,shipped\n",
    "models/sources.yml": "version: 2\nsources:\n  - name: raw\n",
    "models/schema.yml": "version: 2\n",
    "models/stg_orders.sql": "select order_id, status_code, amount from raw\n",
    "models/dim_status.sql": "select status_code, label from status_codes\n",
    "models/fct_orders.sql": "select * from stg_orders left join dim_status\n",
    "README.md": "shop\n",
}


@dataclass
class Shop:
    """One fixture: what the second commit changes, which test fails, and the
    warehouse's state after the break."""

    failing: str = RELATIONSHIPS
    edits: dict[str, str] = field(default_factory=lambda: {"README.md": "shop 2\n"})
    amount_type: str = "INTEGER"
    fct_columns: Sequence[str] = ("order_id", "status_code", "amount", "label")
    snapshot: bool = False
    builds: Callable[[list[Run]], list[tuple]] | None = None


def _runs() -> list[Run]:
    now = datetime.now(UTC).replace(tzinfo=None, minute=0, second=0, microsecond=0)
    return [Run(f"inv-{n}", now - timedelta(hours=5 - n)) for n in range(1, 5)]


def _write(root: Path, files: Mapping[str, str]) -> None:
    for path, text in files.items():
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)


def _commit(root: Path, message: str) -> str:
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "--allow-empty", "-m", message)
    done = subprocess.run(  # noqa: S603
        ["git", "-C", str(root), "rev-parse", "HEAD"],  # noqa: S607
        check=True,
        capture_output=True,
        text=True,
    )
    return done.stdout.strip()


def _snapshot(root: Path, at: datetime) -> None:
    stamp = at.replace(tzinfo=UTC).isoformat()
    snap = Snapshot(
        created_at=stamp,
        connector="duckdb",
        warehouse=WarehouseBaseline(
            datasets=[
                Dataset(
                    identifier="dev.raw.orders",
                    object_type="table",
                    row_count=100,
                    profiled_at=stamp,
                    columns=[
                        ColumnProfile(name="order_id", data_type="INTEGER"),
                        ColumnProfile(name="status_code", data_type="INTEGER"),
                        ColumnProfile(name="amount", data_type="INTEGER"),
                    ],
                )
            ]
        ),
    )
    (root / ".dex").mkdir(exist_ok=True)
    (root / ".dex" / "snapshot.json").write_text(snap.model_dump_json(indent=2))


def _build(tmp_path: Path, shop: Shop) -> tuple[Path, Path]:
    root = tmp_path / "project"
    root.mkdir()
    _git(root, "init", "-q")
    _write(root, BASE_FILES)
    (root / ".gitignore").write_text("target/\ndbt_packages/\n")
    runs = _runs()
    if shop.snapshot:
        _snapshot(root, runs[0].started + timedelta(minutes=30))
    c1 = _commit(root, "c1")
    _write(root, shop.edits)
    c2 = _commit(root, "c2")

    target = root / "target"
    target.mkdir()
    (target / "manifest.json").write_text(json.dumps(_manifest()))
    package = root / "dbt_packages" / "elementary"
    package.mkdir(parents=True)
    (package / "dbt_project.yml").write_text(
        f"name: elementary\nversion: '{RELEASE}'\n"
    )

    results = [
        Result(
            f"r-{run.id}",
            run.id,
            test=shop.failing,
            status="fail" if run.id in ("inv-3", "inv-4") else "pass",
        )
        for run in runs
    ]
    warehouse = _warehouse(tmp_path / "dev.duckdb", runs, results)
    con = duckdb.connect(str(warehouse))
    for run in runs:
        sha = c1 if run.id in ("inv-1", "inv-2") else c2
        con.execute(
            f"UPDATE {SCHEMA}.dbt_invocations SET git_sha = ? WHERE invocation_id = ?",  # noqa: S608
            [sha, run.id],
        )
    _create(con, "dbt_run_results")
    for row in (shop.builds or _in_order)(runs):
        con.execute(
            f"INSERT INTO {SCHEMA}.dbt_run_results "  # noqa: S608
            "(model_execution_id, unique_id, invocation_id, resource_type, status, "
            "execute_started_at, execute_completed_at, created_at) "
            "VALUES (?, ?, ?, 'model', 'success', ?, ?, ?)",
            [f"{row[1]}.{row[0]}", row[0], row[1], row[2], row[3], row[4]],
        )
    con.execute("CREATE SCHEMA raw")
    con.execute(
        f"CREATE TABLE raw.orders (order_id INTEGER, status_code INTEGER, "
        f"amount {shop.amount_type})"
    )
    con.execute("INSERT INTO raw.orders SELECT range, 1, 1 FROM range(1, 101)")
    con.execute("CREATE TABLE main.stg_orders AS SELECT * FROM raw.orders")
    con.execute("CREATE TABLE main.dim_status (status_code INTEGER, label VARCHAR)")
    con.execute("INSERT INTO main.dim_status VALUES (1, 'placed'), (2, 'shipped')")
    columns = {
        "order_id": "1 AS order_id",
        "status_code": "1 AS status_code",
        "amount": "1 AS amount",
        "label": "'placed' AS label",
    }
    selected = ", ".join(columns[c] for c in shop.fct_columns)
    con.execute(f"CREATE TABLE main.fct_orders AS SELECT {selected}")
    con.close()
    return root, warehouse


def _iso(at: datetime) -> str:
    return at.replace(tzinfo=UTC).isoformat()


def _in_order(runs: list[Run]) -> list[tuple]:
    """Every run builds the dimension, then the fact, in one invocation."""

    rows: list[tuple] = []
    for run in runs:
        dim_start = run.started + timedelta(minutes=1)
        fct_start = run.started + timedelta(minutes=2)
        rows.append(
            (
                DIM,
                run.id,
                _iso(dim_start),
                _iso(dim_start + timedelta(seconds=20)),
                run.started + timedelta(minutes=5),
            )
        )
        rows.append(
            (
                FCT,
                run.id,
                _iso(fct_start),
                _iso(fct_start + timedelta(seconds=20)),
                run.started + timedelta(minutes=5),
            )
        )
    return rows


def _split(*, parent_last: bool) -> Callable[[list[Run]], list[tuple]]:
    """The first two runs build both in order. The last two only test: the
    fact and the dimension are built by separate jobs before each, the
    dimension after the fact where ``parent_last``, before it otherwise."""

    def rows(runs: list[Run]) -> list[tuple]:
        held = list(_in_order(runs[:2]))
        for run in runs[2:]:
            fact_at = run.started - timedelta(minutes=10)
            dim_at = run.started - timedelta(minutes=5 if parent_last else 15)
            for uid, at, job in ((FCT, fact_at, "f"), (DIM, dim_at, "d")):
                held.append(
                    (
                        uid,
                        f"{run.id}-{job}",
                        _iso(at),
                        _iso(at + timedelta(seconds=20)),
                        at + timedelta(minutes=1),
                    )
                )
        return held

    return rows


def _diagnose(root: Path, warehouse: Path) -> dict[str, Any]:
    engine = DexEngine(connector="duckdb", path=str(warehouse), repo_root=str(root))
    result = engine.diagnose()
    assert isinstance(result, DiagnoseResult)
    return to_envelope(result).data


def _one(data: dict[str, Any]) -> dict[str, Any]:
    [diagnosis] = data["diagnoses"]
    return diagnosis


def _kind(diagnosis: dict[str, Any], kind: str) -> list[dict[str, Any]]:
    return [
        c
        for part in ("causes", "ruled_out", "not_checked")
        for c in diagnosis[part]
        if c["kind"] == kind
    ]


def _the_cause(diagnosis: dict[str, Any], kind: str) -> dict[str, Any]:
    assert diagnosis["verdict"] == "cause", diagnosis
    [cause] = diagnosis["causes"]
    assert cause["kind"] == kind, cause
    return cause


def _no_cause(diagnosis: dict[str, Any], kind: str) -> dict[str, Any]:
    assert diagnosis["verdict"] == "no_cause", diagnosis["causes"]
    [held] = _kind(diagnosis, kind)
    assert held["status"] == "ruled_out", held
    return held


def _assert_no_value(data: dict[str, Any]) -> None:
    text = json.dumps(data)
    assert '"placed"' not in text
    assert "shipped" not in text


# --- the loader: a source column retyped ----------------------------------------------


def test_a_source_column_retyped_is_named_as_the_loader_cause(tmp_path: Path):
    root, warehouse = _build(
        tmp_path, Shop(failing=NOT_NULL, amount_type="VARCHAR", snapshot=True)
    )
    diagnosis = _one(_diagnose(root, warehouse))
    cause = _the_cause(diagnosis, "source_retyped")

    assert cause["owner"] == "loader"
    assert cause["relation"] == "dev.raw.orders"
    assert cause["columns"] == ["amount"]
    assert cause["check"] == "maintain check: schema axis"
    assert cause["finding"] == "column_retyped"
    assert cause["evidence"]["type_before"] == "INTEGER"
    assert cause["evidence"]["type_after"] == "VARCHAR"
    assert any(
        "explore profile dev.raw.orders" in s["command"]
        for s in diagnosis["next_steps"]
    )


def test_the_same_source_with_no_retype_reports_no_cause(tmp_path: Path):
    root, warehouse = _build(tmp_path, Shop(failing=NOT_NULL, snapshot=True))
    diagnosis = _one(_diagnose(root, warehouse))
    held = _no_cause(diagnosis, "source_retyped")

    assert held["reason"] == "no_retype"
    assert held["evidence"]["relations"] == ["dev.raw.orders"]


def test_with_no_snapshot_a_retype_is_not_checked_rather_than_ruled_out(
    tmp_path: Path,
):
    root, warehouse = _build(tmp_path, Shop(failing=NOT_NULL, amount_type="VARCHAR"))
    diagnosis = _one(_diagnose(root, warehouse))
    [held] = _kind(diagnosis, "source_retyped")

    assert held["status"] == "not_checked"
    assert held["reason"] == "no_measured_before"


# --- the project: a model change in the range -----------------------------------------


def test_a_model_changed_in_the_range_that_verify_finds_broken_is_the_cause(
    tmp_path: Path,
):
    # The edit drops `amount` from fct_orders, which schema.yml still declares.
    root, warehouse = _build(
        tmp_path,
        Shop(
            edits={"models/fct_orders.sql": "select order_id, status_code from x\n"},
            fct_columns=("order_id", "status_code", "label"),
        ),
    )
    diagnosis = _one(_diagnose(root, warehouse))
    cause = _the_cause(diagnosis, "project_change")

    assert cause["owner"] == "project"
    assert cause["check"] == "maintain verify"
    assert cause["finding"] == "column_missing"
    assert cause["evidence"]["files"] == ["models/fct_orders.sql"]
    assert cause["evidence"]["models"] == ["fct_orders"]
    assert cause["evidence"]["commits"] == 1


def test_a_model_changed_in_the_range_that_verify_finds_sound_is_ruled_out(
    tmp_path: Path,
):
    root, warehouse = _build(
        tmp_path,
        Shop(edits={"models/fct_orders.sql": "select * from stg_orders -- tidy\n"}),
    )
    held = _no_cause(_one(_diagnose(root, warehouse)), "project_change")

    assert held["reason"] == "verify_clean"
    assert held["check"] == "maintain verify"


def test_a_change_outside_the_lineage_is_ruled_out_without_verify(tmp_path: Path):
    root, warehouse = _build(
        tmp_path,
        Shop(
            failing=NOT_NULL,
            edits={"models/fct_orders.sql": "select order_id from x\n"},
            fct_columns=("order_id",),
        ),
    )
    held = _no_cause(_one(_diagnose(root, warehouse)), "project_change")

    assert held["reason"] == "not_in_lineage"
    assert held["evidence"] == {"commits": 1, "changed_files": 1}


# --- a package upgrade ----------------------------------------------------------------


def test_a_package_upgrade_the_failing_test_reads_is_the_cause(tmp_path: Path):
    root, warehouse = _build(
        tmp_path,
        Shop(
            failing=COMBO,
            edits={"package-lock.yml": LOCK_V1.replace("1.1.1", "1.3.0")},
        ),
    )
    diagnosis = _one(_diagnose(root, warehouse))
    cause = _the_cause(diagnosis, "package_upgraded")

    assert cause["owner"] == "package"
    assert cause["evidence"]["packages"] == ["dbt_utils"]
    assert cause["evidence"]["changed"] == [
        {"package": "dbt_utils", "version_before": "1.1.1", "version_after": "1.3.0"}
    ]
    assert len(cause["evidence"]["commits"]) == 1
    # The lock is the package's file, not the project's.
    [project] = _kind(diagnosis, "project_change")
    assert project["reason"] == "not_in_lineage"


def test_the_same_lock_at_both_commits_reports_no_cause(tmp_path: Path):
    root, warehouse = _build(tmp_path, Shop(failing=COMBO))
    held = _no_cause(_one(_diagnose(root, warehouse)), "package_upgraded")

    assert held["reason"] == "same_versions"


def test_with_no_lock_committed_a_package_is_not_checked(tmp_path: Path):
    root, warehouse = _build(
        tmp_path, Shop(failing=COMBO, edits={"package-lock.yml": ""})
    )
    (root / "package-lock.yml").unlink()
    _git(root, "rm", "-q", "--cached", "package-lock.yml")
    _git(root, "commit", "-q", "-m", "drop the lock")
    diagnosis = _one(_diagnose(root, warehouse))
    [held] = _kind(diagnosis, "package_upgraded")

    # c2 holds an empty lock, which pins nothing: not read as the same versions.
    assert held["status"] == "not_checked"
    assert held["reason"] == "unpinned"


# --- a seed edited --------------------------------------------------------------------


def test_a_seed_edited_in_the_range_is_the_cause_with_its_file_hashes(
    tmp_path: Path,
):
    root, warehouse = _build(
        tmp_path,
        Shop(edits={"seeds/status_codes.csv": "status_code,label\n2,shipped\n"}),
    )
    data = _diagnose(root, warehouse)
    diagnosis = _one(data)
    cause = _the_cause(diagnosis, "seed_edited")

    assert cause["owner"] == "seed"
    assert cause["relation"] == "status_codes"
    assert cause["evidence"]["path"] == "seeds/status_codes.csv"
    assert cause["evidence"]["hash_before"] != cause["evidence"]["hash_after"]
    assert len(cause["evidence"]["commits"]) == 1
    # The seed's file is the seed's, never also the project's.
    [project] = _kind(diagnosis, "project_change")
    assert project["status"] == "ruled_out"
    _assert_no_value(data)


def test_the_same_seed_at_both_commits_reports_no_cause(tmp_path: Path):
    root, warehouse = _build(tmp_path, Shop())
    held = _no_cause(_one(_diagnose(root, warehouse)), "seed_edited")

    assert held["reason"] == "same_hash"
    assert held["evidence"]["hash_before"] == held["evidence"]["hash_after"]


# --- the schedule: a parent built after its child -------------------------------------


def test_a_dimension_built_after_its_fact_is_the_schedule_cause(tmp_path: Path):
    root, warehouse = _build(tmp_path, Shop(builds=_split(parent_last=True)))
    data = _diagnose(root, warehouse)
    diagnosis = _one(data)
    cause = _the_cause(diagnosis, "built_before_parent")

    assert cause["owner"] == "schedule"
    assert cause["evidence"]["child"] == "fct_orders"
    assert cause["evidence"]["parent"] == "dim_status"
    failing = cause["evidence"]["failing_run"]
    assert failing["run"] == "inv-3"
    assert failing["parent"]["invocation_id"] == "inv-3-d"
    assert failing["child"]["invocation_id"] == "inv-3-f"
    assert cause["evidence"]["last_passing_run"]["parent"]["invocation_id"] == "inv-2"
    assert data["price"]["checks"]["per_table"]
    _assert_no_value(data)


def test_a_dimension_built_before_its_fact_reports_no_cause(tmp_path: Path):
    root, warehouse = _build(tmp_path, Shop(builds=_split(parent_last=False)))
    held = _no_cause(_one(_diagnose(root, warehouse)), "built_before_parent")

    assert held["reason"] == "parent_first"


def test_with_no_dbt_run_results_the_schedule_is_not_checked(tmp_path: Path):
    root, warehouse = _build(tmp_path, Shop())
    con = duckdb.connect(str(warehouse))
    con.execute(f"DROP TABLE {SCHEMA}.dbt_run_results")
    con.close()
    diagnosis = _one(_diagnose(root, warehouse))
    [held] = _kind(diagnosis, "built_before_parent")

    assert held["status"] == "not_checked"
    assert held["reason"] == "no_run_times"


# --- two candidates in one range, one confirmed ---------------------------------------


def test_two_candidates_in_one_range_name_the_confirmed_one_and_rule_out_the_other(
    tmp_path: Path,
):
    # One range edits the seed the failing test reads and bumps codegen, a
    # package nothing in the lineage reads.
    root, warehouse = _build(
        tmp_path,
        Shop(
            edits={
                "seeds/status_codes.csv": "status_code,label\n2,shipped\n",
                "package-lock.yml": LOCK_V1.replace("0.12.1", "0.13.0"),
            }
        ),
    )
    diagnosis = _one(_diagnose(root, warehouse))
    _the_cause(diagnosis, "seed_edited")
    [package] = _kind(diagnosis, "package_upgraded")

    assert package["status"] == "ruled_out"
    assert package["reason"] == "not_in_lineage"
    assert package["evidence"]["changed"][0]["package"] == "codegen"
