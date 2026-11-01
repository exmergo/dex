"""Declaration drift (#497): a changed declaration is a change to what a model's
numbers mean, so `maintain` reports it even when the SQL file is untouched."""

from __future__ import annotations

import json

import yaml

from exmergo_dex_core.maintain.drift import (
    declaration_drift,
    declaration_drift_status,
    rank_findings,
)
from exmergo_dex_core.maintain.snapshot import (
    DEX_META_KEY,
    DeclarationFingerprint,
    MetricDef,
    SemanticLayer,
    SemanticModelDef,
    Snapshot,
    TransformLayer,
    declaration_fingerprints,
)

YML_PATH = "models/staging/stg_orders.yml"

DECLARED_YML = """\
version: 2

models:
  - name: stg_orders
    description: One row per order placed in the shop.
    config:
      meta:
        dex:
          grain: [order_id]
          population:
            rule: Every order except test orders.
            filters:
              - column: status
                excludes: [returned]
    columns:
      - name: order_id
        tests: [unique, not_null]
        config:
          meta:
            dex:
              role: key
      - name: customer_id
        tests: [not_null]
      - name: amount
        description: Order amount in US dollars.
        config:
          meta:
            dex:
              role: measure
              additivity: additive
              unit: currency:USD
"""


def _fingerprints(text: str, path: str = YML_PATH) -> dict[str, DeclarationFingerprint]:
    return declaration_fingerprints([(yaml.safe_load(text), path)])


def _snap(declarations, models=("stg_orders",)) -> Snapshot:
    return Snapshot(
        created_at="2026-10-26T00:00:00+00:00",
        transform_layer=TransformLayer(models=list(models), declarations=declarations),
    )


def _layer(declarations, models=("stg_orders",)) -> TransformLayer:
    return TransformLayer(models=list(models), declarations=declarations)


def _drift(before: str, after: str, **kwargs):
    return declaration_drift(
        _layer(_fingerprints(after)), _snap(_fingerprints(before)), **kwargs
    )


# --- the fingerprint ---------------------------------------------------------


def test_every_declared_field_is_fingerprinted_per_model_and_per_column():
    fingerprint = _fingerprints(DECLARED_YML)["stg_orders"]

    assert fingerprint.path == YML_PATH
    assert set(fingerprint.fields) == {"description", "grain", "population"}
    # A column with only tests declares nothing and is absent, not empty.
    assert set(fingerprint.columns) == {"order_id", "amount"}
    assert set(fingerprint.columns["amount"]) == {
        "description",
        "role",
        "additivity",
        "unit",
    }


def test_the_fingerprint_holds_hashes_and_never_the_declared_values():
    raw = json.dumps(
        {k: v.model_dump() for k, v in _fingerprints(DECLARED_YML).items()}
    )
    for value in ("returned", "currency:USD", "test orders", "US dollars"):
        assert value not in raw


def test_formatting_order_and_placement_are_not_a_change():
    restated = """\
version: 2
models:
  - name: stg_orders
    description: >
      One row per order
      placed in the shop.
    meta:
      dex:
        population:
          filters:
            - excludes: [returned]
              column: status
          rule: "Every   order except test orders."
        grain: [order_id]
    columns:
      - name: amount
        meta:
          dex: {unit: "currency:USD", additivity: additive, role: measure}
        description: Order amount in US dollars.
      - name: order_id
        config: {meta: {dex: {role: key}}}
"""
    before = _fingerprints(DECLARED_YML)
    after = _fingerprints(restated, path="models/staging/_models.yml")

    assert before["stg_orders"].fields == after["stg_orders"].fields
    assert before["stg_orders"].columns == after["stg_orders"].columns
    assert _drift(DECLARED_YML, restated) == []


def test_reordering_a_set_valued_field_is_not_a_change():
    two = DECLARED_YML.replace("grain: [order_id]", "grain: [order_id, status]")
    swapped = DECLARED_YML.replace("grain: [order_id]", "grain: [status, order_id]")
    many = DECLARED_YML.replace("excludes: [returned]", "excludes: [returned, placed]")
    shuffled = DECLARED_YML.replace(
        "excludes: [returned]", "excludes: [placed, returned]"
    )

    assert _drift(two, swapped) == []
    assert _drift(many, shuffled) == []


