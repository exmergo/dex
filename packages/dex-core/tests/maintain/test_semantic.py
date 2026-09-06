"""maintain semantic: impact analysis over definitions, references, and
dimension cardinality. No dimension value ever reaches the envelope."""

from __future__ import annotations

import json
from types import SimpleNamespace

from exmergo_dex_core.cache import ColumnProfile, Dataset
from exmergo_dex_core.maintain.drift import (
    CardinalityCheck,
    cardinality_drift,
    cardinality_plan,
    semantic_free_drift,
)
from exmergo_dex_core.maintain.snapshot import (
    SemanticLayer,
    SemanticModelDef,
    Snapshot,
    WarehouseBaseline,
)

from .conftest import SEMANTIC_YAML

SEMANTIC_PATH = "models/marts/orders_semantic.yml"


class _StubAdapter:
    """Just the surface cardinality_drift touches: the object exists, its column
    is live, and an exact distinct count is returned for it."""

    dialect = "duckdb"

    def __init__(self, identifier: str, columns: list[str], exact: dict[str, int]):
        self._identifier = identifier
        self._columns = columns
        self._exact = exact

    def list_objects(self):
        return [SimpleNamespace(identifier=self._identifier)]

    def table_metadata(self, identifier: str):
        cols = [SimpleNamespace(name=name) for name in self._columns]
        return SimpleNamespace(identifier=identifier), cols

    def exact_distinct_counts(self, identifier: str, columns: list[str]) -> dict:
        return {c: self._exact[c] for c in columns if c in self._exact}


def _semantic():
    model = SimpleNamespace(
        name="orders", model_ref="stg_orders", measures={"order_count": "order_id"}
    )
    return SimpleNamespace(semantic_models=[model], metrics=[])


def _check(identifier: str, baseline: int, *, exact: bool) -> CardinalityCheck:
    return CardinalityCheck(
        identifier=identifier,
        column="status",
        dimension="status",
        semantic_model="orders",
        baseline_distinct=baseline,
        baseline_exact=exact,
    )


def test_approximate_cardinality_wobble_within_error_band_is_not_drift():
    # The field phantom: an approximate baseline of 2757 versus an exact 2756 is
    # HLL noise (0.04%), well inside the sketch's error band, so no finding fires.
    identifier = "test.shop.dim_products"
    adapter = _StubAdapter(identifier, ["status"], {"status": 2756})
    findings = cardinality_drift(
        adapter, [_check(identifier, 2757, exact=False)], _semantic()
    )
    assert findings == []


def test_real_cardinality_change_beyond_the_band_still_fires():
    identifier = "test.shop.dim_products"
    adapter = _StubAdapter(identifier, ["status"], {"status": 3200})
    findings = cardinality_drift(
        adapter, [_check(identifier, 2757, exact=False)], _semantic()
    )
    assert len(findings) == 1
    assert findings[0].code == "dimension_cardinality_changed"
    assert findings[0].exact is False
    assert findings[0].data["distinct_after"] == 3200


def test_small_dimension_delta_of_one_is_not_swallowed_by_the_band():
    # At a handful of distinct values the band is zero, so a genuine new category
    # (5 -> 6) fires even off an approximate baseline: the gate suppresses noise,
    # not real low-cardinality changes.
    identifier = "test.shop.stg_orders"
    adapter = _StubAdapter(identifier, ["status"], {"status": 6})
    findings = cardinality_drift(
        adapter, [_check(identifier, 5, exact=False)], _semantic()
    )
    assert len(findings) == 1
    assert findings[0].data["distinct_after"] == 6


def test_exact_baseline_fires_on_any_change():
    # An exact baseline is proof, so even a delta of one is real drift.
    identifier = "test.shop.dim_products"
    adapter = _StubAdapter(identifier, ["status"], {"status": 2756})
    findings = cardinality_drift(
        adapter, [_check(identifier, 2757, exact=True)], _semantic()
    )
    assert len(findings) == 1
    assert findings[0].exact is True


def test_clean_project_reports_no_semantic_drift(maintain_repo):
    maintain_repo.snapshot()
    rc, payload = maintain_repo.dex("maintain", "semantic")
    assert rc == 0 and payload["status"] == "ok"
    assert payload["data"]["finding_count"] == 0


