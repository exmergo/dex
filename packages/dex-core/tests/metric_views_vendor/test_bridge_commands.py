"""`semantic databricks|ossie ... --from-ossie|--from-metric-view` (#1260).

The configured layer proposed from one file of the other format, through dex's
port of apache/ossie-converters: validated by the target layer's own rules,
stored as a plan where the output is a configured file, returned as a proposal
with the `dex config set` that adopts it otherwise, and every notice in
`data.bridge`.
"""

# The inputs are YAML documents kept as the converter's tests write them, one
# flow mapping per line, so a line can be longer than the code's limit.
# ruff: noqa: E501

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from exmergo_dex_core.cli import main
from exmergo_dex_core.metric_views.ossie_bridge.common import parse_yaml

from .conftest import FIXTURES

pytest.importorskip("sqlglot")


def run(capsys, root: Path, *argv: str) -> tuple[int, dict]:
    rc = main(["--repo-root", str(root), *argv])
    return rc, json.loads(capsys.readouterr().out)


def configure(root: Path, vendor: str, files: list[str]) -> None:
    (root / ".dex").mkdir(exist_ok=True)
    listed = "".join(f"      - {f}\n" for f in files)
    (root / ".dex" / "config.yml").write_text(
        f"connector: databricks\nsemantic:\n  vendor: {vendor}\n  {vendor}:\n"
        f"    files:\n{listed}",
        encoding="utf-8",
    )


def fixture(root: Path, name: str, to: str) -> str:
    target = root / to
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(FIXTURES / name, target)
    return to


OTHER_OSSIE = """\
version: "0.2.0.dev0"
name: other
datasets:
  - name: t
    source: a.b.t
    fields:
      - name: x
        expression: {sql: x}
"""


def ossie_repo(root: Path, files: list[str] | None = None) -> None:
    (root / "semantic").mkdir(parents=True, exist_ok=True)
    (root / "semantic" / "other.ossie.yaml").write_text(OTHER_OSSIE, encoding="utf-8")
    configure(root, "ossie", files or ["semantic/other.ossie.yaml"])


# --- Ossie to Metric View ---------------------------------------------------------------------


def test_an_ossie_document_becomes_a_planned_view_that_applies_and_reads_back(
    tmp_path, capsys
):
    source = fixture(tmp_path, "converter_fixtureB_ossie.yaml", "models/lineitem.yaml")
    configure(tmp_path, "databricks", ["views/lineitem.yaml"])

    rc, out = run(
        capsys,
        tmp_path,
        "semantic",
        "databricks",
        "define",
        "bridge",
        "--from-ossie",
        source,
    )

    assert rc == 0, out
    data = out["data"]
    assert list(data)[:3] == ["plan_id", "intent", "bridge"]
    (conversion,) = data["bridge"]["conversions"]
    assert conversion["status"] == "included"
    assert conversion["output_path"] == "views/lineitem.yaml"
    assert data["bridge"]["direction"] == "ossie_to_metric_view"
    assert data["bridge"]["converter_commit"] == "82006ba2"
    assert conversion["notices"] == [
        {
            "origin": "converter",
            "text": "[dataset 'orders'] primary_key/unique_keys not stored as columns; "
            "used to set rely.at_most_one_match on a matching many_to_one join where "
            "applicable",
        }
    ]
    assert data["defined"] == ["lineitem"]
    assert any("the Ossie bridge reported 1 notice" in w for w in out["warnings"])

    rc, applied = run(capsys, tmp_path, "transform", "apply", data["plan_id"])
    assert rc == 0, applied
    written = parse_yaml((tmp_path / "views/lineitem.yaml").read_text(encoding="utf-8"))
    assert written == parse_yaml((FIXTURES / "converter_fixtureB.yaml").read_text())

    from exmergo_dex_core.metric_views.project import MetricViewSemanticLayer
    from exmergo_dex_core.semantic_source import SemanticSourceContext

    layer = MetricViewSemanticLayer.from_context(
        SemanticSourceContext(
            repo_root=str(tmp_path),
            connector="databricks",
            options={"files": ["views/lineitem.yaml"]},
        )
    )
    assert layer.loaded().errors == []


