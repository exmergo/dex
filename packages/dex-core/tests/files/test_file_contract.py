"""The file-exploration contract: fixed vocabularies, request refusals, and a
capability model read off the protocols a connector satisfies rather than off
anything it claims."""

from __future__ import annotations

import pytest

from exmergo_dex_core import envelope as env
from exmergo_dex_core.adapters import _adapter_class
from exmergo_dex_core.adapters.base import Adapter
from exmergo_dex_core.errors import RequestError
from exmergo_dex_core.files.contract import (
    DIAGNOSTIC_BIN_EDGES,
    FAMILY_FORMATS,
    RESULT_FORMATS,
    SAMPLE_CEILING,
    Capability,
    CapabilityLimitation,
    CollectionKind,
    Diagnostic,
    DiscoveringFileSource,
    DocumentFamily,
    FileCollectionSource,
    FileFormat,
    FileResultSource,
    InventoryRequest,
    Limitation,
    MeasureKind,
    MetadataField,
    NativeProcessing,
    ProcessingStatus,
    ProfileRequest,
    ResultBinding,
    ResultFormatName,
    RowDiagnostics,
    UnavailableReason,
    file_capabilities,
    format_for_content_type,
)

SHIPPED_CONNECTORS = [
    "duckdb",
    "bigquery",
    "snowflake",
    "databricks",
    "postgres",
    "redshift",
    "clickhouse",
]

# --- vocabulary ---------------------------------------------------------------


def test_scanned_image_is_exactly_the_three_document_image_formats():
    assert FAMILY_FORMATS[DocumentFamily.SCANNED_IMAGE] == {
        FileFormat.JPEG,
        FileFormat.PNG,
        FileFormat.TIFF,
    }
    assert FAMILY_FORMATS[DocumentFamily.PDF] == {FileFormat.PDF}


def test_every_format_belongs_to_at_most_one_family_and_other_to_none():
    claimed = [fmt for formats in FAMILY_FORMATS.values() for fmt in formats]
    assert len(claimed) == len(set(claimed))
    assert FileFormat.OTHER not in claimed
    assert FileFormat.UNKNOWN not in claimed


@pytest.mark.parametrize(
    ("content_type", "expected"),
    [
        ("application/pdf", FileFormat.PDF),
        ("Application/PDF; version=1.7", FileFormat.PDF),
        ("  image/jpeg ", FileFormat.JPEG),
        ("image/png", FileFormat.PNG),
        ("image/tiff", FileFormat.TIFF),
        # Well formed, outside the supported families.
        ("text/plain", FileFormat.OTHER),
        (
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            FileFormat.OTHER,
        ),
        # A common misspelling is not what an upload tool records, and guessing
        # at aliases is how two connectors come to disagree.
        ("image/jpg", FileFormat.OTHER),
        # The storage layer saying it does not know.
        ("application/octet-stream", FileFormat.UNKNOWN),
        ("binary/octet-stream", FileFormat.UNKNOWN),
        # Missing or malformed.
        (None, FileFormat.UNKNOWN),
        ("", FileFormat.UNKNOWN),
        ("pdf", FileFormat.UNKNOWN),
        ("invoice.pdf", FileFormat.UNKNOWN),
    ],
)
def test_a_reported_content_type_lands_in_one_fixed_bucket(content_type, expected):
    assert format_for_content_type(content_type) is expected


def test_a_file_name_extension_never_decides_the_format():
    """The bucket is read off the reported content type alone, so a PNG that was
    uploaded under a `.pdf` name counts as a PNG."""

    assert format_for_content_type("image/png") is FileFormat.PNG
    assert format_for_content_type("scan.pdf") is FileFormat.UNKNOWN


def test_every_diagnostic_has_fixed_bin_edges_starting_at_zero():
    for diagnostic in Diagnostic:
        edges = DIAGNOSTIC_BIN_EDGES[diagnostic]
        assert edges[0] == 0
        assert list(edges) == sorted(set(edges))
    # The first bin is exactly the observed zeros.
    assert all(DIAGNOSTIC_BIN_EDGES[d][1] == 1 for d in Diagnostic)


