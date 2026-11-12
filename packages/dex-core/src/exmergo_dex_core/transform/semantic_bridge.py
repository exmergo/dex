"""`semantic databricks|ossie ... --from-ossie|--from-metric-view`: one layer
proposed from the other through the Ossie bridge (#1260).

The repository's layer is always the configured ``semantic.vendor``, which is
what ``transform apply`` writes; the other format is only read, from one file
inside the repository. The conversion is the ported converter plus dex's
adaptations (:mod:`..metric_views.ossie_bridge`), and its output is held to the
target layer's own validation before anything is stored:

- an output path on the layer's write surface goes through
  :func:`.native_semantic.semantic_native` unchanged, so the plan is validated,
  classified and stored exactly as a hand-written ``--edits-file`` would be;
- any other path stores no plan: the document is validated the same way and
  returned with the ``dex config set`` that adopts its path.

Every notice, the converter's and dex's, reaches ``data.bridge``.

What differs between the two directions is a row of :data:`BRIDGES`, keyed by
the target vendor, so neither the CLI nor this module names a vendor at a call
site: the next pair of formats is a row, not a branch.
"""

from __future__ import annotations

import json
import re
import shlex
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

from ..edits import SemanticEditTarget
from ..errors import RequestError
from ..metric_views.dependency import CONVERTER_COMMIT
from .plans import EditKind, PlanEdit
from .results import BridgeResult

if TYPE_CHECKING:
    from ..engine import DexEngine

__all__ = ["BRIDGES", "Bridge", "BridgeError", "bridge_arguments", "semantic_bridge"]


class BridgeError(RequestError):
    """A conversion or validation the bridge refused, with its ``bridge`` block,
    so the notices reach the error envelope too. A ``RequestError``: the source
    file, its conversion or the output path is the caller's to fix."""

    def __init__(self, message: str, bridge: dict[str, Any]) -> None:
        super().__init__(message)
        self.bridge = bridge


@dataclass(frozen=True)
class _Converted:
    """A conversion's output: the document's text, its model name, notices."""

    content: str
    model: str
    notices: tuple[Any, ...]


@dataclass(frozen=True)
class Bridge:
    """One direction of the bridge, keyed by the vendor it writes."""

    #: ``data.bridge.direction``.
    direction: str
    #: The format the source file is in, as the converter names it.
    reads: str
    #: The format written, as a refusal names it.
    writes: str
    #: The flag naming the source file, its argparse ``dest``, and help.
    flag: str
    dest: str
    help: str
    #: The source suffixes read.
    suffixes: tuple[str, ...]
    #: The direction's own options: CLI flag, ``semantic_bridge`` keyword, and
    #: the argparse keywords.
    options: tuple[tuple[str, str, dict[str, Any]], ...]
    convert: Callable[[str, dict[str, Any]], _Converted]
    place: Callable[[Any, str], str]
    validate: Callable[[DexEngine, Any, str, str], list[str]]


# --- the two directions ---------------------------------------------------------------


def _to_metric_view(text: str, options: dict[str, Any]) -> _Converted:
    from ..metric_views.loader import dump_yaml
    from ..metric_views.ossie_bridge.adapt import export_document
    from ..metric_views.ossie_bridge.common import parse_root

    document = parse_root(text, "Apache Ossie")
    converted = export_document(document, options.get("fact"))
    return _Converted(
        dump_yaml(converted.value), str(document.get("name")), converted.notices
    )


def _to_ossie(text: str, options: dict[str, Any]) -> _Converted:
    from ..metric_views.ossie_bridge.adapt import import_view
    from ..metric_views.ossie_bridge.common import parse_root
    from ..yaml_items import dump_items

    view = parse_root(text, "Metric View")
    converted = import_view(
        view, options.get("name"), dataset_metrics=bool(options.get("dataset_metrics"))
    )
    return _Converted(
        dump_items(converted.value), str(converted.value.get("name")), converted.notices
    )


def _place_view(layer: Any, model: str) -> str:
    from ..metric_views.derivation import placement

    directory, suffix = placement(layer)
    return f"{directory}/{model}{suffix}"


