"""maintain reconcile: findings -> proposals (mechanical vs advisory) -> a plan
applied through `transform apply`, never written by reconcile itself."""

from __future__ import annotations


def _proposals_by_axis(payload: dict) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {}
    for proposal in payload["data"]["proposals"]:
        grouped.setdefault(proposal["axis"], []).append(proposal)
    return grouped


def test_drift_added_column_honors_pii_override():
    """A drift-added column gets a name-based flag at base confidence (no
    aggregates exist yet, so it blocks until the next profile); an override
    clears it with the audit recorded."""

    from exmergo_dex_core.cache import ColumnProfile, Dataset
    from exmergo_dex_core.maintain.drift import DriftFinding
    from exmergo_dex_core.maintain.reconcile import _patched_dataset

    base = Dataset(
        identifier="db.main.orders",
        columns=[ColumnProfile(name="id", data_type="INTEGER")],
    )
    finding = DriftFinding(
        axis="schema",
        code="column_added",
        identifier="db.main.orders",
        column="customer_name",
        detail="column customer_name added",
        data={"data_type": "VARCHAR"},
    )

    plain = _patched_dataset(base, [finding], set())
    added = next(c for c in plain.columns if c.name == "customer_name")
    assert added.pii is not None and added.pii.confidence == 0.6

    cleared = _patched_dataset(base, [finding], {"db.main.orders.customer_name"})
    added = next(c for c in cleared.columns if c.name == "customer_name")
    assert added.pii is None
    assert added.pii_overridden is not None


def test_drift_added_column_honors_pattern_pii_override():
    """A pattern-form override (column_name + scope) reaches drift-added
    columns the same way an exact override does: this is the path
    `maintain/commands.py` feeds through `pii_override_paths()`."""

    from exmergo_dex_core.cache import ColumnProfile, Dataset
    from exmergo_dex_core.config import PIIOverride, pii_override_paths
    from exmergo_dex_core.maintain.drift import DriftFinding
    from exmergo_dex_core.maintain.reconcile import _patched_dataset

    base = Dataset(
        identifier="db.raw_orders_qa",
        columns=[ColumnProfile(name="id", data_type="INTEGER")],
    )
    finding = DriftFinding(
        axis="schema",
        code="column_added",
        identifier="db.raw_orders_qa",
        column="customer_name",
        detail="column customer_name added",
        data={"data_type": "VARCHAR"},
    )
    matcher = pii_override_paths(
        [PIIOverride(column_name="customer_name", scope="db.raw_*")]
    )

    cleared = _patched_dataset(base, [finding], matcher)
    added = next(c for c in cleared.columns if c.name == "customer_name")
    assert added.pii is None
    assert added.pii_overridden is not None


def test_orphan_relation_action_names_the_macro_and_the_one_relation():
    from exmergo_dex_core.maintain.drift import DriftFinding
    from exmergo_dex_core.maintain.reconcile import build
    from exmergo_dex_core.maintain.snapshot import Snapshot, WarehouseBaseline

    finding = DriftFinding(
        axis="schema",
        code="orphan_relation",
        identifier="db.marts.old_fct_orders",
        detail="relation exists with no backing model or source",
        data={"drop_statement": "DROP TABLE db.marts.old_fct_orders;"},
    )
    snap = Snapshot(
        created_at="2026-07-03T10:00:00+00:00",
        connector="duckdb",
        warehouse=WarehouseBaseline(datasets=[]),
        warehouse_from="metadata",
    )

    proposals, edits, warnings = build([finding], snap, None, None)

    assert edits == []
    orphan = next(p for p in proposals if p.finding_code == "orphan_relation")
    assert orphan.kind == "advisory"
    assert "transform macro drop_orphan_relations" in orphan.action
    assert "dbt run-operation drop_orphan_relations" in orphan.action
    assert '"db.marts.old_fct_orders"' in orphan.action
    # A single orphan does not earn the batched-invocation warning.
    assert not any("orphan relations found" in w for w in warnings)


