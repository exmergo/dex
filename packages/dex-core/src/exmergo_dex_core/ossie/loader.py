"""Reading a native Ossie document, and judging whether it is one.

Three layers, run in order and collecting rather than failing fast, because a
document with two problems should report two problems. They map onto three
install tiers, and that has to be explicit because the base install carries only
the canonical model and config parsing:

1. **Structure**, by JSON Schema against the bundled draft. Needs a schema
   validator, which is what the `[ossie]` extra carries.
2. **Integrity**, in pure Python: name uniqueness, relationship endpoint
   existence, key-array arity, target-key coverage. No dependency.
3. **Expression syntax**, through sqlglot behind the existing `[sql]` extra.
   Absent, it degrades to a named skip and never to a silent pass, following the
   posture `guards.dialect` already sets.

Layer 2 ports the judgment in upstream's `validation/validate.py` rather than
reinventing it. That script is Apache-2.0 and is a script rather than a package,
so porting creates no runtime dependency and, more importantly, no divergence
about what a valid Ossie document is. Two rules here are dex's own and are
marked as such below; both are findings worth carrying back upstream.

**The assurance boundary, stated rather than implied.** There is no external
validator here the way `dbt parse` is for dbt: neither `apache-ossie` nor
`apache-ossie-dbt` is published, and there is no Ossie runtime to load a
document into. What this proves is that a document is schema-valid and
internally consistent. It does not prove that any consumer can execute it, and
no surface built on it may suggest otherwise.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any

import yaml

from ..errors import DexError
from .dialects import NON_SQL_DIALECTS, SQL_DIALECTS, sqlglot_dialect

__all__ = [
    "DOCUMENT_SUFFIXES",
    "SCHEMA_SHA256",
    "Diagnostic",
    "LoadResult",
    "LoadedDocument",
    "OssieDependencyError",
    "load_documents",
    "schema_bytes",
]

#: The sha256 of the bundled upstream schema. Asserted by a test, so a
#: regeneration is a reviewed diff rather than a quiet update. See
#: `schema/PROVENANCE.md` for the upstream commit and the upgrade procedure.
SCHEMA_SHA256 = "27aab111647b1e8d2229a2413e4682e459f43edc62e0eaadd497317754089e42"

#: The file extensions a native Ossie document may carry. JSON and YAML are the
#: two serializations of one document shape, so both parse to the same mapping
#: and everything downstream is identical.
DOCUMENT_SUFFIXES = (".ossie.yaml", ".ossie.yml", ".ossie.json")

# Severity vocabulary. `error` means dex will not read the document as a semantic
# layer. `warning` means it read it and something in it is questionable, which is
# the level upstream chose for insufficient target-key coverage and the level dex
# keeps for it. `note` means dex declined to check something, and it exists so a
# skipped check can never be mistaken for a passed one.
ERROR = "error"
WARNING = "warning"
NOTE = "note"


class OssieDependencyError(DexError):
    """The `[ossie]` extra is not installed.

    Its own error, and raised rather than degraded around, for the reason
    `guards.dialect.DialectDependencyError` is: a document dex cannot validate is
    a document dex cannot promise is well formed, and reading one anyway would
    put unvalidated declarations behind a surface that reads as validated. This
    module stays importable with `jsonschema` absent, which is why the import
    lives inside the check.
    """


@dataclass(frozen=True)
class Diagnostic:
    """One thing wrong with, or unchecked in, a document.

    ``path`` is the document path the problem sits at, rendered the way upstream
    renders it (`semantic_model -> 0 -> datasets -> 1 -> name`), or `(root)`.
    ``layer`` and ``rule`` say which check spoke, so a caller can act on a class
    of finding without matching on prose.
    """

    file: str
    path: str
    layer: str
    rule: str
    severity: str
    message: str

    def render(self) -> str:
        """One line, for a note list or an exception message."""

        return f"{self.file}: {self.path}: [{self.rule}] {self.message}"


@dataclass(frozen=True)
class LoadedDocument:
    """One document that parsed, with the file it came from."""

    file: str
    data: dict[str, Any]


@dataclass
class LoadResult:
    """Every configured document dex could read, and everything wrong with them.

    ``documents`` holds only documents that passed structure and integrity, so a
    caller normalizing them never has to re-check shape. A document with an
    error is absent from it and present in ``diagnostics``, which is what keeps
    "this could not be read" from looking like "this declares nothing".
    """

    documents: list[LoadedDocument] = field(default_factory=list)
    diagnostics: list[Diagnostic] = field(default_factory=list)

    @property
    def errors(self) -> list[Diagnostic]:
        return [d for d in self.diagnostics if d.severity == ERROR]

    def notes(self) -> list[str]:
        """Every diagnostic as a line, for a caller's note list."""

        return [d.render() for d in self.diagnostics]


