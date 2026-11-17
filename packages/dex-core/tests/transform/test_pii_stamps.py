"""PII stamps on the models `transform plan` authors (#1100).

A model that passes a profiled PII column through carries that flag as dbt
metadata however dex authored it: scaffold and reconcile always stamped, and a
model written through `transform plan` now does too. Every test goes through the
CLI, applies the plan, and reads the YAML back independently with
``yaml.safe_load`` and :func:`entry_meta`, so the assertion is about what dbt and
any other consumer would read, not about dex's own bookkeeping.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from exmergo_dex_core.cache import (
    ColumnProfile,
    Dataset,
    DexCache,
    PIICategory,
    PIIFlag,
)
from exmergo_dex_core.cli import main
from exmergo_dex_core.guards.pii_meta import entry_meta
from exmergo_dex_core.storage import FilesystemStore

#: Values seeded into nothing: the cache holds no value, and these strings must
#: never appear in any envelope either way.
PROTECTED = ("someone@example.com", "call me at home")

STG_REPORTS = (
    "select 1 as report_id, 'someone@example.com' as email,\n"
    "    'call me at home' as comments, 'web' as channel\n"
)


def _run(argv: list[str], capsys) -> tuple[int, dict]:
    rc = main(argv)
    out = capsys.readouterr().out
    assert out.count("\n") == 1, "exactly one line on stdout"
    return rc, json.loads(out)


def _flag(category: str, confidence: float) -> PIIFlag:
    return PIIFlag(category=PIICategory(category), confidence=confidence)


@pytest.fixture
def project(dbt_project_dir: Path, tmp_path: Path) -> Path:
    """`stg_reports` built and profiled: `email` and `comments` flagged."""

    (dbt_project_dir / "models" / "staging" / "stg_reports.sql").write_text(
        STG_REPORTS, encoding="utf-8"
    )
    FilesystemStore(tmp_path).save_cache(
        DexCache(
            connector="duckdb",
            datasets=[
                Dataset(
                    identifier="dev.main.stg_reports",
                    columns=[
                        ColumnProfile(name="report_id", data_type="INTEGER"),
                        ColumnProfile(
                            name="email",
                            data_type="VARCHAR",
                            pii=_flag("email", 0.9),
                        ),
                        ColumnProfile(
                            name="comments",
                            data_type="VARCHAR",
                            pii=_flag("free_text", 0.5),
                        ),
                        ColumnProfile(name="channel", data_type="VARCHAR"),
                    ],
                )
            ],
        )
    )
    return dbt_project_dir


def _plan(tmp_path: Path, capsys, body: dict) -> dict:
    payload = tmp_path / "payload.json"
    payload.write_text(json.dumps(body), encoding="utf-8")
    rc, envelope = _run(
        [
            "--repo-root",
            str(tmp_path),
            "transform",
            "plan",
            "a new mart",
            "--edits-file",
            str(payload),
        ],
        capsys,
    )
    assert rc == 0, envelope
    rendered = json.dumps(envelope)
    for value in PROTECTED:
        assert value not in rendered, "no protected value reaches the envelope"
    return envelope


def _apply(tmp_path: Path, capsys, envelope: dict) -> None:
    rc, applied = _run(
        [
            "--repo-root",
            str(tmp_path),
            "transform",
            "apply",
            envelope["data"]["plan_id"],
        ],
        capsys,
    )
    assert rc == 0, applied


def _sql(sql: str, path: str = "models/marts/rpt_reports.sql") -> dict:
    return {"path": path, "kind": "model_sql", "content": sql}


def _read_entry(project: Path, path: str, model: str = "rpt_reports") -> dict:
    parsed = yaml.safe_load((project / path).read_text(encoding="utf-8"))
    return next(e for e in parsed["models"] if e["name"] == model)


def _column(entry: dict, name: str) -> dict:
    return next(c for c in entry.get("columns") or [] if c["name"] == name)


def _status(envelope: dict, column: str, model: str = "rpt_reports") -> dict:
    return envelope["data"]["pii_metadata"]["models"][model]["columns"][column]


# --- the stamp rule --------------------------------------------------------------


def test_a_pass_through_and_an_alias_are_stamped_with_no_declaration(
    project: Path, tmp_path: Path, capsys
):
    """No declaration at all: the plan still writes a minimal entry holding the
    stamps, beside the model's SQL."""

    envelope = _plan(
        tmp_path,
        capsys,
        {
            "edits": [
                _sql(
                    "select report_id, comments, email as contact, channel\n"
                    "from {{ ref('stg_reports') }}\n"
                )
            ]
        },
    )
    assert _status(envelope, "comments")["status"] == "stamped"
    assert _status(envelope, "comments")["evidence"] == "profile"
    assert _status(envelope, "contact") == {
        "status": "stamped",
        "from": "stg_reports.email",
        "evidence": "profile",
        "lineage": "pass_through",
        "category": "email",
    }
    model = envelope["data"]["pii_metadata"]["models"]["rpt_reports"]
    assert model["created"] is True and model["model_stamp"] == "stamped"
    assert "models/marts/rpt_reports.yml" in envelope["data"]["paths"]
    assert "channel" not in model["columns"], "an unflagged column is not listed"

    _apply(tmp_path, capsys, envelope)
    entry = _read_entry(project, "models/marts/rpt_reports.yml")
    assert entry_meta(entry) == {"contains_pii": True}
    assert entry_meta(_column(entry, "comments")) == {
        "contains_pii": True,
        "pii_category": "free_text",
    }
    assert entry_meta(_column(entry, "contact"))["pii_category"] == "email"
    assert [c["name"] for c in entry["columns"]] == ["comments", "contact"]