def test_multiple_orphans_also_get_one_batched_invocation_warning():
    from exmergo_dex_core.maintain.drift import DriftFinding
    from exmergo_dex_core.maintain.reconcile import build
    from exmergo_dex_core.maintain.snapshot import Snapshot, WarehouseBaseline

    findings = [
        DriftFinding(
            axis="schema",
            code="orphan_relation",
            identifier=identifier,
            detail="relation exists with no backing model or source",
            data={"drop_statement": f"DROP TABLE {identifier};"},
        )
        for identifier in ("db.marts.old_dim_orders", "db.marts.old_fct_orders")
    ]
    snap = Snapshot(
        created_at="2026-07-03T10:00:00+00:00",
        connector="duckdb",
        warehouse=WarehouseBaseline(datasets=[]),
        warehouse_from="metadata",
    )

    proposals, _edits, warnings = build(findings, snap, None, None)

    assert sum(p.finding_code == "orphan_relation" for p in proposals) == 2
    batched = next(w for w in warnings if "orphan relations found" in w)
    assert "dbt run-operation drop_orphan_relations" in batched
    assert '"db.marts.old_dim_orders"' in batched
    assert '"db.marts.old_fct_orders"' in batched


def test_reconcile_needs_a_drift_report(maintain_repo):
    maintain_repo.snapshot()
    rc, payload = maintain_repo.dex("maintain", "reconcile")
    assert rc == 1 and payload["status"] == "error"
    assert "maintain check" in payload["errors"][0]


def test_no_drift_reconciles_to_nothing(maintain_repo):
    maintain_repo.snapshot()
    maintain_repo.dex("maintain", "check")
    rc, payload = maintain_repo.dex("maintain", "reconcile")
    assert rc == 0 and payload["status"] == "ok"
    assert payload["data"]["proposal_count"] == 0
    assert "no drift" in payload["data"]["hint"]


def test_schema_drift_is_mechanical_and_rescaffolds(maintain_repo):
    # Give reconcile a dex-scaffolded staging model to rebuild.
    rc, payload = maintain_repo.dex(
        "transform", "plan", "--scaffold", "orders", "scaffold stg_orders"
    )
    assert payload["status"] == "ok"
    maintain_repo.dex("transform", "apply", payload["data"]["plan_id"])
    maintain_repo.dex("explore", "map")
    maintain_repo.snapshot()

    maintain_repo.sql(
        "ALTER TABLE orders ADD COLUMN discount DOUBLE",
        "ALTER TABLE orders DROP status",
    )
    maintain_repo.dex("maintain", "schema")

    rc, payload = maintain_repo.dex("maintain", "reconcile", "schema")
    assert rc == 0 and payload["status"] == "ok"
    proposal = _proposals_by_axis(payload)["schema"][0]
    assert proposal["kind"] == "mechanical"
    assert "models/staging/stg_orders.sql" in proposal["paths"]

    # The re-scaffolded model reflects the drift: discount in, status out.
    sql_diff = next(
        d for d in payload["diffs"] if d["path"] == "models/staging/stg_orders.sql"
    )
    assert "discount" in sql_diff["unified"]
    assert any(
        line.startswith("-") and "status" in line
        for line in sql_diff["unified"].splitlines()
    )

    # Reconcile proposes; it does not write. The model on disk is untouched
    # until transform apply runs.
    assert (
        "discount"
        not in (
            maintain_repo.project_dir / "models" / "staging" / "stg_orders.sql"
        ).read_text()
    )

    plan_id = payload["data"]["plan_id"]
    assert f"transform apply {plan_id}" in payload["data"]["hint"]
    rc, applied = maintain_repo.dex("transform", "apply", plan_id)
    assert applied["status"] == "ok"
    assert (
        "discount"
        in (
            maintain_repo.project_dir / "models" / "staging" / "stg_orders.sql"
        ).read_text()
    )


