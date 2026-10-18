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

from exmergo_dex_core import DexEngine, MemoryStore, PlanNotFoundError
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


def test_engine_ground_without_plan_raises_a_typed_error():
    with DexEngine(store=MemoryStore()) as engine, pytest.raises(PlanNotFoundError):
        engine.ground()


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
    # No project file declares `revenue`; the compiled catalog does, which is
    # evidence that it exists, so it is resolved and never called missing.
    assert resolved[("metric", "revenue")].defined is None
    assert grounding.missing == []


def test_a_semantic_reference_with_no_catalog_is_a_limit_not_a_silence(
    dbt_project_dir,
):
    """`revenue` exists in the project, and with no catalog dex cannot follow it
    to the models behind it. That is an unfinished answer, not a missing name."""

    from exmergo_dex_core.dbt_project import load

    (dbt_project_dir / "models" / "staging" / "revenue.yml").write_text(
        "version: 2\nmetrics:\n  - name: revenue\n    label: Revenue\n"
        "    type: simple\n    type_params:\n      measure: order_amount\n",
        encoding="utf-8",
    )
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
    assert grounding.missing == []


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


# --- what exists, and where ----------------------------------------------------

MISSING_MART = """select o.order_id
from {{ ref('stg_orders') }} o
join {{ ref('int_orders') }} i on i.order_id = o.order_id
join {{ ref('no_such_model') }} n on n.order_id = o.order_id
join {{ source('raw', 'orders') }} r on r.order_id = o.order_id
join {{ source('raw', 'no_such_table') }} x on x.order_id = o.order_id
"""

SOURCES = """version: 2
sources:
  - name: raw
    tables:
      - name: orders
"""


def _defined(grounding: dict) -> dict[tuple[str, str], str | None]:
    return {
        (d["kind"], d["name"]): d.get("defined")
        for d in grounding["dependencies"]
        if d["kind"] != "column"
    }


def test_a_ref_and_a_source_nothing_defines_are_missing_and_the_answer_complete(
    project_with_parents,
):
    """The issue's reproduction: without a manifest, every relation is `None`.

    So `relation` cannot say whether a name exists, and before this an existing
    model and a missing one came back identical. A name defined nowhere is a
    finding dex finished making, the zero-definition twin of `ambiguous`, so it
    is listed in `missing` and leaves `completeness` alone.
    """

    (
        project_with_parents / "analytics" / "models" / "staging" / "sources.yml"
    ).write_text(SOURCES, encoding="utf-8")
    stored = _plan(
        project_with_parents,
        "a mart",
        [
            _model("models/staging/int_orders.sql", "select 1 as order_id\n"),
            _model("models/staging/mart.sql", MISSING_MART),
        ],
    )
    grounding = _ground(project_with_parents, stored.plan_id)

    assert grounding["completeness"] == "complete"
    assert grounding["limits"] == []
    assert grounding["missing"] == [
        {
            "kind": "model",
            "name": "no_such_model",
            "used_in": ["models/staging/mart.sql"],
        },
        {
            "kind": "source",
            "name": "raw.no_such_table",
            "used_in": ["models/staging/mart.sql"],
        },
    ]
    defined = _defined(grounding)
    assert defined[("model", "stg_orders")] == "project"
    assert defined[("model", "int_orders")] == "plan"
    assert defined[("model", "no_such_model")] == "missing"
    assert defined[("source", "raw.orders")] == "project"
    assert defined[("source", "raw.no_such_table")] == "missing"


def test_a_definition_the_plan_deletes_is_missing(dbt_project_dir):
    """`missing` describes the project after the plan lands, not before it."""

    from exmergo_dex_core.dbt_project import load
    from exmergo_dex_core.edits import content_hash

    view = load(dbt_project_dir)
    customers = "models/staging/stg_customers.sql"
    edits = [
        PlanEdit(
            path=customers,
            new_content=None,
            old_content_hash=content_hash(view.files[customers].content),
            op=EditOp.DELETE,
            kind=EditKind.MODEL_SQL,
        ),
        _model(
            "models/staging/mart.sql", "select id from {{ ref('stg_customers') }}\n"
        ),
    ]
    grounding = ground_plan(view, edits, project=dbt_project_dir)

    assert [(m.kind, m.name) for m in grounding.missing] == [("model", "stg_customers")]


