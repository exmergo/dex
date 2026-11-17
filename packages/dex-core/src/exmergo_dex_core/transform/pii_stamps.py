"""PII stamps on the models a plan authors (#1100).

`explore` flags a column as PII; `transform plan --scaffold` and `maintain
reconcile` already carry that flag into the project as dbt metadata
(``config.meta.contains_pii`` on the model, plus ``pii_category`` on the column,
see :mod:`..guards.pii_meta`). A model an agent writes by hand through
``transform plan`` did not, so the same column read as sensitive or not depending
on which command authored the model. This module closes that gap with one
contract:

- **Which models.** Every model the plan *creates* is stamped, with a minimal
  YAML entry written where the plan writes none. A model that already exists is
  reported, never written: its YAML is somebody's, and the request did not
  settle its metadata.
- **Which columns.** An output whose value is an upstream column's, carried
  unchanged (an alias included) or through a function that keeps the value (a
  cast, case folding, trimming, ``coalesce``, ``min``/``max``), takes that
  column's flag. A measuring aggregate (``count``, ``sum``, ``avg``) is
  ``measured`` and a constant standing in for a flagged column is ``redacted``:
  neither is stamped. Any other expression reading a flagged column (a hash, a
  concatenation, a ``CASE``) is ``unresolved``: not stamped, and warned about,
  since only a person can say whether its values still identify someone.
- **Which evidence.** The input's cached profile, with ``pii_overrides`` already
  applied; failing that, the upstream model's own stamp. An upstream human
  ``contains_pii: false`` is respected. An input with neither is ``unchecked``.
- **Precedence.** A PII mark already on the output column, or on the model, is
  kept: a reviewer's ``false`` is never overwritten, and a disagreement is
  reported. A ``pii_overrides`` entry naming the output column clears it.

Nothing here reads a value, clears a flag, drops or redacts an output column, or
writes outside the ``config.meta`` keys :mod:`..guards.pii_meta` names.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import PurePosixPath
from typing import Any

import yaml

from ..guards.pii_meta import CONTAINS_PII, PII_CATEGORY, PII_MARK_KEYS, entry_meta
from .plans import EditKind, EditOp, PlanEdit

#: Statuses a reported column carries, strongest evidence first.
STAMPED = "stamped"
KEPT = "kept"
DISAGREEMENT = "disagreement"
OVERRIDDEN = "overridden"
CLEARED_UPSTREAM = "cleared_upstream"
MEASURED = "measured"
REDACTED = "redacted"
UNRESOLVED = "unresolved"
UNCHECKED = "unchecked"
NOT_WRITTEN = "not_written"

_STAMPING_KINDS = {"pass_through", "preserved"}


def stamp(
    edits: list[PlanEdit],
    view: Any,
    *,
    cache: Any,
    pii_overrides: Any,
    dialect: str,
    vars: Mapping[str, Any] | None,
    manifest: Mapping[str, Any] | None = None,
) -> tuple[list[PlanEdit], list[str], dict[str, Any] | None]:
    """Stamp the models ``edits`` create, and report every model they write.

    Returns the edits with the stamps folded into ``schema_yml`` edits (merged
    into one the plan already carries for the file), the warnings, and
    ``data.pii_metadata``: ``None`` when no written model reads a flagged
    column, so a plan with nothing to say carries no key.
    """

    from .. import model_versions as mv
    from . import declarations as decl

    files = decl.post_change_files(view, edits)
    prior = {path: source.content for path, source in view.files.items()}
    versions = mv.cached_version_map(files, view.model_paths)
    models: dict[str, tuple[str, bool]] = {}
    for edit in edits:
        if (
            edit.kind is not EditKind.MODEL_SQL
            or edit.op is not EditOp.UPSERT
            or not edit.path.endswith(".sql")
        ):
            continue
        label = mv.label_for_file(edit.path, versions)
        name = label or PurePosixPath(edit.path).stem
        created = (
            edit.path not in prior
            and decl.model_sql_path(prior, view.model_paths, name) is None
        )
        models[name] = (edit.path, created and label is None)

    report: dict[str, Any] = {}
    warnings: list[str] = []
    # A model this plan writes, created or rewritten, is judged by the plan's
    # own stamps when another model reads it through `ref()`: a cached profile
    # describes the relation as it was built before the plan (#1100). Only the
    # `ref()` is set aside, so a source sharing the bare name keeps its profile.
    fresh = frozenset(models)
    for model in _producers_first(models, files):
        sql_path, created = models[model]
        if mv.split_label(model)[1] is not None or mv.lookup(versions, model):
            columns = _columns(
                model,
                sql_path,
                files,
                view,
                cache,
                dialect,
                vars,
                manifest,
                pii_overrides,
                fresh & _refs(files.get(sql_path, "")),
            )
            if columns:
                report[model] = {
                    "created": created,
                    "status": UNCHECKED,
                    "reason_code": "versioned_model",
                    "columns": columns,
                }
            continue
        columns = _columns(
            model,
            sql_path,
            files,
            view,
            cache,
            dialect,
            vars,
            manifest,
            pii_overrides,
            fresh & _refs(files.get(sql_path, "")),
        )
        if not columns:
            continue
        entry: dict[str, Any] = {"created": created, "columns": columns}
        report[model] = entry
        _resolve_precedence(model, columns, files, pii_overrides)
        if not created:
            for column in columns.values():
                if column["status"] == STAMPED:
                    column["status"] = NOT_WRITTEN
                    column["reason_code"] = "existing_model"
            continue
        if any(c["status"] in (STAMPED, KEPT) for c in columns.values()):
            path = decl.schema_path(files, model) or decl._default_schema_path(
                sql_path, model
            )
            if _has_top_level_meta(files.get(path), model):
                for column in columns.values():
                    if column["status"] == STAMPED:
                        column["status"] = NOT_WRITTEN
                        column["reason_code"] = "top_level_meta"
                entry["model_stamp"] = NOT_WRITTEN
            else:
                try:
                    files[path], entry["model_stamp"] = _write(
                        files.get(path), model, columns
                    )
                except (decl.RewriteError, ValueError, yaml.YAMLError) as exc:
                    # Metadata dex adds on its own must never cost the plan:
                    # a layout the splice cannot write (a flow-style column
                    # list, say) is reported for a person to stamp by hand.
                    for column in columns.values():
                        if column["status"] == STAMPED:
                            column["status"] = NOT_WRITTEN
                            column["reason_code"] = "unwritable_yaml"
                    entry["model_stamp"] = NOT_WRITTEN
                    entry["detail"] = f"{type(exc).__name__}: {exc}"
                    continue
                edits = _merge(edits, path, files[path])
                entry["path"] = path

    for model, entry in report.items():
        warnings.extend(_warnings(model, entry))
    return edits, warnings, ({"models": report} if report else None)


_REF = r"""ref\(\s*(?:['"][^'"]+['"]\s*,\s*)?['"]([^'"]+)['"]"""


def _refs(sql: str) -> set[str]:
    """The model names ``sql`` reads through ``ref()``, one- or two-argument.

    A ``source()`` renders to the same relation token as a ``ref()`` in the
    lineage, so this is what tells a project model from a source table that
    happens to share its name."""

    import re

    return set(re.findall(_REF, sql))


def _producers_first(
    models: Mapping[str, tuple[str, bool]], files: Mapping[str, str]
) -> list[str]:
    """The plan's models with each one after the plan's models it ``ref()``s,
    so a stamp written on a new staging model is the evidence for a new mart
    reading it in the same plan. Ties, and a cycle, fall back to name order."""

    refs = {
        model: {
            found
            for found in _refs(files.get(path, ""))
            if found in models and found != model
        }
        for model, (path, _created) in models.items()
    }
    ordered: list[str] = []
    pending = sorted(models)
    while pending:
        ready = [m for m in pending if refs[m] <= set(ordered)] or pending[:1]
        ordered.append(ready[0])
        pending.remove(ready[0])
    return ordered


# --- reading -----------------------------------------------------------------


def _columns(
    model: str,
    sql_path: str,
    files: dict[str, str],
    view: Any,
    cache: Any,
    dialect: str,
    vars: Mapping[str, Any] | None,
    manifest: Mapping[str, Any] | None,
    pii_overrides: Any = None,
    fresh: frozenset[str] = frozenset(),
) -> dict[str, dict[str, Any]]:
    """Per output column that reads a flagged (or unreadable) input, its status
    before precedence: ``stamped`` meaning "would be stamped"."""

    from . import drafting

    reading = drafting.read(
        files[sql_path],
        cache,
        dialect=dialect,
        vars=vars,
        files=files,
        model_paths=view.model_paths,
        manifest=manifest,
    )
    out: dict[str, dict[str, Any]] = {}
    if reading.produced is None:
        reasons = reading.incomplete or ["the SELECT list could not be read"]
        # A model whose outputs cannot be read is reported only where it reads
        # an input something flags, so a plan over clean inputs stays quiet.
        if _reads_flagged(reading, files, view, cache, manifest, fresh):
            out["*"] = {
                "status": UNCHECKED,
                "reason_code": "unreadable_sql",
                "detail": reasons[0],
            }
        return out

    for name in reading.produced:
        paths = reading.value_paths.get(name)
        if paths is None:
            if _reads_flagged(reading, files, view, cache, manifest, fresh):
                out[name] = {"status": UNCHECKED, "reason_code": "lineage_unfollowed"}
            continue
        if not paths:
            redacted = _same_name_flag(
                name, reading, files, view, cache, manifest, fresh
            )
            if redacted is not None:
                out[name] = {
                    "status": REDACTED,
                    "from": redacted[0],
                    "category": redacted[1],
                }
            continue
        flagged: list[tuple[str, dict[str, Any]]] = []
        unprofiled: list[str] = []
        cleared: list[str] = []
        for origin, kind in paths:
            evidence = _evidence(
                origin, files, view, cache, manifest, pii_overrides, fresh
            )
            source = f"{origin.relation}.{origin.column}"
            if evidence is None:
                unprofiled.append(source)
            elif evidence.get("cleared"):
                cleared.append(source)
            elif evidence.get("flagged"):
                flagged.append((kind, {**evidence, "from": source}))
        if flagged:
            stamping = [f for f in flagged if f[0] in _STAMPING_KINDS]
            if stamping:
                kind, evidence = max(
                    stamping, key=lambda f: f[1].get("confidence") or 0.0
                )
                status = STAMPED
            else:
                kinds = {f[0] for f in flagged}
                status = UNRESOLVED if "unresolved" in kinds else MEASURED
                kind, evidence = flagged[0]
            column: dict[str, Any] = {
                "status": status,
                "from": evidence["from"],
                "evidence": evidence["evidence"],
                "lineage": kind,
            }
            if evidence.get("category"):
                column["category"] = evidence["category"]
            out[name] = column
        elif unprofiled:
            # A cleared input never vouches for another the lineage also
            # reads: `coalesce(note, phone)` is unchecked while `phone` is.
            out[name] = {
                "status": UNCHECKED,
                "reason_code": "unprofiled_input",
                "from": unprofiled[0],
            }
        elif cleared:
            out[name] = {"status": CLEARED_UPSTREAM, "from": cleared[0]}
    return out


def _evidence(
    origin: Any,
    files: Mapping[str, str],
    view: Any,
    cache: Any,
    manifest: Mapping[str, Any] | None,
    pii_overrides: Any = None,
    fresh: frozenset[str] = frozenset(),
) -> dict[str, Any] | None:
    """What says whether ``origin`` is PII: the profile, else the upstream
    model's stamp. ``None`` when nothing says either way. A reviewed override
    naming the upstream column clears it, whichever of the two would speak."""

    from . import drafting

    dataset = (
        None
        if origin.is_ref and origin.relation in fresh
        else drafting._dataset_for_model(
            cache,
            origin.relation,
            files,
            view.model_paths,
            manifest=manifest if origin.is_ref else None,
        )
    )
    profile = drafting._column(dataset, origin.column)
    if profile is not None:
        if profile.pii is None:
            # Profiled clean, or cleared by a reviewed override (the cache is
            # masked): either way the profile has spoken.
            return {"flagged": False}
        return {
            "flagged": True,
            "category": profile.pii.category.value,
            "confidence": profile.pii.confidence,
            "evidence": "profile",
        }
    if origin.is_ref:
        if (
            pii_overrides is not None
            and f"{origin.relation}.{origin.column}" in pii_overrides
        ):
            return {"cleared": True}
        marked = _upstream_mark(files, origin.relation, origin.column)
        if marked is not None:
            return marked
    if dataset is not None:
        # Profiled, and the column is not in it: the lineage reads a column the
        # profile does not have, which is nothing to stamp from.
        return None
    return None


def _upstream_mark(
    files: Mapping[str, str], model: str, column: str
) -> dict[str, Any] | None:
    """An upstream model's own stamp on ``column``: flagged, cleared by a
    reviewer (``contains_pii: false``), or ``None`` where it says nothing."""

    entry = _entry(files, model)
    if entry is None:
        return None
    for item in entry.get("columns") or []:
        if not isinstance(item, dict) or str(item.get("name", "")).lower() != (
            column.lower()
        ):
            continue
        meta = entry_meta(item)
        if not any(key in meta for key in PII_MARK_KEYS):
            return None
        if _says(meta):
            found: dict[str, Any] = {
                "flagged": True,
                "evidence": "upstream_metadata",
            }
            if isinstance(meta.get(PII_CATEGORY), str):
                found["category"] = meta[PII_CATEGORY]
            return found
        return {"cleared": True}
    return None


def _says(meta: Mapping[str, Any]) -> bool:
    return any(bool(meta.get(key)) for key in PII_MARK_KEYS)


def _entry(files: Mapping[str, str], model: str) -> dict[str, Any] | None:
    for path in sorted(files):
        if not path.endswith((".yml", ".yaml")) or path == "dbt_project.yml":
            continue
        try:
            parsed = yaml.safe_load(files[path])
        except yaml.YAMLError:
            continue
        if not isinstance(parsed, dict):
            continue
        for entry in parsed.get("models") or []:
            if isinstance(entry, dict) and entry.get("name") == model:
                return entry
    return None


def _reads_flagged(
    reading: Any,
    files: Mapping[str, str],
    view: Any,
    cache: Any,
    manifest: Mapping[str, Any] | None,
    fresh: frozenset[str] = frozenset(),
) -> bool:
    """Whether any relation the model reads has a column something flags."""

    return bool(_flagged_columns(reading, files, view, cache, manifest, fresh))


def _flagged_columns(
    reading: Any,
    files: Mapping[str, str],
    view: Any,
    cache: Any,
    manifest: Mapping[str, Any] | None,
    fresh: frozenset[str] = frozenset(),
) -> dict[str, tuple[str, str | None]]:
    """Lowered column name to (``relation.column``, category) over every input."""

    from . import drafting

    found: dict[str, tuple[str, str | None]] = {}
    inputs = list(reading.input_origins) or [
        # A statement read stopped early (a set operation) names no inputs;
        # every relation its jinja rendered is one.
        (relation, True)
        for relation in reading.tokens.values()
    ]
    for relation, is_ref in inputs:
        if is_ref and relation in fresh:
            dataset = None
        else:
            dataset = drafting._dataset_for_model(
                cache,
                relation,
                files,
                view.model_paths,
                manifest=manifest if is_ref else None,
            )
        for column in dataset.columns if dataset is not None else []:
            if column.pii is not None:
                found.setdefault(
                    column.name.lower(),
                    (f"{relation}.{column.name}", column.pii.category.value),
                )
        entry = _entry(files, relation) if is_ref else None
        for item in (entry or {}).get("columns") or []:
            if not isinstance(item, dict) or not isinstance(item.get("name"), str):
                continue
            meta = entry_meta(item)
            if _says(meta):
                category = meta.get(PII_CATEGORY)
                found.setdefault(
                    item["name"].lower(),
                    (
                        f"{relation}.{item['name']}",
                        category if isinstance(category, str) else None,
                    ),
                )
    return found


def _same_name_flag(
    name: str,
    reading: Any,
    files: Mapping[str, str],
    view: Any,
    cache: Any,
    manifest: Mapping[str, Any] | None,
    fresh: frozenset[str] = frozenset(),
) -> tuple[str, str | None] | None:
    """A flagged input column a constant output stands in for, by its name."""

    return _flagged_columns(reading, files, view, cache, manifest, fresh).get(
        name.lower()
    )


# --- precedence ----------------------------------------------------------------


def _resolve_precedence(
    model: str,
    columns: dict[str, dict[str, Any]],
    files: Mapping[str, str],
    pii_overrides: Any,
) -> None:
    """Keep what a person or an earlier writer stated, and honor an override."""

    entry = _entry(files, model) or {}
    stated = {
        str(item.get("name", "")).lower(): entry_meta(item)
        for item in entry.get("columns") or []
        if isinstance(item, dict)
    }
    for name, column in columns.items():
        if column["status"] != STAMPED:
            continue
        if pii_overrides is not None and f"{model}.{name}" in pii_overrides:
            column["status"] = OVERRIDDEN
            continue
        meta = stated.get(name.lower()) or {}
        if any(key in meta for key in PII_MARK_KEYS):
            column["status"] = KEPT if _says(meta) else DISAGREEMENT


def _has_top_level_meta(content: str | None, model: str) -> bool:
    """A model entry with a top-level ``meta`` and no ``config.meta``: writing
    ``config.meta`` beside it is a dbt parse error, so nothing is written."""

    if not content:
        return False
    try:
        parsed = yaml.safe_load(content)
    except yaml.YAMLError:
        return False
    for entry in (parsed or {}).get("models") or [] if isinstance(parsed, dict) else []:
        if isinstance(entry, dict) and entry.get("name") == model:
            config = entry.get("config")
            has_config_meta = isinstance(config, dict) and "meta" in config
            return "meta" in entry and not has_config_meta
    return False


# --- writing -------------------------------------------------------------------


def _write(
    content: str | None, model: str, columns: dict[str, dict[str, Any]]
) -> tuple[str, str]:
    """``content`` with each ``stamped`` column's stamp and the model's.

    Returns the text and the model-level outcome: ``stamped``, ``kept`` (a mark
    already there that says PII) or ``disagreement`` (one that says it is not).
    """

    from . import declarations as decl

    text = decl._with_entry(content, model)
    entry = lambda root: decl._model_entry(root, model)  # noqa: E731
    text = decl._restyle_flow_entry(text, entry)
    for name, column in columns.items():
        if column["status"] != STAMPED:
            continue
        text = decl._ensure_column(text, model, name)
        locate = decl._column_locator(model, name)
        root = decl._compose(text)
        if decl._carries_anchor(root, locate(root)):
            # A column whose entry, or its config, is shared through a YAML
            # anchor: a stamp spliced in would mark every column that aliases
            # it. Refused here, and reported `not_written` by the caller.
            raise decl.RewriteError(
                f"the entry for column '{name}' shares a YAML anchor with another"
            )
        text = decl._set_meta(text, locate, CONTAINS_PII, True)
        if column.get("category"):
            text = decl._set_meta(text, locate, PII_CATEGORY, column["category"])

    plain = yaml.safe_load(text)
    model_entry = next(
        e
        for e in plain.get("models") or []
        if isinstance(e, dict) and e.get("name") == model
    )
    meta = entry_meta(model_entry)
    if any(key in meta for key in PII_MARK_KEYS):
        return text, (KEPT if _says(meta) else DISAGREEMENT)
    root = decl._compose(text)
    config = decl._child(entry(root), "config")
    if config is not None and decl._carries_anchor(root, config[1]):
        # The model's `config` is shared through a YAML anchor: a mark spliced
        # in would land on every entry that aliases it.
        raise decl.RewriteError(f"the config of '{model}' shares a YAML anchor")
    return decl._set_meta(text, entry, CONTAINS_PII, True), STAMPED


def _merge(edits: list[PlanEdit], path: str, content: str) -> list[PlanEdit]:
    merged = list(edits)
    for i, edit in enumerate(merged):
        if edit.path == path and edit.op is EditOp.UPSERT:
            merged[i] = edit.model_copy(update={"new_content": content})
            return merged
    merged.append(PlanEdit(path=path, kind=EditKind.SCHEMA_YML, new_content=content))
    return merged


# --- telling -------------------------------------------------------------------


def _warnings(model: str, entry: Mapping[str, Any]) -> list[str]:
    out: list[str] = []
    fixes = (
        "stamp it yourself (`config.meta.contains_pii: true` and `pii_category` on "
        "the column, in a `schema_yml` edit) if its values still identify "
        f"someone, or record `- {{column: {model}.<column>}}` under pii_overrides "
        "in .dex/config.yml if a review says they do not"
    )
    for name, column in entry["columns"].items():
        status = column["status"]
        source = column.get("from", "an input")
        if status == UNRESOLVED:
            out.append(
                f"'{model}.{name}' is computed from {source}, which is PII-flagged, "
                "through an expression dex cannot judge (a hash, a concatenation, a "
                "CASE), so it was not stamped: " + fixes.replace("<column>", name)
            )
        elif status == DISAGREEMENT:
            out.append(
                f"'{model}.{name}' is marked as not PII in its YAML, while "
                f"{source} it carries is flagged; the mark was kept, as a person's "
                "word is. If the review stands, record it under pii_overrides so "
                "the two agree"
            )
        elif status == NOT_WRITTEN and column.get("reason_code") == "existing_model":
            out.append(
                f"'{model}.{name}' carries {source}, which is PII-flagged, and has "
                "no stamp; '{model}' already exists, so dex reports this rather "
                "than editing its YAML: ".replace("{model}", model)
                + fixes.replace("<column>", name)
            )
        elif status == NOT_WRITTEN and column.get("reason_code") == "unwritable_yaml":
            out.append(
                f"'{model}.{name}' carries {source}, which is PII-flagged, and was "
                f"not stamped: dex could not splice into the YAML entry for "
                f"'{model}' predictably ({entry.get('detail', 'unsupported layout')})"
                ". Rewrite that entry in block style, or stamp it by hand"
            )
        elif status == NOT_WRITTEN and column.get("reason_code") == "top_level_meta":
            out.append(
                f"'{model}.{name}' carries {source}, which is PII-flagged, and was "
                f"not stamped: the entry for '{model}' keeps its metadata under a "
                "top-level `meta`, and a `config.meta` beside it is a dbt parse "
                "error. Move those keys under `config.meta`, then plan again"
            )
    unchecked = {
        name: column["from"]
        for name, column in entry["columns"].items()
        if column["status"] == UNCHECKED
        and column.get("reason_code") == "unprofiled_input"
    }
    if unchecked:
        inputs = sorted({source.rsplit(".", 1)[0] for source in unchecked.values()})
        out.append(
            f"{len(unchecked)} column(s) of '{model}' read inputs that no profile "
            f"and no stamp covers ({', '.join(inputs)}), so whether they are PII "
            "was not checked and they were not stamped: profile those inputs "
            "(`explore profile`, with `--target dev` for a model built on dev), "
            "then plan again"
        )
    if entry.get("model_stamp") == DISAGREEMENT:
        out.append(
            f"'{model}' is marked as not containing PII in its YAML while columns "
            "dex stamped carry flagged inputs; the model-level mark was kept"
        )
    return out
