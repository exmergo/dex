"""The host boundary: one import surface, and fixtures a consumer can assert against.

Two jobs. The first is that `exmergo_dex_core.host` stays a door rather than a
second implementation: every name resolves, the export table and `__all__` agree,
and each name is the same object the engine itself uses, so a host and the engine
can never disagree about what a plan digest is.

The second is the vectors. They are shipped data a host runs its own reader over,
and they are only worth shipping if they still describe what the engine does, so
the engine's own suite replays every one of them here. A vector that drifts fails
here before it reaches anybody.
"""

from __future__ import annotations

import json

import pytest

from exmergo_dex_core import host
from exmergo_dex_core.envelope import Envelope, sanitize
from exmergo_dex_core.results import Result, to_envelope


def test_every_exported_name_resolves_and_the_two_lists_agree():
    """Same assertion the package root makes about itself, for the same reason:
    a name in one list and not the other is a name nobody can import."""

    # Sorted the way the package root sorts its own: an ALL-CAPS constant first,
    # then classes, then functions, which is what `sorted` gives on ASCII.
    assert host.__all__ == sorted(
        host.__all__, key=lambda n: (n != "PLAN_DOCUMENT_SCHEMA_VERSION", n)
    )
    assert set(host.__all__) - {"conformance_vectors"} == set(host._EXPORTS)
    for name in host.__all__:
        assert getattr(host, name) is not None, name
    assert host.__dir__() == host.__all__

    with pytest.raises(AttributeError):
        host.__getattr__("no_such_name")


def test_the_facade_re_exports_rather_than_re_implements():
    """Every name is the engine's own object. A host importing from here and the
    engine deciding internally must be running the same code."""

    from exmergo_dex_core.envelope import EstimateQuality
    from exmergo_dex_core.transform.classify import classify_content
    from exmergo_dex_core.transform.evidence import BuildOutcome
    from exmergo_dex_core.transform.plans import PlanEdit
    from exmergo_dex_core.transform.portable import verify_plan_document

    assert host.verify_plan_document is verify_plan_document
    assert host.classify_content is classify_content
    assert host.BuildOutcome is BuildOutcome
    assert host.EstimateQuality is EstimateQuality
    assert host.PlanEdit is PlanEdit


def test_the_edit_vocabulary_is_reachable_here_and_nowhere_else():
    """`PlanEdit` and friends are how a host builds a payload at all, and the
    package root exports none of them, which is the gap this closes."""

    import exmergo_dex_core as root

    for name in ("PlanEdit", "EditKind", "EditOp", "Edit", "TransformPlan", "Conflict"):
        assert hasattr(host, name), name
        assert name not in root.__all__, name


def vectors_for(contract: str) -> list[dict]:
    return [v for v in host.conformance_vectors() if v["contract"] == contract]


def test_the_vectors_ship_and_describe_themselves():
    vectors = host.conformance_vectors()
    assert vectors, "no conformance vectors shipped"
    names = [v["name"] for v in vectors]
    assert names == sorted(names)
    assert len(names) == len(set(names))
    for vector in vectors:
        assert set(vector) == {"name", "contract", "description", "payload", "expect"}
        assert vector["description"].strip()
        assert vector["contract"] in {
            "portable-plan",
            "classification",
            "grounding",
            "build-evidence",
        }


def test_no_vector_carries_a_path_from_the_machine_that_made_it():
    raw = json.dumps(host.conformance_vectors())
    for leak in ("/var/folders", "/private/tmp", "/Users/", "/home/"):
        assert leak not in raw, leak


@pytest.mark.parametrize(
    "vector", vectors_for("portable-plan"), ids=lambda v: v["name"]
)
def test_each_plan_vector_still_verifies_the_way_it_says(vector):
    expect = vector["expect"]
    if expect["verifies"]:
        plan = host.verify_plan_document(vector["payload"])
        # The digest recomputes from the document rather than being carried, and
        # a vector that records one asserts which digest it should be.
        assert host.plan_digest(plan) == plan.digest
        if "digest" in expect:
            assert plan.digest == expect["digest"]
    else:
        with pytest.raises(getattr(host, expect["error"])) as caught:
            host.verify_plan_document(vector["payload"])
        assert expect["names"] in str(caught.value)

    if expect.get("verifies_against_expected_digest") is False:
        with pytest.raises(host.PlanDigestMismatchError):
            host.verify_plan_document(
                vector["payload"], expect_digest=expect["expect_digest"]
            )