def test_defined_prefers_the_plan_then_the_project_then_a_package(
    project_with_parents,
):
    """The order dbt resolves in: the root project before any package.

    A model the plan rewrites is `plan` even though it existed before, because
    its definition after the plan lands is the plan's. A model only a package
    ships is `package`, and `package` names which one.
    """

    shared = project_with_parents / "analytics" / "dbt_packages" / "shared"
    (shared / "models").mkdir(parents=True)
    (shared / "dbt_project.yml").write_text(
        'name: shared\nversion: "1.0.0"\nmodel-paths: ["models"]\n', encoding="utf-8"
    )
    for name in ("stg_orders", "pkg_only"):
        (shared / "models" / f"{name}.sql").write_text(
            "select 2 as order_id\n", encoding="utf-8"
        )

    stored = _plan(
        project_with_parents,
        "a mart",
        [
            _model("models/staging/stg_customers.sql", "select 2 as id\n"),
            _model(
                "models/staging/mart.sql",
                "select 1 as one\n"
                "from {{ ref('stg_customers') }}\n"
                "join {{ ref('stg_orders') }} on true\n"
                "join {{ ref('pkg_only') }} on true\n",
            ),
        ],
    )
    grounding = _ground(project_with_parents, stored.plan_id)

    by_name = {d["name"]: d for d in grounding["dependencies"] if d["kind"] == "model"}
    assert by_name["stg_customers"]["defined"] == "plan"
    # Shadowed by a package and still the project's: dbt resolves this copy.
    assert by_name["stg_orders"]["defined"] == "project"
    assert "package" not in by_name["stg_orders"]
    assert [a["name"] for a in grounding["ambiguous"]] == ["stg_orders"]
    assert by_name["pkg_only"]["defined"] == "package"
    assert by_name["pkg_only"]["package"] == "shared"
    assert grounding["missing"] == []


def test_a_package_seed_reached_by_ref_is_reported_as_a_seed(project_with_parents):
    """`ref()` reaches seeds and snapshots too, and the index kinds a ref by this
    project's own nodes. Looking only for a model would call a package's seed
    missing; the dependency takes the kind of what dbt will actually find.
    """

    shared = project_with_parents / "analytics" / "dbt_packages" / "shared"
    (shared / "seeds").mkdir(parents=True)
    (shared / "dbt_project.yml").write_text(
        'name: shared\nversion: "1.0.0"\n', encoding="utf-8"
    )
    (shared / "seeds" / "country_codes.csv").write_text(
        "code,name\nIT,Italy\n", encoding="utf-8"
    )
    stored = _plan(
        project_with_parents,
        "codes",
        [
            _model(
                "models/staging/codes.sql", "select * from {{ ref('country_codes') }}\n"
            )
        ],
    )
    grounding = _ground(project_with_parents, stored.plan_id)

    (codes,) = [d for d in grounding["dependencies"] if d["name"] == "country_codes"]
    assert codes["kind"] == "seed"
    assert codes["defined"] == "package"
    assert codes["package"] == "shared"
    assert grounding["missing"] == []


