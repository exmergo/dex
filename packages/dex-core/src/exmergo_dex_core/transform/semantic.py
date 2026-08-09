"""Semantic-layer authoring: dbt semantic models / MetricFlow YAML as plan edits.

``define`` and ``update`` are plan producers into the same store as model SQL;
``transform apply`` writes them into the project. The engine validates the YAML
the agent authored; MetricFlow's own schemas (via ``dbt-semantic-interfaces``,
pulled in by dbt-duckdb) are the validator of record, with a structural fallback
when that package is absent so validation degrades to a warning, never to silence.

A payload arrives in one of two units. A **whole file** is the original: the
caller sends the complete new content for a path. A **definition** names one
semantic model or metric and nothing else; :func:`splice_definitions` resolves a
set of those into whole-file content, so everything downstream (the plan store,
the diffs, the conflict hashing, ``transform apply``) sees the unit it always
has. The definition unit exists because the whole-file one forces a caller
adding two metrics to restate the twenty-seven it is not touching, which buries
the real change in the review and puts twenty-seven hand-copied definitions at
risk of a typo that only ``dbt parse`` would catch.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from typing import Any, NamedTuple

import yaml

from ..dbt_project import DbtProjectView
from .validate import EditValidationError


def validate_semantic_yaml(path: str, parsed: dict[str, Any]) -> list[str]:
    """Validate one semantic YAML document. Returns warnings; raises on failure."""

    semantic_models = parsed.get("semantic_models") or []
    metrics = parsed.get("metrics") or []
    if not semantic_models and not metrics:
        raise EditValidationError(
            f"{path}: semantic YAML must declare semantic_models or metrics"
        )

    try:
        from dbt_semantic_interfaces.parsing.schemas import (
            metric_validator,
            semantic_model_validator,
        )
    except ImportError:
        _structural_check(path, semantic_models, metrics)
        return [
            "dbt-semantic-interfaces is not installed; semantic YAML got a "
            "structural check only"
        ]

    for entry in semantic_models:
        if not isinstance(entry, dict) or not entry.get("name"):
            raise EditValidationError(f"{path}: semantic model entry needs a name")
        if not entry.get("model"):
            raise EditValidationError(
                f"{path}: semantic model '{entry['name']}' needs a model: ref(...)"
            )
        # dbt schema files say `model: ref(...)`; MetricFlow's schema speaks the
        # compiled `node_relation` instead, so validate the entry minus `model`.
        body = {k: v for k, v in entry.items() if k != "model"}
        _validate_with(
            semantic_model_validator, body, path, f"semantic model '{entry['name']}'"
        )
    for entry in metrics:
        if not isinstance(entry, dict) or not entry.get("name"):
            raise EditValidationError(f"{path}: metric entry needs a name")
        _validate_with(metric_validator, entry, path, f"metric '{entry['name']}'")
    return []


class SemanticNamespace(NamedTuple):
    """Every name the semantic layer can address, split by role.

    ``metrics`` includes the implicit metric a ``create_metric: true`` measure
    declares: dbt's parser sees those even though no ``metrics:`` entry exists,
    so collision checks and reference resolution must see them too.
    """

    semantic_models: set[str]
    metrics: set[str]
    measures: set[str]


def collect_namespace(documents: Iterable[Any]) -> SemanticNamespace:
    """Gather the semantic namespace declared by the given parsed YAML documents."""

    semantic_models: set[str] = set()
    metrics: set[str] = set()
    measures: set[str] = set()
    for parsed in documents:
        if not isinstance(parsed, dict):
            continue
        for entry in parsed.get("semantic_models") or []:
            if not isinstance(entry, dict) or not entry.get("name"):
                continue
            semantic_models.add(entry["name"])
            for measure in entry.get("measures") or []:
                if not isinstance(measure, dict) or not measure.get("name"):
                    continue
                measures.add(measure["name"])
                if measure.get("create_metric"):
                    metrics.add(measure["name"])
        for entry in parsed.get("metrics") or []:
            if isinstance(entry, dict) and entry.get("name"):
                metrics.add(entry["name"])
    return SemanticNamespace(semantic_models, metrics, measures)


def project_namespace(view: DbtProjectView) -> SemanticNamespace:
    """The semantic namespace already declared across the project's YAML files."""

    documents = []
    for source in view.files.values():
        if not source.path.endswith((".yml", ".yaml")):
            continue
        try:
            parsed = yaml.safe_load(source.content)
        except yaml.YAMLError:
            continue  # a broken hand-written file is not this command's problem
        documents.append(parsed)
    return collect_namespace(documents)