@pytest.mark.parametrize(
    ("expression", "lineage"),
    [
        ("cast(email as varchar)", "preserved"),
        ("lower(email)", "preserved"),
        ("trim(email)", "preserved"),
        ("coalesce(email, 'unknown')", "preserved"),
    ],
)
def test_a_value_preserving_function_is_stamped(
    project: Path, tmp_path: Path, capsys, expression: str, lineage: str
):
    envelope = _plan(
        tmp_path,
        capsys,
        {
            "edits": [
                _sql(
                    f"select report_id, {expression} as contact\n"
                    "from {{ ref('stg_reports') }}\n"
                )
            ]
        },
    )
    assert _status(envelope, "contact")["status"] == "stamped"
    assert _status(envelope, "contact")["lineage"] == lineage


def test_min_and_max_carry_a_value_and_are_stamped(
    project: Path, tmp_path: Path, capsys
):
    envelope = _plan(
        tmp_path,
        capsys,
        {
            "edits": [
                _sql(
                    "select channel, min(email) as first_contact\n"
                    "from {{ ref('stg_reports') }} group by channel\n"
                )
            ]
        },
    )
    assert _status(envelope, "first_contact")["status"] == "stamped"


def test_measuring_aggregates_and_redaction_are_reported_and_not_stamped(
    project: Path, tmp_path: Path, capsys
):
    envelope = _plan(
        tmp_path,
        capsys,
        {
            "edits": [
                _sql(
                    "select channel,\n"
                    "    count(email) as with_email,\n"
                    "    count(distinct email) as contacts,\n"
                    "    avg(length(comments)) as comment_length,\n"
                    "    cast(null as varchar) as comments\n"
                    "from {{ ref('stg_reports') }} group by channel\n"
                )
            ]
        },
    )
    for column in ("with_email", "contacts", "comment_length"):
        assert _status(envelope, column)["status"] == "measured", column
    assert _status(envelope, "comments") == {
        "status": "redacted",
        "from": "stg_reports.comments",
        "category": "free_text",
    }
    model = envelope["data"]["pii_metadata"]["models"]["rpt_reports"]
    assert "model_stamp" not in model, "nothing stamped, so nothing written"
    assert not any(p.endswith(".yml") for p in envelope["data"]["paths"])


