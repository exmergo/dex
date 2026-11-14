"""A declaration may name an installed package's model as its parent (#1155).

``population.parent`` and a column's ``references`` resolve against the project
first and then against the installed packages, so a mart built on a package's
staging model can declare that model. Read from files alone: no dbt, no
connection.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from exmergo_dex_core.dbt_project import load as load_project
from exmergo_dex_core.references import ReferenceIndex
from exmergo_dex_core.transform import native_population_parent
from exmergo_dex_core.transform.contradictions import warnings as parent_warnings
from exmergo_dex_core.transform.declarations import (
    DeclarationError,
    apply_declarations,
    parse_declarations,
)
from exmergo_dex_core.transform.declare import _resolves
from exmergo_dex_core.transform.drafting import draft
from exmergo_dex_core.transform.package_targets import find_package_target

CONVERSATIONS = (
    "select conversation_id, created_at, state\n"
    "from {{ source('inbox', 'conversation_history') }}\n"
)
PARTS = (
    "select part_id, conversation_id, author_id\n"
    "from {{ source('inbox', 'conversation_part_history') }}\n"
)
FROM_PARTS = (
    "select p.conversation_id, count(*) as part_count\n"
    "from {{ ref('stg_inbox__conversation_parts') }} p\n"
    "group by 1\n"
)
FROM_PARENT = (
    "select c.conversation_id, count(p.part_id) as part_count\n"
    "from {{ ref('stg_inbox__conversations') }} c\n"
    "left join {{ ref('stg_inbox__conversation_parts') }} p\n"
    "  on p.conversation_id = c.conversation_id\n"
    "group by 1\n"
)


def _project(
    root: Path,
    *,
    mart: str = FROM_PARENT,
    conversations: str = CONVERSATIONS,
    parts: str = PARTS,
    project_extra: str = "",
    install: str = "dbt_packages",
    extra: dict[str, str] | None = None,
) -> Path:
    """A project whose mart reads two staging models of package ``tiny_inbox``."""

    files = {
        "dbt_project.yml": (
            "name: proj\nversion: '1.0'\nconfig-version: 2\nprofile: proj\n"
            "model-paths: [models]\nseed-paths: [seeds]\n"
            + (
                f"packages-install-path: {install}\n"
                if install != "dbt_packages"
                else ""
            )
            + project_extra
        ),
        "packages.yml": "packages:\n  - local: ../tiny_inbox\n",
        "models/marts/inbox__threads.sql": mart,
        f"{install}/tiny_inbox/dbt_project.yml": (
            "name: tiny_inbox\nversion: '1.0'\nconfig-version: 2\n"
            "model-paths: [models]\nseed-paths: [seeds]\n"
        ),
        f"{install}/tiny_inbox/models/stg_inbox__conversations.sql": conversations,
        f"{install}/tiny_inbox/models/stg_inbox__conversation_parts.sql": parts,
        f"{install}/tiny_inbox/models/src_inbox.yml": (
            "version: 2\nsources:\n  - name: inbox\n    tables:\n"
            "      - name: conversation_history\n"
            "      - name: conversation_part_history\n"
        ),
        f"{install}/tiny_inbox/seeds/inbox_states.csv": "state,label\nopen,Open\n",
        **(extra or {}),
    }
    for path, content in files.items():
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return root


def _declare(root: Path, declaration: dict) -> tuple[str, list[str]]:
    """Apply one declaration to the project at ``root``: the YAML it writes and
    the warnings."""

    view = load_project(root)
    edits, warnings = apply_declarations(parse_declarations([declaration]), view, [])
    [edit] = edits
    return edit.new_content or "", warnings


def _parent(model: str = "stg_inbox__conversations", **key: str) -> dict:
    """A threads declaration whose population.parent is ``model``."""
    return {
        "model": "inbox__threads",
        "grain": ["conversation_id"],
        "population": {
            "rule": "Every conversation, with or without parts.",
            "parent": {
                "model": model,
                "key": key or {"conversation_id": "conversation_id"},
            },
        },
    }


def _snapshot(root: Path) -> dict[str, str]:
    """Every installed package file, by path, to compare before and after."""
    package = root / "dbt_packages"
    return {
        p.relative_to(root).as_posix(): p.read_text()
        for p in sorted(package.rglob("*"))
        if p.is_file()
    }


# --- authoring: the declaration is accepted ----------------------------------------


def test_a_package_model_is_accepted_as_population_parent(tmp_path):
    """A package model is a parent; only the project's YAML is written."""
    root = _project(tmp_path / "p")
    before = _snapshot(root)

    content, warnings = _declare(root, _parent())

    entry = yaml.safe_load(content)["models"][0]
    assert entry["name"] == "inbox__threads"
    parent = entry["config"]["meta"]["dex"]["population"]["parent"]
    assert parent == {
        "model": "stg_inbox__conversations",
        "key": {"conversation_id": "conversation_id"},
    }
    assert not any("could not be read" in w for w in warnings)
    assert _snapshot(root) == before, "no package file is ever edited"


