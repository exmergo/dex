"""Free historical context, saved before a pricing compile replaces run results.

This is the last available build artifact, not a baseline of the pre-plan tree.
Only a build may replace it; confirmation retries, compiles and seed refreshes
must not turn their statuses into historical evidence.
"""

from __future__ import annotations

import hashlib
import json
import tempfile
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from .artifact_runtime import artifact_paths, dbt_runtime_key

SNAPSHOT = "dex_previous_build.json"


class _PreviousBuild(BaseModel):
    model_config = ConfigDict(strict=True)

    runtime: str
    target: str
    invocation_id: str | None
    generated_at: str | None
    selection: list[str] | None
    run_results_sha256: str
    statuses: dict[str, str]


def preserve_previous_build(directory: Path, target: str) -> dict[str, Any]:
    """Save a build artifact, or reuse the saved one after a pricing retry.

    The snapshot contains no dbt messages, SQL, environment or profile arguments.
    Failures to read or save history never change whether the build may run.
    """

    snapshot = directory / SNAPSHOT
    current = artifact_paths(directory).run_results
    try:
        raw = current.read_bytes() if current.is_file() else None
        document = json.loads(raw) if raw is not None else None
        if document is not None and not isinstance(document, dict):
            raise ValueError("run results are not an object")
        args = (document or {}).get("args") or {}
        if not isinstance(args, dict):
            raise ValueError("run arguments are not an object")
        if document is not None and raw is not None and args.get("which") == "build":
            metadata = document.get("metadata") or {}
            run_target = args.get("target")
            if not isinstance(run_target, str):
                raise ValueError("build target is not recorded")
            selection = args.get("select")
            if isinstance(selection, str):
                selection = [selection]
            previous = _PreviousBuild(
                runtime=f"dbt-{metadata.get('dbt_version')}",
                target=run_target,
                invocation_id=metadata.get("invocation_id"),
                generated_at=metadata.get("generated_at"),
                selection=selection,
                run_results_sha256="sha256:" + hashlib.sha256(raw).hexdigest(),
                statuses={
                    row["unique_id"]: row["status"] for row in document["results"]
                },
            )
            # Persist before pricing: the next command may be the confirmed
            # retry, by which time run_results belongs to `dbt compile`.
            directory.mkdir(parents=True, exist_ok=True)
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(
                    mode="w", encoding="utf-8", dir=directory, delete=False
                ) as stream:
                    temporary = Path(stream.name)
                    stream.write(previous.model_dump_json())
                temporary.replace(snapshot)
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
        elif snapshot.is_file():
            previous = _PreviousBuild.model_validate_json(snapshot.read_bytes())
        else:
            return {"available": False, "reason_code": "no_previous_build"}
        if previous.target != target:
            return {"available": False, "reason_code": "incompatible_target"}
        if previous.runtime != dbt_runtime_key():
            return {"available": False, "reason_code": "incompatible_runtime"}
        return {"available": True, **previous.model_dump()}
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return {"available": False, "reason_code": "unreadable_previous_build"}


def node_context(previous: dict[str, Any] | None, unique_id: str) -> dict[str, Any]:
    """A node's earlier observation, without claiming causality or equivalence."""

    if not previous or not previous.get("available"):
        return previous or {"available": False, "reason_code": "no_previous_build"}
    context = {key: value for key, value in previous.items() if key != "statuses"}
    status = previous["statuses"].get(unique_id)
    if status is None:
        return {**context, "available": False, "reason_code": "node_not_observed"}
    return {**context, "status": status}