def test_a_hash_is_unresolved_warned_and_not_stamped(
    project: Path, tmp_path: Path, capsys
):
    envelope = _plan(
        tmp_path,
        capsys,
        {
            "edits": [
                _sql(
                    "select report_id, md5(email) as email_hash\n"
                    "from {{ ref('stg_reports') }}\n"
                )
            ]
        },
    )
    assert _status(envelope, "email_hash")["status"] == "unresolved"
    warning = next(w for w in envelope["warnings"] if "email_hash" in w)
    assert "pii_overrides" in warning and "contains_pii" in warning
    assert not any(p.endswith(".yml") for p in envelope["data"]["paths"])


def test_a_set_operation_is_unchecked_rather_than_guessed(
    project: Path, tmp_path: Path, capsys
):
    envelope = _plan(
        tmp_path,
        capsys,
        {
            "edits": [
                _sql(
                    "select email from {{ ref('stg_reports') }}\n"
                    "union all\n"
                    "select email from {{ ref('stg_reports') }}\n"
                )
            ]
        },
    )
    model = envelope["data"]["pii_metadata"]["models"]["rpt_reports"]
    assert model["columns"]["*"]["status"] == "unchecked"
    assert model["columns"]["*"]["reason_code"] == "unreadable_sql"


# --- precedence --------------------------------------------------------------------


def test_a_reviewed_override_on_the_new_column_clears_its_stamp(
    project: Path, tmp_path: Path, capsys
):
    (tmp_path / ".dex").mkdir(exist_ok=True)
    (tmp_path / ".dex" / "config.yml").write_text(
        "connector: duckdb\npii_overrides:\n  - column: rpt_reports.comments\n",
        encoding="utf-8",
    )
    envelope = _plan(
        tmp_path,
        capsys,
        {"edits": [_sql("select report_id, comments from {{ ref('stg_reports') }}\n")]},
    )
    assert _status(envelope, "comments")["status"] == "overridden"
    assert not any(p.endswith(".yml") for p in envelope["data"]["paths"])


def test_an_override_upstream_masks_the_flag_so_nothing_is_stamped(
    project: Path, tmp_path: Path, capsys
):
    (tmp_path / ".dex").mkdir(exist_ok=True)
    (tmp_path / ".dex" / "config.yml").write_text(
        "connector: duckdb\n"
        "pii_overrides:\n"
        "  - column: dev.main.stg_reports.comments\n",
        encoding="utf-8",
    )
    envelope = _plan(
        tmp_path,
        capsys,
        {"edits": [_sql("select report_id, comments from {{ ref('stg_reports') }}\n")]},
    )
    assert envelope["data"].get("pii_metadata") is None