def existing_semantic_names(view: DbtProjectView) -> set[str]:
    """Names of semantic models and metrics already declared in the project."""

    ns = project_namespace(view)
    return ns.semantic_models | ns.metrics


# A definition is addressed by role and name, never by name alone. The two
# namespaces are merged for collision checks (dbt resolves a reference against
# both), but a metric and a semantic model that happen to share a name are
# different objects, and comparing one against the other would report a change
# that is really a category error.
DefinitionKey = tuple[str, str]


def project_definitions(
    view: DbtProjectView,
) -> dict[DefinitionKey, tuple[str, dict[str, Any]]]:
    """Every top-level definition the project declares, keyed by role and name.

    The value is the file that declares it and its parsed body, which is what
    makes "did this edit actually change anything" answerable. Measures are not
    included: they are addressed inside their semantic model, so the model's own
    comparison already covers them.

    A duplicate name across two files is the project's own bug and dbt will
    refuse it; whichever file is read last wins here, because there is no right
    answer and this function is not the place to raise about it.
    """

    definitions: dict[DefinitionKey, tuple[str, dict[str, Any]]] = {}
    for source in view.files.values():
        if not source.path.endswith((".yml", ".yaml")):
            continue
        try:
            parsed = yaml.safe_load(source.content)
        except yaml.YAMLError:
            continue  # a broken hand-written file is not this command's problem
        if not isinstance(parsed, dict):
            continue
        for kind, key in (("semantic_model", "semantic_models"), ("metric", "metrics")):
            for entry in parsed.get(key) or []:
                if isinstance(entry, dict) and entry.get("name"):
                    definitions[(kind, entry["name"])] = (source.path, entry)
    return definitions


def time_spine_warning(
    view: DbtProjectView, parsed_edits: list[dict[str, Any]]
) -> str | None:
    """Warn when semantic YAML lands in a project with no MetricFlow time spine.

    dbt refuses to parse a project that has semantic models but no time spine
    model, so a first semantic plan applied without one produces a project that
    cannot build. The engine cannot author the model itself (propose-don't-impose), so
    it warns at plan time instead of letting the build be the first signal.
    """

    def declares_spine(parsed: Any) -> bool:
        if not isinstance(parsed, dict):
            return False
        return any(
            isinstance(entry, dict) and "time_spine" in entry
            for entry in parsed.get("models") or []
        )

    if any(declares_spine(parsed) for parsed in parsed_edits):
        return None
    for source in view.files.values():
        if not source.path.endswith((".yml", ".yaml")):
            continue
        try:
            parsed = yaml.safe_load(source.content)
        except yaml.YAMLError:
            continue
        if declares_spine(parsed):
            return None
    return (
        "the project has no MetricFlow time spine; dbt cannot parse semantic "
        "models without one. Author a day-grain date model (for example "
        "models/metricflow_time_spine.sql) plus its YAML with a `time_spine:` "
        "config before building"
    )


