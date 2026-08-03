"""The cumulative ceiling under concurrency: two commands, one budget.

The rest of the cost-guard suite drives one gate at a time, which is the shape
in which the guard always worked. What it could not catch is that every ceiling
decision used to derive from a reading taken once at construction, so two
commands overlapping in time were admitted against the same headroom. Both were
legal individually and wrong together, and nothing said so: the ledger afterwards
was well-formed and every entry in it was true.

Two populations can race, and they need different tests. Threads sharing a
process are what an embedding host produces; separate processes are what the CLI
produces, one per subcommand, and that is the shape the defect was reported in.
Neither is covered by the other, since an in-process lock says nothing about two
processes and a file lock says nothing about two threads holding one descriptor.

Deliberately stdlib only: `threading` and `subprocess`, no new dependency and no
parallel-test plugin. The child below drives the real `FilesystemStore` against a
real `.dex/` directory rather than a fake, because the file lock is the part
under test.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from exmergo_dex_core.config import Budget, DexConfig
from exmergo_dex_core.connect import new_cost_gate
from exmergo_dex_core.guards.cost_guard import (
    CostGuardError,
    OverCeilingError,
    utc_day_start,
)
from exmergo_dex_core.storage import FilesystemStore, MemoryStore

CEILING = 1_000.0
ESTIMATE = 600.0  # two of these do not fit under CEILING; exactly one does
ACTUAL = 500.0


def _config() -> DexConfig:
    return DexConfig(
        connector="bigquery",
        budget=Budget(ceiling=CEILING, session_ceiling=CEILING),
    )


def _new_gate(store, index: int):
    """Built through `new_cost_gate` rather than by hand, so these exercise the
    wiring a real command gets (the reader closure, the record hook, and the
    store's own lock) rather than a gate assembled the way a test finds
    convenient."""

    return new_cost_gate(
        "bigquery",
        _config(),
        store,
        confirmed=True,
        command=f"explore profile {index}",
    )


def _run_one(gate, results: list) -> None:
    """One whole billed command from admission onwards: admit, spend, settle."""

    try:
        gate.preflight_command(ESTIMATE)
    except CostGuardError as exc:
        results.append(("refused", type(exc).__name__))
        return
    try:
        gate.record_billed(ACTUAL, statement="select 1")
        results.append(("admitted", gate.spend_summary()))
    finally:
        gate.settle()


def _ledger_total(root: Path) -> float:
    path = root / ".dex" / "spend.jsonl"
    if not path.is_file():
        return 0.0
    return sum(
        json.loads(line).get("billed_bytes", 0.0)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    )


# --- threads sharing one store ---------------------------------------------------


@pytest.mark.parametrize("store_factory", ["filesystem", "memory"])
def test_concurrent_threads_cannot_both_pass_one_session_ceiling(
    tmp_path, store_factory
):
    """The acceptance test, in its in-process shape.

    Both shipped backends, because the guarantee is the store's to provide and
    a host picks the store.

    **The barrier sits between building the gates and admitting through them**,
    which is the thing worth being deliberate about. Ten commands issued at the
    same moment all read a ledger that is still empty, and under the old design
    that reading was the whole basis for every later decision, so all ten
    admitted. Letting the threads construct their own gates instead would make
    the overlap a function of scheduler luck, and a test that passes on the
    broken code some of the time is worse than no test.
    """

    store = (
        FilesystemStore(tmp_path) if store_factory == "filesystem" else MemoryStore()
    )
    results: list = []
    gates = [_new_gate(store, index) for index in range(10)]
    ready = threading.Barrier(len(gates))

    def start(gate) -> None:
        ready.wait()  # release them together, so they genuinely overlap
        _run_one(gate, results)

    threads = [threading.Thread(target=start, args=(gate,)) for gate in gates]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    admitted = [r for r in results if r[0] == "admitted"]
    assert len(admitted) == 1, f"exactly one command fits under the ceiling: {results}"
    assert len(results) == 10

    settled = (
        _ledger_total(tmp_path)
        if store_factory == "filesystem"
        else store.spend_since(utc_day_start(), connector="bigquery")
    )
    assert settled == ACTUAL
    assert settled <= CEILING


def test_a_second_thread_sees_the_first_ones_hold_while_it_is_still_running(tmp_path):
    """The property reservations exist for.

    A command that has been admitted but has not finished contributes nothing to
    the ledger under the old design, so its headroom looks free for as long as it
    runs. On a `transform build` that is minutes.
    """

    store = FilesystemStore(tmp_path)
    first = new_cost_gate(
        "bigquery", _config(), store, confirmed=True, command="transform build"
    )
    first.preflight_command(ESTIMATE)

    second = new_cost_gate(
        "bigquery", _config(), store, confirmed=True, command="explore profile"
    )
    with pytest.raises(OverCeilingError):
        second.preflight_command(ESTIMATE)

    # ...and the headroom comes back when the long command finishes.
    first.record_billed(ACTUAL, statement="select 1")
    first.settle()
    third = new_cost_gate(
        "bigquery", _config(), store, confirmed=True, command="explore profile"
    )
    assert third.preflight_command(400.0).ceiling == CEILING - ACTUAL


# --- separate processes, which is what the CLI actually produces -----------------


# Numbers arrive through argv rather than being interpolated into the source, so
# this stays readable as the Python it is and the ceiling cannot drift from the
# one the parent asserts against.
_CHILD = """
import json, os, sys, time
from exmergo_dex_core.config import Budget, DexConfig
from exmergo_dex_core.connect import new_cost_gate
from exmergo_dex_core.guards.cost_guard import CostGuardError
from exmergo_dex_core.storage import FilesystemStore

root, ready, go, ceiling, estimate, actual = sys.argv[1:7]
gate = new_cost_gate(
    "bigquery",
    DexConfig(
        connector="bigquery",
        budget=Budget(ceiling=float(ceiling), session_ceiling=float(ceiling)),
    ),
    FilesystemStore(root),
    confirmed=True,
    command="explore profile",
)

# Announce readiness, then spin until the parent drops the go file, so every
# child reaches its admission inside the same few milliseconds. Sleeping a fixed
# amount instead would make the overlap a function of process startup time, which
# is exactly the thing that varies most between a laptop and CI.
open(ready, "w").close()
while not os.path.exists(go):
    time.sleep(0.001)

try:
    gate.preflight_command(float(estimate))
except CostGuardError as exc:
    print(json.dumps({"outcome": "refused", "error": type(exc).__name__}))
    sys.exit(0)
try:
    gate.record_billed(float(actual), statement="select 1")
    print(json.dumps({"outcome": "admitted"}))
finally:
    gate.settle()
"""


def test_concurrent_processes_cannot_both_pass_one_session_ceiling(tmp_path):
    """The acceptance test, in the shape the defect was reported in.

    The CLI is one command per process, so the ledger on disk is the only thing
    two commands share and the file lock is the only thing that can serialize
    them. This is the assertion an in-process test cannot make: a `threading`
    lock would pass it while the CLI stayed broken.
    """

    root = tmp_path / "repo"
    (root / ".dex").mkdir(parents=True)
    go = tmp_path / "go"
    children = []
    for index in range(6):
        ready = tmp_path / f"ready-{index}"
        children.append(
            (
                ready,
                subprocess.Popen(  # noqa: S603
                    [
                        sys.executable,
                        "-c",
                        _CHILD,
                        str(root),
                        str(ready),
                        str(go),
                        str(CEILING),
                        str(ESTIMATE),
                        str(ACTUAL),
                    ],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                ),
            )
        )

    for ready, child in children:
        for _ in range(3_000):
            if ready.exists():
                break
            if child.poll() is not None:
                break
            threading.Event().wait(0.01)
    go.touch()

    outcomes = []
    for _, child in children:
        stdout, stderr = child.communicate(timeout=120)
        assert child.returncode == 0, stderr
        outcomes.append(json.loads(stdout.strip())["outcome"])

    assert outcomes.count("admitted") == 1, outcomes
    assert _ledger_total(root) == ACTUAL


def test_an_unreleased_reservation_holds_its_estimate_rather_than_freeing_it(tmp_path):
    """A process killed outright cannot release, and the guard errs closed.

    The wrong direction here would be to expire a stale hold, which would mean
    teaching every backend's `spend_since` to tell the entry kinds apart and
    reason about time. Holding costs an over-conservative day; freeing costs the
    overspend this whole change exists to prevent.
    """

    store = FilesystemStore(tmp_path)
    abandoned = new_cost_gate(
        "bigquery", _config(), store, confirmed=True, command="explore profile"
    )
    abandoned.preflight_command(ESTIMATE)
    del abandoned  # no settle: the process died here

    survivor = new_cost_gate(
        "bigquery", _config(), store, confirmed=True, command="explore profile"
    )
    with pytest.raises(OverCeilingError):
        survivor.preflight_command(ESTIMATE)
