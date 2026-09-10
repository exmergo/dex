"""The file-exploration contract: vocabulary, interfaces, requests, capabilities.

**Optional, and beside the warehouse adapter rather than inside it.** The
:class:`~..adapters.base.Adapter` protocol models tables and columns, and every
connector implements it. File exploration is something only some connectors can
do with a bounded, warehouse-side path to file metadata, so it lives in its own
protocols here. A connector that has nothing to say about files implements
nothing, and :func:`file_capabilities` reports that as a named limitation.
Widening ``Adapter`` instead would have demoted every host-supplied adapter that
had not grown the new members, and made every connector carry placeholder
document operations.

**Tiers, checked structurally, never declared by a flag**, the same idiom as the
project seam in :mod:`..adapters.project`::

    FileCollectionSource   file_collection_inventory()   -- metadata aggregation
    DiscoveringFileSource  list_file_collections()       -- beside it, optional
    FileResultSource         + source_sample_sql()        -- result assessment
                             + run_file_aggregate()
                             + quote_identifier()

``isinstance(adapter, FileResultSource)`` is either true or it is not, so a
connector cannot claim a capability it does not implement. Discovery sits beside
the first tier rather than inside it because a source can aggregate a collection
it was explicitly handed (a bound manifest table) without being able to list
collections at all.

**Every category is a fixed vocabulary.** Formats, statuses, limitations, and
unavailability reasons are enums, and anything a provider reports outside them is
counted under ``unknown`` or ``other`` rather than passed through. A category
that echoed provider strings would be a channel for whatever the provider put in
them, and a document parser's status field is where extracted text and error
bodies end up.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, computed_field, model_validator

from ..errors import RequestError

if TYPE_CHECKING:
    from .results import CollectionInventory, CollectionSummary, Unavailable

__all__ = [
    "CONTENT_TYPE_FORMATS",
    "DIAGNOSTIC_BIN_EDGES",
    "FAMILY_FORMATS",
    "LIMITATION_TEXT",
    "RESULT_FORMATS",
    "SAMPLE_CEILING",
    "SAMPLE_DEFAULT",
    "SHORTLIST_DEFAULT",
    "Capability",
    "CapabilityLimitation",
    "CollectionKind",
    "Diagnostic",
    "DiscoveringFileSource",
    "DocumentFamily",
    "FileCapabilities",
    "FileCollectionSource",
    "FileFormat",
    "FileResultSource",
    "InventoryRequest",
    "Limitation",
    "MeasureKind",
    "MetadataField",
    "NativeProcessing",
    "ProcessingStatus",
    "ProfileRequest",
    "ResultBinding",
    "ResultFormat",
    "ResultFormatName",
    "RowDiagnostics",
    "SamplingMethod",
    "UnavailableReason",
    "file_capabilities",
    "format_for_content_type",
]


# --- Vocabulary --------------------------------------------------------------


class DocumentFamily(str, Enum):
    """A document family an assessment can be narrowed to.

    ``scanned_image`` denotes a document-image input (see
    :data:`FAMILY_FORMATS`). It does not assert that dex verified the image
    depicts a scanned document: nothing here looks at an image.
    """

    PDF = "pdf"
    SCANNED_IMAGE = "scanned_image"


class FileFormat(str, Enum):
    """The fixed format buckets a collection's files are counted into.

    A bucket is *reported metadata*: the content type the storage layer
    records, never an inspection of the bytes, and never a file-name extension.
    ``other`` is a well-formed content type outside the supported families;
    ``unknown`` is a missing, malformed, or explicitly unknown one (see
    :func:`format_for_content_type`).
    """

    PDF = "pdf"
    JPEG = "jpeg"
    PNG = "png"
    TIFF = "tiff"
    OTHER = "other"
    UNKNOWN = "unknown"


FAMILY_FORMATS: Mapping[DocumentFamily, frozenset[FileFormat]] = MappingProxyType(
    {
        DocumentFamily.PDF: frozenset({FileFormat.PDF}),
        DocumentFamily.SCANNED_IMAGE: frozenset(
            {FileFormat.JPEG, FileFormat.PNG, FileFormat.TIFF}
        ),
    }
)

# The one table every connector's bucketing SQL is generated from, so a file
# lands in the same bucket whichever warehouse indexed it. IANA-registered
# essences only: a common misspelling like `image/jpg` is not what a standard
# upload tool records, and guessing at aliases is how two connectors come to
# disagree. Lookup is on the essence, lower-cased, with parameters stripped.
CONTENT_TYPE_FORMATS: Mapping[str, FileFormat] = MappingProxyType(
    {
        "application/pdf": FileFormat.PDF,
        "image/jpeg": FileFormat.JPEG,
        "image/png": FileFormat.PNG,
        "image/tiff": FileFormat.TIFF,
    }
)

# Content types that state the format is not known. S3 and several upload tools
# record these when they had nothing better, so counting them as `other` would
# report a format the storage layer explicitly said it did not know.
_UNKNOWN_CONTENT_TYPES = frozenset({"application/octet-stream", "binary/octet-stream"})

# RFC 6838 type/subtype, restricted-name characters only.
_MIME_ESSENCE = re.compile(r"[a-z0-9][a-z0-9!#$&^_.+-]*/[a-z0-9][a-z0-9!#$&^_.+-]*")


def format_for_content_type(content_type: str | None) -> FileFormat:
    """The bucket a reported content type belongs in.

    This is the reference semantics a connector's warehouse-side bucketing
    expression has to reproduce, and the thing its tests compare against. It is
    not meant to run over file records in Python: bucketing happens inside the
    warehouse, and only the counts per bucket come back.
    """

    if content_type is None:
        return FileFormat.UNKNOWN
    essence = content_type.split(";", 1)[0].strip().lower()
    if not _MIME_ESSENCE.fullmatch(essence) or essence in _UNKNOWN_CONTENT_TYPES:
        return FileFormat.UNKNOWN
    return CONTENT_TYPE_FORMATS.get(essence, FileFormat.OTHER)


class ProcessingStatus(str, Enum):
    """The fixed status vocabulary a stored processing result is counted under.

    A stored value outside it is counted as ``unknown`` and never returned,
    because a status column is where customer strings and provider error bodies
    live.
    """

    SUCCESS = "success"
    FAILURE = "failure"
    PARTIAL = "partial"
    UNKNOWN = "unknown"


class ResultFormatName(str, Enum):
    """The materialized result formats dex knows how to read.

    Three are a provider's documented native output, read through fixed paths;
    ``mapped_columns`` is a pipeline that kept its diagnostics in plain columns
    and discarded the parser payload.
    """

    BIGQUERY_DOCUMENT_AI = "bigquery_document_ai"
    SNOWFLAKE_AI_PARSE_DOCUMENT = "snowflake_ai_parse_document"
    DATABRICKS_AI_PARSE_DOCUMENT = "databricks_ai_parse_document"
    MAPPED_COLUMNS = "mapped_columns"


class Diagnostic(str, Enum):
    """The content diagnostics an assessment reports distributions for.

    Every one is a count computed inside the warehouse from a stored result.
    Named with care: the envelope sanitizer refuses any key containing
    ``token``, so a parser's token count can never be a diagnostic under that
    name.
    """

    TEXT_CHARACTERS = "text_characters"
    REPORTED_PAGES = "reported_pages"
    REPRESENTED_PAGES = "represented_pages"
    TABLES = "tables"
    FORM_FIELDS = "form_fields"
    PARAGRAPHS = "paragraphs"


# Each diagnostic's distribution is reported as fixed bins, each bin holding the
# files whose value is at least its edge and below the next one; the last bin is
# open. Fixed bins rather than percentiles because `SUM(CASE ...)` is the same SQL
# on every dialect and percentile functions are not. The first bin is exactly the
# observed zeros, which is what keeps "reported zero pages" apart from "no page
# count at all".
_COUNT_EDGES = (0, 1, 2, 6, 21, 101)
DIAGNOSTIC_BIN_EDGES: Mapping[Diagnostic, tuple[int, ...]] = MappingProxyType(
    {
        Diagnostic.TEXT_CHARACTERS: (0, 1, 100, 1_000, 10_000, 100_000),
        Diagnostic.REPORTED_PAGES: _COUNT_EDGES,
        Diagnostic.REPRESENTED_PAGES: _COUNT_EDGES,
        Diagnostic.TABLES: _COUNT_EDGES,
        Diagnostic.FORM_FIELDS: _COUNT_EDGES,
        Diagnostic.PARAGRAPHS: _COUNT_EDGES,
    }
)


class CollectionKind(str, Enum):
    """What kind of warehouse resource indexes a collection."""

    BIGQUERY_OBJECT_TABLE = "bigquery_object_table"
    SNOWFLAKE_DIRECTORY_TABLE = "snowflake_directory_table"
    DATABRICKS_VOLUME = "databricks_volume"
    FILE_MANIFEST = "file_manifest"


class MetadataField(str, Enum):
    """The per-file metadata a source can aggregate.

    Deliberately short. A file's path is its identity and is used only inside
    the warehouse to match results; custom metadata key/value pairs and object
    references are customer strings; and an object table's raw-bytes
    pseudocolumn is the document itself. None of those is a field a source
    declares, so none can be asked for.
    """

    SIZE = "size"
    UPDATED = "updated"
    CONTENT_TYPE = "content_type"
    VERSION = "version"


class UnavailableReason(str, Enum):
    """Why an aggregate field has no observed value.

    The reason is what makes an absent measurement distinguishable from a zero
    one: an observed zero is a number, and everything that is not a number
    carries one of these.
    """

    NOT_REPORTED = "not_reported"  # the source's metadata does not carry it
    NOT_BOUND = "not_bound"  # the result binding maps no column for it
    NOT_SUPPORTED_BY_FORMAT = "not_supported_by_format"
    UNRECOGNIZED_SHAPE = "unrecognized_shape"  # payload outside the declared shape
    NO_EVIDENCE = "no_evidence"  # nothing in the assessed set carried a value
    NOT_ASSESSED = "not_assessed"  # outside what this operation measures


class SamplingMethod(str, Enum):
    """How an assessment chose its files.

    ``source_identity_hash`` orders the collection's files by a hash of their
    source identity, breaks ties on the identity itself, and takes the first N,
    all inside the warehouse. Reproducible for unchanged input, and chosen from
    the source collection before any result is matched, so files with no result
    stay in the sample.
    """

    SOURCE_IDENTITY_HASH = "source_identity_hash"


class Limitation(str, Enum):
    """A fixed statement of what an aggregate does not establish.

    Codes rather than prose so that no limitation can quote the data it is
    about; :data:`LIMITATION_TEXT` holds the engine's own wording.
    """

    DOCUMENT_PII_NOT_SCREENED = "document_pii_not_screened"
    NATIVE_PROCESSING_UNAVAILABLE = "native_processing_unavailable"
    SAMPLING_NOT_REPRESENTATIVE = "sampling_not_representative"
    FORMAT_IS_REPORTED_METADATA = "format_is_reported_metadata"
    COLLECTION_IS_REGISTERED_INDEX = "collection_is_registered_index"
    PAGES_PARTIALLY_REPRESENTED = "pages_partially_represented"
    VERSION_EVIDENCE_MISSING = "version_evidence_missing"
    TIMESTAMP_NOT_VERSION_PROOF = "timestamp_not_version_proof"


LIMITATION_TEXT: Mapping[Limitation, str] = MappingProxyType(
    {
        Limitation.DOCUMENT_PII_NOT_SCREENED: (
            "document content was not screened for personal data, so the absence "
            "of a finding says nothing about what the files contain"
        ),
        Limitation.NATIVE_PROCESSING_UNAVAILABLE: (
            "dex invoked no document processing; every figure describes results "
            "that already existed in the warehouse"
        ),
        Limitation.SAMPLING_NOT_REPRESENTATIVE: (
            "the assessed files are a deterministic hash sample of source "
            "identities, which bounds the work and is not a claim of statistical "
            "representativeness"
        ),
        Limitation.FORMAT_IS_REPORTED_METADATA: (
            "formats come from the content type the storage layer reports, not "
            "from inspecting file contents, and a file-name extension is never read"
        ),
        Limitation.COLLECTION_IS_REGISTERED_INDEX: (
            "counts describe what the registered collection indexes, which can "
            "differ from everything in the underlying bucket or volume"
        ),
        Limitation.PAGES_PARTIALLY_REPRESENTED: (
            "some results cover only part of their document's pages, so those "
            "documents were not assessed whole"
        ),
        Limitation.VERSION_EVIDENCE_MISSING: (
            "version evidence is missing on at least one side of the match, so "
            "whether those results describe the current file is unknown"
        ),
        Limitation.TIMESTAMP_NOT_VERSION_PROOF: (
            "a processing timestamp orders attempts but does not prove which "
            "version of a file was processed"
        ),
    }
)

SHORTLIST_DEFAULT = 30
SAMPLE_DEFAULT = 200
SAMPLE_CEILING = 1000


# --- Interfaces --------------------------------------------------------------


class MeasureKind(str, Enum):
    """What one alias of a file aggregate statement is allowed to return."""

    COUNT = "count"
    TIMESTAMP = "timestamp"


@runtime_checkable
class FileCollectionSource(Protocol):
    """A connector that can aggregate a file collection's metadata.

    The declared attributes are static facts about the connector, read without
    opening anything. ``document_families`` is what its metadata can bucket, and
    ``metadata_fields`` is which per-file facts its collections carry, so a field
    outside it is reported unavailable rather than defaulted.

    No member may read file content, refresh an external metadata cache, or
    create a resource: a collection is read exactly as configured.
    """

    #: Stable connector name, the same one the warehouse adapter carries.
    name: str
    collection_kind: CollectionKind
    metadata_fields: frozenset[MetadataField]
    document_families: frozenset[DocumentFamily]

    def file_collection_inventory(self, collection: str) -> CollectionInventory:
        """Aggregate one collection's metadata in a single budgeted statement.

        Admitted through the adapter's own cost gate like any other scan. A
        count the collection's metadata does not carry comes back
        :class:`~.results.Unavailable`, never zero.
        """
        ...


@runtime_checkable
class DiscoveringFileSource(Protocol):
    """A source that can also list the collections inside the source scope.

    Beside :class:`FileCollectionSource` rather than a member of it: a source
    reading an explicitly bound manifest can aggregate it without being able to
    discover anything, and declining discovery is an answer rather than a gap.
    """

    def list_file_collections(self) -> list[CollectionSummary]:
        """Every collection in scope, from catalog metadata alone.

        Never scans a collection to manufacture a count: a count the catalog
        does not carry is unavailable, because turning a listing into a scan per
        collection turns a free command into a bill.
        """
        ...


@runtime_checkable
class FileResultSource(FileCollectionSource, Protocol):
    """A source that can also assess materialized processing results.

    The adapter owns the dialect and the connection; the orchestrator owns the
    shape of the operation. So the adapter contributes the bounded source
    selection and executes the one aggregate statement the orchestrator
    assembles, and never returns a row.
    """

    def quote_identifier(self, name: str) -> str:
        """``name`` quoted as one identifier in this connector's dialect."""
        ...

    def source_sample_sql(
        self,
        collection: str,
        formats: frozenset[FileFormat],
        sample_files: int,
    ) -> str:
        """A SELECT choosing at most ``sample_files`` files of ``formats``.

        Deterministic: ordered by a hash of the source identity with the
        identity itself as the tie-breaker, as :attr:`SamplingMethod.
        SOURCE_IDENTITY_HASH` describes. It yields exactly the columns
        ``source_identity``, ``source_version`` (NULL where the collection
        carries no version evidence), and ``source_format`` (a
        :class:`FileFormat` value), and it is only ever embedded in a statement
        that aggregates it.
        """
        ...

    def run_file_aggregate(
        self, sql: str, measures: Mapping[str, MeasureKind]
    ) -> dict[str, int | datetime | None]:
        """Execute one engine-assembled aggregate statement and return its row.

        Only the aliases named in ``measures`` come back, each checked against
        its kind; a statement that yields more than one row, or a value of the
        wrong kind, is refused rather than coerced. Charged to the adapter's
        own cost gate. ``None`` is a SQL NULL, which the orchestrator turns into
        an :class:`~.results.Unavailable` with the reason it knows and the
        adapter does not.
        """
        ...


