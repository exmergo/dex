"""Semantic authoring: define/update as plans, MetricFlow validation, emit dbt."""

from __future__ import annotations

import json
from pathlib import Path

from exmergo_dex_core.cli import main

_VALID_SEMANTIC_YAML = """\
semantic_models:
  - name: customers
    description: Customer grain over stg_customers.
    model: ref('stg_customers')
    entities:
      - name: customer
        type: primary
        expr: id
    dimensions:
      - name: email_domain
        type: categorical
    measures:
      - name: customer_count
        agg: count
        expr: id

metrics:
  - name: customer_count
    label: Customer count
    type: simple
    type_params:
      measure: customer_count
"""


def _run(argv: list[str], capsys) -> tuple[int, dict]:
    rc = main(argv)
    out = capsys.readouterr().out
    assert out.count("\n") == 1, "exactly one line on stdout"
    return rc, json.loads(out)


def _payload_file(tmp_path: Path, content: str, name: str = "semantic.json") -> Path:
    payload = tmp_path / name
    payload.write_text(
        json.dumps(
            {"edits": [{"path": "models/semantic/customers.yml", "content": content}]}
        ),
        encoding="utf-8",
    )
    return payload


def test_semantic_define_creates_a_plan_not_a_file(
    dbt_project_dir: Path, tmp_path: Path, capsys
):
    payload = _payload_file(tmp_path, _VALID_SEMANTIC_YAML)
    rc, envelope = _run(
        [
            "--repo-root",
            str(tmp_path),
            "semantic",
            "define",
            "customer count metric",
            "--edits-file",
            str(payload),
        ],
        capsys,
    )
    assert rc == 0, envelope
    assert envelope["status"] == "ok"
    assert envelope["diffs"] and envelope["diffs"][0]["op"] == "create"
    assert "emit dbt" in envelope["data"]["next"]
    assert not (dbt_project_dir / "models/semantic/customers.yml").exists()


def test_semantic_define_rejects_invalid_metricflow_yaml(
    dbt_project_dir: Path, tmp_path: Path, capsys
):
    bad = "metrics:\n  - name: revenue\n    type: nope\n    type_params: {}\n"
    payload = _payload_file(tmp_path, bad)
    rc, envelope = _run(
        [
            "--repo-root",
            str(tmp_path),
            "semantic",
            "define",
            "bad",
            "--edits-file",
            str(payload),
        ],
        capsys,
    )
    assert rc == 1
    assert envelope["status"] == "error"


def test_semantic_define_requires_semantic_content(
    dbt_project_dir: Path, tmp_path: Path, capsys
):
    payload = _payload_file(tmp_path, "version: 2\nmodels:\n  - name: stg_customers\n")
    rc, envelope = _run(
        [
            "--repo-root",
            str(tmp_path),
            "semantic",
            "define",
            "x",
            "--edits-file",
            str(payload),
        ],
        capsys,
    )
    assert rc == 1
    assert envelope["status"] == "error"


def test_define_refuses_existing_name_update_requires_it(
    dbt_project_dir: Path, tmp_path: Path, capsys
):
    # Land the semantic model first: define + emit dbt.
    payload = _payload_file(tmp_path, _VALID_SEMANTIC_YAML)
    rc, envelope = _run(
        [
            "--repo-root",
            str(tmp_path),
            "semantic",
            "define",
            "v1",
            "--edits-file",
            str(payload),
        ],
        capsys,
    )
    assert envelope["status"] == "ok"
    rc, envelope = _run(["--repo-root", str(tmp_path), "emit", "dbt"], capsys)
    assert rc == 0, envelope
    assert envelope["status"] == "ok"
    assert envelope["data"]["written"] == ["models/semantic/customers.yml"]
    assert (dbt_project_dir / "models/semantic/customers.yml").is_file()

    # Defining the same names again must point at update instead.
    rc, envelope = _run(
        [
            "--repo-root",
            str(tmp_path),
            "semantic",
            "define",
            "again",
            "--edits-file",
            str(payload),
        ],
        capsys,
    )
    assert rc == 1
    assert envelope["status"] == "error"
    assert "semantic update" in envelope["errors"][0]

    # Update of the now-existing names is accepted.
    updated = _VALID_SEMANTIC_YAML.replace("Customer count", "Count of customers")
    update_payload = _payload_file(tmp_path, updated, name="semantic-v2.json")
    rc, envelope = _run(
        [
            "--repo-root",
            str(tmp_path),
            "semantic",
            "update",
            "v2",
            "--edits-file",
            str(update_payload),
        ],
        capsys,
    )
    assert rc == 0, envelope
    assert envelope["status"] == "ok"