def schema_bytes() -> bytes:
    """The bundled schema's bytes, read from the installed package.

    Through `importlib.resources` rather than `__file__`, so it works from a
    wheel, a zipapp, and an editable checkout alike. This is the read the hash
    constant is asserted against, so it returns bytes rather than a parsed
    object: a hash of a re-serialized dict would pin dex's serializer rather than
    upstream's file.
    """

    asset = resources.files("exmergo_dex_core.ossie") / "schema" / "ossie-schema.json"
    return asset.read_bytes()


def schema_sha256() -> str:
    """The bundled schema's sha256, as recorded in :data:`SCHEMA_SHA256`."""

    return hashlib.sha256(schema_bytes()).hexdigest()


def ensure_available() -> None:
    """Refuse by name when the `[ossie]` extra is absent.

    Never a fallback to a weaker check. Structure validation is what stands
    between an authored file and dex treating it as a semantic layer, and there
    is no second validator to fall back to.
    """

    try:
        import jsonschema  # noqa: F401
    except ImportError as exc:
        raise OssieDependencyError(
            "reading native Apache Ossie documents needs a JSON Schema validator, "
            "which is not installed. Install the extra that carries it:\n"
            '  pip install "exmergo-dex-core[ossie]"\n'
            "dex validates a document's structure against the Ossie schema it "
            "pins before reading it, and there is no weaker check to fall back "
            "to: an unvalidated document read as a semantic layer would put "
            "unchecked declarations behind a surface that reads as validated."
        ) from exc


def load_documents(
    repo_root: Path | str,
    files: Sequence[str],
    *,
    connector: str | None = None,
) -> LoadResult:
    """Read, validate, and return every configured document.

    Never raises for a bad document: a caller on the tier-1 channel may not
    raise, and a caller on the catalog channel wants to decide for itself
    whether an error is fatal. The one thing that does raise is the missing
    extra, because that is a wiring problem rather than a document problem and no
    part of the answer would be trustworthy without it.
    """

    ensure_available()
    root = Path(repo_root).resolve()
    result = LoadResult()
    for name in files:
        path, refusal = _resolve(root, name)
        if refusal is not None:
            result.diagnostics.append(refusal)
            continue
        document, parse_errors = _parse(path, name)
        result.diagnostics.extend(parse_errors)
        if document is None:
            continue
        found = list(_validate_structure(document, name))
        if not any(d.severity == ERROR for d in found):
            found.extend(_validate_integrity(document, name))
        if not any(d.severity == ERROR for d in found):
            found.extend(_validate_expressions(document, name, connector))
        result.diagnostics.extend(found)
        if not any(d.severity == ERROR for d in found):
            result.documents.append(LoadedDocument(file=name, data=document))
    clashing, across = _validate_across_documents(result.documents)
    result.diagnostics.extend(across)
    # A name clash retracts **every** document declaring the clashing model, not
    # only the one reported second. Reading either produces catalog entries that
    # silently mean two different things, so which file the reader happened to
    # configure first is not a reason to trust it.
    if clashing:
        result.documents = [
            d for d in result.documents if not clashing.intersection(_model_names(d))
        ]
    return result


