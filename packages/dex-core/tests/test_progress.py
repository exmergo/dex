"""ProgressReporter: throttled stderr progress, silent on fast runs.

Timing is driven deterministically through the injected ``clock`` seam and output
is captured in an ``io.StringIO`` stream, so these tests never sleep and never
touch real stderr.
"""

from __future__ import annotations

import io
import sys

from exmergo_dex_core.progress import (
    PROGRESS_FIRST_AFTER,
    PROGRESS_INTERVAL,
    ProgressReporter,
)


class _Clock:
    """A hand-cranked monotonic clock: ``tick(dt)`` advances the returned time."""

    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def tick(self, dt: float) -> None:
        self.now += dt


def _reporter(total: int, clock: _Clock, stream: io.StringIO) -> ProgressReporter:
    return ProgressReporter(total, "profiled", "objects", stream=stream, clock=clock)


def test_fast_run_stays_silent() -> None:
    clock, stream = _Clock(), io.StringIO()
    reporter = _reporter(90, clock, stream)
    for _ in range(90):
        clock.tick(0.01)  # whole run finishes well under first_after
        reporter.advance()
    reporter.done()
    assert stream.getvalue() == ""


def test_first_line_only_after_threshold_then_throttled() -> None:
    clock, stream = _Clock(), io.StringIO()
    reporter = _reporter(90, clock, stream)

    # Advances before the threshold emit nothing.
    reporter.advance()
    clock.tick(PROGRESS_FIRST_AFTER)  # exactly at threshold: still silent (> only)
    reporter.advance()
    assert stream.getvalue() == ""

    # Cross the threshold: the next advance fires the first line.
    clock.tick(0.1)
    reporter.advance()
    first = stream.getvalue()
    assert first.count("\n") == 1

    # A further advance inside the interval is throttled (no new line).
    clock.tick(PROGRESS_INTERVAL - 0.1)
    reporter.advance()
    assert stream.getvalue() == first

    # Once the interval elapses, the next advance emits again.
    clock.tick(0.1)
    reporter.advance()
    assert stream.getvalue().count("\n") == 2


def test_line_format() -> None:
    clock, stream = _Clock(), io.StringIO()
    reporter = _reporter(90, clock, stream)
    # 39 advances under the threshold stay silent; the 40th, past it, is the
    # first (and only) line, pinning the exact format.
    for _ in range(39):
        reporter.advance()
    clock.tick(PROGRESS_FIRST_AFTER + 0.1)
    reporter.advance()
    assert stream.getvalue() == "dex: profiled 40/90 objects\n"


def test_done_emits_total_when_a_line_already_fired() -> None:
    clock, stream = _Clock(), io.StringIO()
    reporter = _reporter(90, clock, stream)
    clock.tick(PROGRESS_FIRST_AFTER + 0.1)
    reporter.advance()
    reporter.done()
    assert stream.getvalue().endswith("dex: profiled 90/90 objects\n")


def test_done_silent_when_run_was_silent() -> None:
    clock, stream = _Clock(), io.StringIO()
    reporter = _reporter(90, clock, stream)
    reporter.advance()  # under the threshold, nothing fired
    reporter.done()
    assert stream.getvalue() == ""


def test_total_zero_and_one_stay_silent() -> None:
    for total in (0, 1):
        clock, stream = _Clock(), io.StringIO()
        reporter = _reporter(total, clock, stream)
        # Even past the threshold, a trivial run should never print: 0 has no
        # items, and 1 finishes in a single (throttled) advance below threshold.
        clock.tick(PROGRESS_FIRST_AFTER + 0.1)
        for _ in range(total):
            reporter.advance()
        reporter.done()
        assert stream.getvalue() == ""


def test_stream_defaults_to_the_current_stderr_never_stdout(monkeypatch) -> None:
    # The default stream must be stderr: writing progress to stdout would corrupt
    # the single-JSON-envelope contract. It must also be *the stderr in force
    # when the line is written*, not the one this module saw at import: a
    # `stream: TextIO = sys.stderr` default argument is evaluated once per
    # process, and under pytest that object is a capture buffer which is closed
    # long before any run is slow enough to emit, so a live profiling run died
    # on `ValueError: I/O operation on closed file` after it had already billed.
    clock, stream = _Clock(), io.StringIO()
    reporter = ProgressReporter(90, "profiled", "objects", clock=clock)

    monkeypatch.setattr(sys, "stderr", stream)
    clock.tick(PROGRESS_FIRST_AFTER + 0.1)
    reporter.advance()

    assert stream.getvalue() == "dex: profiled 1/90 objects\n"


def test_a_dead_stream_never_fails_the_run() -> None:
    # Progress narrates work that already happened and, on a metered connector,
    # already billed. A stream that has gone away must not be what turns a
    # finished run into an error.
    clock, stream = _Clock(), io.StringIO()
    reporter = ProgressReporter(90, "profiled", "objects", stream=stream, clock=clock)
    stream.close()

    clock.tick(PROGRESS_FIRST_AFTER + 0.1)
    reporter.advance()  # must not raise
    reporter.done()