def test_names_dbt_resolves_without_a_sql_file_are_never_called_missing(
    dbt_project_dir,
):
    """A Python model, a versioned model and a YAML snapshot have no `.sql` file
    named after them, and dbt resolves every one. Calling them missing would be
    exactly the wrong answer `missing` exists to prevent.
    """

    from exmergo_dex_core.dbt_project import load

    models = dbt_project_dir / "models"
    (models / "py_orders.py").write_text(
        "def model(dbt, session):\n    return dbt.ref('stg_customers')\n",
        encoding="utf-8",
    )
    (models / "dim_orders_v1.sql").write_text("select 1 as id\n", encoding="utf-8")
    (models / "dim_customers.sql").write_text("select 1 as id\n", encoding="utf-8")
    (models / "versions.yml").write_text(
        "version: 2\n"
        "models:\n"
        "  - name: dim_orders\n"
        "    latest_version: 1\n"
        "    versions:\n"
        "      - v: 1\n"
        "  - name: dim_customers\n"
        "    latest_version: 2\n"
        "    versions:\n"
        "      - v: 2\n"
        "        defined_in: dim_customers\n",
        encoding="utf-8",
    )
    (dbt_project_dir / "snapshots").mkdir()
    (dbt_project_dir / "snapshots" / "snapshots.yml").write_text(
        "snapshots:\n"
        "  - name: orders_snapshot\n"
        "    relation: source('raw', 'orders')\n"
        "    config:\n"
        "      unique_key: id\n"
        "      strategy: check\n"
        "      check_cols: all\n",
        encoding="utf-8",
    )
    sql = (
        "select 1 as id from {{ ref('py_orders') }}\nunion all\n"
        "select 1 as id from {{ ref('dim_orders') }}\nunion all\n"
        "select 1 as id from {{ ref('dim_customers') }}\nunion all\n"
        "select 1 as id from {{ ref('orders_snapshot') }}\n"
    )
    grounding = ground_plan(
        load(dbt_project_dir),
        [_model("models/staging/everything.sql", sql)],
        project=dbt_project_dir,
    )

    defined = {(d.kind, d.name): d.defined for d in grounding.dependencies}
    assert defined[("model", "py_orders")] == "project"
    assert defined[("model", "dim_orders")] == "project"
    assert defined[("model", "dim_customers")] == "project"
    assert defined[("snapshot", "orders_snapshot")] == "project"
    assert grounding.missing == []
    # The base-name declaration and the `defined_in` file are one model to dbt.
    assert grounding.ambiguous == []


def test_a_properties_only_snapshot_entry_defines_nothing(dbt_project_dir):
    """Without `relation` a `snapshots:` entry documents a snapshot somewhere else.
    Counting it as a declaration would make any documented name exist."""

    from exmergo_dex_core.dbt_project import load

    (dbt_project_dir / "models" / "staging" / "snapshots.yml").write_text(
        "version: 2\nsnapshots:\n  - name: ghost\n    description: documented only\n",
        encoding="utf-8",
    )
    grounding = ground_plan(
        load(dbt_project_dir),
        [_model("models/staging/g.sql", "select * from {{ ref('ghost') }}\n")],
        project=dbt_project_dir,
    )

    assert [(m.kind, m.name) for m in grounding.missing] == [("model", "ghost")]


def test_a_two_argument_ref_is_never_called_missing(dbt_project_dir):
    """`ref('core', 'x')` can name another dbt project, which dex does not read."""

    from exmergo_dex_core.dbt_project import load

    grounding = ground_plan(
        load(dbt_project_dir),
        [
            _model(
                "models/staging/far.sql",
                "select * from {{ ref('core_platform', 'dim_accounts') }}\n",
            )
        ],
        project=dbt_project_dir,
    )

    (far,) = [d for d in grounding.dependencies if d.name == "dim_accounts"]
    assert far.defined is None
    assert grounding.missing == []
    assert grounding.completeness == "complete"


@pytest.mark.parametrize(
    "installed",
    [(), ("dbt_utils",)],
    ids=["nothing-installed", "one-of-two-installed"],
)
def test_a_name_is_not_called_missing_while_a_declared_package_is_unread(
    dbt_project_dir, installed
):
    """The package nobody installed may be exactly where the name is defined.

    So the name is left out of `missing`, a limit says why, and the answer is
    `partial`. `missing` is conclusive or absent, never a guess.
    """

    from exmergo_dex_core.dbt_project import load

    (dbt_project_dir / "packages.yml").write_text(
        "packages:\n"
        "  - package: dbt-labs/dbt_utils\n    version: 1.3.0\n"
        "  - package: dbt-labs/codegen\n    version: 0.13.1\n",
        encoding="utf-8",
    )
    for name in installed:
        package = dbt_project_dir / "dbt_packages" / name
        package.mkdir(parents=True)
        (package / "dbt_project.yml").write_text(
            f'name: {name}\nversion: "1.0.0"\n', encoding="utf-8"
        )
    grounding = ground_plan(
        load(dbt_project_dir),
        [_model("models/staging/m.sql", "select * from {{ ref('no_such_model') }}\n")],
        project=dbt_project_dir,
    )

    (dependency,) = [d for d in grounding.dependencies if d.kind == "model"]
    assert dependency.defined is None
    assert grounding.missing == []
    assert grounding.completeness == "partial"
    (withheld,) = [limit for limit in grounding.limits if "no_such_model" in limit]
    assert "codegen" in withheld


