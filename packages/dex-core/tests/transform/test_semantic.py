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
    # The label edit is a real change and stays `updated`. The semantic model
    # rides along in the same whole-file payload untouched, which is the noise
    # `unchanged` exists to separate out.
    assert envelope["data"]["updated"] == ["customer_count"]
    assert envelope["data"]["unchanged"] == ["customers"]


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


# --- the unchanged class (#109) ----------------------------------------------


def _land_baseline(tmp_path: Path, capsys, content: str = _VALID_SEMANTIC_YAML) -> None:
    payload = _payload_file(tmp_path, content)
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


def _plan_with(tmp_path: Path, capsys, content: str, name: str) -> dict:
    payload = _payload_file(tmp_path, content, name=name)
    rc, envelope = _run(
        [
            "--repo-root",
            str(tmp_path),
            "semantic",
            "plan",
            "restate",
            "--edits-file",
            str(payload),
        ],
        capsys,
    )
    assert rc == 0, envelope
    return envelope


def test_byte_identical_restate_is_unchanged_not_updated(
    dbt_project_dir: Path, tmp_path: Path, capsys
):
    """The issue as filed: a whole-file payload that changes nothing.

    Restating a file verbatim is what the whole-file edit unit forces on anyone
    extending a shared semantic YAML, and reporting every definition in it as
    `updated` sends a reviewer diff-hunting for a change that is not there.
    """

    _land_baseline(tmp_path, capsys)
    envelope = _plan_with(tmp_path, capsys, _VALID_SEMANTIC_YAML, "restate.json")

    assert envelope["data"]["defined"] == []
    assert envelope["data"]["updated"] == []
    assert envelope["data"]["unchanged"] == ["customer_count", "customers"]
    assert any("changes nothing" in w for w in envelope["warnings"])
    assert all(d["additions"] == 0 and d["deletions"] == 0 for d in envelope["diffs"])


def test_only_the_definition_that_moved_reads_as_updated(
    dbt_project_dir: Path, tmp_path: Path, capsys
):
    _land_baseline(tmp_path, capsys)
    edited = _VALID_SEMANTIC_YAML.replace("Customer count", "Count of customers")
    envelope = _plan_with(tmp_path, capsys, edited, "one-change.json")

    assert envelope["data"]["updated"] == ["customer_count"]
    assert envelope["data"]["unchanged"] == ["customers"]
    assert not any("changes nothing" in w for w in envelope["warnings"])


def test_reordering_a_list_is_a_change(dbt_project_dir: Path, tmp_path: Path, capsys):
    """Key order is formatting; list order is content.

    A parsed mapping compares equal whatever order its keys were written in, so
    reformatting is not reported as a change. A reordered sequence is a
    different document and produces a real diff, so it has to read as one.
    """

    _land_baseline(tmp_path, capsys)
    swapped = _VALID_SEMANTIC_YAML.replace(
        "      - name: email_domain\n"
        "        type: categorical\n"
        "      - name: signup_date\n"
        "        type: time\n"
        "        expr: cast('2020-01-01' as date)\n"
        "        type_params:\n"
        "          time_granularity: day\n",
        "      - name: signup_date\n"
        "        type: time\n"
        "        expr: cast('2020-01-01' as date)\n"
        "        type_params:\n"
        "          time_granularity: day\n"
        "      - name: email_domain\n"
        "        type: categorical\n",
    )
    envelope = _plan_with(tmp_path, capsys, swapped, "swapped.json")
    assert envelope["data"]["updated"] == ["customers"]
    assert envelope["data"]["unchanged"] == ["customer_count"]


