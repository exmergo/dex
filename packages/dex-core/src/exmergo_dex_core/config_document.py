"""``.dex/config.yml`` read as a document, not only as settings.

``load_config`` needs one thing from the file: the settings. Keeping the file
correct needs more. It needs to know which line said what, which keys the schema
does not know, and how to change one setting without rewriting the rest. A
committed config carries its reasoning in comments, in an order its author chose,
and a round trip through a YAML printer throws both away. So every edit here is a
splice into the original text at offsets a reader computed, never a re-render,
and it is checked afterwards by loading the result and comparing it with what was
intended.

This class is the one reading of the file. Every command's load, ``dex config
get|validate|set`` and the one-time ceiling answer all go through it, so the
validator and the loader cannot disagree about whether a file is usable.
"""

from __future__ import annotations

import copy
import difflib
import functools
import itertools
import re
import shutil
import types
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Union, get_args, get_origin

import yaml
from pydantic import BaseModel, TypeAdapter, ValidationError
from pydantic_core import to_jsonable_python

from .config import CONFIG_FILE, SEMANTIC_ENVIRONMENT, DexConfig
from .diffs import file_diff
from .envelope import redact, secret_like
from .errors import ConfigurationError, RequestError
from .storage import DEX_DIR
from .yaml_spans import before_trailing_blanks, block_end, indent_of

CONFIG_RELPATH = f"{DEX_DIR}/{CONFIG_FILE}"

# A syntax error or a value the schema refuses stops every command; the rest are
# reported and the file still loads. An unknown key is not fatal because the
# loader has always ignored one, and refusing it now would break every project
# carrying a key a later release retired.
_FATAL_KINDS = frozenset({"syntax", "invalid"})

_NULL_TAG = "tag:yaml.org,2002:null"
#: What `written` returns for a key the file does not set, distinct from a
#: key written as `null`.
ABSENT: Any = object()


@dataclass(frozen=True)
class ConfigProblem:
    """One thing wrong with the file, located by setting and line.

    ``kind`` is ``syntax``, ``invalid``, ``unknown_key``, ``duplicate_key``,
    ``secret_like_key`` or ``project_not_found``. ``(kind, key, message)``
    identifies a problem across edits, because an edit shifts every line below it.
    """

    kind: str
    key: str
    message: str
    line: int | None = None
    suggestion: str | None = None

    @property
    def fatal(self) -> bool:
        return self.kind in _FATAL_KINDS

    @property
    def identity(self) -> tuple[str, str, str]:
        return (self.kind, self.key, self.message)

    def describe(self) -> str:
        where = f"line {self.line}: " if self.line else ""
        # The non-fatal kinds read as a sentence about the key; the fatal ones
        # carry a parser's or validator's own words, set off by a colon.
        joint = ": " if self.fatal else " "
        subject = f"{self.key}{joint}" if self.key else ""
        hint = f"; did you mean {self.suggestion}?" if self.suggestion else ""
        return f"{where}{subject}{self.message}{hint}"

    def data(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "key": self.key,
            "line": self.line,
            "message": self.message,
            "suggestion": self.suggestion,
            "fatal": self.fatal,
        }


class InvalidConfigError(ConfigurationError):
    """The file cannot be turned into settings. Carries every fatal problem."""

    def __init__(self, problems: list[ConfigProblem]) -> None:
        self.problems = list(problems)
        listed = "; ".join(problem.describe() for problem in self.problems)
        super().__init__(
            f"{CONFIG_RELPATH} cannot be used: {listed}. `dex config validate` "
            "lists every problem in the file"
        )


class ConfigEditError(ConfigurationError):
    """An edit dex cannot make without guessing at the file's layout.

    Nothing is written when this is raised. The message names what to change by
    hand, because the alternative to refusing is rewriting the file, which is the
    thing this module exists not to do.
    """


@dataclass(frozen=True)
class ConfigSetting:
    """One effective setting, and where its value came from.

    ``source`` is one of:

    - ``file``: the key is in the file;
    - ``default``: it is not, and the schema default applies;
    - ``derived``: it is not, but a validator computed a value from other keys;
    - ``environment``: a variable beats the file at run time.

    ``as_written`` is the file's own spelling when that differs from the
    effective value (``backend: api`` is read as ``dbt_cloud``). A value under a
    credential-like name is ``withheld`` and never carried at all.
    """

    key: str
    value: Any
    source: str
    line: int | None = None
    as_written: Any = ABSENT
    variable: str | None = None
    withheld: bool = False

    def data(self) -> dict[str, Any]:
        row: dict[str, Any] = {"key": self.key, "source": self.source}
        if self.withheld:
            row["withheld"] = True
        else:
            row["value"] = self.value
        if self.line is not None:
            row["line"] = self.line
        if self.as_written is not ABSENT and not self.withheld:
            row["as_written"] = self.as_written
        if self.variable is not None:
            row["variable"] = self.variable
        return row


@dataclass(frozen=True)
class SettingKey:
    """A dotted key resolved against the schema.

    ``section`` is the model when the key names a block of settings rather than
    one value. ``free_form`` marks a key inside a mapping dex does not interpret
    (``cache.options``), where any sub-key is accepted and the backend that reads
    it is the one that validates it.
    """

    key: str
    annotation: Any
    default: Any
    section: type[BaseModel] | None = None
    free_form: bool = False


