"""The three SKILL.md bodies: procedure first, and every pointer one a skill can open.

A body enters an agent's context only when the Skill tool loads it, and its
bundled reference files are read even less, so what a body says first decides
what an agent does (#489). Three properties are held here:

- Each body opens with the procedure, delimited by markers. #502 will assert the
  text between the markers against the engine's own; until then the three copies
  are held equal to each other, so they cannot drift apart before it lands.
- Every `${CLAUDE_SKILL_DIR}` path a skill cites exists in that skill's directory.
  A broken pointer deletes guidance rather than moving it.
- No skill cites a repository-root document as though it could open it. A skill
  installed through `npx skills add exmergo/dex` has no `references/` beside it,
  so where a policy matters the body states its consequence instead.
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


def test_the_three_procedure_sections_are_one_text():
    """Until #502 holds them to the engine's text, they are held to each other."""

    texts = {skill: procedure(body(skill)) for skill in SKILLS}
    reference = texts[SKILLS[0]]
    stale = [skill for skill, text in texts.items() if text != reference]
    assert not stale, (
        f"the procedure in {stale} differs from skills/{SKILLS[0]}/SKILL.md; a "
        "change to it lands in all three"
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