def test_a_hand_written_mark_is_kept_and_a_false_one_is_a_disagreement(
    project: Path, tmp_path: Path, capsys
):
    schema = (
        "version: 2\n"
        "models:\n"
        "  - name: rpt_reports\n"
        "    # reviewed by the data owner\n"
        "    config:\n"
        "      meta:\n"
        "        owner: analytics\n"
        "    columns:\n"
        "      - name: comments\n"
        "        config:\n"
        "          meta:\n"
        "            contains_pii: false  # a review cleared it\n"
        "      - name: email\n"
        "        config:\n"
        "          meta:\n"
        "            contains_pii: true\n"
        "            pii_category: email\n"
    )
    envelope = _plan(
        tmp_path,
        capsys,
        {
            "edits": [
                _sql(
                    "select report_id, comments, email, email as contact\n"
                    "from {{ ref('stg_reports') }}\n"
                ),
                {
                    "path": "models/marts/_marts.yml",
                    "kind": "schema_yml",
                    "content": schema,
                },
            ]
        },
    )
    assert _status(envelope, "comments")["status"] == "disagreement"
    assert _status(envelope, "email")["status"] == "kept"
    assert _status(envelope, "contact")["status"] == "stamped"
    assert any("marked as not PII" in w for w in envelope["warnings"])
    # One entry, the authored one: never a second `rpt_reports.yml` beside it.
    assert "models/marts/rpt_reports.yml" not in envelope["data"]["paths"]

    _apply(tmp_path, capsys, envelope)
    text = (project / "models/marts/_marts.yml").read_text(encoding="utf-8")
    assert "# reviewed by the data owner" in text
    assert "contains_pii: false  # a review cleared it" in text
    entry = _read_entry(project, "models/marts/_marts.yml")
    assert entry_meta(entry) == {"owner": "analytics", "contains_pii": True}
    assert entry_meta(_column(entry, "comments")) == {"contains_pii": False}
    assert entry_meta(_column(entry, "contact")) == {
        "contains_pii": True,
        "pii_category": "email",
    }


def test_the_stamp_lands_beside_the_declaration_in_one_entry(
    project: Path, tmp_path: Path, capsys
):
    envelope = _plan(
        tmp_path,
        capsys,
        {
            "edits": [
                _sql("select report_id, comments from {{ ref('stg_reports') }}\n")
            ],
            "declarations": [
                {
                    "model": "rpt_reports",
                    "description": "One row per report.",
                    "grain": ["report_id"],
                    "columns": {"comments": {"role": "attribute"}},
                }
            ],
        },
    )
    _apply(tmp_path, capsys, envelope)
    entry = _read_entry(project, "models/marts/rpt_reports.yml")
    comments = _column(entry, "comments")
    assert entry_meta(comments)["dex"] == {"role": "attribute"}
    assert entry_meta(comments)["contains_pii"] is True
    assert entry_meta(entry)["dex"]["grain"] == ["report_id"]
    assert entry_meta(entry)["contains_pii"] is True


def test_a_model_entry_with_a_top_level_meta_is_reported_not_written(
    project: Path, tmp_path: Path, capsys
):
    schema = "version: 2\nmodels:\n  - name: rpt_reports\n    meta:\n      owner: x\n"
    envelope = _plan(
        tmp_path,
        capsys,
        {
            "edits": [
                _sql("select report_id, comments from {{ ref('stg_reports') }}\n"),
                {
                    "path": "models/marts/_marts.yml",
                    "kind": "schema_yml",
                    "content": schema,
                },
            ]
        },
    )
    assert _status(envelope, "comments")["reason_code"] == "top_level_meta"
    assert any("top-level `meta`" in w for w in envelope["warnings"])


# --- scope and evidence ------------------------------------------------------------


def test_an_existing_model_is_reported_and_its_yaml_left_alone(
    project: Path, tmp_path: Path, capsys
):
    """stg_customers exists: its rewrite passes `email` through from a flagged
    input, and dex reports the missing stamp instead of editing its YAML."""

    (project / "models" / "staging" / "stg_customers.sql").write_text(
        "select report_id as id, email from {{ ref('stg_reports') }}\n",
        encoding="utf-8",
    )
    before = (project / "models/staging/schema.yml").read_text(encoding="utf-8")
    envelope = _plan(
        tmp_path,
        capsys,
        {
            "edits": [
                _sql(
                    "select report_id as id, lower(email) as email\n"
                    "from {{ ref('stg_reports') }}\n",
                    path="models/staging/stg_customers.sql",
                )
            ]
        },
    )
    model = envelope["data"]["pii_metadata"]["models"]["stg_customers"]
    assert model["created"] is False
    assert model["columns"]["email"] == {
        "status": "not_written",
        "reason_code": "existing_model",
        "from": "stg_reports.email",
        "evidence": "profile",
        "lineage": "preserved",
        "category": "email",
    }
    assert any("already exists" in w for w in envelope["warnings"])
    assert envelope["data"]["paths"] == ["models/staging/stg_customers.sql"]
    _apply(tmp_path, capsys, envelope)
    assert (project / "models/staging/schema.yml").read_text(encoding="utf-8") == before