def test_update_refuses_unknown_name(dbt_project_dir: Path, tmp_path: Path, capsys):
    payload = _payload_file(tmp_path, _VALID_SEMANTIC_YAML)
    rc, envelope = _run(
        [
            "--repo-root",
            str(tmp_path),
            "semantic",
            "update",
            "x",
            "--edits-file",
            str(payload),
        ],
        capsys,
    )
    assert rc == 1
    assert envelope["status"] == "error"
    assert "semantic define" in envelope["errors"][0]


def test_semantic_rejects_model_sql_edits(
    dbt_project_dir: Path, tmp_path: Path, capsys
):
    payload = tmp_path / "mixed.json"
    payload.write_text(
        json.dumps(
            {
                "edits": [
                    {
                        "path": "models/staging/stg_x.sql",
                        "kind": "model_sql",
                        "content": "select 1\n",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    rc, envelope = _run(
        [
            "--repo-root",
            str(tmp_path),
            "semantic",
            "define",
            "x",
            "--edits-file",
            str(payload),
        ],
        capsys,
    )
    assert rc == 1
    assert envelope["status"] == "error"


def test_emit_dbt_without_a_semantic_plan_is_a_clean_error(
    dbt_project_dir: Path, tmp_path: Path, capsys
):
    rc, envelope = _run(["--repo-root", str(tmp_path), "emit", "dbt"], capsys)
    assert rc == 1
    assert envelope["status"] == "error"
    assert "semantic define" in envelope["errors"][0]


def test_define_warns_when_project_lacks_a_time_spine(
    dbt_project_dir: Path, tmp_path: Path, capsys
):
    payload = _payload_file(tmp_path, _VALID_SEMANTIC_YAML)
    _rc, envelope = _run(
        [
            "--repo-root",
            str(tmp_path),
            "semantic",
            "define",
            "x",
            "--edits-file",
            str(payload),
        ],
        capsys,
    )
    assert envelope["status"] == "ok"
    assert any("time spine" in w for w in envelope["warnings"])


def test_no_time_spine_warning_when_the_project_has_one(
    dbt_project_dir: Path, tmp_path: Path, capsys
):
    (dbt_project_dir / "models" / "metricflow_time_spine.sql").write_text(
        "select cast(range as date) as date_day\n"
        "from range(date '2020-01-01', date '2030-01-01', interval 1 day)\n",
        encoding="utf-8",
    )
    (dbt_project_dir / "models" / "metricflow_time_spine.yml").write_text(
        "version: 2\n"
        "models:\n"
        "  - name: metricflow_time_spine\n"
        "    time_spine:\n"
        "      standard_granularity_column: date_day\n"
        "    columns:\n"
        "      - name: date_day\n"
        "        granularity: day\n",
        encoding="utf-8",
    )
    payload = _payload_file(tmp_path, _VALID_SEMANTIC_YAML)
    _rc, envelope = _run(
        [
            "--repo-root",
            str(tmp_path),
            "semantic",
            "define",
            "x",
            "--edits-file",
            str(payload),
        ],
        capsys,
    )
    assert envelope["status"] == "ok"
    assert not any("time spine" in w for w in envelope["warnings"])
