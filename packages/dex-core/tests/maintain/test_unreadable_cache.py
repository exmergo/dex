"""A cache that will not parse is a prerequisite failure, not a bad request.

`load_cache` raises on a document it cannot parse, and pydantic's
`ValidationError` subclasses `ValueError`, so an unreadable cache reached the CLI
catch-all and was classified as a REQUEST error: the operator was told they typed
something wrong when the fix is `explore map`. `storage/base.py` already asked
backends to raise a `ValueError` "so the load is classifiable when it gets one";
`readable_cache` is the wrapper that classifies it.

**Why these tests live under `tests/maintain/`** even though most of the commands
they drive are `explore` ones: `maintain_repo` is the only fixture that builds a
warehouse, runs `explore map`, and hands back a CLI runner, which is exactly the
state a cache test needs. `tests/maintain/test_snapshot.py` sets the precedent by
parametrizing six commands from here for the baseline half of the same defect.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

#: Every entry point that loads the cache and can be driven from the CLI with no
#: extra state, which is the set `readable_cache` replaced.
#:
#: Parametrized rather than asserted once on `explore profile`, for the reason
#: `BASELINE_READERS` gives in test_snapshot.py: a refusal arm that runs on one
#: command proves the helper and says nothing about whether the others reach it.
#: Reverting any single site to a bare `store.load_cache()` has to turn exactly
#: one of these red, and that is the property the parametrization buys.
CACHE_READERS = (
    ("explore", "profile", "customers"),
    ("explore", "relationships"),
    ("explore", "map"),
    ("explore", "diagram"),
    ("maintain", "snapshot"),
)


def _rewrite_cache(root: Path, **changes) -> None:
    """Edit the stored cache in place, the way a bad migration or a hand-edit
    would. Goes through the file rather than the model so the document can hold a
    shape the model would refuse to construct."""

    path = root / ".dex" / "cache.json"
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc.update(changes)
    path.write_text(json.dumps(doc), encoding="utf-8")


@pytest.mark.parametrize("argv", CACHE_READERS, ids=lambda a: " ".join(a))
def test_a_corrupt_cache_is_a_prerequisite_not_a_bad_request(maintain_repo, argv):
    """The defect. `reason: request` tells a caller its input was wrong, so a
    host retries with different arguments forever while the real fix is to
    rebuild the cache. `prerequisite` is the retry-versus-stop distinction."""

    _rewrite_cache(maintain_repo.root, datasets="not a list")

    code, payload = maintain_repo.dex(*argv)

    assert code != 0
    assert payload["reason"] == "prerequisite", (
        "a corrupt cache must not report as a bad request: the call is well "
        "formed and a named command fixes it"
    )
    message = " ".join(payload["errors"])
    assert "could not be read" in message and "explore map" in message


@pytest.mark.parametrize("argv", CACHE_READERS, ids=lambda a: " ".join(a))
def test_the_refusal_says_that_rebuilding_bills(maintain_repo, argv):
    """`maintain snapshot` is free everywhere, so "just re-run it" costs nothing
    and the baseline's remedy can say so plainly. `explore map` re-profiles the
    warehouse and BILLS. An operator choosing between investigating the stored
    document and replacing it needs that said before they run it, not after."""

    _rewrite_cache(maintain_repo.root, datasets="not a list")

    _, payload = maintain_repo.dex(*argv)

    message = " ".join(payload["errors"])
    assert "bills" in message, (
        "the remedy names a billed command; saying so is the difference between "
        "an informed replacement and a surprise invoice"
    )


def test_an_absent_cache_is_not_reported_as_unreadable(maintain_repo):
    """The arm that keeps the two states apart, and the one that makes this a
    classifier rather than a `_require_cache`.

    Absence is LEGAL at most of these call sites: a first run has no cache, and
    `explore map` exists precisely to create one. So the absent case must not
    borrow the corrupt case's message, and `explore map` must still succeed.
    """

    (maintain_repo.root / ".dex" / "cache.json").unlink()

    code, payload = maintain_repo.dex("explore", "map")

    assert code == 0, payload.get("errors")
    assert payload["reason"] is None
    assert "could not be read" not in json.dumps(payload)


def test_a_first_run_with_no_cache_is_not_refused(maintain_repo):
    """The second half of the absence arm, on a command that only READS a prior
    cache to merge pre-run state. A `_require_cache` mirroring
    `_require_baseline` would refuse here, which is why this fix classifies
    instead of requiring."""

    (maintain_repo.root / ".dex" / "cache.json").unlink()

    code, payload = maintain_repo.dex("explore", "profile", "customers")

    assert code == 0, payload.get("errors")
    assert payload["reason"] is None


@pytest.mark.parametrize("argv", CACHE_READERS, ids=lambda a: " ".join(a))
def test_a_readable_cache_is_not_refused(maintain_repo, argv):
    """The quiet arm. Without it the assertions above are equally consistent with
    EVERY cache being refused, on every command -- which would also make the
    corrupt-cache test pass, for the wrong reason."""

    code, payload = maintain_repo.dex(*argv)

    assert code == 0, payload.get("errors")
    assert payload["reason"] is None


def test_the_error_is_public_and_distinct_from_the_absent_one():
    """The unit arm on the public surface. A host that wants to page on "the
    cache is corrupt" and not on "nothing has been explored" has to be able to
    import the class and tell the two apart without matching on prose."""

    import exmergo_dex_core as dex
    from exmergo_dex_core.errors import PrerequisiteError
    from exmergo_dex_core.explore.commands import CacheRequiredError

    assert issubclass(dex.CacheUnreadableError, PrerequisiteError), (
        "same status as its sibling: the call was well formed"
    )
    assert not issubclass(dex.CacheUnreadableError, CacheRequiredError), (
        "distinct from absence, or the distinction this adds is unobservable"
    )
    assert not issubclass(CacheRequiredError, dex.CacheUnreadableError)
