"""The aggregates a file operation returns, and nothing else.

These are the only types that cross from a file source into the command layer,
and they are built so that they *cannot* carry document content, rather than so
that they are merely not given any. Three properties do that, and
``tests/files/test_file_results.py`` walks every model to hold them:

- **Every leaf is a number, a fixed category, a timestamp, or a relation name.**
  Counts are strict non-negative integers (a string of digits is refused, not
  coerced), categories are the enums in :mod:`.contract`, and the one string
  type, :data:`RelationName`, refuses anything shaped like a path or a URL. There
  is no free-text field anywhere, so there is nowhere for an excerpt, a file
  name, or a provider's error body to go.
- **Unknown keys are refused.** Every model forbids extra fields, so a ``uri`` or
  a ``text`` cannot ride along on an otherwise valid aggregate.
- **Absent and zero never share a spelling.** No field admits ``None``. An
  observed zero is ``0``; a measurement the evidence does not support is an
  :class:`Unavailable` carrying the reason. A caller can always tell "no file had
  a table" from "tables were never counted".

The models also refuse internally inconsistent reports: the coverage groups must
partition the sample, so files with no result can never drop out of the
denominator, and the mandatory limitations must be present, so an aggregate can
never be read as saying more than it measured.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    Strict,
    StringConstraints,
    computed_field,
    model_validator,
)

from .contract import (
    DIAGNOSTIC_BIN_EDGES,
    SAMPLE_CEILING,
    CollectionKind,
    Diagnostic,
    DocumentFamily,
    Limitation,
    MetadataField,
    ResultFormatName,
    SamplingMethod,
    UnavailableReason,
)

__all__ = [
    "RELATION_NAME_PATTERN",
    "Bin",
    "CollectionInventory",
    "CollectionScope",
    "CollectionSummary",
    "Count",
    "Coverage",
    "Currency",
    "DiagnosticDistributions",
    "Distribution",
    "FileAggregate",
    "FileProfile",
    "FormatCounts",
    "Instant",
    "NonNegative",
    "Processing",
    "Ratio",
    "RelationName",
    "StatusCounts",
    "Unavailable",
]


NonNegative = Annotated[int, Strict(), Field(ge=0)]

# Dotted identifier parts: letters, digits, underscore, `$`, and the hyphen a
# BigQuery project id carries. What it exists to refuse is everything a path or a
# URL needs (`/`, `:`, `?`, `=`, `&`, `%`, whitespace, quotes), so a `gs://` URI or
# a signed URL can never be stored as a collection name. It cannot tell a bare
# file name like `scan.pdf` from a two-part relation, and does not try: the
# guarantee against file names is that no field is meant to hold one, and this
# pattern is the second line behind it.
RELATION_NAME_PATTERN = r"^[\w$-]+(?:\.[\w$-]+)*$"
RelationName = Annotated[
    str, StringConstraints(pattern=RELATION_NAME_PATTERN, max_length=1024)
]


class FileAggregate(BaseModel):
    """Base for every file aggregate: immutable, and closed to unknown keys."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    def payload(self) -> dict[str, Any]:
        """The JSON shape this aggregate reports as."""

        return self.model_dump(mode="json")


class Unavailable(FileAggregate):
    """A measurement with no observed value, and why."""

    reason: UnavailableReason


Count = NonNegative | Unavailable
Instant = AwareDatetime | Unavailable


def _limitations_present(
    limitations: tuple[Limitation, ...], required: frozenset[Limitation], what: str
) -> None:
    if len(set(limitations)) != len(limitations):
        raise ValueError(f"{what} names a limitation twice")
    missing = sorted(lim.value for lim in required - set(limitations))
    if missing:
        raise ValueError(f"{what} must state its limitations: {', '.join(missing)}")