def test_an_empty_field_declares_nothing():
    empty = """\
models:
  - name: stg_orders
    description: ""
    config: {meta: {dex: {grain: []}}}
    columns:
      - name: amount
        config: {meta: {dex: {}}}
"""
    assert _fingerprints(empty) == {}


def test_a_field_dex_does_not_define_is_still_fingerprinted():
    extended = DECLARED_YML.replace(
        "          grain: [order_id]\n",
        "          grain: [order_id]\n          owner: finance\n",
    )
    assert "owner" in _fingerprints(extended)["stg_orders"].fields

    changed = extended.replace("owner: finance", "owner: growth")
    [finding] = _drift(extended, changed)
    assert finding.data["field"] == "owner"
    assert finding.severity == "medium"


def test_the_namespace_matches_the_declaration_writer():
    from exmergo_dex_core.transform.declarations import (
        DEX_META_KEY as WRITER_KEY,
    )

    assert DEX_META_KEY == WRITER_KEY


def test_the_declarations_survive_a_snapshot_round_trip():
    captured = _snap(_fingerprints(DECLARED_YML))
    restored = Snapshot.model_validate_json(captured.model_dump_json())
    assert restored.transform_layer == captured.transform_layer

    empty = Snapshot.model_validate_json(_snap({}).model_dump_json())
    assert empty.transform_layer.declarations == {}

    unrecorded = Snapshot.model_validate_json(_snap(None).model_dump_json())
    assert unrecorded.transform_layer.declarations is None


# --- what is comparable ---------------------------------------------------------


def test_a_baseline_pinned_before_declarations_is_unchecked_not_clean():
    current = _layer(_fingerprints(DECLARED_YML))
    old = _snap(None)

    checked, reason = declaration_drift_status(current, old)
    assert checked is False
    assert "before declarations were fingerprinted" in reason
    # Not every current declaration reported as newly added.
    assert declaration_drift(current, old) == []


def test_a_format_that_does_not_read_declarations_is_unchecked():
    checked, reason = declaration_drift_status(
        _layer(None), _snap(_fingerprints(DECLARED_YML))
    )
    assert checked is False
    assert "does not read model declarations" in reason


def test_an_intentionally_empty_declaration_set_is_checked_and_clean():
    checked, reason = declaration_drift_status(_layer({}), _snap({}))
    assert (checked, reason) == (True, None)
    assert declaration_drift(_layer({}), _snap({})) == []


def test_no_project_on_either_side_is_unchecked():
    snap = Snapshot(created_at="2026-10-26T00:00:00+00:00")
    checked, reason = declaration_drift_status(None, snap)
    assert checked is False
    assert "no transformation project" in reason


# --- the findings ---------------------------------------------------------------


def test_a_population_change_names_the_field_and_ranks_above_a_description_change():
    changed = DECLARED_YML.replace(
        "excludes: [returned]", "excludes: [returned, placed]"
    ).replace("Order amount in US dollars.", "Order amount, in US dollars.")

    findings = rank_findings(_drift(DECLARED_YML, changed))

    assert [(f.code, f.column, f.data["field"], f.severity) for f in findings] == [
        ("declaration_changed", None, "population", "high"),
        ("declaration_changed", "amount", "description", "low"),
    ]
    population = findings[0]
    assert population.axis == "semantic"
    assert population.identifier == "stg_orders"
    assert population.data["change"] == "changed"
    assert population.data["path"] == YML_PATH


def test_one_changed_field_reports_that_field_alone():
    changed = DECLARED_YML.replace("additivity: additive", "additivity: non_additive")

    [finding] = _drift(DECLARED_YML, changed)
    assert finding.code == "declaration_changed"
    assert (finding.column, finding.data["field"]) == ("amount", "additivity")
    assert finding.severity == "high"
    assert "additivity" in finding.detail


def test_a_role_change_is_reported_below_meaning_but_above_words():
    changed = DECLARED_YML.replace("role: measure", "role: attribute")
    [finding] = _drift(DECLARED_YML, changed)
    assert (finding.data["field"], finding.severity) == ("role", "medium")


def test_a_field_set_or_cleared_on_an_existing_declaration():
    added = DECLARED_YML.replace(
        "              unit: currency:USD\n",
        "              unit: currency:USD\n              null_rule: never\n",
    )
    [set_field] = _drift(DECLARED_YML, added)
    assert (set_field.data["field"], set_field.data["change"]) == (
        "null_rule",
        "added",
    )
    assert set_field.severity == "low"

    cleared = DECLARED_YML.replace("              additivity: additive\n", "")
    [cleared_field] = _drift(DECLARED_YML, cleared)
    assert (cleared_field.data["field"], cleared_field.data["change"]) == (
        "additivity",
        "removed",
    )
    # The record is gone, but the rule did not become a different one.
    assert cleared_field.severity == "medium"