def test_a_package_model_is_accepted_as_a_column_reference(tmp_path):
    """A package model is a reference, rendered as a relationships test to it."""
    root = _project(tmp_path / "p")

    content, _ = _declare(
        root,
        {
            "model": "inbox__threads",
            "columns": {
                "conversation_id": {
                    "role": "foreign_key",
                    "references": "stg_inbox__conversations.conversation_id",
                }
            },
        },
    )

    column = yaml.safe_load(content)["models"][0]["columns"][0]
    [test] = [t for t in column["data_tests"] if "relationships" in t]
    assert test["relationships"]["arguments"] == {
        "to": "ref('stg_inbox__conversations')",
        "field": "conversation_id",
    }


def test_a_package_seed_is_accepted_and_its_header_checked(tmp_path):
    """A package seed is a reference target, checked against its CSV header."""
    root = _project(tmp_path / "p")
    declaration = {
        "model": "inbox__threads",
        "columns": {"conversation_id": {"references": "inbox_states.state"}},
    }
    _declare(root, declaration)

    declaration["columns"]["conversation_id"]["references"] = "inbox_states.nope"
    with pytest.raises(DeclarationError, match="seed of package tiny_inbox"):
        _declare(root, declaration)


# --- authoring: what is still refused, and how ---------------------------------


def test_a_column_the_package_model_lacks_is_refused_naming_the_package(tmp_path):
    """A parent column the package model lacks is refused, naming it."""
    root = _project(tmp_path / "p")

    with pytest.raises(DeclarationError) as raised:
        _declare(root, _parent(conversation_id="thread_id"))

    message = str(raised.value)
    assert "package tiny_inbox" in message
    assert "dbt_packages/tiny_inbox/models/stg_inbox__conversations.sql" in message
    assert "does not produce a column 'thread_id'" in message


def test_unreadable_package_columns_are_a_warning_not_a_refusal(tmp_path):
    """Macro-listed package columns are reported unchecked, not refused."""
    macro = (
        "select {{ fivetran_utils.fill_staging_columns(source_columns=[], "
        "staging_columns=get_columns()) }}\n"
        "from {{ source('inbox', 'conversation_history') }}\n"
    )
    root = _project(tmp_path / "p", conversations=macro)

    _, warnings = _declare(root, _parent(conversation_id="anything"))

    [warning] = [w for w in warnings if "could not be read" in w]
    assert "package tiny_inbox" in warning
    assert "'anything' was not checked" in warning
    assert "transform build --verify" in warning


def test_the_compiled_manifest_answers_for_a_macro_listed_package_model(tmp_path):
    """The manifest's compiled SQL settles columns the raw SQL cannot."""
    macro = "select {{ dbt_utils.star(source('inbox', 'conversation_history')) }}\n"
    manifest = {
        "nodes": {
            "model.tiny_inbox.stg_inbox__conversations": {
                "resource_type": "model",
                "package_name": "tiny_inbox",
                "name": "stg_inbox__conversations",
                "compiled_code": "select conversation_id, state from raw.c",
            }
        }
    }
    root = _project(
        tmp_path / "p",
        conversations=macro,
        extra={"target/manifest.json": json.dumps(manifest)},
    )

    _, warnings = _declare(root, _parent())
    assert not any("could not be read" in w for w in warnings)
    with pytest.raises(DeclarationError, match="does not produce a column 'created'"):
        _declare(root, _parent(conversation_id="created"))