def check_mode(
    mode: str,
    parsed_edits: Sequence[tuple[str, dict[str, Any]]],
    view: DbtProjectView,
    *,
    scope: set[DefinitionKey] | None = None,
) -> dict[str, list[str]]:
    """Classify every proposed name; enforce the strict modes.

    ``define`` refuses existing names and ``update`` refuses new ones (both are
    typo guards); ``plan`` accepts a mix, so one payload can evolve existing
    definitions and add the helpers they depend on.

    Three classes, not two: ``defined`` is new to the project, ``updated``
    genuinely differs from what is on disk, and ``unchanged`` is re-stated
    content identical to the current definition in the same file. The third
    class exists because the whole-file edit unit makes re-stating the untouched
    definitions the normal way to add one, so name membership alone reports a
    two-object change as a thirty-object one and buries the real blast radius in
    the place a reviewer looks to confirm it.

    Equality is over the parsed structure, so key order and formatting are not
    changes but list order is: a reordered ``dimensions:`` block is a real diff
    and reads as one. The same-file requirement is deliberate. Identical content
    landing in a different file is a move, the diff for it is real, and calling
    that unchanged would be the same lie pointing the other way.

    ``parsed_edits`` pairs each edit's project-relative path with its parsed
    document, because a definition's identity here includes where it lives.

    ``scope`` narrows the classification to definitions the caller actually
    named. A per-definition payload is lowered to whole-file edits before it
    gets here, so without it the file's other definitions would be classified
    too, and a two-definition payload would report the file's other twenty-five
    as ``unchanged``: a quieter version of the noise this class exists to remove.
    """

    existing = existing_semantic_names(view)
    on_disk = project_definitions(view)

    proposed: set[str] = set()
    # Tracked per name rather than per role, because the envelope classifies
    # names: one name can be proposed as both a semantic model and a metric, and
    # a change to either half is a change to the name.
    matched: set[str] = set()
    differed: set[str] = set()
    for path, parsed in parsed_edits:
        if not isinstance(parsed, dict):
            continue
        for kind, key in (("semantic_model", "semantic_models"), ("metric", "metrics")):
            for entry in parsed.get(key) or []:
                if not isinstance(entry, dict) or not entry.get("name"):
                    continue
                name = entry["name"]
                if scope is not None and (kind, name) not in scope:
                    continue
                proposed.add(name)
                current = on_disk.get((kind, name))
                if current is None:
                    # No counterpart in this role. Either the name is new (it
                    # lands in `defined`) or it exists only in the other role,
                    # which makes this entry an addition, so nothing to match.
                    continue
                (matched if current == (path, entry) else differed).add(name)

    if mode == "define":
        clashes = sorted(proposed & existing)
        if clashes:
            raise EditValidationError(
                f"already defined in the project: {', '.join(clashes)}; use "
                "`semantic update` to evolve an existing definition, or "
                "`semantic plan` to mix new and existing names"
            )
    elif mode == "update":
        missing = sorted(proposed - existing)
        if missing:
            raise EditValidationError(
                f"not defined in the project: {', '.join(missing)}; use "
                "`semantic define` to add a new definition, or "
                "`semantic plan` to mix new and existing names"
            )

    # Unchanged means every proposed entry for the name matched. One half of a
    # dual-role name differing makes the name updated, so `differed` subtracts.
    evolving = proposed & existing
    unchanged = (matched - differed) & evolving
    return {
        "defined": sorted(proposed - existing),
        "updated": sorted(evolving - unchanged),
        "unchanged": sorted(unchanged),
    }


def check_references(parsed_edits: list[dict[str, Any]], view: DbtProjectView) -> None:
    """Resolve every metric input against the merged namespace (project plus
    the proposed edits, so references across edits in one payload work).

    The jsonschema validation is shape-only: a metric whose input names nothing
    real still passes it and then fails ``dbt build`` two commands later. The
    load-bearing rule is dbt's: ratio and derived inputs reference *metrics*,
    not measures, and a measure only becomes a metric via ``create_metric``.
    All problems are collected and raised together so one round-trip fixes the
    payload.
    """

    project_ns = project_namespace(view)
    proposed_ns = collect_namespace(parsed_edits)
    metrics = project_ns.metrics | proposed_ns.metrics
    measures = project_ns.measures | proposed_ns.measures

    problems: list[str] = []

    def metric_ref(metric_name: str, role: str, value: Any) -> None:
        name = _ref_name(value)
        if name is None or name in metrics:
            return
        if name in measures:
            problems.append(
                f"metric '{metric_name}': {role} '{name}' is a measure, not a "
                "metric; add create_metric: true to the measure or reference "
                "a metric"
            )
        else:
            problems.append(
                f"metric '{metric_name}': {role} references unknown metric '{name}'"
            )

    def measure_ref(metric_name: str, role: str, value: Any) -> None:
        name = _ref_name(value)
        if name is not None and name not in measures:
            problems.append(
                f"metric '{metric_name}': {role} references unknown measure '{name}'"
            )

    for parsed in parsed_edits:
        if not isinstance(parsed, dict):
            continue
        for entry in parsed.get("metrics") or []:
            if not isinstance(entry, dict) or not entry.get("name"):
                continue
            name = entry["name"]
            metric_type = str(entry.get("type", "")).lower()
            params = entry.get("type_params")
            if not isinstance(params, dict):
                continue
            if metric_type in {"simple", "cumulative"}:
                measure_ref(name, "measure", params.get("measure"))
            elif metric_type == "ratio":
                metric_ref(name, "numerator", params.get("numerator"))
                metric_ref(name, "denominator", params.get("denominator"))
            elif metric_type == "derived":
                for input_metric in params.get("metrics") or []:
                    metric_ref(name, "input metric", input_metric)
            elif metric_type == "conversion":
                conversion = params.get("conversion_type_params")
                if isinstance(conversion, dict):
                    measure_ref(name, "base_measure", conversion.get("base_measure"))
                    measure_ref(
                        name, "conversion_measure", conversion.get("conversion_measure")
                    )
            # Unknown metric types are left to the schema validator and dbt.
    if problems:
        raise EditValidationError("; ".join(problems))


