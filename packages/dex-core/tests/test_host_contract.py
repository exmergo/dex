"""The host boundary: one import surface, and fixtures a consumer can assert against.

Two jobs. The first is that `exmergo_dex_core.host` stays a door rather than a
second implementation: every name resolves, the export table and `__all__` agree,
and each name is the same object the engine itself uses, so a host and the engine
can never disagree about what a plan digest is.

The second is the vectors. They are shipped data a host runs its own reader over,
and they are only worth shipping if they still describe what the engine does, so
the engine's own suite replays every one it can recompute here: plans,
classifications, and groundings, which carry the project they were grounded
against. A vector that drifts fails here before it reaches anybody. A build
vector records a dbt run that cannot be repeated offline, so it is checked for
saying what it claims rather than recomputed.
"""

from __future__ import annotations

import copy
import json

import pytest

from exmergo_dex_core import host
from exmergo_dex_core.envelope import Envelope, sanitize
from exmergo_dex_core.results import Result, to_envelope


def test_every_exported_name_resolves_and_the_two_lists_agree():
    """Same assertion the package root makes about itself, for the same reason:
    a name in one list and not the other is a name nobody can import."""

    # Sorted the way the package root sorts its own (ruff's RUF022): ALL-CAPS
    # constants first, then classes, then functions, which is what `sorted`
    # gives on ASCII once the constants are lifted out.
    assert host.__all__ == sorted(host.__all__, key=lambda n: (not n.isupper(), n))
    assert set(host.__all__) - {"conformance_vectors"} == set(host._EXPORTS)
    for name in host.__all__:
        assert getattr(host, name) is not None, name
    assert host.__dir__() == host.__all__

    with pytest.raises(AttributeError):
        host.__getattr__("no_such_name")


def test_the_facade_re_exports_rather_than_re_implements():
    """Every name is the engine's own object. A host importing from here and the
    engine deciding internally must be running the same code."""

    from exmergo_dex_core.envelope import EstimateQuality, Reason, reason_for
    from exmergo_dex_core.transform.classify import classify_content
    from exmergo_dex_core.transform.evidence import (
        BuildEvidenceError,
        BuildOutcome,
        recompute_outcome,
        verify_build_evidence,
    )
    from exmergo_dex_core.transform.grounding import MissingReference
    from exmergo_dex_core.transform.plans import PlanEdit
    from exmergo_dex_core.transform.portable import verify_plan_document

    assert host.verify_plan_document is verify_plan_document
    assert host.verify_build_evidence is verify_build_evidence
    assert host.recompute_outcome is recompute_outcome
    assert host.BuildEvidenceError is BuildEvidenceError
    assert host.classify_content is classify_content
    assert host.BuildOutcome is BuildOutcome
    assert host.EstimateQuality is EstimateQuality
    assert host.PlanEdit is PlanEdit
    assert host.MissingReference is MissingReference
    assert host.reason_for is reason_for
    assert host.Reason is Reason


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


def test_plan_digest_reads_every_form_verify_plan_document_reads():
    vector = next(v for v in vectors_for("portable-plan") if v["name"] == "plan-valid")
    document = vector["payload"]
    forms = {
        "PortablePlan": host.verify_plan_document(document),
        "dict": document,
        "str": json.dumps(document),
        "bytes": json.dumps(document).encode("utf-8"),
    }
    for label, form in forms.items():
        assert host.plan_digest(form) == vector["expect"]["digest"], label

    with pytest.raises(host.PlanDocumentError):
        host.plan_digest({"not": "a plan"})


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
    if "missing_count" in vector["expect"]:
        assert len(result["missing"]) == vector["expect"]["missing_count"]
    # Every grounding payload carries the verdict and the three findings before
    # the dependencies, so a truncating reader loses the long list rather than
    # the honesty.
    assert list(result)[:5] == [
        "completeness",
        "limits",
        "unresolved",
        "ambiguous",
        "missing",
    ]


@pytest.mark.parametrize("vector", vectors_for("grounding"), ids=lambda v: v["name"])
def test_each_grounding_vector_replays_from_the_project_it_carries(vector, tmp_path):
    """Grounded again from `payload.project`, the answer is the one that shipped.

    Two fields cannot match by construction and are left out: the engine version,
    and the newest source file's modification time, which is when this test wrote
    the file. Everything else, the digests included, is a function of the files.
    """

    pytest.importorskip("sqlglot")
    from exmergo_dex_core.dbt_project import load

    project = tmp_path / "analytics"
    for path, content in vector["payload"]["project"].items():
        target = project / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    edits = [host.PlanEdit.model_validate(e) for e in vector["payload"]["edits"]]
    replayed = host.ground_plan(load(project), edits, project=project).data()
    shipped = copy.deepcopy(vector["expect"]["result"])
    for result in (replayed, shipped):
        del result["binding"]["engine_version"]
        del result["freshness"]["newest_source_at"]
    assert replayed == shipped