def test_a_project_override_wins_over_the_package_model(tmp_path):
    """A project model of the same name is the parent, as for ref()."""
    root = _project(
        tmp_path / "p",
        project_extra=(
            "models:\n  tiny_inbox:\n    stg_inbox__conversations:\n"
            "      +enabled: false\n"
        ),
        extra={
            "models/overrides/stg_inbox__conversations.sql": (
                "select conversation_id as thread_id from "
                "{{ source('inbox', 'conversation_history') }}\n"
            )
        },
    )

    with pytest.raises(DeclarationError) as raised:
        _declare(root, _parent())
    assert "models/overrides/stg_inbox__conversations.sql" in str(raised.value)
    _declare(root, _parent(conversation_id="thread_id"))


def test_a_disabled_package_model_with_no_override_is_refused(tmp_path):
    """A package model the project disables, with nothing replacing it, is refused."""
    root = _project(
        tmp_path / "p",
        project_extra=(
            "models:\n  tiny_inbox:\n    stg_inbox__conversations:\n"
            "      +enabled: false\n"
        ),
    )

    with pytest.raises(DeclarationError, match="is disabled in this project"):
        _declare(root, _parent())


def test_a_custom_packages_install_path_is_followed(tmp_path):
    """``packages-install-path`` decides where packages are read from."""
    root = _project(tmp_path / "p", install="vendor_packages")

    _declare(root, _parent())
    found = find_package_target(
        root,
        "stg_inbox__conversations",
        project_yml=(root / "dbt_project.yml").read_text(),
    )
    assert found is not None
    assert found.path.startswith("vendor_packages/tiny_inbox/")


def test_package_target_reads_schema_from_package_or_project(tmp_path):
    """#1199: find_package_target reads the custom schema for package models."""

    pkg_dbt = (
        "name: tiny_inbox\nversion: '1.0'\nconfig-version: 2\n"
        "model-paths: [models]\nmodels:\n  tiny_inbox:\n    +schema: inbox\n"
    )
    root = _project(
        tmp_path / "p",
        extra={"dbt_packages/tiny_inbox/dbt_project.yml": pkg_dbt},
    )
    found = find_package_target(
        root,
        "stg_inbox__conversations",
        project_yml=(root / "dbt_project.yml").read_text(),
    )
    assert found is not None
    assert found.schema == "inbox"

    override_yml = (
        root / "dbt_project.yml"
    ).read_text() + "models:\n  tiny_inbox:\n    +schema: custom_inbox\n"
    overridden = find_package_target(
        root,
        "stg_inbox__conversations",
        project_yml=override_yml,
    )
    assert overridden is not None
    assert overridden.schema == "custom_inbox"


def test_sql_schema_does_not_match_other_config_keys_ending_in_schema():
    """#1199: _sql_schema requires a word boundary before schema."""

    from exmergo_dex_core.transform.package_targets import _sql_schema

    assert _sql_schema("{{ config(some_schema='ignored', schema='real') }}") == "real"
    assert _sql_schema("{{ config(some_schema='ignored') }}") is None


def test_a_source_is_still_refused_and_says_so(tmp_path):
    """A source table is not a parent."""
    root = _project(tmp_path / "p")

    with pytest.raises(DeclarationError) as raised:
        _declare(root, _parent(model="conversation_history"))
    message = str(raised.value)
    assert "neither the project nor an installed package" in message


def test_a_project_source_named_as_a_parent_says_a_source_is_not_one(tmp_path):
    """The refusal names the source and the fix."""
    root = _project(
        tmp_path / "p",
        extra={
            "models/staging/src_raw.yml": (
                "version: 2\nsources:\n  - name: raw\n    tables:\n"
                "      - name: conversations\n"
            )
        },
    )

    with pytest.raises(DeclarationError) as raised:
        _declare(root, _parent(model="conversations"))
    assert "is a table of source 'raw'" in str(raised.value)
    assert "name the staging model" in str(raised.value)


def test_a_name_nothing_defines_is_still_refused(tmp_path):
    """A name nothing defines is refused."""
    root = _project(tmp_path / "p")

    with pytest.raises(DeclarationError, match="neither the project nor an installed"):
        _declare(root, _parent(model="stg_inbox__nothing"))


# --- the other authoring gates --------------------------------------------------


