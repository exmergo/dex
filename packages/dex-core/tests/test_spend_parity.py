"""Envelope-shape parity for spend: every billed command reports what it cost in
one place, under the same keys (issue #276).

`transform build` used to stamp its billed magnitude at the top of `data` *and*
under `data.spend`, while every other billed command reported only the latter. A
caller that read `data.bytes_billed` and defaulted a miss to zero therefore
reported a `maintain check` that had just scanned 0.89 GB as free, which is the
one direction a cost guarantee must never round.

The fix is a contract rather than a per-command patch, so the test is too: it
drives the five billed commands through the real CLI against one fake BigQuery
warehouse and compares the *shape* of what came back, not the figures. A sixth
command growing its own spelling of spend fails here.
"""

from __future__ import annotations

import importlib
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

pytest.importorskip("google.cloud.bigquery")

from exmergo_dex_core.cache import ColumnProfile, Dataset, DexCache, PIIFlag
from exmergo_dex_core.cli import main
from exmergo_dex_core.config import BigQueryTarget
from exmergo_dex_core.engine import DexEngine
from exmergo_dex_core.envelope import Paradigm
from exmergo_dex_core.guards.cost_guard import CostGate, ledger_field, utc_day_start
from exmergo_dex_core.maintain.snapshot import Snapshot, WarehouseBaseline
from exmergo_dex_core.storage import FilesystemStore

MB = 1024 * 1024
BUDGET = str(float(500 * MB))

# Every spelling of "what this cost" that has ever appeared in an envelope or in
# the ledger. None of them may appear at the top of `data`: that is the level
# where a missing key reads as zero, because `data` is where a command's own
# findings live and a caller cannot tell an omitted figure from an absent one.
SPEND_SPELLINGS = frozenset(
    {
        "bytes_billed",
        "seconds_billed",
        "billed_bytes",
        "billed_seconds",
        "compute_unit_hours_billed",
        "usd_billed",
        "session_spent_today",
        "spent_today",
    }
)

# The one query the fake resolves for `explore query`. Cheap, single-table, and
# already adjudicated in the seeded cache, so the run prices the query alone.
QUERY_SQL = "SELECT COUNT(*) AS n FROM `test-proj`.`shop`.`customers`"


def _scan_resolver(sql: str):
    """Answer every scanning statement the profile, map and grain paths issue.

    One superset row rather than a resolver per command: these tests assert on
    envelope shape, so what each aggregate alias comes back as matters only
    insofar as the scan completes and bills.
    """

    values = {"n_total": 100}
    for i in range(10):
        values[f"nn_{i}"] = 100
        values[f"nd_{i}"] = 100 if i == 0 else 40
        values[f"mn_{i}"] = 1
        values[f"mx_{i}"] = 100
        values[f"d_{i}"] = 100 if i == 0 else 40
    values["nonnull_fk"] = 100
    values["orphans"] = 0
    return [values]


def _run(argv: list[str], capsys) -> dict:
    """One command through the real CLI, as an agent wrapper sees it."""

    rc = main(argv)
    out = capsys.readouterr().out
    assert out.count("\n") == 1, "exactly one line on stdout"
    envelope = json.loads(out)
    assert rc == 0, envelope
    assert envelope["status"] == "ok", envelope
    return envelope


def _billed_gate(store: FilesystemStore, command: str) -> CostGate:
    """A confirmed, budgeted bytes-scanned gate wired to the store the command's
    own engine will read back, so `session_spent_today` is the real day total."""

    return CostGate(
        paradigm=Paradigm.BYTES_SCANNED,
        ceiling=float(500 * MB),
        session_ceiling=None,
        session_spent=lambda: store.spend_since(
            utc_day_start(),
            field=ledger_field(Paradigm.BYTES_SCANNED),
            connector="bigquery",
        ),
        confirmed=True,
        connector="bigquery",
        command=command,
        record=store.append_spend_log,
        lock=store.spend_lock,
    )


def _route_warehouse(monkeypatch, fake_bq_client, root: Path, command: str) -> None:
    """Point the engine's one adapter funnel at the fake warehouse.

    `DexEngine._adapter` rather than `connect.open_adapter`: it is the seam every
    command opens a connection through, so a command that grew a second way in
    would fail here rather than quietly reaching a real warehouse.
    """

    from exmergo_dex_core.adapters.bigquery import BigQueryAdapter

    store = FilesystemStore(root)

    def opener(self, cmd=None, *, budget=None, confirmed=None):
        return BigQueryAdapter(
            project="test-proj",
            cost_gate=_billed_gate(store, command),
            target=BigQueryTarget(),
            client=fake_bq_client,
            principal_type="user",
        )

    monkeypatch.setattr(DexEngine, "_adapter", opener)


