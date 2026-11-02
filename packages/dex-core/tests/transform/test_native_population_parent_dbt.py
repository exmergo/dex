"""Native population.parent dbt probe must not require the sql extra (#1045)."""

from __future__ import annotations

import builtins
from types import SimpleNamespace

from exmergo_dex_core.transform import native_population_parent as subject


def test_dbt_parent_resolvable_without_sqlglot_when_model_sql_exists(tmp_path):
    """An [ossie]-only install still accepts a parent that the project defines."""

    models = tmp_path / "models"
    models.mkdir()
    (models / "dim_customers.sql").write_text("select 1 as customer_id\n")
    (tmp_path / "dbt_project.yml").write_text(
        "name: demo\nversion: '1.0'\nprofile: demo\nmodel-paths: [models]\n"
        "seed-paths: [seeds]\nsnapshot-paths: [snapshots]\n"
    )

    engine = SimpleNamespace(repo_root=tmp_path, project_dir=lambda: tmp_path)

    real_import = builtins.__import__

    def block_sqlglot(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "sqlglot" or name.startswith("sqlglot."):
            raise ImportError("no sqlglot")
        if (
            name.endswith("declarations")
            or name == "exmergo_dex_core.transform.declarations"
        ):
            # Belts and braces: declarations itself imports sqlglot via rewrite.
            raise ImportError("no declarations under ossie-only")
        return real_import(name, globals, locals, fromlist, level)

    builtins.__import__ = block_sqlglot
    try:
        assert subject._dbt_parent_resolvable(engine, "dim_customers", ["customer_id"])
        assert not subject._dbt_parent_resolvable(
            engine, "missing_model", ["customer_id"]
        )
    finally:
        builtins.__import__ = real_import
