"""The native semantic authoring route is vendor-neutral.

`semantic <vendor> define|update|plan` reaches one route for every vendor in
`SEMANTIC_DOCUMENT_AUTHORING`, and the layer validates its own documents. These
tests register a vendor that exists only in this module's tables, so a route
that still knew a vendor by name could not serve it.
"""

from __future__ import annotations

import json
import pathlib
import sys
import types
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

import exmergo_dex_core
from exmergo_dex_core import config as config_mod
from exmergo_dex_core.cli import main
from exmergo_dex_core.config import DexConfig, save_config
from exmergo_dex_core.edits import ApplyResult, SemanticEditValidation, content_hash
from exmergo_dex_core.engine import DexEngine
from exmergo_dex_core.transform.native_semantic import semantic_native
from exmergo_dex_core.transform.plans import EditKind, PlanEdit

VENDOR = "testvendor"
DOCUMENT = "semantics/layer.test.yaml"
_MODULE = "dex_test_native_semantic_vendor"


@dataclass(frozen=True)
class _File:
    content: str
    sha256: str


@dataclass(frozen=True)
class _View:
    root: str
    files: dict[str, _File]


class _Layer:
    """A file-backed layer with its own validator, which records what it saw."""

    def __init__(self, root: Path, calls: list[tuple[str, Any]]) -> None:
        self.root = root
        self.calls = calls

    def semantic_catalog(self) -> Any:
        raise AssertionError("authoring never reads the catalog")

    def semantic_edit_view(self) -> _View:
        files = {}
        target = self.root / DOCUMENT
        if target.is_file():
            text = target.read_text(encoding="utf-8")
            files[DOCUMENT] = _File(content=text, sha256=content_hash(text))
        return _View(root=str(self.root), files=files)

    def semantic_editing_surface(self) -> list[str]:
        return [DOCUMENT]

    def write_semantic_edits(self, edits: Any, *, confirmed: bool = False) -> Any:
        for edit in edits:
            target = self.root / edit.path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(edit.new_content, encoding="utf-8")
        return ApplyResult(written=[edit.path for edit in edits])

    def validate_semantic_edits(
        self, edits: Any, mode: str, *, cache: Any = None
    ) -> SemanticEditValidation:
        self.calls.append(("validate", mode))
        if any("invalid" in edit.new_content for edit in edits):
            raise ValueError("the test vendor refuses this document")
        return SemanticEditValidation(
            defined=["orders"],
            notes=["validated by the test vendor"],
            warnings=["a test-vendor warning"],
        )


class _UnvalidatedLayer(_Layer):
    """Every edit-target method but the validator."""

    validate_semantic_edits = None  # type: ignore[assignment]


@pytest.fixture
def vendor(monkeypatch, tmp_path: Path):
    """Register the test vendor in every table a vendor is read from."""

    calls: list[tuple[str, Any]] = []
    layer_type: list[type] = [_Layer]

    def build(context: Any) -> Any:
        calls.append(("build", None))
        return layer_type[0](Path(context.repo_root), calls)

    module = types.ModuleType(_MODULE)
    module.build = build  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, _MODULE, module)
    monkeypatch.setitem(config_mod.SEMANTIC_DEPLOYMENTS, VENDOR, ("local",))
    monkeypatch.setitem(
        config_mod.SEMANTIC_SOURCE_FACTORIES, VENDOR, f"{_MODULE}:build"
    )
    monkeypatch.setattr(
        config_mod,
        "SEMANTIC_DOCUMENT_AUTHORING",
        (*config_mod.SEMANTIC_DOCUMENT_AUTHORING, VENDOR),
    )
    save_config(
        DexConfig(
            connector="duckdb",
            duckdb={"path": "unused.duckdb"},
            semantic={"vendor": VENDOR},
        ),
        tmp_path,
    )
    return types.SimpleNamespace(root=tmp_path, calls=calls, layer_type=layer_type)