def _place_document(layer: Any, model: str) -> str:
    from ..ossie.derivation import document_directory

    return f"{document_directory(layer)}/{model}.ossie.yaml"


def _documents(layer: Any, path: str, content: str) -> dict[str, str]:
    documents = {
        p: entry.content
        for p, entry in layer.semantic_edit_view().files.items()
        if entry.content is not None
    }
    documents[path] = content
    return documents


def _validate_view(engine: DexEngine, layer: Any, path: str, content: str) -> list[str]:
    from ..metric_views.authoring import refusals_for

    validated, refusals = refusals_for(_documents(layer, path, content), {path})
    problems = [d.render() for d in refusals if d.file == path]
    if not problems and validated.view(PurePosixPath(path).stem) is None:
        problems.append(f"{path} did not read back as a view")
    return problems


def _validate_document(
    engine: DexEngine, layer: Any, path: str, content: str
) -> list[str]:
    from ..ossie.loader import validate_document_contents

    result = validate_document_contents(
        _documents(layer, path, content),
        connector=engine.connector or engine.config.connector,
    )
    return [
        d.render()
        for d in result.diagnostics
        if d.severity == "error" and d.file == path
    ]


_INTO = (
    "--into",
    "into",
    {"metavar": "PATH", "help": "output path (default: beside the configured files)"},
)

#: The bridge, by the vendor it writes.
BRIDGES: dict[str, Bridge] = {
    "databricks": Bridge(
        direction="ossie_to_metric_view",
        reads="Apache Ossie",
        writes="Metric View",
        flag="--from-ossie",
        dest="from_ossie",
        help="propose the Metric View from these Ossie documents",
        suffixes=(".yaml", ".yml", ".json"),
        options=(
            (
                "--fact",
                "fact",
                {
                    "metavar": "DATASET",
                    "help": "the dataset the view is on (several roots)",
                },
            ),
            _INTO,
        ),
        convert=_to_metric_view,
        place=_place_view,
        validate=_validate_view,
    ),
    "ossie": Bridge(
        direction="metric_view_to_ossie",
        reads="Metric View",
        writes="Ossie",
        flag="--from-metric-view",
        dest="from_metric_view",
        help="propose the Ossie documents from these Metric Views",
        suffixes=(".yaml", ".yml"),
        options=(
            (
                "--name",
                "name",
                {"metavar": "MODEL", "help": "the model and fact dataset name"},
            ),
            (
                "--dataset-metrics",
                "dataset_metrics",
                {
                    "action": "store_true",
                    "help": "write fact-only measures as dataset metrics",
                },
            ),
            _INTO,
        ),
        convert=_to_ossie,
        place=_place_document,
        validate=_validate_document,
    ),
}


def bridge_arguments(vendor: str) -> list[tuple[str, dict[str, Any]]]:
    """The argparse arguments ``semantic <vendor>`` adds for the bridge, if any."""

    bridge = BRIDGES.get(vendor)
    if bridge is None:
        return []
    source = (
        bridge.flag,
        {
            "dest": bridge.dest,
            "metavar": "FILE",
            # Repeatable, or several after one flag: new configured files can only
            # be authored together, since the layer validates them all at once.
            "action": "extend",
            "nargs": "+",
            "help": bridge.help,
        },
    )
    return [
        source,
        *((flag, {"dest": dest, **kw}) for flag, dest, kw in bridge.options),
    ]


# --- the command ----------------------------------------------------------------------


@dataclass
class _Conversion:
    """One conversion's entry under ``data.bridge.conversions``."""

    source: str
    output_path: str | None = None
    name: str | None = None
    status: str = "failed"
    reason: str | None = None
    notices: list[dict[str, str]] = field(default_factory=list)
    content: str | None = None
    next: str | None = None

    def data(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "source": self.source,
            "output_path": self.output_path,
            "name": self.name,
            "status": self.status,
        }
        if self.reason is not None:
            out["reason"] = self.reason
        out["notices"] = self.notices
        if self.content is not None:
            out["content"] = self.content
        if self.next is not None:
            out["next"] = self.next
        return out