def test_no_vocabulary_value_trips_the_envelope_key_screens():
    """Diagnostics become payload keys, and the sanitizer refuses any key that
    reads like a secret or like row data. A diagnostic named after a parser's
    `tokens` would take every profile down on the way out."""

    screens = (*env._SECRET_KEY_PATTERNS, *env._RAW_ROW_KEY_PATTERNS)
    for enum in (Diagnostic, FileFormat, ProcessingStatus, MetadataField):
        for member in enum:
            assert not any(p in member.value for p in screens), member


# --- requests -----------------------------------------------------------------


def _binding(**overrides) -> ResultBinding:
    fields = {
        "collection": "proj.docs.files_obj",
        "result_table": "proj.docs.parsed",
        "identity_column": "uri",
        "result_format": ResultFormatName.BIGQUERY_DOCUMENT_AI,
        "payload_column": "ml_process_document_result",
        "status_column": "ml_process_document_status",
    }
    fields.update(overrides)
    return ResultBinding(**fields)


def test_a_profile_request_defaults_to_every_family_and_two_hundred_files():
    request = ProfileRequest(collection="proj.docs.files_obj", binding=_binding())
    assert request.families == set(DocumentFamily)
    assert request.sample_files == 200
    assert request.formats == {
        FileFormat.PDF,
        FileFormat.JPEG,
        FileFormat.PNG,
        FileFormat.TIFF,
    }


def test_a_profile_request_takes_family_names_the_way_a_command_line_sends_them():
    request = ProfileRequest(
        collection="proj.docs.files_obj",
        binding=_binding(),
        families=frozenset({"scanned_image"}),
    )
    assert request.families == {DocumentFamily.SCANNED_IMAGE}
    assert request.formats == FAMILY_FORMATS[DocumentFamily.SCANNED_IMAGE]


@pytest.mark.parametrize("sample", [0, -1, SAMPLE_CEILING + 1, True, "200", 2.5])
def test_a_profile_request_refuses_a_sample_outside_one_to_the_ceiling(sample):
    with pytest.raises(RequestError, match="sample_files"):
        ProfileRequest(
            collection="proj.docs.files_obj", binding=_binding(), sample_files=sample
        )


def test_the_ceiling_itself_is_accepted_and_there_is_no_full_mode():
    request = ProfileRequest(
        collection="proj.docs.files_obj",
        binding=_binding(),
        sample_files=SAMPLE_CEILING,
    )
    assert request.sample_files == 1000


def test_an_unknown_family_is_refused_by_name_with_the_ones_that_exist():
    with pytest.raises(RequestError) as refused:
        ProfileRequest(
            collection="proj.docs.files_obj",
            binding=_binding(),
            families=frozenset({"docx"}),
        )
    assert "'docx'" in str(refused.value)
    assert "pdf, scanned_image" in str(refused.value)


def test_a_profile_request_needs_at_least_one_family():
    with pytest.raises(RequestError, match="at least one"):
        ProfileRequest(
            collection="proj.docs.files_obj", binding=_binding(), families=frozenset()
        )


def test_a_binding_for_another_collection_is_refused():
    with pytest.raises(RequestError, match=r"proj\.docs\.files_obj"):
        ProfileRequest(collection="proj.docs.other_obj", binding=_binding())


@pytest.mark.parametrize("limit", [0, -5, True, "30"])
def test_an_inventory_limit_must_be_a_positive_whole_number(limit):
    with pytest.raises(RequestError, match="limit"):
        InventoryRequest(limit=limit)


def test_an_inventory_request_defaults_to_a_shortlist_of_thirty():
    request = InventoryRequest()
    assert (request.collection, request.limit, request.show_all) == (None, 30, False)


def test_a_native_binding_needs_its_payload_column():
    with pytest.raises(RequestError, match="payload_column"):
        _binding(payload_column=None)


def test_a_native_binding_refuses_mapped_columns():
    with pytest.raises(RequestError, match="mapped_columns"):
        _binding(mapped_columns={Diagnostic.REPORTED_PAGES: "page_count"})


def test_a_mapped_binding_reads_no_payload():
    with pytest.raises(RequestError, match="payload_column"):
        _binding(result_format=ResultFormatName.MAPPED_COLUMNS)