def test_grain_drift_is_advisory_with_a_visibility_test(maintain_repo):
    # A staging model whose key carries no unique test (a common omission):
    # the break would pass builds silently until reconcile proposes the test.
    maintain_repo.edit(
        "models/staging/stg_orders.yml",
        "version: 2\n"
        "models:\n"
        "  - name: stg_orders\n"
        "    columns:\n"
        "      - name: order_id\n"
        "        tests: [not_null]\n",
    )
    maintain_repo.dex("explore", "map")
    maintain_repo.snapshot()

    maintain_repo.sql("INSERT INTO orders SELECT * FROM orders WHERE order_id <= 10")
    maintain_repo.dex("maintain", "grain")

    rc, payload = maintain_repo.dex("maintain", "reconcile", "grain")
    assert rc == 0 and payload["status"] == "ok"
    proposal = next(
        p
        for p in payload["data"]["proposals"]
        if p["finding_code"] == "key_lost_uniqueness"
    )
    assert proposal["kind"] == "advisory"
    assert "decide" in proposal["action"]
    # The advisory is backed by a unique test that makes the break visible in
    # builds, but the dedup decision stays with the human.
    yml_diff = next(
        d for d in payload["diffs"] if d["path"] == "models/staging/stg_orders.yml"
    )
    assert "unique" in yml_diff["unified"]


def test_grain_drift_adds_no_test_when_one_already_alerts(maintain_repo):
    # The fixture's stg_orders.yml already tests order_id for uniqueness, so a
    # broken key already fails builds; reconcile does not add a redundant test.
    maintain_repo.snapshot()
    maintain_repo.sql("INSERT INTO orders SELECT * FROM orders WHERE order_id <= 10")
    maintain_repo.dex("maintain", "grain")

    rc, payload = maintain_repo.dex("maintain", "reconcile", "grain")
    assert rc == 0 and payload["status"] == "ok"
    assert payload["data"]["mechanical_count"] == 0
    assert payload["diffs"] == []


# --- a declared composite grain outranks a column-level unique -----------------
#
# The reported case (#337). A model declares its grain as a combination, one
# member of that combination was unique in the data at profile time and no longer
# is, and reconcile used to propose a column-level `unique` on it. dbt runs that
# test alongside the composite one, so it fails every build from then on and can
# only go green by changing the declared grain; a project format that resolves the
# two the way dbt's semantics imply discards it and the plan applies having
# changed nothing.
#
# Drift is induced on `orders` rather than on `stg_orders`, because the test edit
# lands in the *staging model's* declaration: a finding on `stg_orders` resolves to
# `stg_stg_orders.yml` and never reaches the check under test.

_COMPOSITE_GRAIN = (
    "    tests:\n"
    "      - dbt_utils.unique_combination_of_columns:\n"
    "          combination_of_columns: [order_id, customer_id, ordered_at]\n"
)

_STG_ORDERS_YML = (
    "version: 2\n"
    "models:\n"
    "  - name: stg_orders\n"
    "{grain}"
    "    columns:\n"
    "      - name: order_id\n"
    "        tests: [not_null]\n"
    "      - name: customer_id\n"
    "        tests: [not_null]\n"
    "      - name: ordered_at\n"
)


def _drift_a_member_of_the_grain(repo, *, grain: str) -> dict:
    """Reconcile a lost single-column key against ``grain`` on stg_orders.

    The UPDATE breaks `order_id` alone while leaving every declared combination
    intact, which is the situation the report describes: the grain the project
    declares still holds, and only a column measurement once proved unique does
    not.
    """

    repo.edit("models/staging/stg_orders.yml", _STG_ORDERS_YML.format(grain=grain))
    repo.dex("explore", "map", "--verify")
    repo.snapshot()
    repo.sql("UPDATE orders SET order_id = 1 WHERE order_id BETWEEN 2 AND 5")
    rc, grain_payload = repo.dex("maintain", "grain")
    assert rc == 0 and any(
        f["code"] == "key_lost_uniqueness" and f["column"] == "order_id"
        for f in grain_payload["data"]["findings"]
    ), grain_payload

    rc, payload = repo.dex("maintain", "reconcile", "grain")
    assert rc == 0 and payload["status"] == "ok", payload
    return payload