def test_a_column_gaining_or_losing_its_whole_declaration():
    gained = DECLARED_YML.replace(
        "      - name: customer_id\n        tests: [not_null]\n",
        "      - name: customer_id\n        tests: [not_null]\n"
        "        config: {meta: {dex: {role: foreign_key}}}\n",
    )
    [added] = _drift(DECLARED_YML, gained)
    assert (added.code, added.column, added.data["fields"]) == (
        "declaration_added",
        "customer_id",
        ["role"],
    )
    assert added.severity == "low"

    [removed] = _drift(gained, DECLARED_YML)
    assert (removed.code, removed.column) == ("declaration_removed", "customer_id")
    assert removed.data["model_present"] is True
    assert removed.severity == "medium"


def test_a_model_losing_its_whole_declaration_is_one_finding():
    bare = "models:\n  - name: stg_orders\n    columns:\n      - name: order_id\n"

    [removed] = _drift(DECLARED_YML, bare)
    assert removed.code == "declaration_removed"
    assert removed.column is None
    assert removed.data["fields"] == ["description", "grain", "population"]
    assert removed.data["columns"] == ["amount", "order_id"]
    assert removed.severity == "medium"

    [added] = _drift(bare, DECLARED_YML)
    assert added.code == "declaration_added"
    assert added.data["columns"] == ["amount", "order_id"]


def test_a_removed_model_still_reports_its_declaration_removed():
    findings = declaration_drift(
        _layer({}, models=()), _snap(_fingerprints(DECLARED_YML))
    )
    [removed] = findings
    assert removed.code == "declaration_removed"
    assert removed.data["model_present"] is False
    assert "the model itself was removed" in removed.detail


def test_a_finding_names_the_metrics_built_on_the_model():
    semantic = SemanticLayer(
        semantic_models=[
            SemanticModelDef(
                name="orders",
                content_sha256="x",
                model_ref="stg_orders",
                entities={"order_id": "order_id"},
                measures={"order_amount": "amount", "order_count": "order_id"},
            )
        ],
        metrics=[
            MetricDef(
                name="revenue", content_sha256="y", input_measures=["order_amount"]
            ),
            MetricDef(
                name="order_volume", content_sha256="z", input_measures=["order_count"]
            ),
        ],
    )
    unit = DECLARED_YML.replace("currency:USD", "currency:EUR")
    population = DECLARED_YML.replace("excludes: [returned]", "excludes: [placed]")

    [column_level] = _drift(DECLARED_YML, unit, semantic=semantic)
    assert column_level.impacted_metrics == ["revenue"]

    [model_level] = _drift(DECLARED_YML, population, semantic=semantic)
    assert model_level.impacted_metrics == ["order_volume", "revenue"]


# --- through the CLI ------------------------------------------------------------


def _declare(repo, text: str = DECLARED_YML) -> None:
    repo.edit(YML_PATH, text)


def _snapshot_json(repo) -> dict:
    return json.loads((repo.root / ".dex" / "snapshot.json").read_text("utf-8"))


def test_snapshot_counts_the_declared_models(maintain_repo):
    payload = maintain_repo.snapshot()
    assert payload["data"]["transform_layer"]["declared_model_count"] == 0

    _declare(maintain_repo)
    payload = maintain_repo.snapshot()
    assert payload["data"]["transform_layer"]["declared_model_count"] == 1
    declarations = _snapshot_json(maintain_repo)["transform_layer"]["declarations"]
    assert set(declarations) == {"stg_orders"}


def test_an_unchanged_declaration_is_checked_and_clean(maintain_repo):
    _declare(maintain_repo)
    maintain_repo.snapshot()

    for command in ("semantic", "check"):
        rc, payload = maintain_repo.dex("maintain", command)
        assert rc == 0 and payload["status"] == "ok", payload
        assert payload["data"]["finding_count"] == 0
        assert payload["data"]["declaration_drift"] == {
            "checked": True,
            "baseline_declared_models": 1,
            "declared_models": 1,
        }


