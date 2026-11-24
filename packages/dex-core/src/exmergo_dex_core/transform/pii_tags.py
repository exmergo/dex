"""Elementary's PII tag on the columns Dex marks as personal data (#1487).

Dex marks personal data with ``config.meta.contains_pii`` and ``pii_category``
(:mod:`..guards.pii_meta`). The Elementary package does not read those keys.
It reads tags: with its var ``disable_samples_on_pii_tags`` on, a test on a
model tagged with one of ``pii_tags`` keeps no sample of its failing rows, and a
test whose query names a tagged column, or selects ``*`` from a model that has
one, keeps none either. Every one of those tests, the project's own included.

With ``monitors.vendor: elementary`` and the package installed (or added by the
same plan), a plan writes that tag where it would matter:

* on each column a model's YAML marks as PII, under any key a reader accepts and
  at any confidence, in ``config.meta.tags``, the one ``meta`` location Dex
  writes;
* on the model holding such a column, in ``config.tags`` (:func:`_tag_model`,
  the one place the model tag is written);
* and ``disable_samples_on_pii_tags: true`` under ``vars:`` in
  ``dbt_project.yml``, as a ``project_yml`` edit in the same plan, unless the
  project already sets it.

The tag is ``pii``, or the first entry of the project's own ``pii_tags`` where
it sets one. Only the models the plan writes are tagged: a model whose SQL it
writes, a model it declares, and a model whose YAML entry it changes. Nothing
here clears a mark, removes a tag, or overwrites a value the project set: a
``disable_samples_on_pii_tags: false`` the project wrote is kept and said.

The rule is younger than some releases Dex supports. Releases before 0.19.2
have no switch, so the tags do nothing there; 0.19.2 to 0.19.4 read a model's
tags and a column's ``tags``, ``config.tags`` and ``meta.tags``, but not its
``config.meta.tags``, so there only the model tag counts; 0.20.0 and later read
all of them. ``data.pii_tags.rule`` says which applies to the installed release.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from pathlib import PurePosixPath
from typing import Any

import yaml

from ..guards.pii_meta import entry_meta, says_pii
from .plans import EditKind, EditOp, PlanEdit

#: The tag Elementary reads when the project sets no ``pii_tags``.
DEFAULT_TAG = "pii"
#: Elementary's switch for its PII sampling rule, off by default.
SWITCH = "disable_samples_on_pii_tags"
#: Elementary's list of the tags that mark PII.
TAGS_VAR = "pii_tags"

#: The first release with the switch and the model tag (read from the tags).
SWITCH_SINCE = "0.19.2"
#: The first release that reads a column's ``config.meta.tags``.
COLUMN_META_SINCE = "0.20.0"

# What the installed release does with the tags (``data.pii_tags.rule``).
RULE_NONE = "none"
RULE_MODEL_ONLY = "model_tags_only"
RULE_FULL = "model_and_column_tags"
RULE_UNKNOWN = "unknown"

# A column's or a model's status.
ADDED = "added"
ALREADY_TAGGED = "already_tagged"
NOT_WRITTEN = "not_written"

# The switch's status.
SWITCH_ADDED = "added"
SWITCH_ON = "already_on"
SWITCH_OFF = "set_off_by_project"
SWITCH_UNREAD = "unread"
SWITCH_NOT_WRITTEN = "not_written"
SWITCH_WITHHELD = "withheld"

#: What a tagged model loses, stated by every plan that tags one.
SAMPLING = (
    "every test on a tagged model, the project's own included, keeps no sample "
    "of its failing rows, wherever Elementary's test materialization runs it"
)


def tag(
    view: Any,
    edits: list[PlanEdit],
    gate: Any,
    declared: Iterable[str] = (),
) -> tuple[list[PlanEdit], list[str], dict[str, Any] | None]:
    """Tag the PII-marked columns of the models ``edits`` write.

    ``gate`` is the vendor monitors' gate (:func:`.monitors.gate`), or
    ``None`` where ``monitors.vendor`` is unset. ``declared`` names the models
    the plan's declarations cover, which count as written even where their
    entry comes out unchanged (``transform declare`` on a model that owes only
    its tags).

    Returns the edits, the warnings, and ``data.pii_tags``: ``None`` where the
    gate is shut or no written model has a PII-marked column.
    """

    from . import declarations as decl
    from .monitors import ELEMENTARY, NOT_INSTALLED

    if gate is None or gate.vendor != ELEMENTARY or gate.package == NOT_INSTALLED:
        return edits, [], None

    files = decl.post_change_files(view, edits)
    prior = {path: source.content for path, source in view.files.items()}
    project_yml = files.get("dbt_project.yml")
    tag_name, tag_source = project_tag(project_yml)

    models = _written_models(edits, files, prior, declared)
    rows: list[dict[str, Any]] = []
    for model in sorted(models):
        path = decl.schema_path(files, model)
        if path is None:
            continue
        text, row = tag_entry(files[path], model, tag_name)
        if row is None:
            continue
        row = {"model": model, "path": path, **row}
        rows.append(row)
        if text != files[path]:
            files[path] = text
            edits = _merge(edits, path, EditKind.SCHEMA_YML, text)
    if not rows:
        return edits, [], None

    switch = switch_status(project_yml)
    warnings: list[str] = []
    rewritten = _rewritten_models(edits, prior)
    if switch == SWITCH_ABSENT and rewritten and not _edits_project(edits, prior):
        # A `dbt_project.yml` edit can change how any model compiles, so a plan
        # carrying one gets no refactor comparison and no downstream impact on
        # the model it rewrites. The switch waits for a plan of its own.
        switch = SWITCH_WITHHELD
        names = sorted(r["model"] for r in rows)
        warnings.append(
            f"`{SWITCH}: true` was left out of this plan, which rewrites the SQL of "
            f"{', '.join(rewritten)}: a dbt_project.yml edit would stop the refactor "
            "comparison and the downstream impact check from re-executing it. Apply "
            f"this plan, then run `dex transform declare {' '.join(names)}` to add "
            "the switch in a plan of its own; until then the tags change no sample"
        )
    elif switch == SWITCH_ABSENT:
        from .package_repair import RepairRefusedError, set_nested

        try:
            text = set_nested(project_yml or "", ["vars", SWITCH], True)
        except RepairRefusedError as exc:
            switch = SWITCH_NOT_WRITTEN
            warnings.append(
                f"the plan tags personal-data columns '{tag_name}' but could not "
                f"turn on Elementary's PII sampling rule ({exc}); add `{SWITCH}: "
                "true` under `vars:` in dbt_project.yml by hand, or the tags do "
                "nothing"
            )
        else:
            switch = SWITCH_ADDED
            edits = _merge(edits, "dbt_project.yml", EditKind.PROJECT_YML, text)
    elif switch == SWITCH_OFF:
        warnings.append(
            f"dbt_project.yml sets `{SWITCH}: false`, which is kept as written, so "
            f"the '{tag_name}' tags this plan writes change no sample until it is "
            "true"
        )
    elif switch == SWITCH_UNREAD:
        warnings.append(
            f"dex cannot read the `{SWITCH}` var in dbt_project.yml, so it was left "
            f"as written; the '{tag_name}' tags this plan writes change samples only "
            "where it renders true"
        )

    if string_tags(project_yml):
        warnings.append(
            f"dbt_project.yml writes `{TAGS_VAR}` as one string; write it as a list "
            f"(`[{tag_name}]`): some Elementary releases, 0.23.0 among them, read a "
            "single string character by character when they match a model's tags, "
            "so the model tag does nothing there"
        )
    rule = release_rule(gate.release)
    counts = _counts(rows)
    tagged = sorted(
        r["model"]
        for r in rows
        if r["model_tag"] == ADDED or any(c["status"] == ADDED for c in r["columns"])
    )
    if tagged or switch == SWITCH_ADDED:
        shown = tagged or sorted(r["model"] for r in rows)
        warnings.append(
            f"this plan tags the personal-data columns of {len(shown)} model(s) "
            f"'{tag_name}' for Elementary's PII sampling rule"
            + (
                f" and turns the rule on (`{SWITCH}: true`)"
                if switch == SWITCH_ADDED
                else ""
            )
            + f": {SAMPLING} ({', '.join(shown)})"
        )
    note = _release_note(rule, gate.release, tag_name)
    if note is not None:
        warnings.append(note)
    for row in rows:
        warnings.extend(_unwritten(row))

    return (
        edits,
        warnings,
        {
            "vendor": gate.vendor,
            "tag": tag_name,
            "tag_source": tag_source,
            "switch": {"var": SWITCH, "status": switch, "path": "dbt_project.yml"},
            "release": gate.release,
            "rule": rule,
            "sampling": SAMPLING,
            "counts": counts,
            "models": rows,
        },
    )


def owes(content: str | None, model: str, project_yml: str | None) -> bool:
    """Whether ``model``'s entry in ``content`` has a PII-marked column (or the
    model itself) still lacking the tag, or has one while the project does not
    set the switch: a model ``transform declare`` should propose although no
    measured fact is left to declare."""

    if not content:
        return False
    from .rewrite import RewriteError

    tag_name, _ = project_tag(project_yml)
    try:
        text, row = tag_entry(content, model, tag_name)
    except (RewriteError, ValueError, yaml.YAMLError):
        return False
    return row is not None and (
        text != content or switch_status(project_yml) == SWITCH_ABSENT
    )


# --- reading the project ---------------------------------------------------------


SWITCH_ABSENT = "absent"


def _vars(project_yml: str | None) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    """The project's root ``vars`` and the block scoped to the project itself.

    Elementary reads the switch and ``pii_tags`` with ``var()`` while it runs a
    test, in that test's context, and dbt merges into the global vars only the
    block keyed by the node's own package (``VarProvider.vars_for``). The
    project's own tests belong to the root project, so the block keyed by its
    ``name`` wins there, and a block keyed ``elementary`` is never read.
    """

    try:
        loaded = yaml.safe_load(project_yml) if project_yml else None
    except yaml.YAMLError:
        loaded = None
    if not isinstance(loaded, Mapping):
        return {}, {}
    found = loaded.get("vars")
    root = found if isinstance(found, Mapping) else {}
    name = loaded.get("name")
    scoped = root.get(name) if isinstance(name, str) else None
    return root, scoped if isinstance(scoped, Mapping) else {}


def _var(project_yml: str | None, name: str) -> tuple[bool, Any]:
    """``(set, value)`` for ``name`` as the project's own tests read it: the
    block scoped to the root project wins over the global one."""

    root, scoped = _vars(project_yml)
    for level in (scoped, root):
        if name in level:
            return True, level[name]
    return False, None


def project_tag(project_yml: str | None) -> tuple[str, str]:
    """The tag to write and where it came from: ``(tag, "pii_tags")`` where the
    project sets ``pii_tags`` (its first entry), else ``("pii", "default")``."""

    found, value = _var(project_yml, TAGS_VAR)
    if found:
        if isinstance(value, str) and value.strip() and "{" not in value:
            return value.strip(), TAGS_VAR
        if isinstance(value, list) and value:
            first = value[0]
            if isinstance(first, str) and first.strip() and "{" not in first:
                return first.strip(), TAGS_VAR
    return DEFAULT_TAG, "default"


def string_tags(project_yml: str | None) -> bool:
    """Whether the project writes ``pii_tags`` as one string rather than a list.

    Some releases (0.23.0 among those Dex tests) read the model tag by iterating
    ``pii_tags``, and a Jinja string iterates by character, so a model tag never
    matches there."""

    found, value = _var(project_yml, TAGS_VAR)
    return found and isinstance(value, str)


def switch_status(project_yml: str | None) -> str:
    """``absent``, ``already_on``, ``set_off_by_project`` or ``unread``."""

    found, value = _var(project_yml, SWITCH)
    if not found:
        return SWITCH_ABSENT
    if value is True or (isinstance(value, str) and value.strip().lower() == "true"):
        return SWITCH_ON
    if value is False or (isinstance(value, str) and value.strip().lower() == "false"):
        return SWITCH_OFF
    return SWITCH_UNREAD


def release_rule(release: str | None) -> str:
    """What the installed release does with the tags (module docstring)."""

    from .elementary import release_key

    key = release_key(release)
    if key is None:
        return RULE_UNKNOWN
    if key < release_key(SWITCH_SINCE):  # type: ignore[operator]
        return RULE_NONE
    if key < release_key(COLUMN_META_SINCE):  # type: ignore[operator]
        return RULE_MODEL_ONLY
    return RULE_FULL


def _release_note(rule: str, release: str | None, tag_name: str) -> str | None:
    if rule == RULE_NONE:
        return (
            f"the installed Elementary release {release} has no PII sampling rule "
            f"(it first appears in {SWITCH_SINCE}), so the '{tag_name}' tags and "
            f"`{SWITCH}` do nothing until the package is upgraded"
        )
    if rule == RULE_MODEL_ONLY:
        return (
            f"the installed Elementary release {release} reads a model's tags but "
            f"not a column's `config.meta.tags` (read from {COLUMN_META_SINCE}), so "
            "there only the model tag keeps samples out"
        )
    if rule == RULE_UNKNOWN:
        return (
            "dex could not read the installed Elementary release, so whether it "
            f"reads the '{tag_name}' tags is unchecked: releases from {SWITCH_SINCE} "
            f"read the model tag, and from {COLUMN_META_SINCE} the column's too"
        )
    return None


# --- which models ------------------------------------------------------------------


def _written_models(
    edits: list[PlanEdit],
    files: Mapping[str, str],
    prior: Mapping[str, str],
    declared: Iterable[str],
) -> set[str]:
    """The models whose SQL the plan writes, the models it declares, and the
    models whose YAML entry it changes."""

    from .. import model_versions as mv

    versions = mv.cached_version_map(files)
    out = {_base(name) for name in declared}
    for edit in edits:
        if edit.op is not EditOp.UPSERT:
            continue
        if edit.kind is EditKind.MODEL_SQL and edit.path.endswith(".sql"):
            label = mv.label_for_file(edit.path, versions)
            out.add(_base(label or PurePosixPath(edit.path).stem))
        elif edit.kind is EditKind.SCHEMA_YML:
            before = _entries(prior.get(edit.path))
            for name, entry in _entries(files.get(edit.path)).items():
                if before.get(name) != entry:
                    out.add(name)
    return out


def _base(model: str) -> str:
    """A version label's model (``dim_teams.v2`` -> ``dim_teams``): every
    version is tagged in the one YAML entry that holds them."""

    from ..model_versions import split_label

    return split_label(model)[0]


def _rewritten_models(edits: list[PlanEdit], prior: Mapping[str, str]) -> list[str]:
    """The models whose existing SQL file the plan rewrites."""

    return sorted(
        PurePosixPath(edit.path).stem
        for edit in edits
        if edit.kind is EditKind.MODEL_SQL
        and edit.op is EditOp.UPSERT
        and edit.path in prior
    )


def _edits_project(edits: list[PlanEdit], prior: Mapping[str, str]) -> bool:
    """Whether the plan already changes what makes the checks treat it as global.

    An edit that resends such a file unchanged is no change: the plan drops it
    before it is stored, so merging the switch into it would be what made the
    plan global."""

    def changes(edit: PlanEdit) -> bool:
        if edit.op is EditOp.DELETE or edit.path not in prior:
            return True
        return _lf(edit.new_content or "") != _lf(prior[edit.path])

    return any(
        edit.kind in (EditKind.PROJECT_YML, EditKind.PACKAGES_YML, EditKind.MACRO_SQL)
        and changes(edit)
        for edit in edits
    )


def _lf(text: str) -> str:
    return text.replace("\r\n", "\n")


def _entries(content: str | None) -> dict[str, Any]:
    try:
        parsed = yaml.safe_load(content) if content else None
    except yaml.YAMLError:
        return {}
    if not isinstance(parsed, Mapping):
        return {}
    return {
        str(entry["name"]): entry
        for entry in parsed.get("models") or []
        if isinstance(entry, Mapping) and isinstance(entry.get("name"), str)
    }


# --- writing -------------------------------------------------------------------


def _lowered(value: Any) -> set[str]:
    if isinstance(value, str):
        return {value.lower()}
    if isinstance(value, list):
        return {v.lower() for v in value if isinstance(v, str)}
    return set()


def _column_tags(item: Mapping[str, Any]) -> set[str]:
    """Every tag a column carries where Elementary looks for one: ``tags``,
    ``config.tags``, ``meta.tags`` and ``config.meta.tags``."""

    config = _mapping(item.get("config"))
    meta = _mapping(item.get("meta"))
    nested = _mapping(config.get("meta"))
    found: set[str] = set()
    for value in (
        item.get("tags"),
        config.get("tags"),
        meta.get("tags"),
        nested.get("tags"),
    ):
        found |= _lowered(value)
    return found


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _model_tags(entry: Mapping[str, Any]) -> set[str]:
    config = _mapping(entry.get("config"))
    found = _lowered(entry.get("tags")) | _lowered(config.get("tags"))
    for meta in (_mapping(entry.get("meta")), _mapping(config.get("meta"))):
        found |= _lowered(meta.get("tags"))
    return found


def _appended(value: Any, tag_name: str) -> list[Any]:
    if isinstance(value, list):
        return [*value, tag_name]
    if isinstance(value, str):
        return [value, tag_name]
    return [tag_name]


#: The marks that say yes or no, as against ``pii_category``, which only names.
_FLAGS = ("pii", "contains_pii", "is_pii")


def _marked(item: Any) -> bool:
    """Whether a column's YAML marks it as personal data. A ``false`` a person
    wrote under any flag wins over a ``pii_category`` left beside it: the tag
    changes what the project's tests keep, so a reviewer's no is never read as
    a yes. A null flag is no answer and does not win."""

    if not isinstance(item, Mapping):
        return False
    meta = entry_meta(item)
    if any(key in meta and _said_no(meta[key]) for key in _FLAGS):
        return False
    return says_pii(meta)


def _said_no(value: Any) -> bool:
    """An explicit no: ``false`` (YAML's ``no`` loads as it), or that text. A
    null or an empty value says nothing, so a ``pii_category`` beside it still
    marks the column."""

    if value is False:
        return True
    return isinstance(value, str) and value.strip().lower() in ("false", "no")


def tag_entry(
    content: str, model: str, tag_name: str
) -> tuple[str, dict[str, Any] | None]:
    """``content`` with ``model``'s PII-marked columns and the model tagged.

    Returns the text and the model's row (``model_tag`` and ``columns``, each
    with its status), or ``None`` for the row where no column is marked.
    """

    from . import declarations as decl
    from .rewrite import RewriteError

    model = _base(model)
    entry = _entries(content).get(model)
    if entry is None:
        return content, None
    marked: list[tuple[str | None, str, Mapping[str, Any]]] = [
        (None, str(item["name"]), item)
        for item in entry.get("columns") or []
        if _marked(item) and isinstance(item.get("name"), str)
    ]
    for version in entry.get("versions") or []:
        if not isinstance(version, Mapping) or version.get("v") is None:
            continue
        marked.extend(
            (str(version["v"]), str(item["name"]), item)
            for item in version.get("columns") or []
            if _marked(item) and isinstance(item.get("name"), str)
        )
    if not marked:
        return content, None

    wanted = tag_name.lower()
    text = content
    columns: list[dict[str, Any]] = []
    for version, name, item in marked:
        row: dict[str, Any] = {"column": name}
        if version is not None:
            row["version"] = version
        if wanted in _column_tags(item):
            columns.append({**row, "status": ALREADY_TAGGED})
            continue
        if "meta" in item and not (
            isinstance(item.get("config"), Mapping) and "meta" in item["config"]
        ):
            # A top-level `meta` and no `config.meta`: writing the second form
            # beside the first is refused by dbt, so the tag is left to a person.
            columns.append({**row, "status": NOT_WRITTEN, "reason": "top_level_meta"})
            continue
        locate = (
            decl._column_locator(model, name)
            if version is None
            else decl._version_column_locator(model, version, name)
        )
        try:
            if decl._carries_anchor(decl._compose(text), locate(decl._compose(text))):
                raise RewriteError(
                    f"the entry for column '{name}' shares a YAML anchor with another"
                )
            config = item.get("config")
            nested = config.get("meta") if isinstance(config, Mapping) else None
            existing = nested.get("tags") if isinstance(nested, Mapping) else None
            text = decl._set_meta(text, locate, "tags", _appended(existing, tag_name))
        except (RewriteError, ValueError, yaml.YAMLError) as exc:
            columns.append(
                {
                    **row,
                    "status": NOT_WRITTEN,
                    "reason": "unwritable_yaml",
                    "detail": f"{type(exc).__name__}: {exc}",
                }
            )
            continue
        columns.append({**row, "status": ADDED})

    text, model_tag, detail = _tag_model(text, model, entry, tag_name)
    out: dict[str, Any] = {"model_tag": model_tag, "columns": columns}
    if detail is not None:
        out["model_tag_reason"] = "unwritable_yaml"
        out["model_tag_detail"] = detail
    return text, out


def _tag_model(
    text: str, model: str, entry: Mapping[str, Any], tag_name: str
) -> tuple[str, str, str | None]:
    """The model tag: ``config.tags`` on a model that has a PII-marked column.

    The one place it is written. Elementary zeroes the samples of every test on
    a model so tagged, a test on one of its unmarked columns included; without
    it, a column tag alone still zeroes every test whose query names that column
    or selects ``*`` from the model. Dropping this call keeps the column tags.
    """

    from . import declarations as decl
    from .rewrite import RewriteError

    if tag_name.lower() in _model_tags(entry):
        return text, ALREADY_TAGGED, None

    def locate(root: Any) -> Any:
        return decl._model_entry(root, model)

    try:
        config = entry.get("config")
        existing = config.get("tags") if isinstance(config, Mapping) else None
        written = _set_config(text, locate, "tags", _appended(existing, tag_name))
    except (RewriteError, ValueError, yaml.YAMLError) as exc:
        return text, NOT_WRITTEN, f"{type(exc).__name__}: {exc}"
    return written, ADDED, None


def _set_config(text: str, locate: Any, key: str, value: Any) -> str:
    """Write ``key: value`` under the located entry's ``config``, creating it
    where it is missing and restyling a flow-style one, as
    :func:`.declarations._set_meta` does one level down."""

    from . import declarations as decl
    from .rewrite import RewriteError

    text = decl._restyle_flow_entry(text, locate)
    root = decl._compose(text)
    node = locate(root)
    config = decl._child(node, "config")
    if config is not None and decl._carries_anchor(root, config[1]):
        raise RewriteError("the entry's config shares a YAML anchor")
    if (
        config is not None
        and isinstance(config[1], yaml.MappingNode)
        and config[1].flow_style
    ):
        text = decl._restyle_flow_value(text, root, node, "config")
        root = decl._compose(text)
        node = locate(root)
        config = decl._child(node, "config")
    if config is None or not (
        isinstance(config[1], yaml.MappingNode) and config[1].value
    ):
        return decl._set_key(text, locate, "config", {key: value})

    def config_node(r: Any) -> Any:
        found = decl._child(locate(r), "config")
        return found[1] if found else None

    return decl._set_key(text, config_node, key, value)


def _merge(
    edits: list[PlanEdit], path: str, kind: EditKind, content: str
) -> list[PlanEdit]:
    merged = list(edits)
    for i, edit in enumerate(merged):
        if edit.path == path and edit.op is EditOp.UPSERT:
            merged[i] = edit.model_copy(update={"new_content": content})
            return merged
    merged.append(PlanEdit(path=path, kind=kind, new_content=content))
    return merged


# --- telling -------------------------------------------------------------------


def _counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    counts = {ADDED: 0, ALREADY_TAGGED: 0, NOT_WRITTEN: 0}
    for row in rows:
        for column in row["columns"]:
            counts[column["status"]] += 1
    return counts


def _unwritten(row: Mapping[str, Any]) -> list[str]:
    out: list[str] = []
    for column in row["columns"]:
        if column["status"] != NOT_WRITTEN:
            continue
        why = (
            "its entry keeps its metadata under a top-level `meta`, and a "
            "`config.meta` beside it is a dbt parse error"
            if column.get("reason") == "top_level_meta"
            else f"dex could not splice into its YAML ({column.get('detail')})"
        )
        out.append(
            f"'{row['model']}.{column['column']}' is marked as personal data and was "
            f"not tagged for Elementary: {why}; add the tag to its "
            "`config.meta.tags` by hand"
        )
    if row["model_tag"] == NOT_WRITTEN:
        out.append(
            f"'{row['model']}' has personal-data columns and its model tag was not "
            f"written ({row.get('model_tag_detail')}); add it to its `config.tags` "
            "by hand"
        )
    return out
