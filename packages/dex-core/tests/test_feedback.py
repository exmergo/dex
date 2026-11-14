"""`dex feedback` and the report pointer on internal errors (#1299).

Held here:

- The command answers with no project, no config, no connector and no engine, in
  full and values-free mode alike: a broken project is a common reason to report.
- The environment names the connector and semantic vendor only where
  `.dex/config.yml` writes them, and survives a config that does not load.
- Every field a link prefills is a text field of the form it targets, by id, so
  renaming a form field cannot silently empty a link.
- An error with `reason: internal` carries `data.report`, whose link names the
  command and the exception class and never the error text; a deliberate
  refusal carries none.
"""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
import yaml

from exmergo_dex_core import feedback
from exmergo_dex_core.cli import main

_TEMPLATES = Path(__file__).resolve().parents[3] / ".github" / "ISSUE_TEMPLATE"


def _run(argv: list[str], capsys) -> tuple[int, dict]:
    code = main(argv)
    out = capsys.readouterr().out
    assert out.count("\n") == 1, "exactly one envelope on stdout"
    return code, json.loads(out)


def _query(link: str) -> dict[str, str]:
    parts = urlsplit(link)
    assert f"{parts.scheme}://{parts.netloc}" == "https://github.com"
    assert parts.path == "/exmergo/dex/issues/new"
    return {key: values[0] for key, values in parse_qs(parts.query).items()}


@pytest.fixture
def bare_dir(tmp_path, monkeypatch):
    """A directory with no project, no config, and no engine allowed to exist."""

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DEX_RESULT_MODE", raising=False)

    def refuse(*_args, **_kwargs):
        raise AssertionError("dex feedback built an engine")

    monkeypatch.setattr("exmergo_dex_core.cli.DexEngine.from_repo", refuse)
    return tmp_path


def test_it_answers_with_nothing_configured(bare_dir, capsys):
    code, envelope = _run(["feedback"], capsys)
    assert code == 0 and envelope["status"] == "ok"
    data = envelope["data"]
    assert set(data) == {"environment", "links", "guidance"}
    environment = data["environment"]
    assert environment["engine_version"] and environment["python"]
    assert environment["connector"] is None
    assert environment["semantic_vendor"] is None
    assert environment["result_mode"] == "full"
    assert set(data["links"]) == {"bug", "feature", "issues", "security"}
    assert any("SECURITY.md" in line for line in data["guidance"])
    assert not list(bare_dir.iterdir()), "nothing is written"


def test_values_free_mode_is_allowed_and_stated(bare_dir, capsys, monkeypatch):
    monkeypatch.setenv("DEX_RESULT_MODE", "values_free")
    code, envelope = _run(["feedback"], capsys)
    assert code == 0
    assert envelope["data"]["result_mode"] == "values_free"
    assert envelope["data"]["environment"]["result_mode"] == "values_free"


def test_only_what_the_config_writes_is_reported(bare_dir, capsys):
    (bare_dir / ".dex").mkdir()
    config = bare_dir / ".dex" / "config.yml"
    config.write_text("connector: snowflake\n", encoding="utf-8")
    _, envelope = _run(["feedback"], capsys)
    environment = envelope["data"]["environment"]
    assert environment["connector"] == "snowflake"
    # The schema defaults the vendor to dbt; a file that does not say so has
    # chosen no semantic layer, and the report must not claim one.
    assert environment["semantic_vendor"] is None

    config.write_text(
        "connector: snowflake\nsemantic:\n  vendor: ossie\n  ossie:\n"
        "    files: [semantic/model.ossie.yaml]\n",
        encoding="utf-8",
    )
    _, envelope = _run(["feedback"], capsys)
    assert envelope["data"]["environment"]["semantic_vendor"] == "ossie"


def test_a_config_that_does_not_load_still_answers(bare_dir, capsys):
    (bare_dir / ".dex").mkdir()
    (bare_dir / ".dex" / "config.yml").write_text("connector: [\n", encoding="utf-8")
    code, envelope = _run(["feedback"], capsys)
    assert code == 0 and envelope["status"] == "ok"
    assert envelope["data"]["environment"]["connector"] is None


def test_the_command_is_named_in_the_bug_link(bare_dir, capsys):
    _, envelope = _run(["feedback", "--command", "dex explore profile"], capsys)
    query = _query(envelope["data"]["links"]["bug"])
    assert query["template"] == feedback.BUG_TEMPLATE
    assert query["command"] == "dex explore profile"
    assert query["title"].startswith("[bug] dex explore profile")
    assert query["version"] == envelope["data"]["environment"]["engine_version"]


def _text_field_ids(template: str) -> set[str]:
    form = yaml.safe_load((_TEMPLATES / template).read_text(encoding="utf-8"))
    return {
        element["id"]
        for element in form["body"]
        if element.get("type") in {"input", "textarea"} and "id" in element
    }


@pytest.mark.parametrize(
    ("template", "fields", "link"),
    [
        (
            feedback.BUG_TEMPLATE,
            feedback.BUG_PREFILL_FIELDS,
            feedback.bug_link(feedback.environment(), command="explore map"),
        ),
        (
            feedback.FEATURE_TEMPLATE,
            feedback.FEATURE_PREFILL_FIELDS,
            feedback.feature_link(feedback.environment()),
        ),
    ],
)
def test_every_prefilled_field_is_a_text_field_of_its_form(template, fields, link):
    ids = _text_field_ids(template)
    assert set(fields) <= ids, f"{template} lacks {set(fields) - ids}"
    query = _query(link)
    assert query["template"] == template
    assert set(query) - {"template", "title"} <= set(fields)


def test_an_internal_error_carries_a_report_without_the_error_text(
    tmp_path, capsys, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DEX_RESULT_MODE", raising=False)
    message = "analytics.customer_emails at /home/someone/project"

    def explode(*_args, **_kwargs):
        raise RuntimeError(message)

    monkeypatch.setattr("exmergo_dex_core.cli.DexEngine.from_repo", explode)
    code, envelope = _run(["explore", "inventory", "--connector", "duckdb"], capsys)
    assert code == 1
    assert envelope["reason"] == "internal"
    report = envelope["data"]["report"]
    assert report["likely_dex_defect"] is True
    assert report["command"] == "dex feedback --command 'explore inventory'"
    query = _query(report["link"])
    assert query["command"] == "dex explore inventory"
    assert "exception: RuntimeError" in query["env"]
    # The engine never got built, so the flag is what names the connector.
    assert "connector: duckdb" in query["env"]
    assert "customer_emails" not in report["link"]
    assert "someone" not in report["link"]


def test_a_deliberate_refusal_carries_no_report(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DEX_RESULT_MODE", raising=False)
    from exmergo_dex_core.errors import RequestError

    def refuse(*_args, **_kwargs):
        raise RequestError("no such thing")

    monkeypatch.setattr("exmergo_dex_core.cli.DexEngine.from_repo", refuse)
    _, envelope = _run(["explore", "inventory", "--connector", "duckdb"], capsys)
    assert envelope["reason"] == "request"
    assert "report" not in envelope["data"]