@pytest.mark.parametrize(
    "vector", vectors_for("build-evidence"), ids=lambda v: v["name"]
)
def test_each_build_vector_records_the_outcome_it_claims(vector):
    result = vector["expect"]["result"]
    assert result["outcome"] == vector["expect"]["outcome"]
    assert host.BuildOutcome(result["outcome"])
    # Issue #581: Every shipped build-* vector recomputes to its outcome
    recomputed = host.recompute_outcome(result)
    assert recomputed.value == vector["expect"]["outcome"]
    # And verify_build_evidence verifies the document cleanly
    evidence = host.verify_build_evidence(result)
    assert evidence.outcome.value == vector["expect"]["outcome"]
    assert host.verify_build_evidence(
        result, expect_outcome=vector["expect"]["outcome"]
    )


def test_build_evidence_refuses_contradictory_and_forged_documents():
    """Issue #581: A vector with a forged outcome is refused, and
    {"outcome": "validated"} is refused or recomputes to empty_selection."""
    # {"outcome": "validated"} is refused at validation
    assert host.recompute_outcome({"outcome": "validated"}) in {
        host.BuildOutcome.NOT_RUN,
        host.BuildOutcome.EMPTY_SELECTION,
    }
    assert (
        host.recompute_outcome(
            {"outcome": "validated", "invocation": {"invocation_id": "abc"}}
        )
        is host.BuildOutcome.EMPTY_SELECTION
    )
    # And is refused at validation
    with pytest.raises((host.BuildEvidenceError, ValueError)):
        host.BuildEvidence.model_validate({"outcome": "validated"})
    with pytest.raises(host.BuildEvidenceError):
        host.verify_build_evidence({"outcome": "validated"})

    # Every shipped build vector with a forged outcome is refused
    for vector in vectors_for("build-evidence"):
        original_outcome = vector["expect"]["outcome"]
        for forged_outcome in host.BuildOutcome:
            if forged_outcome.value == original_outcome:
                continue
            forged_result = copy.deepcopy(vector["expect"]["result"])
            forged_result["outcome"] = forged_outcome.value
            with pytest.raises(host.BuildEvidenceError):
                host.verify_build_evidence(forged_result)
            with pytest.raises((host.BuildEvidenceError, ValueError)):
                host.BuildEvidence.model_validate(forged_result)


def test_verify_build_evidence_contract():
    """Issue #581: verify_build_evidence accepts BuildEvidence, dict, str, and bytes."""
    valid_vector = next(
        v for v in vectors_for("build-evidence") if v["name"] == "build-validated"
    )
    result = valid_vector["expect"]["result"]

    # Dict
    ev_from_dict = host.verify_build_evidence(result)
    assert isinstance(ev_from_dict, host.BuildEvidence)
    assert ev_from_dict.outcome is host.BuildOutcome.VALIDATED

    # BuildEvidence model
    ev_from_model = host.verify_build_evidence(ev_from_dict)
    assert ev_from_model is ev_from_dict

    # JSON string
    ev_from_str = host.verify_build_evidence(json.dumps(result))
    assert isinstance(ev_from_str, host.BuildEvidence)

    # JSON bytes
    ev_from_bytes = host.verify_build_evidence(json.dumps(result).encode("utf-8"))
    assert isinstance(ev_from_bytes, host.BuildEvidence)

    # expect_outcome mismatch
    with pytest.raises(host.BuildEvidenceError):
        host.verify_build_evidence(result, expect_outcome="failed")

    # Bad inputs
    with pytest.raises(host.BuildEvidenceError):
        host.verify_build_evidence("{invalid json")

    with pytest.raises(host.BuildEvidenceError):
        host.verify_build_evidence("[]")

    with pytest.raises(host.BuildEvidenceError):
        host.verify_build_evidence(12345)


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
        host.BuildEvidenceError("outcome contradicts fields"),
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


def _classified_rows() -> list[type[BaseException]]:
    from exmergo_dex_core.envelope import _reason_overrides

    return [exc_type for exc_type, _ in _reason_overrides()]


def _raised_through_the_cli(exc, monkeypatch, capsys, duckdb_file) -> dict:
    """The envelope the CLI prints when a real command raises ``exc``.

    Raised from inside ``_run`` so it reaches the envelope through whichever door
    the CLI gives it: one of ``dispatch``'s typed arms, or ``main``'s catch-all.
    """

    from exmergo_dex_core import cli

    def raiser(args, engine):
        raise exc

    monkeypatch.setattr(cli, "_run", raiser)
    cli.main(["connect", "test", "--path", str(duckdb_file)])
    return json.loads(capsys.readouterr().out)


