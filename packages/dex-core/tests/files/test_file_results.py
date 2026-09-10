"""The file aggregates: structurally incapable of carrying document content, and
never able to spell an absent measurement the way they spell a zero."""

from __future__ import annotations

import enum
import json
import types
import typing
from datetime import UTC, datetime

import pytest
from pydantic import (
    AwareDatetime,
    Strict,
    StringConstraints,
    TypeAdapter,
    ValidationError,
)

from exmergo_dex_core import envelope as env
from exmergo_dex_core.files import results as file_results
from exmergo_dex_core.files.contract import (
    DIAGNOSTIC_BIN_EDGES,
    CollectionKind,
    Diagnostic,
    DocumentFamily,
    Limitation,
    MetadataField,
    ResultFormatName,
    SamplingMethod,
    UnavailableReason,
)
from exmergo_dex_core.files.results import (
    Bin,
    CollectionInventory,
    CollectionScope,
    CollectionSummary,
    Coverage,
    Currency,
    DiagnosticDistributions,
    Distribution,
    FileAggregate,
    FileProfile,
    FormatCounts,
    Processing,
    Ratio,
    StatusCounts,
    Unavailable,
)

NOW = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
EARLIER = datetime(2026, 9, 1, 8, 30, tzinfo=UTC)


# --- the structural guarantee -------------------------------------------------


def _aggregate_models(module=file_results) -> list[type[FileAggregate]]:
    found, stack = [], [FileAggregate]
    while stack:
        for sub in stack.pop().__subclasses__():
            stack.append(sub)
            if sub.__module__ == module.__name__:
                found.append(sub)
    return found


def _leaves(annotation, metadata):
    """Every leaf type an annotation admits, with the constraints on it."""

    origin = typing.get_origin(annotation)
    if origin is typing.Annotated:
        base, *extra = typing.get_args(annotation)
        yield from _leaves(base, [*metadata, *extra])
    elif origin in (typing.Union, types.UnionType):
        for arg in typing.get_args(annotation):
            yield from _leaves(arg, [])
    elif origin is tuple:
        for arg in typing.get_args(annotation):
            if arg is not Ellipsis:
                yield from _leaves(arg, [])
    else:
        yield annotation, metadata


def _constraints(metadata):
    """Constraints, including the ones pydantic nests inside a FieldInfo."""

    for item in metadata:
        yield item
        yield from getattr(item, "metadata", ())


def _admissible(leaf, metadata) -> bool:
    constraints = list(_constraints(metadata))
    if leaf is int:
        return any(isinstance(c, Strict) and c.strict for c in constraints)
    if leaf is str:
        return any(
            isinstance(c, StringConstraints)
            and c.pattern == file_results.RELATION_NAME_PATTERN
            for c in constraints
        )
    if leaf is AwareDatetime:
        return True
    return isinstance(leaf, type) and issubclass(leaf, (enum.Enum, FileAggregate))


def _inadmissible_fields(model: type[FileAggregate]) -> list[str]:
    return sorted(
        name
        for name, info in model.model_fields.items()
        if not all(
            _admissible(leaf, meta)
            for leaf, meta in _leaves(info.annotation, info.metadata)
        )
    )


def test_the_walker_sees_every_public_aggregate():
    names = {m.__name__ for m in _aggregate_models()}
    assert names >= {
        "Unavailable",
        "Ratio",
        "FormatCounts",
        "CollectionSummary",
        "CollectionInventory",
        "Bin",
        "Distribution",
        "DiagnosticDistributions",
        "StatusCounts",
        "CollectionScope",
        "Coverage",
        "Currency",
        "Processing",
        "FileProfile",
    }


@pytest.mark.parametrize("model", _aggregate_models(), ids=lambda m: m.__name__)
def test_no_aggregate_field_can_hold_free_text_none_or_a_loose_number(model):
    """Every leaf is a strict count, a fixed category, a timestamp, another
    aggregate, or a relation name, so there is no field an excerpt, a file path,
    or a provider's error body could be written into, and none that reads an
    absent measurement as ``None``. The next person to add ``uri: str`` meets
    this test."""

    assert _inadmissible_fields(model) == []


def test_the_walker_catches_the_fields_it_exists_to_catch():
    class Leaky(FileAggregate):
        uri: str
        note: str | None
        pages: int
        extra: dict
        fine: file_results.NonNegative

    Leaky.__module__ = "tests.leaky"
    assert _inadmissible_fields(Leaky) == ["extra", "note", "pages", "uri"]