def resolve_key(key: str) -> SettingKey:
    """``key`` resolved against the schema, or a refusal naming the nearest key.

    Walks the pydantic model tree rather than a hand-kept table, so a setting
    added to ``config.py`` is settable and readable here without a change.
    """

    parts = key.split(".") if key else []
    if not parts or any(not part for part in parts):
        raise RequestError(
            f"'{key}' is not a setting key; keys are dotted paths "
            "such as budget.session_ceiling"
        )
    if "[" in key:
        raise RequestError(
            f"'{key}' names one item of a list; a list is set as a whole, e.g. "
            "dex config set bigquery.datasets '[raw, staging]'"
        )
    model: type[BaseModel] = DexConfig
    for index, part in enumerate(parts):
        fields = model.model_fields
        if part not in fields:
            parent = ".".join(parts[:index])
            raise RequestError(
                f"'{key}' is not a setting dex reads"
                + _hint(_suggest(parent, part, fields))
            )
        info = fields[part]
        rest = parts[index + 1 :]
        section = _model_in(info.annotation)
        if not rest:
            return SettingKey(
                key=key,
                annotation=info.annotation,
                default=info.get_default(call_default_factory=True),
                section=section,
                free_form=_is_free_form(info.annotation),
            )
        if section is not None:
            model = section
            continue
        if _is_free_form(info.annotation):
            return SettingKey(key=key, annotation=Any, default=None, free_form=True)
        raise RequestError(
            f"'{'.'.join(parts[: index + 1])}' holds a single value, not settings, "
            f"so '{key}' is not a setting"
        )
    raise AssertionError("unreachable")  # pragma: no cover


def parse_value(key: str, text: str) -> Any:
    """The value ``text`` means for ``key``, read the way the schema reads it.

    YAML first, so ``5`` is a number, ``[raw, staging]`` a list and ``null`` a
    null. YAML 1.1 also reads ``no`` as false and ``123`` as a number, so when the
    field refuses what YAML produced and accepts the text as written, the text
    wins: ``dex config set dbt_target no`` sets the string. A string the field
    reads as a number is written as that number, since ``1e12`` is a string to
    PyYAML and would otherwise land in the file quoted.

    Refuses a section (its keys are set one at a time, which also keeps the
    comments inside it), and a credential-like name either in the key or
    anywhere inside the value (``cache.options '{api_token: ...}'``), because
    this file is committed.
    """

    target = resolve_key(key)
    if target.section is not None:
        listed = ", ".join(f"{key}.{name}" for name in target.section.model_fields)
        raise RequestError(
            f"'{key}' is a block of settings; set its keys one at a time: {listed}"
        )
    committed = (
        f"{CONFIG_RELPATH} is committed: credentials are read at run time, never "
        "written here"
    )
    credential = _credential_in(key, None)
    if credential is not None:
        raise RequestError(
            f"'{key}' reads as a credential ('{credential}'), and {committed}"
        )
    try:
        parsed = yaml.safe_load(text)
    except yaml.YAMLError:
        parsed = text
    credential = _credential_in(key, parsed)
    if credential is not None:
        raise RequestError(
            f"the value for '{key}' sets '{credential}', which reads as a "
            f"credential, and {committed}"
        )
    if target.free_form or target.annotation is Any:
        return parsed
    adapter = TypeAdapter(target.annotation)
    try:
        validated = adapter.validate_python(parsed)
    except ValidationError:
        try:
            adapter.validate_python(text)
        except ValidationError:
            # Left for the whole-file validation to refuse, so the message is
            # the one every other path gives for the same value.
            return parsed
        return text
    if (
        isinstance(parsed, str)
        and isinstance(validated, int | float)
        and not isinstance(validated, bool)
    ):
        return validated
    return parsed