def test_a_declared_composite_grain_gets_no_column_level_unique(maintain_repo):
    payload = _drift_a_member_of_the_grain(maintain_repo, grain=_COMPOSITE_GRAIN)

    assert payload["diffs"] == []
    assert payload["data"].get("plan_id") is None
    # The warning names the combination, because that is the fact that decides
    # what the operator does next: re-baseline if this is still the grain, or go
    # find what relied on the column alone.
    declined = next(w for w in payload["warnings"] if "declares a composite grain" in w)
    assert "order_id, customer_id, ordered_at" in declined

    proposal = next(
        p
        for p in payload["data"]["proposals"]
        if p["finding_code"] == "key_lost_uniqueness"
    )
    assert proposal["paths"] == []
    # The advisory used to promise a test unconditionally, so the payload said an
    # edit was in the plan while the plan was empty.
    assert "unique test" not in proposal["action"]


def test_without_the_composite_the_same_drift_still_gets_the_unique_edit(
    maintain_repo,
):
    """The positive control, and it is not optional.

    A run that reports "no edit" proves nothing unless the same harness reports
    an edit when one is due. Identical project, identical SQL, identical
    findings; the composite declaration is the only difference, so anything that
    differs below is attributable to it.
    """

    payload = _drift_a_member_of_the_grain(maintain_repo, grain="")

    yml_diff = next(
        d for d in payload["diffs"] if d["path"] == "models/staging/stg_orders.yml"
    )
    assert "unique" in yml_diff["unified"]
    assert not [w for w in payload["warnings"] if "composite grain" in w]

    proposal = next(
        p
        for p in payload["data"]["proposals"]
        if p["finding_code"] == "key_lost_uniqueness"
    )
    assert proposal["paths"] == ["models/staging/stg_orders.yml"]
    assert "the unique test keeps the break visible in builds" in proposal["action"]


def test_a_composite_grain_not_covering_the_drifted_column_still_gets_the_edit(
    maintain_repo,
):
    # The decline is about *this* column being a member, not about the model
    # carrying a composite test at all: a grain over other columns says nothing
    # either way about whether order_id is unique.
    payload = _drift_a_member_of_the_grain(
        maintain_repo,
        grain=(
            "    tests:\n"
            "      - dbt_utils.unique_combination_of_columns:\n"
            "          combination_of_columns: [customer_id, ordered_at, status]\n"
        ),
    )

    assert [d["path"] for d in payload["diffs"]] == ["models/staging/stg_orders.yml"]
    assert not [w for w in payload["warnings"] if "composite grain" in w]


def test_a_declared_grain_that_never_held_is_advisory_and_proposes_no_edit(
    maintain_repo,
):
    # `declared_grain_not_unique` is a different fact from a lost key: nothing
    # lapsed, the project asserts a grain the data does not have. Reconcile has no
    # edit for it either, because widening or narrowing a declared grain is
    # choosing one.
    maintain_repo.edit(
        "models/staging/stg_orders.yml",
        _STG_ORDERS_YML.format(grain=_COMPOSITE_GRAIN),
    )
    maintain_repo.dex("explore", "map", "--verify")
    maintain_repo.snapshot()
    maintain_repo.sql(
        "INSERT INTO stg_orders SELECT * FROM stg_orders WHERE order_id <= 10"
    )
    maintain_repo.dex("maintain", "grain")

    rc, payload = maintain_repo.dex("maintain", "reconcile", "grain")
    assert rc == 0 and payload["status"] == "ok", payload
    proposal = next(
        p
        for p in payload["data"]["proposals"]
        if p["finding_code"] == "declared_grain_not_unique"
    )
    assert proposal["kind"] == "advisory"
    assert "declaration to fix" in proposal["action"]
    assert proposal["paths"] == []
    # Not the fallback text a code with no entry in the action table would get.
    assert "no automatic fix applies" not in proposal["action"]