def test_definition_change_and_churn_are_reported(maintain_repo):
    maintain_repo.snapshot()
    changed = SEMANTIC_YAML.replace("label: Revenue", "label: Gross revenue").replace(
        "  - name: order_volume\n"
        "    label: Order volume\n"
        "    type: simple\n"
        "    type_params:\n"
        "      measure: order_count\n",
        "  - name: orders_shipped\n"
        "    label: Orders shipped\n"
        "    type: simple\n"
        "    type_params:\n"
        "      measure: order_count\n",
    )
    maintain_repo.edit(SEMANTIC_PATH, changed)

    rc, payload = maintain_repo.dex("maintain", "semantic")
    assert rc == 0 and payload["status"] == "ok"
    by_code = {}
    for finding in payload["data"]["findings"]:
        by_code.setdefault(finding["code"], []).append(finding)

    changed_names = {f["data"]["name"] for f in by_code["definition_changed"]}
    assert changed_names == {"revenue"}
    # The dbt path names the file, which is the non-degraded half of the pair
    # asserted by test_a_pathless_definition_omits_its_provenance below.
    assert by_code["definition_changed"][0]["data"]["path"] == SEMANTIC_PATH
    assert {f["data"]["name"] for f in by_code["definition_added"]} == {
        "orders_shipped"
    }
    assert {f["data"]["name"] for f in by_code["definition_removed"]} == {
        "order_volume"
    }


def test_dangling_model_reference_when_model_is_deleted(maintain_repo):
    maintain_repo.snapshot()
    (maintain_repo.project_dir / "models" / "staging" / "stg_orders.sql").unlink()

    _rc, payload = maintain_repo.dex("maintain", "semantic")
    dangling = [
        f for f in payload["data"]["findings"] if f["code"] == "dangling_reference"
    ]
    assert any(
        f["data"].get("missing_model") == "stg_orders"
        and f["impacted_metrics"] == ["order_volume", "revenue"]
        for f in dangling
    )


def test_dangling_column_reference_when_dimension_column_drops(maintain_repo):
    maintain_repo.snapshot()
    maintain_repo.sql("ALTER TABLE stg_orders DROP status")

    _rc, payload = maintain_repo.dex("maintain", "semantic")
    dangling = [
        f for f in payload["data"]["findings"] if f["code"] == "dangling_reference"
    ]
    assert len(dangling) == 1
    finding = dangling[0]
    assert finding["identifier"] == "warehouse.main.stg_orders"
    assert finding["column"] == "status"
    assert finding["severity"] == "high"
    assert finding["data"] == {
        "semantic_model": "orders",
        "role": "dimension",
        "name": "status",
    }
    # A broken dimension breaks the whole semantic model, so all its metrics.
    assert finding["impacted_metrics"] == ["order_volume", "revenue"]
    assert finding["impacted_models"] == ["stg_orders"]


def test_new_categorical_value_is_a_cardinality_delta_never_a_value(maintain_repo):
    maintain_repo.snapshot()
    maintain_repo.sql(
        "INSERT INTO stg_orders VALUES (999, 1, 5.0, 'refunded', DATE '2024-03-01')"
    )

    _rc, payload = maintain_repo.dex("maintain", "semantic")
    findings = [
        f
        for f in payload["data"]["findings"]
        if f["code"] == "dimension_cardinality_changed"
    ]
    assert len(findings) == 1
    finding = findings[0]
    assert finding["identifier"] == "warehouse.main.stg_orders"
    assert finding["column"] == "status"
    assert finding["data"]["distinct_before"] == 5
    assert finding["data"]["distinct_after"] == 6
    assert finding["data"]["dimension"] == "status"
    assert "widened" in finding["detail"]
    assert finding["impacted_metrics"] == ["order_volume", "revenue"]

    # The whole point of cardinality-delta detection: the new value itself
    # never reaches the envelope (or `.dex/`); naming it is a job for a
    # firewalled `explore query` if the user asks.
    assert "refunded" not in json.dumps(payload)


def test_a_pathless_definition_omits_its_provenance():
    """A definition with no file yields `definition_changed` without a `path`.

    Direct rather than through the CLI because this payload reads the *current*
    project, and `maintain semantic` refuses without a dbt project to read, so a
    non-dbt format reaches this detector only as a caller. `kind` and `name`
    still identify the definition, which is what makes the finding actionable
    with the file gone.
    """

    baseline = SemanticLayer(
        semantic_models=[
            SemanticModelDef(name="orders", content_sha256="before"),
        ],
        notes=["definitions are objects, not files"],
    )
    current = SemanticLayer(
        semantic_models=[
            SemanticModelDef(name="orders", content_sha256="after"),
        ]
    )
    snap = Snapshot(created_at="2024-01-01T00:00:00", semantic_layer=baseline)

    findings = semantic_free_drift(None, current, [], snap)

    changed = [f for f in findings if f.code == "definition_changed"]
    assert len(changed) == 1
    assert changed[0].data == {"kind": "semantic_model", "name": "orders"}


