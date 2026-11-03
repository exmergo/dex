"""The three SKILL.md bodies: procedure first, and every pointer one a skill can open.

A body enters an agent's context only when the Skill tool loads it, and its
bundled reference files are read even less, so what a body says first decides
what an agent does (#489). Four properties are held here:

- Each body opens with the procedure, delimited by markers, and the text between
  them is exactly the engine's (#502), as is the copy in `AGENTS.md`. A change
  lands in `exmergo_dex_core/procedure/procedure.md` and
  `scripts/sync_procedure.py` carries it to every copy.
- Every `${CLAUDE_SKILL_DIR}` path a skill cites exists in that skill's directory.
  A broken pointer deletes guidance rather than moving it.
- No skill cites a repository-root document as though it could open it. A skill
  installed through `npx skills add exmergo/dex` has no `references/` beside it,
  so where a policy matters the body states its consequence instead.
- Each frontmatter fits the Agent Skills specification's caps, which the strictest
  directories and agents enforce.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
SKILLS = ("explore", "transform", "maintain")

# Same reasoning as test_docs_policy.py: an installed wheel has no skills/ beside it.
pytestmark = pytest.mark.skipif(
    not (ROOT / "skills").is_dir(), reason="needs a repository checkout"
)

BEGIN = "<!-- dex:procedure:begin -->"
END = "<!-- dex:procedure:end -->"

SKILL_DIR_PATH = re.compile(r"\$\{CLAUDE_SKILL_DIR\}/([\w./-]+)")
# A bare `references/x.md`, not preceded by `${CLAUDE_SKILL_DIR}/` or by a URL
# path segment. Inside a skill that names the repository's root directory.
ROOT_REFERENCE = re.compile(r"(?<![/\w])references/[\w-]+\.md")


def body(skill: str) -> str:
    return (ROOT / "skills" / skill / "SKILL.md").read_text(encoding="utf-8")


def skill_markdown(skill: str) -> dict[Path, str]:
    directory = ROOT / "skills" / skill
    return {
        path: path.read_text(encoding="utf-8")
        for path in sorted(directory.rglob("*.md"))
    }


def procedure(text: str) -> str:
    assert text.count(BEGIN) == 1 and text.count(END) == 1, (
        "a body carries exactly one procedure section between the markers"
    )
    start, end = text.index(BEGIN), text.index(END)
    assert start < end, "the procedure's begin marker comes before its end marker"
    return text[start + len(BEGIN) : end]


@pytest.mark.parametrize("skill", SKILLS)
def test_each_body_opens_with_the_procedure(skill: str):
    text = body(skill)
    procedure(text)
    after_frontmatter = text.split("\n---\n", 1)[1]
    first_section = after_frontmatter.index("\n## ")
    assert after_frontmatter.index(BEGIN) < first_section, (
        f"skills/{skill}/SKILL.md has a section before the procedure; the "
        "procedure is what an agent must read first"
    )


@pytest.mark.parametrize("skill", SKILLS)
def test_the_frontmatter_parses_as_yaml_with_a_string_description(skill: str):
    """`npx skills install` reads the frontmatter with a strict YAML parser. A
    `description` that is an unquoted plain scalar containing `: ` reads as a
    nested mapping there, and the skill silently fails to install (the 1.0.1
    fix). The descriptions are long and edited by hand (#233), so the parse is
    held here rather than discovered by the next installer."""

    import yaml

    text = body(skill)
    assert text.startswith("---\n"), f"skills/{skill}/SKILL.md opens with frontmatter"
    frontmatter = text.split("\n---\n", 1)[0].removeprefix("---\n")
    parsed = yaml.safe_load(frontmatter)
    assert isinstance(parsed, dict), "the frontmatter is a mapping"
    assert parsed.get("name") == skill
    description = parsed.get("description")
    assert isinstance(description, str) and description.strip(), (
        f"skills/{skill}/SKILL.md's description is one string, not a mapping or empty"
    )


# The Agent Skills specification's caps, in code points (what `skills-ref validate`
# and `len()` both count).
DESCRIPTION_CAP = 1024
COMPATIBILITY_CAP = 500


@pytest.mark.parametrize("skill", SKILLS)
def test_the_frontmatter_fits_the_agent_skills_caps(skill: str):
    """The specification caps `description` at 1,024 characters and the strictest
    consumers enforce it: OpenAI's plugin portal rejects a longer one and
    awesome-copilot's `vally lint` fails it, while Codex, VS Code Copilot and
    SkillsMP cut it mid-word. Claude Code allows 1,536, so a description tuned only
    in Claude Code drifts past the cap without anything in this repo noticing.
    `compatibility` is optional and capped at 500."""

    import yaml

    frontmatter = body(skill).split("\n---\n", 1)[0].removeprefix("---\n")
    parsed = yaml.safe_load(frontmatter)
    description = parsed["description"]
    assert len(description) <= DESCRIPTION_CAP, (
        f"skills/{skill}/SKILL.md's description is {len(description)} characters; "
        f"the Agent Skills specification caps it at {DESCRIPTION_CAP}"
    )
    compatibility = parsed.get("compatibility", "")
    assert len(compatibility) <= COMPATIBILITY_CAP, (
        f"skills/{skill}/SKILL.md's compatibility is {len(compatibility)} "
        f"characters; the Agent Skills specification caps it at {COMPATIBILITY_CAP}"
    )


def test_every_procedure_copy_is_the_engines_text():
    """The procedure lives once, in the engine (#502). Every copy an agent reads
    (the three skill bodies and `AGENTS.md`) is held to it, and a failure names
    every stale copy at once rather than the first."""

    from exmergo_dex_core.procedure import text

    copies = [f"skills/{skill}/SKILL.md" for skill in SKILLS] + ["AGENTS.md"]
    stale = [
        relpath
        for relpath in copies
        if procedure((ROOT / relpath).read_text(encoding="utf-8")) != "\n" + text()
    ]
    assert not stale, (
        f"these copies of the procedure differ from the engine's text: {stale}. "
        "Edit packages/dex-core/src/exmergo_dex_core/procedure/procedure.md, then "
        "run `uv run --project packages/dex-core python scripts/sync_procedure.py`"
    )


@pytest.mark.parametrize("skill", SKILLS)
def test_every_cited_skill_path_exists(skill: str):
    directory = ROOT / "skills" / skill
    missing = [
        f"{path.relative_to(ROOT)}: ${{CLAUDE_SKILL_DIR}}/{target}"
        for path, text in skill_markdown(skill).items()
        for target in SKILL_DIR_PATH.findall(text)
        if not (directory / target).exists()
    ]
    assert not missing, "a skill cites a file it does not ship:\n  " + "\n  ".join(
        missing
    )


@pytest.mark.parametrize("skill", SKILLS)
def test_every_shipped_reference_is_cited_by_the_body(skill: str):
    """A reference file nothing points at is one no agent will ever open."""

    text = body(skill)
    uncited = [
        path.name
        for path in sorted((ROOT / "skills" / skill / "references").glob("*.md"))
        if f"${{CLAUDE_SKILL_DIR}}/references/{path.name}" not in text
    ]
    assert not uncited, f"skills/{skill}/SKILL.md never cites: {uncited}"


@pytest.mark.parametrize("skill", SKILLS)
def test_no_skill_cites_a_repository_root_document(skill: str):
    offenders = [
        f"{path.relative_to(ROOT)}:{line_no}: {line.strip()}"
        for path, text in skill_markdown(skill).items()
        for line_no, line in enumerate(text.splitlines(), start=1)
        if ROOT_REFERENCE.search(line) or "engine repository" in line
    ]
    assert not offenders, (
        "a skill cites a repository-root document it cannot open once installed. "
        "Cite a file under ${CLAUDE_SKILL_DIR}, give an absolute URL meant for a "
        "human, or state the policy's consequence instead.\n  " + "\n  ".join(offenders)
    )