def test_reconcile_surfaces_what_the_format_could_not_supply(maintain_repo):
    """`ProjectDefinitions.notes` reaches the envelope.

    Reconcile was the one project-reading command that dropped layer notes, and
    the declarations channel was one no command read at all. A stale manifest is
    the shipped format saying its declarations may lag the files, which is
    exactly the caveat that matters when the declarations are what decided
    whether to propose an edit.
    """

    from conftest import write_manifest

    write_manifest(
        maintain_repo.project_dir,
        models={"stg_orders": '"warehouse"."main"."stg_orders"'},
        generated_at="2020-01-01T00:00:00+00:00",
    )
    maintain_repo.snapshot()
    maintain_repo.sql("INSERT INTO orders SELECT * FROM orders WHERE order_id <= 10")
    maintain_repo.dex("maintain", "grain")

    rc, payload = maintain_repo.dex("maintain", "reconcile", "grain")
    assert rc == 0 and payload["status"] == "ok", payload
    assert any("older than the model sources" in w for w in payload["warnings"]), (
        payload["warnings"]
    )


def test_semantic_and_volume_drift_are_advisory_only(maintain_repo):
    maintain_repo.snapshot()
    maintain_repo.sql(
        "DELETE FROM orders WHERE order_id > 20",
        "INSERT INTO stg_orders VALUES (999, 1, 5.0, 'refunded', NULL)",
    )
    maintain_repo.dex("maintain", "check")

    rc, payload = maintain_repo.dex("maintain", "reconcile")
    assert rc == 0 and payload["status"] == "ok"
    assert payload["data"]["mechanical_count"] == 0
    assert payload["data"]["advisory_count"] >= 2
    # Nothing to apply: no plan is minted when every proposal is advisory
    # without a backing test edit.
    assert "plan_id" not in payload["data"]
    assert payload["diffs"] == []

    by_axis = _proposals_by_axis(payload)
    assert all(p["kind"] == "advisory" for p in by_axis["volume"])
    assert all(p["kind"] == "advisory" for p in by_axis["semantic"])


def test_reconcile_conflict_surfaces_at_apply_not_reconcile(maintain_repo):
    _rc, payload = maintain_repo.dex(
        "transform", "plan", "--scaffold", "orders", "scaffold stg_orders"
    )
    maintain_repo.dex("transform", "apply", payload["data"]["plan_id"])
    maintain_repo.dex("explore", "map")
    maintain_repo.snapshot()

    maintain_repo.sql("ALTER TABLE orders ADD COLUMN discount DOUBLE")
    maintain_repo.dex("maintain", "schema")
    _rc, payload = maintain_repo.dex("maintain", "reconcile", "schema")
    plan_id = payload["data"]["plan_id"]

    # A human edits the model after reconcile planned against it.
    maintain_repo.edit(
        "models/staging/stg_orders.sql", "select 1 as id -- hand-tuned\n"
    )
    _rc, apply1 = maintain_repo.dex("transform", "apply", plan_id)
    assert apply1["status"] == "needs_confirmation"
    assert apply1["data"]["conflicts"]
    assert (
        "hand-tuned"
        in (
            maintain_repo.project_dir / "models" / "staging" / "stg_orders.sql"
        ).read_text()
    )

    _rc, apply2 = maintain_repo.dex("transform", "apply", plan_id, "--confirm")
    assert apply2["status"] == "ok"
    assert apply2["data"]["conflicts_overridden"]