class ConfigDocument:
    """The text of ``.dex/config.yml``, with what it says and what is wrong with it.

    Immutable: an edit returns a new document built from the new text, so a
    document always describes exactly the bytes it holds. ``edit_warnings``
    carries what an edit could not keep (comments inside a list it replaced) for
    the caller to show beside the diff. ``repo_root`` is the directory that holds
    ``.dex/``; without it, a ``dbt_project_dir`` pin is not checked against the
    filesystem.
    """

    def __init__(
        self,
        text: str,
        *,
        edit_warnings: list[str] | None = None,
        repo_root: Path | None = None,
    ) -> None:
        self.text = text
        self.edit_warnings = list(edit_warnings or [])
        self.repo_root = repo_root
        self._syntax: ConfigProblem | None = None
        self._root: yaml.Node | None = None
        self._loaded: Any = None
        try:
            self._root = yaml.compose(text, Loader=yaml.SafeLoader)
            self._loaded = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            mark = getattr(exc, "problem_mark", None) or getattr(
                exc, "context_mark", None
            )
            self._syntax = ConfigProblem(
                "syntax",
                "",
                _yaml_message(exc),
                line=mark.line + 1 if mark is not None else None,
            )
        if isinstance(self._root, yaml.ScalarNode) and self._root.tag == _NULL_TAG:
            # A document of only `---` composes to a null, which means no settings.
            self._root = None
        self._lines: dict[str, int] = {}
        self._duplicates: list[ConfigProblem] = []
        if self._root is not None:
            self._index(self._root, "")
        self._problems: list[ConfigProblem] | None = None

    @classmethod
    def read(cls, path: Path) -> ConfigDocument:
        # Bytes, not `read_text`: universal newlines would turn a CRLF file into
        # LF, and every edit would then rewrite every line ending in the file.
        return cls(path.read_bytes().decode("utf-8"), repo_root=path.parent.parent)

    def write(self, path: Path) -> None:
        """Replace the file atomically, keeping its permissions.

        A config half-written by a crash would fail every command in the repo,
        so the new text goes to a sibling file first and is moved into place.
        """

        temporary = path.with_name(f".{path.name}.dex-edit")
        temporary.write_bytes(self.text.encode("utf-8"))
        if path.exists():
            shutil.copymode(path, temporary)
        temporary.replace(path)

    # --- what the file says ---------------------------------------------------

    def problems(self) -> list[ConfigProblem]:
        """Every problem in the file, in line order."""

        if self._problems is None:
            self._problems = self._find_problems()
        return self._problems

    def config(self) -> DexConfig:
        """The settings, exactly as every command reads them.

        The same validation the loader has always run, over the same mapping, so
        a file that loaded before loads to the same settings now. What changed is
        how a refusal reads: by setting and line rather than as pydantic's text.
        """

        fatal = [problem for problem in self.problems() if problem.fatal]
        if fatal:
            raise InvalidConfigError(fatal)
        return DexConfig.model_validate(self._mapping())

    def warnings(self) -> list[str]:
        """The problems that do not stop a command, phrased for an envelope."""

        return [
            f"{CONFIG_RELPATH} {problem.describe()}"
            for problem in self.problems()
            if not problem.fatal
        ]

    def settings(
        self, key: str | None = None, *, environ: Mapping[str, str]
    ) -> list[ConfigSetting]:
        """The effective settings, one row per value, optionally under ``key``.

        Flat rows rather than a nested mapping, so no key a user wrote (a
        free-form option, say) ever becomes a dict key in a payload that the
        boundary scan reads for credential names. ``environ`` is passed in rather
        than read here, so what a row says is decided by the caller's
        environment and not by whatever process happens to run the read.
        """

        target = resolve_key(key) if key else None
        config = self.config()
        rows: list[ConfigSetting] = []
        self._collect(
            DexConfig, config.model_dump(mode="json"), self._mapping(), "", rows
        )
        for name, variable in SEMANTIC_ENVIRONMENT.items():
            found = environ.get(variable)
            if not found:
                continue
            setting_key = f"semantic.{name}"
            rows = [
                ConfigSetting(
                    key=row.key,
                    value=redact(found),
                    source="environment",
                    line=row.line,
                    as_written=row.value if row.source == "file" else ABSENT,
                    variable=variable,
                )
                if row.key == setting_key
                else row
                for row in rows
            ]
        if target is None:
            return rows
        selected = [
            row
            for row in rows
            if row.key == target.key or row.key.startswith(target.key + ".")
        ]
        if selected:
            return selected
        # A valid key with no row of its own: a field of a connector section the
        # file does not declare, or an option nobody set. Its value is the default.
        return [
            ConfigSetting(
                key=target.key,
                value=to_jsonable_python(target.default),
                source="default",
                withheld=_withheld(target.key, target.default),
            )
        ]

    def written(self, key: str) -> Any:
        """What the file itself says for ``key``, or ``ABSENT`` when it says nothing.

        The value as written, before validation fills defaults or normalizes a
        spelling, which is what a change reports as its ``before``.
        """

        found: Any = self._mapping()
        for part in key.split("."):
            if not isinstance(found, dict) or part not in found:
                return ABSENT
            found = found[part]
        return found

    def closest_written(self, key: str) -> str | None:
        """The key this file writes that ``key`` was probably meant to be."""

        written = [path for path in self._lines if "[" not in path]
        close = difflib.get_close_matches(key, written, n=1, cutoff=0.6)
        return close[0] if close else None

    def diff_from(self, before: ConfigDocument) -> dict[str, Any]:
        """The change from ``before`` as a reviewable diff, credentials withheld.

        A diff carries the lines around a change as well as the change, and the
        envelope's ``diffs`` are not scanned at the boundary the way ``data`` is,
        so a credential committed near any edit would otherwise cross with it.
        """

        return file_diff(CONFIG_RELPATH, before._shown(), self._shown())

    def changes_from(
        self, before: ConfigDocument, keys: list[str]
    ) -> list[dict[str, Any]]:
        """What changed under ``keys`` since ``before``, one row per value.

        A row has ``before`` and ``after``, each left out where the value is
        absent on that side, or only ``withheld`` where either side sits under or
        holds a credential-like name. Flat rows, as ``settings`` gives, so no key
        a user wrote in a block being replaced or removed ever becomes a dict key
        in a payload the boundary scan reads.
        """

        rows: list[dict[str, Any]] = []
        for key in keys:
            old = dict(_leaves(key, before.written(key)))
            new = dict(_leaves(key, self.written(key)))
            for path in [*old, *(path for path in new if path not in old)]:
                was, now = old.get(path, ABSENT), new.get(path, ABSENT)
                # By type as well, since `True == 1` in Python and a file that
                # changes one for the other has changed.
                if type(was) is type(now) and was == now:
                    continue
                row: dict[str, Any] = {"key": path}
                if _withheld(path, was) or _withheld(path, now):
                    row["withheld"] = True
                else:
                    if was is not ABSENT:
                        row["before"] = was
                    if now is not ABSENT:
                        row["after"] = now
                rows.append(row)
        return rows

    # --- edits ----------------------------------------------------------------

    def with_value(self, key: str, value: Any) -> ConfigDocument:
        """A new document with ``key`` set to ``value``, every other byte kept.

        A value already in the file is replaced in place, so its trailing comment
        stays. A new key goes at the end of its block, after the keys already
        there, and missing parent blocks are created. The result is loaded and
        compared with what was intended before it is returned, and anything that
        does not match is refused rather than written.
        """

        self._require_editable()
        parts = key.split(".")
        warnings: list[str] = []
        edits = self._set_edits(parts, value, warnings)
        expected = copy.deepcopy(self._mapping())
        _assign(expected, parts, value)
        return self._verified(_splice(self.text, edits), expected, key, warnings)

    def without(self, key: str) -> ConfigDocument:
        """A new document with ``key`` removed, or this one when it is absent.

        Removes the key's own lines, its value and any comment written inside
        it; a comment on the line above stays, since nothing says which key it
        was written for. A block left with no keys goes too, rather than staying
        as an empty key that reads as null and fails validation. In a file that
        separates its blocks with blank lines, one separator goes with the
        entry, so the file keeps one blank line between the blocks that remain
        and gains none at its top or end.
        """

        self._require_editable()
        parts = key.split(".")
        chain = self._locate(parts)
        if chain is None:
            return self
        # Climb while the block would be left empty: removing its only key removes
        # the block. The loaded mapping is pruned by the same rule below, so the
        # verification compares like with like.
        level = len(chain) - 1
        while level > 0 and len(chain[level][0].value) == 1:
            level -= 1
        mapping, index = chain[level]
        edits = self._removal(mapping, index)
        expected = copy.deepcopy(self._mapping())
        _prune(expected, parts[: level + 1])
        return self._verified(_splice(self.text, edits), expected, key, [])

    # --- reading, internals -----------------------------------------------------

    def _mapping(self) -> dict[str, Any]:
        return self._loaded if isinstance(self._loaded, dict) else {}

    def _index(self, node: yaml.Node, prefix: str) -> None:
        """Record the line of every key, and every key written twice."""

        if isinstance(node, yaml.MappingNode):
            seen: dict[str, int] = {}
            for key_node, value_node in node.value:
                name = key_node.value if isinstance(key_node, yaml.ScalarNode) else ""
                path = f"{prefix}.{name}" if prefix else name
                line = key_node.start_mark.line + 1
                if name in seen:
                    self._duplicates.append(
                        ConfigProblem(
                            "duplicate_key",
                            path,
                            f"is set twice; the value on line {line} wins and the "
                            f"one on line {seen[name]} is ignored",
                            line=line,
                        )
                    )
                seen[name] = line
                self._lines[path] = line
                self._index(value_node, path)
        elif isinstance(node, yaml.SequenceNode):
            for position, item in enumerate(node.value):
                path = f"{prefix}[{position}]"
                self._lines[path] = item.start_mark.line + 1
                self._index(item, path)

    def _line_of(self, key: str) -> int | None:
        while key:
            if key in self._lines:
                return self._lines[key]
            cut = max(key.rfind("."), key.rfind("["))
            key = key[:cut] if cut > 0 else ""
        return None

    def _shown(self) -> str:
        """The text as a reviewer is shown it, with every value under a
        credential-like name replaced by ``<withheld>``.

        Each value keeps its own line breaks, so a diff of two shown texts has
        the file's line numbers and the file's addition and deletion counts. Key
        names stay, as ``config get`` names a withheld setting. A text with no
        settings has nothing to withhold, and one that does not parse is never
        edited, so either is shown as it is.
        """

        if self._root is None:
            return self.text
        spans: set[tuple[int, int]] = set()

        def walk(node: yaml.Node, hidden: bool) -> None:
            if isinstance(node, yaml.MappingNode):
                for key_node, value_node in node.value:
                    name = (
                        key_node.value if isinstance(key_node, yaml.ScalarNode) else ""
                    )
                    walk(value_node, hidden or secret_like(str(name)))
            elif isinstance(node, yaml.SequenceNode):
                for item in node.value:
                    walk(item, hidden)
            elif hidden and node.end_mark.index > node.start_mark.index:
                spans.add((node.start_mark.index, node.end_mark.index))

        walk(self._root, False)
        shown = self.text
        for start, end in sorted(spans, reverse=True):
            breaks = "".join(char for char in shown[start:end] if char in "\r\n")
            shown = shown[:start] + "<withheld>" + breaks + shown[end:]
        return shown

    def _find_problems(self) -> list[ConfigProblem]:
        if self._syntax is not None:
            return [self._syntax]
        if self._loaded is not None and not isinstance(self._loaded, dict):
            return [
                ConfigProblem(
                    "invalid",
                    "",
                    "the file has to be a mapping of settings, and it holds a "
                    f"{type(self._loaded).__name__}",
                    line=1,
                )
            ]
        found = list(self._duplicates)
        self._unknown_keys(self._mapping(), DexConfig, "", found)
        self._credential_keys(self._mapping(), "", found)
        self._project_pin(self._mapping(), found)
        try:
            DexConfig.model_validate(self._mapping())
        except ValidationError as exc:
            found.extend(self._invalid(error) for error in exc.errors())
        return sorted(found, key=lambda p: (p.line is None, p.line or 0, p.key, p.kind))

    def _unknown_keys(
        self,
        value: Any,
        model: type[BaseModel],
        prefix: str,
        found: list[ConfigProblem],
    ) -> None:
        if not isinstance(value, dict):
            return
        fields = model.model_fields
        for name, sub in value.items():
            path = f"{prefix}{name}"
            if name not in fields:
                found.append(
                    ConfigProblem(
                        "unknown_key",
                        path,
                        "is not a setting dex reads, so it is ignored",
                        line=self._line_of(path),
                        suggestion=_suggest(prefix.rstrip("."), str(name), fields),
                    )
                )
                continue
            annotation = fields[name].annotation
            section = _model_in(annotation)
            if section is not None:
                self._unknown_keys(sub, section, f"{path}.", found)
                continue
            item = _item_model(annotation)
            if item is not None and isinstance(sub, list):
                for position, entry in enumerate(sub):
                    self._unknown_keys(entry, item, f"{path}[{position}].", found)

    def _credential_keys(
        self, value: Any, prefix: str, found: list[ConfigProblem]
    ) -> None:
        if isinstance(value, dict):
            for name, sub in value.items():
                path = f"{prefix}{name}"
                if secret_like(str(name)):
                    found.append(
                        ConfigProblem(
                            "secret_like_key",
                            path,
                            "reads as a credential, and this file is committed; "
                            "read credentials at run time instead",
                            line=self._line_of(path),
                        )
                    )
                self._credential_keys(sub, f"{path}.", found)
        elif isinstance(value, list):
            for position, entry in enumerate(value):
                self._credential_keys(
                    entry, f"{prefix.rstrip('.')}[{position}].", found
                )

    def _project_pin(self, mapping: dict[str, Any], found: list[ConfigProblem]) -> None:
        # Not fatal, like an unknown key: commands that never open the dbt
        # project still work, and every command carries the warning.
        pin = mapping.get("dbt_project_dir")
        if self.repo_root is None or not isinstance(pin, str) or not pin:
            return
        if not (self.repo_root / pin / "dbt_project.yml").is_file():
            found.append(
                ConfigProblem(
                    "project_not_found",
                    "dbt_project_dir",
                    f"names {pin}, which holds no dbt_project.yml",
                    line=self._line_of("dbt_project_dir"),
                )
            )

    def _invalid(self, error: Any) -> ConfigProblem:
        key = _dotted(error["loc"])
        message = str(error["msg"]).removeprefix("Value error, ")
        given = error.get("input")
        if (
            error["type"] != "value_error"
            and isinstance(given, str | int | float | bool)
            and not _withheld(key, given)
        ):
            message = f"{message} (got {given!r})"
        return ConfigProblem("invalid", key, message, line=self._line_of(key))

    def _collect(
        self,
        model: type[BaseModel],
        dumped: dict[str, Any],
        raw: dict[str, Any] | None,
        prefix: str,
        rows: list[ConfigSetting],
    ) -> None:
        for name, info in model.model_fields.items():
            key = f"{prefix}{name}"
            value = dumped.get(name)
            in_file = isinstance(raw, dict) and name in raw
            written = raw.get(name) if in_file and raw is not None else None
            section = _model_in(info.annotation)
            if section is not None and isinstance(value, dict):
                self._collect(
                    section,
                    value,
                    written if isinstance(written, dict) else None,
                    f"{key}.",
                    rows,
                )
                continue
            if _is_free_form(info.annotation) and isinstance(value, dict) and value:
                self._flatten(key, value, rows)
                continue
            if in_file:
                source = "file"
                as_written = written if written != value else ABSENT
            else:
                default = to_jsonable_python(
                    info.get_default(call_default_factory=True)
                )
                source = "derived" if value != default else "default"
                as_written = ABSENT
            rows.append(
                ConfigSetting(
                    key=key,
                    value=value,
                    source=source,
                    line=self._lines.get(key),
                    as_written=as_written,
                    withheld=_withheld(key, value),
                )
            )

    def _flatten(self, key: str, value: dict[str, Any], rows: list[ConfigSetting]):
        for name, sub in value.items():
            path = f"{key}.{name}"
            if isinstance(sub, dict) and sub:
                self._flatten(path, sub, rows)
                continue
            rows.append(
                ConfigSetting(
                    key=path,
                    value=sub,
                    source="file",
                    line=self._lines.get(path),
                    withheld=_withheld(path, sub),
                )
            )

    # --- editing, internals -----------------------------------------------------

    def _require_editable(self) -> None:
        if self._syntax is not None:
            raise ConfigEditError(
                f"{CONFIG_RELPATH} does not parse ({self._syntax.describe()}); "
                "fix that by hand first"
            )
        if self._root is not None and not isinstance(self._root, yaml.MappingNode):
            raise ConfigEditError(
                f"{CONFIG_RELPATH} holds no mapping of settings to edit; fix it by hand"
            )
        for event in yaml.parse(self.text, Loader=yaml.SafeLoader):
            if isinstance(event, yaml.AliasEvent) or getattr(event, "anchor", None):
                raise ConfigEditError(
                    f"{CONFIG_RELPATH} uses a YAML anchor or alias, so one edit "
                    "could change a value in two places; edit it by hand"
                )
        for line in self.text.splitlines():
            if "\t" in line[: len(line) - len(line.lstrip(" \t"))]:
                raise ConfigEditError(
                    f"{CONFIG_RELPATH} indents with tabs; edit it by hand"
                )

    def _verified(
        self, text: str, expected: dict[str, Any], key: str, warnings: list[str]
    ) -> ConfigDocument:
        try:
            loaded = yaml.safe_load(text)
        except yaml.YAMLError:
            loaded = ABSENT
        if (loaded if loaded is not None else {}) != expected:
            raise ConfigEditError(
                f"dex could not change {key} in {CONFIG_RELPATH} without guessing "
                "at the file's layout, so nothing was changed; edit it by hand"
            )
        return ConfigDocument(
            text,
            edit_warnings=[*self.edit_warnings, *warnings],
            repo_root=self.repo_root,
        )

    @functools.cached_property
    def _newline(self) -> str:
        return "\r\n" if "\r\n" in self.text else "\n"

    @functools.cached_property
    def _unit(self) -> int:
        """The file's own indent step, read from its first nested block."""

        if isinstance(self._root, yaml.MappingNode):
            for key_node, value_node in self._root.value:
                if (
                    isinstance(value_node, yaml.MappingNode)
                    and value_node.flow_style is not True
                    and value_node.value
                ):
                    step = (
                        value_node.value[0][0].start_mark.column
                        - key_node.start_mark.column
                    )
                    if step > 0:
                        return step
        return 2

    def _line_start(self, index: int) -> int:
        return self.text.rfind("\n", 0, index) + 1

    def _line_end(self, index: int) -> int:
        """The offset just past the line ending of the line holding ``index``."""

        newline = self.text.find("\n", index)
        return len(self.text) if newline < 0 else newline + 1

    def _colon_after(self, key_node: yaml.Node) -> int:
        return self.text.index(":", key_node.end_mark.index) + 1

    def _entry_end(self, key_node: yaml.Node, value_node: yaml.Node) -> int:
        """Where a block mapping entry stops, value included.

        By indentation, which covers a nested block and any continuation lines.
        A sequence written at its key's own column (indentless) is the one value
        indentation cannot bound from the key line, so its items are measured
        too.
        """

        start = self._line_start(key_node.start_mark.index)
        end = block_end(self.text, start, indent_of(self.text, start))
        if _is_block_collection(value_node) and value_node.value:
            end = max(end, self._collection_end(value_node))
        return end

    def _collection_end(self, node: yaml.Node) -> int:
        if isinstance(node, yaml.MappingNode):
            last_key, last_value = node.value[-1]
            return self._entry_end(last_key, last_value)
        last = node.value[-1]
        start = self._line_start(last.start_mark.index)
        end = block_end(self.text, start, indent_of(self.text, start))
        if _is_block_collection(last) and last.value:
            end = max(end, self._collection_end(last))
        return end

    def _set_edits(
        self, parts: list[str], value: Any, warnings: list[str]
    ) -> list[tuple[int, int, str]]:
        node = self._root
        parent_key: yaml.Node | None = None
        for index, part in enumerate(parts):
            rest = parts[index:]
            if node is None:
                return self._append_at_root(rest, value)
            if (
                isinstance(node, yaml.ScalarNode)
                and node.tag == _NULL_TAG
                and parent_key is not None
            ):
                return self._fill_null(parent_key, node, rest, value)
            if not isinstance(node, yaml.MappingNode):
                raise ConfigEditError(
                    f"{'.'.join(parts[:index])} holds a value in {CONFIG_RELPATH}, "
                    f"not a block of settings, so {'.'.join(parts)} cannot go "
                    "under it; edit it by hand"
                )
            pairs = [
                (key_node, value_node)
                for key_node, value_node in node.value
                if isinstance(key_node, yaml.ScalarNode) and key_node.value == part
            ]
            if len(pairs) > 1:
                raise ConfigEditError(
                    f"{'.'.join(parts[: index + 1])} is set twice in "
                    f"{CONFIG_RELPATH}; remove one by hand first"
                )
            if not pairs:
                return self._insert(node, rest, value)
            key_node, value_node = pairs[0]
            if index == len(parts) - 1:
                return self._replace(key_node, value_node, value, warnings)
            node, parent_key = value_node, key_node
        raise AssertionError("unreachable")  # pragma: no cover

    def _replace(
        self,
        key_node: yaml.Node,
        node: yaml.Node,
        value: Any,
        warnings: list[str],
    ) -> list[tuple[int, int, str]]:
        start, end = node.start_mark.index, node.end_mark.index
        if isinstance(node, yaml.ScalarNode):
            if node.style in ("|", ">"):
                raise ConfigEditError(
                    f"{key_node.value} is a block scalar in {CONFIG_RELPATH}; "
                    "edit it by hand"
                )
            if _flow_ok(value):
                if start == end:
                    return [(start, start, " " + _inline(value))]
                quote = node.style if node.style in ("'", '"') else None
                return [(start, end, _inline(value, quote=quote))]
            return self._as_block(key_node, node, value)
        if node.flow_style is True:
            if _flow_ok(value):
                return [(start, end, _inline(value))]
            return self._as_block(key_node, node, value)
        if not node.value:
            return self._as_block(key_node, node, value)
        first = (
            node.value[0][0] if isinstance(node, yaml.MappingNode) else node.value[0]
        )
        span_start = self._line_start(first.start_mark.index)
        span_end = before_trailing_blanks(self.text, self._collection_end(node))
        dropped = sum(
            1
            for line in self.text[span_start:span_end].splitlines()
            if re.search(r"(^|\s)#", line)
        )
        if dropped:
            warnings.append(
                f"replacing {key_node.value} drops {dropped} comment line"
                f"{'' if dropped == 1 else 's'} written inside it"
            )
        if isinstance(value, list | dict) and not value:
            colon = self._colon_after(key_node)
            return [(colon, colon, " " + _inline(value)), (span_start, span_end, "")]
        block = self._block(value, indent_of(self.text, span_start))
        if not self.text[span_start:span_end].endswith("\n"):
            block = block.rstrip("\r\n")
        return [(span_start, span_end, block)]

    def _as_block(
        self, key_node: yaml.Node, node: yaml.Node, value: Any
    ) -> list[tuple[int, int, str]]:
        """Make ``key_node`` hold ``value`` as block lines under its own line."""

        colon = self._colon_after(key_node)
        after = self._line_end(max(node.end_mark.index, colon))
        pad = key_node.start_mark.column + self._unit
        block = self._block(value, pad)
        if after == len(self.text) and not self.text.endswith("\n"):
            block = self._newline + block.rstrip("\r\n")
        edits = [(after, after, block)]
        if node.end_mark.index > colon and not _is_block_collection(node):
            edits.append((colon, node.end_mark.index, ""))
        return edits

    def _insert(
        self, mapping: yaml.MappingNode, rest: list[str], value: Any
    ) -> list[tuple[int, int, str]]:
        if mapping.flow_style is True:
            start, end = mapping.start_mark.index, mapping.end_mark.index
            if mapping.start_mark.line != mapping.end_mark.line:
                raise ConfigEditError(
                    f"{CONFIG_RELPATH} writes the block {rest[0]} would join as a "
                    "flow mapping across several lines; edit it by hand"
                )
            entry = f"{_inline(rest[0])}: {_inline(_nest(rest[1:], value))}"
            if not mapping.value:
                return [(start, end, "{" + entry + "}")]
            if self.text[end - 1] != "}":
                raise ConfigEditError(
                    f"a flow mapping in {CONFIG_RELPATH} is not closed where dex "
                    "expected; edit it by hand"
                )
            return [(end - 1, end - 1, ", " + entry)]
        first_key = mapping.value[0][0]
        last_key, last_value = mapping.value[-1]
        at = before_trailing_blanks(self.text, self._entry_end(last_key, last_value))
        lines = self._nested(rest, value, first_key.start_mark.column)
        # A file that separates its blocks with a blank line gets one before the
        # new key too, judged by whether the last key already has one above it.
        last_line = self._line_start(last_key.start_mark.index)
        if self.text[:last_line].endswith(("\n\n", "\n\r\n")):
            lines = self._newline + lines
        if at == len(self.text) and self.text and not self.text.endswith("\n"):
            lines = self._newline + lines.rstrip("\r\n")
        return [(at, at, lines)]

    def _fill_null(
        self,
        key_node: yaml.Node,
        null: yaml.Node,
        rest: list[str],
        value: Any,
    ) -> list[tuple[int, int, str]]:
        """Give a key written with no value (``budget:`` or ``budget: ~``) children."""

        colon = self._colon_after(key_node)
        after = self._line_end(colon)
        lines = self._nested(rest, value, key_node.start_mark.column + self._unit)
        if after == len(self.text) and not self.text.endswith("\n"):
            lines = self._newline + lines.rstrip("\r\n")
        edits = [(after, after, lines)]
        if null.end_mark.index > null.start_mark.index:
            edits.append((colon, null.end_mark.index, ""))
        return edits

    def _append_at_root(
        self, rest: list[str], value: Any
    ) -> list[tuple[int, int, str]]:
        lines = self._nested(rest, value, 0)
        at = len(self.text)
        if self.text and not self.text.endswith("\n"):
            lines = self._newline + lines
        return [(at, at, lines)]

    def _nested(self, parts: list[str], value: Any, column: int) -> str:
        """``parts[0]: ... parts[-1]: value`` as block lines starting at ``column``."""

        lines = []
        for depth, part in enumerate(parts[:-1]):
            lines.append(" " * (column + depth * self._unit) + f"{_inline(part)}:")
        pad = column + (len(parts) - 1) * self._unit
        head = " " * pad + f"{_inline(parts[-1])}:"
        if _flow_ok(value):
            lines.append(f"{head} {_inline(value)}")
            return self._newline.join(lines) + self._newline
        lines.append(head)
        return (
            self._newline.join(lines)
            + self._newline
            + self._block(value, pad + self._unit)
        )

    def _block(self, value: Any, pad: int) -> str:
        dumped = yaml.dump(
            value,
            Dumper=_BlockDumper,
            default_flow_style=False,
            sort_keys=False,
            allow_unicode=True,
            indent=self._unit,
            width=10**9,
        )
        lines = [" " * pad + line if line else line for line in dumped.splitlines()]
        return self._newline.join(lines) + self._newline

    def _locate(self, parts: list[str]) -> list[tuple[yaml.MappingNode, int]] | None:
        """The mapping and pair index at each level of ``parts``, or None."""

        chain: list[tuple[yaml.MappingNode, int]] = []
        node = self._root
        for part in parts:
            if not isinstance(node, yaml.MappingNode):
                return None
            matches = [
                position
                for position, (key_node, _value) in enumerate(node.value)
                if isinstance(key_node, yaml.ScalarNode) and key_node.value == part
            ]
            if not matches:
                return None
            if len(matches) > 1:
                raise ConfigEditError(
                    f"{part} is set twice in {CONFIG_RELPATH}; remove one by hand first"
                )
            chain.append((node, matches[0]))
            node = node.value[matches[0]][1]
        return chain

    def _removal(
        self, mapping: yaml.MappingNode, index: int
    ) -> list[tuple[int, int, str]]:
        key_node, value_node = mapping.value[index]
        if mapping.flow_style is True:
            if len(mapping.value) == 1:
                return [(mapping.start_mark.index, mapping.end_mark.index, "{}")]
            if index < len(mapping.value) - 1:
                following = mapping.value[index + 1][0]
                return [(key_node.start_mark.index, following.start_mark.index, "")]
            previous = mapping.value[index - 1][1]
            return [(previous.end_mark.index, value_node.end_mark.index, "")]
        start = self._line_start(key_node.start_mark.index)
        if self.text[start : key_node.start_mark.index].strip():
            raise ConfigEditError(
                f"{key_node.value} shares its line with other YAML in "
                f"{CONFIG_RELPATH}; edit it by hand"
            )
        end = before_trailing_blanks(self.text, self._entry_end(key_node, value_node))
        top = before_trailing_blanks(self.text, start)
        below = self._past_blank_lines(end)
        if below == len(self.text) and top < start:
            # The last entry in the file: the separator above it would be left
            # trailing.
            start = top
        elif below > end and (top < start or not self.text[:start].strip()):
            # Between two separators, keep one; at the top of the file, keep none.
            end = below
        return [(start, end, "")]

    def _past_blank_lines(self, index: int) -> int:
        """``index`` moved forward over the blank lines that start there."""

        while index < len(self.text):
            line_end = self._line_end(index)
            if self.text[index:line_end].strip():
                break
            index = line_end
        return index


