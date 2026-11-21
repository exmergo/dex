"""Each node's last Dex-run attempt, kept across builds (#1524).

dbt keeps one ``run_results.json`` per target path, and each run replaces it.
Dex's builds share one artifact directory, so a build that selects a few
nodes erases the record of every node the build before it attempted. A unit
test fixed and passed in one build, followed by a build of an unrelated
semantic model, left only an earlier hand-run ``dbt test`` that still showed it
failing, and ``maintain verify`` reported that failure as current.

After every building dbt run Dex starts, this keeps one entry per node in
``dex_node_runs.json`` beside the run results: the status and the run's
``invocation_id``, ``generated_at`` and command. Nothing else: no dbt message,
no compiled SQL, no row, so the file holds no warehouse text and no value.
Readers turn the entries back into dated runs (:func:`recorded_runs`), which
:func:`~.artifact_runtime.building_runs` offers beside the run results on disk
when a caller asks for them. Failures to read or write the file never change
what a build does.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

RECORD = "dex_node_runs.json"
_VERSION = 1


def _load(path: Path) -> dict[str, dict[str, Any]]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(document, dict) or document.get("version") != _VERSION:
        return {}
    nodes = document.get("nodes")
    if not isinstance(nodes, dict):
        return {}
    return {
        str(uid): entry
        for uid, entry in nodes.items()
        if isinstance(entry, dict) and isinstance(entry.get("status"), str)
    }


def record_node_runs(directory: Path | str) -> None:
    """Fold the run results in ``directory`` into its node record.

    Only a building command's results count (a compile's ``success`` says a node
    compiled), and an entry is replaced only by a run at least as new, so
    recording the same results twice, or an older file, changes nothing.
    """

    from .artifact_runtime import BUILDING_COMMANDS

    folder = Path(directory)
    try:
        document = json.loads((folder / "run_results.json").read_text("utf-8"))
    except (OSError, ValueError):
        return
    if not isinstance(document, dict):
        return
    args = document.get("args")
    command = str((args if isinstance(args, dict) else {}).get("which") or "build")
    if command not in BUILDING_COMMANDS:
        return
    raw_meta = document.get("metadata")
    meta = raw_meta if isinstance(raw_meta, dict) else {}
    invocation = meta.get("invocation_id")
    stamp = str(meta.get("generated_at") or "")
    path = folder / RECORD
    nodes = _load(path)
    changed = False
    for row in document.get("results") or []:
        if not isinstance(row, dict) or not row.get("unique_id"):
            continue
        uid = str(row["unique_id"])
        held = nodes.get(uid)
        if held is not None and str(held.get("generated_at") or "") > stamp:
            continue
        nodes[uid] = {
            "status": str(row.get("status") or "").lower(),
            "invocation_id": str(invocation) if invocation else None,
            "generated_at": stamp or None,
            "command": command,
        }
        changed = True
    if not changed:
        return
    temporary: Path | None = None
    try:
        folder.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=folder, delete=False
        ) as stream:
            temporary = Path(stream.name)
            json.dump({"version": _VERSION, "nodes": nodes}, stream, sort_keys=True)
        temporary.replace(path)
        temporary = None
    except OSError:
        return
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def recorded_runs(directory: Path) -> list[dict[str, Any]]:
    """The record in ``directory``, regrouped into one run per invocation.

    Each item is ``{"invocation_id", "generated_at", "command", "mtime",
    "results"}``, ``results`` holding only ``unique_id`` and ``status``, the
    shape of a ``run_results.json`` row with everything else left out.
    """

    path = directory / RECORD
    nodes = _load(path)
    if not nodes:
        return []
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return []
    runs: dict[tuple[str, str, str], dict[str, Any]] = {}
    for uid, entry in nodes.items():
        invocation = str(entry.get("invocation_id") or "")
        stamp = str(entry.get("generated_at") or "")
        command = str(entry.get("command") or "build")
        key = (invocation, stamp, command)
        run = runs.setdefault(
            key,
            {
                "invocation_id": invocation or None,
                "generated_at": stamp,
                "command": command,
                "mtime": mtime,
                "results": [],
            },
        )
        run["results"].append({"unique_id": uid, "status": entry["status"]})
    return list(runs.values())
