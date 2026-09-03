"""Ossie through the command surface: what answers, and what refuses.

The refusals are the interesting half. Ossie specifies interchange metadata and
not a portable query runtime, so a command that would need execution semantics
has to say so rather than invent them, and it has to say so through the generic
capability machinery rather than a vendor branch.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from exmergo_dex_core.cli import main
from exmergo_dex_core.config import DexConfig, save_config
from exmergo_dex_core.engine import DexEngine
from exmergo_dex_core.explore.semantic import SemanticBackendError, resolve_backend
from exmergo_dex_core.explore.semantic.ossie import LocalOssieBackend


@pytest.fixture
def configured(repo: Path) -> Path:
    save_config(
        DexConfig(
            connector="duckdb",
            duckdb={"path": "demo.duckdb"},
            semantic={
                "vendor": "ossie",
                "ossie": {"files": ["commerce.ossie.yaml"]},
            },
        ),
        repo,
    )
    return repo


def run(repo: Path, *argv: str, capsys) -> dict:
    code = main(["--repo-root", str(repo), *argv])
    payload = json.loads(capsys.readouterr().out)
    payload["_exit"] = code
    return payload


# --- resolution ------------------------------------------------------------


def test_the_vendor_resolves_to_the_ossie_backend(configured: Path):
    backend = resolve_backend(DexEngine.from_repo(str(configured)))

    assert isinstance(backend, LocalOssieBackend)
    assert backend.descriptor.vendor == "ossie"
    assert backend.descriptor.deployment == "local"
    assert backend.descriptor.execution == "dex"


def test_the_backend_reads_its_catalog_through_the_injected_format(
    configured: Path,
):
    """The backend never constructs a reader, which is what lets one class serve
    both configuration routes without this class knowing which it is in."""

    backend = resolve_backend(DexEngine.from_repo(str(configured)))

    assert backend._project.name == "ossie"


def test_a_hosted_source_is_refused_for_a_local_only_vendor(configured: Path):
    engine = DexEngine.from_repo(str(configured))
    engine.semantic_source = lambda: ("host", "env", "token")

    with pytest.raises(SemanticBackendError, match="no meaning for native"):
        resolve_backend(engine)


def test_the_api_override_is_refused_by_name(configured: Path):
    """Ossie has no hosted deployment to override to, and there will not be one:
    a hosted Ossie would be some vendor's service speaking its own protocol,
    which is a different vendor rather than a second deployment of this one."""

    with pytest.raises(SemanticBackendError, match="no hosted execution override"):
        resolve_backend(DexEngine.from_repo(str(configured)), api=True)


# --- list ------------------------------------------------------------------


def test_semantic_list_reads_the_native_documents(configured: Path, capsys):
    payload = run(configured, "explore", "semantic", "list", capsys=capsys)

    assert payload["_exit"] == 0
    assert payload["status"] == "ok"
    data = payload["data"]
    assert (data["vendor"], data["deployment"], data["execution"]) == (
        "ossie",
        "local",
        "dex",
    )
    assert [m["name"] for m in data["metrics"]] == ["revenue", "order_count"]
    assert "commerce.orders" in [m["name"] for m in data["semantic_models"]]


def test_the_catalog_declares_what_ossie_structurally_cannot_supply(
    configured: Path, capsys
):
    """An absent field and an undeclared one are indistinguishable to a caller,
    so a consumer reads "Ossie has no measures" as "this layer declares none"
    and stops looking."""

    data = run(configured, "explore", "semantic", "list", capsys=capsys)["data"]

    unavailable = data["unavailable"]
    assert "dimensions" in unavailable["metrics"]
    assert set(unavailable["entities"]) >= {"name", "type", "roles"}
    assert set(unavailable["measures"]) >= {"name", "agg", "expr"}


def test_the_pii_gate_lookup_never_reaches_the_payload(configured: Path, capsys):
    payload = run(configured, "explore", "semantic", "list", capsys=capsys)

    assert "physical_columns" not in json.dumps(payload)


def test_dialect_metadata_survives_into_the_payload(configured: Path, capsys):
    data = run(configured, "explore", "semantic", "list", capsys=capsys)["data"]

    placed = next(d for d in data["dimensions"] if d["name"] == "orders__placed_on")

    assert placed["vendor_params"]["dialects"] == {
        "ANSI_SQL": "placed_at",
        "MDX": "[Order].[Placed At]",
    }


def test_a_dimension_with_nothing_to_carry_omits_vendor_params(
    configured: Path, capsys
):
    """Sparse serialization: an unset optional field is omitted, not null."""

    data = run(configured, "explore", "semantic", "list", capsys=capsys)["data"]
    models = data["semantic_models"]

    assert all("model_ref" not in m for m in models)


# --- refusals --------------------------------------------------------------


def test_for_dimension_refuses_through_the_declared_gap(configured: Path, capsys):
    """Read off the backend's own declaration rather than off a vendor name.

    Without it, the inversion runs over empty groupable lists and reports "the
    layer declares the dimension and no metric reaches it", which is a statement
    about the layer where the truth is that this backend was never told.
    """

    payload = run(
        configured,
        "explore",
        "semantic",
        "list",
        "--for-dimension",
        "orders__order_id",
        capsys=capsys,
    )

    assert payload["status"] == "error"
    assert "cannot say which dimensions" in payload["errors"][0]


def test_a_generic_metric_query_is_refused_with_the_alternative_named(
    configured: Path, capsys
):
    payload = run(configured, "explore", "semantic", "query", "revenue", capsys=capsys)

    assert payload["status"] == "error"
    message = payload["errors"][0]
    assert "not a portable query runtime" in message
    assert "explore query" in message


def test_dimension_values_are_refused_with_the_alternative_named(
    configured: Path, capsys
):
    payload = run(
        configured,
        "explore",
        "semantic",
        "values",
        "orders__order_id",
        capsys=capsys,
    )

    assert payload["status"] == "error"
    assert "profile that relation" in payload["errors"][0]


# --- use-project -----------------------------------------------------------


def test_map_reaches_the_ossie_catalog_without_a_vendor_branch(configured: Path):
    """`explore map --use-project` folds the layer in through the same helper it
    uses for dbt, which now asks the semantic axis rather than the vendor."""

    from exmergo_dex_core.explore.commands import _semantic_catalog

    view = _semantic_catalog(DexEngine.from_repo(str(configured)), True)

    assert view is not None
    assert "commerce.orders" in [m.name for m in view.semantic_models]


def test_an_unreadable_document_degrades_rather_than_failing_a_map(
    configured: Path,
):
    """That helper declines on every condition and never raises: `explore map`
    is a question about a warehouse that happens to have a project."""

    from exmergo_dex_core.explore.commands import _semantic_catalog

    (configured / "commerce.ossie.yaml").write_text("a: [1,\n", encoding="utf-8")

    assert _semantic_catalog(DexEngine.from_repo(str(configured)), True) is None


def test_no_command_carries_a_vendor_branch():
    """The architectural constraint, asserted rather than reviewed.

    An issue that satisfies its own tests while reintroducing one of these is
    implemented wrong, and the next format would add three more.
    """

    import pathlib

    import exmergo_dex_core

    root = pathlib.Path(exmergo_dex_core.__file__).parent
    offenders = [
        f"{path.relative_to(root)}:{number}"
        for path in root.rglob("*.py")
        if path.parts[-2] != "ossie"
        for number, line in enumerate(path.read_text().splitlines(), start=1)
        if '"ossie"' in line
        and ("==" in line or "!=" in line)
        # config.py is where a vendor's own coordinates are declared and
        # cross-checked, which is the one place naming one is the job.
        and path.name != "config.py"
    ]

    assert not offenders, (
        "a vendor name compared at a call site is the shape the project seam "
        f"exists to remove: {offenders}"
    )
