"""Shape parity for spend, on both surfaces a caller reads it from: the envelope
each billed command returns (issue #276) and the `.dex/spend.jsonl` ledger those
commands write (issue #277).

One defect twice. `transform build` used to stamp its billed magnitude at the top
of `data` *and* under `data.spend`, while every other billed command reported
only the latter, so a caller that read `data.bytes_billed` and defaulted a miss to
zero reported a `maintain check` that had just scanned 0.89 GB as free. In the
ledger the same command wrote a row carrying no `entry` at all, so a caller
filtering the documented artifact on `entry == "settlement"` dropped every build,
which is the largest spender in a normal session: an 85% undercount in the one
direction a cost guarantee must never round.

Both fixes are contracts rather than per-command patches, so the tests are too:
one run of the five billed commands through the real CLI against one fake
BigQuery warehouse, and assertions on the *shape* of the envelope and of the
rows, not on the figures. A sixth command growing its own spelling of spend, or
building a ledger row by hand, fails here.

A cumulative ceiling is set, generously, so the run writes all three entry kinds
rather than settlements alone: a reservation and a release exist only where a
project has a daily cap, and their shape is as much a part of the artifact as a
settlement's.
"""

from __future__ import annotations

import importlib
import json
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest

pytest.importorskip("google.cloud.bigquery")

from exmergo_dex_core.cache import ColumnProfile, Dataset, DexCache, PIIFlag
from exmergo_dex_core.cli import main
from exmergo_dex_core.config import BigQueryTarget
from exmergo_dex_core.engine import DexEngine
from exmergo_dex_core.envelope import Paradigm
from exmergo_dex_core.guards.cost_guard import (
    LEDGER_ENTRY_KINDS,
    CostGate,
    ledger_field,
    utc_day_start,
)
from exmergo_dex_core.maintain.snapshot import Snapshot, WarehouseBaseline
from exmergo_dex_core.storage import FilesystemStore

MB = 1024 * 1024
BUDGET = str(float(500 * MB))
# Far above anything these commands can bill, so the cap is present without being
# binding: what it buys the test is the reservation and release rows, which a
# project with no daily cap never writes.
SESSION_CEILING = float(100 * 1024 * MB)

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


@dataclass(frozen=True)
class BilledRun:
    """One billed command's two spend surfaces, as a caller sees them.

    Kept together because the contract under test is that they agree: the
    envelope says what the command billed and the ledger says the same thing in
    a form other tooling reads back.
    """

    data: dict
    ledger: list[dict]


def _ledger(root: Path) -> list[dict]:
    """Every row a command left in its own repo root's ledger, in append order.

    Read as the artifact rather than through the store, because what this file
    asserts is what an external reader of `.dex/spend.jsonl` gets.
    """

    path = root / ".dex" / "spend.jsonl"
    if not path.is_file():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


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
        session_ceiling=SESSION_CEILING,
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
def billed_runs(
    fake_bq_client, bigquery_project: Path, tmp_path: Path, capsys, monkeypatch
) -> dict[str, BilledRun]:
    """What each billed command reported and what it wrote, run against one fake
    warehouse.

    Each command gets its own repo root: they disagree about what the `.dex/`
    cache should hold going in (a seeded query cache, a drift baseline, a cold
    cache for `map`), and a shared root would make one command's prerequisite
    another's cache hit, which is the fastest way to a billed command that
    quietly bills nothing. The separate roots give the ledger assertions
    something they need too: one file per command, so a row's shape can be
    attributed to the command that wrote it.
    """

    payloads: dict[str, dict] = {}
    roots: dict[str, Path] = {}

    def root(name: str) -> Path:
        path = tmp_path / name
        path.mkdir()
        return path

    # `transform build` alone runs at the repo root the dbt project lives under,
    # because that is how the project is discovered.
    roots["transform build"] = tmp_path
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
        roots["maintain check"] = check_root
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
        roots["explore query"] = query_root
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
        roots["explore map"] = map_root
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
        roots["explore profile"] = profile_root
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

    return {
        command: BilledRun(data=data, ledger=_ledger(roots[command]))
        for command, data in payloads.items()
    }


def test_every_billed_command_reports_what_it_billed(
    billed_runs: dict[str, BilledRun],
):
    """The acceptance criterion, stated directly: one key, on all of them.

    The figure is asserted nonzero to keep the test honest about its own premise.
    A command that stopped scanning (a cache hit where the fixture meant a scan)
    would satisfy every shape assertion here while proving nothing about a billed
    run, and it is precisely a spend of zero that this issue is about misreading.
    """

    for command, run in billed_runs.items():
        data = run.data
        assert "spend" in data, f"{command} reported no spend at all"
        assert "bytes_billed" in data["spend"], (
            f"{command} reported spend without the connector's unit: {data['spend']}"
        )
        assert data["spend"]["bytes_billed"] > 0, (
            f"{command} was meant to bill and did not, so it proves nothing here"
        )