@pytest.mark.parametrize(
    "hostile",
    [
        "gs://example-bucket/2026/CANARY-FILENAME-personal.pdf",
        "https://storage.googleapis.com/b/o.pdf?X-Goog-Signature=abc&X-Goog-Expires=600",
        "/Volumes/main/raw/docs/contract.pdf",
        "@stage/docs/contract.pdf",
        "CANARY-DOC-TEXT Total due 54.00",
        "INVALID_ARGUMENT: could not read 'Jane Doe'",
        "proj.dataset.table; DROP TABLE x",
        "`proj.dataset.table`",
        "",
    ],
)
def test_a_relation_name_refuses_paths_urls_text_and_quoting(hostile):
    with pytest.raises(ValidationError):
        TypeAdapter(file_results.RelationName).validate_python(hostile)


@pytest.mark.parametrize(
    "name", ["my-project.docs.files_obj", "RAW.DOCS.INVOICES", "main.docs"]
)
def test_a_relation_name_accepts_the_identifiers_connectors_report(name):
    assert TypeAdapter(file_results.RelationName).validate_python(name) == name


def test_an_aggregate_refuses_a_key_it_does_not_declare():
    payload = _inventory().model_dump(mode="json")
    payload["uri"] = "gs://bucket/secret.pdf"
    with pytest.raises(ValidationError, match="uri"):
        CollectionInventory.model_validate(payload)


def test_an_aggregate_cannot_be_edited_after_it_is_built():
    inventory = _inventory()
    with pytest.raises(ValidationError):
        inventory.file_count = 3


# --- absent versus zero -------------------------------------------------------


@pytest.mark.parametrize("value", [None, "12", True, -1, 1.0])
def test_a_count_refuses_anything_but_a_non_negative_integer(value):
    with pytest.raises(ValidationError):
        TypeAdapter(file_results.Count).validate_python(value)


def test_an_observed_zero_and_an_unavailable_count_serialize_differently():
    counts = TypeAdapter(file_results.Count)
    assert counts.dump_python(0, mode="json") == 0
    unavailable = Unavailable(reason=UnavailableReason.NOT_REPORTED)
    assert counts.dump_python(unavailable, mode="json") == {"reason": "not_reported"}


def test_an_instant_is_a_timezone_aware_moment_or_unavailable():
    instants = TypeAdapter(file_results.Instant)
    assert instants.validate_python(NOW) == NOW
    with pytest.raises(ValidationError):
        instants.validate_python(datetime(2026, 9, 10))
    with pytest.raises(ValidationError):
        instants.validate_python(None)


def test_a_ratio_carries_both_sides_and_stays_within_its_denominator():
    assert Ratio(numerator=3, denominator=4).payload() == {
        "numerator": 3,
        "denominator": 4,
        "fraction": 0.75,
    }
    assert Ratio(numerator=0, denominator=0).fraction is None
    with pytest.raises(ValidationError, match="cannot exceed"):
        Ratio(numerator=5, denominator=4)


# --- inventory ------------------------------------------------------------------


def _inventory(**overrides) -> CollectionInventory:
    fields = {
        "collection": "my-project.docs.files_obj",
        "kind": CollectionKind.BIGQUERY_OBJECT_TABLE,
        "observed_at": NOW,
        "file_count": 21,
        "known_bytes": 91_234,
        "missing_size": 0,
        "zero_byte": 1,
        "formats": FormatCounts(pdf=12, jpeg=2, png=3, tiff=2, other=1, unknown=1),
        "earliest_update": EARLIER,
        "latest_update": NOW,
        "metadata_refreshed_at": Unavailable(reason=UnavailableReason.NOT_REPORTED),
        "limitations": (
            Limitation.FORMAT_IS_REPORTED_METADATA,
            Limitation.COLLECTION_IS_REGISTERED_INDEX,
        ),
    }
    fields.update(overrides)
    return CollectionInventory(**fields)


def test_an_inventory_round_trips_through_json():
    inventory = _inventory()
    assert (
        CollectionInventory.model_validate_json(inventory.model_dump_json())
        == inventory
    )
    assert CollectionInventory.model_validate(
        json.loads(inventory.model_dump_json())
    ) == (inventory)