def test_transform_declare_proposes_a_package_reference(tmp_path):
    """``transform declare`` may propose a reference to a package model."""
    root = _project(tmp_path / "p")
    view = load_project(root)
    files = {path: source.content for path, source in view.files.items()}

    assert _resolves("stg_inbox__conversations.conversation_id", files, view)
    assert not _resolves("stg_inbox__conversations.thread_id", files, view)
    assert not _resolves("stg_inbox__nothing.conversation_id", files, view)


def test_native_layers_resolve_a_package_parent(tmp_path):
    """The native semantic routes resolve a package parent."""
    root = _project(tmp_path / "p")
    engine = SimpleNamespace(repo_root=root, project_dir=lambda: root)

    resolvable = native_population_parent._dbt_parent_resolvable
    assert resolvable(engine, "stg_inbox__conversations", ["conversation_id"])
    assert not resolvable(engine, "stg_inbox__conversations", ["thread_id"])


# --- the plan-time driver check (#1029) -----------------------------------------------


def _parent_rows(root: Path, mart: str) -> list[dict]:
    """The population_parent contradictions the draft reports for ``mart``."""
    view = load_project(root)
    files = {path: source.content for path, source in view.files.items()}
    files["models/marts/inbox__threads.sql"] = mart
    files["models/marts/inbox__threads.yml"] = yaml.safe_dump(
        {
            "version": 2,
            "models": [
                {
                    "name": "inbox__threads",
                    "config": {
                        "meta": {
                            "dex": {
                                "population": {
                                    "parent": {
                                        "model": "stg_inbox__conversations",
                                        "key": {"conversation_id": "conversation_id"},
                                    }
                                }
                            }
                        }
                    },
                }
            ],
        }
    )
    for path in ("models/marts/inbox__threads.sql", "models/marts/inbox__threads.yml"):
        (root / path).write_text(files[path])
    drafts, _ = draft(
        ["inbox__threads"],
        files,
        ["models"],
        None,
        dialect="duckdb",
        prior={},
        index=lambda: ReferenceIndex(load_project(root)),
    )
    return [
        c
        for c in drafts["inbox__threads"].get("contradictions") or []
        if c["check"] == "population_parent"
    ]


def test_a_mart_driven_from_a_package_sibling_contradicts_its_package_parent(tmp_path):
    """The ADE case: driven from the parts, it contradicts the parent."""
    root = _project(tmp_path / "p")

    [found] = _parent_rows(root, FROM_PARTS)

    assert found["status"] == "contradicted"
    assert found["parent"] == "stg_inbox__conversations"
    assert found["authored"] == "stg_inbox__conversation_parts"
    [warning] = parent_warnings("inbox__threads", [found])
    assert "stg_inbox__conversation_parts" in warning


@pytest.mark.parametrize(
    "mart",
    [
        pytest.param(FROM_PARENT, id="the-package-parent"),
        pytest.param(
            "select conversation_id from {{ ref('tiny_inbox', "
            "'stg_inbox__conversations') }}\n",
            id="two-argument-ref",
        ),
        pytest.param(
            "select conversation_id\n"
            "from {{ source('inbox', 'conversation_history') }}\n",
            id="the-parents-source",
        ),
    ],
)
def test_a_mart_driven_from_its_package_parent_is_silent(tmp_path, mart):
    """Driven from the parent, or from what the parent is built from, is silent."""
    root = _project(tmp_path / "p")
    assert _parent_rows(root, mart) == []


def test_a_package_staging_model_written_with_column_macros_is_still_judged(tmp_path):
    """Column-only macros in a package's lineage hide no relation."""
    fivetran = (
        "with base as (select * from {{ ref('stg_inbox__conversations_tmp') }}),\n"
        "fields as (select {{ fivetran_utils.fill_staging_columns("
        "source_columns=adapter.get_columns_in_relation("
        "ref('stg_inbox__conversations_tmp')), "
        "staging_columns=get_conversation_columns()) }}\n"
        "{{ fivetran_utils.source_relation() }} from base)\n"
        "select conversation_id, {{ dbt.type_timestamp() }} as created_at "
        "from fields\n"
    )
    root = _project(
        tmp_path / "p",
        conversations=fivetran,
        extra={
            "dbt_packages/tiny_inbox/models/tmp/stg_inbox__conversations_tmp.sql": (
                "select * from {{ source('inbox', 'conversation_history') }}\n"
            )
        },
    )

    [found] = _parent_rows(root, FROM_PARTS)
    assert found["status"] == "contradicted"


