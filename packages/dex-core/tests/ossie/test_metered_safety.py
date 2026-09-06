"""Native Ossie declarations under the cost guard, on a billed connector.

Ossie contributes declared keys and relationships at confidence 1.0, and
verifying one is a warehouse scan. That scan reaches the warehouse through the
same admission, budget, and ledger machinery every other scan does, and this
asserts it does rather than assuming it: a second, vendor-shaped route to the
warehouse is exactly the shape a guard gets bypassed by.

The parametrized detail lives here rather than in the safety spine, which carries
the one-line command-level statement of each rule. Nothing here stubs `CostGate`,
admission, reservations, or ledger settlement: the gate is built by
`connect.new_cost_gate`, so what is under test is the wiring the engine uses.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from exmergo_dex_core.config import DexConfig, save_config
from exmergo_dex_core.engine import DexEngine
from exmergo_dex_core.storage import FilesystemStore

pytest.importorskip("google.cloud.bigquery")

MB = 1024 * 1024

#: Two relationships onto one shared parent, plus a composite one. The shared
#: parent is what makes the batching assertion mean something: unbatched, a
#: shared dimension pays the scan once per edge that joins it.
DOCUMENT = """
version: "0.2.0.dev0"
semantic_model:
  - name: shop
    datasets:
      - name: orders
        source: test-proj.shop.customers
        primary_key: [id]
        fields:
          - name: id
            expression:
              dialects: [{dialect: BIGQUERY, expression: id}]
          - name: email
            expression:
              dialects: [{dialect: BIGQUERY, expression: email}]
      - name: events
        source: test-proj.shop.events
        primary_key: [id]
        fields:
          - name: id
            expression:
              dialects: [{dialect: BIGQUERY, expression: id}]
    relationships:
      - name: events_to_orders
        from: events
        to: orders
        from_columns: [id]
        to_columns: [id]
      - name: orders_to_orders
        from: orders
        to: orders
        from_columns: [id]
        to_columns: [id]