@dataclass(frozen=True)
class RowDiagnostics:
    """What a result format computes over one stored result row.

    Each value is a SQL expression over that row, or an
    :class:`~.results.Unavailable` where the format does not support the
    measurement. ``status`` must evaluate to a :class:`ProcessingStatus` value,
    and ``payload_valid`` to a boolean saying whether the stored payload has the
    shape the format declares. Every :class:`Diagnostic` has an entry, so an
    unsupported one is stated rather than silently absent.
    """

    status: str | Unavailable
    payload_valid: str | Unavailable
    diagnostics: Mapping[Diagnostic, str | Unavailable]

    def __post_init__(self) -> None:
        missing = [d.value for d in Diagnostic if d not in self.diagnostics]
        if missing:
            raise ValueError(
                "a result format must state every diagnostic, as an expression or "
                f"as unavailable; missing: {', '.join(missing)}"
            )
        object.__setattr__(
            self, "diagnostics", MappingProxyType(dict(self.diagnostics))
        )


@runtime_checkable
class ResultFormat(Protocol):
    """Interpretation of one materialized result format.

    It generates SQL expressions and nothing else. It never executes a
    statement, and in particular never calls a document-processing function:
    a result format reads what a previous pipeline stored.
    """

    name: ResultFormatName
    #: The connectors whose dialect this format's expressions are written in.
    connectors: frozenset[str]
    document_families: frozenset[DocumentFamily]

    def row_diagnostics(
        self, binding: ResultBinding, quote: Callable[[str], str]
    ) -> RowDiagnostics:
        """The per-row expressions for ``binding``'s result table.

        ``quote`` is the source's identifier quoting, passed in so a format that
        spans dialects never has to know which one it is writing for.
        """
        ...