class _BlockDumper(yaml.SafeDumper):
    """Indents a sequence under its key, the way dbt projects and dex's own docs
    write one, rather than PyYAML's default of starting it at the key's column."""

    def increase_indent(self, flow: bool = False, indentless: bool = False) -> None:
        return super().increase_indent(flow, False)


# --- module helpers -----------------------------------------------------------


def _inline(value: Any, *, quote: str | None = None) -> str:
    """``value`` as a one-line YAML scalar or flow collection.

    A string with a line break is double-quoted so the break is an escape on one
    line rather than a continuation whose indentation would have to be right.
    """

    style = quote if isinstance(value, str) else None
    if isinstance(value, str) and ("\n" in value or "\r" in value):
        style = '"'
    dumped = yaml.safe_dump(
        value,
        default_flow_style=True,
        default_style=style,
        width=10**9,
        allow_unicode=True,
        sort_keys=False,
    )
    if dumped.endswith("\n...\n"):
        dumped = dumped[: -len("\n...\n")]
    return dumped.rstrip("\n")


def _flow_ok(value: Any) -> bool:
    """Whether ``value`` reads well on one line: a scalar or a list of scalars."""

    if isinstance(value, dict):
        return not value
    if isinstance(value, list):
        return all(not isinstance(item, dict | list) for item in value)
    return True