def test_a_package_macro_that_may_read_a_relation_is_unchecked(tmp_path):
    """A macro that may read relations leaves the check unchecked."""
    union = (
        "{{ fivetran_utils.union_data("
        "table_identifier='conversation_part_history') }}\n"
    )
    root = _project(tmp_path / "p", parts=union)

    [found] = _parent_rows(root, FROM_PARTS)
    assert found["status"] == "unchecked"
    assert "union_data" in found["reason"]


# --- the verify sweep reads the namespaces dbt derives from the dev one --------------


@pytest.mark.parametrize(
    ("dev", "relations", "expected"),
    [
        pytest.param(
            "dbt_dev",
            [
                "`acme-dw`.`dbt_dev`.`threads`",
                "`acme-dw`.`dbt_dev_inbox`.`stg_inbox__conversations`",
                "`acme-dw`.`dbt_dev_inbox`.`stg_inbox__conversation_parts`",
            ],
            ["dbt_dev_inbox"],
            id="bigquery-dataset",
        ),
        pytest.param(
            "ANALYTICS.DBT_DEV",
            ['"ANALYTICS"."DBT_DEV_STG_INTERCOM"."STG_INTERCOM__CONVERSATIONS"'],
            ["ANALYTICS.DBT_DEV_STG_INTERCOM"],
            id="snowflake-database-schema",
        ),
        pytest.param(
            "dbt_dev",
            ['"warehouse"."dbt_dev_inbox"."x"', '"warehouse"."analytics"."y"'],
            ["dbt_dev_inbox"],
            id="postgres-schema-only-the-derived-one",
        ),
        pytest.param(
            "dbt_dev",
            [
                "`acme-dw`.`staging_dev`.`stg_orders`",
                "`acme-dw`.`intermediate_dev`.`int_orders`",
                "`acme-dw`.`marts_dev`.`fct_orders`",
            ],
            ["intermediate_dev", "marts_dev", "staging_dev"],
            id="bigquery-layered-schemas",
        ),
        pytest.param(
            "ANALYTICS.DEV",
            [
                '"ANALYTICS"."STAGING_DEV"."STG_ORDERS"',
                '"ANALYTICS"."INTERMEDIATE_DEV"."INT_ORDERS"',
                '"ANALYTICS"."MARTS_DEV"."FCT_ORDERS"',
            ],
            [
                "ANALYTICS.INTERMEDIATE_DEV",
                "ANALYTICS.MARTS_DEV",
                "ANALYTICS.STAGING_DEV",
            ],
            id="snowflake-layered-schemas",
        ),
        pytest.param(
            "dev",
            ['"staging_dev"."stg_orders"', '"marts_dev"."fct_orders"'],
            ["marts_dev", "staging_dev"],
            id="postgres-layered-schemas",
        ),
        pytest.param(
            "dev.dbt_dev",
            ["prod.dbt_dev_inbox.x", "dev.dbt_devious.y", "dev.dbt_dev.z"],
            [],
            id="another-container-a-mere-prefix-and-the-dev-one-are-not",
        ),
        pytest.param(None, ["a.dbt_dev_inbox.x"], [], id="no-dev-namespace"),
    ],
)
def test_derived_dev_scopes(dev, relations, expected):
    """namespaces dbt derives from the dev one, per connector spelling."""
    from exmergo_dex_core.connect import derived_dev_scopes

    assert derived_dev_scopes(dev, relations) == expected


def test_derived_dev_scopes_with_explicit_target_name():
    """a custom dbt_target derives layer schemas ending with that target name."""
    from exmergo_dex_core.connect import derived_dev_scopes

    relations = [
        "`acme-dw`.`staging_local`.`stg_orders`",
        "`acme-dw`.`marts_local`.`fct_orders`",
    ]
    assert derived_dev_scopes("dbt_local", relations, target_name="local") == [
        "marts_local",
        "staging_local",
    ]


def test_derived_dev_scopes_excludes_dev_suffix_with_explicit_target_name():
    """a schema ending in _dev is excluded when an explicit target name is set."""
    from exmergo_dex_core.connect import derived_dev_scopes

    relations = [
        "`acme-dw`.`finance_dev`.`fct_revenue`",
        "`acme-dw`.`staging_local`.`stg_orders`",
    ]
    assert derived_dev_scopes("dbt_local", relations, target_name="local") == [
        "staging_local",
    ]


