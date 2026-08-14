"""A drift report that will not parse is treated as absent, not refused.

The opposite policy from the baseline and the cache, and deliberately so. The
reasoning was already written down beside `DRIFT_SCHEMA_VERSION` in
`maintain/drift.py` before this shipped: the baseline is *vouched for* and
nothing else reproduces it, while a drift report is *derived* and `maintain
check` regenerates it from the baseline on demand. So an unreadable report has a
cheap correct answer -- rebuild -- that an unreadable baseline does not have.

That note also named the defect it was leaving open: an unparseable report
"still raises out of `load_drift` uncaught and is classified as a request
error". `_stored_drift` closes it by taking the rebuild path both callers
already had for a missing report.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from exmergo_dex_core.maintain.drift import DriftReport


def _corrupt_drift(root: Path) -> None:
    """Break the stored drift report through the file, so the document holds a
    shape the model would refuse to construct.

    ⚠️ The write is followed by a positive control on the inducer itself, and it
    is not decoration: the first version of this helper set a ``findings`` key,
    which `DriftReport` does not have. The document still parsed, every arm below
    exercised a perfectly healthy report, and the refusal test passed for the
    wrong reason. A corruption fixture that has quietly stopped corrupting is
    indistinguishable from a fix that works.
    """

    path = root / ".dex" / "drift.json"
    assert path.exists(), "no drift report to corrupt; run `maintain check` first"
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc["axes"] = "not a mapping"
    path.write_text(json.dumps(doc), encoding="utf-8")

    with pytest.raises(ValidationError):
        DriftReport.model_validate_json(path.read_text(encoding="utf-8"))


def test_a_corrupt_drift_report_is_rebuilt_rather_than_refused(maintain_repo):
    """The defect, and the shape of its fix.

    Before `_stored_drift`, the `ValidationError` from `load_drift` reached the
    CLI catch-all and `maintain check` reported `reason: request` -- an operator
    told to fix their arguments, for a file they never typed. It now rebuilds,
    which is what `_record_axes` already did for a report measured against a
    different baseline.
    """

    maintain_repo.snapshot()
    maintain_repo.dex("maintain", "check")
    _corrupt_drift(maintain_repo.root)

    code, payload = maintain_repo.dex("maintain", "check")

    assert code == 0, payload.get("errors")
    assert payload["reason"] is None, (
        "an unreadable derived document is rebuilt, not reported as bad input"
    )


def test_the_rebuilt_report_is_written_back_readable(maintain_repo):
    """Rebuilding has to leave the corruption gone rather than route around it
    once. Otherwise every subsequent run pays the same silent recovery, and the
    stored document stays broken for anything that reads it directly."""

    maintain_repo.snapshot()
    maintain_repo.dex("maintain", "check")
    _corrupt_drift(maintain_repo.root)

    maintain_repo.dex("maintain", "check")

    stored = (maintain_repo.root / ".dex" / "drift.json").read_text(encoding="utf-8")
    assert isinstance(json.loads(stored)["axes"], dict), (
        "the rebuild replaced the corrupt document rather than skipping past it"
    )
    DriftReport.model_validate_json(stored)


def test_reconcile_names_the_command_that_rebuilds(maintain_repo):
    """`reconcile` is the caller whose absent path REFUSES rather than rebuilds,
    and that is still the right answer for a corrupt report: it has nothing to
    propose from. The refusal it already had for a missing report is the correct
    one here too, so this adds no class -- it routes to the existing message."""

    maintain_repo.snapshot()
    maintain_repo.dex("maintain", "check")
    _corrupt_drift(maintain_repo.root)

    code, payload = maintain_repo.dex("maintain", "reconcile")

    assert code != 0
    assert payload["reason"] == "prerequisite"
    message = " ".join(payload["errors"])
    assert "maintain check" in message, (
        "the remedy names the command that produces a report, which is the same "
        "answer an absent one gets"
    )


@pytest.mark.parametrize("subcommand", ("check", "schema", "volume"))
def test_a_readable_drift_report_is_not_discarded(maintain_repo, subcommand):
    """The quiet arm, and it has to assert MORE than a zero exit.

    "Treat unreadable as absent" is one bug away from "treat everything as
    absent", and that bug is invisible on exit codes: axes would silently stop
    merging across runs and every report would look freshly built. So this pins
    that a prior axis SURVIVES a later focused run, which is the behaviour a
    too-eager rebuild would destroy.
    """

    maintain_repo.snapshot()
    maintain_repo.dex("maintain", "check")

    code, payload = maintain_repo.dex("maintain", subcommand)
    assert code == 0, payload.get("errors")

    doc = json.loads(
        (maintain_repo.root / ".dex" / "drift.json").read_text(encoding="utf-8")
    )
    assert doc["axes"], "a readable report kept its merged axes across runs"