class Ratio(FileAggregate):
    """A rate that always carries what it is a rate of."""

    numerator: NonNegative
    denominator: NonNegative

    @model_validator(mode="before")
    @classmethod
    def _accept_own_fraction(cls, data: Any) -> Any:
        """Read back a ratio this model serialized, which carries ``fraction``.

        The fraction is derived, so a stored one is accepted only when it is the
        value the two counts produce; anything else is refused rather than
        silently recomputed, because a disagreeing fraction means the record was
        edited.
        """

        if not isinstance(data, dict) or "fraction" not in data:
            return data
        data = dict(data)
        stored = data.pop("fraction")
        numerator, denominator = data.get("numerator"), data.get("denominator")
        if isinstance(numerator, int) and isinstance(denominator, int):
            derived = numerator / denominator if denominator else None
            if stored != derived:
                raise ValueError(
                    f"a stored fraction of {stored} does not follow from "
                    f"{numerator} over {denominator}"
                )
        return data

    @model_validator(mode="after")
    def _within_denominator(self) -> Ratio:
        if self.numerator > self.denominator:
            raise ValueError(
                f"a ratio's numerator ({self.numerator}) cannot exceed its "
                f"denominator ({self.denominator})"
            )
        return self

    @computed_field  # type: ignore[prop-decorator]
    @property
    def fraction(self) -> float | None:
        """``None`` only over an empty denominator, where no rate exists."""

        return self.numerator / self.denominator if self.denominator else None


class FormatCounts(FileAggregate):
    """Files per format bucket. Every file lands in exactly one, so the buckets
    sum to the file count, and a file whose metadata carries no content type is
    counted under ``unknown`` rather than dropped."""

    pdf: NonNegative
    jpeg: NonNegative
    png: NonNegative
    tiff: NonNegative
    other: NonNegative
    unknown: NonNegative

    @property
    def total(self) -> int:
        return self.pdf + self.jpeg + self.png + self.tiff + self.other + self.unknown


class CollectionSummary(FileAggregate):
    """One collection as discovery sees it, from catalog metadata only.

    ``file_count`` is usually unavailable here, because discovery never scans a
    collection to count it.
    """

    collection: RelationName
    kind: CollectionKind
    file_count: Count
    metadata_refreshed_at: Instant


_INVENTORY_LIMITATIONS = frozenset(
    {
        Limitation.FORMAT_IS_REPORTED_METADATA,
        Limitation.COLLECTION_IS_REGISTERED_INDEX,
    }
)


class CollectionInventory(FileAggregate):
    """One collection's metadata, aggregated inside the warehouse.

    ``file_count`` is always observed: an inventory is a count. Everything else
    depends on what the collection's metadata carries, so each is a
    :data:`Count` or an :data:`Instant`. ``missing_size`` counts files whose size
    is unknown, and ``known_bytes`` sums only the rest, which is why the two are
    reported side by side.
    """

    collection: RelationName
    kind: CollectionKind
    observed_at: AwareDatetime
    file_count: NonNegative
    known_bytes: Count
    missing_size: Count
    zero_byte: Count
    formats: FormatCounts
    earliest_update: Instant
    latest_update: Instant
    metadata_refreshed_at: Instant
    limitations: tuple[Limitation, ...]

    @model_validator(mode="after")
    def _consistent(self) -> CollectionInventory:
        _limitations_present(
            self.limitations, _INVENTORY_LIMITATIONS, "a collection inventory"
        )
        if self.formats.total != self.file_count:
            raise ValueError(
                f"format buckets sum to {self.formats.total}, not the "
                f"{self.file_count} files counted"
            )
        missing = self.missing_size if isinstance(self.missing_size, int) else 0
        if missing > self.file_count:
            raise ValueError("more files are missing a size than were counted")
        if (
            isinstance(self.zero_byte, int)
            and self.zero_byte > self.file_count - missing
        ):
            raise ValueError("more files are zero bytes than have a known size")
        if (
            isinstance(self.earliest_update, datetime)
            and isinstance(self.latest_update, datetime)
            and self.earliest_update > self.latest_update
        ):
            raise ValueError("the earliest update is later than the latest one")
        return self