def _resolve(root: Path, name: str) -> tuple[Path | None, Diagnostic | None]:
    """The configured name as a path inside the repository, or a refusal.

    Confinement is checked on the *resolved* path, so a symlink pointing out of
    the repository is caught along with a `..` that walks out of it. Writes are
    confined to the repo everywhere in dex; a read that leaves it would let a
    committed config file name `/etc/passwd` and have dex parse it.
    """

    def refusal(rule: str, message: str) -> tuple[None, Diagnostic]:
        return None, Diagnostic(
            file=name,
            path="(config)",
            layer="coordinates",
            rule=rule,
            severity=ERROR,
            message=message,
        )

    if not name.endswith(DOCUMENT_SUFFIXES):
        listed = ", ".join(DOCUMENT_SUFFIXES)
        return refusal(
            "suffix",
            f"a native Ossie document is named {listed}; '{name}' is not",
        )
    candidate = Path(name)
    if candidate.is_absolute():
        return refusal(
            "confinement",
            "an Ossie document is named relative to the repository root, and "
            f"'{name}' is an absolute path",
        )
    resolved = (root / candidate).resolve()
    try:
        resolved.relative_to(root)
    except ValueError:
        return refusal(
            "confinement",
            f"'{name}' resolves outside the repository ({resolved}); dex reads "
            "native Ossie documents only from within the repository it was "
            "pointed at",
        )
    if not resolved.is_file():
        return refusal(
            "missing",
            f"no such Ossie document: '{name}' (looked in {root})",
        )
    return resolved, None


def _parse(path: Path, name: str) -> tuple[dict[str, Any] | None, list[Diagnostic]]:
    """Parse one file to a mapping, or say why not.

    `yaml.YAMLError` descends from `Exception` rather than `ValueError`, which is
    a trap this repository has already been caught by once, so it is caught by
    name here rather than by ancestry.
    """

    def refusal(rule: str, message: str) -> tuple[None, list[Diagnostic]]:
        return None, [
            Diagnostic(
                file=name,
                path="(root)",
                layer="parse",
                rule=rule,
                severity=ERROR,
                message=message,
            )
        ]

    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return refusal("unreadable", f"could not read the file: {exc}")
    try:
        raw = json.loads(text) if path.name.endswith(".json") else yaml.safe_load(text)
    except (ValueError, yaml.YAMLError) as exc:
        kind = "JSON" if path.name.endswith(".json") else "YAML"
        return refusal("malformed", f"not valid {kind}: {exc}")
    if not isinstance(raw, dict):
        return refusal(
            "root",
            "an Ossie document is a mapping with `version` and `semantic_model` "
            f"at its root, and this parsed as {type(raw).__name__}",
        )
    return raw, []


def _validate_structure(document: dict[str, Any], name: str) -> Iterable[Diagnostic]:
    """Layer 1: the bundled JSON Schema.

    The schema does three jobs at once here. It checks shape and types; it
    enforces the pinned `version` constant, which upstream declares as a `const`
    so the document's own version is checked without dex writing that check; and
    it rejects unknown structural keys, because every object in the schema
    except structured AI context sets `additionalProperties: false`.

    An unknown key is reported as **incompatible with the pinned draft**, and the
    wording is deliberate: it may be a typo or it may be a key from a newer draft,
    dex cannot tell which, and a message that guesses sends the reader to the
    wrong fix half the time.
    """

    from jsonschema import Draft202012Validator

    validator = Draft202012Validator(json.loads(schema_bytes()))
    for error in validator.iter_errors(document):
        path = _render_path(error.absolute_path)
        if error.validator == "additionalProperties":
            yield Diagnostic(
                file=name,
                path=path,
                layer="structure",
                rule="unknown_key",
                severity=ERROR,
                message=(
                    f"{error.message}. That key is not in the Ossie schema dex "
                    "pins, so this document is incompatible with it. It may be a "
                    "typo or it may be authored for a newer draft; dex cannot "
                    "tell which. See the pinned draft in "
                    "exmergo_dex_core/ossie/schema/PROVENANCE.md"
                ),
            )
            continue
        if error.validator == "const" and list(error.absolute_path) == ["version"]:
            yield Diagnostic(
                file=name,
                path=path,
                layer="structure",
                rule="version",
                severity=ERROR,
                message=(
                    f"{error.message}. dex pins one Ossie schema by content and "
                    "reads documents declaring the version that schema states"
                ),
            )
            continue
        yield Diagnostic(
            file=name,
            path=path,
            layer="structure",
            rule=error.validator or "schema",
            severity=ERROR,
            message=error.message,
        )


