"""The two policy documents are the source of truth, and this is what makes that true.

`references/pii-policy.md` and `references/cost-controls.md` exist to end a
restatement problem: the PII blocking threshold was stated in five places in four
wordings, and one paragraph about auto-profile pricing was copied verbatim into
five connector documents. Adding two documents only helps if the copies leave,
and only stays helped if new copies cannot arrive.

So three things are held here. The number every document states is the number the
engine holds. The set of documents allowed to state it is frozen. The prose that
was copied lives in one file.

When this fails, fix the document. Editing an allowlist is the fix only when a new
file has a real reason to carry the policy, and that reason belongs next to the
entry, not in a commit message.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

from exmergo_dex_core.guards import PII_BLOCK_CONFIDENCE

ROOT = Path(__file__).resolve().parents[3]

# Resolved once, and absolute: the corpus below is whatever git reports, so a
# `git` picked up from a mutable PATH would decide what this suite checks.
GIT = shutil.which("git")

# The suite runs from a checkout in CI and in development. An installed wheel has
# no repository around it, and nothing checked here is a property of the wheel, so
# skip rather than fail. `tests/ossie/test_corpus.py` makes the same assumption
# about `references/`; this only states it out loud. A checkout with no `git` on
# PATH skips for the same reason rather than failing on a missing executable.
pytestmark = pytest.mark.skipif(
    not (ROOT / "references").is_dir() or GIT is None,
    reason="needs a repository checkout with git available",
)

PII_DOC = "references/pii-policy.md"
COST_DOC = "references/cost-controls.md"

# Append-only history. Every past wording of every policy is in here on purpose,
# and rewriting history to satisfy a lint is worse than the drift it would catch.
EXEMPT = frozenset({"CHANGELOG.md"})

# Where the PII blocking threshold may be stated, and why. Everything else links
# to references/pii-policy.md.
#
# The rule behind the list, so a reviewer can apply it without reading the list:
# a file may carry the number only when it is the file that owns the policy.
# Every other document links, including the three skills, which ship standalone
# through `npx skills add exmergo/dex` and cannot link to `references/`: they
# carry the policy's consequence ("below the blocking threshold it projects with
# a warning") and never its constant, so a skill can go stale on wording but not
# on the number.
PII_RESTATEMENT_ALLOWED = frozenset({PII_DOC})

# "Confidence" alone is too broad to key on: join inference reports a declared
# join at confidence 1.0, and a walkthrough transcript quotes the flag on one
# column at 0.95. Neither states the policy. What identifies a restatement of the
# constant is the threshold concept itself, so key on that and let example
# confidences and join confidences through.
THRESHOLD_SUBJECT = re.compile(r"(?i)\bthreshold\b|blocks projection")
DECIMAL = re.compile(r"(?<![\w.])\d\.\d+(?![\w.])")


def versioned_markdown() -> dict[str, str]:
    """Every committed markdown file, keyed by repository-relative path.

    `git ls-files` rather than a glob, for the same reason the em-dash CI job uses
    it: `unversioned-docs/` is present in a working tree and absent in CI, and a
    check that sees different files on the two machines is worse than no check.
    """

    # Tracked files, plus files that are new but not ignored. Without the second
    # call a policy document added in the working tree is invisible here, so the
    # census would pass on a branch that has not committed yet and fail the
    # moment it does. `--exclude-standard` keeps `unversioned-docs/` out, which
    # is the whole reason for asking git rather than globbing.
    paths: list[str] = []
    for extra in ([], ["--others", "--exclude-standard"]):
        listed = subprocess.run(  # noqa: S603  (a fixed argv, no shell)
            [GIT, "ls-files", "-z", *extra, "*.md", "*.mdc"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        paths.extend(p for p in listed.split("\0") if p)
    return {
        path: (ROOT / path).read_text(encoding="utf-8")
        for path in sorted(set(paths))
        if path not in EXEMPT and (ROOT / path).is_file()
    }


def prose_sentences(text: str):
    """Yield (line number, sentence) for prose only.

    Fenced blocks are dropped whole and inline code spans blanked, on the same
    reasoning as `scripts/check_no_em_dashes.py`: a YAML fragment showing a
    connector's own ceiling is configuration, not a restatement of policy, and a
    check that cannot tell them apart gets silenced rather than satisfied.
    """

    in_fence = False
    block: list[str] = []
    block_start = 1

    def flush():
        if not block:
            return
        joined = " ".join(block)
        joined = re.sub(r"`[^`]*`", lambda m: " " * len(m.group()), joined)
        joined = re.sub(r"https?://\S+", lambda m: " " * len(m.group()), joined)
        for sentence in re.split(r"(?<=[.;:])\s+", joined):
            if sentence.strip():
                yield block_start, sentence

    for line_no, line in enumerate(text.splitlines(), start=1):
        if line.lstrip().startswith("```"):
            yield from flush()
            block = []
            in_fence = not in_fence
            continue
        if in_fence or not line.strip():
            yield from flush()
            block = []
            continue
        # A table row is its own paragraph. Joining a backtick-dense table into
        # one block makes the inline-code masking pair backticks across rows and
        # blank out prose that is not code at all, which is how a restatement
        # hides in a command table.
        if line.lstrip().startswith("|"):
            yield from flush()
            block = [line.strip()]
            block_start = line_no
            yield from flush()
            block = []
            continue
        # These documents hard-wrap, so a sentence routinely straddles a line
        # break. Scanning line by line would miss every claim whose number and
        # subject land on different lines, which is most of them. Paragraphs are
        # the smallest unit that keeps a sentence whole; the reported line is
        # where the paragraph starts.
        if not block:
            block_start = line_no
        block.append(line.strip())
    yield from flush()


def threshold_carriers() -> dict[str, list[str]]:
    """Files whose prose states a decimal in a sentence about the PII threshold."""

    carriers: dict[str, list[str]] = {}
    for path, text in versioned_markdown().items():
        for line_no, sentence in prose_sentences(text):
            if THRESHOLD_SUBJECT.search(sentence) and DECIMAL.search(sentence):
                carriers.setdefault(path, []).append(f"{path}:{line_no}")
    return carriers


def test_every_document_that_states_the_pii_threshold_states_the_engine_s_number():
    expected = f"{PII_BLOCK_CONFIDENCE:g}"
    wrong = []
    for path, text in versioned_markdown().items():
        for line_no, sentence in prose_sentences(text):
            if not THRESHOLD_SUBJECT.search(sentence):
                continue
            wrong.extend(
                f"{path}:{line_no}: states {hit.group()}, engine holds "
                f"{expected}: {sentence.strip()[:120]}"
                for hit in DECIMAL.finditer(sentence)
                if hit.group() != expected
            )
    assert not wrong, (
        "the PII blocking threshold moved in the engine and these documents still "
        f"carry the old number. PII_BLOCK_CONFIDENCE is {expected} "
        "(packages/dex-core/src/exmergo_dex_core/guards/__init__.py).\n  "
        + "\n  ".join(wrong)
    )


def test_the_canonical_pii_document_states_the_threshold_at_all():
    """Deleting the sentence is the other way a source of truth stops being one."""

    text = (ROOT / PII_DOC).read_text(encoding="utf-8")
    assert any(
        THRESHOLD_SUBJECT.search(sentence) and f"{PII_BLOCK_CONFIDENCE:g}" in sentence
        for _, sentence in prose_sentences(text)
    ), f"{PII_DOC} names no blocking threshold, so nothing else can point at it"


def test_only_the_canonical_document_restates_the_threshold():
    carriers = threshold_carriers()
    added = sorted(set(carriers) - PII_RESTATEMENT_ALLOWED)
    gone = sorted(PII_RESTATEMENT_ALLOWED - set(carriers))
    assert not added, (
        "a new restatement of the PII blocking threshold appeared. Link to "
        f"{PII_DOC} instead of repeating the number, or, if this file genuinely "
        "cannot link, add it to PII_RESTATEMENT_ALLOWED with the reason.\n  "
        + "\n  ".join(f"{p}: {', '.join(carriers[p])}" for p in added)
    )
    assert not gone, (
        "these files no longer state the threshold, which is the good direction. "
        "Delete them from PII_RESTATEMENT_ALLOWED so the list keeps meaning what "
        "it says.\n  " + "\n  ".join(gone)
    )


# Prose that was copied verbatim across connector documents before the refactor.
# Matched whitespace-normalized so a reflow does not read as a rewrite.
SHARED_PROSE = {
    "auto-profile pricing": (
        "That scan is billed, and it is priced into the same handshake as the "
        "statements rather than added afterward, so the estimate you confirm is "
        "the whole cost."
    ),
}
SHARED_PROSE_HOME = {"auto-profile pricing": COST_DOC}


def test_the_shared_cost_prose_lives_in_exactly_one_document():
    corpus = {
        path: " ".join(text.split()) for path, text in versioned_markdown().items()
    }
    for label, paragraph in SHARED_PROSE.items():
        needle = " ".join(paragraph.split())
        carriers = sorted(path for path, text in corpus.items() if needle in text)
        home = SHARED_PROSE_HOME[label]
        assert carriers == [home], (
            f"the {label} prose is the kind that was copied into five connector "
            f"documents and drifted. It belongs in {home} and nowhere else; the "
            f"other files link. Found in: {carriers}"
        )


CEILING_FRAGMENT = re.compile(
    r"session_ceiling:\s+\d+\s+# cumulative (bytes|seconds) per UTC day"
)


def test_every_connector_config_example_uses_the_same_ceiling_fragment():
    """The value is per-connector and stays so. The comment around it is policy.

    Six connector documents show this fragment with three different magnitudes and
    two units, all correct. What must not drift is the sentence, because a reader
    copying one fragment reads the comment as the definition of the key.
    """

    offenders = []
    for path, text in versioned_markdown().items():
        for line_no, line in enumerate(text.splitlines(), start=1):
            if "session_ceiling:" in line and not CEILING_FRAGMENT.search(line):
                offenders.append(f"{path}:{line_no}: {line.strip()}")
    assert not offenders, (
        "a connector document invented its own wording for the cumulative ceiling. "
        "Use `session_ceiling: <n>  # cumulative <unit> per UTC day`, and see "
        f"{COST_DOC} for what it means.\n  " + "\n  ".join(offenders)
    )


LINK = re.compile(r"\]\((?!https?:|#|mailto:)([^)\s]+)\)")


def test_relative_links_between_documents_resolve():
    """In a refactor that replaces prose with pointers, a broken pointer deletes
    documentation rather than centralizing it."""

    broken = []
    for path, text in versioned_markdown().items():
        base = (ROOT / path).parent
        for line_no, line in enumerate(text.splitlines(), start=1):
            for target in LINK.findall(line):
                file_part = target.partition("#")[0]
                if file_part and not (base / file_part).exists():
                    broken.append(f"{path}:{line_no}: -> {target}")
    assert not broken, "a relative link points at nothing.\n  " + "\n  ".join(broken)