def test_the_suite_classifies_against_the_full_table():
    """The positive control for the parity test below, which reads the table it
    runs over: with the ``sql`` extra missing the second tier is absent, the
    parity test would pass over the first tier alone, and nothing would say so."""

    pytest.importorskip("sqlglot")
    from exmergo_dex_core.guards.cost_guard import CostGuardError

    assert CostGuardError in _classified_rows()


@pytest.mark.parametrize("exc_type", _classified_rows(), ids=lambda c: c.__name__)
def test_a_host_reads_the_reason_the_cli_prints(
    exc_type, monkeypatch, capsys, tmp_path, duckdb_file
):
    """`host.reason_for` is the `reason` the CLI's envelope carries, for every row
    of the classification table.

    They agree because every error envelope the CLI builds from an exception goes
    through `error_for`, which calls the function a host imports. A new arm that
    sets `reason` another way, or builds an envelope without one, fails here and
    names the class it broke.
    """

    from exmergo_dex_core.transform.commands import BuildFailedError
    from exmergo_dex_core.transform.results import BuildResult

    monkeypatch.chdir(tmp_path)
    if issubclass(exc_type, BuildFailedError):  # it carries the failed build
        exc = exc_type("dbt build failed", result=BuildResult())
    else:
        exc = exc_type("refused")

    envelope = _raised_through_the_cli(exc, monkeypatch, capsys, duckdb_file)

    assert envelope["status"] == "error"
    assert envelope["reason"] == host.reason_for(exc).value


def test_a_confirmation_request_is_not_a_refusal_a_host_classifies(
    monkeypatch, capsys, tmp_path, duckdb_file
):
    """The one exception where `reason_for` and the CLI part ways, on purpose.

    `ConfirmationRequiredError` is a `CostGuardError`, so the table reads it as
    `guard`. It asks for a confirmation rather than refusing, and the CLI answers
    it with `needs_confirmation` and no `reason`, which is why the docstring and
    the reference tell a host to catch it first.
    """

    from exmergo_dex_core import ConfirmationRequiredError
    from exmergo_dex_core.envelope import Cost

    monkeypatch.chdir(tmp_path)
    exc = ConfirmationRequiredError(Cost(estimate=1.0))

    envelope = _raised_through_the_cli(exc, monkeypatch, capsys, duckdb_file)

    assert envelope["status"] == "needs_confirmation"
    assert envelope["reason"] is None
    assert host.reason_for(exc) is host.Reason.GUARD


def test_the_vectors_are_packaged_beside_the_code_that_reads_them():
    """They have to ship in the wheel, or a host that installs the package gets
    the reader and not the fixtures."""

    assert host.VECTORS_DIR.is_dir()
    assert host.VECTORS_DIR.parent.name == "host"
    assert any(host.VECTORS_DIR.glob("*.json"))


def test_results_carry_typed_host_models_and_typed_accessors():
    """Issue #579: Result objects carry typed host models and accessors."""
    from exmergo_dex_core.transform.results import (
        BuildResult,
        ClassificationResult,
        GroundingResult,
        PlanExportResult,
    )

    plan_model = host.PortablePlan(
        plan_id="p123",
        intent="testing",
        created_at="2026-01-01",
        project_dir="analytics",
    )
    export_result = PlanExportResult(
        plan_id="p123", digest="sha256:abc", plan=plan_model
    )
    assert isinstance(export_result.plan, host.PortablePlan)
    assert export_result.plan_model() is plan_model

    classification_model = host.ArtifactClassification(
        path="models/test.sql", artifact_class=host.ArtifactClass.DECLARATIVE
    )
    class_result = ClassificationResult(
        plan_id="p123", classifications=[classification_model]
    )
    assert isinstance(class_result.classifications[0], host.ArtifactClassification)
    assert class_result.classification_models() == [classification_model]

    grounding_model = host.Grounding(completeness="complete")
    ground_result = GroundingResult(plan_id="p123", grounding=grounding_model)
    assert isinstance(ground_result.grounding, host.Grounding)
    assert ground_result.grounding_model() is grounding_model

    evidence_model = host.BuildEvidence(
        outcome=host.BuildOutcome.VALIDATED,
        nodes=[host.BuildNode(unique_id="model.test", name="test", status="success")],
    )
    build_result = BuildResult(success=True, evidence=evidence_model)
    assert isinstance(build_result.evidence, host.BuildEvidence)
    assert build_result.evidence_model() is evidence_model