def _validate_integrity(document: dict[str, Any], name: str) -> Iterable[Diagnostic]:
    """Layer 2: what the schema cannot say, in pure Python.

    Reached only after layer 1 passed, so the shapes are known good and this can
    read them directly. Everything here reports and nothing repairs: a
    declaration dex rewrites is a declaration its author no longer controls.
    """

    for index, model in enumerate(document.get("semantic_model") or []):
        base = f"semantic_model -> {index}"
        model_name = model.get("name", "<unnamed>")
        datasets = model.get("datasets") or []
        by_name = {d["name"]: d for d in datasets if d.get("name")}

        yield from _duplicates(
            [d.get("name") for d in datasets],
            file=name,
            path=f"{base} -> datasets",
            rule="duplicate_dataset",
            what="dataset",
            where=f"semantic model '{model_name}'",
        )
        for position, dataset in enumerate(datasets):
            yield from _duplicates(
                [f.get("name") for f in dataset.get("fields") or []],
                file=name,
                path=f"{base} -> datasets -> {position} -> fields",
                rule="duplicate_field",
                what="field",
                where=f"dataset '{dataset.get('name', '<unnamed>')}'",
            )
        yield from _duplicates(
            [m.get("name") for m in model.get("metrics") or []],
            file=name,
            path=f"{base} -> metrics",
            rule="duplicate_metric",
            what="metric",
            where=f"semantic model '{model_name}'",
        )
        yield from _duplicates(
            [r.get("name") for r in model.get("relationships") or []],
            file=name,
            path=f"{base} -> relationships",
            rule="duplicate_relationship",
            what="relationship",
            where=f"semantic model '{model_name}'",
        )

        for position, rel in enumerate(model.get("relationships") or []):
            path = f"{base} -> relationships -> {position}"
            rel_name = rel.get("name", "<unnamed>")
            for side in ("from", "to"):
                referenced = rel.get(side)
                if referenced not in by_name:
                    yield Diagnostic(
                        file=name,
                        path=f"{path} -> {side}",
                        layer="integrity",
                        rule="unknown_dataset",
                        severity=ERROR,
                        message=(
                            f"relationship '{rel_name}' names dataset "
                            f"'{referenced}', which semantic model "
                            f"'{model_name}' does not declare"
                        ),
                    )
            from_columns = rel.get("from_columns") or []
            to_columns = rel.get("to_columns") or []
            # dex's own rule, not upstream's. The schema constrains both arrays
            # to be non-empty and says nothing about their lengths, but a join
            # pairs them positionally, so unequal arity is a relationship no
            # consumer can resolve. Reported, never repaired, and carried
            # upstream as a consumer finding.
            if len(from_columns) != len(to_columns):
                yield Diagnostic(
                    file=name,
                    path=path,
                    layer="integrity",
                    rule="key_arity",
                    severity=ERROR,
                    message=(
                        f"relationship '{rel_name}' pairs "
                        f"{len(from_columns)} from_columns with "
                        f"{len(to_columns)} to_columns. A join matches them in "
                        "order, so the two arrays have to be the same length"
                    ),
                )
            target = by_name.get(rel.get("to"))
            if target is not None and to_columns:
                yield from _coverage(
                    target,
                    to_columns,
                    file=name,
                    path=path,
                    rel_name=rel_name,
                    model_name=model_name,
                )