def _edit(content: str = "orders: {}\n") -> PlanEdit:
    return PlanEdit(path=DOCUMENT, kind=EditKind.SEMANTIC_DOCUMENT, new_content=content)


def _run(root: Path, capsys, *args: str) -> dict:
    code = main(["--repo-root", str(root), *args])
    payload = json.loads(capsys.readouterr().out)
    payload["_exit"] = code
    return payload


def test_a_vendor_known_only_to_the_tables_authors_with_its_own_validator(
    vendor, capsys
):
    with DexEngine.from_repo(str(vendor.root)) as engine:
        result = engine.semantic_native(VENDOR, "plan", "add orders", [_edit()])

    assert ("validate", "plan") in vendor.calls
    assert result.defined == ["orders"]
    assert result.updated == []
    assert result.notes == ["validated by the test vendor"]
    assert "a test-vendor warning" in result.warnings
    assert not (vendor.root / DOCUMENT).exists()

    applied = _run(vendor.root, capsys, "transform", "apply", result.plan_id)
    assert applied["_exit"] == 0, applied
    assert (vendor.root / DOCUMENT).read_text("utf-8") == "orders: {}\n"


def test_the_layer_refusing_its_documents_stores_no_plan(vendor):
    with DexEngine.from_repo(str(vendor.root)) as engine:
        with pytest.raises(ValueError, match="the test vendor refuses"):
            semantic_native(engine, VENDOR, "bad", [_edit("invalid\n")], mode="plan")
        assert engine.require_full_store("listing").list_plans() == []


def test_a_layer_without_a_validator_cannot_be_authored(vendor):
    vendor.layer_type[0] = _UnvalidatedLayer

    with (
        DexEngine.from_repo(str(vendor.root)) as engine,
        pytest.raises(ValueError) as refused,
    ):
        semantic_native(engine, VENDOR, "add", [_edit()], mode="plan")

    message = str(refused.value)
    assert f"'{VENDOR}' semantic layer does not support native" in message
    assert ", ".join(config_mod.SEMANTIC_DOCUMENT_AUTHORING) in message


def test_a_mismatched_subcommand_is_refused_before_any_file_is_read(vendor, capsys):
    # A payload path that does not exist: reading it would be a different error.
    refused = _run(
        vendor.root,
        capsys,
        "semantic",
        "ossie",
        "plan",
        "--edits-file",
        str(vendor.root / "never-read.json"),
    )

    assert refused["status"] == "error"
    message = " ".join(refused["errors"])
    assert f"`semantic.vendor` is '{VENDOR}'" in message
    assert f"use `semantic {VENDOR}`" in message
    assert "not found" not in message
    # The source was never built, so no semantic document was read either.
    assert vendor.calls == []


def test_a_vendor_that_cannot_author_is_named_with_those_that_can(
    tmp_path: Path, capsys
):
    save_config(
        DexConfig(connector="duckdb", duckdb={"path": "unused.duckdb"}), tmp_path
    )

    refused = _run(
        tmp_path,
        capsys,
        "semantic",
        "ossie",
        "plan",
        "--edits-file",
        str(tmp_path / "never-read.json"),
    )

    message = " ".join(refused["errors"])
    assert "the configured 'dbt' semantic layer does not support native" in message
    assert ", ".join(config_mod.SEMANTIC_DOCUMENT_AUTHORING) in message
    assert "not found" not in message


def test_the_route_and_its_dispatch_name_no_vendor():
    root = pathlib.Path(exmergo_dex_core.__file__).parent
    route = (root / "transform" / "native_semantic.py").read_text(encoding="utf-8")
    cli = (root / "cli.py").read_text(encoding="utf-8")
    dispatch = cli[cli.index("def dispatch(") :]
    dispatch = dispatch[: dispatch.index("\ndef ", 1)]

    for name in config_mod.SEMANTIC_SOURCE_FACTORIES:
        assert name not in route.lower(), f"native_semantic.py names '{name}'"
        assert name not in dispatch.lower(), f"cli.dispatch names '{name}'"
