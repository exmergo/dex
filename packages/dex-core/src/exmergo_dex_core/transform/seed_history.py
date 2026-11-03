"""Remember type declarations that successfully loaded each dev seed.

Pricing and `dbt ls` replace the manifest before seed checks run. This small
artifact retains the last applied declarations, even across narrow selections,
so removing a declaration can restore CSV inference with a full refresh.
"""

from __future__ import annotations

import json
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from .artifact_runtime import artifact_paths, dbt_runtime_key

SNAPSHOT = "dex_seed_column_types.json"


class SeedTypes(BaseModel):
    """Applied declarations for one target and runtime, keyed by seed unique ID."""

    model_config = ConfigDict(strict=True)
    target: str
    runtime: str
    seeds: dict[str, dict[str, str]] = Field(default_factory=dict)
    loaded_at: dict[str, str] = Field(default_factory=dict)


def column_types(config: dict) -> dict[str, str]:
    """Normalize declaration names without changing their warehouse type spelling."""
    declared = config.get("column_types") or {}
    if not isinstance(declared, dict):
        return {}
    return {
        str(name).strip('"`').lower(): kind
        for name, kind in declared.items()
        if isinstance(kind, str)
    }


def remember_seed_types(
    artifacts: Path, directory: Path, target: str
) -> dict[str, dict[str, str]]:
    """Merge successful seed loads into history and return the applied contracts.

    Only matching build/seed run results and manifests may update the record.
    Compile results, failed seeds and unselected seeds never replace it. Cold
    and ordinary builds share this record because they write the same dev seeds;
    their prerequisite-status histories remain independent.
    """
    state = SeedTypes(target=target, runtime=dbt_runtime_key())
    saved = directory / SNAPSHOT
    if saved.is_file():
        loaded = SeedTypes.model_validate_json(saved.read_bytes())
        if loaded.target == target and loaded.runtime == state.runtime:
            state = loaded
    paths = artifact_paths(artifacts)
    try:
        results = json.loads(paths.run_results.read_bytes())
        manifest = json.loads(paths.manifest.read_bytes())
        metadata = results.get("metadata") or {}
        manifest_meta = manifest.get("metadata") or {}
        args = results.get("args") or {}
        if (
            args.get("which") not in {"build", "seed"}
            or args.get("target") != target
            or f"dbt-{metadata.get('dbt_version')}" != state.runtime
            or not metadata.get("invocation_id")
            or metadata.get("invocation_id") != manifest_meta.get("invocation_id")
        ):
            return state.seeds
        changed = False
        generated_at = metadata.get("generated_at")
        if not isinstance(generated_at, str):
            return state.seeds
        stamp = datetime.fromisoformat(generated_at.replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            return state.seeds
        applied_at = stamp.astimezone(UTC).isoformat(timespec="microseconds")
        for result in results.get("results") or []:
            uid = result.get("unique_id") or ""
            if not uid.startswith("seed.") or result.get("status") != "success":
                continue
            node = (manifest.get("nodes") or {}).get(uid)
            if not isinstance(node, dict) or node.get("resource_type") != "seed":
                continue
            if state.loaded_at.get(uid, "") >= applied_at:
                continue
            declared = column_types(node.get("config") or {})
            state.seeds[uid] = declared
            state.loaded_at[uid] = applied_at
            changed = True
    except (OSError, ValueError, TypeError, AttributeError):
        return state.seeds
    if changed:
        directory.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=directory, delete=False
            ) as stream:
                temporary = Path(stream.name)
                stream.write(state.model_dump_json())
            temporary.replace(saved)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
    return state.seeds