def test_a_name_is_not_called_missing_when_the_plan_changes_the_packages(
    dbt_project_dir,
):
    """A plan that adds a package and reads a model from it is the ordinary way to
    adopt a source package. Nothing has installed it yet, so the model it reads
    cannot be found, and that is not evidence that it does not exist."""

    from exmergo_dex_core.dbt_project import load

    grounding = ground_plan(
        load(dbt_project_dir),
        [
            PlanEdit(
                path="packages.yml",
                new_content=(
                    "packages:\n  - package: fivetran/hubspot_source\n"
                    "    version: 0.15.0\n"
                ),
                op=EditOp.UPSERT,
                kind=EditKind.PACKAGES_YML,
            ),
            _model(
                "models/staging/contacts.sql",
                "select * from {{ ref('stg_hubspot__contact') }}\n",
            ),
        ],
        project=dbt_project_dir,
    )

    assert grounding.missing == []
    assert grounding.completeness == "partial"
    assert any("packages.yml" in limit for limit in grounding.limits)


def test_a_yaml_file_that_does_not_parse_withholds_missing_only_when_it_matters(
    dbt_project_dir,
):
    """A broken YAML file may hold the definition, so it blocks a `missing`
    claim. It is no reason to doubt a plan whose every name was found."""

    from exmergo_dex_core.dbt_project import load

    (dbt_project_dir / "models" / "staging" / "broken.yml").write_text(
        "sources: [unclosed\n", encoding="utf-8"
    )
    view = load(dbt_project_dir)

    unsure = ground_plan(
        view,
        [
            _model(
                "models/staging/m.sql", "select * from {{ source('raw', 'orders') }}\n"
            )
        ],
        project=dbt_project_dir,
    )
    assert unsure.missing == []
    assert unsure.completeness == "partial"
    assert any("models/staging/broken.yml" in limit for limit in unsure.limits)

    found = ground_plan(
        view,
        [_model("models/staging/m.sql", "select id from {{ ref('stg_customers') }}\n")],
        project=dbt_project_dir,
    )
    assert found.completeness == "complete"
    assert found.limits == []


# --- semantic definitions ---------------------------------------------------------

SEMANTIC_MODEL = """version: 2
semantic_models:
  - name: customers
    model: ref('stg_customers')
    entities:
      - name: customer
        type: primary
        expr: id
    dimensions:
      - name: email
        type: categorical
    measures:
      - name: customer_count
        agg: count
        expr: id
metrics:
  - name: customer_total
    label: Customers
    type: simple
    type_params:
      measure: customer_count
"""


def _write_unrelated_catalog(project: Path) -> None:
    """A compiled catalog that carries none of the names a test reads."""

    write_semantic_manifest(
        project,
        semantic_models=[
            {
                "name": "accounts",
                "node_relation": {"alias": "accounts", "relation_name": "accounts"},
                "measures": [{"name": "account_count", "agg": "count"}],
            }
        ],
        metrics=[],
    )


def test_a_semantic_model_the_plan_adds_is_defined_by_the_plan(dbt_project_dir):
    """A semantic model's own entities, dimensions and measures are declarations
    in the document under edit. They are the plan's, and they need no compiled
    catalog to exist, so a fresh checkout with no semantic manifest is complete.
    """

    from exmergo_dex_core.dbt_project import load

    grounding = ground_plan(
        load(dbt_project_dir),
        [
            PlanEdit(
                path="models/staging/customers_semantic.yml",
                new_content=SEMANTIC_MODEL,
                op=EditOp.UPSERT,
                kind=EditKind.SEMANTIC_YML,
            )
        ],
        project=dbt_project_dir,
        catalog=None,
    )

    assert grounding.completeness == "complete"
    assert grounding.limits == []
    defined = {(d.kind, d.name): d.defined for d in grounding.dependencies}
    assert defined[("entity", "customer")] == "plan"
    assert defined[("dimension", "email")] == "plan"
    assert defined[("measure", "customer_count")] == "plan"
    assert defined[("model", "stg_customers")] == "project"