def test_an_inventory_counts_every_file_in_exactly_one_format_bucket():
    with pytest.raises(ValidationError, match="format buckets sum to 20"):
        _inventory(
            formats=FormatCounts(pdf=11, jpeg=2, png=3, tiff=2, other=1, unknown=1)
        )


def test_an_inventory_cannot_have_more_zero_byte_files_than_sized_ones():
    with pytest.raises(ValidationError, match="zero bytes"):
        _inventory(missing_size=20, zero_byte=2)


def test_an_inventory_with_no_size_metadata_says_so_instead_of_reporting_zero():
    unreported = Unavailable(reason=UnavailableReason.NOT_REPORTED)
    inventory = _inventory(
        known_bytes=unreported, missing_size=unreported, zero_byte=unreported
    )
    payload = inventory.payload()
    assert payload["known_bytes"] == {"reason": "not_reported"}
    assert payload["zero_byte"] == {"reason": "not_reported"}


def test_an_inventory_states_its_limitations():
    with pytest.raises(ValidationError, match="collection_is_registered_index"):
        _inventory(limitations=(Limitation.FORMAT_IS_REPORTED_METADATA,))
    with pytest.raises(ValidationError, match="twice"):
        _inventory(
            limitations=(
                Limitation.FORMAT_IS_REPORTED_METADATA,
                Limitation.COLLECTION_IS_REGISTERED_INDEX,
                Limitation.FORMAT_IS_REPORTED_METADATA,
            )
        )


def test_an_inventory_update_range_runs_forwards():
    with pytest.raises(ValidationError, match="earliest update"):
        _inventory(earliest_update=NOW, latest_update=EARLIER)


def test_discovery_reports_a_count_it_did_not_scan_for_as_unavailable():
    summary = CollectionSummary(
        collection="my-project.docs.files_obj",
        kind=CollectionKind.BIGQUERY_OBJECT_TABLE,
        file_count=Unavailable(reason=UnavailableReason.NOT_REPORTED),
        metadata_refreshed_at=Unavailable(reason=UnavailableReason.NOT_REPORTED),
    )
    assert summary.payload()["file_count"] == {"reason": "not_reported"}


# --- profile --------------------------------------------------------------------


def _distribution(
    diagnostic: Diagnostic, values: list[int], *, absent: int = 0, invalid: int = 0
) -> Distribution:
    edges = DIAGNOSTIC_BIN_EDGES[diagnostic]
    files = [0] * len(edges)
    for value in values:
        files[max(i for i, edge in enumerate(edges) if value >= edge)] += 1
    no_evidence = Unavailable(reason=UnavailableReason.NO_EVIDENCE)
    return Distribution(
        assessed=len(values) + absent + invalid,
        files_with_value=len(values),
        files_absent=absent,
        files_invalid=invalid,
        minimum=min(values) if values else no_evidence,
        maximum=max(values) if values else no_evidence,
        bins=tuple(Bin(at_least=e, files=n) for e, n in zip(edges, files, strict=True)),
    )


def _diagnostics(**overrides) -> DiagnosticDistributions:
    unsupported = Unavailable(reason=UnavailableReason.NOT_SUPPORTED_BY_FORMAT)
    fields = {
        # Seven matched files: five with text (one an observed empty string), a
        # failure with no payload, and one malformed payload.
        "text_characters": _distribution(
            Diagnostic.TEXT_CHARACTERS, [2400, 400, 0, 150, 600], absent=1, invalid=1
        ),
        "reported_pages": _distribution(
            Diagnostic.REPORTED_PAGES, [3, 1, 1, 1, 2], absent=1, invalid=1
        ),
        "represented_pages": _distribution(
            Diagnostic.REPRESENTED_PAGES, [3, 1, 1, 1, 1], absent=1, invalid=1
        ),
        "tables": _distribution(
            Diagnostic.TABLES, [1, 0, 0, 0, 0], absent=1, invalid=1
        ),
        "form_fields": unsupported,
        "paragraphs": unsupported,
    }
    fields.update(overrides)
    return DiagnosticDistributions(**fields)


