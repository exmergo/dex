"""Semantic authoring: define/update as plans, MetricFlow validation, apply."""

from __future__ import annotations

import json
from pathlib import Path

from exmergo_dex_core.cli import main

_VALID_SEMANTIC_YAML = """\
semantic_models:
  - name: customers
    description: Customer grain over stg_customers.
    model: ref('stg_customers')
    defaults:
      agg_time_dimension: signup_date
    entities:
      - name: customer
        type: primary
        expr: id
    dimensions:
      - name: email_domain
        type: categorical
      - name: signup_date
        type: time
        expr: cast('2020-01-01' as date)
        type_params:
          time_granularity: day
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


_RATIO_METRICS_YAML = """\
semantic_models:
  - name: pit_stops
    description: Pit stop grain.
    model: ref('stg_customers')
    entities:
      - name: stop
        type: primary
        expr: id
    measures:
      - name: clean_stop_seconds
        agg: sum
        expr: id
      - name: total_stops
        agg: count
        expr: id

metrics:
  - name: avg_clean_pit_stop_seconds
    label: Avg clean stop seconds
    type: ratio
    type_params:
      numerator: clean_stop_seconds
      denominator: total_stops
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
    assert "transform apply" in envelope["data"]["next"]
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
    # Land the semantic model first: define + transform apply.
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
    rc, envelope = _run(["--repo-root", str(tmp_path), "transform", "apply"], capsys)
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


def test_ratio_metric_referencing_a_measure_names_the_fix(
    dbt_project_dir: Path, tmp_path: Path, capsys
):
    payload = _payload_file(tmp_path, _RATIO_METRICS_YAML)
    rc, envelope = _run(
        [
            "--repo-root",
            str(tmp_path),
            "semantic",
            "define",
            "pit stop kpi",
            "--edits-file",
            str(payload),
        ],
        capsys,
    )
    assert rc == 1
    assert envelope["status"] == "error"
    assert "create_metric: true" in envelope["errors"][0]
    # Both unresolved inputs are reported in one round-trip.
    assert "numerator 'clean_stop_seconds'" in envelope["errors"][0]
    assert "denominator 'total_stops'" in envelope["errors"][0]


def test_ratio_metric_over_create_metric_measures_passes(
    dbt_project_dir: Path, tmp_path: Path, capsys
):
    fixed = _RATIO_METRICS_YAML.replace(
        "        agg: sum\n", "        agg: sum\n        create_metric: true\n"
    ).replace(
        "        agg: count\n",
        "        agg: count\n        create_metric: true\n",
    )
    payload = _payload_file(tmp_path, fixed)
    rc, envelope = _run(
        [
            "--repo-root",
            str(tmp_path),
            "semantic",
            "define",
            "pit stop kpi",
            "--edits-file",
            str(payload),
        ],
        capsys,
    )
    assert rc == 0, envelope
    assert envelope["status"] == "ok"


def test_simple_metric_with_unknown_measure_is_refused(
    dbt_project_dir: Path, tmp_path: Path, capsys
):
    bad = _VALID_SEMANTIC_YAML.replace(
        "      measure: customer_count", "      measure: nonexistent_measure"
    )
    payload = _payload_file(tmp_path, bad)
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
    assert "unknown measure 'nonexistent_measure'" in envelope["errors"][0]


def test_derived_metric_with_unknown_input_is_refused(
    dbt_project_dir: Path, tmp_path: Path, capsys
):
    derived = (
        _VALID_SEMANTIC_YAML + "  - name: customer_count_doubled\n"
        "    label: Doubled\n"
        "    type: derived\n"
        "    type_params:\n"
        "      expr: missing_metric * 2\n"
        "      metrics:\n"
        "        - name: missing_metric\n"
    )
    payload = _payload_file(tmp_path, derived)
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
    assert "unknown metric 'missing_metric'" in envelope["errors"][0]


def test_references_resolve_across_edits_in_one_payload(
    dbt_project_dir: Path, tmp_path: Path, capsys
):
    measures_only = (
        _RATIO_METRICS_YAML.split("metrics:")[0]
        .replace(
            "        agg: sum\n", "        agg: sum\n        create_metric: true\n"
        )
        .replace(
            "        agg: count\n",
            "        agg: count\n        create_metric: true\n",
        )
    )
    ratio_only = "metrics:" + _RATIO_METRICS_YAML.split("metrics:")[1]
    payload = tmp_path / "two-edits.json"
    payload.write_text(
        json.dumps(
            {
                "edits": [
                    {"path": "models/semantic/pit_stops.yml", "content": measures_only},
                    {"path": "models/semantic/pit_kpis.yml", "content": ratio_only},
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
    assert rc == 0, envelope
    assert envelope["status"] == "ok"


def test_define_clashes_with_an_implicit_create_metric_name(
    dbt_project_dir: Path, tmp_path: Path, capsys
):
    # A hand-written measure with create_metric: true implicitly declares a
    # metric; a define proposing that name must be refused as a collision.
    (dbt_project_dir / "models" / "semantic").mkdir(parents=True, exist_ok=True)
    (dbt_project_dir / "models" / "semantic" / "existing.yml").write_text(
        "semantic_models:\n"
        "  - name: laps\n"
        "    model: ref('stg_customers')\n"
        "    entities:\n"
        "      - name: lap\n"
        "        type: primary\n"
        "        expr: id\n"
        "    measures:\n"
        "      - name: lap_count\n"
        "        agg: count\n"
        "        expr: id\n"
        "        create_metric: true\n",
        encoding="utf-8",
    )
    clash = (
        "metrics:\n"
        "  - name: lap_count\n"
        "    label: Laps\n"
        "    type: simple\n"
        "    type_params:\n"
        "      measure: lap_count\n"
    )
    payload = _payload_file(tmp_path, clash)
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
    assert "already defined" in envelope["errors"][0]


def test_semantic_plan_accepts_mixed_new_and_existing_names(
    dbt_project_dir: Path, tmp_path: Path, capsys
):
    # Land the baseline, then evolve it and add a dependent helper in ONE call:
    # the mixed intent define/update each refuse.
    payload = _payload_file(tmp_path, _VALID_SEMANTIC_YAML)
    _run(
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
    rc, envelope = _run(["--repo-root", str(tmp_path), "transform", "apply"], capsys)
    assert rc == 0, envelope

    mixed = (
        _VALID_SEMANTIC_YAML.replace("Customer count", "Count of customers")
        + "  - name: customer_count_doubled\n"
        "    label: Doubled\n"
        "    type: derived\n"
        "    type_params:\n"
        "      expr: customer_count * 2\n"
        "      metrics:\n"
        "        - name: customer_count\n"
    )
    mixed_payload = _payload_file(tmp_path, mixed, name="mixed.json")
    rc, envelope = _run(
        [
            "--repo-root",
            str(tmp_path),
            "semantic",
            "plan",
            "evolve and extend",
            "--edits-file",
            str(mixed_payload),
        ],
        capsys,
    )
    assert rc == 0, envelope
    assert envelope["status"] == "ok"
    assert envelope["data"]["defined"] == ["customer_count_doubled"]
    assert envelope["data"]["updated"] == ["customer_count", "customers"]


def test_define_reports_classification(dbt_project_dir: Path, tmp_path: Path, capsys):
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
    assert rc == 0, envelope
    assert envelope["data"]["defined"] == ["customer_count", "customers"]
    assert envelope["data"]["updated"] == []


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