def test_identical_content_in_another_file_is_a_move_not_a_no_op(
    dbt_project_dir: Path, tmp_path: Path, capsys
):
    """Same bytes, different file. The project changes, so `updated` is honest."""

    _land_baseline(tmp_path, capsys)
    elsewhere = tmp_path / "moved.json"
    elsewhere.write_text(
        json.dumps(
            {
                "edits": [
                    {
                        "path": "models/semantic/other.yml",
                        "content": _VALID_SEMANTIC_YAML,
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
            "plan",
            "relocate",
            "--edits-file",
            str(elsewhere),
            "--no-parse",
        ],
        capsys,
    )
    assert rc == 0, envelope
    assert envelope["data"]["unchanged"] == []
    assert envelope["data"]["updated"] == ["customer_count", "customers"]


# --- the definition edit unit (#109) -----------------------------------------


# The valid baseline, plus the two things a round-trip through safe_dump would
# destroy: a banner between sections, and a note indented inside one
# definition's body.
_COMMENTED_YAML = _VALID_SEMANTIC_YAML.replace(
    "metrics:\n  - name: customer_count\n    label: Customer count\n"
    "    type: simple\n    type_params:\n      measure: customer_count\n",
    "metrics:\n"
    "  # ---- volume -----------------------------------------------------\n"
    "  - name: customer_count\n"
    "    label: Customer count\n"
    "    type: simple\n"
    "    type_params:\n"
    "      measure: customer_count\n"
    "      # counts rows, not distinct ids\n"
    "\n"
    "  # ---- ratios -----------------------------------------------------\n"
    "  - name: doubled\n"
    "    label: Doubled\n"
    "    type: derived\n"
    "    type_params:\n"
    "      expr: customer_count * 2\n"
    "      metrics:\n"
    "        - name: customer_count\n",
)
assert "# ---- ratios" in _COMMENTED_YAML, "fixture must patch the metrics block"


def _definitions_file(tmp_path: Path, definitions: list[dict], name: str) -> Path:
    payload = tmp_path / name
    payload.write_text(json.dumps({"definitions": definitions}), encoding="utf-8")
    return payload


def _plan_definitions(
    tmp_path: Path, capsys, definitions: list[dict], name: str, mode: str = "plan"
) -> tuple[int, dict]:
    payload = _definitions_file(tmp_path, definitions, name)
    return _run(
        [
            "--repo-root",
            str(tmp_path),
            "semantic",
            mode,
            "one definition",
            "--definitions-file",
            str(payload),
            "--no-parse",
        ],
        capsys,
    )


def test_definition_edit_touches_only_its_own_definition(
    dbt_project_dir: Path, tmp_path: Path, capsys
):
    """The point of the unit: name one metric, diff one metric.

    The same change through the whole-file unit restates every definition in the
    file, so the diff is the same size but the classification is not, and every
    untouched definition is retyped by hand on the way in.
    """

    _land_baseline(tmp_path, capsys, _COMMENTED_YAML)
    rc, envelope = _plan_definitions(
        tmp_path,
        capsys,
        [
            {
                "kind": "metric",
                "content": (
                    "name: customer_count\n"
                    "label: Count of customers\n"
                    "type: simple\n"
                    "type_params:\n"
                    "  measure: customer_count\n"
                ),
            }
        ],
        "one.json",
    )
    assert rc == 0, envelope
    assert envelope["data"]["updated"] == ["customer_count"]
    assert envelope["data"]["unchanged"] == []
    # `doubled` was never mentioned, so it is in no class at all.
    assert envelope["data"]["defined"] == []

    diff = envelope["diffs"][0]["unified"]
    changed = [
        line
        for line in diff.splitlines()
        if line[:1] in {"+", "-"} and not line.startswith(("+++", "---"))
    ]
    assert "-    label: Customer count" in changed
    assert "+    label: Count of customers" in changed
    # The note nested in this definition's body goes with it; the section banner
    # and the neighbouring metric do not move at all.
    assert changed == [
        "-    label: Customer count",
        "+    label: Count of customers",
        "-      # counts rows, not distinct ids",
    ]


def test_definition_edit_keeps_the_rest_of_the_file_byte_for_byte(
    dbt_project_dir: Path, tmp_path: Path, capsys
):
    _land_baseline(tmp_path, capsys, _COMMENTED_YAML)
    rc, envelope = _plan_definitions(
        tmp_path,
        capsys,
        [
            {
                "kind": "metric",
                "content": (
                    "name: tripled\n"
                    "label: Tripled\n"
                    "type: derived\n"
                    "type_params:\n"
                    "  expr: customer_count * 3\n"
                    "  metrics:\n"
                    "    - name: customer_count\n"
                ),
                "path": "models/semantic/customers.yml",
            }
        ],
        "add.json",
    )
    assert rc == 0, envelope
    assert envelope["data"]["defined"] == ["tripled"]

    diff = envelope["diffs"][0]
    assert diff["deletions"] == 0
    assert "+  - name: tripled" in diff["unified"]


def test_definition_edit_finds_the_file_that_already_declares_the_name(
    dbt_project_dir: Path, tmp_path: Path, capsys
):
    """Omitting the path is the ergonomic half: the caller should not have to
    know, or restate, where a definition currently lives."""

    _land_baseline(tmp_path, capsys, _COMMENTED_YAML)
    rc, envelope = _plan_definitions(
        tmp_path,
        capsys,
        [
            {
                "kind": "metric",
                "content": (
                    "name: doubled\n"
                    "label: Twice over\n"
                    "type: derived\n"
                    "type_params:\n"
                    "  expr: customer_count * 2\n"
                    "  metrics:\n"
                    "    - name: customer_count\n"
                ),
            }
        ],
        "nopath.json",
    )
    assert rc == 0, envelope
    assert envelope["data"]["paths"] == ["models/semantic/customers.yml"]
    assert envelope["data"]["updated"] == ["doubled"]


def test_definition_edit_drops_a_note_about_a_line_it_removed(
    dbt_project_dir: Path, tmp_path: Path, capsys
):
    """A comment indented inside the body is part of that definition.

    Leaving it behind would strand a note about a field that no longer exists,
    at an indentation that no longer means anything.
    """

    _land_baseline(tmp_path, capsys, _COMMENTED_YAML)
    rc, envelope = _plan_definitions(
        tmp_path,
        capsys,
        [
            {
                "kind": "metric",
                "content": (
                    "name: customer_count\n"
                    "label: Customer count\n"
                    "type: simple\n"
                    "type_params:\n"
                    "  measure: customer_count\n"
                ),
            }
        ],
        "note.json",
    )
    assert rc == 0, envelope
    assert "-      # counts rows, not distinct ids" in envelope["diffs"][0]["unified"]


def test_definition_edit_refuses_to_duplicate_a_name_into_a_second_file(
    dbt_project_dir: Path, tmp_path: Path, capsys
):
    _land_baseline(tmp_path, capsys, _COMMENTED_YAML)
    rc, envelope = _plan_definitions(
        tmp_path,
        capsys,
        [
            {
                "kind": "metric",
                "path": "models/semantic/other.yml",
                "content": "name: doubled\ntype: simple\ntype_params:\n  measure: x\n",
            }
        ],
        "move.json",
    )
    assert rc == 1
    assert envelope["status"] == "error"
    assert "--edits-file" in envelope["errors"][0]


def test_definition_edit_refuses_a_file_it_cannot_span(
    dbt_project_dir: Path, tmp_path: Path, capsys
):
    """Fail closed and name the way out. A wrong splice is worse than a refusal
    that costs the caller a whole-file edit."""

    _land_baseline(tmp_path, capsys)
    (dbt_project_dir / "models" / "semantic" / "flow.yml").write_text(
        "version: 2\n\nmetrics: [{name: flow_metric, type: simple, "
        "type_params: {measure: customer_count}}]\n",
        encoding="utf-8",
    )
    rc, envelope = _plan_definitions(
        tmp_path,
        capsys,
        [
            {
                "kind": "metric",
                "content": (
                    "name: flow_metric\n"
                    "label: Renamed\n"
                    "type: simple\n"
                    "type_params:\n"
                    "  measure: customer_count\n"
                ),
            }
        ],
        "flow.json",
    )
    assert rc == 1
    assert "flow" in envelope["errors"][0]
    assert "--edits-file" in envelope["errors"][0]


def test_definition_payload_reads_the_name_from_the_content(
    dbt_project_dir: Path, tmp_path: Path, capsys
):
    rc, envelope = _plan_definitions(
        tmp_path,
        capsys,
        [
            {
                "kind": "metric",
                "path": "models/semantic/x.yml",
                "content": "type: simple\n",
            }
        ],
        "nameless.json",
    )
    assert rc == 1
    assert "name" in envelope["errors"][0]


def test_the_two_payload_units_are_mutually_exclusive(
    dbt_project_dir: Path, tmp_path: Path, capsys
):
    edits = _payload_file(tmp_path, _VALID_SEMANTIC_YAML)
    definitions = _definitions_file(
        tmp_path,
        [
            {
                "kind": "metric",
                "path": "models/semantic/x.yml",
                "content": "name: n\ntype: simple\ntype_params:\n  measure: m\n",
            }
        ],
        "both.json",
    )
    rc, envelope = _run(
        [
            "--repo-root",
            str(tmp_path),
            "semantic",
            "plan",
            "both",
            "--edits-file",
            str(edits),
            "--definitions-file",
            str(definitions),
        ],
        capsys,
    )
    assert rc == 1
    assert "not both" in envelope["errors"][0]


def test_definition_mode_guards_still_apply(
    dbt_project_dir: Path, tmp_path: Path, capsys
):
    """The unit changes how a change is expressed, not what the modes mean."""

    _land_baseline(tmp_path, capsys, _COMMENTED_YAML)
    rc, envelope = _plan_definitions(
        tmp_path,
        capsys,
        [
            {
                "kind": "metric",
                "content": (
                    "name: customer_count\n"
                    "label: Clash\n"
                    "type: simple\n"
                    "type_params:\n"
                    "  measure: customer_count\n"
                ),
            }
        ],
        "clash.json",
        mode="define",
    )
    assert rc == 1
    assert "already defined" in envelope["errors"][0]