def _coverage(
    dataset: dict[str, Any],
    to_columns: list[str],
    *,
    file: str,
    path: str,
    rel_name: str,
    model_name: str,
) -> Iterable[Diagnostic]:
    """Whether `to_columns` covers a declared key of the target dataset.

    Upstream's semantics exactly, and they are subtler than they look. Coverage
    means superset, not equality, because a superset of a key still guarantees
    the many-to-one join. Column order is irrelevant, because a key is a set. A
    dataset declaring no key at all is skipped rather than failed, because the
    key fields are optional and their absence is not evidence of anything. And
    it is a **warning**: upstream recently downgraded it, and a consumer that
    refused here would refuse documents upstream considers valid.
    """

    candidates = [dataset.get("primary_key"), *(dataset.get("unique_keys") or [])]
    declared = [k for k in candidates if isinstance(k, list) and k]
    if not declared:
        return
    wanted = set(to_columns)
    if any(set(key) <= wanted for key in declared):
        return
    yield Diagnostic(
        file=file,
        path=path,
        layer="integrity",
        rule="key_coverage",
        severity=WARNING,
        message=(
            f"relationship '{rel_name}' in semantic model '{model_name}' joins "
            f"to_columns {to_columns} on dataset "
            f"'{dataset.get('name', '<unnamed>')}', which do not cover its "
            "primary key or any of its unique keys. The join may fan out"
        ),
    )


def _validate_expressions(
    document: dict[str, Any], name: str, connector: str | None
) -> Iterable[Diagnostic]:
    """Layer 3: does each SQL expression parse.

    Only the SQL dialects are parsed. `MDX`, `TABLEAU` and `MAQL` are preserved
    verbatim, never handed to a SQL parser, and never reported as validated: a
    skipped check that reads as a passed one is the failure mode this whole
    module is shaped to avoid, so their skip is stated as a note.
    """

    try:
        import sqlglot
        from sqlglot.errors import ParseError, TokenError
    except ImportError:
        yield Diagnostic(
            file=name,
            path="(root)",
            layer="expressions",
            rule="sql_unavailable",
            severity=NOTE,
            message=(
                "expression syntax was not checked: the dialect engine is not "
                'installed. Install it with pip install "exmergo-dex-core[sql]" '
                "(every connector extra already carries it). The document's "
                "structure and internal consistency were checked"
            ),
        )
        return

    def parses(expression: str, dialect: str) -> str | None:
        """Upstream's two-attempt parse: bare, then wrapped in a SELECT."""

        target = sqlglot_dialect(dialect)
        failure = ""
        # A field expression is often a bare column reference, which is not a
        # statement, so the wrapped attempt is what lets `order_total` through.
        # Only ParseError and TokenError are caught: anything else out of the
        # dialect engine is a bug there rather than a bad expression here.
        for candidate in (expression, f"SELECT {expression}"):
            try:
                sqlglot.parse_one(candidate, dialect=target)
            except (ParseError, TokenError) as exc:
                failure = str(exc).splitlines()[0]
                continue
            return None
        return failure

    skipped: set[str] = set()
    for path, label, expression in _expressions(document):
        dialects = (expression or {}).get("dialects") or []
        for position, entry in enumerate(dialects):
            dialect = entry.get("dialect")
            text = entry.get("expression")
            if not text:
                continue
            if dialect in NON_SQL_DIALECTS:
                skipped.add(dialect)
                continue
            if dialect not in SQL_DIALECTS:
                continue
            failure = parses(text, dialect)
            if failure is not None:
                yield Diagnostic(
                    file=name,
                    path=f"{path} -> expression -> dialects -> {position}",
                    layer="expressions",
                    rule="sql_syntax",
                    severity=ERROR,
                    message=f"{label} does not parse as {dialect}: {failure}",
                )
    if skipped:
        listed = ", ".join(sorted(skipped))
        yield Diagnostic(
            file=name,
            path="(root)",
            layer="expressions",
            rule="non_sql_dialect",
            severity=NOTE,
            message=(
                f"{listed} expressions are preserved as written and were not "
                "checked: they are not SQL, so dex neither parses them nor reads "
                "a physical column out of one"
            ),
        )


