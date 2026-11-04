"""Shared project fixtures and helpers for the mutation command tests."""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

import pytest

from exmergo_dex_core.config import DexConfig
from exmergo_dex_core.engine import DexEngine
from exmergo_dex_core.storage import FilesystemStore

MODEL_SQL = """select
    o.id,
    o.amount,
    c.region,
    case when o.amount > 100 then 'large' else 'small' end as band
from {{ ref('stg_orders') }} o
inner join {{ ref('stg_customers') }} c on o.cid = c.id
where o.status <> 'cancelled'
"""


SUMMARY_SQL = "select region, count(*) as n from {{ ref('fct') }} group by 1"


SUMMARY_TEST = """with s as (select * from {{ ref('fct_summary') }})
select 'unexpected' as problem from s where not (region = 'eu' and n = 2)
union all
select 'missing' from (select count(*) as c from s) t where c <> 1
"""


def _engine(project: Path, connector: str = "duckdb", **kwargs) -> DexEngine:
    return DexEngine(
        connector=connector,
        repo_root=str(project.parent),
        store=FilesystemStore(project.parent),
        config=DexConfig(
            connector=connector, dbt_target="dev", dbt_project_dir=project.name
        ),
        **kwargs,
    )


def _tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if not path.is_file() or any(
            part in {"target", "logs", ".git"} for part in path.parts
        ):
            continue
        digest.update(str(path.relative_to(root)).encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


@pytest.fixture
def measurable_project(tmp_path: Path, duckdb_file: Path) -> Path:
    """A project whose one mart has a suite worth measuring, on real DuckDB."""

    duckdb = pytest.importorskip("duckdb")
    warehouse = tmp_path / "dev.duckdb"
    shutil.copy(duckdb_file, warehouse)
    con = duckdb.connect(str(warehouse))
    con.execute(
        "create or replace table main.raw_orders as select * from (values "
        "(1, 7, 100.0::double, 'placed'), (2, 7, 10.0::double, 'placed'), "
        "(3, 8, 20.0::double, 'cancelled')) t(id, cid, amount, status)"
    )
    con.execute(
        "create or replace table main.raw_customers as select * from (values "
        "(7, 'eu'), (8, 'us')) t(id, region)"
    )
    con.close()

    project = tmp_path / "analytics"
    (project / "models").mkdir(parents=True)
    (project / "dbt_project.yml").write_text(
        'name: p\nversion: "1.0.0"\nprofile: p\nmodel-paths: ["models"]\n',
        encoding="utf-8",
    )
    (project / "profiles.yml").write_text(
        f"p:\n  target: dev\n  outputs:\n    dev:\n      type: duckdb\n"
        f"      path: {warehouse}\n",
        encoding="utf-8",
    )
    (project / "models" / "stg_orders.sql").write_text(
        "select id, cid, amount, status from main.raw_orders", encoding="utf-8"
    )
    (project / "models" / "stg_customers.sql").write_text(
        "select id, region from main.raw_customers", encoding="utf-8"
    )
    (project / "models" / "fct.sql").write_text(MODEL_SQL, encoding="utf-8")
    return project


def _schema_yml(unit_test: bool) -> str:
    base = """version: 2
models:
  - name: fct
    columns:
      - name: id
        data_tests: [not_null]
"""
    if not unit_test:
        return base
    return (
        base
        + """
unit_tests:
  - name: fct_pins_its_rules
    model: fct
    given:
      - input: ref('stg_orders')
        rows:
          - {id: 1, cid: 7, amount: 100.0, status: 'placed'}
          - {id: 2, cid: 7, amount: 10.0, status: 'placed'}
          - {id: 3, cid: 8, amount: 20.0, status: 'cancelled'}
          - {id: 4, cid: 99, amount: 30.0, status: 'placed'}
      - input: ref('stg_customers')
        rows:
          - {id: 7, region: 'eu'}
    expect:
      rows:
        - {id: 1, region: 'eu', band: 'small'}
        - {id: 2, region: 'eu', band: 'small'}
"""
    )


def _with_summary(project: Path) -> Path:
    (project / "models" / "schema.yml").write_text(_schema_yml(False), encoding="utf-8")
    (project / "models" / "fct_summary.sql").write_text(SUMMARY_SQL, encoding="utf-8")
    (project / "tests").mkdir(exist_ok=True)
    (project / "tests" / "assert_summary_counts.sql").write_text(
        SUMMARY_TEST, encoding="utf-8"
    )
    return project


def _with_versioned_summary(project: Path) -> Path:
    """Same mart and count pin as `_with_summary`, declared as dbt model version 1.

    The SQL lives at `fct_summary_v1.sql` and `schema.yml` names it
    `fct_summary` with `versions: [{v: 1}]`. The singular test still refs the
    unversioned name; routing must report the version label `fct_summary.v1`.
    """

    schema = """version: 2
models:
  - name: fct
    columns:
      - name: id
        data_tests: [not_null]
  - name: fct_summary
    latest_version: 1
    versions:
      - v: 1
"""
    (project / "models" / "schema.yml").write_text(schema, encoding="utf-8")
    (project / "models" / "fct_summary_v1.sql").write_text(
        SUMMARY_SQL, encoding="utf-8"
    )
    (project / "tests").mkdir(exist_ok=True)
    (project / "tests" / "assert_summary_counts.sql").write_text(
        SUMMARY_TEST, encoding="utf-8"
    )
    return project