def _is_block_collection(node: yaml.Node) -> bool:
    return isinstance(node, yaml.MappingNode | yaml.SequenceNode) and (
        node.flow_style is not True
    )


def _splice(text: str, edits: list[tuple[int, int, str]]) -> str:
    ordered = sorted(edits, key=lambda edit: (edit[0], edit[1]))
    for (_, end, _), (start, _, _) in itertools.pairwise(ordered):
        if start < end:
            raise ConfigEditError(
                f"dex computed overlapping edits to {CONFIG_RELPATH}, so nothing "
                "was changed; edit it by hand"
            )
    for start, end, replacement in reversed(ordered):
        text = text[:start] + replacement + text[end:]
    return text


def _nest(parts: list[str], value: Any) -> Any:
    for part in reversed(parts):
        value = {part: value}
    return value


def _assign(mapping: dict[str, Any], parts: list[str], value: Any) -> None:
    for part in parts[:-1]:
        if not isinstance(mapping.get(part), dict):
            mapping[part] = {}
        mapping = mapping[part]
    mapping[parts[-1]] = value


def _prune(mapping: dict[str, Any], parts: list[str]) -> None:
    for part in parts[:-1]:
        mapping = mapping[part]
    del mapping[parts[-1]]


def _yaml_message(exc: yaml.YAMLError) -> str:
    """The parser's own words on one line. An unclosed construct is reported
    where the parser gave up, often the end of the file, so the line it began on
    is named too."""

    context = getattr(exc, "context", None)
    problem = getattr(exc, "problem", None)
    if not problem:
        return str(exc).splitlines()[0]
    if not context:
        return str(problem)
    began = getattr(exc, "context_mark", None)
    ended = getattr(exc, "problem_mark", None)
    if began is not None and ended is not None and began.line != ended.line:
        context = f"{context} begun on line {began.line + 1}"
    return f"{context}, {problem}"


