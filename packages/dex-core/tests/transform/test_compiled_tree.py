"""The compiled tree the toggle and retirement checks share (#1156, #1170)."""

from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace

from exmergo_dex_core.transform.build import _failed_node
from exmergo_dex_core.transform.compiled_tree import (
    Branch,
    macro_callers,
    read_manifest,
)
from exmergo_dex_core.transform.toggle import _not_compiled


def _completed(*messages: str) -> subprocess.CompletedProcess:
    """A failed dbt run whose JSON log carries ``messages``."""

    stdout = "\n".join(
        json.dumps({"info": {"level": "error", "msg": msg}}) for msg in messages
    )
    return subprocess.CompletedProcess(["dbt"], 1, stdout=stdout, stderr="")


def test_a_failing_node_is_named_by_its_file() -> None:
    """dbt's header names the node's file; the reason is the line after it."""

    found = _failed_node(
        _completed(
            "Encountered an error:\nRuntime Error\n  Compilation Error in model bad "
            "(models/bad.sql)\n    'undefined_macro' is undefined."
        )
    )
    assert found == ("models/bad.sql", "'undefined_macro' is undefined.")


def test_a_failure_pinned_on_no_node_is_not_isolated() -> None:
    assert _failed_node(_completed("Could not find profile named 'shop'")) is None


MANIFEST = {
    "metadata": {"project_name": "shop"},
    "nodes": {
        "model.shop.rpt": {
            "name": "rpt",
            "package_name": "shop",
            "original_file_path": "models/rpt.sql",
            "compiled_code": "select 1 as a",
            "relation_name": "dev.rpt",
            "depends_on": {"macros": ["macro.shop.outer"], "nodes": []},
        },
        "model.shop.plain": {
            "name": "plain",
            "package_name": "shop",
            "original_file_path": "models/plain.sql",
            "compiled_code": "select 1 as a",
            "depends_on": {"macros": ["macro.dbt.ref"], "nodes": []},
        },
        "model.shop.bad": {
            "name": "bad",
            "package_name": "shop",
            "original_file_path": "models/bad.sql",
        },
    },
    "macros": {
        "macro.shop.outer": {
            "original_file_path": "macros/outer.sql",
            "depends_on": {"macros": ["macro.shop.inner", "macro.dbt.ref"]},
        },
        "macro.shop.inner": {
            "original_file_path": "macros/inner.sql",
            "depends_on": {"macros": ["macro.shop.outer"]},
        },
    },
    "disabled": {
        "model.shop.legacy": [
            {
                "name": "legacy",
                "original_file_path": "models/legacy.sql",
                "depends_on": {"macros": ["macro.shop.inner"]},
            }
        ],
        "model.shop.quiet": [
            {"name": "quiet", "original_file_path": "models/quiet.sql"}
        ],
    },
}


def _branch() -> Branch:
    branch = Branch()
    read_manifest(branch, MANIFEST, "shop", {"models/bad.sql": "no such macro"})
    return branch


def test_an_isolated_model_is_failed_not_empty() -> None:
    branch = _branch()
    assert set(branch.models) == {"rpt", "plain"}
    assert branch.failed == {"bad": "no such macro"}
    assert branch.disabled == {
        "legacy": "models/legacy.sql",
        "quiet": "models/quiet.sql",
    }


def test_a_macro_is_attributed_to_its_callers_through_other_macros() -> None:
    """``inner`` is reached only through ``outer``; the cycle terminates."""

    assert macro_callers(_branch(), "shop") == {
        "macros/inner.sql": ["rpt"],
        "macros/outer.sql": ["rpt"],
    }


def test_only_a_disabled_model_that_reads_the_flag_is_not_compiled() -> None:
    """``legacy`` calls a macro that reads it; ``quiet`` reads nothing."""

    branch = _branch()
    branches = {"on": branch, "off": branch, "before": branch}
    view = SimpleNamespace(macro_paths=["macros"])
    assert _not_compiled(branches, ["macros/inner.sql:3"], view) == [
        {"model": "legacy", "path": "models/legacy.sql"}
    ]
    assert _not_compiled(branches, ["models/quiet.sql:2"], view) == [
        {"model": "quiet", "path": "models/quiet.sql"}
    ]


def test_a_disabled_model_reached_only_through_its_own_macros_is_not_compiled():
    """No enabled model calls ``legacy_fees()``, and the read is one macro deeper
    (CodeRabbit on #1192): the disabled model is still owed."""

    manifest = {
        "metadata": {"project_name": "shop"},
        "nodes": {},
        "macros": {
            "macro.shop.legacy_fees": {
                "original_file_path": "macros/legacy_fees.sql",
                "depends_on": {"macros": ["macro.shop.fee_flag"]},
            },
            "macro.shop.fee_flag": {"original_file_path": "macros/fee_flag.sql"},
        },
        "disabled": {
            "model.shop.legacy": [
                {
                    "name": "legacy",
                    "original_file_path": "models/legacy.sql",
                    "depends_on": {"macros": ["macro.shop.legacy_fees"]},
                }
            ]
        },
    }
    branch = Branch()
    read_manifest(branch, manifest, "shop")
    branches = {"on": branch, "off": branch, "before": branch}
    view = SimpleNamespace(macro_paths=["macros"])
    assert macro_callers(branch, "shop") == {}
    assert _not_compiled(branches, ["macros/fee_flag.sql:1"], view) == [
        {"model": "legacy", "path": "models/legacy.sql"}
    ]
