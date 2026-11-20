"""The tests Dex generates from declarations, beyond the three built-ins (#1449).

A declaration already renders dbt's ``unique``, ``not_null`` and
``relationships`` (the built-ins, always on). A **generated test** checks a
declared fact those cannot (a composite grain, a population filter, a parent's
population, ...), each a generic test of Dex's own. Generated tests run in the
customer's production runs, which Dex does not run and whose cost the customer
pays, so two rules hold for every one of them:

- **They are opt-in.** ``declarations.render_tests`` in ``.dex/config.yml``
  turns them on, and is off by default. ``true`` turns on every kind this
  release renders; a list turns on the kinds it names, by test name or by
  cadence, so a project can start with the cheap ones.
- **Each carries a cadence tag**, ``dex_every_run`` or ``dex_scheduled``,
  written as a dbt tag so an orchestrator selects them with ``dbt build
  --select tag:dex_scheduled`` or leaves them out with ``--exclude``. The tag is
  fixed per kind, never chosen per project: a scheduled test is one that scans
  whole tables (an anti-join, a gap search).

This module is the catalog: each kind, its cadence, and which rows of a
declaration it checks. Each generated-test issue (#1452 onward) registers its
kind here with :func:`register`. It imports nothing heavier than the standard
library, because ``config validate`` reads it to check the names a list gives.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

#: The cadence tag of a test cheap enough to run on every production run.
EVERY_RUN = "dex_every_run"
#: The cadence tag of a test that scans whole tables, run on its own schedule.
SCHEDULED = "dex_scheduled"
#: Every cadence tag, in the order the docs give them.
CADENCES: tuple[str, ...] = (EVERY_RUN, SCHEDULED)

#: The setting key, named in messages so a reader can find it.
SETTING = "declarations.render_tests"

#: The tests a declaration renders whatever the switch says. Never tagged.
BUILT_INS: frozenset[str] = frozenset({"unique", "not_null", "relationships"})


@dataclass(frozen=True)
class Instance:
    """One test a kind renders for one declaration.

    ``column`` is the column the test sits under, or ``None`` for a model-level
    test (``data_tests`` on the model entry). ``field``, ``columns`` and
    ``params`` are the claim it checks (``transform.claims.claim_id``), so two
    instances of one kind on one model (two population filters) get two claims.
    ``arguments`` are the generic test's arguments, written under
    ``arguments:`` as dbt 1.10 and later read them.
    """

    column: str | None
    field: str
    columns: tuple[str, ...]
    params: Mapping[str, Any] = field(default_factory=dict)
    arguments: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class GeneratedTestKind:
    """A generated test: its name (the generic test's), its cadence, and the
    instances a declaration gives it."""

    name: str
    cadence: str
    #: The instances one declaration gives this kind; empty where it declares
    #: nothing this kind checks.
    instances: Callable[[Any], Iterable[Instance]]


_KINDS: dict[str, GeneratedTestKind] = {}


def register(kind: GeneratedTestKind) -> GeneratedTestKind:
    """Add ``kind`` to the catalog. Refuses a second kind of one name, a name
    that is a cadence or a built-in, and a cadence that is not one of
    :data:`CADENCES`."""

    if kind.cadence not in CADENCES:
        raise ValueError(
            f"generated test '{kind.name}' has cadence '{kind.cadence}'; a cadence "
            f"is one of {', '.join(CADENCES)}"
        )
    if kind.name in CADENCES or kind.name in BUILT_INS:
        raise ValueError(f"'{kind.name}' cannot name a generated test")
    if kind.name in _KINDS:
        raise ValueError(f"generated test '{kind.name}' is already registered")
    _KINDS[kind.name] = kind
    return kind


def unregister(name: str) -> None:
    """Take ``name`` out of the catalog (a test's fixture kind)."""

    _KINDS.pop(name, None)


def kinds() -> tuple[GeneratedTestKind, ...]:
    """Every kind this release renders, in the order they were registered."""

    return tuple(_KINDS.values())


def unknown_entries(setting: Any) -> list[str]:
    """The entries of a ``render_tests`` list that name neither a kind this
    release renders nor a cadence. They turn nothing on; the config reports them
    without refusing, so a file written for a later release still loads."""

    if not isinstance(setting, list):
        return []
    return [
        entry
        for entry in setting
        if isinstance(entry, str) and entry not in _KINDS and entry not in CADENCES
    ]


def enabled(kind: GeneratedTestKind, setting: bool | list[str]) -> bool:
    """Whether ``setting`` turns ``kind`` on: ``true`` turns on every kind, and
    a list the kinds it names and the kinds of each cadence it names."""

    if isinstance(setting, bool):
        return setting
    return kind.name in setting or kind.cadence in setting


def off_reason(setting: bool | list[str]) -> str:
    """Why a kind ``setting`` leaves off is off: the switch is off, or the list
    does not name it."""

    return "render_tests_off" if setting is False else "kind_not_enabled"


def is_generated(test: Any) -> bool:
    """Whether a parsed test entry carries a cadence tag, the mark of a test
    Dex generated (a built-in never carries one)."""

    if not isinstance(test, Mapping) or len(test) != 1:
        return False
    body = next(iter(test.values()))
    if not isinstance(body, Mapping):
        return False
    tags: Any = None
    config = body.get("config")
    if isinstance(config, Mapping):
        tags = config.get("tags")
    if tags is None:
        tags = body.get("tags")
    if isinstance(tags, str):
        tags = [tags]
    return isinstance(tags, list) and any(tag in CADENCES for tag in tags)