def _dotted(location: tuple[Any, ...]) -> str:
    key = ""
    for part in location:
        if isinstance(part, int):
            key += f"[{part}]"
        else:
            key = f"{key}.{part}" if key else str(part)
    return key


def _credential_in(key: str, value: Any) -> str | None:
    """The first credential-like name on ``key``'s path or anywhere inside
    ``value``, or None. The one rule for what the editor will not write and
    what a reader must not be shown."""

    for part in re.split(r"[.\[\]]", key):
        if part and secret_like(part):
            return part

    def inside(found: Any) -> str | None:
        children: list[Any] = []
        if isinstance(found, dict):
            for name, sub in found.items():
                if secret_like(str(name)):
                    return str(name)
                children.append(sub)
        elif isinstance(found, list):
            children = found
        for child in children:
            if (hit := inside(child)) is not None:
                return hit
        return None

    return inside(value)


def _withheld(key: str, value: Any) -> bool:
    """Whether a value must not be carried: a credential-like name on its path,
    or a credential-like key anywhere inside it."""

    return _credential_in(key, value) is not None


def _leaves(key: str, value: Any) -> list[tuple[str, Any]]:
    """``value`` as ``(key, value)`` pairs down to its scalars, the shape a row
    takes. A list of scalars and an empty collection are one value each, as
    ``config get`` reports them; ``ABSENT`` has none."""

    if value is ABSENT:
        return []
    if isinstance(value, dict) and value:
        return [
            leaf
            for name, sub in value.items()
            for leaf in _leaves(f"{key}.{name}", sub)
        ]
    if isinstance(value, list) and any(isinstance(item, dict | list) for item in value):
        return [
            leaf
            for position, sub in enumerate(value)
            for leaf in _leaves(f"{key}[{position}]", sub)
        ]
    return [(key, value)]