@pytest.mark.parametrize("with_catalog", [False, True], ids=["no-catalog", "catalog"])
def test_a_metric_input_nothing_defines_is_missing_not_a_limit(
    dbt_project_dir, with_catalog
):
    """A missing measure used to be a limit and a missing model nothing at all.
    Both are now the same finding, whether or not a catalog was compiled."""

    from exmergo_dex_core.dbt_project import load, semantic_catalog

    if with_catalog:
        _write_unrelated_catalog(dbt_project_dir)
    grounding = ground_plan(
        load(dbt_project_dir),
        [
            PlanEdit(
                path="models/staging/m.yml",
                new_content=(
                    "version: 2\nmetrics:\n  - name: m\n    label: M\n"
                    "    type: simple\n    type_params:\n"
                    "      measure: no_such_measure\n"
                ),
                op=EditOp.UPSERT,
                kind=EditKind.SEMANTIC_YML,
            )
        ],
        project=dbt_project_dir,
        catalog=semantic_catalog(dbt_project_dir) if with_catalog else None,
    )

    assert [(m.kind, m.name) for m in grounding.missing] == [
        ("measure", "no_such_measure")
    ]
    assert grounding.limits == []
    assert grounding.completeness == "complete"


def test_a_metric_the_source_defines_and_the_catalog_lacks_stays_a_limit(
    dbt_project_dir,
):
    """The name exists, so it is not missing, but the catalog compiled before it
    cannot carry it to its models. That is an unfinished answer, so `partial`."""

    from exmergo_dex_core.dbt_project import load, semantic_catalog

    (dbt_project_dir / "models" / "staging" / "revenue.yml").write_text(
        "version: 2\nmetrics:\n  - name: revenue\n    label: Revenue\n"
        "    type: simple\n    type_params:\n      measure: order_amount\n",
        encoding="utf-8",
    )
    _write_unrelated_catalog(dbt_project_dir)
    grounding = ground_plan(
        load(dbt_project_dir),
        [
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
        ],
        project=dbt_project_dir,
        catalog=semantic_catalog(dbt_project_dir),
    )

    (revenue,) = [d for d in grounding.dependencies if d.name == "revenue"]
    assert revenue.defined == "project"
    assert grounding.missing == []
    assert grounding.completeness == "partial"
    assert any("metric 'revenue'" in limit for limit in grounding.limits)


def test_a_metric_and_its_semantic_model_are_each_one_dependency(dbt_project_dir):
    """Reaching one semantic model through three names is still one model.

    Before, each metric or measure that reached it appended another entry, and a
    metric the plan read appeared twice: once as read, once as resolved.
    """

    from exmergo_dex_core.dbt_project import load, semantic_catalog
    from exmergo_dex_core.transform.grounding import ORIGINS

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
                "measures": [
                    {"name": "order_amount", "agg": "sum", "expr": "amount"},
                    {"name": "order_count", "agg": "count", "expr": "order_id"},
                ],
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
    grounding = ground_plan(
        load(dbt_project_dir),
        [
            PlanEdit(
                path="models/staging/derived.yml",
                new_content=(
                    "version: 2\nmetrics:\n"
                    "  - name: a\n    label: A\n    type: derived\n"
                    "    type_params:\n      expr: revenue\n      metrics:\n"
                    "        - revenue\n"
                    "  - name: b\n    label: B\n    type: simple\n"
                    "    type_params:\n      measure: order_amount\n"
                    "  - name: c\n    label: C\n    type: simple\n"
                    "    type_params:\n      measure: order_count\n"
                ),
                op=EditOp.UPSERT,
                kind=EditKind.SEMANTIC_YML,
            )
        ],
        project=dbt_project_dir,
        catalog=semantic_catalog(dbt_project_dir),
    )

    keys = [(d.kind, d.name) for d in grounding.dependencies]
    assert len(keys) == len(set(keys))
    (orders,) = [d for d in grounding.dependencies if d.kind == "semantic_model"]
    assert orders.used_in == [
        "metric:revenue",
        "measure:order_amount",
        "measure:order_count",
    ]
    (revenue,) = [d for d in grounding.dependencies if d.name == "revenue"]
    assert revenue.used_in == ["models/staging/derived.yml"]
    assert revenue.origin == "semantic_manifest"
    assert {d.origin for d in grounding.dependencies} <= set(ORIGINS)
    assert "cache" not in ORIGINS
