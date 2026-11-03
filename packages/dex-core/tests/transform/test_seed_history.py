"""Applied seed declarations survive pricing, selection and build-mode changes."""

import json
from datetime import UTC, datetime

import pytest

from exmergo_dex_core.transform.artifact_runtime import dbt_runtime_key
from exmergo_dex_core.transform.seed_history import remember_seed_types

UID = "seed.proj.tasks"


def _artifacts(
    root, *, status="success", which="build", target="dev", invocation="run"
):
    """Write matching dbt artifacts for a seed with an explicitly typed column."""
    root.mkdir(parents=True, exist_ok=True)
    metadata = {
        "invocation_id": invocation,
        "generated_at": datetime.now(UTC).isoformat(),
        "dbt_version": dbt_runtime_key().removeprefix("dbt-"),
    }
    (root / "run_results.json").write_text(
        json.dumps(
            {
                "metadata": metadata,
                "args": {"which": which, "target": target},
                "results": [{"unique_id": UID, "status": status}],
            }
        )
    )
    (root / "manifest.json").write_text(
        json.dumps(
            {
                "metadata": metadata,
                "nodes": {
                    UID: {
                        "resource_type": "seed",
                        "config": {"column_types": {"`Due_At`": "timestamp"}},
                    }
                },
            }
        )
    )


@pytest.mark.parametrize(
    ("which", "status", "target"),
    [
        ("compile", "success", "dev"),
        ("build", "error", "dev"),
        ("build", "skipped", "dev"),
        ("build", "success", "other_dev"),
    ],
)
def test_only_successful_loads_update_seed_history(tmp_path, which, status, target):
    """Compilation, failures and another target cannot claim a type was loaded."""
    _artifacts(tmp_path, which=which, status=status, target=target)
    assert remember_seed_types(tmp_path, tmp_path, "dev") == {}


def test_narrow_builds_and_compiles_preserve_applied_seed_types(tmp_path):
    """A selected model must not erase the contract of an unselected seed."""
    _artifacts(tmp_path)
    expected = {UID: {"due_at": "timestamp"}}
    assert remember_seed_types(tmp_path, tmp_path, "dev") == expected
    _artifacts(tmp_path, which="compile")
    manifest_path = tmp_path / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["nodes"][UID]["config"] = {}
    manifest_path.write_text(json.dumps(manifest))
    assert remember_seed_types(tmp_path, tmp_path, "dev") == expected
    path = tmp_path / "run_results.json"
    results = json.loads(path.read_text())
    results["args"]["which"] = "build"
    results["results"] = [{"unique_id": "model.proj.reader", "status": "success"}]
    path.write_text(json.dumps(results))
    assert remember_seed_types(tmp_path, tmp_path, "dev") == expected


def test_an_unrelated_manifest_cannot_restate_applied_types(tmp_path):
    """Only the manifest from the seed's actual invocation describes its load."""
    _artifacts(tmp_path)
    path = tmp_path / "manifest.json"
    manifest = json.loads(path.read_text())
    manifest["metadata"]["invocation_id"] = "parse"
    path.write_text(json.dumps(manifest))
    assert remember_seed_types(tmp_path, tmp_path, "dev") == {}


def test_successful_cold_seed_loads_update_the_shared_dev_contract(tmp_path):
    """Cold and ordinary builds update the same physical seed's applied types."""
    _artifacts(tmp_path)
    assert remember_seed_types(tmp_path, tmp_path, "dev")[UID]
    cold = tmp_path / "cold"
    _artifacts(cold)
    path = cold / "manifest.json"
    manifest = json.loads(path.read_text())
    manifest["nodes"][UID]["config"] = {}
    path.write_text(json.dumps(manifest))
    assert remember_seed_types(cold, tmp_path, "dev") == {UID: {}}
    # Re-reading an older warm artifact cannot replace the newer cold load.
    assert remember_seed_types(tmp_path, tmp_path, "dev") == {UID: {}}