def _suggest(parent: str, name: str, siblings: Any) -> str | None:
    """The key ``name`` was probably meant to be: a near sibling, or the one key
    elsewhere in the schema with exactly that name (a setting written at the
    wrong level)."""

    close = difflib.get_close_matches(name, list(siblings), n=1, cutoff=0.6)
    if close:
        return f"{parent}.{close[0]}" if parent else close[0]
    elsewhere = [key for key in _schema_keys() if key.rsplit(".", 1)[-1] == name]
    return elsewhere[0] if len(elsewhere) == 1 else None


def _hint(suggestion: str | None) -> str:
    return f"; did you mean {suggestion}?" if suggestion else ""


@functools.cache
def _schema_keys() -> tuple[str, ...]:
    keys: list[str] = []

    def walk(model: type[BaseModel], prefix: str) -> None:
        for name, info in model.model_fields.items():
            key = f"{prefix}{name}"
            keys.append(key)
            section = _model_in(info.annotation)
            if section is not None:
                walk(section, f"{key}.")

    walk(DexConfig, "")
    return tuple(keys)


def _model_in(annotation: Any) -> type[BaseModel] | None:
    """The model an annotation names, directly or as ``Model | None``."""

    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return annotation
    if get_origin(annotation) in (Union, types.UnionType):
        models = [arg for arg in get_args(annotation) if _model_in(arg) is not None]
        if len(models) == 1:
            return models[0]
    return None


def _item_model(annotation: Any) -> type[BaseModel] | None:
    if get_origin(annotation) is list:
        (item,) = get_args(annotation) or (None,)
        return _model_in(item)
    return None


def _is_free_form(annotation: Any) -> bool:
    return annotation is dict or get_origin(annotation) is dict


__all__ = [
    "ABSENT",
    "CONFIG_RELPATH",
    "ConfigDocument",
    "ConfigEditError",
    "ConfigProblem",
    "ConfigSetting",
    "InvalidConfigError",
    "SettingKey",
    "parse_value",
    "resolve_key",
]