def _route_build(monkeypatch, root: Path, project: Path) -> None:
    """Make a billed `transform build` run without a warehouse or a real dbt.

    The adapter is a stub carrying a real gate (a build settles outside it, so
    only its paradigm and ceiling are load-bearing), `compile_estimate` is stubbed
    so the free preflight issues no dry-runs, the dev-target check is neutralized
    so it opens no second connection, and the dbt runner writes the per-node
    billing artifact a real dbt-bigquery run writes.
    """

    from exmergo_dex_core.transform import dev_target

    build_module = importlib.import_module("exmergo_dex_core.transform.build")
    store = FilesystemStore(root)

    class StubAdapter:
        paradigm = Paradigm.BYTES_SCANNED
        name = "bigquery"

        def __init__(self):
            self.cost_gate = _billed_gate(store, "transform build")

        def close(self):
            pass

    monkeypatch.setattr(
        DexEngine, "_adapter", lambda self, cmd=None, **kw: StubAdapter()
    )
    monkeypatch.setattr(
        build_module,
        "compile_estimate",
        lambda proj, adapter, *, target, select=None, **kw: (
            float(5 * MB),
            {"stg_customers": float(5 * MB)},
            [],
        ),
    )
    monkeypatch.setattr(dev_target, "check", lambda *a, **k: [])

    run_results = json.dumps(
        {
            "results": [
                {
                    "unique_id": "model.dex_test.stg_customers",
                    "status": "success",
                    "execution_time": 1.0,
                    "adapter_response": {"bytes_billed": 3000},
                }
            ]
        }
    )

    def fake(timeout: float, cwd, env=None):
        def run(argv: list[str]):
            artifact = project / "target" / "run_results.json"
            artifact.parent.mkdir(parents=True, exist_ok=True)
            artifact.write_text(run_results, encoding="utf-8")
            return subprocess.CompletedProcess(
                args=argv, returncode=0, stdout="", stderr=""
            )

        return run

    monkeypatch.setattr(build_module, "_default_runner", fake)


def _seed_query_cache(root: Path) -> None:
    """A cache that already adjudicates `shop.customers`, so `explore query`
    prices the query itself instead of auto-profiling first. The column signature
    matches the fake's live schema, or the engine re-profiles before trusting it."""

    FilesystemStore(root).save_cache(
        DexCache(
            datasets=[
                Dataset(
                    identifier="test-proj.shop.customers",
                    columns=[
                        ColumnProfile(name="id", data_type="INTEGER", nullable=False),
                        ColumnProfile(
                            name="email",
                            data_type="STRING",
                            pii=PIIFlag(category="email", confidence=0.9),
                        ),
                    ],
                )
            ]
        )
    )


def _seed_snapshot(root: Path) -> None:
    """A baseline `maintain check` can drift against: customers with a proven
    single-column key, which is what sends the grain axis to the warehouse."""

    now = datetime.now(UTC).isoformat()
    FilesystemStore(root).save_snapshot(
        Snapshot(
            created_at=now,
            connector="bigquery",
            warehouse=WarehouseBaseline(
                datasets=[
                    Dataset(
                        identifier="test-proj.shop.customers",
                        row_count=100,
                        byte_size=5_000,
                        columns=[
                            ColumnProfile(
                                name="id",
                                data_type="INTEGER",
                                nullable=False,
                                null_fraction=0.0,
                                distinct_count=100,
                                distinct_count_exact=True,
                                is_unique=True,
                            ),
                            ColumnProfile(name="email", data_type="STRING"),
                        ],
                        candidate_keys=[["id"]],
                        grain=["id"],
                        profiled_at=now,
                    )
                ]
            ),
            warehouse_from="cache",
        )
    )


@pytest.fixture
def bigquery_project(dbt_project_dir: Path) -> Path:
    """The shared dbt project retyped to BigQuery: the dev-target preflight
    (correctly) refuses a build whose profile names a different adapter than the
    connector governing it, so the profile has to say what the test claims."""

    (dbt_project_dir / "profiles.yml").write_text(
        "dex_test:\n"
        "  target: dev\n"
        "  outputs:\n"
        "    dev:\n"
        "      type: bigquery\n"
        "      method: oauth\n"
        "      project: dex-test\n"
        "      dataset: dbt_dev\n",
        encoding="utf-8",
    )
    return dbt_project_dir


