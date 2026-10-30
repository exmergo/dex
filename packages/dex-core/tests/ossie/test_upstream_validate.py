"""Upstream's own validator agrees with dex, on dex's corpus and on what dex writes.

Opt-in: set ``DEX_OSSIE_UPSTREAM`` to an Apache Ossie checkout at the pinned
commit, and this runs that checkout's ``validation/validate.py`` (a script, not a
package, needing ``jsonschema``, ``pyyaml`` and ``sqlglot``, which dex's own test
environment carries). Skipped otherwise, because the corpus and every other test
here run offline against the bundled schema alone.

Two directions, because agreement is two claims. A document dex reads must pass
upstream, or dex is reading something the format does not allow; and a document
dex refuses for a reason upstream also checks must fail upstream, or dex is
stricter than the format without saying so. dex's one cross-file rule (a model
name declared in two files) has no upstream counterpart, since upstream reads one
file at a time.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

FIXTURES = Path(__file__).parent / "fixtures"
DOCUMENTS = FIXTURES / "documents"

UPSTREAM = os.environ.get("DEX_OSSIE_UPSTREAM")

pytestmark = pytest.mark.skipif(
    not UPSTREAM, reason="set DEX_OSSIE_UPSTREAM to an Apache Ossie checkout"
)


def upstream_passes(path: Path) -> tuple[bool, str]:
    validator = Path(UPSTREAM or "") / "validation" / "validate.py"
    done = subprocess.run(  # noqa: S603 - the person running the suite named the checkout
        [sys.executable, str(validator), str(path)],
        capture_output=True,
        text=True,
        check=False,
    )
    return done.returncode == 0, done.stdout + done.stderr


def _verdicts() -> list[tuple[str, bool]]:
    """Each document's verdict read alone, as upstream reads one at a time.

    A case of several documents refuses for a cross-file reason that is dex's
    own (a model name declared twice), which a validator reading one file
    cannot see, so such a case says only that its documents read on their own.
    """

    cases = yaml.safe_load((FIXTURES / "cases.yaml").read_text(encoding="utf-8"))
    out: dict[str, bool] = {}
    for case in cases["cases"]:
        refused = any(d["severity"] == "error" for d in case["diagnostics"])
        for name in case["documents"]:
            alone = len(case["documents"]) == 1
            out[name] = out.get(name, False) or (refused and alone)
    return sorted(out.items())


@pytest.mark.parametrize(("name", "refused"), _verdicts())
def test_upstream_agrees_with_the_corpus_verdict(name: str, refused: bool) -> None:
    passed, output = upstream_passes(DOCUMENTS / name)

    assert passed is not refused, (
        f"dex {'refuses' if refused else 'reads'} {name} and upstream "
        f"{'passes' if passed else 'fails'} it:\n{output}"
    )


def _apply(root: Path, capsys, *argv: str, body: dict) -> None:
    from exmergo_dex_core.cli import main

    payload = root / "payload.json"
    payload.write_text(json.dumps(body), encoding="utf-8")
    rc = main(["--repo-root", str(root), *argv, "--edits-file", str(payload)])
    assert rc == 0, capsys.readouterr().out
    capsys.readouterr()
    assert main(["--repo-root", str(root), "transform", "apply"]) == 0
    capsys.readouterr()


def test_documents_semantic_ossie_writes_pass_upstream(tmp_path: Path, capsys):
    """A whole document authored with `define`, then a dataset annotated with
    `plan` and declarations: both written files pass upstream's validator."""

    from .test_derivation import COMMERCE, ORDERS_TEXT, _configure

    _configure(tmp_path, [COMMERCE])
    _apply(
        tmp_path,
        capsys,
        "semantic",
        "ossie",
        "define",
        "author",
        body={"edits": [{"path": COMMERCE, "content": ORDERS_TEXT}]},
    )
    passed, output = upstream_passes(tmp_path / COMMERCE)
    assert passed, output

    _apply(
        tmp_path,
        capsys,
        "semantic",
        "ossie",
        "plan",
        "declare",
        body={
            "declarations": [
                {
                    "model": "orders",
                    "grain": ["o_orderkey"],
                    "population": {"rule": "Placed orders"},
                    "columns": {
                        "o_custkey": {
                            "role": "foreign_key",
                            "references": "stg_customers.id",
                        },
                        "o_total": {"role": "measure", "aggregation": "sum"},
                    },
                }
            ]
        },
    )
    passed, output = upstream_passes(tmp_path / COMMERCE)
    assert passed, output


def test_a_derived_dataset_passes_upstream(tmp_path: Path) -> None:
    from .test_derivation import COMMERCE, COMMERCE_TEXT, TestOssieDerivation, put

    derivation = TestOssieDerivation()
    derivation.root = tmp_path
    put(tmp_path, COMMERCE, COMMERCE_TEXT)
    result = derivation.make_derivation_source().derive_semantic(
        derivation.a_derive_request()
    )
    (edit,) = result.edits
    derived = tmp_path / "derived.ossie.yaml"
    derived.write_text(edit.new_content or "", encoding="utf-8")

    passed, output = upstream_passes(derived)

    assert passed, output
