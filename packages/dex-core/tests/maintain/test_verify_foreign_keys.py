"""A project format's declared foreign keys, judged with no dbt artifacts (#1197).

The world is the issue's reproduction: three relations in an ``analytics``
schema, a format with no ``target/`` that places each model through
``model_relations`` and declares two foreign keys from ``fact_orders`` to the
same parent, the way dagster-dex reads a ``relationships`` test (no physical
relation on the key, so placement is the format's map). There is no dbt project
in the repository at all, so anything `maintain verify` reports about a key came
through the format seam.

Every case asserts the record as well as the findings, because a key that went
unmeasured must never read as satisfied.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path
from typing import ClassVar

import pytest

from exmergo_dex_core.dbt_project import (
    DeclaredCompositeKey,
    DeclaredForeignKey,
    DeclaredKey,
    ProjectDefinitions,
)
from exmergo_dex_core.explore import relationships as rel_mod
from exmergo_dex_core.maintain.snapshot import SemanticLayer, TransformLayer

_MODELS = ("dim_date", "dim_store", "fact_orders")

#: The two keys the issue declares, as dagster-dex hands them over.
_ORDER_DATE = DeclaredForeignKey(
    model="fact_orders",
    column="order_date_key",
    to_model="dim_date",
    to_column="date_key",
    source="declaration",
)
_SHIP_DATE = DeclaredForeignKey(
    model="fact_orders",
    column="ship_date_key",
    to_model="dim_date",
    to_column="date_key",
    source="declaration",
)


class KeysProject:
    """A project format with no files and no dbt artifacts.

    Class attributes stand in for what a test changes about the format: which
    models it places, and which foreign keys it declares.
    """

    name = "keys"
    relations: ClassVar[dict[str, str]] = {m: f"analytics.{m}" for m in _MODELS}
    foreign_keys: ClassVar[list[DeclaredForeignKey]] = [_ORDER_DATE, _SHIP_DATE]

    def __init__(self, context) -> None:
        self.context = context

    def definitions(self) -> ProjectDefinitions:
        return ProjectDefinitions(
            present=True,
            relationship_source="declaration",
            declared_keys=[
                DeclaredKey(
                    model="dim_date", column="date_key", unique=True, source="yaml"
                ),
                DeclaredKey(
                    model="fact_orders", column="order_id", unique=True, source="yaml"
                ),
            ],
            declared_composite_keys=[
                DeclaredCompositeKey(
                    model="dim_store", columns=["region", "store_code"], source="yaml"
                )
            ],
            foreign_keys=list(self.foreign_keys),
            model_relations=dict(self.relations),
            built_relation_names=list(_MODELS),
        )

    def transform_layer(self) -> TransformLayer:
        return TransformLayer(models=list(_MODELS))

    def semantic_layer(self) -> SemanticLayer:
        return SemanticLayer()


class Repo:
    def __init__(self, root: Path, db_path: Path, run) -> None:
        self.root = root
        self.db_path = db_path
        self._run = run

    def verify(self, *argv: str) -> dict:
        rc, payload = self._run(
            "--repo-root", str(self.root), "maintain", "verify", *argv
        )
        assert rc == 0 and payload["status"] == "ok", payload
        return payload

    def sql(self, *statements: str) -> None:
        import duckdb

        conn = duckdb.connect(str(self.db_path))
        for statement in statements:
            conn.execute(statement)
        conn.close()


@pytest.fixture
def keys_repo(tmp_path: Path, dex, monkeypatch: pytest.MonkeyPatch) -> Repo:
    """The issue's clean arm: 90 consecutive days, 20 stores, 400 orders whose
    two date keys both fall inside ``dim_date``."""

    pytest.importorskip("duckdb")
    module = types.ModuleType("dex_keys_format")
    module.keys_project = KeysProject
    monkeypatch.setitem(sys.modules, "dex_keys_format", module)

    root = tmp_path / "repo"
    root.mkdir()
    db_path = root / "warehouse.duckdb"
    repo = Repo(root, db_path, dex)
    repo.sql(
        "CREATE SCHEMA analytics",
        "CREATE TABLE analytics.dim_date AS SELECT "
        "CAST(strftime(DATE '2024-01-01' + i::INTEGER, '%Y%m%d') AS INTEGER) "
        "AS date_key FROM range(0, 90) t(i)",
        "CREATE TABLE analytics.dim_store AS SELECT "
        "(['east','west','north','south'])[(i % 4) + 1] AS region, "
        "(i // 4)::INTEGER AS store_code FROM range(0, 20) t(i)",
        "CREATE TABLE analytics.fact_orders AS SELECT i::INTEGER AS order_id, "
        "CAST(strftime(DATE '2024-01-01' + (i % 90)::INTEGER, '%Y%m%d') AS INTEGER) "
        "AS order_date_key, "
        "CAST(strftime(DATE '2024-01-01' + ((i + 2) % 90)::INTEGER, '%Y%m%d') "
        "AS INTEGER) AS ship_date_key FROM range(1, 401) t(i)",
    )
    (root / ".dex").mkdir()
    (root / ".dex" / "config.yml").write_text(
        f"connector: duckdb\nduckdb:\n  path: {db_path.as_posix()}\n"
        "project:\n  format: dex_keys_format:keys_project\n",
        encoding="utf-8",
    )
    return repo


def _plant_orphan(repo: Repo) -> None:
    """The issue's orphan: one order shipped on a day ``dim_date`` lacks, while
    the day it was ordered is there."""

    repo.sql(
        "UPDATE analytics.fact_orders SET ship_date_key = 20300101 WHERE order_id = 11"
    )


def _records(payload: dict) -> dict[str, dict]:
    return {r["column"]: r for r in payload["data"]["foreign_key_coverage"]}


def _codes(payload: dict) -> list[tuple[str, str | None]]:
    return [
        (f["code"], f.get("column"))
        for f in payload["data"]["findings"]
        if f["code"] == "foreign_key_orphans"
    ]


def test_clean_keys_are_satisfied_and_report_nothing(keys_repo):
    payload = keys_repo.verify()

    assert payload["data"]["findings"] == []
    assert "foreign_key_contract" not in payload["data"]["suppressed"]
    records = _records(payload)
    assert set(records) == {"order_date_key", "ship_date_key"}
    for column, record in records.items():
        assert record["status"] == "satisfied", record
        assert record["model"] == "fact_orders"
        assert record["column"] == column
        assert record["to_model"] == "dim_date"
        assert record["to_column"] == "date_key"
        assert record["relation"] == "analytics.fact_orders"
        assert record["to_relation"] == "analytics.dim_date"
        assert record["threshold_source"] == "contract"
        assert record["thresholds"] == {"unmatched_rows": 0}
        assert record["measured"]["eligible_rows"] == 400
        assert record["measured"]["unmatched_rows"] == 0


def test_one_orphan_contradicts_its_own_key_and_not_its_sibling(keys_repo):
    """1 row in 400 is far under any default fraction; a declared key holds
    containment, so the one row is the finding. The other key to the same
    parent stays satisfied, as its own record."""

    _plant_orphan(keys_repo)

    payload = keys_repo.verify()

    assert _codes(payload) == [("foreign_key_orphans", "ship_date_key")]
    (finding,) = [
        f for f in payload["data"]["findings"] if f["code"] == "foreign_key_orphans"
    ]
    assert finding["identifier"] == "fact_orders"
    assert finding["severity"] == "high"
    assert finding["data"]["to_model"] == "dim_date"
    assert finding["data"]["unmatched_rows"] == 1
    assert finding["data"]["unmatched_keys"] == 1
    assert finding["data"]["threshold"] == 0
    records = _records(payload)
    assert records["ship_date_key"]["status"] == "contradicted"
    assert records["ship_date_key"]["finding"] == "foreign_key_orphans"
    assert records["ship_date_key"]["measured"]["unmatched_rows"] == 1
    assert records["order_date_key"]["status"] == "satisfied"


def test_the_violated_arm_as_filed_keeps_the_key_beside_the_grains(keys_repo):
    """The issue's whole violated arm: the two grain breaks it already reported
    are unchanged, and the orphan is now the third finding."""

    keys_repo.sql(
        "INSERT INTO analytics.fact_orders SELECT * FROM analytics.fact_orders "
        "WHERE order_id = 7",
        "INSERT INTO analytics.dim_store VALUES ('east', 0)",
    )
    _plant_orphan(keys_repo)

    payload = keys_repo.verify()

    found = sorted(
        (f["code"], f["identifier"], f.get("column"))
        for f in payload["data"]["findings"]
    )
    assert found == [
        ("foreign_key_orphans", "fact_orders", "ship_date_key"),
        ("grain_broken", "dim_store", None),
        ("grain_broken", "fact_orders", "order_id"),
    ]


def test_null_child_values_are_not_orphans(keys_repo):
    """dbt's relationships test does not constrain NULLs; neither does this."""

    keys_repo.sql(
        "UPDATE analytics.fact_orders SET ship_date_key = NULL WHERE order_id <= 5"
    )

    payload = keys_repo.verify()

    assert _codes(payload) == []
    record = _records(payload)["ship_date_key"]
    assert record["status"] == "satisfied"
    assert record["null_keys"] == "excluded"
    assert record["measured"]["null_key_rows"] == 5
    assert record["measured"]["eligible_rows"] == 395