# The registered result formats. Empty until a format adapter is implemented, so
# today every connector reports result assessment unavailable by name.
RESULT_FORMATS: tuple[ResultFormat, ...] = ()


# --- Requests ----------------------------------------------------------------


@dataclass(frozen=True)
class ResultBinding:
    """A collection bound to the materialized table holding its results.

    The engine-side, already-resolved form. Project configuration is where a
    binding is authored and validated, including that every relation it names is
    inside the source scope and that every column is a plain identifier; this is
    what that validation produces. It carries identifiers only, never an
    expression, which is what keeps a binding from being a way to run SQL.

    A native format reads ``payload_column`` (and ``status_column`` where the
    provider stores one separately). ``mapped_columns`` maps diagnostics onto
    plain columns for the ``mapped_columns`` format, with ``status_column`` as
    the status. ``version_column`` and ``processed_at_column`` are optional
    evidence: without the first, correspondence to the current file is unknown;
    without the second, duplicate results cannot be resolved to a latest one.
    """

    collection: str
    result_table: str
    identity_column: str
    result_format: ResultFormatName
    payload_column: str | None = None
    status_column: str | None = None
    mapped_columns: Mapping[Diagnostic, str] = field(default_factory=dict)
    version_column: str | None = None
    processed_at_column: str | None = None

    def __post_init__(self) -> None:
        if self.result_format is ResultFormatName.MAPPED_COLUMNS:
            if self.payload_column is not None:
                raise RequestError(
                    "a mapped_columns binding maps diagnostics onto columns and "
                    "reads no parser payload; drop payload_column"
                )
            if not self.mapped_columns and self.status_column is None:
                raise RequestError(
                    "a mapped_columns binding needs a status_column or at least "
                    "one mapped diagnostic column"
                )
        else:
            if self.payload_column is None:
                raise RequestError(
                    f"a {self.result_format.value} binding reads the provider's "
                    "stored payload; name its payload_column"
                )
            if self.mapped_columns:
                raise RequestError(
                    f"a {self.result_format.value} binding reads fixed payload "
                    "paths; mapped_columns applies only to the mapped_columns format"
                )
        object.__setattr__(
            self, "mapped_columns", MappingProxyType(dict(self.mapped_columns))
        )


