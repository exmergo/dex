"""Grounding: what a plan depends on, and whether resolution finished.

The verdict is the contract. An empty dependency list means "this plan depends on
nothing" or "resolution did not finish", and a host reading the first when it
should have read the second admits a change nobody checked. Every test here is
about keeping those apart.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import write_manifest, write_semantic_manifest

from exmergo_dex_core import DexEngine
from exmergo_dex_core.edits import EditOp
from exmergo_dex_core.transform.grounding import (
    config_digest,
    ground_plan,
    packages_digest,
    source_digest,
)
from exmergo_dex_core.transform.plans import EditKind, PlanEdit

MART = """select o.order_id, c.region
from {{ ref('stg_orders') }} o
left join {{ ref('stg_customers') }} c on o.customer_id = c.id
"""

DYNAMIC = "select * from {{ ref(var('which_upstream')) }}\n"


def _plan(repo: Path, intent: str, edits: list[PlanEdit]):
    with DexEngine.from_repo(str(repo)) as engine:
        return engine.plan(intent, edits=edits)


def _ground(repo: Path, plan_id: str) -> dict:
    with DexEngine.from_repo(str(repo)) as engine:
        return engine.ground(plan_id).data()


def _model(path: str, content: str) -> PlanEdit:
    return PlanEdit(
        path=path, new_content=content, op=EditOp.UPSERT, kind=EditKind.MODEL_SQL
    )


@pytest.fixture
def project_with_parents(dbt_project_dir: Path) -> Path:
    (dbt_project_dir / "models" / "staging" / "stg_orders.sql").write_text(
        "select 1 as order_id, 1 as customer_id\n", encoding="utf-8"
    )
    return dbt_project_dir.parent


def test_a_fully_resolved_plan_reports_complete_with_no_limits(project_with_parents):
    stored = _plan(
        project_with_parents, "a mart", [_model("models/staging/mart.sql", MART)]
    )
    grounding = _ground(project_with_parents, stored.plan_id)

    assert grounding["completeness"] == "complete"
    assert grounding["limits"] == []
    assert grounding["unresolved"] == []
    names = {(d["kind"], d["name"]) for d in grounding["dependencies"]}
    assert ("model", "stg_orders") in names
    assert ("model", "stg_customers") in names


def test_a_reference_dex_cannot_read_makes_the_answer_partial_and_says_where(
    project_with_parents,
):
    """`{{ ref(var('x')) }}` is a dependency dex knows exists and cannot name.

    Reporting it is the difference between an incomplete answer and a wrong one.
    """

    stored = _plan(
        project_with_parents, "dynamic", [_model("models/staging/dyn.sql", DYNAMIC)]
    )
    grounding = _ground(project_with_parents, stored.plan_id)

    assert grounding["completeness"] == "partial"
    (unresolved,) = grounding["unresolved"]
    assert unresolved["path"] == "models/staging/dyn.sql"
    assert unresolved["form"] == "ref_call"
    assert unresolved["kind"] == "model"
    assert unresolved["line"] == 1
    # The var it reads is still a dependency; only the model it names is not.
    assert ("var", "which_upstream") in {
        (d["kind"], d["name"]) for d in grounding["dependencies"]
    }


def test_a_plan_that_depends_on_nothing_is_complete_not_unresolved(dbt_project_dir):
    repo = dbt_project_dir.parent
    stored = _plan(
        repo, "a constant", [_model("models/staging/k.sql", "select 1 as one\n")]
    )
    grounding = _ground(repo, stored.plan_id)

    assert grounding["completeness"] == "complete"
    assert [d for d in grounding["dependencies"] if d["kind"] == "model"] == []


def test_a_declared_but_uninstalled_package_is_a_named_limit(project_with_parents):
    stored = _plan(
        project_with_parents, "a mart", [_model("models/staging/mart.sql", MART)]
    )
    (project_with_parents / "analytics" / "packages.yml").write_text(
        "packages:\n  - package: dbt-labs/dbt_utils\n    version: 1.3.0\n",
        encoding="utf-8",
    )
    grounding = _ground(project_with_parents, stored.plan_id)

    assert grounding["completeness"] == "partial"
    assert any("not installed" in limit for limit in grounding["limits"])


def test_a_name_a_package_also_defines_is_reported_as_ambiguous(project_with_parents):
    """dbt resolves the project's copy, and the package still ships its own.

    Reporting only the winner is how an override goes unnoticed until somebody
    deletes the local file.
    """

    packages = project_with_parents / "analytics" / "dbt_packages" / "shared"
    (packages / "models").mkdir(parents=True)
    (packages / "dbt_project.yml").write_text(
        'name: shared\nversion: "1.0.0"\nmodel-paths: ["models"]\n', encoding="utf-8"
    )
    (packages / "models" / "stg_orders.sql").write_text(
        "select 2 as order_id\n", encoding="utf-8"
    )

    stored = _plan(
        project_with_parents, "a mart", [_model("models/staging/mart.sql", MART)]
    )
    grounding = _ground(project_with_parents, stored.plan_id)

    ambiguous = {a["name"]: a for a in grounding["ambiguous"]}
    assert "stg_orders" in ambiguous
    assert len(ambiguous["stg_orders"]["candidates"]) == 2
    assert any("shared" in c for c in ambiguous["stg_orders"]["candidates"])


def test_freshness_is_reported_beside_completeness_not_folded_into_it(
    project_with_parents,
):
    """A fully resolved graph read from a stale manifest is both, at once."""

    write_manifest(project_with_parents / "analytics", models={})
    stored = _plan(
        project_with_parents, "a mart", [_model("models/staging/mart.sql", MART)]
    )
    grounding = _ground(project_with_parents, stored.plan_id)

    freshness = grounding["freshness"]
    assert freshness["manifest_present"] is True
    # The completeness verdict is untouched by the freshness fact.
    assert grounding["completeness"] == "complete"


def test_freshness_says_unknown_rather_than_fresh_with_no_manifest(
    project_with_parents,
):
    stored = _plan(
        project_with_parents, "a mart", [_model("models/staging/mart.sql", MART)]
    )
    freshness = _ground(project_with_parents, stored.plan_id)["freshness"]
    assert freshness["manifest_present"] is False
    assert freshness["stale"] is None


def test_a_relation_is_reported_only_where_the_manifest_proves_one(
    project_with_parents,
):
    write_manifest(
        project_with_parents / "analytics",
        models={
            "stg_orders": '"dev"."main"."stg_orders"',
            # An ephemeral model compiles to no relation at all, and inventing
            # one would send a caller looking for a table that does not exist.
            "stg_customers": None,
        },
    )
    stored = _plan(
        project_with_parents, "a mart", [_model("models/staging/mart.sql", MART)]
    )
    grounding = _ground(project_with_parents, stored.plan_id)

    by_name = {d["name"]: d for d in grounding["dependencies"]}
    assert by_name["stg_orders"]["relation"] == "dev.main.stg_orders"
    assert by_name["stg_orders"]["origin"] == "manifest"
    assert "relation" not in by_name["stg_customers"]
    assert grounding["relations"] == ["dev.main.stg_orders"]


def test_the_binding_moves_with_the_source_the_config_and_the_packages(
    project_with_parents,
):
    stored = _plan(
        project_with_parents, "a mart", [_model("models/staging/mart.sql", MART)]
    )
    before = _ground(project_with_parents, stored.plan_id)["binding"]

    (
        project_with_parents / "analytics" / "models" / "staging" / "extra.sql"
    ).write_text("select 1 as id\n", encoding="utf-8")
    (project_with_parents / "analytics" / "packages.yml").write_text(
        "packages:\n  - package: dbt-labs/dbt_utils\n    version: 1.3.0\n",
        encoding="utf-8",
    )
    after = _ground(project_with_parents, stored.plan_id)["binding"]

    assert after["plan_digest"] == before["plan_digest"]
    assert after["source_digest"] != before["source_digest"]
    assert before["packages_digest"] is None
    assert after["packages_digest"] is not None
    assert after["engine_version"] == before["engine_version"]


def test_the_binding_is_the_same_from_two_checkouts_of_one_source_state(
    project_with_parents, tmp_path
):
    import shutil

    stored = _plan(
        project_with_parents, "a mart", [_model("models/staging/mart.sql", MART)]
    )
    here = _ground(project_with_parents, stored.plan_id)["binding"]

    elsewhere = tmp_path / "second"
    shutil.copytree(project_with_parents, elsewhere)
    there = _ground(elsewhere, stored.plan_id)["binding"]

    assert there["source_digest"] == here["source_digest"]
    assert there["plan_digest"] == here["plan_digest"]


def test_a_measure_resolves_through_its_semantic_model_to_its_dbt_model(
    dbt_project_dir,
):
    """The acceptance case: metric to measure to semantic model to relation.

    A name join through the compiled artifacts, not a second resolver.
    """

    from exmergo_dex_core.dbt_project import load, semantic_catalog

    write_semantic_manifest(
        dbt_project_dir,
        semantic_models=[
            {
                "name": "orders",
                "node_relation": {
                    "alias": "fct_orders",
                    "relation_name": '"dev"."main"."fct_orders"',
                },
                "entities": [{"name": "order_id", "type": "primary"}],
                "measures": [{"name": "order_amount", "agg": "sum", "expr": "amount"}],
            }
        ],
        metrics=[
            {
                "name": "revenue",
                "type": "simple",
                "type_params": {"input_measures": [{"name": "order_amount"}]},
            }
        ],
    )
    semantic_yaml = (
        "version: 2\n"
        "metrics:\n"
        "  - name: revenue_per_order\n"
        "    type: derived\n"
        "    label: Revenue per order\n"
        "    type_params:\n"
        "      expr: revenue\n"
        "      metrics:\n"
        "        - revenue\n"
    )
    edits = [
        PlanEdit(
            path="models/staging/derived.yml",
            new_content=semantic_yaml,
            op=EditOp.UPSERT,
            kind=EditKind.SEMANTIC_YML,
        )
    ]
    view = load(dbt_project_dir)
    catalog = semantic_catalog(dbt_project_dir)
    grounding = ground_plan(view, edits, project=dbt_project_dir, catalog=catalog)

    resolved = {(d.kind, d.name): d for d in grounding.dependencies}
    # The plan names `revenue`; the catalog carries it to the measure, the
    # measure to the semantic model, and the semantic model to the relation.
    assert ("metric", "revenue") in resolved
    assert resolved[("metric", "revenue")].resolved_to == "order_amount"
    model = resolved[("semantic_model", "orders")]
    assert model.resolved_to == "fct_orders"
    assert model.relation == "dev.main.fct_orders"
    assert model.origin == "semantic_manifest"


def test_a_semantic_reference_with_no_catalog_is_a_limit_not_a_silence(
    dbt_project_dir,
):
    from exmergo_dex_core.dbt_project import load

    edits = [
        PlanEdit(
            path="models/staging/derived.yml",
            new_content=(
                "version: 2\nmetrics:\n  - name: rpo\n    type: derived\n"
                "    label: R\n    type_params:\n      expr: revenue\n"
                "      metrics:\n        - revenue\n"
            ),
            op=EditOp.UPSERT,
            kind=EditKind.SEMANTIC_YML,
        )
    ]
    grounding = ground_plan(
        load(dbt_project_dir), edits, project=dbt_project_dir, catalog=None
    )
    assert grounding.completeness == "partial"
    assert any("semantic catalog" in limit for limit in grounding.limits)


def test_the_digest_helpers_distinguish_absent_from_empty(dbt_project_dir):
    from exmergo_dex_core.config import DexConfig
    from exmergo_dex_core.dbt_project import load

    assert packages_digest(dbt_project_dir) is None

    (dbt_project_dir / "packages.yml").write_text("packages: []\n", encoding="utf-8")
    empty = packages_digest(dbt_project_dir)
    assert empty is not None

    (dbt_project_dir / "packages.yml").write_text(
        "packages:\n  - package: a/b\n    version: 1.0.0\n", encoding="utf-8"
    )
    assert packages_digest(dbt_project_dir) != empty

    assert config_digest(None) is None
    assert config_digest(DexConfig()) == config_digest(DexConfig())
    assert source_digest(load(dbt_project_dir)).startswith("sha256:")


def test_grounding_opens_no_connection(project_with_parents, monkeypatch):
    """Repo-only and free on every connector, like `transform references`."""

    import exmergo_dex_core.connect as connect

    def refuse(*_args, **_kwargs):
        raise AssertionError("grounding opened a warehouse connection")

    monkeypatch.setattr(connect, "open_adapter", refuse)
    stored = _plan(
        project_with_parents, "a mart", [_model("models/staging/mart.sql", MART)]
    )
    assert _ground(project_with_parents, stored.plan_id)["completeness"] == "complete"


def test_the_plan_edits_are_grounded_not_the_project_as_it_stands(
    project_with_parents,
):
    """A plan's dependencies are the dependencies of the project after it lands.

    The edits are overlaid in memory, exactly as the delete guard does, so a
    reference the plan introduces is grounded before the file exists on disk.
    """

    stored = _plan(
        project_with_parents, "a mart", [_model("models/staging/mart.sql", MART)]
    )
    assert not (
        project_with_parents / "analytics" / "models" / "staging" / "mart.sql"
    ).exists()
    grounding = _ground(project_with_parents, stored.plan_id)
    assert ("model", "stg_orders") in {
        (d["kind"], d["name"]) for d in grounding["dependencies"]
    }