def _ref_name(value: Any) -> str | None:
    """A metric/measure input is either a bare name or {name: ...}."""

    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        name = value.get("name")
        return name if isinstance(name, str) else None
    return None


def _validate_with(
    validator: Any, document: dict[str, Any], path: str, label: str
) -> None:
    try:
        validator.validate(document)
    except Exception as exc:
        first_line = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
        raise EditValidationError(f"{path}: {label}: {first_line}") from exc


def _structural_check(
    path: str, semantic_models: list[Any], metrics: list[Any]
) -> None:
    for entry in semantic_models:
        if (
            not isinstance(entry, dict)
            or not entry.get("name")
            or not entry.get("model")
        ):
            raise EditValidationError(
                f"{path}: each semantic model needs at least name and model"
            )
    for entry in metrics:
        if (
            not isinstance(entry, dict)
            or not entry.get("name")
            or not entry.get("type")
            or "type_params" not in entry
        ):
            raise EditValidationError(
                f"{path}: each metric needs at least name, type, and type_params"
            )


# --- the definition edit unit -------------------------------------------------

_TOP_LEVEL_KEY = {"semantic_model": "semantic_models", "metric": "metrics"}


class DefinitionEdit(NamedTuple):
    """One semantic model or metric, authored on its own.

    ``content`` is the entry's body as a YAML mapping, not a list item: the
    caller writes ``name: revenue`` at column zero and the splice indents it to
    match its siblings. ``path`` may be ``None`` for a definition the project
    already declares, in which case it is rewritten where it already lives.
    """

    kind: str
    name: str
    content: str
    parsed: dict[str, Any]
    path: str | None = None


def parse_definition_payload(entries: Iterable[Any]) -> list[DefinitionEdit]:
    """Read the ``definitions`` payload into typed edits.

    The name is read from the content rather than declared beside it, so the
    two cannot disagree.
    """

    parsed_entries: list[DefinitionEdit] = []
    for index, entry in enumerate(entries):
        where = f"definitions[{index}]"
        if not isinstance(entry, dict):
            raise EditValidationError(f"{where}: each definition must be an object")
        kind = entry.get("kind")
        if kind not in _TOP_LEVEL_KEY:
            raise EditValidationError(
                f"{where}: kind must be one of {', '.join(sorted(_TOP_LEVEL_KEY))}, "
                f"got {kind!r}"
            )
        content = entry.get("content")
        if not isinstance(content, str) or not content.strip():
            raise EditValidationError(f"{where}: needs YAML content")
        try:
            body = yaml.safe_load(content)
        except yaml.YAMLError as exc:
            raise EditValidationError(f"{where}: invalid YAML: {exc}") from exc
        if not isinstance(body, dict) or not body.get("name"):
            raise EditValidationError(
                f"{where}: content must be a YAML mapping with a name; write the "
                "definition's body alone, without the leading '- '"
            )
        path = entry.get("path")
        if path is not None and not isinstance(path, str):
            raise EditValidationError(f"{where}: path must be a string")
        parsed_entries.append(DefinitionEdit(kind, body["name"], content, body, path))
    return parsed_entries