@dataclass(frozen=True)
class InventoryRequest:
    """List the collections in scope, or aggregate one.

    ``limit`` and ``show_all`` shape the shortlist of a listing; neither ever
    triggers a scan of a collection.
    """

    collection: str | None = None
    limit: int = SHORTLIST_DEFAULT
    show_all: bool = False

    def __post_init__(self) -> None:
        if isinstance(self.limit, bool) or not isinstance(self.limit, int):
            raise RequestError(f"limit must be a whole number, not {self.limit!r}")
        if self.limit < 1:
            raise RequestError(f"limit must be at least 1, not {self.limit}")


@dataclass(frozen=True)
class ProfileRequest:
    """Assess a collection's materialized results over a deterministic sample.

    ``families`` accepts the family values as strings too, since that is how a
    command line delivers them, and defaults to every family. ``sample_files``
    bounds the assessment set, not the warehouse's scan, which the statement's
    own estimate prices at admission.
    """

    collection: str
    binding: ResultBinding
    families: frozenset[DocumentFamily] = frozenset(DocumentFamily)
    sample_files: int = SAMPLE_DEFAULT

    def __post_init__(self) -> None:
        families: set[DocumentFamily] = set()
        for value in self.families:
            try:
                families.add(DocumentFamily(value))
            except ValueError:
                allowed = ", ".join(f.value for f in DocumentFamily)
                raise RequestError(
                    f"'{value}' is not a document family dex assesses; use one of "
                    f"{allowed}"
                ) from None
        if not families:
            raise RequestError("name at least one document family to assess")
        object.__setattr__(self, "families", frozenset(families))

        sample = self.sample_files
        if isinstance(sample, bool) or not isinstance(sample, int):
            raise RequestError(f"sample_files must be a whole number, not {sample!r}")
        if not 1 <= sample <= SAMPLE_CEILING:
            raise RequestError(
                f"sample_files must be between 1 and {SAMPLE_CEILING}, not {sample}; "
                "there is no full-collection content assessment"
            )
        if self.binding.collection != self.collection:
            raise RequestError(
                f"the result binding is for '{self.binding.collection}', not "
                f"'{self.collection}'"
            )

    @property
    def formats(self) -> frozenset[FileFormat]:
        """The format buckets the selected families cover."""

        return frozenset().union(*(FAMILY_FORMATS[f] for f in self.families))