def test_an_unconfigured_output_is_proposed_with_its_adopt_command(tmp_path, capsys):
    source = fixture(tmp_path, "converter_fixtureA_ossie.yaml", "models/sales.yaml")
    configure(tmp_path, "databricks", ["views/other.yaml"])
    fixture(tmp_path, "converter_tpcds.yaml", "views/other.yaml")

    rc, out = run(
        capsys, tmp_path, "semantic", "databricks", "plan", "--from-ossie", source
    )

    assert rc == 0, out
    data = out["data"]
    assert data["plan_id"] is None
    (conversion,) = data["bridge"]["conversions"]
    assert conversion["status"] == "proposed"
    assert conversion["output_path"] == "views/sales.yaml"
    assert parse_yaml(conversion["content"]) == parse_yaml(
        (FIXTURES / "converter_fixtureA.yaml").read_text()
    )
    assert (
        "dex config set semantic.databricks.files '[views/other.yaml, views/sales.yaml]'"
        in (data["next"])
    )
    assert "--from-ossie models/sales.yaml" in data["next"]
    assert not (tmp_path / "views/sales.yaml").exists()
    assert not list((tmp_path / ".dex").glob("plans/*.json"))


def test_update_with_an_unconfigured_output_is_refused(tmp_path, capsys):
    source = fixture(tmp_path, "converter_fixtureA_ossie.yaml", "models/sales.yaml")
    fixture(tmp_path, "converter_tpcds.yaml", "views/other.yaml")
    configure(tmp_path, "databricks", ["views/other.yaml"])

    rc, out = run(
        capsys, tmp_path, "semantic", "databricks", "update", "--from-ossie", source
    )

    assert rc != 0
    assert out["status"] == "error"
    assert "nothing to update" in out["errors"][0]
    assert out["data"]["bridge"]["conversions"][0]["status"] == "failed"


def test_define_over_an_existing_view_is_refused_and_update_classifies_it(
    tmp_path, capsys
):
    source = fixture(tmp_path, "converter_fixtureB_ossie.yaml", "models/lineitem.yaml")
    fixture(tmp_path, "converter_tpcds.yaml", "views/lineitem.yaml")
    configure(tmp_path, "databricks", ["views/lineitem.yaml"])

    rc, out = run(
        capsys, tmp_path, "semantic", "databricks", "define", "--from-ossie", source
    )
    assert rc != 0
    assert "the Metric View rules refuse it" in out["errors"][0]
    assert out["data"]["bridge"]["conversions"][0]["status"] == "failed"

    rc, out = run(
        capsys, tmp_path, "semantic", "databricks", "update", "--from-ossie", source
    )
    assert rc == 0, out
    assert out["data"]["updated"] == ["lineitem"]


def test_a_document_the_converter_refuses_names_its_kind_and_reason(tmp_path, capsys):
    (tmp_path / "models").mkdir()
    (tmp_path / "models/bad.yaml").write_text(
        "version: '0.2.0.dev0'\nname: m\ndatasets:\n"
        "- {name: a, source: c.s.a}\n- {name: b, source: c.s.b}\n",
        encoding="utf-8",
    )
    fixture(tmp_path, "converter_tpcds.yaml", "views/other.yaml")
    configure(tmp_path, "databricks", ["views/other.yaml"])

    rc, out = run(
        capsys,
        tmp_path,
        "semantic",
        "databricks",
        "plan",
        "--from-ossie",
        "models/bad.yaml",
    )

    assert rc != 0
    assert out["errors"][0].startswith("the converter refuses it (NOT_REPRESENTABLE):")
    assert "cannot determine the fact table" in out["errors"][0]


def test_fact_names_the_dataset_when_the_graph_has_several_roots(tmp_path, capsys):
    (tmp_path / "models").mkdir()
    (tmp_path / "models/m.yaml").write_text(
        """\
version: "0.2.0.dev0"
name: m
datasets:
- {name: orders, source: c.s.orders, fields: [{name: s, expression: {sql: s}}]}
- {name: returns, source: c.s.returns}
- {name: customer, source: c.s.customer, primary_key: [c_custkey]}
relationships:
- {name: oc, from: orders, to: customer, from_columns: [o_custkey], to_columns: [c_custkey]}
- {name: rc, from: returns, to: customer, from_columns: [re_custkey], to_columns: [c_custkey]}
""",
        encoding="utf-8",
    )
    fixture(tmp_path, "converter_tpcds.yaml", "views/other.yaml")
    configure(tmp_path, "databricks", ["views/other.yaml"])

    rc, out = run(
        capsys,
        tmp_path,
        "semantic",
        "databricks",
        "plan",
        "--from-ossie",
        "models/m.yaml",
    )
    assert rc != 0
    assert "dex names that dataset with --fact" in out["errors"][0]

    (tmp_path / "models/two.yaml").write_text(
        """\
version: "0.2.0.dev0"
name: two
datasets:
- {name: orders, source: c.s.orders}
- {name: customer, source: c.s.customer, primary_key: [c_custkey], fields: [{name: s, expression: {sql: s}}]}
relationships:
- {name: oc, from: orders, to: customer, from_columns: [o_custkey], to_columns: [c_custkey]}
metrics:
- {name: n, expression: {sql: COUNT(*)}}
""",
        encoding="utf-8",
    )
    rc, out = run(
        capsys,
        tmp_path,
        "semantic",
        "databricks",
        "plan",
        "--from-ossie",
        "models/two.yaml",
        "--fact",
        "customer",
    )
    assert rc == 0, out
    content = parse_yaml(out["data"]["bridge"]["conversions"][0]["content"])
    assert content["source"] == "c.s.customer"
    assert content["joins"][0]["cardinality"] == "one_to_many"