def test_a_changed_declaration_is_drift_with_the_sql_untouched(maintain_repo):
    _declare(maintain_repo)
    maintain_repo.snapshot()
    _declare(
        maintain_repo,
        DECLARED_YML.replace(
            "excludes: [returned]", "excludes: [returned, placed]"
        ).replace("One row per order placed", "One row per order taken"),
    )

    rc, payload = maintain_repo.dex("maintain", "semantic")
    assert rc == 0 and payload["status"] == "ok", payload
    declared = [
        f for f in payload["data"]["findings"] if f["code"].startswith("declaration_")
    ]
    assert [(f["data"]["field"], f["severity"]) for f in declared] == [
        ("population", "high"),
        ("description", "low"),
    ]
    # The SQL did not change, so the schema axis's model_changed has nothing to say.
    rc, payload = maintain_repo.dex("maintain", "check")
    codes = {f["code"] for f in payload["data"]["findings"]}
    assert "declaration_changed" in codes and "model_changed" not in codes
    assert payload["data"]["axes"]["semantic"]["finding_count"] == 2


def test_an_old_baseline_reports_the_axis_unchecked(maintain_repo):
    _declare(maintain_repo)
    maintain_repo.snapshot()
    path = maintain_repo.root / ".dex" / "snapshot.json"
    document = json.loads(path.read_text("utf-8"))
    del document["transform_layer"]["declarations"]
    path.write_text(json.dumps(document), encoding="utf-8")

    for command in ("semantic", "check"):
        rc, payload = maintain_repo.dex("maintain", command)
        assert rc == 0 and payload["status"] == "ok", payload
        status = payload["data"]["declaration_drift"]
        assert status["checked"] is False
        assert "before declarations were fingerprinted" in status["reason"]
        assert not any(
            f["code"].startswith("declaration_") for f in payload["data"]["findings"]
        )
        assert any(
            w.startswith("declaration drift was not checked")
            for w in payload["warnings"]
        )


def test_a_project_only_snapshot_accepts_an_intended_change(maintain_repo):
    _declare(maintain_repo)
    maintain_repo.snapshot()
    _declare(maintain_repo, DECLARED_YML.replace("currency:USD", "currency:EUR"))

    rc, payload = maintain_repo.dex("maintain", "snapshot", "--project-only")
    assert rc == 0 and payload["status"] == "ok", payload
    rc, payload = maintain_repo.dex("maintain", "semantic")
    assert payload["data"]["finding_count"] == 0
    assert payload["data"]["declaration_drift"]["checked"] is True


def test_scoping_by_model_or_column_keeps_its_declaration_findings(maintain_repo):
    _declare(maintain_repo)
    maintain_repo.snapshot()
    _declare(maintain_repo, DECLARED_YML.replace("currency:USD", "currency:EUR"))

    rc, payload = maintain_repo.dex("maintain", "semantic", "amount")
    assert [f["code"] for f in payload["data"]["findings"]] == ["declaration_changed"]
    rc, payload = maintain_repo.dex("maintain", "semantic", "customer_id")
    assert rc == 0 and payload["data"]["finding_count"] == 0


def test_reconcile_advises_and_never_rewrites_a_declaration(maintain_repo):
    _declare(maintain_repo)
    maintain_repo.snapshot()
    changed = DECLARED_YML.replace("additivity: additive", "additivity: semi_additive")
    _declare(maintain_repo, changed)
    maintain_repo.dex("maintain", "semantic")

    rc, payload = maintain_repo.dex("maintain", "reconcile", "semantic")
    assert rc == 0 and payload["status"] == "ok", payload
    [proposal] = payload["data"]["proposals"]
    assert proposal["kind"] == "advisory"
    assert proposal["finding_code"] == "declaration_changed"
    assert "maintain snapshot --project-only" in proposal["action"]
    assert "plan_id" not in payload["data"]
    assert (maintain_repo.root / YML_PATH).read_text("utf-8") == changed


def test_a_changed_temporal_policy_is_high_severity_drift():
    from exmergo_dex_core.maintain.drift import _declaration_weight

    assert _declaration_weight("history") == "high"
    before = DECLARED_YML.replace(
        "          grain: [order_id]\n",
        "          grain: [order_id]\n"
        "          history: {initial_observation: baseline}\n",
    )
    after = before.replace("baseline", "transition")

    def fingerprint(text: str):
        return declaration_fingerprints([(yaml.safe_load(text), YML_PATH)])

    assert fingerprint(before) != fingerprint(after)
