#!/usr/bin/env python3
"""Flag em dashes and the look-alikes people use to smuggle the same pause back in.

Usage:
    python check_em_dashes.py path/to/draft.txt
    echo "Your text here" | python check_em_dashes.py

Reads from the file arguments if given, otherwise from stdin. Prints every hit
with its line and column and a short suggested fix, then exits 1 if anything was
found and 0 if the text is clean.

The goal is to catch dashes doing the work of sentence punctuation, not to
"correct" legitimate hyphens. The checker therefore leaves alone:
  - hyphenated compounds (well-known, state-of-the-art, mother-in-law)
  - number, date, and score ranges (10-20, 2019-2024, a 3-1 win)
  - hyphens inside code spans, fenced code blocks, and URLs
  - minus signs in arithmetic (5 - 3)
  - list markers at the start of a line ("- item")
  - markdown structure: front matter delimiters and horizontal rules (a line of
    three or more hyphens) and table separator rows (| --- | --- |)
"""

import re
import sys

EM_DASH = "—"        # — the real thing
EN_DASH = "–"        # – sometimes used as a stand-in between words
HORIZONTAL_BAR = "―"  # ― another look-alike

GENERIC_FIX = (
    "Replace with a comma, semicolon, colon, period, or parentheses, "
    "and recast the sentence if a plain swap reads awkwardly."
)


def mask_code_and_urls(line):
    """Blank out inline code spans and URLs so hyphens inside them are ignored.

    Returns a copy of the line where those regions are replaced by spaces, which
    preserves column numbers for everything else on the line.
    """
    masked = list(line)

    def blank(match):
        for i in range(match.start(), match.end()):
            masked[i] = " "

    for m in re.finditer(r"`[^`]*`", line):       # inline code: `like this`
        blank(m)
    for m in re.finditer(r"https?://\S+", line):  # bare URLs
        blank(m)
    for m in re.finditer(r"\S+/\S+", line):        # file paths / slashed tokens
        blank(m)
    return "".join(masked)


def is_markdown_dash_structure(line):
    """True if the line is markdown structure that legitimately uses hyphen runs.

    These are formatting, not sentence punctuation, so they are not stand-ins:
      - a front matter delimiter or horizontal rule (a line of only hyphens, 3+),
      - a table separator row (only pipes, hyphens, colons, and spaces).

    A line of prose is never matched here, so a real stand-in that happens to use
    three hyphens, like "she paused --- then left", is still caught.
    """
    stripped = line.strip()
    if re.fullmatch(r"-{3,}", stripped):
        return True
    if "|" in stripped and "-" in stripped and re.fullmatch(r"[\s|:-]+", stripped):
        return True
    return False


def find_hits(line):
    """Yield (column, matched_text, fix) for every dash problem on one line."""
    scan = mask_code_and_urls(line)

    # 1. The em dash itself, plus the horizontal bar look-alike: always a hit.
    for ch in (EM_DASH, HORIZONTAL_BAR):
        start = 0
        while True:
            idx = scan.find(ch, start)
            if idx == -1:
                break
            yield idx, ch, GENERIC_FIX
            start = idx + 1

    # 2. En dash used between letters or padded with spaces (a stand-in pause).
    #    A digit-to-digit en dash (a real range) is left alone.
    for m in re.finditer(rf"(?<=[^\W\d])\s*{EN_DASH}\s*(?=[^\W\d])", scan):
        yield m.start(), m.group(), (
            "En dash used as a pause. " + GENERIC_FIX
        )

    # 3. Double hyphen standing in for an em dash: word--word or spaced -- .
    for m in re.finditer(r"(?<=\w)--(?=\w)|(?<=\s)--(?=\s)|(?<=\s)--(?=\S)", scan):
        yield m.start(), m.group(), (
            "Double hyphen used as a dash. " + GENERIC_FIX
        )

    # 4. Hyphen padded with spaces between two words (letter - letter).
    #    Skips a list marker at line start and digit-to-digit ranges/minus signs.
    for m in re.finditer(r"(?<=[^\W\d])\s-\s(?=[^\W\d])", scan):
        yield m.start(), m.group(), (
            "Spaced hyphen used as a pause. " + GENERIC_FIX
        )


def check(text):
    """Return a list of (line_no, col_no, matched, fix), 1-indexed."""
    hits = []
    in_fence = False
    for line_no, line in enumerate(text.splitlines(), start=1):
        # Toggle fenced code blocks (``` ... ```) and skip their contents.
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        # Leave markdown structure (front matter, horizontal rules, table
        # separators) alone; its hyphen runs are formatting, not punctuation.
        if is_markdown_dash_structure(line):
            continue
        for col, matched, fix in find_hits(line):
            hits.append((line_no, col + 1, matched, fix))
    return hits


def read_input(argv):
    if argv:
        chunks = []
        for path in argv:
            with open(path, "r", encoding="utf-8") as fh:
                chunks.append((path, fh.read()))
        return chunks
    return [("<stdin>", sys.stdin.read())]


def main():
    sources = read_input(sys.argv[1:])
    total = 0
    for label, text in sources:
        hits = check(text)
        for line_no, col_no, matched, fix in hits:
            shown = matched.strip() or matched
            print(f"{label}:{line_no}:{col_no}: found '{shown}'")
            print(f"    fix: {fix}")
        total += len(hits)

    if total:
        noun = "issue" if total == 1 else "issues"
        print(f"\n{total} {noun} found. Rewrite each one using real punctuation.")
        return 1
    print("Clean: no em dashes or stand-ins found.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
