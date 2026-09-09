"""Classification: what an edit contains, from the content and nothing else.

The declared :class:`~exmergo_dex_core.transform.plans.EditKind` and the filename
are supplied by whoever authored the edit and describe where the file goes. A
host deciding whether a change may be applied offline is asking what is in it, and
these assert the two never get confused.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from exmergo_dex_core import DexEngine
from exmergo_dex_core.edits import Edit, EditOp
from exmergo_dex_core.transform.classify import (
    ArtifactClass,
    classify_content,
    classify_edit,
)
from exmergo_dex_core.transform.plans import EditKind, PlanEdit

DECLARATIVE_SEMANTIC = """version: 2
semantic_models:
  - name: things
    model: ref('things')
    entities: [{name: thing, type: primary, expr: id}]
    measures: [{name: things_count, agg: count, expr: id}]
metrics:
  - name: things_count
    type: simple
    label: Things
    type_params: {measure: things_count}
"""

HOOKED = (
    DECLARATIVE_SEMANTIC
    + """
models:
  - name: things
    config:
      post-hook: "grant select on {{ this }} to role reporter"
"""
)

GRANTING = (
    DECLARATIVE_SEMANTIC
    + """
models:
  - name: things
    config:
      grants:
        select: ['reporter']
"""
)

FILTERED_METRIC = """metrics:
  - name: live_sessions
    label: Live sessions
    type: simple
    type_params:
      measure:
        name: session_count
        filter: "{{ Dimension('session__is_deleted') }} = false"
