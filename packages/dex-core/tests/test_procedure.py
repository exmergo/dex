"""`dex procedure`: the agent procedure, held once in the engine (#502).

Held here:

- The command answers with no project, no `.dex/config.yml`, no connector and no
  connection, in full and values-free mode alike, because it is what an agent
  reads before any of those exist.
- `--block` is exactly what `check` accepts as current, so a pasted block is
  recognized as current.
- `--check` reports each copy's status and never echoes a file's content, and
  any copy that is not current makes the envelope an error, so it gates CI.
- The text cannot cite a command or flag the CLI does not have, and its example
  declaration parses as a real payload. A renamed flag fails here, not in an
  agent's session.
"""

from __future__ import annotations

import argparse
import json
import re

import pytest

from exmergo_dex_core import procedure
from exmergo_dex_core.cli import COMMAND_SURFACE, _build_parser, main
from exmergo_dex_core.semantic_declarations import Evidence, parse_declarations


def _run(argv: list[str], capsys) -> tuple[int, dict]:
    code = main(argv)
    out = capsys.readouterr().out
    assert out.count("\n") == 1, "exactly one envelope on stdout"
    return code, json.loads(out)


@pytest.fixture
def bare_dir(tmp_path, monkeypatch):
    """A directory with no project, no config, and no engine allowed to exist."""

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DEX_RESULT_MODE", raising=False)

    def refuse(*_args, **_kwargs):
        raise AssertionError("dex procedure built an engine")

    monkeypatch.setattr("exmergo_dex_core.cli.DexEngine.from_repo", refuse)
    return tmp_path


def test_it_answers_with_nothing_configured(bare_dir, capsys):
    code, envelope = _run(["procedure"], capsys)
    assert code == 0 and envelope["status"] == "ok"
    data = envelope["data"]
    assert data["text"] == procedure.text()
    assert data["digest"] == procedure.digest()
    assert data["markers"] == {"begin": procedure.BEGIN, "end": procedure.END}
    assert "block" not in data and "copies" not in data
    assert data["text"].startswith("## Procedure\n")
    assert not list(bare_dir.iterdir()), "nothing is written"


def test_values_free_mode_is_allowed_and_stated(bare_dir, capsys, monkeypatch):
    monkeypatch.setenv("DEX_RESULT_MODE", "values_free")
    code, envelope = _run(["procedure"], capsys)
    assert code == 0
    assert envelope["data"]["result_mode"] == "values_free"


def test_the_block_is_what_check_calls_current(bare_dir, capsys):
    _, envelope = _run(["procedure", "--block"], capsys)
    pasted = "# Our project\n\nSome notes.\n\n" + envelope["data"]["block"]
    assert procedure.check(pasted) is procedure.CopyStatus.CURRENT
    (bare_dir / "CLAUDE.md").write_text(pasted, encoding="utf-8")
    code, envelope = _run(["procedure", "--check", "CLAUDE.md"], capsys)
    assert code == 0
    assert envelope["data"]["copies"] == [
        {
            "path": "CLAUDE.md",
            "status": "current",
            "detail": "matches this engine's procedure",
        }
    ]


def test_check_names_every_copy_that_is_not_current_and_never_echoes_it(
    bare_dir, capsys
):
    sentinel = "SENTINEL-PROJECT-SECRET-PROSE"
    stale = procedure.block().replace("Dex works in one loop", sentinel)
    (bare_dir / "stale.md").write_text(stale, encoding="utf-8")
    (bare_dir / "missing.md").write_text(f"# notes\n{sentinel}\n", encoding="utf-8")
    (bare_dir / "twice.md").write_text(procedure.block() * 2, encoding="utf-8")
    (bare_dir / "current.md").write_text(procedure.block(), encoding="utf-8")
    argv = ["procedure"]
    for name in ("current.md", "stale.md", "missing.md", "twice.md", "absent.md"):
        argv += ["--check", name]

    code, envelope = _run(argv, capsys)

    assert code == 1
    assert envelope["status"] == "error" and envelope["reason"] == "request"
    statuses = {row["path"]: row["status"] for row in envelope["data"]["copies"]}
    assert statuses == {
        "current.md": "current",
        "stale.md": "stale",
        "missing.md": "missing",
        "twice.md": "malformed",
        "absent.md": "absent",
    }
    assert len(envelope["errors"]) == 4
    assert sentinel not in json.dumps(envelope)
    # The refusal still carries the text to refresh the copies with.
    assert envelope["data"]["text"] == procedure.text()