def test_widen_dev_reads_reads_the_dev_namespace_and_what_derives_from_it():
    """Both are read; nothing changes when nothing derives."""
    from exmergo_dex_core.config import BigQueryTarget, DexConfig
    from exmergo_dex_core.connect import widen_dev_reads

    seen: list = []
    adapter = SimpleNamespace(name="bigquery", read_dev_target=seen.append)
    config = DexConfig(
        connector="bigquery",
        bigquery=BigQueryTarget(project="p", datasets=["raw"], dev_dataset="dbt_dev"),
    )

    added = widen_dev_reads(adapter, config, ["p.dbt_dev_inbox.x", "p.raw.y"])

    assert added == ["dbt_dev_inbox"]
    assert seen == [["dbt_dev", "dbt_dev_inbox"]]
    assert widen_dev_reads(adapter, config, ["p.dbt_dev.x"]) == []
    assert len(seen) == 1, "nothing derived, so the adapter's dev read is untouched"

    added_layered = widen_dev_reads(adapter, config, ["p.staging_dev.x"])
    assert added_layered == ["staging_dev"]
    assert seen == [["dbt_dev", "dbt_dev_inbox"], ["dbt_dev", "staging_dev"]]


def test_with_dev_target_folds_every_existing_entry_once():
    """Each existing dev namespace is folded into the scope once."""
    from exmergo_dex_core.adapters.base import dev_target_entries, with_dev_target

    entries = dev_target_entries(["dbt_dev", " dbt_dev_inbox ", "dbt_dev"], str.upper)
    assert entries == ("DBT_DEV", "DBT_DEV_INBOX")
    assert with_dev_target(["RAW"], entries, lambda e: e != "DBT_DEV_GONE") == [
        "DBT_DEV",
        "DBT_DEV_INBOX",
        "RAW",
    ]
    assert with_dev_target(["RAW"], None, lambda e: True) == ["RAW"]
    assert dev_target_entries(None) is None


# --- review follow-ups ------------------------------------------------------------


def test_a_derived_dataset_in_another_bigquery_project_is_not_read():
    """A bare dev dataset is read in the adapter's project, so a derived dataset
    a `+database` puts in another project is left out rather than read there."""

    from exmergo_dex_core.connect import derived_dev_scopes

    relations = [
        "`acme-dw`.`dbt_dev_inbox`.`stg_inbox__conversations`",
        "`other-proj`.`dbt_dev_crm`.`stg_crm__accounts`",
    ]
    assert derived_dev_scopes("dbt_dev", relations, container="acme-dw") == [
        "dbt_dev_inbox"
    ]


def test_maintain_verify_reads_a_package_seeds_derived_namespace(tmp_path):
    """The manifest's seeds and snapshots count, beside the models."""

    from exmergo_dex_core.maintain.commands import _dev_relations

    target = tmp_path / "target"
    target.mkdir()
    (target / "manifest.json").write_text(
        json.dumps(
            {
                "nodes": {
                    "seed.tiny_inbox.inbox_states": {
                        "resource_type": "seed",
                        "relation_name": '"dev"."main_inbox"."inbox_states"',
                    },
                    "test.proj.t": {"resource_type": "test", "relation_name": None},
                }
            }
        )
    )
    relations = _dev_relations({"threads": "dev.main.threads"}, tmp_path)
    assert relations == ["dev.main.threads", '"dev"."main_inbox"."inbox_states"']
    assert _dev_relations({"threads": "dev.main.threads"}, None) == ["dev.main.threads"]


def test_drafting_reads_package_models_as_the_plan_leaves_the_project(tmp_path):
    """A package model the plan disables is no reference target."""
    from exmergo_dex_core.transform.commands import _package_lookup

    root = _project(tmp_path / "p")
    view = load_project(root)
    disabled = (root / "dbt_project.yml").read_text() + (
        "models:\n  tiny_inbox:\n    stg_inbox__conversations:\n      +enabled: false\n"
    )

    assert _package_lookup(view)("stg_inbox__conversations") == "tiny_inbox"
    assert _package_lookup(view, disabled)("stg_inbox__conversations") is None