"""


def signals(result) -> set[str]:
    return {s.signal for s in result.signals}


def test_a_semantic_declaration_is_declarative():
    result = classify_content(DECLARATIVE_SEMANTIC, path="models/things.yml")
    assert result.artifact_class is ArtifactClass.DECLARATIVE
    assert result.parsed is True
    # `ref()` and the other names a project already declares are not macro calls:
    # flagging them would make the signal useless on every real semantic file.
    assert signals(result) == set()


def test_a_semantic_declaration_beside_a_hook_is_executable():
    result = classify_content(HOOKED, path="models/things.yml")
    assert result.artifact_class is ArtifactClass.EXECUTABLE
    assert "post_hook" in signals(result)
    # The finding is located where a reviewer will open the file.
    where = next(s.where for s in result.signals if s.signal == "post_hook")
    assert where.endswith("config.post-hook")


def test_a_metric_filters_dimension_reference_is_declarative():
    """`Dimension('...')` dispatches into the semantic layer's own resolution
    against definitions the project already declares, not into a macro the
    repository wrote (#445), so it must not read the same as an unknown call."""

    result = classify_content(FILTERED_METRIC, path="models/live_sessions.yml")
    assert result.artifact_class is ArtifactClass.DECLARATIVE
    # Still reported: a host applying its own policy wants the reference even
    # where it did not decide the class.
    assert "semantic_ref" in signals(result)
    assert "macro_call" not in signals(result)
    detail = next(s.detail for s in result.signals if s.signal == "semantic_ref")
    assert detail == "Dimension(session__is_deleted)"


@pytest.mark.parametrize(
    ("callee", "args"),
    [
        ("Dimension", "'session__is_deleted'"),
        ("TimeDimension", "'metric_time', 'day'"),
        ("Entity", "'session'"),
    ],
)
def test_every_metricflow_filter_callee_is_declarative(callee, args):
    """The narrowing covers all three names the vendor's grammar defines, not
    just the one in the report."""

    content = FILTERED_METRIC.replace(
        "Dimension('session__is_deleted')", f"{callee}({args})"
    )
    result = classify_content(content, path="models/live_sessions.yml")
    assert result.artifact_class is ArtifactClass.DECLARATIVE
    assert "semantic_ref" in signals(result)


def test_a_filtered_metric_beside_a_hook_is_still_executable():
    """The stronger-verdict rule is untouched: what changes is only whether a
    bare dimension reference is evidence of repository-controlled execution."""

    hooked = (
        FILTERED_METRIC
        + """
models:
  - name: sessions
    config:
      post-hook: "select 1"
"""
    )
    result = classify_content(hooked, path="models/live_sessions.yml")
    assert result.artifact_class is ArtifactClass.EXECUTABLE
    assert {"semantic_ref", "post_hook"} <= signals(result)


def test_a_metric_filter_calling_a_repository_macro_is_still_executable():
    """The narrowing is to the vendor's own vocabulary, not to filters in
    general: a macro the project defines is still repository-controlled code."""

    content = FILTERED_METRIC.replace(
        "Dimension('session__is_deleted')", "my_custom_macro('x')"
    )
    result = classify_content(content, path="models/live_sessions.yml")
    assert result.artifact_class is ArtifactClass.EXECUTABLE
    assert "macro_call" in signals(result)
    detail = next(s.detail for s in result.signals if s.signal == "macro_call")
    assert detail == "my_custom_macro"


def test_unparseable_content_with_a_dimension_like_call_stays_unknown():
    """The new callee narrowing must not weaken the never-declarative-on-
    silence guarantee: unparseable content is unknown regardless of what it
    contains."""

    result = classify_content("a: [1, 2\n  Dimension('x')\n", path="models/x.yml")
    assert result.artifact_class is ArtifactClass.UNKNOWN
    assert result.parsed is False


def test_a_repository_macro_named_dimension_outside_a_filter_is_still_a_macro_call():
    """The narrowing is scoped to the ``filter:`` value MetricFlow's grammar
    actually defines, not to the callee name everywhere it appears (#445
    review). A project is free to define its own macro called ``Dimension``;
    calling it from a description is calling repository-controlled code, and
    must classify the same as any other unrecognized macro."""

    content = """version: 2
models:
  - name: things
    description: "{{ Dimension('not_a_real_filter') }}"
"""
    result = classify_content(content, path="models/things.yml")
    assert result.artifact_class is ArtifactClass.EXECUTABLE
    assert "macro_call" in signals(result)
    assert "semantic_ref" not in signals(result)
    detail = next(s.detail for s in result.signals if s.signal == "macro_call")
    assert detail == "Dimension"


def test_a_filter_key_outside_metrics_is_still_a_macro_call():
    """The narrowing is anchored to ``metrics[...]``, where MetricFlow's
    filter grammar is actually defined, not to the bare key name ``filter``
    (#445 review). ``models[].config.filter`` names no such grammar; dex does
    not know what it means and must not read it as a semantic reference."""

    content = """version: 2
models:
  - name: things
    config:
      filter: "{{ Dimension('not_metricflow') }}"
"""
    result = classify_content(content, path="models/things.yml")
    assert result.artifact_class is ArtifactClass.EXECUTABLE
    assert "macro_call" in signals(result)
    assert "semantic_ref" not in signals(result)


def test_a_grant_is_authority_bearing():
    result = classify_content(GRANTING, path="models/things.yml")
    assert result.artifact_class is ArtifactClass.AUTHORITY_BEARING
    assert "grants" in signals(result)


def test_execution_outranks_authority_and_both_stay_in_the_signals():
    both = (
        GRANTING
        + """
      post-hook: "select 1"
"""
    )
    result = classify_content(both, path="models/things.yml")
    assert result.artifact_class is ArtifactClass.EXECUTABLE
    assert {"grants", "post_hook"} <= signals(result)


@pytest.mark.parametrize(
    "content",
    [
        "a: [1, 2\n  b: :::\n",
        "\tthis is not: yaml: at: all\n",
    ],
)
def test_content_that_does_not_parse_is_unknown_never_declarative(content):
    result = classify_content(content, path="models/things.yml")
    assert result.artifact_class is ArtifactClass.UNKNOWN
    assert result.parsed is False


def test_an_empty_file_is_unknown_rather_than_declarative():
    """It declares nothing and runs nothing, so any verdict but unknown is a claim."""

    assert (
        classify_content("", path="models/x.yml").artifact_class
        is ArtifactClass.UNKNOWN
    )


def test_the_declared_kind_never_decides_the_verdict():
    """A caller-supplied kind that contradicts the content loses to the content."""

    # A hook filed as a plain schema.yml: the kind says declarative, the bytes do not.
    schema = classify_edit(
        PlanEdit(
            path="models/schema.yml",
            new_content=HOOKED,
            op=EditOp.UPSERT,
            kind=EditKind.SCHEMA_YML,
        )
    )
    assert schema.artifact_class is ArtifactClass.EXECUTABLE

    # And the reverse: a semantic_yml kind over content that is only declarations.
    semantic = classify_edit(
        PlanEdit(
            path="models/things.yml",
            new_content=DECLARATIVE_SEMANTIC,
            op=EditOp.UPSERT,
            kind=EditKind.SEMANTIC_YML,
        )
    )
    assert semantic.artifact_class is ArtifactClass.DECLARATIVE


def test_model_sql_is_executable_and_a_macro_call_is_named():
    plain = classify_content("select 1 as id\n", path="models/m.sql")
    assert plain.artifact_class is ArtifactClass.EXECUTABLE
    assert "sql_statement" in signals(plain)

    with_macro = classify_content(
        "select {{ my_macro('x') }} as id from {{ ref('other') }}\n",
        path="models/m.sql",
    )
    assert "macro_call" in signals(with_macro)
    detail = next(s.detail for s in with_macro.signals if s.signal == "macro_call")
    assert detail == "my_macro"


def test_a_seed_is_data():
    assert (
        classify_content("id,name\n1,a\n2,b\n", path="seeds/s.csv").artifact_class
        is ArtifactClass.DATA
    )


def test_a_ragged_file_is_unknown_rather_than_guessed_as_data():
    """Guessing data in the permissive direction is the one mistake to avoid.

    Classifying something dex failed to read as inert values is exactly what
    would let executable content through, so anything ragged falls to unknown.
    """

    assert (
        classify_content("id,name\n1\n2,b,c\n", path="seeds/s.csv").artifact_class
        is ArtifactClass.UNKNOWN
    )


def test_a_delete_is_classified_from_the_file_it_removes():
    edit = Edit(path="models/things.yml", op=EditOp.DELETE)

    from_disk = classify_edit(edit, existing_content=HOOKED)
    assert from_disk.artifact_class is ArtifactClass.EXECUTABLE
    assert from_disk.basis == "existing_content"

    # With nothing to read, the answer is absent rather than safe.
    blind = classify_edit(edit)
    assert blind.artifact_class is ArtifactClass.UNKNOWN
    assert blind.basis == "absent"


def test_a_python_model_is_executable():
    result = classify_content(
        "import pandas\n\n\ndef model(dbt, session):\n    return None\n",
        path="models/m.py",
    )
    assert result.artifact_class is ArtifactClass.EXECUTABLE
    assert "python_model" in signals(result)


def test_jinja_dex_cannot_read_is_a_signal_rather_than_a_silence():
    """A value dex cannot see is the same problem for a host as a macro call."""

    result = classify_content(
        'version: 2\nmodels:\n  - name: m\n    description: "{{ some_var }}"\n',
        path="models/m.yml",
    )
    assert result.artifact_class is ArtifactClass.EXECUTABLE
    assert "jinja_expression" in signals(result)


def test_classify_reads_a_stored_plan_and_an_edits_payload(dbt_project_dir: Path):
    repo = dbt_project_dir.parent
    with DexEngine.from_repo(str(repo)) as engine:
        stored = engine.plan(
            "a hooked schema",
            edits=[
                PlanEdit(
                    path="models/staging/hooked.yml",
                    new_content=HOOKED,
                    op=EditOp.UPSERT,
                    kind=EditKind.SCHEMA_YML,
                )
            ],
        )
        by_id = engine.classify(stored.plan_id)
        assert by_id.plan_id == stored.plan_id
        assert by_id.classifications[0]["artifact_class"] == "executable"

        # And without a plan at all, for a caller classifying before it plans.
        ad_hoc = engine.classify(
            edits=[
                PlanEdit(
                    path="models/staging/plain.yml",
                    new_content=DECLARATIVE_SEMANTIC,
                    op=EditOp.UPSERT,
                    kind=EditKind.SCHEMA_YML,
                )
            ]
        )
        assert "plan_id" not in ad_hoc.data()
        assert ad_hoc.classifications[0]["artifact_class"] == "declarative"