# --- Metric View to Ossie ---------------------------------------------------------------------


def test_a_metric_view_becomes_a_planned_ossie_document(tmp_path, capsys):
    pytest.importorskip("jsonschema")
    source = fixture(tmp_path, "converter_fixtureA.yaml", "views/orders.yaml")
    ossie_repo(tmp_path, ["semantic/other.ossie.yaml", "semantic/sales.ossie.yaml"])

    rc, out = run(
        capsys,
        tmp_path,
        "semantic",
        "ossie",
        "define",
        "--from-metric-view",
        source,
        "--name",
        "sales",
        "--dataset-metrics",
    )

    assert rc == 0, out
    data = out["data"]
    (conversion,) = data["bridge"]["conversions"]
    assert conversion["status"] == "included"
    assert conversion["output_path"] == "semantic/sales.ossie.yaml"
    assert data["defined"] == ["sales"]
    assert {n["origin"] for n in conversion["notices"]} == {"dex"}

    rc, applied = run(capsys, tmp_path, "transform", "apply", data["plan_id"])
    assert rc == 0, applied
    from exmergo_dex_core.ossie.loader import load_documents

    loaded = load_documents(
        str(tmp_path), ["semantic/other.ossie.yaml", "semantic/sales.ossie.yaml"]
    )
    assert [d.render() for d in loaded.diagnostics if d.severity == "error"] == []
    sales = next(d.data for d in loaded.documents if d.data["name"] == "sales")
    assert [m["name"] for m in sales["datasets"][0]["metrics"]] == [
        "total_revenue",
        "order_count",
    ]


def test_into_places_the_ossie_document(tmp_path, capsys):
    pytest.importorskip("jsonschema")
    source = fixture(tmp_path, "converter_fixtureB.yaml", "views/lineitem.yaml")
    ossie_repo(tmp_path)

    rc, out = run(
        capsys,
        tmp_path,
        "semantic",
        "ossie",
        "plan",
        "--from-metric-view",
        source,
        "--into",
        "elsewhere/li.ossie.yaml",
    )

    assert rc == 0, out
    conversion = out["data"]["bridge"]["conversions"][0]
    assert conversion["status"] == "proposed"
    assert conversion["output_path"] == "elsewhere/li.ossie.yaml"
    assert "--into elsewhere/li.ossie.yaml" in out["data"]["next"]


# --- refusals -----------------------------------------------------------------------------


def test_the_bridge_flag_of_the_other_vendor_is_not_a_flag(tmp_path, capsys):
    ossie_repo(tmp_path)
    with pytest.raises(SystemExit):
        main(
            [
                "--repo-root",
                str(tmp_path),
                "semantic",
                "ossie",
                "plan",
                "--from-ossie",
                "x",
            ]
        )
    capsys.readouterr()


def test_the_vendor_must_be_the_configured_one(tmp_path, capsys):
    source = fixture(tmp_path, "converter_fixtureB_ossie.yaml", "models/lineitem.yaml")
    ossie_repo(tmp_path)

    rc, out = run(
        capsys, tmp_path, "semantic", "databricks", "plan", "--from-ossie", source
    )

    assert rc != 0
    assert "semantic.vendor" in out["errors"][0]


