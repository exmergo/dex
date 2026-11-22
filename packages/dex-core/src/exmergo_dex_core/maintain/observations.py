"""What every test-outcome source hands the incident model and the timeline.

The value types an adapter builds (#1465, #1466): the subject and signal a
check is about, one observed outcome, one run a source holds, and a point in
time a source recorded. :mod:`.incidents` folds them into incidents and
:mod:`.timeline` lays them out as a coverage timeline; both read them from
here, so an adapter builds one set of records for both.

No value crosses: each model declares ids, statuses, a severity token, times
and a commit, and refuses any other field.
"""

from __future__ import annotations

import itertools
import statistics
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

#: The three outcomes an interval of a check's history can have.
Outcome = Literal["good", "bad", "unobserved"]

#: Written where no source holds the answer, never a guess.
UNKNOWN: Literal["unknown"] = "unknown"

#: Why a run a source holds recorded no test outcome at all: Elementary's hooks
#: did not run (``edr_cli_run``, or the package disabled for the run), or they
#: ran with the test-results upload off (``disable_tests_results``).
RunUnobserved = Literal["hooks_off", "uploads_off"]

#: Fewest dated points a schedule is derived from (two gaps).
MIN_SCHEDULE_POINTS = 3

#: A source's name, as the adapters spell it (``dbt_run_results``, ``elementary``).
SOURCE_PATTERN = r"^[a-z][a-z0-9_]{0,63}$"

#: A severity is a source's own token (``warn``, ``ERROR``), never free text: a
#: pattern this narrow cannot carry a sampled value.
SEVERITY_PATTERN = r"^[A-Za-z][A-Za-z_ -]{0,31}$"


class Value(BaseModel):
    """Immutable, and refusing every field it does not declare."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class Subject(Value):
    """What an incident is about: a declared claim, or one test.

    The claim id from ``config.meta.dex.claim`` where Dex rendered the test
    (it survives a rename), otherwise the test's dbt ``unique_id``. The kind is
    part of the identity, so a claim and a ``unique_id`` that happen to be
    spelled alike are two subjects.
    """

    kind: Literal["claim", "unique_id"]
    key: str = Field(min_length=1, max_length=512)

    @classmethod
    def of(cls, unique_id: str, claim: str | None = None) -> Subject:
        """The subject of a test: its claim where it carries one."""

        if claim:
            return cls(kind="claim", key=claim)
        return cls(kind="unique_id", key=unique_id)


class Signal(Value):
    """What failed, beyond the subject: the column and metric of a test that
    writes one row per column (an Elementary anomaly test), else nothing.

    Never the test's name: a renamed test checking the same claim is the same
    signal, so it stays one incident.
    """

    column: str | None = Field(default=None, max_length=512)
    metric: str | None = Field(default=None, max_length=512)


class Observation(Value):
    """One source's outcome for one subject and signal in one run.

    ``sequence`` is the run's position in invocation order, which the caller
    supplies: sources stamp times with different clocks, so the order is the
    invocations', and ``at`` is carried for the schedule and for reporting.
    Two sources observing the same invocation give it the same ``sequence``.
    """

    subject: Subject
    signal: Signal = Field(default_factory=Signal)
    #: The test's dbt ``unique_id`` in this run, which a rename changes.
    test: str = Field(min_length=1, max_length=512)
    outcome: Outcome
    source: str = Field(pattern=SOURCE_PATTERN)
    run: str = Field(min_length=1, max_length=128)
    sequence: int
    at: datetime | None = None
    commit: str | None = Field(default=None, max_length=64)
    #: In the source's own words, never mapped onto a Dex scale.
    severity: str | None = Field(default=None, pattern=SEVERITY_PATTERN)


class RunRef(Value):
    """One run a source holds, whether or not it observed a given subject.

    ``commit`` is the commit the run's invocation records (``git_sha``), or
    ``None`` where it records none. ``unobserved`` says the run happened and
    recorded no test outcome, and why; a run whose source recorded outcomes
    leaves it ``None``.
    """

    run: str = Field(min_length=1, max_length=128)
    sequence: int
    at: datetime | None = None
    commit: str | None = Field(default=None, max_length=64)
    unobserved: RunUnobserved | None = None


class ObservedPoint(Value):
    """A run and its time, as one source recorded them."""

    run: str
    at: datetime | None = None
    source: str


def utc(moment: datetime) -> datetime:
    """The instant in UTC; a time with no zone is read as UTC, as dbt writes
    ``generated_at``. An aware time is converted, so two wall-clock times a
    DST change repeats stay two instants."""

    if moment.tzinfo is None:
        return moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)


def median_gap(times: Iterable[datetime | None]) -> tuple[float | None, int]:
    """The median gap in seconds between distinct dated points, and how many
    dated points there were; ``None`` with fewer than three."""

    dated = sorted({utc(at) for at in times if at is not None})
    if len(dated) < MIN_SCHEDULE_POINTS:
        return None, len(dated)
    gaps = [
        (later - earlier).total_seconds()
        for earlier, later in itertools.pairwise(dated)
    ]
    return float(statistics.median(gaps)), len(dated)
