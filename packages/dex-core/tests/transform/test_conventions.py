"""The house-convention warnings `transform plan` reads out of a project's own
models: what makes one fire, and everything that keeps it quiet."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from exmergo_dex_core.cli import main
from exmergo_dex_core.config import ConventionWarnings
from exmergo_dex_core.dbt_project import load as load_project
from exmergo_dex_core.storage import FilesystemStore
from exmergo_dex_core.transform import plans as transform
from exmergo_dex_core.transform.conventions import (
    MIN_PRECEDENT,
    unresolved_key_warnings,
)

RAW = "select order_id, supplier_id, order_status from {{ ref('stg_orders') }}\n"

# The folder the convention is read out of: three dimensions each resolving a
# key of the same shape, plus the parents they resolve against. Anything the
# check needs beyond a bare `dbt_project_dir`.
_MARTS = {
    "dim_products": "select product_id, brand_id, brand_name, price",
    "dim_stores": "select store_id, region_id, region_name",
    "dim_users": "select user_id, country_id, country_name",
    "dim_brands": "select brand_id, brand_name",
    "dim_regions": "select region_id, region_name",
    "dim_countries": "select country_id, country_name",
    "dim_suppliers": "select supplier_id, supplier_name, supplier_company",
}


@pytest.fixture
def marts(dbt_project_dir: Path) -> Path:
    folder = dbt_project_dir / "models" / "marts"
    folder.mkdir(parents=True)
    for name, select in _MARTS.items():
        (folder / f"{name}.sql").write_text(
            f"{select} from {{{{ ref('stg_{name[4:]}') }}}}\n", encoding="utf-8"
        )
    return dbt_project_dir


def _edit(path: str, sql: str, op=transform.EditOp.UPSERT) -> transform.PlanEdit:
    return transform.PlanEdit(
        path=path,
        new_content=sql if op is transform.EditOp.UPSERT else None,
        kind=transform.EditKind.MODEL_SQL,
        op=op,
    )


def _warn(project: Path, *edits: transform.PlanEdit) -> list[str]:
    return unresolved_key_warnings(list(edits), load_project(project), project)


def _dim_orders(sql: str = RAW) -> transform.PlanEdit:
    return _edit("models/marts/dim_orders.sql", sql)


# --- the convention fires -----------------------------------------------------


def test_a_raw_key_in_a_folder_that_resolves_them_warns(marts: Path):
    """The acceptance case: a new dimension exposing `supplier_id` where every
    sibling resolves its keys. The warning has to be arguable, so it names the
    column, the siblings that set the precedent, and the parent that could
    resolve it."""

    warnings = _warn(marts, _dim_orders())

    assert len(warnings) == 1
    warning = warnings[0]
    assert "models/marts/dim_orders.sql" in warning
    assert "supplier_id" in warning
    assert "no resolved counterpart" in warning
    for sibling in ("dim_products", "dim_stores", "dim_users"):
        assert sibling in warning, warning
    assert "dim_suppliers" in warning
    assert "conventions.resolved_keys: false" in warning


def test_a_sibling_authored_in_the_same_plan_counts_as_precedent(marts: Path):
    """Two dimensions authored together are each other's precedent, the way the
    declared-column comparison reads a model and its schema.yml from one plan.
    Deleting the third sibling first is what proves the in-plan one carried it:
    without it the folder is one short of the threshold."""

    (marts / "models" / "marts" / "dim_users.sql").unlink()
    reauthored = _edit(
        "models/marts/dim_users.sql",
        "select user_id, country_id, country_name from {{ ref('stg_users') }}\n",
    )

    assert not _warn(marts, _dim_orders())
    assert _warn(marts, _dim_orders(), reauthored)


def test_a_thin_folder_reads_the_convention_at_the_layer_instead(
    dbt_project_dir: Path,
):
    """A shop with one flat `models/` directory still has a `dim_` layer. The
    folder is preferred where it holds a precedent, and widens where it does
    not, which is the same convention read one level out."""

    folder = dbt_project_dir / "models"
    for name, select in _MARTS.items():
        (folder / f"{name}.sql").write_text(
            f"{select} from {{{{ ref('stg_{name[4:]}') }}}}\n", encoding="utf-8"
        )
    (folder / "marts").mkdir()
    (folder / "marts" / "dim_carts.sql").write_text(
        "select cart_id, region_id, region_name from {{ ref('stg_carts') }}\n",
        encoding="utf-8",
    )

    assert _warn(dbt_project_dir, _edit("models/marts/dim_orders.sql", RAW))


# --- everything that keeps it quiet -------------------------------------------


def test_a_folder_with_no_consistent_convention_says_nothing(marts: Path):
    """One dimension passing a raw key through is a counter-example, and a
    counter-example ends the convention. Unanimity is the whole gate: a folder
    where the house has not made up its mind must not be read as one where it
    has."""

    (marts / "models" / "marts" / "dim_carts.sql").write_text(
        "select cart_id, supplier_id, total from {{ ref('stg_carts') }}\n",
        encoding="utf-8",
    )

    assert not _warn(marts, _dim_orders())


def test_a_fact_table_beside_the_dimensions_is_not_a_counter_example(marts: Path):
    """A fact table carries raw foreign keys legitimately, so it neither sets
    the dimensions' precedent nor breaks it. Siblings are the models sharing
    both the folder and the layer prefix, which is what keeps a mixed marts
    folder readable."""

    (marts / "models" / "marts" / "fct_orders.sql").write_text(
        "select order_id, supplier_id, amount from {{ ref('stg_orders') }}\n",
        encoding="utf-8",
    )

    assert _warn(marts, _dim_orders())


def test_two_resolving_siblings_are_not_several(marts: Path):
    """`MIN_PRECEDENT` siblings, not a majority of however few there are. Two
    authors agreeing is a coincidence a warning should not be built on."""

    (marts / "models" / "marts" / "dim_users.sql").unlink()

    assert MIN_PRECEDENT == 3
    assert not _warn(marts, _dim_orders())


def test_a_model_that_resolves_the_key_says_nothing(marts: Path):
    """Both spellings of a resolved key: the prefixed attribute the siblings
    use, and the bare entity name."""

    resolved = "select order_id, supplier_id, supplier_name from {{ ref('o') }}\n"
    bare = "select order_id, supplier_id, supplier from {{ ref('o') }}\n"

    assert not _warn(marts, _dim_orders(resolved))
    assert not _warn(marts, _dim_orders(bare))


def test_another_key_does_not_resolve_a_key(marts: Path):
    """`supplier_id` beside `supplier_key` resolves nothing: the counterpart
    has to be a descriptive attribute, not a second identifier."""

    assert _warn(
        marts,
        _dim_orders("select order_id, supplier_id, supplier_key from {{ ref('o') }}\n"),
    )


def test_a_model_s_own_key_is_never_a_passed_through_key(marts: Path):
    """`dim_suppliers.supplier_id` is that model's identity. A dimension is
    required to expose its own key and must never be warned for doing so, and
    that holds for a variant named after the same entity (`dim_suppliers_eu`)
    and for an aliased spelling of it (`dim_customers.cust_id`)."""

    for path, sql in (
        ("dim_suppliers", "select supplier_id, region_id, region_name"),
        ("dim_suppliers_eu", "select supplier_id, region_id, region_name"),
        ("dim_customers", "select cust_id, region_id, region_name"),
    ):
        authored = _edit(f"models/marts/{path}.sql", f"{sql} from {{{{ ref('s') }}}}\n")
        assert not _warn(marts, authored), path


def test_a_plural_the_singularizer_mangles_is_still_the_model_s_own_key(
    marts: Path,
):
    """Found by dogfooding, not by the suite. The shared singularizer lands on
    `enterpri` for `enterprises`, which no longer prefixes the `enterprise` the
    model's own key is named for, so `dim_enterprises.enterprise_id` read as a
    foreign key the model had declined to resolve. The same mangling hid
    `dim_enterprises` from the parent lookup, so a genuine `enterprise_id`
    elsewhere had nothing to resolve against."""

    (marts / "models" / "marts" / "dim_enterprises.sql").write_text(
        "select enterprise_id, enterprise_name from {{ ref('stg_ent') }}\n",
        encoding="utf-8",
    )
    own_key = _edit(
        "models/marts/dim_enterprises.sql",
        "select enterprise_id, region_id, region_name from {{ ref('stg_ent') }}\n",
    )
    foreign_key = _dim_orders(
        "select order_id, enterprise_id, status from {{ ref('stg_orders') }}\n"
    )

    assert not _warn(marts, own_key)
    assert any("dim_enterprises" in w for w in _warn(marts, foreign_key))


def test_no_parent_in_the_project_means_no_actionable_fix(marts: Path):
    """The fix is a `ref()`. A key whose parent the project does not hold is a
    warning the caller cannot act on, so it is not raised."""

    assert not _warn(
        marts, _dim_orders("select order_id, gizmo_id, status from {{ ref('o') }}\n")
    )


def test_a_parent_of_nothing_but_keys_cannot_resolve_anything(marts: Path):
    """A bridge table named for the entity has no attribute to resolve to, so
    it is not a parent."""

    (marts / "models" / "marts" / "dim_gizmos.sql").write_text(
        "select gizmo_id, owner_id from {{ ref('stg_gizmos') }}\n", encoding="utf-8"
    )

    assert not _warn(
        marts, _dim_orders("select order_id, gizmo_id, status from {{ ref('o') }}\n")
    )


def test_suffix_shapes_do_not_cross(marts: Path):
    """A house that resolves every `*_id` has said nothing about how it treats
    a `*_key`. Reading one as precedent for the other is the confident guess
    this check exists not to make."""

    assert not _warn(
        marts,
        _dim_orders("select order_id, supplier_key, status from {{ ref('o') }}\n"),
    )


def test_a_select_star_is_silent_rather_than_hedged(marts: Path):
    """Unlike the declared-column comparison, which reports its skip because a
    declaration went unchecked, nothing was promised here, so there is nothing
    to report."""

    assert not _warn(marts, _dim_orders("select * from {{ ref('stg_orders') }}\n"))


def test_a_select_star_sibling_is_neither_precedent_nor_counter_example(marts: Path):
    """A sibling dex cannot read is dropped from the count rather than assumed
    to comply, which is what drops this folder below the threshold."""

    (marts / "models" / "marts" / "dim_users.sql").write_text(
        "select * from {{ ref('stg_users') }}\n", encoding="utf-8"
    )

    assert not _warn(marts, _dim_orders())


def test_a_sibling_this_plan_deletes_is_not_a_precedent(marts: Path):
    """The convention is read against the project as it will stand once the
    plan applies, so a precedent the same plan removes does not prop it up."""

    removed = _edit("models/marts/dim_users.sql", "", op=transform.EditOp.DELETE)

    assert not _warn(marts, _dim_orders(), removed)


def test_a_model_already_in_the_project_is_not_this_plan_s_warning(marts: Path):
    """Only authored models are judged. An existing violation is a real one and
    somebody else's; surfacing it on an unrelated plan would make the check
    noise the first time it ran on a legacy project."""

    (marts / "models" / "marts" / "dim_baskets.sql").write_text(
        "select basket_id, supplier_id, total from {{ ref('stg_baskets') }}\n",
        encoding="utf-8",
    )
    unrelated = _edit(
        "models/marts/dim_shelves.sql",
        "select shelf_id, region_id, region_name from {{ ref('s') }}\n",
    )

    assert not _warn(marts, unrelated)


# --- through the whole command ------------------------------------------------


def test_the_plan_still_validates_and_stores(marts: Path):
    """A style judgment never refuses. The plan exists, the diffs exist, and
    the warning rides in the same envelope."""

    plan, diffs, warnings = transform.plan(
        "add dim_orders",
        [_dim_orders()],
        marts,
        repo_root=marts.parent,
        store=FilesystemStore(marts.parent),
    )

    assert plan.plan_id
    assert diffs
    assert any("supplier_id" in w for w in warnings), warnings


def test_the_project_switches_the_check_off(marts: Path):
    """A house that has a convention and has decided it is not this one turns
    it off in one line, and the plan is otherwise unchanged."""

    off = ConventionWarnings(resolved_keys=False)
    plan, _diffs, warnings = transform.plan(
        "add dim_orders",
        [_dim_orders()],
        marts,
        repo_root=marts.parent,
        store=FilesystemStore(marts.parent),
        conventions=off,
    )

    assert plan.plan_id
    assert not any("resolved counterpart" in w for w in warnings), warnings


def test_the_warning_reaches_the_envelope(marts: Path, capsys):
    """End to end through the CLI, with the config read off disk, since that is
    the only path that proves `engine.config.conventions` is actually wired."""

    repo_root = marts.parent
    payload = repo_root / "edits.json"
    payload.write_text(
        json.dumps(
            {
                "edits": [
                    {
                        "path": "models/marts/dim_orders.sql",
                        "kind": "model_sql",
                        "content": RAW,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    argv = [
        "--repo-root",
        str(repo_root),
        "transform",
        "plan",
        "add dim_orders",
        "--edits-file",
        str(payload),
    ]

    assert main(argv) == 0
    envelope = json.loads(capsys.readouterr().out)
    assert envelope["status"] == "ok"
    assert any("supplier_id" in w for w in envelope["warnings"]), envelope["warnings"]

    dex_dir = repo_root / ".dex"
    dex_dir.mkdir(exist_ok=True)
    (dex_dir / "config.yml").write_text(
        "conventions:\n  resolved_keys: false\n", encoding="utf-8"
    )

    assert main(argv) == 0
    envelope = json.loads(capsys.readouterr().out)
    assert envelope["status"] == "ok"
    assert not any("resolved counterpart" in w for w in envelope["warnings"])