@pytest.mark.parametrize(
    ("parts", "status"),
    [
        pytest.param(PARTS, "contradicted", id="plain-sql"),
        pytest.param(
            "{{ fivetran_utils.union_data("
            "table_identifier='conversation_part_history') }}\n",
            "unchecked",
            id="union-data",
        ),
    ],
)
def test_an_install_path_outside_the_project_is_read_for_lineage(
    tmp_path, parts, status
):
    """A package installed outside the project is indexed and its SQL read, so
    the verdict comes from its lineage rather than from a missing package."""

    root = _project(tmp_path / "p", install="../vendor_packages", parts=parts)
    index = ReferenceIndex(load_project(root))
    for node in ("stg_inbox__conversations", "stg_inbox__conversation_parts"):
        assert index.package_node_kind(node) == ("model", "tiny_inbox")

    [found] = _parent_rows(root, FROM_PARTS)
    assert found["status"] == status
    if status == "unchecked":
        assert "union_data" in found["reason"]


LINKED_PARTS = (
    "select p.part_id, p.conversation_id\n"
    "from {{ source('inbox', 'conversation_part_history') }} p\n"
    "join {{ ref('stg_inbox__conversations') }} c using (conversation_id)\n"
)


@pytest.mark.parametrize(
    "override",
    [
        pytest.param(
            {
                "models/overrides/stg_inbox__conversation_parts.py": (
                    "def model(dbt, session):\n"
                    "    return dbt.source('inbox', 'conversation_part_history')\n"
                )
            },
            id="python-model",
        ),
        pytest.param(
            {
                "models/overrides/stg_inbox__conversation_parts_v1.sql": PARTS,
                "models/overrides/_parts.yml": (
                    "version: 2\nmodels:\n  - name: stg_inbox__conversation_parts\n"
                    "    latest_version: 1\n    versions:\n      - v: 1\n"
                ),
            },
            id="versioned-model",
        ),
    ],
)
def test_a_project_override_dex_cannot_read_still_shadows_the_package(
    tmp_path, override
):
    """A Python or versioned project model of the package model's name shadows
    it too: the package file's lineage to the parent is not the project's, so
    the check cannot hold on it and reads ``unchecked``."""

    root = _project(tmp_path / "p", parts=LINKED_PARTS, extra=override)

    index = ReferenceIndex(load_project(root))
    assert index.package_node_kind("stg_inbox__conversation_parts") is None
    assert "stg_inbox__conversations" not in index.ancestors_of(
        "stg_inbox__conversation_parts"
    )
    [found] = _parent_rows(root, FROM_PARTS)
    assert found["status"] == "unchecked"
    assert "stg_inbox__conversation_parts" in found["reason"]


def test_a_shadowed_package_models_lineage_is_not_the_overrides(tmp_path):
    """A project override replaces the package model: the disabled package
    file's ``ref()`` to the parent links nothing, so a mart driven from the
    override, which reads only a source, still contradicts the parent."""

    root = _project(
        tmp_path / "p",
        parts=LINKED_PARTS,
        project_extra=(
            "models:\n  tiny_inbox:\n    stg_inbox__conversation_parts:\n"
            "      +enabled: false\n"
        ),
        extra={"models/overrides/stg_inbox__conversation_parts.sql": PARTS},
    )

    index = ReferenceIndex(load_project(root))
    assert "stg_inbox__conversations" not in index.ancestors_of(
        "stg_inbox__conversation_parts"
    )
    [found] = _parent_rows(root, FROM_PARTS)
    assert found["status"] == "contradicted"


def test_a_cost_guard_refusal_while_widening_dev_reads_propagates(monkeypatch):
    """A cost-guard refusal is re-raised, not turned into a warning."""
    from exmergo_dex_core import connect
    from exmergo_dex_core.guards.cost_guard import CostGuardError
    from exmergo_dex_core.transform.verify import BuildVerification, _widen_dev_reads

    def refuse(*_args, **_kwargs):
        """Refuse as the cost guard would."""
        raise CostGuardError("over the ceiling")

    monkeypatch.setattr(connect, "widen_dev_reads", refuse)
    engine = SimpleNamespace(config=None)
    result = BuildVerification(ran=True)

    with pytest.raises(CostGuardError):
        _widen_dev_reads(engine, object(), lambda: [], result)
    assert result.warnings == []
