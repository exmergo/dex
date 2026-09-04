"""Native Ossie define/update/plan and the shared atomic apply spine."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import yaml

from exmergo_dex_core.cli import main
from exmergo_dex_core.config import DexConfig, save_config
from exmergo_dex_core.edits import SemanticEditTarget
from exmergo_dex_core.ossie import OssieSemanticLayer

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


def test_define_and_update_enforce_the_semantic_model_namespace(
    tmp_path: Path, capsys
):
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


def test_apply_revalidates_unedited_documents_before_writing(
    tmp_path: Path, capsys
):
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