def _profile(**overrides) -> FileProfile:
    fields = {
        "collection": CollectionScope(
            collection="my-project.docs.files_obj",
            kind=CollectionKind.BIGQUERY_OBJECT_TABLE,
            result_table="my-project.docs.parsed_documents",
            result_format=ResultFormatName.BIGQUERY_DOCUMENT_AI,
            families=(DocumentFamily.PDF, DocumentFamily.SCANNED_IMAGE),
            metadata_fields=(MetadataField.SIZE, MetadataField.VERSION),
        ),
        "observed_at": NOW,
        "coverage": Coverage(
            requested=200,
            eligible=10,
            sampled=10,
            sampling_method=SamplingMethod.SOURCE_IDENTITY_HASH,
            matched=Ratio(numerator=7, denominator=10),
            missing_result=Ratio(numerator=2, denominator=10),
            ambiguous=Ratio(numerator=1, denominator=10),
        ),
        "currency": Currency(
            version_matched=Ratio(numerator=5, denominator=7),
            version_mismatched=Ratio(numerator=1, denominator=7),
            version_unknown=Ratio(numerator=1, denominator=7),
        ),
        "processing": Processing(
            statuses=StatusCounts(success=5, failure=1, partial=1, unknown=0),
            payload_valid=Ratio(numerator=5, denominator=7),
            partial_page_files=1,
            diagnostics=_diagnostics(),
        ),
        "limitations": (
            Limitation.DOCUMENT_PII_NOT_SCREENED,
            Limitation.NATIVE_PROCESSING_UNAVAILABLE,
            Limitation.SAMPLING_NOT_REPRESENTATIVE,
            Limitation.PAGES_PARTIALLY_REPRESENTED,
        ),
    }
    fields.update(overrides)
    return FileProfile(**fields)


def test_a_profile_round_trips_through_json():
    profile = _profile()
    assert FileProfile.model_validate_json(profile.model_dump_json()) == profile
    assert FileProfile.model_validate(json.loads(profile.model_dump_json())) == profile


def test_a_profile_and_an_inventory_pass_the_envelope_sanitizer():
    envelope = env.ok(
        {"profile": _profile().payload(), "inventory": _inventory().payload()}
    )
    assert env.sanitize(envelope) is envelope


def test_the_diagnostics_model_and_the_diagnostic_vocabulary_stay_in_step():
    assert set(DiagnosticDistributions.model_fields) == {d.value for d in Diagnostic}


def test_files_without_a_result_can_never_leave_the_denominator():
    with pytest.raises(ValidationError, match="stays in the denominator"):
        Coverage(
            requested=200,
            eligible=10,
            sampled=10,
            sampling_method=SamplingMethod.SOURCE_IDENTITY_HASH,
            matched=Ratio(numerator=7, denominator=10),
            missing_result=Ratio(numerator=0, denominator=10),
            ambiguous=Ratio(numerator=1, denominator=10),
        )


def test_coverage_rates_share_the_sample_as_their_denominator():
    with pytest.raises(ValidationError, match="over the sampled files"):
        Coverage(
            requested=200,
            eligible=10,
            sampled=10,
            sampling_method=SamplingMethod.SOURCE_IDENTITY_HASH,
            matched=Ratio(numerator=7, denominator=7),
            missing_result=Ratio(numerator=2, denominator=10),
            ambiguous=Ratio(numerator=1, denominator=10),
        )


@pytest.mark.parametrize(
    ("requested", "eligible", "sampled"), [(5, 10, 10), (200, 10, 5)]
)
def test_a_sample_takes_every_eligible_file_up_to_the_requested_size(
    requested, eligible, sampled
):
    with pytest.raises(ValidationError, match="smaller of the two"):
        Coverage(
            requested=requested,
            eligible=eligible,
            sampled=sampled,
            sampling_method=SamplingMethod.SOURCE_IDENTITY_HASH,
            matched=Ratio(numerator=sampled, denominator=sampled),
            missing_result=Ratio(numerator=0, denominator=sampled),
            ambiguous=Ratio(numerator=0, denominator=sampled),
        )


@pytest.mark.parametrize("requested", [0, 1001])
def test_a_sample_request_stays_within_the_ceiling(requested):
    with pytest.raises(ValidationError):
        Coverage(
            requested=requested,
            eligible=0,
            sampled=0,
            sampling_method=SamplingMethod.SOURCE_IDENTITY_HASH,
            matched=Ratio(numerator=0, denominator=0),
            missing_result=Ratio(numerator=0, denominator=0),
            ambiguous=Ratio(numerator=0, denominator=0),
        )