class Bin(FileAggregate):
    """Files whose value is at least ``at_least`` and below the next bin's edge.

    The last bin of a distribution is open-ended.
    """

    at_least: NonNegative
    files: NonNegative


class Distribution(FileAggregate):
    """One diagnostic over the assessed files.

    ``assessed`` is partitioned three ways: a usable value, no value at all
    (``files_absent``), or a value that cannot be one (``files_invalid``: a
    negative page count, a type the format does not declare). The first bin is
    the observed zeros, so zero and absent stay apart here too.
    """

    assessed: NonNegative
    files_with_value: NonNegative
    files_absent: NonNegative
    files_invalid: NonNegative
    minimum: Count
    maximum: Count
    bins: tuple[Bin, ...]

    @model_validator(mode="after")
    def _consistent(self) -> Distribution:
        total = self.files_with_value + self.files_absent + self.files_invalid
        if total != self.assessed:
            raise ValueError(
                f"with-value, absent, and invalid files sum to {total}, not the "
                f"{self.assessed} assessed"
            )
        edges = [b.at_least for b in self.bins]
        if not edges or edges[0] != 0 or edges != sorted(set(edges)):
            raise ValueError("bins must start at 0 and rise strictly")
        if sum(b.files for b in self.bins) != self.files_with_value:
            raise ValueError("bins must hold exactly the files with a value")
        if self.files_with_value == 0:
            if isinstance(self.minimum, int) or isinstance(self.maximum, int):
                raise ValueError(
                    "no file had a value, so there is no minimum or maximum"
                )
        elif not (isinstance(self.minimum, int) and isinstance(self.maximum, int)):
            raise ValueError(
                "files had values, so the minimum and maximum are observed"
            )
        elif self.minimum > self.maximum:
            raise ValueError("the minimum exceeds the maximum")
        return self


class DiagnosticDistributions(FileAggregate):
    """One entry per :class:`~.contract.Diagnostic`, each a distribution or the
    reason there is none."""

    text_characters: Distribution | Unavailable
    reported_pages: Distribution | Unavailable
    represented_pages: Distribution | Unavailable
    tables: Distribution | Unavailable
    form_fields: Distribution | Unavailable
    paragraphs: Distribution | Unavailable

    def by_diagnostic(self) -> dict[Diagnostic, Distribution | Unavailable]:
        return {d: getattr(self, d.value) for d in Diagnostic}

    @model_validator(mode="after")
    def _fixed_bins(self) -> DiagnosticDistributions:
        for diagnostic, entry in self.by_diagnostic().items():
            if not isinstance(entry, Distribution):
                continue
            edges = tuple(b.at_least for b in entry.bins)
            if edges != DIAGNOSTIC_BIN_EDGES[diagnostic]:
                raise ValueError(
                    f"{diagnostic.value} must use its fixed bin edges "
                    f"{DIAGNOSTIC_BIN_EDGES[diagnostic]}"
                )
        return self


class StatusCounts(FileAggregate):
    """Matched files per processing status; anything else a provider stored is
    counted under ``unknown`` and never returned."""

    success: NonNegative
    failure: NonNegative
    partial: NonNegative
    unknown: NonNegative

    @property
    def total(self) -> int:
        return self.success + self.failure + self.partial + self.unknown


class CollectionScope(FileAggregate):
    """What was assessed: which collection, against which results, for which
    families, with which metadata available."""

    collection: RelationName
    kind: CollectionKind
    result_table: RelationName
    result_format: ResultFormatName
    families: tuple[DocumentFamily, ...] = Field(min_length=1)
    metadata_fields: tuple[MetadataField, ...]


