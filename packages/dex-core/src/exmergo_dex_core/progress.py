"""Periodic stderr progress for the long explore loops.

The engine's contract is one sanitized JSON envelope on stdout and nothing else;
stderr is reserved for logs and diagnostics. A long profiling run (many objects,
or ``--verify`` adding a probe per inferred join) otherwise produces no output at
all until it finishes, so a live-but-slow run is indistinguishable from a hung
one. :class:`ProgressReporter` fills that gap: it emits a minimal
``dex: <label> <done>/<total> <noun>`` line to **stderr** as the loop advances,
gated so fast runs stay completely silent and the stdout envelope is never
touched.

The line carries only the label, noun, and counts, never an object identifier or
column name, so nothing sensitive reaches stderr.

Known limitation: progress proves "N items done," not per-second liveness. A
stall *inside* one item (a single blocking ``column_aggregates`` call) still
shows no line until that call returns, because there is no background timer
thread. Adding one would introduce concurrency and interleaved stderr writes into
an otherwise single-threaded engine; a background heartbeat is a possible future
enhancement, not this change.
"""

from __future__ import annotations

import sys
import time
from collections.abc import Callable
from typing import TextIO

# Seconds a run must exceed before the first progress line fires, and the minimum
# gap between subsequent lines. Public (no underscore) so tests import them.
PROGRESS_FIRST_AFTER = 5.0
PROGRESS_INTERVAL = 5.0


class ProgressReporter:
    """Counter/timing state for one long loop; emits throttled stderr lines.

    Construct with the total item count, a ``label`` (e.g. ``"profiled"`` /
    ``"verified"``), and a ``noun`` (``"objects"`` / ``"joins"``). The loop calls
    :meth:`advance` per item and :meth:`done` once at the end; all threshold,
    stream, and clock decisions live here so the loop stays ignorant of them.

    ``stream`` must never default to stdout: writing there would corrupt the JSON
    envelope. It defaults to ``None``, meaning "whatever ``sys.stderr`` is when a
    line is actually written", which is not the same thing as a ``sys.stderr``
    default argument: that captures the stream this module saw at import and
    holds it for the life of the process (see :meth:`_emit`). The ``clock`` seam
    mirrors the adapters' injected ``time.monotonic`` so tests can drive timing
    deterministically.
    """

    def __init__(
        self,
        total: int,
        label: str,
        noun: str,
        *,
        stream: TextIO | None = None,
        clock: Callable[[], float] = time.monotonic,
        first_after: float = PROGRESS_FIRST_AFTER,
        interval: float = PROGRESS_INTERVAL,
    ) -> None:
        self.total = total
        self.label = label
        self.noun = noun
        self._stream = stream
        self._clock = clock
        self._first_after = first_after
        self._interval = interval
        self._done = 0
        self._start = clock()
        self._last: float | None = None  # monotonic time of the last emitted line

    def advance(self, n: int = 1) -> None:
        """Record ``n`` more items done and emit a line if the gates allow.

        A line fires only once the run has been going longer than
        ``first_after`` and either no line has fired yet or ``interval`` has
        elapsed since the last one, so a fast run never prints and a slow run is
        throttled to roughly one line per ``interval``.

        The final item is never announced here: an in-progress line means "still
        working, more to come," which the last item isn't, and :meth:`done`
        already emits the clean ``<total>/<total>`` completion line. This is also
        what keeps a ``total`` of 0 or 1 silent by construction (a 1-item run's
        only advance is its last).
        """

        self._done += n
        if self._done >= self.total:
            return
        now = self._clock()
        if now - self._start <= self._first_after:
            return
        if self._last is not None and now - self._last < self._interval:
            return
        self._last = now
        self._emit(self._done)

    def done(self) -> None:
        """Emit a final ``<total>/<total>`` line, but only if a line already fired.

        A run that never crossed ``first_after`` stays silent; a run that did
        gets one clean completion line so its last visible count is the total,
        confirming it finished rather than dying mid-phase.
        """

        if self._last is None:
            return
        self._emit(self.total)

    def _emit(self, done: int) -> None:
        """Write one line to the *current* stderr, and never fail the caller.

        Resolving the stream here rather than binding it as a default argument
        is the whole point. A ``stream: TextIO = sys.stderr`` default is
        evaluated once, when this module is first imported, so the reporter
        writes to that object forever after: under pytest, the capture buffer
        live at collection time, which is closed by the time any run is slow
        enough to cross ``first_after`` (`ValueError: I/O operation on closed
        file`), and for an embedder that reassigns ``sys.stderr`` or wraps a
        call in ``contextlib.redirect_stderr``, the wrong destination entirely.

        The write is also best-effort. This line narrates work that has already
        happened and, on a metered connector, already billed; a diagnostic the
        caller may not even be reading must not be what turns a finished
        profiling run into an error envelope.
        """

        stream = sys.stderr if self._stream is None else self._stream
        try:
            stream.write(f"dex: {self.label} {done}/{self.total} {self.noun}\n")
            stream.flush()
        except (OSError, ValueError, AttributeError):
            return
