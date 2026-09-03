"""The three validation layers, and what each one refuses.

Every test here names the layer it exercises, because the layers have different
install requirements and a failure that crosses them reads very differently: a
structure failure is a bad document, a missing-extra failure is a bad install.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from exmergo_dex_core.ossie import SCHEMA_SHA256, load_documents
from exmergo_dex_core.ossie.loader import ERROR, NOTE, WARNING, schema_bytes

from .conftest import (
    dataset,
    document,
    expression,
    field,
    model,
    reference_document,
    write,
)


def rules(result, severity=None):
    return sorted(
        d.rule for d in result.diagnostics if severity is None or d.severity == severity
    )


def load(root: Path, *names: str, connector: str = "duckdb"):
    return load_documents(root, list(names), connector=connector)


# --- the pin ---------------------------------------------------------------


def test_the_bundled_schema_matches_its_recorded_hash():
    """The whole pinning mechanism, in one assertion.

    Upstream declares the document version as a constant that does not move when
    the schema does, so a version check is worthless as a drift signal and this
    is the check that replaces it. A regeneration then has to update the constant
    in the same commit, which makes it a reviewed diff instead of a quiet update.
    """

    assert hashlib.sha256(schema_bytes()).hexdigest() == SCHEMA_SHA256


def test_the_schema_ships_beside_a_provenance_record():
    """A vendored asset with no provenance cannot be upgraded safely."""

    from importlib import resources

    provenance = (
        resources.files("exmergo_dex_core.ossie") / "schema" / "PROVENANCE.md"
    ).read_text(encoding="utf-8")

    assert SCHEMA_SHA256 in provenance
    assert "b5da5d66f0da4a0cd3388d52201dbf5523221a77" in provenance
    assert "Apache License" in provenance


# --- coordinates -----------------------------------------------------------


def test_a_document_outside_the_repository_is_refused(tmp_path: Path):
    """Reads are confined to the repository, as writes are.

    A committed config file naming `../../etc/passwd` would otherwise have dex
    parse it, and the parse result reaches an envelope.
    """

    outside = tmp_path.parent / "escape.ossie.yaml"
    outside.write_text("version: '0.2.0.dev0'\nsemantic_model: []\n", encoding="utf-8")

    result = load(tmp_path, "../escape.ossie.yaml")

    assert rules(result) == ["confinement"]
    assert not result.documents


def test_a_symlink_leaving_the_repository_is_refused(tmp_path: Path):
    """Confinement is checked on the resolved path, so a symlink cannot cheat."""

    outside = tmp_path.parent / "linked.ossie.yaml"
    outside.write_text("version: '0.2.0.dev0'\nsemantic_model: []\n", encoding="utf-8")
    (tmp_path / "inside.ossie.yaml").symlink_to(outside)

    assert rules(load(tmp_path, "inside.ossie.yaml")) == ["confinement"]


def test_a_missing_document_is_named(tmp_path: Path):
    result = load(tmp_path, "absent.ossie.yaml")

    assert rules(result) == ["missing"]
    assert "absent.ossie.yaml" in result.diagnostics[0].message


def test_a_file_that_is_not_an_ossie_document_is_refused_by_suffix(tmp_path: Path):
    (tmp_path / "schema.yml").write_text("version: x\n", encoding="utf-8")

    assert rules(load(tmp_path, "schema.yml")) == ["suffix"]


# --- layer 1: structure ----------------------------------------------------


def test_a_valid_yaml_document_loads(repo: Path):
    result = load(repo, "commerce.ossie.yaml")

    assert not result.errors
    assert [d.file for d in result.documents] == ["commerce.ossie.yaml"]


def test_a_valid_json_document_loads_identically(tmp_path: Path):
    """JSON and YAML are two spellings of one document shape.

    Asserted by reading the same content both ways and comparing the parsed
    result, rather than by checking that JSON merely parses.
    """

    write(tmp_path, "a.ossie.yaml", reference_document())
    write(tmp_path, "b.ossie.json", reference_document())

    both = load(tmp_path, "a.ossie.yaml"), load(tmp_path, "b.ossie.json")

    assert both[0].documents[0].data == both[1].documents[0].data


def test_unparseable_yaml_is_a_diagnostic_and_never_a_traceback(tmp_path: Path):
    """`yaml.YAMLError` descends from Exception, not ValueError.

    A handler that looked complete has already let one past in this repository,
    so the case is asserted rather than assumed.
    """

    (tmp_path / "broken.ossie.yaml").write_text("a: [1,\n  b: {\n", encoding="utf-8")

    assert rules(load(tmp_path, "broken.ossie.yaml")) == ["malformed"]


def test_a_document_that_is_not_a_mapping_is_refused(tmp_path: Path):
    (tmp_path / "list.ossie.yaml").write_text("- one\n- two\n", encoding="utf-8")

    result = load(tmp_path, "list.ossie.yaml")

    assert rules(result) == ["root"]
    assert "list" in result.diagnostics[0].message


def test_a_wrong_version_is_refused_by_name(tmp_path: Path):
    write(tmp_path, "old.ossie.yaml", document(version="0.1.0"))

    result = load(tmp_path, "old.ossie.yaml")

    assert "version" in rules(result)


def test_an_unknown_structural_key_reports_incompatibility_not_a_cause(
    tmp_path: Path,
):
    """The document may be wrong or it may be newer, and dex cannot tell which.

    A message that picks one sends half the readers to the wrong fix, so it
    reports the incompatibility and names where the pinned draft is recorded.
    """

    doc = reference_document()
    doc["semantic_model"][0]["datasets"][0]["grain"] = ["order_id"]
    write(tmp_path, "future.ossie.yaml", doc)

    result = load(tmp_path, "future.ossie.yaml")
    found = [d for d in result.diagnostics if d.rule == "unknown_key"]

    assert found, rules(result)
    assert "PROVENANCE" in found[0].message
    assert "typo" in found[0].message and "newer draft" in found[0].message


def test_a_nested_malformed_shape_collects_rather_than_raising(tmp_path: Path):
    """A document with several problems reports several problems."""

    doc = document(model("m", {"name": "d", "source": 42, "fields": [{"name": "f"}]}))
    write(tmp_path, "bad.ossie.yaml", doc)

    result = load(tmp_path, "bad.ossie.yaml")

    assert len(result.errors) >= 2
    assert not result.documents


# --- layer 2: integrity ----------------------------------------------------


@pytest.mark.parametrize(
    ("mutate", "rule"),
    [
        (
            lambda d: d["semantic_model"][0]["datasets"].append(
                dataset("orders", "demo.main.other", field("x"))
            ),
            "duplicate_dataset",
        ),
        (
            lambda d: d["semantic_model"][0]["datasets"][0]["fields"].append(
                field("order_id")
            ),
            "duplicate_field",
        ),
        (
            lambda d: d["semantic_model"][0]["metrics"].append(
                {"name": "revenue", "expression": expression(ANSI_SQL="1")}
            ),
            "duplicate_metric",
        ),
        (
            lambda d: d["semantic_model"][0]["relationships"].append(
                {
                    "name": "orders_to_customers",
                    "from": "orders",
                    "to": "customers",
                    "from_columns": ["customer_id"],
                    "to_columns": ["customer_id"],
                }
            ),
            "duplicate_relationship",
        ),
    ],
)
def test_a_duplicate_name_is_refused_at_each_scope(tmp_path: Path, mutate, rule):
    doc = reference_document()
    mutate(doc)
    write(tmp_path, "dupe.ossie.yaml", doc)

    assert rule in rules(load(tmp_path, "dupe.ossie.yaml"))


def test_a_relationship_naming_an_undeclared_dataset_is_refused(tmp_path: Path):
    doc = reference_document()
    doc["semantic_model"][0]["relationships"][0]["to"] = "suppliers"
    write(tmp_path, "dangling.ossie.yaml", doc)

    result = load(tmp_path, "dangling.ossie.yaml")

    assert "unknown_dataset" in rules(result)
    assert "suppliers" in " ".join(d.message for d in result.errors)


def test_unequal_key_arity_is_refused(tmp_path: Path):
    """dex's own rule: the schema constrains neither array's length.

    A join pairs the two arrays in order, so unequal lengths describe a join no
    consumer can resolve. Carried upstream as a consumer finding.
    """

    doc = reference_document()
    doc["semantic_model"][0]["relationships"][0]["to_columns"] = [
        "customer_id",
        "country_code",
    ]
    write(tmp_path, "arity.ossie.yaml", doc)

    assert "key_arity" in rules(load(tmp_path, "arity.ossie.yaml"))


def test_insufficient_target_key_coverage_warns_rather_than_refusing(
    tmp_path: Path,
):
    """Upstream downgraded this to a warning, and dex follows.

    Refusing here would refuse documents upstream considers valid, which is
    exactly the divergence porting its judgment exists to avoid.
    """

    doc = reference_document()
    doc["semantic_model"][0]["relationships"][0]["to_columns"] = ["country_code"]
    write(tmp_path, "coverage.ossie.yaml", doc)

    result = load(tmp_path, "coverage.ossie.yaml")

    assert "key_coverage" in rules(result, WARNING)
    assert result.documents, "a warning must not withhold the document"


def test_target_key_coverage_accepts_a_superset_in_any_order(tmp_path: Path):
    """A superset of a key still guarantees the many-to-one join."""

    doc = reference_document()
    doc["semantic_model"][0]["relationships"][0]["from_columns"] = [
        "customer_id",
        "region",
    ]
    doc["semantic_model"][0]["relationships"][0]["to_columns"] = [
        "country_code",
        "customer_id",
    ]
    write(tmp_path, "superset.ossie.yaml", doc)

    assert "key_coverage" not in rules(load(tmp_path, "superset.ossie.yaml"))


def test_a_target_declaring_no_key_is_skipped_rather_than_failed(tmp_path: Path):
    """Absent keys are not evidence of anything: the fields are optional."""

    doc = reference_document()
    del doc["semantic_model"][0]["datasets"][1]["primary_key"]
    write(tmp_path, "keyless.ossie.yaml", doc)

    assert "key_coverage" not in rules(load(tmp_path, "keyless.ossie.yaml"))


def test_one_semantic_model_name_across_the_configured_set(tmp_path: Path):
    """dex's own rule, forced by how it namespaces catalog entries.

    Upstream validates one file at a time and has no reason to check this. dex
    reads a configured set together and names entries `<model>.<dataset>`, so two
    models sharing a name collapse different datasets onto one entry.
    """

    write(tmp_path, "one.ossie.yaml", reference_document())
    write(tmp_path, "two.ossie.yaml", reference_document())

    result = load(tmp_path, "one.ossie.yaml", "two.ossie.yaml")

    assert "duplicate_semantic_model_across_files" in rules(result)
    assert not result.documents, (
        "a clashing document is retracted rather than half-read: reading either "
        "one produces catalog entries that silently mean two different things"
    )


# --- layer 3: expression syntax --------------------------------------------


def test_invalid_sql_is_refused_with_the_dialect_named(tmp_path: Path):
    doc = reference_document()
    doc["semantic_model"][0]["metrics"][0]["expression"] = expression(
        ANSI_SQL="SUM(FROM WHERE"
    )
    write(tmp_path, "sql.ossie.yaml", doc)

    result = load(tmp_path, "sql.ossie.yaml")

    assert "sql_syntax" in rules(result)
    assert "ANSI_SQL" in " ".join(d.message for d in result.errors)


def test_a_bare_column_reference_parses(repo: Path):
    """The wrapped second attempt is what lets a field expression through.

    A field is routinely `order_id`, which is not a statement, so a single
    parse attempt would reject nearly every valid document.
    """

    assert "sql_syntax" not in rules(load(repo, "commerce.ossie.yaml"))


def test_a_non_sql_dialect_is_preserved_and_never_reported_as_validated(
    repo: Path,
):
    """MDX is not SQL, so dex neither parses it nor claims it checked it."""

    result = load(repo, "commerce.ossie.yaml")
    notes = [d for d in result.diagnostics if d.rule == "non_sql_dialect"]

    assert notes and notes[0].severity == NOTE
    assert "MDX" in notes[0].message
    assert not [d for d in result.diagnostics if d.severity == ERROR]


def test_absent_sql_support_is_a_named_skip_and_never_a_pass(
    tmp_path: Path, monkeypatch
):
    """The refusal posture `guards.dialect` sets, applied to a soft check.

    A skipped check that reads as a passed one is the failure this module is
    shaped to avoid, so the absence is stated on the result.
    """

    import builtins

    real = builtins.__import__

    def refuse(name, *args, **kwargs):
        if name == "sqlglot" or name.startswith("sqlglot."):
            raise ImportError("no dialect engine")
        return real(name, *args, **kwargs)

    doc = reference_document()
    doc["semantic_model"][0]["metrics"][0]["expression"] = expression(
        ANSI_SQL="SUM(FROM WHERE"
    )
    write(tmp_path, "noglot.ossie.yaml", doc)
    monkeypatch.setattr(builtins, "__import__", refuse)

    result = load(tmp_path, "noglot.ossie.yaml")

    assert "sql_unavailable" in rules(result, NOTE)
    assert "sql_syntax" not in rules(result)
    assert result.documents, "a skipped check does not withhold the document"


def test_the_missing_ossie_extra_refuses_by_name(tmp_path: Path, monkeypatch):
    """Unlike the SQL layer, structure validation has nothing to degrade to."""

    import builtins

    from exmergo_dex_core.ossie import OssieDependencyError

    real = builtins.__import__

    def refuse(name, *args, **kwargs):
        if name == "jsonschema" or name.startswith("jsonschema."):
            raise ImportError("no validator")
        return real(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", refuse)

    with pytest.raises(OssieDependencyError, match=r"exmergo-dex-core\[ossie\]"):
        load(tmp_path, "anything.ossie.yaml")


# --- diagnostics -----------------------------------------------------------


def test_a_diagnostic_names_the_file_and_the_document_path(tmp_path: Path):
    """An error a reader cannot locate is an error they cannot fix."""

    doc = reference_document()
    doc["semantic_model"][0]["datasets"][0]["fields"].append(field("order_id"))
    write(tmp_path, "where.ossie.yaml", doc)

    found = next(
        d
        for d in load(tmp_path, "where.ossie.yaml").diagnostics
        if d.rule == "duplicate_field"
    )

    assert found.file == "where.ossie.yaml"
    assert found.path == "semantic_model -> 0 -> datasets -> 0 -> fields"
    assert found.render().startswith("where.ossie.yaml: semantic_model -> 0")
