"""An id repair at staging that collapses rows in an unedited dimension (#1231).

The fixture the issue specifies, after the analytics_engineering007 run on
1.17.5: five products were loaded twice, once under their integer id and once
under an md5 hash of it. The repair decodes the hashed ids in ``stg_products``
back to integers through a mapping, which is right, and the unedited
``dim_products``, which groups by the id, then holds 40 rows where it held 45.
Nothing the plan edited says so, and on 1.17.5 nothing reported it.

What must hold: the build measures ``dim_products`` against the tree before the
plan and leads ``data.completion.impact`` with it (five keys removed, caused by
``stg_products``); a high finding names the upstream edit; the outcome is not
``validated``; an unedited descendant whose rows did not move reads
``unchanged``; and no key value reaches the envelope. A declaration recording
the new population at ``request`` tier settles it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from tests.transform.test_transform_compare import _engine

from exmergo_dex_core.transform.plans import EditKind, PlanEdit

pytestmark = pytest.mark.live_open_adapter

_RAW = """
select cast(i as varchar) as product_id,
    'product ' || cast(i as varchar) as product_name
from range(1, 41) as t(i)
union all
select md5(cast(i as varchar)) as product_id, 'product ' || cast(i as varchar)
from range(1, 6) as t(i)
"""

_MAP = """
select md5(cast(i as varchar)) as hashed_id, i as product_id
from range(1, 41) as t(i)
"""

_STAGING = """
select product_id, product_name
from {{ ref('raw_products') }}
"""

#: The repair: hashed ids are decoded back to their integer id.
_REPAIRED = """
select
    coalesce(m.product_id, try_cast(p.product_id as integer)) as product_id,
    p.product_name
from {{ ref('raw_products') }} p
left join {{ ref('raw_product_id_map') }} m on m.hashed_id = p.product_id
"""

_DIM = """
select product_id, max(product_name) as product_name
from {{ ref('stg_products') }}
group by product_id
"""

#: An unedited descendant whose rows the repair does not move.
_NAMES = """
select distinct product_name
from {{ ref('stg_products') }}
"""

_SETTLED = """
version: 2
models:
  - name: dim_products
    config:
      meta:
        dex:
          assumptions:
            - decision: Which products the dimension holds
              chosen: one row per repaired integer product id
              evidence: request
              column: product_id
"""


@pytest.fixture
def project(tmp_path: Path) -> Path:
    pytest.importorskip("dbt.adapters.duckdb")
    root = tmp_path / "analytics"
    models = root / "models"
    (models / "staging").mkdir(parents=True)
    (models / "marts").mkdir(parents=True)
    (root / "dbt_project.yml").write_text(
        'name: p\nversion: "1.0.0"\nprofile: p\nmodel-paths: ["models"]\n',
        encoding="utf-8",
    )
    (root / "profiles.yml").write_text(
        "p:\n  target: dev\n  outputs:\n    dev:\n      type: duckdb\n"
        f"      path: {tmp_path / 'dev.duckdb'}\n",
        encoding="utf-8",
    )
    (models / "staging" / "raw_products.sql").write_text(_RAW, encoding="utf-8")
    (models / "staging" / "raw_product_id_map.sql").write_text(_MAP, encoding="utf-8")
    (models / "staging" / "stg_products.sql").write_text(_STAGING, encoding="utf-8")
    (models / "marts" / "dim_products.sql").write_text(_DIM, encoding="utf-8")
    (models / "marts" / "product_names.sql").write_text(_NAMES, encoding="utf-8")
    return root


def _repair(project: Path) -> str:
    engine = _engine(project)
    engine.build(target="dev", verify=True)
    engine.close()
    planned = _engine(project).plan(
        "decode the md5-hashed product ids back to integers",
        edits=[
            PlanEdit(
                path="models/staging/stg_products.sql",
                new_content=_REPAIRED,
                kind=EditKind.MODEL_SQL,
            )
        ],
    )
    # The plan names, for free, what it can move without editing (#1231).
    downstream = planned.change_scope["downstream"]
    assert downstream["models"]["stg_products"]["models"] == [
        "dim_products",
        "product_names",
    ]
    assert "--for-plan" in downstream["measured_by"]
    # The staging model's own population is not what this fixture is about.
    applied = _engine(project).apply(
        planned.plan_id, accept_population_change="id repair under test"
    )
    assert applied.written
    return planned.plan_id


def _build(project: Path, plan_id: str) -> dict:
    engine = _engine(project)
    try:
        return engine.build(target="dev", verify=True, for_plan=plan_id).data()
    finally:
        engine.close()


def test_an_id_repair_that_collapses_an_unedited_dimension_is_reported(project):
    plan_id = _repair(project)
    data = _build(project, plan_id)

    impact = data["verification"]["impact"]
    assert impact["ran"] is True, impact
    assert impact["baseline"] == "apply"
    by_model = {record["model"]: record for record in impact["models"]}

    dim = by_model["dim_products"]
    assert dim["role"] == "descendant"
    assert dim["layer"] == "mart"
    assert dim["upstream"] == ["stg_products"]
    assert dim["key"] == ["product_id"]
    assert dim["key_source"] == "drafted"
    assert dim["rows"] == {"baseline": 45, "proposed": 40, "delta": -5}
    assert dim["keys"]["removed"] == 5
    assert dim["keys"]["added"] == 0
    assert dim["keys"]["kept"] == 40
    assert dim["finding"] == "downstream_population_changed"
    # The largest change leads.
    assert impact["models"][0]["model"] == "dim_products"

    names = by_model["product_names"]
    assert names["change"] == "unchanged"
    assert "finding" not in names

    edited = by_model["stg_products"]
    assert edited["role"] == "edited"
    assert edited["rows"]["baseline"] == edited["rows"]["proposed"] == 45

    finding = next(
        f
        for f in data["verification"]["findings"]
        if f["code"] == "downstream_population_changed"
    )
    assert finding["identifier"] == "dim_products"
    assert finding["severity"] == "high"
    assert "caused by stg_products" in finding["detail"]
    assert finding["attribution"] == "introduced"
    assert finding["attribution_basis"] == "measured"

    completion = data["completion"]
    assert completion["impact"]["top"][0]["model"] == "dim_products"
    assert completion["impact"]["top"][0]["rows"] == "45 -> 40"
    assert completion["impact"]["top"][0]["keys_removed"] == 5
    assert data["outcome"] == "built_unverified"
    reasons = {(u["model"], u["reason"]) for u in completion["unverified"]}
    assert ("dim_products", "downstream_population_changed") in reasons

    # Counts and names only: no product id, hashed or decoded, is in the payload.
    dumped = json.dumps(data)
    assert "c4ca4238a0b923820dcc509a6f75849b" not in dumped  # md5('1')
    assert "product 1" not in dumped


def test_a_population_the_declaration_records_is_settled(project):
    (project / "models" / "marts" / "schema.yml").write_text(_SETTLED, encoding="utf-8")
    plan_id = _repair(project)
    data = _build(project, plan_id)

    dim = next(
        r
        for r in data["verification"]["impact"]["models"]
        if r["model"] == "dim_products"
    )
    assert dim["population"] == "moved"
    assert dim["settled_by"] == {"column": "product_id", "evidence": "request"}
    assert "finding" not in dim
    assert not any(
        f["code"] == "downstream_population_changed"
        for f in data["verification"]["findings"]
    )
    reasons = {u["reason"] for u in data["completion"].get("unverified") or []}
    assert "downstream_population_changed" not in reasons
    assert data["completion"]["impact"]["top"][0]["settled"] is True