def test_edits_file_and_a_bridge_flag_are_refused_together(tmp_path, capsys):
    source = fixture(tmp_path, "converter_fixtureB_ossie.yaml", "models/lineitem.yaml")
    configure(tmp_path, "databricks", ["views/lineitem.yaml"])
    (tmp_path / "edits.json").write_text('{"edits": []}', encoding="utf-8")

    rc, out = run(
        capsys,
        tmp_path,
        "semantic",
        "databricks",
        "plan",
        "--from-ossie",
        source,
        "--edits-file",
        str(tmp_path / "edits.json"),
    )

    assert rc != 0
    assert "--edits-file and the bridge flag" in out["errors"][0]


@pytest.mark.parametrize(
    ("source", "message"),
    [
        ("../outside.yaml", "resolves outside the repository"),
        ("missing.yaml", "is not a file"),
        ("views/body.sql", "is a .sql file"),
    ],
)
def test_a_source_must_be_a_readable_file_in_the_repository(
    tmp_path, capsys, source, message
):
    (tmp_path.parent / "outside.yaml").write_text("x: 1\n", encoding="utf-8")
    (tmp_path / "views").mkdir()
    (tmp_path / "views/body.sql").write_text("CREATE VIEW x", encoding="utf-8")
    ossie_repo(tmp_path)

    rc, out = run(
        capsys, tmp_path, "semantic", "ossie", "plan", "--from-metric-view", source
    )

    assert rc != 0
    assert message in out["errors"][0]


def test_into_must_stay_in_the_repository(tmp_path, capsys):
    source = fixture(tmp_path, "converter_fixtureB.yaml", "views/lineitem.yaml")
    ossie_repo(tmp_path)

    rc, out = run(
        capsys,
        tmp_path,
        "semantic",
        "ossie",
        "plan",
        "--from-metric-view",
        source,
        "--into",
        "../x.ossie.yaml",
    )

    assert rc != 0
    assert "must be a path inside the repository" in out["errors"][0]


# --- several files, one plan --------------------------------------------------------------


def test_several_ossie_documents_become_one_plan(tmp_path, capsys):
    """The layer validates every configured file together, so two new files can
    only be authored in one plan: each alone is refused for the other's absence."""
    a = fixture(tmp_path, "converter_fixtureA_ossie.yaml", "models/sales.yaml")
    b = fixture(tmp_path, "converter_fixtureB_ossie.yaml", "models/lineitem.yaml")
    configure(tmp_path, "databricks", ["views/sales.yaml", "views/lineitem.yaml"])

    rc, out = run(
        capsys, tmp_path, "semantic", "databricks", "define", "--from-ossie", a
    )
    assert rc != 0
    assert "views/lineitem.yaml" in out["errors"][0]

    rc, out = run(
        capsys, tmp_path, "semantic", "databricks", "define", "--from-ossie", a, b
    )

    assert rc == 0, out
    data = out["data"]
    assert [c["status"] for c in data["bridge"]["conversions"]] == [
        "included",
        "included",
    ]
    assert sorted(data["defined"]) == ["lineitem", "sales"]
    rc, applied = run(capsys, tmp_path, "transform", "apply", data["plan_id"])
    assert rc == 0, applied


def test_a_mix_stores_the_configured_and_proposes_the_rest(tmp_path, capsys):
    a = fixture(tmp_path, "converter_fixtureA_ossie.yaml", "models/sales.yaml")
    b = fixture(tmp_path, "converter_fixtureB_ossie.yaml", "models/lineitem.yaml")
    configure(tmp_path, "databricks", ["views/lineitem.yaml"])

    rc, out = run(
        capsys,
        tmp_path,
        "semantic",
        "databricks",
        "plan",
        "--from-ossie",
        a,
        "--from-ossie",
        b,
    )

    assert rc == 0, out
    data = out["data"]
    statuses = {c["name"]: c["status"] for c in data["bridge"]["conversions"]}
    assert statuses == {"sales": "proposed", "lineitem": "included"}
    assert data["plan_id"]
    assert (
        "views/sales.yaml is not under semantic.databricks.files"
        in (data["bridge"]["next"])
    )
    assert (
        "--from-ossie models/sales.yaml models/lineitem.yaml" in data["bridge"]["next"]
    )


def test_into_and_name_take_one_source(tmp_path, capsys):
    a = fixture(tmp_path, "converter_fixtureA.yaml", "views/a.yaml")
    b = fixture(tmp_path, "converter_fixtureB.yaml", "views/b.yaml")
    ossie_repo(tmp_path)

    rc, out = run(
        capsys,
        tmp_path,
        "semantic",
        "ossie",
        "plan",
        "--from-metric-view",
        a,
        b,
        "--name",
        "x",
    )

    assert rc != 0
    assert "name one output" in out["errors"][0]