def _expressions(
    document: dict[str, Any],
) -> Iterable[tuple[str, str, dict[str, Any]]]:
    """Every expression in the document, as ``(path, label, expression)``."""

    for index, model in enumerate(document.get("semantic_model") or []):
        base = f"semantic_model -> {index}"
        model_name = model.get("name", "<unnamed>")
        for position, dataset in enumerate(model.get("datasets") or []):
            dataset_name = dataset.get("name", "<unnamed>")
            for offset, field_ in enumerate(dataset.get("fields") or []):
                yield (
                    f"{base} -> datasets -> {position} -> fields -> {offset}",
                    f"field '{dataset_name}.{field_.get('name', '<unnamed>')}' "
                    f"in semantic model '{model_name}'",
                    field_.get("expression") or {},
                )
        for offset, metric in enumerate(model.get("metrics") or []):
            yield (
                f"{base} -> metrics -> {offset}",
                f"metric '{metric.get('name', '<unnamed>')}' in semantic model "
                f"'{model_name}'",
                metric.get("expression") or {},
            )


def _model_names(document: LoadedDocument) -> set[str]:
    """Every semantic-model name one document declares."""

    return {
        model["name"]
        for model in document.data.get("semantic_model") or []
        if isinstance(model.get("name"), str)
    }


def _validate_across_documents(
    documents: Sequence[LoadedDocument],
) -> tuple[set[str], list[Diagnostic]]:
    """dex's own rule: one semantic-model name across every configured document.

    Returns the clashing names and one diagnostic per clash. Upstream checks
    uniqueness *within* a semantic model and has no reason to go further,
    because upstream validates one file at a time. dex reads a configured set of
    them together and namespaces every catalog name as
    `<semantic_model>.<dataset>`, so two models sharing a name collapse two
    different datasets onto one catalog entry. Also carried upstream as a
    consumer finding.
    """

    declared_in: dict[str, list[str]] = {}
    for document in documents:
        for model_name in sorted(_model_names(document)):
            declared_in.setdefault(model_name, []).append(document.file)
    clashing = {name for name, files in declared_in.items() if len(files) > 1}
    found = [
        Diagnostic(
            file=declared_in[name][1],
            path="semantic_model",
            layer="integrity",
            rule="duplicate_semantic_model_across_files",
            severity=ERROR,
            message=(
                f"semantic model '{name}' is declared in more than one "
                f"configured document ({', '.join(declared_in[name])}). dex "
                "names catalog entries `<semantic model>.<dataset>`, so the two "
                "would collapse onto the same entries; neither is read"
            ),
        )
        for name in sorted(clashing)
    ]
    return clashing, found


def _duplicates(
    names: Iterable[Any], *, file: str, path: str, rule: str, what: str, where: str
) -> Iterable[Diagnostic]:
    """One diagnostic per repeated name, in first-repeat order."""

    seen: set[str] = set()
    reported: set[str] = set()
    for name in names:
        if not isinstance(name, str):
            continue
        if name in seen and name not in reported:
            reported.add(name)
            yield Diagnostic(
                file=file,
                path=path,
                layer="integrity",
                rule=rule,
                severity=ERROR,
                message=f"duplicate {what} name '{name}' in {where}",
            )
        seen.add(name)


def _render_path(path: Iterable[Any]) -> str:
    """A schema error's location, rendered the way upstream renders it."""

    parts = [str(part) for part in path]
    return " -> ".join(parts) if parts else "(root)"