def semantic_bridge(
    engine: DexEngine,
    vendor: str,
    mode: str,
    intent: str,
    source: str | list[str],
    **options: Any,
) -> BridgeResult:
    """Propose ``vendor``'s layer from the other format's file(s) ``source``.

    Several files are converted together and stored as one plan, because the
    target layer validates every configured file at once: a set of new files can
    only be authored together. ``options`` are the direction's own (``fact`` and
    ``into`` for a Metric View, ``name``, ``dataset_metrics`` and ``into`` for an
    Ossie document); one that belongs to the other direction is refused by name,
    and ``into`` and ``name`` name one output, so they take one source.
    """

    from .native_semantic import MODES, require_configured_vendor

    if mode not in MODES:
        raise ValueError(f"unknown semantic {vendor} mode '{mode}'")
    bridge = BRIDGES.get(vendor)
    if bridge is None:
        raise ValueError(
            f"`semantic {vendor}` has no Ossie bridge: "
            + "; ".join(
                f"`semantic {v} {b.flag}` proposes a {b.writes} layer from a {b.reads} "
                "file"
                for v, b in BRIDGES.items()
            )
        )
    require_configured_vendor(engine, vendor)
    sources = [source] if isinstance(source, str) else list(dict.fromkeys(source))
    if not sources:
        raise ValueError(f"{bridge.flag} needs a file")
    options = {k: v for k, v in options.items() if v not in (None, False)}
    _refuse_foreign_options(vendor, bridge, options)
    single = [flag for flag, dest, _ in bridge.options if dest in ("into", "name")]
    if len(sources) > 1 and any(d in options for d in ("into", "name")):
        raise ValueError(
            f"{' and '.join(single)} name one output, and {len(sources)} files were "
            f"given to {bridge.flag}: convert those one at a time, or drop the flag"
        )

    repo_root = Path(engine.require_repo_root("reading a file for the Ossie bridge"))
    entries = [_Conversion(source=s) for s in sources]
    bridge_data: dict[str, Any] = {
        "direction": bridge.direction,
        "converter_commit": CONVERTER_COMMIT,
        "conversions": entries,
    }

    def failer(entry: _Conversion) -> Callable[[str], BridgeError]:
        def fail(reason: str) -> BridgeError:
            entry.status = "failed"
            entry.reason = reason
            return BridgeError(
                reason if len(entries) == 1 else f"{entry.source}: {reason}",
                _bridge_data(bridge_data),
            )

        return fail

    layer = engine.semantic_catalog_source()
    if not isinstance(layer, SemanticEditTarget):
        raise failer(entries[0])(f"the configured {vendor} layer cannot be written")
    contents: dict[str, str] = {}
    for entry in entries:
        fail = failer(entry)
        text, relative = _read_source(repo_root, entry.source, bridge, fail)
        entry.source = relative
        converted = _convert(bridge, text, options, fail)
        entry.name = converted.model
        entry.notices = [
            {"origin": n.origin, "text": n.render()} for n in converted.notices
        ]
        path = _output_path(
            repo_root, bridge, layer, converted.model, options.get("into"), fail
        )
        if path in contents:
            raise fail(
                f"{path} is also the output of another file given to {bridge.flag}: "
                "the two models share a name; convert them one at a time with --into"
            )
        entry.output_path = path
        contents[path] = converted.content

    surface = set(layer.semantic_editing_surface())
    included = [e for e in entries if e.output_path in surface]
    proposed = [e for e in entries if e.output_path not in surface]
    named = ", ".join(e.name or "" for e in entries)
    intent = intent or (
        f"propose {named} ({bridge.writes}) from "
        + ", ".join(e.source for e in entries)
    )

    for entry in proposed:
        fail = failer(entry)
        if mode == "update":
            raise fail(
                f"{entry.output_path} is not a configured file, so there is nothing "
                f"to update: run `semantic {vendor} define` (or `plan`) to propose it"
            )
        problems = bridge.validate(
            engine, layer, str(entry.output_path), contents[str(entry.output_path)]
        )
        if problems:
            raise fail(f"the {bridge.writes} rules refuse it: " + "; ".join(problems))
    if proposed:
        paths = [str(e.output_path) for e in proposed]
        rerun = _rerun(bridge, [e.source for e in entries], options)
        adopt = _adopt(vendor, layer, paths, rerun, repo_root)
        for entry in proposed:
            entry.status = "proposed"
            entry.content = contents[str(entry.output_path)]
            entry.next = adopt
        bridge_data["next"] = adopt

    if included:
        from .native_semantic import semantic_native

        edits = [
            PlanEdit(
                path=str(e.output_path),
                kind=EditKind.SEMANTIC_DOCUMENT,
                new_content=contents[str(e.output_path)],
            )
            for e in included
        ]
        try:
            planned = semantic_native(engine, vendor, intent, edits, mode=mode)
        except ValueError as exc:
            for entry in included:
                entry.status = "failed"
                entry.reason = f"the {bridge.writes} rules refuse it: {exc}"
            raise BridgeError(
                f"the {bridge.writes} rules refuse it: {exc}", _bridge_data(bridge_data)
            ) from exc
        for entry in included:
            entry.status = "included"
        result = BridgeResult(**planned.model_dump(), bridge=_bridge_data(bridge_data))
        result.warnings = [*result.warnings, *_notice_warning(entries)]
        if proposed:
            result.notes = [*result.notes, _proposal_note(vendor, proposed)]
        return result

    return BridgeResult(
        intent=intent,
        bridge=_bridge_data(bridge_data),
        notes=[_proposal_note(vendor, proposed)],
        warnings=_notice_warning(entries),
    )