# --- Capabilities ------------------------------------------------------------


class CapabilityLimitation(str, Enum):
    """Why a file capability is unavailable on this connection."""

    NO_FILE_SOURCE = "no_file_source"
    NO_COLLECTION_DISCOVERY = "no_collection_discovery"
    NO_RESULT_SOURCE = "no_result_source"
    NO_RESULT_FORMAT = "no_result_format"


_CAPABILITY_LIMITATION_TEXT: Mapping[CapabilityLimitation, str] = MappingProxyType(
    {
        CapabilityLimitation.NO_FILE_SOURCE: (
            "this connector implements no file-exploration source; dex does not "
            "read object storage on its own and does not switch to another connector"
        ),
        CapabilityLimitation.NO_COLLECTION_DISCOVERY: (
            "this connector aggregates a collection it is given but cannot list "
            "collections, so name one explicitly"
        ),
        CapabilityLimitation.NO_RESULT_SOURCE: (
            "this connector can aggregate collection metadata but cannot assess "
            "materialized processing results"
        ),
        CapabilityLimitation.NO_RESULT_FORMAT: (
            "no supported result format is written for this connector's dialect"
        ),
    }
)


class Capability(BaseModel):
    """One file capability: available, or unavailable with its named reason."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    available: bool
    limitation: CapabilityLimitation | None = None

    @model_validator(mode="after")
    def _limitation_explains_absence(self) -> Capability:
        if self.available and self.limitation is not None:
            raise ValueError("an available capability carries no limitation")
        if not self.available and self.limitation is None:
            raise ValueError("an unavailable capability must name its limitation")
        return self

    @computed_field  # type: ignore[prop-decorator]
    @property
    def explanation(self) -> str | None:
        if self.limitation is None:
            return None
        return _CAPABILITY_LIMITATION_TEXT[self.limitation]


class NativeProcessing(BaseModel):
    """Whether dex may invoke document processing: never, and why.

    There is no field to set. ``available`` and ``reason`` are computed
    constants, so no construction, copy, deserialization, or confirmation flag
    can report it available. Opening it requires a provider-enforced hard
    monetary cap that covers the exact operation, holds against concurrent and
    in-flight work, and that dex can verify; estimates, page limits, delayed
    quotas, and user confirmation do not substitute for one. Changing that is a
    code change reviewed on its own, never a configuration.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    @computed_field  # type: ignore[prop-decorator]
    @property
    def available(self) -> bool:
        return False

    @computed_field  # type: ignore[prop-decorator]
    @property
    def reason(self) -> str:
        return "no_verified_hard_spend_cap"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def explanation(self) -> str:
        return (
            "new document processing needs a provider-enforced hard spend cap "
            "that dex can verify, and no supported processing path offers one; "
            "dex assesses results that already exist and never invokes a "
            "document processor"
        )


