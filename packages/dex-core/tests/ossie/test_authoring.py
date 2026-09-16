"""Native Ossie define/update/plan and the shared atomic apply spine."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from exmergo_dex_core.cache import CacheProvenance, ColumnProfile, Dataset, DexCache
from exmergo_dex_core.cli import main
from exmergo_dex_core.config import DexConfig, save_config
from exmergo_dex_core.edits import SemanticEditTarget
from exmergo_dex_core.ossie import OssieSemanticLayer
from exmergo_dex_core.storage import FilesystemStore

from .conftest import dataset, document, field, model, write


def _configure(root: Path, files: list[str]) -> None:
    save_config(
        DexConfig(
            connector="duckdb",
            duckdb={"path": "unused.duckdb"},
            semantic={"vendor": "ossie", "ossie": {"files": files}},
        ),
        root,
    )


def _payload(root: Path, edits: list[dict], name: str = "edits.json") -> Path:
    path = root / name
    path.write_text(json.dumps({"edits": edits}), encoding="utf-8")
    return path


def _run(root: Path, capsys, *args: str) -> dict:
    code = main(["--repo-root", str(root), *args])
    payload = json.loads(capsys.readouterr().out)
    payload["_exit"] = code
    return payload


def _yaml(name: str) -> str:
    return yaml.safe_dump(
        document(
            model(
                name,
                dataset("orders", "demo.main.orders", field("order_id")),
            )
        ),
        sort_keys=False,
    )


def _save_cache(
    root: Path,
    *datasets: Dataset,
    inventory_namespaces: list[str] | None = None,
    connector: str = "duckdb",
) -> None:
    FilesystemStore(root).save_cache(
        DexCache(
            datasets=list(datasets),
            provenance=CacheProvenance(
                connector=connector,
                inventory_namespaces=inventory_namespaces or [],
            ),
        )
    )


def _profile(identifier: str, *columns: str) -> Dataset:
    return Dataset(
        identifier=identifier,
        columns=[ColumnProfile(name=name, data_type="VARCHAR") for name in columns],
    )


def test_ossie_claims_the_semantic_edit_capability_not_the_project_tier():
    from exmergo_dex_core.adapters.project import EditableProject

    layer = OssieSemanticLayer(".", ["configured.ossie.yaml"])

    assert isinstance(layer, SemanticEditTarget)
    assert not isinstance(layer, EditableProject)


def test_define_plans_a_configured_missing_document_and_apply_writes_exact_bytes(
    tmp_path: Path, capsys
):
    _configure(tmp_path, ["semantics/new.ossie.yaml"])
    authored = "# retained comment\n" + _yaml("commerce")
    payload = _payload(
        tmp_path,
        [{"path": "semantics/new.ossie.yaml", "content": authored}],
    )

    planned = _run(
        tmp_path,
        capsys,
        "semantic",
        "ossie",
        "define",
        "commerce semantics",
        "--edits-file",
        str(payload),
    )

    assert planned["_exit"] == 0, planned
    assert planned["data"]["defined"] == ["commerce"]
    assert planned["data"]["updated"] == []
    assert not (tmp_path / "semantics/new.ossie.yaml").exists()
    assert any("byte-for-byte" in warning for warning in planned["warnings"])

    applied = _run(tmp_path, capsys, "transform", "apply")
    assert applied["_exit"] == 0, applied
    assert (tmp_path / "semantics/new.ossie.yaml").read_text("utf-8") == authored


def test_define_and_update_enforce_the_semantic_model_namespace(tmp_path: Path, capsys):
    write(
        tmp_path,
        "existing.ossie.yaml",
        document(model("commerce", dataset("x", "x"))),
    )
    _configure(tmp_path, ["existing.ossie.yaml"])

    redefine = _payload(
        tmp_path,
        [{"path": "existing.ossie.yaml", "content": _yaml("commerce")}],
        "redefine.json",
    )
    result = _run(
        tmp_path,
        capsys,
        "semantic",
        "ossie",
        "define",
        "bad",
        "--edits-file",
        str(redefine),
    )
    assert result["_exit"] == 1
    assert "already defined" in result["errors"][0]

    _configure(tmp_path, ["existing.ossie.yaml", "new.ossie.yaml"])
    new = _payload(
        tmp_path,
        [{"path": "new.ossie.yaml", "content": _yaml("support")}],
        "new.json",
    )
    result = _run(
        tmp_path,
        capsys,
        "semantic",
        "ossie",
        "update",
        "bad",
        "--edits-file",
        str(new),
    )
    assert result["_exit"] == 1
    assert "do not exist" in result["errors"][0]


def test_plan_accepts_new_and_existing_models_atomically(tmp_path: Path, capsys):
    write(
        tmp_path,
        "existing.ossie.yaml",
        document(model("commerce", dataset("x", "x"))),
    )
    _configure(tmp_path, ["existing.ossie.yaml", "new.ossie.yaml"])
    mixed = _payload(
        tmp_path,
        [
            {"path": "existing.ossie.yaml", "content": _yaml("commerce")},
            {"path": "new.ossie.yaml", "content": _yaml("support")},
        ],
    )

    result = _run(
        tmp_path,
        capsys,
        "semantic",
        "ossie",
        "plan",
        "mixed",
        "--edits-file",
        str(mixed),
    )

    assert result["_exit"] == 0, result
    assert result["data"]["defined"] == ["support"]
    assert result["data"]["updated"] == ["commerce"]


def test_invalid_or_unconfigured_content_stores_no_plan(tmp_path: Path, capsys):
    write(
        tmp_path,
        "existing.ossie.yaml",
        document(model("commerce", dataset("x", "x"))),
    )
    _configure(tmp_path, ["existing.ossie.yaml"])
    invalid = _payload(
        tmp_path,
        [{"path": "existing.ossie.yaml", "content": "version: wrong\n"}],
    )

    result = _run(
        tmp_path,
        capsys,
        "semantic",
        "ossie",
        "update",
        "invalid",
        "--edits-file",
        str(invalid),
    )
    assert result["_exit"] == 1
    assert "no plan was stored" in result["errors"][0]
    assert not list((tmp_path / ".dex" / "plans").glob("*.json"))

    outside = _payload(
        tmp_path,
        [{"path": "not-configured.ossie.yaml", "content": _yaml("other")}],
        "outside.json",
    )
    result = _run(
        tmp_path,
        capsys,
        "semantic",
        "ossie",
        "define",
        "outside",
        "--edits-file",
        str(outside),
    )
    assert result["_exit"] == 1
    assert "not configured" in result["errors"][0]
    assert not (tmp_path / "not-configured.ossie.yaml").exists()


def test_stale_edit_refuses_the_whole_apply(tmp_path: Path, capsys):
    first = write(
        tmp_path,
        "first.ossie.yaml",
        document(model("first", dataset("x", "x"))),
    )
    second = write(
        tmp_path,
        "second.ossie.yaml",
        document(model("second", dataset("x", "x"))),
    )
    _configure(tmp_path, [first, second])
    before_second = (tmp_path / second).read_text("utf-8")
    payload = _payload(
        tmp_path,
        [
            {"path": first, "content": _yaml("first")},
            {"path": second, "content": _yaml("second")},
        ],
    )
    planned = _run(
        tmp_path,
        capsys,
        "semantic",
        "ossie",
        "update",
        "both",
        "--edits-file",
        str(payload),
    )
    assert planned["_exit"] == 0, planned

    (tmp_path / first).write_text("# human edit\n" + _yaml("first"), encoding="utf-8")
    applied = _run(tmp_path, capsys, "transform", "apply")

    assert applied["status"] == "needs_confirmation"
    assert applied["data"]["conflicts"][0]["path"] == first
    assert (tmp_path / second).read_text("utf-8") == before_second


def test_apply_revalidates_unedited_documents_before_writing(tmp_path: Path, capsys):
    first = write(
        tmp_path,
        "first.ossie.yaml",
        document(model("first", dataset("x", "x"))),
    )
    second = write(
        tmp_path,
        "second.ossie.yaml",
        document(model("second", dataset("x", "x"))),
    )
    _configure(tmp_path, [first, second])
    replacement = _yaml("first")
    payload = _payload(
        tmp_path,
        [{"path": first, "content": replacement}],
    )
    planned = _run(
        tmp_path,
        capsys,
        "semantic",
        "ossie",
        "update",
        "first only",
        "--edits-file",
        str(payload),
    )
    assert planned["_exit"] == 0, planned
    before_first = (tmp_path / first).read_text("utf-8")

    (tmp_path / second).write_text("version: wrong\n", encoding="utf-8")
    applied = _run(tmp_path, capsys, "transform", "apply")

    assert applied["_exit"] == 1
    assert "invalid at apply time" in applied["errors"][0]
    assert (tmp_path / first).read_text("utf-8") == before_first


def test_native_authoring_needs_no_sql_dbt_or_metricflow_runtime(tmp_path: Path):
    _configure(tmp_path, ["new.ossie.yaml"])
    payload = _payload(
        tmp_path,
        [{"path": "new.ossie.yaml", "content": _yaml("commerce")}],
    )
    argv = [
        "--repo-root",
        str(tmp_path),
        "semantic",
        "ossie",
        "define",
        "x",
        "--edits-file",
        str(payload),
    ]
    probe = (
        "import sys; "
        "sys.modules['sqlglot'] = None; "
        "sys.modules['dbt'] = None; "
        "sys.modules['metricflow'] = None; "
        "from exmergo_dex_core.cli import main; "
        f"raise SystemExit(main({argv!r}))"
    )

    completed = subprocess.run(  # noqa: S603 (fixed interpreter and authored args)
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    result = json.loads(completed.stdout)
    assert result["status"] == "ok"
    assert any("syntax was not checked" in note for note in result["warnings"])


def test_absent_cache_is_an_unknown_note_not_a_refusal(tmp_path: Path, capsys):
    _configure(tmp_path, ["new.ossie.yaml"])
    payload = _payload(
        tmp_path,
        [{"path": "new.ossie.yaml", "content": _yaml("commerce")}],
    )

    result = _run(
        tmp_path,
        capsys,
        "semantic",
        "ossie",
        "define",
        "without cache",
        "--edits-file",
        str(payload),
    )

    assert result["_exit"] == 0, result
    assert any(
        "there is no exploration cache" in note for note in result["data"]["notes"]
    )
    assert any(
        "no warehouse connection was opened" in note for note in result["data"]["notes"]
    )


def test_unprofiled_columns_are_an_unknown_note(tmp_path: Path, capsys):
    _configure(tmp_path, ["new.ossie.yaml"])
    _save_cache(
        tmp_path,
        Dataset(identifier="demo.main.orders"),
        inventory_namespaces=["demo.main"],
    )
    payload = _payload(
        tmp_path,
        [{"path": "new.ossie.yaml", "content": _yaml("commerce")}],
    )

    result = _run(
        tmp_path,
        capsys,
        "semantic",
        "ossie",
        "define",
        "unprofiled",
        "--edits-file",
        str(payload),
    )

    assert result["_exit"] == 0, result
    assert any("columns are unprofiled" in note for note in result["data"]["notes"])


def test_relation_absent_from_a_complete_cached_namespace_refuses_the_plan(
    tmp_path: Path, capsys
):
    _configure(tmp_path, ["new.ossie.yaml"])
    _save_cache(
        tmp_path,
        _profile("demo.main.customers", "customer_id"),
        inventory_namespaces=["demo.main"],
    )
    payload = _payload(
        tmp_path,
        [{"path": "new.ossie.yaml", "content": _yaml("commerce")}],
    )

    result = _run(
        tmp_path,
        capsys,
        "semantic",
        "ossie",
        "define",
        "missing relation",
        "--edits-file",
        str(payload),
    )

    assert result["_exit"] == 1
    assert "cached inventory" in result["errors"][0]
    assert "demo.main.orders" in result["errors"][0]
    assert "no plan was stored" in result["errors"][0]
    assert not list((tmp_path / ".dex" / "plans").glob("*.json"))


@pytest.mark.parametrize(
    ("authored", "expected"),
    [
        (
            document(
                model(
                    "commerce",
                    dataset(
                        "orders",
                        "demo.main.orders",
                        field("missing_field"),
                    ),
                )
            ),
            "field 'missing_field' names column 'missing_field'",
        ),
        (
            document(
                model(
                    "commerce",
                    dataset(
                        "orders",
                        "demo.main.orders",
                        field("order_id"),
                        primary_key=["missing_key"],
                    ),
                )
            ),
            "primary_key names column 'missing_key'",
        ),
        (
            document(
                model(
                    "commerce",
                    dataset(
                        "orders",
                        "demo.main.orders",
                        field("order_id"),
                    ),
                    dataset(
                        "customers",
                        "demo.main.customers",
                        field("customer_id"),
                        primary_key=["customer_id"],
                    ),
                    relationships=[
                        {
                            "name": "orders_to_customers",
                            "from": "orders",
                            "to": "customers",
                            "from_columns": ["missing_fk"],
                            "to_columns": ["customer_id"],
                        }
                    ],
                )
            ),
            "relationship 'orders_to_customers' from_columns names column 'missing_fk'",
        ),
    ],
    ids=["field", "key", "relationship-endpoint"],
)
def test_columns_known_missing_from_cached_profiles_refuse_the_plan(
    tmp_path: Path, capsys, authored, expected
):
    _configure(tmp_path, ["new.ossie.yaml"])
    _save_cache(
        tmp_path,
        _profile("demo.main.orders", "order_id", "customer_id"),
        _profile("demo.main.customers", "customer_id"),
        inventory_namespaces=["demo.main"],
    )
    payload = _payload(
        tmp_path,
        [
            {
                "path": "new.ossie.yaml",
                "content": yaml.safe_dump(authored, sort_keys=False),
            }
        ],
    )

    result = _run(
        tmp_path,
        capsys,
        "semantic",
        "ossie",
        "define",
        "bad column",
        "--edits-file",
        str(payload),
    )

    assert result["_exit"] == 1
    assert expected in result["errors"][0]
    assert "no plan was stored" in result["errors"][0]
    assert not list((tmp_path / ".dex" / "plans").glob("*.json"))


def test_computed_fields_and_query_sources_are_not_overvalidated(
    tmp_path: Path, capsys
):
    _configure(tmp_path, ["new.ossie.yaml"])
    _save_cache(
        tmp_path,
        _profile("demo.main.orders", "order_id", "amount", "discount"),
        inventory_namespaces=["demo.main"],
    )
    authored = document(
        model(
            "commerce",
            dataset(
                "orders",
                "demo.main.orders",
                field("order_id"),
                field("net", "amount - discount"),
            ),
            dataset(
                "recent",
                "SELECT * FROM demo.main.missing",
                field("not_cache_provable"),
            ),
        )
    )
    payload = _payload(
        tmp_path,
        [
            {
                "path": "new.ossie.yaml",
                "content": yaml.safe_dump(authored, sort_keys=False),
            }
        ],
    )

    result = _run(
        tmp_path,
        capsys,
        "semantic",
        "ossie",
        "define",
        "opaque references",
        "--edits-file",
        str(payload),
    )

    assert result["_exit"] == 0, result
    notes = " ".join(result["data"]["notes"])
    assert "query-backed" in notes
    assert "computed" in notes


def test_cache_validation_never_opens_a_warehouse_connection(
    tmp_path: Path, capsys, monkeypatch
):
    _configure(tmp_path, ["new.ossie.yaml"])
    _save_cache(
        tmp_path,
        _profile("demo.main.orders", "order_id"),
        inventory_namespaces=["demo.main"],
    )
    payload = _payload(
        tmp_path,
        [{"path": "new.ossie.yaml", "content": _yaml("commerce")}],
    )

    def forbidden(*_args, **_kwargs):
        raise AssertionError("cache validation opened a warehouse connection")

    monkeypatch.setattr("exmergo_dex_core.connect.open_adapter", forbidden)
    result = _run(
        tmp_path,
        capsys,
        "semantic",
        "ossie",
        "define",
        "cache only",
        "--edits-file",
        str(payload),
    )

    assert result["_exit"] == 0, result
    assert any(
        "no warehouse connection was opened" in note for note in result["data"]["notes"]
    )