def test_host_models_support_backwards_compatible_subscripting():
    """Issue #579: Host models support __getitem__, __contains__, and get()."""
    plan = host.PortablePlan(
        plan_id="p123",
        intent="testing",
        created_at="2026-01-01",
        project_dir="analytics",
    )
    assert plan["plan_id"] == "p123"
    assert "plan_id" in plan
    assert "nonexistent" not in plan
    assert plan.get("plan_id") == "p123"
    assert plan.get("missing", "default") == "default"
    with pytest.raises(KeyError):
        _ = plan["nonexistent"]

    edit = host.PortableEdit(
        path="models/test.sql",
        kind=host.EditKind.MODEL_SQL,
    )
    assert edit["path"] == "models/test.sql"
    assert "path" in edit
    assert "nonexistent" not in edit
    assert edit.get("path") == "models/test.sql"
    assert edit.get("missing", 42) == 42
    with pytest.raises(KeyError):
        _ = edit["nonexistent"]

    classification = host.ArtifactClassification(
        path="models/test.sql",
        artifact_class=host.ArtifactClass.EXECUTABLE,
    )
    assert classification["path"] == "models/test.sql"
    assert classification["artifact_class"] == "executable"
    assert "artifact_class" in classification
    assert "nonexistent" not in classification
    assert classification.get("artifact_class") == "executable"
    assert classification.get("missing", None) is None
    with pytest.raises(KeyError):
        _ = classification["nonexistent"]

    grounding = host.Grounding(completeness="complete")
    assert grounding["completeness"] == "complete"
    assert "completeness" in grounding
    assert "nonexistent" not in grounding
    assert grounding.get("completeness") == "complete"
    assert grounding.get("nonexistent", "fallback") == "fallback"
    with pytest.raises(KeyError):
        _ = grounding["nonexistent"]

    evidence = host.BuildEvidence(
        outcome=host.BuildOutcome.VALIDATED,
        nodes=[host.BuildNode(unique_id="model.test", name="test", status="success")],
    )
    assert evidence["outcome"] == "validated"
    assert "outcome" in evidence
    assert "nonexistent" not in evidence
    assert evidence.get("outcome") == "validated"
    assert evidence.get("nonexistent", "none") == "none"
    with pytest.raises(KeyError):
        _ = evidence["nonexistent"]


def test_result_data_serializes_cleanly_for_cli_envelopes():
    """Issue #579: result.data() and to_envelope() cleanly serialize models to dicts."""
    from exmergo_dex_core.transform.results import (
        BuildResult,
        ClassificationResult,
        GroundingResult,
        PlanExportResult,
    )

    plan = host.PortablePlan(
        plan_id="p123",
        intent="testing",
        created_at="2026-01-01",
        project_dir="analytics",
    )
    res_export = PlanExportResult(plan_id="p123", digest="sha256:abc", plan=plan)
    env_export = to_envelope(res_export)
    assert env_export.status == "ok"
    assert isinstance(env_export.data["plan"], dict)
    assert env_export.data["plan"]["plan_id"] == "p123"

    classification = host.ArtifactClassification(
        path="models/test.sql", artifact_class=host.ArtifactClass.DECLARATIVE
    )
    res_class = ClassificationResult(plan_id="p123", classifications=[classification])
    env_class = to_envelope(res_class)
    assert env_class.status == "ok"
    assert isinstance(env_class.data["classifications"][0], dict)
    assert env_class.data["classifications"][0]["artifact_class"] == "declarative"

    grounding = host.Grounding(completeness="complete")
    res_ground = GroundingResult(plan_id="p123", grounding=grounding)
    env_ground = to_envelope(res_ground)
    assert env_ground.status == "ok"
    assert env_ground.data["completeness"] == "complete"

    evidence = host.BuildEvidence(
        outcome=host.BuildOutcome.VALIDATED,
        nodes=[host.BuildNode(unique_id="model.test", name="test", status="success")],
    )
    res_build = BuildResult(success=True, evidence=evidence)
    env_build = to_envelope(res_build)
    assert env_build.status == "ok"
    assert env_build.data["outcome"] == "validated"
    assert isinstance(env_build.data["evidence"], dict)
    assert env_build.data["evidence"]["outcome"] == "validated"


def test_build_result_docstring_documents_typed_evidence():
    """Issue #579: BuildResult docstring references the BuildEvidence class."""
    from exmergo_dex_core.transform.results import BuildResult

    assert ":class:`~exmergo_dex_core.transform.evidence.BuildEvidence`" in (
        BuildResult.__doc__ or ""
    )
