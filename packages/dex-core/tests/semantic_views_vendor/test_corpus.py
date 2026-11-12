"""The fixture corpus, read through dex and held to Snowflake's own verdicts (#1288).

The corpus doubles as the set a maintainer deploys to Snowflake with
`scripts/snowflake_semantic_views.py`, and each file's verdict here is the one
Snowflake's `SYSTEM$CREATE_SEMANTIC_VIEW_FROM_YAML(..., verify_only => TRUE)`
returned on a live account:

- `valid/`: Snowflake accepts it, and dex reads it with no error and exactly the
  warning its `# warns:` header names (none where it names none);
- `invalid/`: Snowflake refuses it, and dex refuses it by the rule its
  `# expect:` header names, except where the header says `# static: unchecked`,
  which only the warehouse can decide;
- `stricter/`: Snowflake accepts it, and dex refuses it by design (a duplicate
  YAML key, a legacy Cortex Analyst model), by the rule its header names.

`readback/` holds what `SYSTEM$READ_YAML_FROM_SEMANTIC_VIEW` returned for each
deployed `valid/` view: Snowflake's own normalization (upper-case names, sorted
lists, defaulted `access_modifier` and `data_type`), which must read cleanly and
fingerprint as the file it came from.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("sqlglot")

from exmergo_dex_core.semantic_views.catalog import resolution, semantic_catalog
from exmergo_dex_core.semantic_views.loader import load_files
from exmergo_dex_core.semantic_views.snapshot import semantic_layer

from .conftest import FIXTURES


def _header(path: Path, key: str) -> str | None:
    """The value of a ``# key: value`` header line."""
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith(f"# {key}:"):
            return line.split(":", 1)[1].strip()
    return None


def _files(kind: str) -> list[Path]:
    """The corpus files of one kind, sorted."""
    return sorted((FIXTURES / kind).glob("*.yaml"))


@pytest.mark.parametrize("path", _files("valid"), ids=lambda p: p.stem)
def test_snowflake_accepts_it_and_dex_reads_it(path: Path) -> None:
    """No error, and only the warning the file says it carries."""
    loaded = load_files(FIXTURES, [f"valid/{path.name}"])

    assert loaded.errors == [], [d.render() for d in loaded.errors]
    warned = {d.rule for d in loaded.diagnostics}
    expected = _header(path, "warns")
    assert warned == ({expected} if expected else set()), loaded.notes()
    assert len(loaded.views) == 1
    view = loaded.views[0]
    # Every member reaches the catalog: a refused member would be missing here.
    catalog = semantic_catalog(loaded, resolution(loaded, "snowflake"))
    listed = {d.name for d in catalog.dimensions} | {m.name for m in catalog.metrics}
    for table in view.tables:
        for member in table.members():
            assert f"{view.name}.{table.name}.{member.name}" in listed


@pytest.mark.parametrize(
    "path", [*_files("invalid"), *_files("stricter")], ids=lambda p: p.stem
)
def test_dex_refuses_it_by_the_rule_it_breaks(path: Path) -> None:
    """Named with the file and the element."""
    kind = path.parent.name
    loaded = load_files(FIXTURES, [f"{kind}/{path.name}"])
    if (_header(path, "static") or "").startswith("unchecked"):
        # The one rule only the warehouse can decide: a column's existence.
        assert loaded.errors == []
        return
    rule = _header(path, "expect")
    assert rule, f"{path.name} names no `# expect:` rule"
    refused = [d for d in loaded.errors if d.rule == rule]
    assert refused, (rule, loaded.notes())
    assert all(d.file == f"{kind}/{path.name}" for d in refused)


@pytest.mark.parametrize("path", _files("readback"), ids=lambda p: p.stem)
def test_a_read_back_view_fingerprints_as_its_file(path: Path) -> None:
    """Snowflake's normalization is not a definition change."""
    original = FIXTURES / "valid" / path.name
    if not original.exists():
        original = next(
            p
            for p in _files("valid")
            if _view_name(p).lower() == _view_name(path).lower()
        )
    back = load_files(FIXTURES, [f"readback/{path.name}"])
    authored = load_files(FIXTURES, [f"valid/{original.name}"])
    assert back.errors == [], back.notes()

    def prints(loaded):
        """Each definition's identity, hash and structure, exactly: the
        fingerprint names definitions in lower case, so an upper-cased
        read-back is the same definition rather than a removal and an addition."""
        layer = semantic_layer(loaded, resolution(loaded, "snowflake"))
        return (
            {
                m.name: (m.content_sha256, m.keys, m.relation, m.dimensions, m.measures)
                for m in layer.semantic_models
            },
            {m.name: (m.content_sha256, m.input_measures) for m in layer.metrics},
            {
                r.name: (
                    r.content_sha256,
                    r.model,
                    r.to_model,
                    r.column_pairs,
                    r.cardinality,
                )
                for r in layer.relationships
            },
        )

    assert prints(back) == prints(authored)


def _view_name(path: Path) -> str:
    """The view a file declares, read off its top-level `name:` line."""
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("name:"):
            return line.split(":", 1)[1].strip()
    return path.stem