def splice_definitions(
    definitions: Sequence[DefinitionEdit], view: DbtProjectView
) -> list[tuple[str, str]]:
    """Lower per-definition edits into whole-file content, one entry per path.

    Every byte outside the definitions being written is preserved, which is the
    whole point: a YAML round trip through ``safe_dump`` would reformat the file
    and strip the comments a hand-written semantic layer depends on, producing a
    larger diff than the whole-file payload this unit replaces.

    Refuses rather than guesses. A file whose structure the line scanner cannot
    span safely is reported with ``--edits-file`` named as the way to edit it,
    and the spliced result is re-parsed and compared against the incoming body
    so a splice that landed in the wrong place cannot reach the plan store.
    """

    on_disk = project_definitions(view)
    targets: list[tuple[str, DefinitionEdit]] = []
    for definition in definitions:
        current = on_disk.get((definition.kind, definition.name))
        if definition.path is None:
            if current is None:
                raise EditValidationError(
                    f"{definition.kind} '{definition.name}' is not in the project, "
                    "so there is no file to rewrite: give it a path"
                )
            targets.append((current[0], definition))
            continue
        if current is not None and current[0] != definition.path:
            raise EditValidationError(
                f"{definition.kind} '{definition.name}' is declared in "
                f"'{current[0]}' but this edit targets '{definition.path}'. "
                "Writing it to both would duplicate the name; move a definition "
                "with whole-file edits (--edits-file) so the removal and the "
                "addition land in one plan"
            )
        targets.append((definition.path, definition))

    ordered_paths: list[str] = []
    for path, _definition in targets:
        if path not in ordered_paths:
            ordered_paths.append(path)

    spliced: list[tuple[str, str]] = []
    for path in ordered_paths:
        source = view.files.get(path)
        text = source.content if source is not None else ""
        applied = [d for target_path, d in targets if target_path == path]
        for definition in applied:
            text = _splice_entry(path, text, definition)
        _verify_splice(path, text, applied)
        spliced.append((path, text))
    return spliced


# Constructs the line scanner cannot span without risking a wrong edit. Each
# refusal costs the caller a whole-file edit, which still works, so failing
# closed here is cheap; a mis-sliced definition would not be.
def _reject_unspannable(path: str, text: str) -> None:
    if "\t" in text:
        raise EditValidationError(
            f"{path}: the file indents with tabs, which YAML does not allow for "
            "structure and this editor will not guess at; use --edits-file"
        )
    if re.search(r"^---\s*$", text[1:], re.MULTILINE) or re.search(
        r"^\.\.\.\s*$", text, re.MULTILINE
    ):
        raise EditValidationError(
            f"{path}: the file holds more than one YAML document; a definition "
            "edit cannot tell which one to write into, use --edits-file"
        )
    if re.search(r"(^|[\s:\[{,])[&*][A-Za-z0-9_][^\s]*", text):
        raise EditValidationError(
            f"{path}: the file uses YAML anchors or aliases, whose expansion the "
            "definition editor does not track; use --edits-file"
        )


class _Block(NamedTuple):
    """Where a top-level sequence lives in the file, by line index."""

    start: int  # first line after the `key:` line
    end: int  # one past the block's last line
    indent: str  # the indent its `- ` items carry


def _find_block(path: str, lines: list[str], key: str) -> _Block | None:
    key_line = None
    for index, line in enumerate(lines):
        if re.match(rf"^{key}:\s*$", line):
            key_line = index
            break
        if re.match(rf"^{key}:\s*\[", line):
            raise EditValidationError(
                f"{path}: '{key}' is written as an inline (flow) sequence, which "
                "this editor will not rewrite; use --edits-file"
            )
    if key_line is None:
        return None

    end = len(lines)
    for index in range(key_line + 1, len(lines)):
        line = lines[index]
        if line.strip() and not line[:1].isspace():
            end = index
            break

    indent = None
    for index in range(key_line + 1, end):
        match = re.match(r"^(\s+)-\s", lines[index])
        if match:
            indent = match.group(1)
            break
    return _Block(key_line + 1, end, indent if indent is not None else "  ")