def test_currency_splits_the_matched_files_three_ways():
    with pytest.raises(ValidationError, match="must add up"):
        Currency(
            version_matched=Ratio(numerator=5, denominator=7),
            version_mismatched=Ratio(numerator=1, denominator=7),
            version_unknown=Ratio(numerator=0, denominator=7),
        )


def test_a_profile_measures_currency_over_the_matched_files():
    with pytest.raises(ValidationError, match="currency"):
        _profile(
            currency=Currency(
                version_matched=Ratio(numerator=8, denominator=10),
                version_mismatched=Ratio(numerator=1, denominator=10),
                version_unknown=Ratio(numerator=1, denominator=10),
            )
        )


def test_statuses_count_the_matched_files_only():
    processing = _profile().processing
    with pytest.raises(ValidationError, match="statuses"):
        _profile(
            processing=processing.model_copy(
                update={
                    "statuses": StatusCounts(success=6, failure=1, partial=1, unknown=0)
                }
            )
        )


def test_ambiguous_duplicates_stay_out_of_every_content_distribution():
    """Eight assessed would mean the ambiguous file was arbitrarily resolved."""

    processing = _profile().processing
    widened = _diagnostics(
        text_characters=_distribution(
            Diagnostic.TEXT_CHARACTERS,
            [2400, 400, 0, 150, 600, 90],
            absent=1,
            invalid=1,
        )
    )
    with pytest.raises(ValidationError, match="excludes ambiguous"):
        _profile(processing=processing.model_copy(update={"diagnostics": widened}))


def test_a_profile_states_what_it_did_not_establish():
    with pytest.raises(ValidationError, match="document_pii_not_screened"):
        _profile(
            limitations=(
                Limitation.NATIVE_PROCESSING_UNAVAILABLE,
                Limitation.SAMPLING_NOT_REPRESENTATIVE,
            )
        )


def test_a_profile_has_no_readiness_score():
    fields = set(FileProfile.model_fields)
    assert fields == {
        "collection",
        "observed_at",
        "coverage",
        "currency",
        "processing",
        "limitations",
    }
    assert not any("score" in f or "ready" in f for f in fields)


# --- distributions ----------------------------------------------------------------


def test_a_distribution_partitions_its_assessed_files():
    with pytest.raises(ValidationError, match="sum to 6"):
        Distribution(
            assessed=7,
            files_with_value=5,
            files_absent=1,
            files_invalid=0,
            minimum=0,
            maximum=3,
            bins=(Bin(at_least=0, files=1), Bin(at_least=1, files=4)),
        )


def test_an_observed_zero_lands_in_the_first_bin_and_an_absence_does_not():
    distribution = _distribution(Diagnostic.TABLES, [0, 0, 1], absent=2)
    assert distribution.bins[0].files == 2
    assert distribution.files_absent == 2
    assert distribution.minimum == 0


def test_with_no_values_there_is_no_minimum_or_maximum():
    empty = _distribution(Diagnostic.TABLES, [], absent=3)
    assert empty.minimum == Unavailable(reason=UnavailableReason.NO_EVIDENCE)
    with pytest.raises(ValidationError, match="no minimum or maximum"):
        Distribution(
            assessed=3,
            files_with_value=0,
            files_absent=3,
            files_invalid=0,
            minimum=0,
            maximum=0,
            bins=(Bin(at_least=0, files=0),),
        )


def test_bins_start_at_zero_rise_and_hold_exactly_the_valued_files():
    with pytest.raises(ValidationError, match="start at 0"):
        Distribution(
            assessed=1,
            files_with_value=1,
            files_absent=0,
            files_invalid=0,
            minimum=2,
            maximum=2,
            bins=(Bin(at_least=1, files=1),),
        )
    with pytest.raises(ValidationError, match="exactly the files"):
        Distribution(
            assessed=2,
            files_with_value=2,
            files_absent=0,
            files_invalid=0,
            minimum=1,
            maximum=2,
            bins=(Bin(at_least=0, files=0), Bin(at_least=1, files=1)),
        )


def test_each_diagnostic_uses_its_own_fixed_bins():
    wrong = _distribution(Diagnostic.TEXT_CHARACTERS, [3, 1], absent=0)
    with pytest.raises(ValidationError, match="fixed bin edges"):
        _diagnostics(reported_pages=wrong)