def test_an_existing_model_already_stamped_is_kept_not_reported_missing(
    project: Path, tmp_path: Path, capsys
):
    (project / "models" / "staging" / "stg_customers.sql").write_text(
        "select report_id as id, email from {{ ref('stg_reports') }}\n",
        encoding="utf-8",
    )
    (project / "models" / "staging" / "schema.yml").write_text(
        "version: 2\n"
        "models:\n"
        "  - name: stg_customers\n"
        "    columns:\n"
        "      - name: email\n"
        "        config:\n"
        "          meta:\n"
        "            contains_pii: true\n"
        "            pii_category: email\n",
        encoding="utf-8",
    )
    envelope = _plan(
        tmp_path,
        capsys,
        {
            "edits": [
                _sql(
                    "select report_id as id, email, comments\n"
                    "from {{ ref('stg_reports') }}\n",
                    path="models/staging/stg_customers.sql",
                )
            ]
        },
    )
    columns = envelope["data"]["pii_metadata"]["models"]["stg_customers"]["columns"]
    assert columns["email"]["status"] == "kept"
    assert columns["comments"]["status"] == "not_written"
    assert not any("'stg_customers.email'" in w for w in envelope["warnings"])


def test_unprofiled_inputs_are_one_warning_per_model(
    dbt_project_dir: Path, tmp_path: Path, capsys
):
    (dbt_project_dir / "models/staging/stg_contacts.sql").write_text(
        "select 1 as id, 'x' as phone, 'y' as region\n", encoding="utf-8"
    )
    (dbt_project_dir / "models/staging/_contacts.yml").write_text(
        "version: 2\nmodels:\n  - name: stg_contacts\n    columns:\n"
        "      - name: phone\n        config:\n          meta:\n"
        "            contains_pii: true\n",
        encoding="utf-8",
    )
    envelope = _plan(
        tmp_path,
        capsys,
        {
            "edits": [
                _sql(
                    "select id, phone, region from {{ ref('stg_contacts') }}\n",
                    path="models/marts/dim_contacts.sql",
                )
            ]
        },
    )
    columns = envelope["data"]["pii_metadata"]["models"]["dim_contacts"]["columns"]
    assert {c for c, v in columns.items() if v["status"] == "unchecked"} == {
        "id",
        "region",
    }
    (warning,) = [w for w in envelope["warnings"] if "no profile" in w]
    assert warning.startswith("2 column(s) of 'dim_contacts'")
    assert "(stg_contacts)" in warning


def test_an_upstream_stamp_stands_in_for_a_missing_profile(
    dbt_project_dir: Path, tmp_path: Path, capsys
):
    """No profile of `stg_contacts`: its own YAML stamp is the evidence, and a
    reviewer's `false` upstream is respected."""

    (dbt_project_dir / "models/staging/stg_contacts.sql").write_text(
        "select 1 as id, 'x' as phone, 'y' as note\n", encoding="utf-8"
    )
    (dbt_project_dir / "models/staging/_contacts.yml").write_text(
        "version: 2\n"
        "models:\n"
        "  - name: stg_contacts\n"
        "    config:\n"
        "      meta:\n"
        "        contains_pii: true\n"
        "    columns:\n"
        "      - name: phone\n"
        "        config:\n"
        "          meta:\n"
        "            contains_pii: true\n"
        "            pii_category: phone\n"
        "      - name: note\n"
        "        config:\n"
        "          meta:\n"
        "            contains_pii: false\n",
        encoding="utf-8",
    )
    envelope = _plan(
        tmp_path,
        capsys,
        {
            "edits": [
                _sql(
                    "select id, phone, note, id as contact_id\n"
                    "from {{ ref('stg_contacts') }}\n",
                    path="models/marts/dim_contacts.sql",
                )
            ]
        },
    )
    columns = envelope["data"]["pii_metadata"]["models"]["dim_contacts"]["columns"]
    assert columns["phone"]["status"] == "stamped"
    assert columns["phone"]["evidence"] == "upstream_metadata"
    assert columns["note"] == {
        "status": "cleared_upstream",
        "from": "stg_contacts.note",
    }
    assert columns["id"] == {
        "status": "unchecked",
        "reason_code": "unprofiled_input",
        "from": "stg_contacts.id",
    }
    _apply(tmp_path, capsys, envelope)
    entry = _read_entry(
        dbt_project_dir, "models/marts/dim_contacts.yml", "dim_contacts"
    )
    assert entry_meta(_column(entry, "phone")) == {
        "contains_pii": True,
        "pii_category": "phone",
    }
    assert [c["name"] for c in entry["columns"]] == ["phone"]


