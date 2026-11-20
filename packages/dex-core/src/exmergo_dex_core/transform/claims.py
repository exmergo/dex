"""The claim a rendered test checks, named so the name survives the test's own (#1448).

A test a declaration renders checks one declared fact. Incidents and "last
verified" history are kept per claim, so the claim needs a name that does not
move when dbt's does: a test's node name and ``unique_id`` are built from the
model, the column, the test and its arguments.

The id is **derived at first render and kept as written after it.** It is a
hash of what the claim asserts (the model's label, the declared field, its
columns and its parameters, and the check that asserts it), never of a test's
name, so the same declaration renders the same id in any process, plan or
fresh clone, with nothing kept anywhere. Once written it is the file's: Dex
leaves a test it finds listed by name exactly as written (#1348), so a model
renamed through ``transform rename`` keeps its claim ids, and a reader takes the
id from ``config.meta.dex.claim`` rather than recomputing it.

It sits under the ``dex`` key of the test's ``config.meta``, beside Elementary's
``disable_test_samples``. Neither dbt nor Elementary reads a ``dex`` key.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from typing import Any

#: The key under ``config.meta.dex`` that holds a rendered test's claim id.
CLAIM_KEY = "claim"

#: Bumped only if the derivation changes; an id already written never changes.
_DERIVATION_VERSION = 1


def claim_id(
    model: str,
    field: str,
    columns: Iterable[str],
    params: Mapping[str, Any] | None = None,
) -> str:
    """The id of the claim that ``field`` on ``columns`` of ``model`` makes.

    ``params`` carries what distinguishes two claims of one field on one column
    (the check a grain is asserted by, the relation a reference names). Names
    are folded to lower case, because dbt resolves them case-insensitively on
    every adapter Dex supports, and a declaration respelled ``Order_ID`` states
    the same fact.
    """

    identity = {
        "v": _DERIVATION_VERSION,
        "model": model.strip().lower(),
        "field": field,
        "columns": [column.strip().lower() for column in columns],
        "params": {
            str(key): value.strip().lower() if isinstance(value, str) else value
            for key, value in sorted((params or {}).items())
        },
    }
    canonical = json.dumps(identity, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def claim_of(test: Any) -> str | None:
    """The claim id a parsed test entry carries, or ``None``.

    Reads ``config.meta.dex.claim``, and the top-level ``meta`` spelling dbt
    still accepts on a test, so a reader finds a claim moved by hand.
    """

    if not isinstance(test, Mapping) or len(test) != 1:
        return None
    return claim_in(next(iter(test.values())))


def claim_in(body: Any) -> str | None:
    """The claim id a test's body carries, or ``None``: the mapping under a
    parsed test entry's name, or a test's node in the dbt manifest (#1498)."""

    if not isinstance(body, Mapping):
        return None
    config = body.get("config")
    for meta in (
        config.get("meta") if isinstance(config, Mapping) else None,
        body.get("meta"),
    ):
        dex = meta.get("dex") if isinstance(meta, Mapping) else None
        claim = dex.get(CLAIM_KEY) if isinstance(dex, Mapping) else None
        if isinstance(claim, str) and claim:
            return claim
    return None


def without_claim(test: Any) -> Any:
    """A parsed test entry with its ``config.meta.dex.claim`` removed.

    What lets a test Dex rendered be recognised whether or not it carries a
    claim: an empty ``dex`` left behind goes too, so a claimed render and the
    render before claims compare equal.
    """

    if not isinstance(test, Mapping) or len(test) != 1:
        return test
    [(name, body)] = test.items()
    if not isinstance(body, Mapping):
        return test
    config = body.get("config")
    meta = config.get("meta") if isinstance(config, Mapping) else None
    dex = meta.get("dex") if isinstance(meta, Mapping) else None
    if not isinstance(dex, Mapping) or CLAIM_KEY not in dex:
        return test
    rest = {key: value for key, value in dex.items() if key != CLAIM_KEY}
    new_meta = {key: value for key, value in meta.items() if key != "dex"}
    if rest:
        new_meta["dex"] = rest
    return {name: {**body, "config": {**config, "meta": new_meta}}}