def test_region_and_check_agree_on_marker_edge_cases():
    body = procedure.block()
    assert procedure.region(body) == procedure.text()
    assert procedure.check(procedure.END + "\n" + procedure.BEGIN) is (
        procedure.CopyStatus.MALFORMED
    )
    assert procedure.check(procedure.BEGIN) is procedure.CopyStatus.MALFORMED
    assert procedure.check("") is procedure.CopyStatus.MISSING


def test_the_package_ships_the_text():
    text = procedure.text()
    assert text.endswith("\n") and not text.endswith("\n\n")
    assert procedure.BEGIN not in text and procedure.END not in text, (
        "the text must not contain its own markers, or every copy is malformed"
    )


# --- the text cannot drift from the CLI it describes -----------------------

_BACKTICK = re.compile(r"`([^`\n]+)`")


def _subparsers(parser: argparse.ArgumentParser) -> dict[str, argparse.ArgumentParser]:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return dict(action.choices)
    return {}


def _cited_commands() -> list[list[str]]:
    return [
        span.split()
        for span in _BACKTICK.findall(procedure.text())
        if span.startswith("dex ") and span != "dex <command>"
    ]


def test_every_cited_command_and_flag_exists():
    groups = _subparsers(_build_parser())
    cited = _cited_commands()
    assert len(cited) >= 10, "the procedure names its commands literally"
    problems: list[str] = []
    for tokens in cited:
        group = tokens[1]
        if group not in COMMAND_SURFACE:
            problems.append(f"{' '.join(tokens)}: no group {group!r}")
            continue
        parser = groups[group]
        rest = tokens[2:]
        if COMMAND_SURFACE[group] and not rest:
            continue  # the group named as a whole ("a `dex config` diff")
        if COMMAND_SURFACE[group]:
            if rest[0] not in COMMAND_SURFACE[group]:
                problems.append(f"{' '.join(tokens)}: no such subcommand")
                continue
            parser = _subparsers(parser)[rest[0]]
            rest = rest[1:]
        known = {opt for action in parser._actions for opt in action.option_strings}
        problems += [
            f"{' '.join(tokens)}: no flag {flag}"
            for flag in rest
            if flag.startswith("--") and flag not in known
        ]
    assert not problems, "\n".join(problems)


def test_the_example_declaration_is_a_valid_payload():
    match = re.search(r"```json\n(.*?)```", procedure.text(), re.DOTALL)
    assert match, "the procedure shows the payload a declaration travels in"
    payload = json.loads(match.group(1))
    assert {"edits", "declarations"} <= set(payload)
    parsed = parse_declarations(payload["declarations"])
    assert parsed and parsed[0].assumptions, "the example records an assumption"


def test_the_evidence_order_is_the_engines_tiers_strongest_first():
    text = procedure.text()
    ordered = re.findall(r"^\d\. `(\w+)`:", text, re.MULTILINE)
    assert ordered == [tier.value for tier in Evidence]


def test_the_vocabulary_it_teaches_is_the_engines():
    """An agent reading only CLAUDE.md learns the role and additivity values here
    (#492's first baseline: without them it wrote `ratio` and `count:games`), so
    they must be exactly the values the declaration parser accepts."""

    from exmergo_dex_core.semantic_declarations import Additivity, Role

    text = " ".join(procedure.text().split())
    roles = re.search(r"`role` is (.*?);", text).group(1)
    assert re.findall(r"`(\w+)`", roles) == [role.value for role in Role]
    additivity = re.search(r"`additivity` is (.*?);", text).group(1)
    assert re.findall(r"`(\w+)`", additivity) == [a.value for a in Additivity]