def test_reconcile_warns_on_stale_drift_report(maintain_repo):
    maintain_repo.snapshot()
    maintain_repo.sql("ALTER TABLE customers ADD COLUMN phone VARCHAR")
    maintain_repo.dex("maintain", "schema")
    # Re-snapshot after the check: the report now predates the baseline.
    maintain_repo.dex("explore", "map")
    maintain_repo.snapshot()

    _rc, payload = maintain_repo.dex("maintain", "reconcile")
    assert any("older snapshot" in w for w in payload["warnings"])


def test_dropped_source_reconcile_is_advisory_when_no_scaffold(maintain_repo):
    maintain_repo.snapshot()
    maintain_repo.sql("DROP TABLE orders")
    maintain_repo.dex("maintain", "schema")

    rc, payload = maintain_repo.dex("maintain", "reconcile", "schema")
    assert rc == 0 and payload["status"] == "ok"
    # stg_orders exists but was hand-written (not the dex scaffold shape); the
    # table_dropped / dangling_source findings are advisory regardless.
    assert all(p["kind"] == "advisory" for p in payload["data"]["proposals"])
    codes = {p["finding_code"] for p in payload["data"]["proposals"]}
    assert "dangling_source" in codes or "table_dropped" in codes


def test_orphan_relation_reconcile_proposes_the_governed_macro(maintain_repo):
    maintain_repo.snapshot()
    (maintain_repo.project_dir / "models" / "staging" / "stg_orders.sql").unlink()
    maintain_repo.dex("maintain", "schema")

    rc, payload = maintain_repo.dex("maintain", "reconcile", "schema")
    assert rc == 0 and payload["status"] == "ok"
    by_axis = _proposals_by_axis(payload)
    orphan_proposals = [
        p for p in by_axis["schema"] if p["finding_code"] == "orphan_relation"
    ]
    assert orphan_proposals and all(p["kind"] == "advisory" for p in orphan_proposals)
    action = orphan_proposals[0]["action"]
    assert "transform macro drop_orphan_relations" in action
    assert "dbt run-operation drop_orphan_relations" in action
    assert "warehouse.main.stg_orders" in action


def test_the_no_format_fallback_answers_exactly_what_dbt_answers():
    """`_placed(None, ...)` is `DbtProject.edit_path` inlined, including its `None`.

    The fallback exists for callers holding no format and is documented as the
    dbt scaffold convention, so the two have to agree about every kind or the
    convention has two definitions. Declining is the half that is easy to lose:
    a suffix picked by "SQL or otherwise" answers a `models/staging/*.yml` path
    for kinds that have nothing to do with staging models, and a path is not
    `None`, so the caller builds an edit instead of degrading to the advisory it
    produces for a kind nobody can place.
    """

    from exmergo_dex_core.adapters.project import DbtProject
    from exmergo_dex_core.maintain.reconcile import _placed
    from exmergo_dex_core.transform.plans import EditKind

    for kind in EditKind:
        assert _placed(None, kind, "orders") == DbtProject().edit_path(kind, "orders")

    # And the two kinds reconcile proposes still resolve, so the parity above is
    # not both sides having gone silent.
    staged = "models/staging/stg_orders"
    assert _placed(None, EditKind.MODEL_SQL, "orders") == f"{staged}.sql"
    assert _placed(None, EditKind.SCHEMA_YML, "orders") == f"{staged}.yml"
    # `None` is a complete answer, not a gap: reconcile proposes staging models
    # and their schema.yml, and a kind it does not propose gets no path this
    # class would be inventing.
    for unplaced in (
        EditKind.MACRO_SQL,
        EditKind.SNAPSHOT_SQL,
        EditKind.SEED_CSV,
        EditKind.TEST_SQL,
        EditKind.ANALYSIS_SQL,
    ):
        assert _placed(None, unplaced, "orders") is None