def _entry_spans(lines: list[str], block: _Block) -> list[tuple[str | None, int, int]]:
    """Each item in the block as ``(name, start, end)`` line indices.

    An entry ends at its last line of content, so the blank lines and comments
    that separate it from the next item stay where the author put them: those
    usually head the *following* definition, and moving them would relocate a
    section banner every time a neighbour is edited.
    """

    starts = [
        index
        for index in range(block.start, block.end)
        if lines[index].startswith(f"{block.indent}- ")
    ]
    spans: list[tuple[str | None, int, int]] = []
    for position, start in enumerate(starts):
        limit = starts[position + 1] if position + 1 < len(starts) else block.end
        end = start
        for index in range(start, limit):
            line = lines[index]
            if not line.strip():
                continue
            if line.lstrip().startswith("#") and not _inside_entry(line, block.indent):
                # A comment at the item's own indent is a banner for what comes
                # next (`# ---- metrics ----`), so it stays put when the entry
                # above it is rewritten. One indented deeper annotates a field of
                # this entry, and leaving it behind would strand a note about a
                # line that no longer exists.
                continue
            end = index + 1
        item = "".join(lines[start:end])
        try:
            parsed = yaml.safe_load(item)
        except yaml.YAMLError:
            parsed = None
        name = None
        if isinstance(parsed, list) and parsed and isinstance(parsed[0], dict):
            name = parsed[0].get("name")
        spans.append((name, start, end))
    return spans


def _inside_entry(line: str, item_indent: str) -> bool:
    """Is this comment line part of the entry above it, or a banner below it?

    Depth decides. A comment written at the item's own indent sits between two
    definitions and belongs to neither; anything deeper is inside the mapping.
    """

    return len(line) - len(line.lstrip()) > len(item_indent)


def _as_item(content: str, indent: str) -> str:
    body = content.rstrip("\n").split("\n")
    rendered = [f"{indent}- {body[0].rstrip()}\n"]
    rendered += [
        f"{indent}  {line.rstrip()}\n" if line.strip() else "\n" for line in body[1:]
    ]
    return "".join(rendered)


def _splice_entry(path: str, text: str, definition: DefinitionEdit) -> str:
    _reject_unspannable(path, text)
    key = _TOP_LEVEL_KEY[definition.kind]
    lines = text.splitlines(keepends=True)
    block = _find_block(path, lines, key)

    if block is None:
        # No block for this kind yet. Append one, keeping a blank line between
        # it and whatever the file already holds.
        prefix = text if text.endswith("\n") or not text else text + "\n"
        if not prefix:
            prefix = "version: 2\n"
        separator = "" if prefix.endswith("\n\n") else "\n"
        return f"{prefix}{separator}{key}:\n{_as_item(definition.content, '  ')}"

    item = _as_item(definition.content, block.indent)
    for name, start, end in _entry_spans(lines, block):
        if name == definition.name:
            return "".join(lines[:start]) + item + "".join(lines[end:])

    # New definition in an existing block: after the last item's content, so it
    # lands inside the block rather than after any trailing comment.
    spans = _entry_spans(lines, block)
    insert_at = spans[-1][2] if spans else block.start
    tail = "".join(lines[insert_at:])
    head = "".join(lines[:insert_at])
    # A file that does not end in a newline would otherwise fuse with the item.
    if head and not head.endswith("\n"):
        head += "\n"
    return f"{head}\n{item}{tail}"


def _verify_splice(path: str, text: str, definitions: Sequence[DefinitionEdit]) -> None:
    """The result must parse, and each definition must be exactly what was sent.

    Cheap insurance against the line scanner landing a definition in the wrong
    item or the wrong block: the comparison is against the caller's own parsed
    body, so a splice that reads as valid YAML but says something else cannot be
    stored.
    """

    try:
        parsed = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise EditValidationError(
            f"{path}: writing these definitions produced invalid YAML ({exc}); "
            "use --edits-file for this file"
        ) from exc
    if not isinstance(parsed, dict):
        raise EditValidationError(
            f"{path}: writing these definitions produced a document that is not a "
            "mapping; use --edits-file for this file"
        )
    for definition in definitions:
        key = _TOP_LEVEL_KEY[definition.kind]
        landed = [
            entry
            for entry in parsed.get(key) or []
            if isinstance(entry, dict) and entry.get("name") == definition.name
        ]
        if len(landed) != 1 or landed[0] != definition.parsed:
            raise EditValidationError(
                f"{path}: {definition.kind} '{definition.name}' did not write "
                "cleanly into this file's layout; use --edits-file for it"
            )
