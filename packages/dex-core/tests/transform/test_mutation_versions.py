"""Real dbt mutation tests: one version of a versioned model (#1374).

`fct` is a native dbt versioned model with two versions whose SQL differs, and
`latest_version: 1`, so the version an unversioned `ref('fct')` resolves to is
not the greatest one. Two marts read it: `reads_latest` through `ref('fct')`
and `reads_v2` through `ref('fct', v=2)`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from exmergo_dex_core.transform import mutation

from .mutation_helpers import _engine
from .mutation_helpers import measurable_project as measurable_project

V1_SQL = (
    "select id, amount, status from {{ ref('stg_orders') }} where status <> 'cancelled'"
)
V2_SQL = "select id, amount from {{ ref('stg_orders') }} where amount > 50"

SCHEMA = """version: 2
models:
  - name: fct
    latest_version: 1
    columns:
      - name: id
        data_tests: [not_null]
    versions:
      - v: 1
      - v: 2
  - name: reads_latest
    columns:
      - name: n
        data_tests: [not_null]
  - name: reads_v2
    columns:
      - name: n
        data_tests: [not_null]
"""


@pytest.fixture
def versioned_project(measurable_project: Path) -> Path:
    pytest.importorskip("dbt.adapters.duckdb")
    models = measurable_project / "models"
    (models / "fct.sql").unlink()
    (models / "fct_v1.sql").write_text(V1_SQL, encoding="utf-8")
    (models / "fct_v2.sql").write_text(V2_SQL, encoding="utf-8")
    (models / "reads_latest.sql").write_text(
        "select count(*) as n from {{ ref('fct') }}", encoding="utf-8"
    )
    (models / "reads_v2.sql").write_text(
        "select count(*) as n from {{ ref('fct', v=2) }}", encoding="utf-8"
    )
    (models / "schema.yml").write_text(SCHEMA, encoding="utf-8")
    return measurable_project


def test_a_version_label_mutates_that_version_alone_and_coverage_reads_it(
    versioned_project: Path,
):
    """`fct.v2` plants defects in v2's SQL only, routes them to the mart that
    refs v2, and records under v2's unique id. Coverage then reads v2 `fresh`
    and v1 `missing`, and `--mutate` refreshes v1 by its label rather than
    skipping the versioned model."""

    engine = _engine(versioned_project)
    engine.build(target="dev")

    result = engine.test_mutations("fct.v2")

    assert result.model == "fct.v2"
    # v2 filters on amount and has no status column: no mutant names status.
    assert result.mutants and all("status" not in m["before"] for m in result.mutants)
    assert result.downstream["models"] == ["reads_v2"], result.downstream
    assert set(engine.store.load_mutation_records()) == {"model.p.fct.v2"}

    read = engine.coverage(["fct"])
    strength = {r["name"]: r["strength"]["status"] for r in read.sections["models"]}
    assert strength == {"fct.v1": "missing", "fct.v2": "fresh"}

    refreshed = engine.coverage(["fct"], mutate=True)
    assert refreshed.refresh["skipped"] == []
    assert [e["model"] for e in refreshed.refresh["refreshed"]] == ["fct.v1"]
    assert set(engine.store.load_mutation_records()) == {
        "model.p.fct.v1",
        "model.p.fct.v2",
    }
    strength = {
        r["name"]: r["strength"]["status"] for r in refreshed.sections["models"]
    }
    assert strength == {"fct.v1": "fresh", "fct.v2": "fresh"}


def test_a_bare_name_measures_the_version_an_unversioned_ref_resolves_to(
    versioned_project: Path,
):
    """The bare name is `ref('fct')`'s version, `latest_version: 1`, not the
    greatest; the envelope names it, a warning says how to pick another, and
    the mutants reach the mart that reads it through `ref('fct')`."""

    engine = _engine(versioned_project)
    engine.build(target="dev")

    result = engine.test_mutations("fct")

    assert result.model == "fct.v1"
    assert any("status" in m["before"] for m in result.mutants)
    assert result.downstream["models"] == ["reads_latest"], result.downstream
    assert any("measures fct.v1" in w and "fct.v<N>" in w for w in result.warnings)
    assert set(engine.store.load_mutation_records()) == {"model.p.fct.v1"}


def test_a_name_that_is_no_version_is_refused_for_free(versioned_project: Path):
    engine = _engine(versioned_project)
    for name, match in (
        ("fct.v3", "lists versions 1, 2"),
        ("fct_v2", "name it as 'fct.v2'"),
        ("stg_orders.v1", "declares no versioned model 'stg_orders'"),
    ):
        with pytest.raises(mutation.MutationError, match=match):
            engine.test_mutations(name)
    assert engine.store.load_mutation_records() == {}