def test_a_model_the_format_does_not_place_is_unchecked_not_found_by_name(
    keys_repo, monkeypatch: pytest.MonkeyPatch
):
    """dagster-dex leaves a model that names no table out of model_relations.
    Its table is in the warehouse under the same name, with an orphan in it,
    and is still not read: the format did not say that is the relation."""

    _plant_orphan(keys_repo)
    monkeypatch.setattr(
        KeysProject,
        "relations",
        {m: f"analytics.{m}" for m in _MODELS if m != "fact_orders"},
    )

    payload = keys_repo.verify()

    assert _codes(payload) == []
    records = _records(payload)
    for record in records.values():
        assert record["status"] == "unchecked", record
        assert record["reason_code"] == "no_declared_relation"
        assert record["relation"] is None
        assert "fact_orders" in record["reason"]
        assert "measured" not in record
    assert any(
        "declared foreign key(s) were not measured" in w and "no_declared_relation" in w
        for w in payload["warnings"]
    )


def test_a_parent_the_format_does_not_place_is_unchecked(
    keys_repo, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(
        KeysProject,
        "relations",
        {m: f"analytics.{m}" for m in _MODELS if m != "dim_date"},
    )

    records = _records(keys_repo.verify())

    assert {r["status"] for r in records.values()} == {"unchecked"}
    assert {r["reason_code"] for r in records.values()} == {"no_declared_relation"}
    assert {r["to_relation"] for r in records.values()} == {None}


def test_a_placed_relation_missing_from_the_warehouse_is_unchecked(
    keys_repo, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(
        KeysProject,
        "relations",
        {**KeysProject.relations, "dim_date": "analytics.dim_calendar"},
    )

    payload = keys_repo.verify()

    records = _records(payload)
    assert {r["reason_code"] for r in records.values()} == {"relation_not_found"}
    assert {r["status"] for r in records.values()} == {"unchecked"}
    # The relation check says why on its own axis.
    assert [
        f["identifier"]
        for f in payload["data"]["findings"]
        if f["code"] == "no_relation"
    ] == ["dim_date"]


def test_a_declared_column_the_relation_lacks_is_unchecked(
    keys_repo, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(
        KeysProject,
        "foreign_keys",
        [_ORDER_DATE, _SHIP_DATE.model_copy(update={"column": "shipped_date_key"})],
    )

    records = _records(keys_repo.verify())

    assert records["shipped_date_key"]["status"] == "unchecked"
    assert records["shipped_date_key"]["reason_code"] == "column_not_found"
    assert records["order_date_key"]["status"] == "satisfied"


def test_an_empty_child_is_not_applicable_rather_than_satisfied(keys_repo):
    keys_repo.sql("DELETE FROM analytics.fact_orders")

    records = _records(keys_repo.verify())

    for record in records.values():
        assert record["status"] == "not_applicable", record
        assert record["reason_code"] == "empty_input"


def test_a_key_declared_twice_is_one_record(keys_repo, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        KeysProject,
        "foreign_keys",
        [_ORDER_DATE, _SHIP_DATE, _SHIP_DATE.model_copy(update={"source": "yaml"})],
    )

    payload = keys_repo.verify()

    assert sorted(r["column"] for r in payload["data"]["foreign_key_coverage"]) == [
        "order_date_key",
        "ship_date_key",
    ]


def test_a_format_declaring_no_key_says_so(keys_repo, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(KeysProject, "foreign_keys", [])

    payload = keys_repo.verify()

    assert "foreign_key_coverage" not in payload["data"]
    assert payload["data"]["suppressed"]["foreign_key_contract"] == (
        "the project format declares no foreign key"
    )


def test_a_scoped_run_judges_only_the_selected_models_keys(keys_repo):
    _plant_orphan(keys_repo)

    payload = keys_repo.verify("dim_date")

    assert "foreign_key_coverage" not in payload["data"]
    assert payload["data"]["suppressed"]["foreign_key_contract"] == (
        "the project format declares no foreign key in scope"
    )
    assert _codes(keys_repo.verify("fact_orders")) == [
        ("foreign_key_orphans", "ship_date_key")
    ]


def test_an_unconfirmed_probe_is_offered_and_every_key_unchecked(
    keys_repo, monkeypatch: pytest.MonkeyPatch
):
    """The probe is a scan: priced through verify's one combined handshake, and
    a key it did not buy reads as unchecked, never satisfied."""

    import exmergo_dex_core.command_args as command_args_mod
    from exmergo_dex_core.results import ConfirmationRequest

    asked: list[dict] = []

    def offer(command, adapter, estimate, **kwargs):
        asked.append(kwargs)
        return ConfirmationRequest(data={"axes": kwargs["axes"]})

    monkeypatch.setattr(command_args_mod, "confirmation_request", offer)
    _plant_orphan(keys_repo)

    payload = keys_repo.verify()

    assert len(asked) == 1
    assert asked[0]["axes"] == ["foreign_key_contract"]
    assert set(asked[0]["per_table"]) == {"(foreign key probes)"}
    assert "1 foreign key coverage statement(s)" in asked[0]["notes"][0]
    assert _codes(payload) == []
    records = _records(payload)
    assert {r["status"] for r in records.values()} == {"unchecked"}
    assert {r["reason_code"] for r in records.values()} == {"scan_offered"}
    assert payload["data"]["offer"]["axes"] == ["foreign_key_contract"]


def test_a_refused_probe_is_unchecked(keys_repo, monkeypatch: pytest.MonkeyPatch):
    def refuse(adapter, relationships, **kwargs):
        return [], [(rel, "the warehouse refused it") for rel in relationships]

    monkeypatch.setattr(rel_mod, "measure_coverage", refuse)

    records = _records(keys_repo.verify())

    assert {r["status"] for r in records.values()} == {"unchecked"}
    assert {r["reason_code"] for r in records.values()} == {"probe_refused"}


def test_a_dbt_project_reports_no_foreign_key_records(
    maintain_repo, monkeypatch: pytest.MonkeyPatch
):
    """dbt judges a declared foreign key itself, as a `relationships` test that
    build status reads, so the axis does not run and is not listed, though the
    project declares one."""

    from exmergo_dex_core.maintain import verify as verify_mod

    # The fixture never runs dbt; its own parse is not what is under test, and
    # a project that does not compile returns before any of this is reached.
    monkeypatch.setattr(
        verify_mod,
        "shadow_parse",
        lambda *a, **k: {
            "available": True,
            "reason": None,
            "success": True,
            "messages": [],
        },
    )
    maintain_repo.edit(
        "models/staging/stg_orders.yml",
        "version: 2\n\nmodels:\n  - name: stg_orders\n    columns:\n"
        "      - name: order_id\n        tests: [unique, not_null]\n"
        "      - name: customer_id\n        tests:\n          - not_null\n"
        "          - relationships:\n"
        "              to: source('main', 'customers')\n              field: id\n",
    )

    rc, payload = maintain_repo.dex("maintain", "verify")

    assert rc == 0, payload
    assert [
        f["code"]
        for f in payload["data"]["findings"]
        if f["code"] == "project_does_not_compile"
    ] == []
    assert "foreign_key_coverage" not in payload["data"]
    assert "foreign_key_contract" not in payload["data"]["suppressed"]
