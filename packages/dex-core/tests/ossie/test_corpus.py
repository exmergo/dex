"""The reviewed Ossie corpus: real documents, and what dex should make of them.

The documents in `fixtures/documents/` are native Ossie files a person can read,
and `fixtures/cases.yaml` says what each one means. Both halves are authored
rather than captured: an expectation recorded from a run asserts only that the
implementation still does what it did, and the question worth asking is whether
it does what it is supposed to.

Why a manifest and not one test per case. The corpus is the artifact a schema
upgrade is reviewed against: bump the pin, run this, and every case whose verdict
moved is the upgrade telling you what changed. That review is only possible if
the verdicts are in one place, in a form a person can read next to the diff, and
`origin` on each expected diagnostic is what says whether a moved verdict is
upstream's decision or ours to explain.

Everything here is offline. The corpus never reaches a warehouse, never fetches
upstream, and validates against the bundled schema alone.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml

from exmergo_dex_core.ossie import OssieSemanticLayer
from exmergo_dex_core.ossie.loader import SCHEMA_SHA256, load_documents, schema_sha256
from exmergo_dex_core.semantic_source import SemanticSourceContext

FIXTURES = Path(__file__).parent / "fixtures"
DOCUMENTS = FIXTURES / "documents"
MANIFEST = FIXTURES / "cases.yaml"


def _manifest() -> dict[str, Any]:
    return yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))


CASES = _manifest()["cases"]


def _staged(case: dict[str, Any], tmp_path: Path) -> tuple[Path, list[str]]:
    """The case's documents copied into a repository of their own.

    Copied rather than read in place, because a semantic source is confined to a
    repository root and the corpus has to exercise it the way a repository does.
    """

    names = []
    for name in case["documents"]:
        shutil.copyfile(DOCUMENTS / name, tmp_path / name)
        names.append(name)
    return tmp_path, names


def _source(case: dict[str, Any], tmp_path: Path) -> OssieSemanticLayer:
    root, names = _staged(case, tmp_path)
    return OssieSemanticLayer.from_context(
        SemanticSourceContext(
            repo_root=str(root),
            connector=case.get("connector", "duckdb"),
            options={"files": names},
        )
    )


def declaring(*fields: str) -> Any:
    """Parametrize over the cases that declare one of ``fields``.

    Rather than parametrizing over every case and skipping the ones with nothing
    to say: the matrix is deliberately sparse, so skipping would make two thirds
    of the corpus's output be cases opting out of an assertion they were never
    in. What comes out instead is a list of the cases each rule is actually held
    against, which is the thing a reviewer wants to read.
    """

    selected = [c for c in CASES if any(f in c for f in fields)]
    assert selected, f"no case declares any of {fields}"
    return pytest.mark.parametrize("case", selected, ids=[c["id"] for c in selected])


@declaring("diagnostics")
def test_the_diagnostics_a_case_declares_are_the_diagnostics_it_gets(
    case: dict[str, Any], tmp_path: Path
) -> None:
    """Both directions, and the second one is the one that catches a regression.

    A case naming a rule dex no longer raises is an obvious failure. A case that
    quietly acquires an *extra* error is the expensive one: the document is still
    refused, the suite is still green somewhere, and the reason it is refused has
    changed underneath a corpus that says why.
    """

    root, names = _staged(case, tmp_path)
    loaded = load_documents(root, names, connector=case.get("connector", "duckdb"))

    seen = {(d.rule, d.severity) for d in loaded.diagnostics}
    expected = {(d["rule"], d["severity"]) for d in case["diagnostics"]}

    missing = sorted(expected - seen)
    assert not missing, (
        f"{case['id']} expects {missing} and got "
        f"{sorted(seen)}: {[d.render() for d in loaded.diagnostics]}"
    )

    unexpected = sorted(
        (rule, severity)
        for rule, severity in seen
        if severity == "error" and (rule, severity) not in expected
    )
    assert not unexpected, (
        f"{case['id']} was refused for reasons it does not declare: {unexpected}. "
        "A corpus that records why a document is refused is worth nothing if the "
        f"reason can change under it: {[d.render() for d in loaded.diagnostics]}"
    )


@declaring("semantic_models")
def test_the_catalog_carries_the_semantic_models_a_case_declares(
    case: dict[str, Any], tmp_path: Path
) -> None:
    source = _source(case, tmp_path)
    if case["semantic_models"] == []:
        # A document that cannot be read is not a layer that declares nothing,
        # so the catalog channel refuses rather than answering empty.
        from exmergo_dex_core.errors import ProjectError

        with pytest.raises(ProjectError):
            source.semantic_catalog()
        return

    names = [m.name for m in source.semantic_catalog().semantic_models]

    assert sorted(names) == sorted(case["semantic_models"]), (
        f"{case['id']} expected {sorted(case['semantic_models'])}, got {sorted(names)}"
    )


@declaring("physical_columns")
def test_the_physical_link_is_exactly_what_a_case_declares(
    case: dict[str, Any], tmp_path: Path
) -> None:
    """Equality, not containment, and that is the whole point of the case.

    Physical linkage is the input to the PII gate: a token resolves to a relation
    and a column, and the gate reads that column's evidence. A link too few costs
    a screening. A link too many screens the wrong column and reports the verdict
    as evidence-backed, which is worse, and containment would not catch it.
    """

    view = _source(case, tmp_path).semantic_catalog()
    expected = {token: tuple(pair) for token, pair in case["physical_columns"].items()}

    assert view.physical_columns == expected, (
        f"{case['id']} expected {expected}, got {view.physical_columns}"
    )


@declaring("composite_keys", "single_keys")
def test_the_declared_keys_are_exactly_what_a_case_declares(
    case: dict[str, Any], tmp_path: Path
) -> None:
    """Composite and single kept apart, because the claims differ in strength.

    A composite says a combination is unique and none of its members are. Leaking
    a member into the single list makes the grain axis verify a claim the author
    never wrote, and reconcile propose a `unique` test on a column that is not.
    """

    declarations = _source(case, tmp_path).declared_definitions()

    if "composite_keys" in case:
        seen = sorted(
            (key.model, tuple(key.columns))
            for key in declarations.declared_composite_keys
        )
        expected = sorted(
            (entry["model"], tuple(entry["columns"]))
            for entry in case["composite_keys"]
        )
        assert seen == expected, f"{case['id']}: composite keys {seen} != {expected}"

    if "single_keys" in case:
        seen = sorted(
            (key.model, key.column) for key in declarations.declared_keys if key.unique
        )
        expected = sorted(
            (entry["model"], entry["column"]) for entry in case["single_keys"]
        )
        assert seen == expected, f"{case['id']}: single keys {seen} != {expected}"


@declaring("relationships")
def test_the_declared_relationships_keep_every_ordered_pair(
    case: dict[str, Any], tmp_path: Path
) -> None:
    declared = _source(case, tmp_path).declared_definitions().declared_relationships

    seen = sorted(
        (r.model, r.to_model, tuple(tuple(p) for p in r.column_pairs)) for r in declared
    )
    expected = sorted(
        (
            entry["model"],
            entry["to_model"],
            tuple(tuple(pair) for pair in entry["pairs"]),
        )
        for entry in case["relationships"]
    )

    assert seen == expected, f"{case['id']}: relationships {seen} != {expected}"


def test_a_warning_retains_the_document_it_warns_about() -> None:
    """The severity distinction, asserted rather than left to the manifest.

    An error and a warning are not two strengths of the same verdict. An error
    means dex could not read the document; a warning means it read it and has
    something to say. A warning that dropped the document would silently narrow
    the layer, and the narrowing would look like a layer the author never wrote.
    """

    case = next(c for c in CASES if c["id"] == "target_key_coverage_warns")

    assert all(d["severity"] == "warning" for d in case["diagnostics"])
    assert case["semantic_models"], (
        "the case has to assert what survives, or it is asserting that a warning "
        "is quiet rather than that it is non-fatal"
    )


def test_the_three_document_suffixes_read_identically(tmp_path: Path) -> None:
    """Equivalent semantics, from three byte-different serializations.

    The three fixtures are deliberately not each other's pretty-printer output:
    one is block YAML, one is flow YAML with a different key order, one is JSON.
    Reading them the same is a statement about the reader rather than about the
    files.
    """

    def read(name: str) -> tuple[list[str], dict[str, Any]]:
        root = tmp_path / name.replace(".", "_")
        root.mkdir()
        shutil.copyfile(DOCUMENTS / name, root / name)
        view = OssieSemanticLayer(root, [name], connector="duckdb").semantic_catalog()
        return [d.definition for d in view.dimensions], dict(view.physical_columns)

    first = read("minimal.ossie.yaml")
    assert first == read("minimal.ossie.yml")
    assert first == read("minimal.ossie.json")


def test_every_case_names_documents_that_exist_and_every_document_a_case(
    tmp_path: Path,
) -> None:
    """The corpus and the manifest are one artifact, and this keeps them one.

    A case naming a document that was renamed away is a case that silently stops
    asserting. A document no case names is worse: it looks like reviewed coverage
    and is not read by anything, so an upgrade that changes its verdict changes
    nothing anybody sees.
    """

    on_disk = {path.name for path in DOCUMENTS.iterdir() if path.is_file()}
    named = {name for case in CASES for name in case["documents"]}

    assert not (named - on_disk), (
        f"cases name absent documents: {sorted(named - on_disk)}"
    )
    assert not (on_disk - named), (
        f"documents no case reads: {sorted(on_disk - named)}. An unread fixture "
        "looks like coverage and is not"
    )

    ids = [case["id"] for case in CASES]
    assert len(ids) == len(set(ids)), "case ids must be unique: they are the handle"


def test_the_manifest_pins_the_same_schema_the_loader_and_provenance_do() -> None:
    """One pin, recorded in four places, checked in one.

    The hash lives in the loader (what validation uses), in PROVENANCE.md (what a
    reader is told), in this manifest (what the corpus was reviewed against), and
    in the compatibility matrix (what a user is promised). Updating one and not
    the others is the ordinary way a pin rots, and it rots silently because every
    individual file still looks right.

    This checks consistency. It cannot check that a human reviewed the upgrade,
    and it should not be read as though it does.
    """

    manifest = _manifest()["schema"]
    provenance = (
        Path(__file__).resolve().parents[2]
        / "src"
        / "exmergo_dex_core"
        / "ossie"
        / "schema"
        / "PROVENANCE.md"
    ).read_text(encoding="utf-8")
    matrix = (
        Path(__file__).resolve().parents[4] / "references" / "ossie-compatibility.md"
    ).read_text(encoding="utf-8")

    assert manifest["sha256"] == SCHEMA_SHA256
    assert schema_sha256() == SCHEMA_SHA256, "the bundled schema's bytes moved"

    for document, label in ((provenance, "PROVENANCE.md"), (matrix, "the matrix")):
        assert SCHEMA_SHA256 in document, f"{label} does not carry the pinned hash"
        assert manifest["upstream_commit"] in document, (
            f"{label} does not carry the pinned upstream commit"
        )
        assert manifest["declared_version"] in document, (
            f"{label} does not carry the declared schema version"
        )


def test_the_matrix_names_every_case_the_corpus_holds() -> None:
    """A matrix row is a promise, and a case id is the evidence behind it.

    Naming the case in the matrix is what lets a reader check a claim rather than
    take it, and it is what makes an upgrade's changed verdict land in the
    document that made the promise.
    """

    matrix = (
        Path(__file__).resolve().parents[4] / "references" / "ossie-compatibility.md"
    ).read_text(encoding="utf-8")

    missing = [case["id"] for case in CASES if case["id"] not in matrix]

    assert not missing, f"the compatibility matrix names no case for: {missing}"