class FileCapabilities(BaseModel):
    """What file exploration this connection supports, derived structurally."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    collection_discovery: Capability
    metadata_aggregation: Capability
    result_assessment: Capability
    document_families: tuple[DocumentFamily, ...]
    result_formats: tuple[ResultFormatName, ...]
    native_processing: NativeProcessing = NativeProcessing()

    def payload(self) -> dict[str, Any]:
        """The JSON shape a capability report carries."""

        return self.model_dump(mode="json")


def _in_declared_order(values, enum: type[Enum]) -> tuple:
    chosen = set(values)
    return tuple(member for member in enum if member in chosen)


def file_capabilities(
    adapter: object, result_formats: tuple[ResultFormat, ...] = RESULT_FORMATS
) -> FileCapabilities:
    """What ``adapter`` can do with files, read off the protocols it satisfies.

    Nothing is probed and nothing is claimed: a connector that implements no
    file source gets every capability unavailable with the same named reason,
    and this never looks for another connector to answer instead. Result
    formats count only when they are written for this connector's dialect.
    """

    if not isinstance(adapter, FileCollectionSource):
        absent = Capability(
            available=False, limitation=CapabilityLimitation.NO_FILE_SOURCE
        )
        return FileCapabilities(
            collection_discovery=absent,
            metadata_aggregation=absent,
            result_assessment=absent,
            document_families=(),
            result_formats=(),
        )

    available = Capability(available=True)
    discovery = (
        available
        if isinstance(adapter, DiscoveringFileSource)
        else Capability(
            available=False, limitation=CapabilityLimitation.NO_COLLECTION_DISCOVERY
        )
    )

    formats: tuple[ResultFormatName, ...] = ()
    if not isinstance(adapter, FileResultSource):
        assessment = Capability(
            available=False, limitation=CapabilityLimitation.NO_RESULT_SOURCE
        )
    else:
        formats = _in_declared_order(
            (f.name for f in result_formats if adapter.name in f.connectors),
            ResultFormatName,
        )
        assessment = (
            available
            if formats
            else Capability(
                available=False, limitation=CapabilityLimitation.NO_RESULT_FORMAT
            )
        )

    return FileCapabilities(
        collection_discovery=discovery,
        metadata_aggregation=available,
        result_assessment=assessment,
        document_families=_in_declared_order(adapter.document_families, DocumentFamily),
        result_formats=formats,
    )