def test_no_billed_command_spells_spend_at_the_top_of_data(
    billed_runs: dict[str, BilledRun],
):
    """`data.spend` is the only place spend is reported.

    A duplicate one level up is not a harmless convenience: `transform build`
    carried `data.bytes_billed` and nothing else did, so the same read that
    worked on a build reported the next command as free.
    """

    for command, run in billed_runs.items():
        stray = SPEND_SPELLINGS & set(run.data)
        assert not stray, f"{command} reports spend outside data.spend: {sorted(stray)}"


def test_the_spend_payload_has_the_same_keys_on_every_billed_command(
    billed_runs: dict[str, BilledRun],
):
    """Parity, which is the property that makes one read work everywhere.

    Asserted as an equality across commands rather than against a literal list of
    keys: what a connector reports (a translated USD figure, compute-unit-hours)
    is the connector's business, but it cannot be the business of which command
    asked.
    """

    shapes = {
        command: frozenset(run.data["spend"]) for command, run in billed_runs.items()
    }
    assert len(set(shapes.values())) == 1, (
        "billed commands disagree about the spend payload's keys: "
        f"{ {command: sorted(keys) for command, keys in shapes.items()} }"
    )


# --- the ledger side: issue #277 ---------------------------------------------


def test_every_ledger_row_declares_its_kind(billed_runs: dict[str, BilledRun]):
    """No row carries a null or absent `entry`, and no row invents a kind.

    The reported defect, stated where a sixth billed command would trip over it.
    `entry` is the field an external reader filters on to get settled spend, and
    a `transform build` row that left it null dropped the largest spender in the
    session out of that filter while still holding a correct `billed_bytes`, so
    the artifact under-reported spend while the accounting behind it was right.
    """

    for command, run in billed_runs.items():
        assert run.ledger, f"{command} wrote no ledger rows, so it proves nothing here"
        kinds = [row.get("entry") for row in run.ledger]
        assert all(kind in LEDGER_ENTRY_KINDS for kind in kinds), (
            f"{command} wrote a ledger row with no kind or a kind outside the "
            f"closed vocabulary {LEDGER_ENTRY_KINDS}: got {kinds}"
        )


def test_the_run_writes_every_entry_kind(billed_runs: dict[str, BilledRun]):
    """The premise the two tests below rest on.

    A project with no cumulative ceiling writes settlements alone, and against
    that ledger a parity assertion across kinds passes without ever having seen a
    reservation. The fixture sets a ceiling precisely so it has, and this is what
    fails if that ever stops being true.
    """

    kinds = {row["entry"] for run in billed_runs.values() for row in run.ledger}
    assert kinds == set(LEDGER_ENTRY_KINDS), (
        "the run was meant to exercise every entry kind and did not, so the "
        f"shape assertions below cover less than they claim: saw {sorted(kinds)}"
    )


def test_every_ledger_row_has_the_same_key_set(billed_runs: dict[str, BilledRun]):
    """One shape, across every command and every kind.

    Asserted as an equality rather than against a literal list, for the reason
    the envelope parity test above gives: what a row carries can be the ledger's
    business, but it cannot be the business of which command wrote it or which
    kind it is. This is the assertion that fails if a future writer builds a row
    by hand instead of going through `ledger_row`, which is exactly how
    `transform build` came to write a settlement carrying no `reservation_id` at
    all: a reader joining settlements on that key skipped or mis-joined every
    build, because an absent key and a null one are different claims.
    """

    shapes: dict[tuple[str, str], frozenset[str]] = {}
    for command, run in billed_runs.items():
        for row in run.ledger:
            shapes[(command, row["entry"])] = frozenset(row)
    assert len(set(shapes.values())) == 1, (
        "ledger rows disagree about their keys: "
        f"{ {key: sorted(keys) for key, keys in shapes.items()} }"
    )


def test_summing_settlements_reproduces_the_days_total(
    billed_runs: dict[str, BilledRun],
):
    """Settled spend is the `entry == "settlement"` filter, and on a quiesced day
    it is the day's total.

    The precondition is load-bearing and is why this is stated per command rather
    than as an invariant: `session_spent_today` is settled spend *plus* headroom
    held by commands still in flight, deliberately, because that is the number a
    concurrent command has to be measured against. Each command here ran alone
    and settled, so every reservation it took has been released and the two
    figures meet. They would not while another billed command was running, and
    they would not for a day holding the reservation of a process that was
    killed outright.
    """

    for command, run in billed_runs.items():
        settled = sum(
            row["billed_bytes"] for row in run.ledger if row["entry"] == "settlement"
        )
        assert settled == run.data["spend"]["session_spent_today"], (
            f"{command}: summing the ledger's settlements gave {settled}, and the "
            f"envelope reported a day's total of "
            f"{run.data['spend']['session_spent_today']}. Nothing else was "
            "running, so these have to be the same number"
        )