def test_a_plan_over_clean_inputs_carries_no_pii_metadata(
    project: Path, tmp_path: Path, capsys
):
    envelope = _plan(
        tmp_path,
        capsys,
        {"edits": [_sql("select report_id, channel from {{ ref('stg_reports') }}\n")]},
    )
    assert "pii_metadata" not in envelope["data"]


# --- review follow-ups -------------------------------------------------------------


def test_a_lag_default_carries_its_value(project: Path, tmp_path: Path, capsys):
    """`lag(channel, 1, email)` returns `email` on the first row."""

    envelope = _plan(
        tmp_path,
        capsys,
        {
            "edits": [
                _sql(
                    "select report_id,\n"
                    "    lag(channel, 1, email) over (order by report_id) as prior\n"
                    "from {{ ref('stg_reports') }}\n"
                )
            ]
        },
    )
    assert _status(envelope, "prior")["status"] == "stamped"
    assert _status(envelope, "prior")["from"] == "stg_reports.email"


def test_a_new_mart_reads_the_stamp_of_a_new_staging_model_in_the_same_plan(
    dbt_project_dir: Path, tmp_path: Path, capsys
):
    """`a_report` sorts before `z_stage` but reads it: the producer is stamped
    first, so its stamp is the consumer's evidence."""

    (dbt_project_dir / "models/staging/stg_contacts.sql").write_text(
        "select 1 as id, 'x' as phone\n", encoding="utf-8"
    )
    (dbt_project_dir / "models/staging/_contacts.yml").write_text(
        "version: 2\nmodels:\n  - name: stg_contacts\n    columns:\n"
        "      - name: phone\n        config:\n          meta:\n"
        "            contains_pii: true\n            pii_category: phone\n",
        encoding="utf-8",
    )
    envelope = _plan(
        tmp_path,
        capsys,
        {
            "edits": [
                _sql(
                    "select phone from {{ ref('dex_test', 'z_stage') }}\n",
                    path="models/marts/a_report.sql",
                ),
                _sql(
                    "select id, phone from {{ ref('stg_contacts') }}\n",
                    path="models/staging/z_stage.sql",
                ),
            ]
        },
    )
    models = envelope["data"]["pii_metadata"]["models"]
    assert models["z_stage"]["columns"]["phone"]["status"] == "stamped"
    assert models["a_report"]["columns"]["phone"]["status"] == "stamped"
    assert models["a_report"]["columns"]["phone"]["evidence"] == "upstream_metadata"