class Coverage(FileAggregate):
    """How much of the sample has a result at all.

    ``eligible`` is the collection's files in the selected formats and
    ``sampled`` the ones chosen from them. The sample splits three ways, and the
    three always add back up to it: one resolvable result (``matched``), no
    result (``missing_result``), or duplicates that could not be resolved to one
    (``ambiguous``), which are excluded from every content distribution rather
    than arbitrarily picked.
    """

    requested: Annotated[int, Strict(), Field(ge=1, le=SAMPLE_CEILING)]
    eligible: NonNegative
    sampled: NonNegative
    sampling_method: SamplingMethod
    matched: Ratio
    missing_result: Ratio
    ambiguous: Ratio

    @model_validator(mode="after")
    def _partitions_the_sample(self) -> Coverage:
        if self.sampled != min(self.requested, self.eligible):
            raise ValueError(
                "a deterministic sample takes every eligible file up to the "
                "requested size, so sampled must equal the smaller of the two"
            )
        parts = (self.matched, self.missing_result, self.ambiguous)
        if any(p.denominator != self.sampled for p in parts):
            raise ValueError("every coverage rate is over the sampled files")
        if sum(p.numerator for p in parts) != self.sampled:
            raise ValueError(
                "matched, missing, and ambiguous files must add up to the sample; "
                "a file with no result stays in the denominator"
            )
        return self


class Currency(FileAggregate):
    """Whether each matched result describes the file as it is now.

    Compared only where both sides carry compatible version evidence; everything
    else is ``version_unknown``, never current.
    """

    version_matched: Ratio
    version_mismatched: Ratio
    version_unknown: Ratio

    @model_validator(mode="after")
    def _partitions_matched(self) -> Currency:
        parts = (self.version_matched, self.version_mismatched, self.version_unknown)
        denominators = {p.denominator for p in parts}
        if len(denominators) != 1:
            raise ValueError("every currency rate is over the same matched files")
        if sum(p.numerator for p in parts) != denominators.pop():
            raise ValueError("matched, mismatched, and unknown versions must add up")
        return self


class Processing(FileAggregate):
    """What the matched results report about their own processing.

    ``payload_valid`` is whether a stored payload has the shape its format
    declares, which is separate from coverage: a file can have a result whose
    payload is unusable.
    """

    statuses: StatusCounts
    payload_valid: Ratio | Unavailable
    partial_page_files: Count
    diagnostics: DiagnosticDistributions


_PROFILE_LIMITATIONS = frozenset(
    {
        Limitation.DOCUMENT_PII_NOT_SCREENED,
        Limitation.NATIVE_PROCESSING_UNAVAILABLE,
        Limitation.SAMPLING_NOT_REPRESENTATIVE,
    }
)


class FileProfile(FileAggregate):
    """An assessment of a collection's existing processing results.

    Five groups: what was assessed, how much of it has results, whether those
    results are current, what they report, and what the assessment does not
    establish. There is deliberately no overall readiness score: successful
    parsing, long text, or detected tables do not make an extraction correct.
    """

    collection: CollectionScope
    observed_at: AwareDatetime
    coverage: Coverage
    currency: Currency
    processing: Processing
    limitations: tuple[Limitation, ...]

    @model_validator(mode="after")
    def _consistent(self) -> FileProfile:
        _limitations_present(self.limitations, _PROFILE_LIMITATIONS, "a file profile")
        matched = self.coverage.matched.numerator
        if self.currency.version_matched.denominator != matched:
            raise ValueError("currency is measured over the matched files")
        if self.processing.statuses.total != matched:
            raise ValueError("processing statuses are counted over the matched files")
        valid = self.processing.payload_valid
        if isinstance(valid, Ratio) and valid.denominator != matched:
            raise ValueError("payload validity is measured over the matched files")
        partial = self.processing.partial_page_files
        if isinstance(partial, int) and partial > matched:
            raise ValueError("more files are partially represented than matched")
        for diagnostic, entry in self.processing.diagnostics.by_diagnostic().items():
            if isinstance(entry, Distribution) and entry.assessed != matched:
                raise ValueError(
                    f"{diagnostic.value} is distributed over the matched files, "
                    "which excludes ambiguous duplicates"
                )
        return self