@pytest.fixture
def billed_data(
    fake_bq_client, bigquery_project: Path, tmp_path: Path, capsys, monkeypatch
) -> dict[str, dict]:
    """The `data` payload of each billed command, run against one fake warehouse.

    Each command gets its own repo root: they disagree about what the `.dex/`
    cache should hold going in (a seeded query cache, a drift baseline, a cold
    cache for `map`), and a shared root would make one command's prerequisite
    another's cache hit, which is the fastest way to a billed command that
    quietly bills nothing.
    """

    payloads: dict[str, dict] = {}

    def root(name: str) -> Path:
        path = tmp_path / name
        path.mkdir()
        return path

    # `transform build` alone runs at the repo root the dbt project lives under,
    # because that is how the project is discovered.
    with monkeypatch.context() as patch:
        _route_build(patch, tmp_path, bigquery_project)
        payloads["transform build"] = _run(
            [
                "--repo-root",
                str(tmp_path),
                "--connector",
                "bigquery",
                "transform",
                "build",
                "--target",
                "dev",
                "--confirm",
                "--budget",
                BUDGET,
            ],
            capsys,
        )["data"]

    fake_bq_client.row_resolver = _scan_resolver

    check_root = root("maintain-check")
    _seed_snapshot(check_root)
    with monkeypatch.context() as patch:
        _route_warehouse(patch, fake_bq_client, check_root, "maintain")
        payloads["maintain check"] = _run(
            [
                "--repo-root",
                str(check_root),
                "--connector",
                "bigquery",
                "maintain",
                "check",
                "--confirm",
                "--budget",
                BUDGET,
            ],
            capsys,
        )["data"]

    query_root = root("explore-query")
    _seed_query_cache(query_root)
    with monkeypatch.context() as patch:
        _route_warehouse(patch, fake_bq_client, query_root, "explore")
        fake_bq_client.row_resolver = lambda sql: [{"n": 100}]
        payloads["explore query"] = _run(
            [
                "--repo-root",
                str(query_root),
                "--connector",
                "bigquery",
                "explore",
                "query",
                QUERY_SQL,
                "--confirm",
                "--budget",
                BUDGET,
            ],
            capsys,
        )["data"]

    fake_bq_client.row_resolver = _scan_resolver

    map_root = root("explore-map")
    with monkeypatch.context() as patch:
        _route_warehouse(patch, fake_bq_client, map_root, "explore")
        payloads["explore map"] = _run(
            [
                "--repo-root",
                str(map_root),
                "--connector",
                "bigquery",
                "explore",
                "map",
                "--confirm",
                "--budget",
                BUDGET,
            ],
            capsys,
        )["data"]

    profile_root = root("explore-profile")
    with monkeypatch.context() as patch:
        _route_warehouse(patch, fake_bq_client, profile_root, "explore")
        payloads["explore profile"] = _run(
            [
                "--repo-root",
                str(profile_root),
                "--connector",
                "bigquery",
                "explore",
                "profile",
                "customers",
                "--confirm",
                "--budget",
                BUDGET,
            ],
            capsys,
        )["data"]

    return payloads


def test_every_billed_command_reports_what_it_billed(billed_data: dict[str, dict]):
    """The acceptance criterion, stated directly: one key, on all of them.

    The figure is asserted nonzero to keep the test honest about its own premise.
    A command that stopped scanning (a cache hit where the fixture meant a scan)
    would satisfy every shape assertion here while proving nothing about a billed
    run, and it is precisely a spend of zero that this issue is about misreading.
    """

    for command, data in billed_data.items():
        assert "spend" in data, f"{command} reported no spend at all"
        assert "bytes_billed" in data["spend"], (
            f"{command} reported spend without the connector's unit: {data['spend']}"
        )
        assert data["spend"]["bytes_billed"] > 0, (
            f"{command} was meant to bill and did not, so it proves nothing here"
        )


def test_no_billed_command_spells_spend_at_the_top_of_data(
    billed_data: dict[str, dict],
):
    """`data.spend` is the only place spend is reported.

    A duplicate one level up is not a harmless convenience: `transform build`
    carried `data.bytes_billed` and nothing else did, so the same read that
    worked on a build reported the next command as free.
    """

    for command, data in billed_data.items():
        stray = SPEND_SPELLINGS & set(data)
        assert not stray, f"{command} reports spend outside data.spend: {sorted(stray)}"


def test_the_spend_payload_has_the_same_keys_on_every_billed_command(
    billed_data: dict[str, dict],
):
    """Parity, which is the property that makes one read work everywhere.

    Asserted as an equality across commands rather than against a literal list of
    keys: what a connector reports (a translated USD figure, compute-unit-hours)
    is the connector's business, but it cannot be the business of which command
    asked.
    """

    shapes = {
        command: frozenset(data["spend"]) for command, data in billed_data.items()
    }
    assert len(set(shapes.values())) == 1, (
        "billed commands disagree about the spend payload's keys: "
        f"{ {command: sorted(keys) for command, keys in shapes.items()} }"
    )