def test_a_mapped_binding_needs_something_to_read():
    with pytest.raises(RequestError, match="status_column"):
        _binding(
            result_format=ResultFormatName.MAPPED_COLUMNS,
            payload_column=None,
            status_column=None,
        )


def test_a_bindings_column_mapping_cannot_be_changed_after_it_is_resolved():
    mapping = {Diagnostic.REPORTED_PAGES: "page_count"}
    binding = _binding(
        result_format=ResultFormatName.MAPPED_COLUMNS,
        payload_column=None,
        mapped_columns=mapping,
    )
    mapping[Diagnostic.TABLES] = "table_count"
    assert Diagnostic.TABLES not in binding.mapped_columns
    with pytest.raises(TypeError):
        binding.mapped_columns[Diagnostic.TABLES] = "table_count"


def test_a_result_format_has_to_state_every_diagnostic():
    from exmergo_dex_core.files.results import Unavailable

    unsupported = Unavailable(reason=UnavailableReason.NOT_SUPPORTED_BY_FORMAT)
    with pytest.raises(ValueError, match="paragraphs"):
        RowDiagnostics(
            status="'success'",
            payload_valid="TRUE",
            diagnostics={
                d: unsupported for d in Diagnostic if d != Diagnostic.PARAGRAPHS
            },
        )


# --- compatibility --------------------------------------------------------------


@pytest.mark.parametrize("connector", SHIPPED_CONNECTORS)
def test_no_shipped_connector_implements_a_file_source(connector):
    """Existing connectors stay compatible by implementing nothing, and each
    reports the absence as a named limitation rather than an empty answer.
    Built without ``__init__`` so no connection is opened: the check is
    structural and needs none."""

    adapter = object.__new__(_adapter_class(connector))
    assert not isinstance(adapter, FileCollectionSource)

    capabilities = file_capabilities(adapter)
    for capability in (
        capabilities.collection_discovery,
        capabilities.metadata_aggregation,
        capabilities.result_assessment,
    ):
        assert capability.available is False
        assert capability.limitation is CapabilityLimitation.NO_FILE_SOURCE
    assert capabilities.document_families == ()
    assert capabilities.result_formats == ()
    assert capabilities.native_processing.available is False


def test_the_warehouse_adapter_protocol_gained_no_file_member():
    """A new member on the runtime-checkable ``Adapter`` would demote every
    host-supplied adapter that has not grown it."""

    members = set(vars(Adapter)) | set(Adapter.__annotations__)
    file_members = {
        "file_collection_inventory",
        "list_file_collections",
        "source_sample_sql",
        "run_file_aggregate",
        "collection_kind",
        "metadata_fields",
        "document_families",
    }
    assert not members & file_members


# --- capabilities -----------------------------------------------------------------


class _AggregatingSource:
    """Tier 1 only. Every member refuses to run, because deriving capabilities
    must never probe a connection."""

    name = "fakewh"
    collection_kind = CollectionKind.FILE_MANIFEST
    metadata_fields = frozenset({MetadataField.SIZE, MetadataField.CONTENT_TYPE})
    document_families = frozenset({DocumentFamily.SCANNED_IMAGE, DocumentFamily.PDF})

    def file_collection_inventory(self, collection):
        raise AssertionError("capabilities must not aggregate anything")


class _DiscoveringSource(_AggregatingSource):
    def list_file_collections(self):
        raise AssertionError("capabilities must not list anything")


class _ResultSource(_AggregatingSource):
    def quote_identifier(self, name):
        raise AssertionError("capabilities must not build SQL")

    def source_sample_sql(self, collection, formats, sample_files):
        raise AssertionError("capabilities must not build SQL")

    def run_file_aggregate(self, sql, measures):
        raise AssertionError("capabilities must not execute anything")


class _Format:
    name = ResultFormatName.MAPPED_COLUMNS
    document_families = frozenset(DocumentFamily)

    def __init__(self, *connectors: str) -> None:
        self.connectors = frozenset(connectors)

    def row_diagnostics(self, binding, quote):
        raise AssertionError("capabilities must not build SQL")


