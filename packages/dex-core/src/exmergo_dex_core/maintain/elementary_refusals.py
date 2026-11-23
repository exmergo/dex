"""The Elementary incident reader's refusal and its codes (#1471).

Apart from :mod:`.elementary_incidents`, which renders SQL with sqlglot and so
needs the ``sql`` extra: a caller catching the refusal, or the package's own
export of it, imports nothing a core-only install lacks.
"""

from __future__ import annotations

from collections.abc import Mapping

from ..errors import DexError

READ_TOO_LARGE = "elementary_read_too_large"
NO_INVOCATIONS = "elementary_no_invocations"
INVOCATION_UNREADABLE = "elementary_invocation_unreadable"
INVOCATION_REPEATED = "elementary_invocation_repeated"
RELEASE_MIXED = "elementary_rows_untested_release"
STATUS_UNKNOWN = "elementary_status_unknown"
RESULT_CONFLICT = "elementary_result_id_conflict"
ID_TOO_LONG = "elementary_id_too_long"


class ElementaryReadError(DexError):
    """The reader refused what the tables hold. ``code`` says why, ``counts``
    the rows or runs it applies to, and the message quotes no value from a row
    but a status or version token."""

    def __init__(
        self, code: str, message: str, counts: Mapping[str, int] | None = None
    ):
        self.code = code
        self.counts = dict(counts or {})
        super().__init__(f"{message} ({code})")