def _snapshot_with_two_semantic_models() -> tuple[Snapshot, SemanticLayer]:
    """Two unrelated models, each with one categorical dimension carrying a
    distinct-count baseline: the minimum shape `cardinality_plan` needs."""

    warehouse = WarehouseBaseline(
        datasets=[
            Dataset(
                identifier="db.main.orders",
                columns=[
                    ColumnProfile(
                        name="status",
                        data_type="VARCHAR",
                        distinct_count=5,
                        distinct_count_exact=True,
                    )
                ],
            ),
            Dataset(
                identifier="db.main.customers",
                columns=[
                    ColumnProfile(
                        name="region",
                        data_type="VARCHAR",
                        distinct_count=4,
                        distinct_count_exact=True,
                    )
                ],
            ),
        ]
    )
    semantic = SemanticLayer(
        semantic_models=[
            SemanticModelDef(
                name="orders",
                path="orders.yml",
                content_sha256="a",
                model_ref="orders",
                categorical_dimensions={"status": "status"},
            ),
            SemanticModelDef(
                name="customers",
                path="customers.yml",
                content_sha256="b",
                model_ref="customers",
                categorical_dimensions={"region": "region"},
            ),
        ]
    )
    snap = Snapshot(
        created_at="2024-01-01T00:00:00", warehouse=warehouse, semantic_layer=semantic
    )
    return snap, semantic


def test_cardinality_plan_unscoped_covers_every_semantic_model():
    snap, semantic = _snapshot_with_two_semantic_models()
    checks = cardinality_plan(semantic, snap)
    assert {c.identifier for c in checks} == {"db.main.orders", "db.main.customers"}


def test_cardinality_plan_scope_narrows_to_the_named_identifier():
    """#115: the paid axis must actually skip the unscoped checks, not just
    filter the findings they would have produced."""

    snap, semantic = _snapshot_with_two_semantic_models()
    checks = cardinality_plan(semantic, snap, {"orders"})
    assert {c.identifier for c in checks} == {"db.main.orders"}


def test_cardinality_plan_scope_matches_dimension_name_too():
    """Scope vocabulary matches _semantic_scope's: a dimension name narrows
    the plan just like a table name does."""

    snap, semantic = _snapshot_with_two_semantic_models()
    checks = cardinality_plan(semantic, snap, {"region"})
    assert {c.identifier for c in checks} == {"db.main.customers"}


def test_semantic_needs_a_dbt_project(maintain_repo):
    maintain_repo.snapshot()
    (maintain_repo.project_dir / "dbt_project.yml").unlink()

    rc, payload = maintain_repo.dex("maintain", "semantic")
    assert rc == 1 and payload["status"] == "error"
    assert "dbt project" in payload["errors"][0]


def test_scope_by_semantic_name(maintain_repo):
    maintain_repo.snapshot()
    maintain_repo.edit(
        SEMANTIC_PATH, SEMANTIC_YAML.replace("label: Revenue", "label: Gross")
    )
    maintain_repo.sql("INSERT INTO stg_orders VALUES (999, 1, 5.0, 'refunded', NULL)")

    _rc, payload = maintain_repo.dex("maintain", "semantic", "status")
    codes = {f["code"] for f in payload["data"]["findings"]}
    assert codes == {"dimension_cardinality_changed"}


def test_the_free_half_is_an_answer_and_the_scan_is_an_offer(
    maintain_repo, monkeypatch
):
    """`maintain semantic` on a billed connector, at the branch that decides.

    The reference and definition checks are free and complete on every call, so
    the response is `ok` and those findings are final. The cardinality scan is
    priced work the caller did not ask for, and it comes back as an offer rather
    than gating the answer behind a confirmation of it. Driven through a stubbed
    handshake because the fixture warehouse is DuckDB, which has no cost gate at
    all and so never reaches this branch.
    """

    from exmergo_dex_core import command_args
    from exmergo_dex_core.envelope import Cost, Paradigm, Status
    from exmergo_dex_core.maintain import commands as maintain_cmds
    from exmergo_dex_core.results import ConfirmationRequest, to_envelope

    maintain_repo.snapshot()
    maintain_repo.sql(
        "INSERT INTO stg_orders VALUES (999, 1, 5.0, 'refunded', DATE '2024-03-01')"
    )

    captured: dict = {}

    def stub(command, adapter, estimate, **kwargs):
        captured.update(command=command, estimate=estimate, **kwargs)
        return ConfirmationRequest(
            cost=Cost(paradigm=Paradigm.BYTES_SCANNED, estimate=estimate),
            data={"command": command, "estimated_bytes": estimate, **kwargs},
        )

    monkeypatch.setattr(command_args, "confirmation_request", stub)

    from exmergo_dex_core.engine import DexEngine

    with DexEngine.from_repo(str(maintain_repo.root)) as engine:
        result = maintain_cmds.semantic_drift(engine)

    assert result.pending_confirmation is None
    assert result.pending_offer is not None
    assert captured["axes"] == ["semantic_cardinality"]

    envelope = to_envelope(result)
    assert envelope.status is Status.OK
    assert envelope.data["offer"]["axes"] == ["semantic_cardinality"]
    assert envelope.cost.estimate is None
    # The free half really is complete: the definition/reference findings are in
    # the envelope, while the value behind the cardinality delta never ran.
    assert not [
        f
        for f in envelope.data["findings"]
        if f["code"] == "dimension_cardinality_changed"
    ]