def test_the_fakes_satisfy_exactly_the_tiers_they_are_meant_to():
    assert isinstance(_AggregatingSource(), FileCollectionSource)
    assert not isinstance(_AggregatingSource(), DiscoveringFileSource)
    assert not isinstance(_AggregatingSource(), FileResultSource)
    assert isinstance(_DiscoveringSource(), DiscoveringFileSource)
    assert isinstance(_ResultSource(), FileResultSource)
    assert MeasureKind.COUNT.value == "count"


def test_an_aggregating_source_without_discovery_or_results_says_which_is_missing():
    capabilities = file_capabilities(_AggregatingSource())
    assert capabilities.metadata_aggregation.available is True
    assert capabilities.collection_discovery.limitation is (
        CapabilityLimitation.NO_COLLECTION_DISCOVERY
    )
    assert capabilities.result_assessment.limitation is (
        CapabilityLimitation.NO_RESULT_SOURCE
    )
    # Declared order, whatever order the frozenset iterates in.
    assert capabilities.document_families == (
        DocumentFamily.PDF,
        DocumentFamily.SCANNED_IMAGE,
    )


def test_discovery_is_its_own_capability():
    capabilities = file_capabilities(_DiscoveringSource())
    assert capabilities.collection_discovery.available is True
    assert capabilities.collection_discovery.limitation is None


def test_a_result_source_with_no_format_for_its_dialect_cannot_assess():
    capabilities = file_capabilities(_ResultSource(), (_Format("otherwh"),))
    assert capabilities.result_assessment.limitation is (
        CapabilityLimitation.NO_RESULT_FORMAT
    )
    assert capabilities.result_formats == ()


def test_a_result_source_with_a_format_for_its_dialect_can_assess():
    capabilities = file_capabilities(
        _ResultSource(), (_Format("otherwh"), _Format("fakewh"))
    )
    assert capabilities.result_assessment.available is True
    assert capabilities.result_formats == (ResultFormatName.MAPPED_COLUMNS,)


def test_no_result_format_is_registered_yet():
    assert RESULT_FORMATS == ()
    assert file_capabilities(_ResultSource()).result_assessment.limitation is (
        CapabilityLimitation.NO_RESULT_FORMAT
    )


def test_a_capability_is_available_or_names_why_not():
    with pytest.raises(ValueError, match="no limitation"):
        Capability(available=True, limitation=CapabilityLimitation.NO_FILE_SOURCE)
    with pytest.raises(ValueError, match="name its limitation"):
        Capability(available=False)
    explained = Capability(
        available=False, limitation=CapabilityLimitation.NO_FILE_SOURCE
    )
    assert "object storage" in explained.explanation


# --- native processing ------------------------------------------------------------


def test_native_processing_is_unavailable_with_a_specific_reason():
    native = NativeProcessing()
    assert native.available is False
    assert native.reason == "no_verified_hard_spend_cap"
    assert "hard spend cap" in native.explanation
    assert native.model_dump() == {
        "available": False,
        "reason": "no_verified_hard_spend_cap",
        "explanation": native.explanation,
    }


def test_nothing_can_report_native_processing_available():
    """No field exists to set, so construction and deserialization refuse the
    key outright, and a copy that tries to write it still reports false."""

    with pytest.raises(ValueError):
        NativeProcessing(available=True)
    with pytest.raises(ValueError):
        NativeProcessing.model_validate({"available": True})
    copied = NativeProcessing().model_copy(update={"available": True})
    assert copied.available is False
    assert copied.model_dump()["available"] is False

    payload = file_capabilities(_ResultSource(), (_Format("fakewh"),)).payload()
    assert payload["native_processing"]["available"] is False


def test_the_capability_payload_is_plain_json_that_passes_the_sanitizer():
    payload = file_capabilities(_DiscoveringSource()).payload()
    env.sanitize(env.ok({"files": payload}))
    assert payload["metadata_aggregation"] == {
        "available": True,
        "limitation": None,
        "explanation": None,
    }
    assert payload["document_families"] == ["pdf", "scanned_image"]


def test_limitation_codes_are_the_whole_vocabulary():
    """A new limitation needs engine-owned wording before it can be reported."""

    from exmergo_dex_core.files.contract import LIMITATION_TEXT

    assert set(LIMITATION_TEXT) == set(Limitation)
    assert all(text and "\n" not in text for text in LIMITATION_TEXT.values())