def test_the_model_mark_is_written_when_every_column_mark_was_already_there(
    project: Path, tmp_path: Path, capsys
):
    schema = (
        "version: 2\nmodels:\n  - name: rpt_reports\n    columns:\n"
        "      - name: email\n        config:\n          meta:\n"
        "            contains_pii: true\n"
    )
    envelope = _plan(
        tmp_path,
        capsys,
        {
            "edits": [
                _sql("select report_id, email from {{ ref('stg_reports') }}\n"),
                {
                    "path": "models/marts/_marts.yml",
                    "kind": "schema_yml",
                    "content": schema,
                },
            ]
        },
    )
    assert _status(envelope, "email")["status"] == "kept"
    model = envelope["data"]["pii_metadata"]["models"]["rpt_reports"]
    assert model["model_stamp"] == "stamped"
    _apply(tmp_path, capsys, envelope)
    entry = _read_entry(project, "models/marts/_marts.yml")
    assert entry_meta(entry) == {"contains_pii": True}
    assert entry_meta(_column(entry, "email")) == {"contains_pii": True}


def _contacts(project: Path, note_meta: str) -> None:
    (project / "models/staging/stg_contacts.sql").write_text(
        "select 1 as id, 'x' as phone, 'y' as note\n", encoding="utf-8"
    )
    (project / "models/staging/_contacts.yml").write_text(
        "version: 2\nmodels:\n  - name: stg_contacts\n    columns:\n"
        "      - name: phone\n        config:\n          meta:\n"
        "            contains_pii: true\n"
        "      - name: note\n        config:\n          meta:\n"
        f"            contains_pii: {note_meta}\n",
        encoding="utf-8",
    )


def test_a_cleared_input_does_not_hide_an_unchecked_one(
    dbt_project_dir: Path, tmp_path: Path, capsys
):
    _contacts(dbt_project_dir, "false")
    envelope = _plan(
        tmp_path,
        capsys,
        {
            "edits": [
                _sql(
                    "select coalesce(note, cast(id as varchar)) as label\n"
                    "from {{ ref('stg_contacts') }}\n",
                    path="models/marts/dim_contacts.sql",
                )
            ]
        },
    )
    label = envelope["data"]["pii_metadata"]["models"]["dim_contacts"]["columns"][
        "label"
    ]
    assert label["status"] == "unchecked"
    assert label["from"] == "stg_contacts.id"


def test_an_override_on_the_upstream_column_beats_its_stale_stamp(
    dbt_project_dir: Path, tmp_path: Path, capsys
):
    _contacts(dbt_project_dir, "false")
    (tmp_path / ".dex").mkdir(exist_ok=True)
    (tmp_path / ".dex" / "config.yml").write_text(
        "connector: duckdb\npii_overrides:\n  - column: stg_contacts.phone\n",
        encoding="utf-8",
    )
    envelope = _plan(
        tmp_path,
        capsys,
        {
            "edits": [
                _sql(
                    "select phone from {{ ref('stg_contacts') }}\n",
                    path="models/marts/dim_contacts.sql",
                )
            ]
        },
    )
    columns = envelope["data"]["pii_metadata"]["models"]["dim_contacts"]["columns"]
    assert columns["phone"]["status"] == "cleared_upstream"
    assert not any(p.endswith(".yml") for p in envelope["data"]["paths"])


def test_a_flow_style_column_list_is_reported_rather_than_failing_the_plan(
    project: Path, tmp_path: Path, capsys
):
    schema = (
        "version: 2\nmodels:\n  - name: rpt_reports\n"
        "    columns: [{name: email, description: keep}]\n"
    )
    envelope = _plan(
        tmp_path,
        capsys,
        {
            "edits": [
                _sql("select report_id, email from {{ ref('stg_reports') }}\n"),
                {
                    "path": "models/marts/_marts.yml",
                    "kind": "schema_yml",
                    "content": schema,
                },
            ]
        },
    )
    email = _status(envelope, "email")
    assert email["status"] == "not_written"
    assert email["reason_code"] == "unwritable_yaml"
    assert any("could not splice" in w for w in envelope["warnings"])
    assert "models/marts/rpt_reports.sql" in envelope["data"]["paths"]