def _proposal_note(vendor: str, proposed: list[_Conversion]) -> str:
    paths = ", ".join(str(e.output_path) for e in proposed)
    one = len(proposed) == 1
    return (
        f"{paths} {'is' if one else 'are'} not under semantic.{vendor}.files, so no "
        f"plan writes {'it' if one else 'them'}; each document is in its "
        "data.bridge.conversions entry's content"
    )


def _bridge_data(bridge: dict[str, Any]) -> dict[str, Any]:
    return {
        **{k: v for k, v in bridge.items() if k != "conversions"},
        "conversions": [c.data() for c in bridge["conversions"]],
    }


def _refuse_foreign_options(
    vendor: str, bridge: Bridge, options: dict[str, Any]
) -> None:
    own = {dest for _, dest, _ in bridge.options}
    for dest in options:
        if dest in own:
            continue
        owner = next(
            (
                (v, flag)
                for v, b in BRIDGES.items()
                for flag, d, _ in b.options
                if d == dest
            ),
            None,
        )
        if owner is None:
            raise ValueError(f"the Ossie bridge takes no option '{dest}'")
        raise ValueError(
            f"{owner[1]} belongs to `semantic {owner[0]} {BRIDGES[owner[0]].flag}`; "
            f"`semantic {vendor} {bridge.flag}` takes "
            + ", ".join(flag for flag, _, _ in bridge.options)
        )


def _read_source(
    repo_root: Path, source: str, bridge: Bridge, fail: Any
) -> tuple[str, str]:
    """The source file's text and its path relative to the repository.

    Confined as the readers confine a configured file: inside the repository,
    symlinks resolved, and a file.
    """
    root = repo_root.resolve()
    candidate = Path(source)
    resolved = (candidate if candidate.is_absolute() else root / candidate).resolve()
    try:
        relative = resolved.relative_to(root).as_posix()
    except ValueError:
        raise fail(
            f"{source} resolves outside the repository, so dex does not read it"
        ) from None
    if not resolved.is_file():
        raise fail(f"{source} is not a file")
    suffix = resolved.suffix.lower()
    if suffix == ".sql":
        raise fail(
            f"{source} is a .sql file: the bridge reads a Metric View's YAML body, "
            "the text between the $$ delimiters of `CREATE VIEW ... WITH METRICS`"
        )
    if suffix not in bridge.suffixes:
        raise fail(f"{source} is not a {' or '.join(bridge.suffixes)} file")
    try:
        return resolved.read_text(encoding="utf-8"), relative
    except (OSError, UnicodeDecodeError) as exc:
        raise fail(f"{source} could not be read: {type(exc).__name__}") from exc