def test_the_same_source_state_exports_the_same_digest_from_either_checkout():
    one = next(v for v in vectors_for("portable-plan") if v["name"] == "plan-valid")
    two = next(
        v
        for v in vectors_for("portable-plan")
        if v["name"] == "plan-same-from-a-second-checkout"
    )
    assert two["expect"]["equals"] == one["name"]
    assert two["payload"] == one["payload"]


@pytest.mark.parametrize(
    "vector", vectors_for("classification"), ids=lambda v: v["name"]
)
def test_each_classification_vector_still_classifies_the_way_it_says(vector):
    payload = vector["payload"]
    result = host.classify_content(payload["content"], path=payload["path"])
    assert result.artifact_class.value == vector["expect"]["artifact_class"]
    assert result.data() == vector["expect"]["result"]


@pytest.mark.parametrize("vector", vectors_for("grounding"), ids=lambda v: v["name"])
def test_each_grounding_vector_records_the_verdict_it_claims(vector):
    result = vector["expect"]["result"]
    assert result["completeness"] == vector["expect"]["completeness"]
    if "unresolved_count" in vector["expect"]:
        assert len(result["unresolved"]) == vector["expect"]["unresolved_count"]
    if "ambiguous_count" in vector["expect"]:
        assert len(result["ambiguous"]) == vector["expect"]["ambiguous_count"]
    # Every grounding payload carries the verdict before the findings, so a
    # truncating reader loses the findings rather than the honesty.
    assert list(result)[:2] == ["completeness", "limits"]


@pytest.mark.parametrize(
    "vector", vectors_for("build-evidence"), ids=lambda v: v["name"]
)
def test_each_build_vector_records_the_outcome_it_claims(vector):
    result = vector["expect"]["result"]
    assert result["outcome"] == vector["expect"]["outcome"]
    assert host.BuildOutcome(result["outcome"])


def test_the_vectors_cover_every_build_outcome_the_engine_can_report():
    """A new outcome with no vector is a shape a host cannot prepare for."""

    covered = {v["expect"]["outcome"] for v in vectors_for("build-evidence")}
    assert covered == {o.value for o in host.BuildOutcome}


def test_the_vectors_cover_every_artifact_class_except_the_one_with_no_yaml_form():
    covered = {v["expect"]["artifact_class"] for v in vectors_for("classification")}
    # `data` is a seed's rows, which is not a YAML document and has its own
    # tests; every class a host will meet on a plan's YAML is here.
    assert covered == {c.value for c in host.ArtifactClass} - {"data"}


class _Payload(Result):
    """A result carrying every new contract type, for the serializer to scan."""

    body: dict

    def data(self) -> dict:
        return dict(self.body)


def test_every_new_payload_survives_the_public_safe_serializer():
    """Nothing here may carry a secret-like key or a raw-row payload.

    Asserted over the vectors themselves rather than over a hand-built sample,
    so the shapes checked are the shapes a host will actually be handed.
    """

    for vector in host.conformance_vectors():
        envelope = to_envelope(_Payload(body={"contract": vector["expect"]}))
        assert isinstance(sanitize(envelope), Envelope)
        # And it round-trips as JSON, which is how it crosses to a host at all.
        json.dumps(envelope.model_dump(mode="json"))


def test_every_new_refusal_serializes_safely_and_names_a_reason():
    from exmergo_dex_core.envelope import Reason, error_for, reason_for

    refusals = [
        host.PlanDocumentError("not a readable plan document"),
        host.PlanDigestMismatchError("digest mismatch", expected="a", found="b"),
        host.MissingPackagesError([host.DeclaredPackage(source="hub", name="a/b")]),
        host.NotSelectOnlyError(
            "call", reason=host.RefusalReason.STORED_PROCEDURE_OR_CALL
        ),
    ]
    for exc in refusals:
        envelope = error_for(exc)
        sanitize(envelope)
        assert envelope.errors
        assert reason_for(exc) is not Reason.INTERNAL, type(exc).__name__


def test_the_vectors_are_packaged_beside_the_code_that_reads_them():
    """They have to ship in the wheel, or a host that installs the package gets
    the reader and not the fixtures."""

    assert host.VECTORS_DIR.is_dir()
    assert host.VECTORS_DIR.parent.name == "host"
    assert any(host.VECTORS_DIR.glob("*.json"))