"""


def _repo(root: Path) -> None:
    (root / "layer.ossie.yaml").write_text(DOCUMENT, encoding="utf-8")
    save_config(
        DexConfig(
            connector="bigquery",
            bigquery={"project": "p"},
            semantic={"vendor": "ossie", "ossie": {"files": ["layer.ossie.yaml"]}},
            # The day's cap, decided once and committed, the way a project that
            # has answered the one-time ask carries it. Without it every
            # confirmed command below would refuse on that question instead of
            # on the one under test.
            budget={"session_ceiling": float(1024 * MB)},
        ),
        root,
    )


def _engine(fake_bq_client, root: Path, monkeypatch, **kwargs) -> DexEngine:
    """An Ossie-configured engine whose one connection is the BigQuery fake.

    The gate comes from ``connect.new_cost_gate`` rather than being constructed
    here, so a change to how the engine admits work reaches this test.
    """

    import exmergo_dex_core.connect as connect_mod
    from exmergo_dex_core.adapters.bigquery import BigQueryAdapter
    from exmergo_dex_core.config import BigQueryTarget, load_config
    from exmergo_dex_core.connect import new_cost_gate

    config = load_config(root)
    store = FilesystemStore(root)

    def opener(**opened):
        return BigQueryAdapter(
            project="test-proj",
            cost_gate=new_cost_gate(
                "bigquery",
                config,
                store,
                budget=opened.get("budget"),
                confirmed=opened.get("confirmed", False),
                command=opened.get("command"),
            ),
            target=BigQueryTarget(),
            client=fake_bq_client,
            principal_type="user",
        )

    monkeypatch.setattr(connect_mod, "open_adapter", opener)
    return DexEngine(config=config, store=store, repo_root=str(root), **kwargs)


def _aggregate_rows(sql: str) -> list[dict]:
    """One row carrying every alias a profile or a batched overlap probe reads.

    A confirmed verify profiles the relations first, so the resolver has to
    answer both statement shapes. Indexed aliases rather than a fixed set,
    because overlap probes are batched and one statement asks about several
    joins at once.
    """

    row: dict[str, object] = {"n_total": 100}
    for index in range(10):
        row[f"nn_{index}"] = 100
        row[f"nd_{index}"] = 100 if index == 0 else 40
        row[f"mn_{index}"] = 1
        row[f"mx_{index}"] = 100
        row[f"d_{index}"] = 100
        row[f"nonnull_fk_{index}"] = 100
        row[f"orphans_{index}"] = 0
    return [row]


@pytest.fixture
def ossie_bq(fake_bq_client, tmp_path, monkeypatch):
    _repo(tmp_path)
    fake_bq_client.row_resolver = _aggregate_rows

    def build(**kwargs):
        return _engine(fake_bq_client, tmp_path, monkeypatch, **kwargs)

    return build


def test_unconfirmed_declared_verification_executes_no_billed_statement(
    ossie_bq, fake_bq_client
):
    """Free work is allowed; billed work is not. A dry run is the estimate, so
    it is what the caller is being shown rather than something they paid for."""

    from exmergo_dex_core import ConfirmationRequiredError

    with ossie_bq() as engine, pytest.raises(ConfirmationRequiredError) as caught:
        engine.relationships(verify=True, use_project=True)

    assert caught.value.request.cost.estimate > 0
    assert fake_bq_client.query_calls, "nothing was priced, so nothing was proven"
    assert all(call.dry_run for call in fake_bq_client.query_calls)


def test_an_insufficient_command_budget_prevents_the_scan(ossie_bq, fake_bq_client):
    """Confirmation is not an override: an estimate past the ceiling refuses
    first, however confirmed the caller is."""

    from exmergo_dex_core.guards.cost_guard import OverCeilingError

    with (
        ossie_bq(confirmed=True, budget=1.0) as engine,
        pytest.raises(OverCeilingError),
    ):
        engine.relationships(verify=True, use_project=True)

    assert all(call.dry_run for call in fake_bq_client.query_calls)


def test_an_insufficient_session_budget_prevents_the_scan(
    ossie_bq, fake_bq_client, tmp_path
):
    """The day's cap binds a command whose own budget would have allowed it.

    Booked against the ledger rather than against a counter in the process, so
    spend a previous command already settled is spend this one cannot have.
    """

    from exmergo_dex_core.guards.cost_guard import OverCeilingError, utc_day_start

    store = FilesystemStore(tmp_path)
    store.append_spend_log(
        {
            "at": utc_day_start(),
            "connector": "bigquery",
            "command": "explore",
            "entry": "settlement",
            "billed_bytes": float(64 * MB),
        }
    )
    save_config(
        DexConfig(
            connector="bigquery",
            bigquery={"project": "p"},
            semantic={"vendor": "ossie", "ossie": {"files": ["layer.ossie.yaml"]}},
            budget={"session_ceiling": float(65 * MB)},
        ),
        tmp_path,
    )

    with (
        ossie_bq(confirmed=True, budget=float(500 * MB)) as engine,
        pytest.raises(OverCeilingError),
    ):
        engine.relationships(verify=True, use_project=True)

    assert all(call.dry_run for call in fake_bq_client.query_calls)


def test_a_confirmed_scan_is_server_capped_and_settles_in_the_ledger(
    ossie_bq, fake_bq_client, tmp_path
):
    """The two halves of the same guarantee.

    The ceiling is enforced by BigQuery itself through
    ``maximum_bytes_billed``, so an estimate that turns out low is stopped by
    the server rather than by dex noticing afterwards. And what it actually
    billed is written to the ledger, which is what the next command's session
    headroom is computed from.
    """

    with ossie_bq(confirmed=True, budget=float(500 * MB)) as engine:
        engine.relationships(verify=True, use_project=True)

    executed = [call for call in fake_bq_client.query_calls if not call.dry_run]
    assert executed, "the confirmed scan executed nothing"
    for call in executed:
        assert call.job_config.maximum_bytes_billed, (
            "an executed statement carried no server-side cap, so the ceiling "
            "held only as long as the estimate did"
        )

    entries = [
        json.loads(line)
        for line in (tmp_path / ".dex" / "spend.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip()
    ]
    settled = [e for e in entries if e.get("entry") == "settlement"]
    assert settled, f"the confirmed scan settled nothing in the ledger: {entries}"
    assert all(e["connector"] == "bigquery" for e in settled)


def test_the_priced_statements_and_the_executed_statements_are_one_set(
    ossie_bq, fake_bq_client
):
    """Pricing N probes and issuing N+M under-reports spend before it happens,
    which is the one thing the cost guard exists to prevent.

    The document declares two edges onto one shared parent, so this also holds
    the shared-table batching: grouped, a shared parent is read once rather than
    once per edge, and the priced set has to be grouped the same way.
    """

    with ossie_bq(confirmed=True, budget=float(500 * MB)) as engine:
        engine.relationships(verify=True, use_project=True)

    priced = [call.sql for call in fake_bq_client.query_calls if call.dry_run]
    executed = [call.sql for call in fake_bq_client.query_calls if not call.dry_run]

    assert executed, "nothing executed, so the comparison asserts nothing"
    assert set(executed) <= set(priced), (
        "a statement was executed that was never priced: "
        f"{sorted(set(executed) - set(priced))}"
    )

    def probes(statements: list[str]) -> set[str]:
        return {sql for sql in statements if "nonnull_fk_0" in sql}

    # Sets, not lists: one statement may legitimately be priced more than once
    # (a free dry run costs nothing and the estimate is taken at more than one
    # point). What may not differ is *which* statements the two halves name.
    assert probes(executed), "no overlap probe ran, so batching is unasserted"
    assert probes(executed) == probes(priced), (
        "the overlap probes that ran and the overlap probes that were priced "
        "are not the same statements, so the estimate described different work "
        "than the run did"
    )
    assert len(probes(executed)) == 1, (
        "the two declared edges share a parent relation and ran as "
        f"{len(probes(executed))} statements. Grouped, a shared parent is read "
        "once rather than once per edge, and splitting them here would mean the "
        "estimate and the run agree only because both are wrong"
    )


def test_a_declared_edge_keeps_confidence_one_whatever_the_probe_measures(
    ossie_bq, fake_bq_client
):
    """A measurement never revises a declaration's confidence.

    The layer's author asserted the join. A probe that finds the parent largely
    missing is a finding about the data, not evidence that the author did not
    say what they said, and rewriting the confidence would erase the difference
    between a declared edge and an inferred one.
    """

    fake_bq_client.row_resolver = lambda sql: [
        {**_aggregate_rows(sql)[0], "orphans_0": 100, "orphans_1": 100}
    ]

    with ossie_bq(confirmed=True, budget=float(500 * MB)) as engine:
        result = engine.relationships(verify=True, use_project=True)

    declared = [
        edge
        for edge in result.relationships
        if "ossie" in str(getattr(edge, "declared_by", "")).lower()
        or getattr(edge, "confidence", 0) == 1.0
    ]
    assert declared, "no declared edge reached the result"
    assert all(edge.confidence == 1.0 for edge in declared)


def test_the_free_catalog_opens_no_warehouse_connection(ossie_bq, fake_bq_client):
    """Reading the layer is reading files in the repository.

    A catalog read that opened a connection would put a billed connector between
    a caller and a question the repository already answers, and on a metered
    warehouse that is a handshake nobody should be asked for.
    """

    with ossie_bq() as engine:
        result = engine.semantic_list()

    assert result.catalog.view.semantic_models
    assert fake_bq_client.query_calls == []


def test_a_project_only_snapshot_opens_no_warehouse_connection(
    ossie_bq, fake_bq_client
):
    """`--project-only` re-fingerprints the repository and carries the warehouse
    block forward. It deliberately preserves warehouse staleness rather than
    laundering it into a fresh measurement, and a connection here would be that
    laundering."""

    with ossie_bq(confirmed=True, budget=float(500 * MB)) as engine:
        engine.snapshot()
        fake_bq_client.query_calls.clear()
        engine.snapshot(project_only=True)

    assert fake_bq_client.query_calls == []


def test_cached_authoring_validation_runs_no_warehouse_query(
    ossie_bq, fake_bq_client, tmp_path
):
    """Plan-time validation reads the exploration cache, never the warehouse.

    Authoring is a repository edit. Reaching a billed warehouse to decide
    whether a document may be written would put a cost handshake in front of a
    write, which is a different command than the one the caller ran.
    """

    from exmergo_dex_core.transform.native_semantic import semantic_ossie
    from exmergo_dex_core.transform.plans import EditKind, PlanEdit

    content = (tmp_path / "layer.ossie.yaml").read_text(encoding="utf-8")
    edit = PlanEdit(
        path="layer.ossie.yaml",
        kind=EditKind.SEMANTIC_DOCUMENT,
        new_content=content + "\n# reviewed\n",
    )

    with ossie_bq() as engine:
        fake_bq_client.query_calls.clear()
        semantic_ossie(engine, "revise the layer", [edit], mode="plan")

    assert fake_bq_client.query_calls == []