def _convert(
    bridge: Bridge, text: str, options: dict[str, Any], fail: Any
) -> _Converted:
    from ..metric_views.ossie_bridge import ConversionError

    try:
        return bridge.convert(text, options)
    except ConversionError as exc:
        reason = f"the converter refuses it ({exc.kind.value}): {exc.reason}"
        if "--source" in exc.reason:
            reason += "; dex names that dataset with --fact"
        raise fail(reason) from exc


def _notice_warning(entries: list[_Conversion]) -> list[str]:
    notices = [n for e in entries for n in e.notices]
    if not notices:
        return []
    by_origin = {"converter": 0, "dex": 0}
    for notice in notices:
        by_origin[notice["origin"]] = by_origin.get(notice["origin"], 0) + 1
    return [
        f"the Ossie bridge reported {len(notices)} notice(s) "
        f"({by_origin['converter']} from the converter, {by_origin['dex']} from dex) "
        "naming what was dropped or rewritten; see data.bridge.conversions[].notices"
    ]


def _output_path(
    repo_root: Path, bridge: Bridge, layer: Any, model: str, into: Any, fail: Any
) -> str:
    """Where the proposal goes: ``into``, else beside the configured files."""
    if into is not None:
        candidate = PurePosixPath(str(into).replace("\\", "/"))
        if candidate.is_absolute() or ".." in candidate.parts:
            raise fail(
                f"--into {into} must be a path inside the repository, relative to it"
            )
        resolved = (repo_root / candidate).resolve()
        try:
            resolved.relative_to(repo_root.resolve())
        except ValueError:
            raise fail(f"--into {into} resolves outside the repository") from None
        return candidate.as_posix()
    if not (model and all(c.isalnum() or c in "_-" for c in model)):
        raise fail(
            f"the model is named '{model}', which is not a file name dex writes; name "
            "the output with --into"
        )
    return bridge.place(layer, model)


def _rerun(bridge: Bridge, sources: list[str], options: dict[str, Any]) -> str:
    """The flags to run this conversion again with, as the caller passed them,
    each path shell-quoted."""
    words = [bridge.flag, *(shlex.quote(source) for source in sources)]
    for flag, dest, kwargs in bridge.options:
        value = options.get(dest)
        if value in (None, False):
            continue
        if kwargs.get("action") == "store_true":
            words.append(flag)
        else:
            words += [flag, shlex.quote(str(value))]
    return " ".join(words)


_PLAIN_PATH = re.compile(r"[A-Za-z0-9_./-]+")


def _yaml_list(paths: list[str]) -> str:
    """``paths`` as a YAML flow list: plain where a path is, JSON-quoted (which
    YAML reads) where it holds a space, a comma or a quote."""
    items = [p if _PLAIN_PATH.fullmatch(p) else json.dumps(p) for p in paths]
    return "[" + ", ".join(items) + "]"


def _adopt(
    vendor: str, layer: Any, paths: list[str], flags: str, repo_root: Path
) -> str:
    listed = _yaml_list(
        list(dict.fromkeys([*layer.semantic_editing_surface(), *paths]))
    )
    # `define` refuses a file that already exists, and an unconfigured path can:
    # once configured, that file is an update.
    exists = any((repo_root / p).exists() for p in paths)
    mode = "update" if exists else "define"
    return (
        f"{', '.join(paths)} {'is' if len(paths) == 1 else 'are'} not under "
        f"semantic.{vendor}.files, the files dex may write: run "
        f"`dex config set semantic.{vendor}.files {shlex.quote(listed)}`, then run "
        f"`semantic {vendor} {mode} {flags}` again to store the plan"
    )
