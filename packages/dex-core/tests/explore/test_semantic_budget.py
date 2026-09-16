"""The catalog's payload budget: caps, elision accounting, `--search`, `--full`.

`explore semantic list` was the one explore command that budgeted nothing. It
serialized every element of every kind with no cap, no way to narrow it and
nothing saying anything had been left out, while five rounds of object-model work
widened every element in it.

These lock the two halves of the fix. The caps and their accounting are asserted
against the conformance reference layer, so the fixture is the same one the
backend contract runs on; the command-layer behavior is asserted through
`semantic_list` with a backend injected, because the order the three narrowing
flags compose in is the command's decision rather than a backend's.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from exmergo_dex_core import dbt_project as dbt_project_module
from exmergo_dex_core import envelope as env
from exmergo_dex_core.adapters.project import DbtProject
from exmergo_dex_core.config import DexConfig, QueryLimits
from exmergo_dex_core.engine import DexEngine
from exmergo_dex_core.explore import semantic as sem
from exmergo_dex_core.explore.semantic import (
    SemanticBackendError,
)
from exmergo_dex_core.explore.semantic import (
    commands as semantic_commands,
)
from exmergo_dex_core.explore.semantic.conformance import write_reference_project
from exmergo_dex_core.explore.semantic.local import LocalMetricFlowBackend
from exmergo_dex_core.results import to_envelope
from exmergo_dex_core.storage import MemoryStore


class _Layer(DbtProject):
    def __init__(self, root: Path, project: Path) -> None:
        super().__init__(root, project)

    def semantic_catalog(self):
        return dbt_project_module.semantic_catalog(
            self.project_dir, resolve_paths=lambda _text: None
        )


@pytest.fixture
def backend(tmp_path: Path) -> LocalMetricFlowBackend:
    """The reference layer read locally, with the join graph left unresolved.

    Unresolved on purpose: the budget is about how much of a catalog is emitted,
    and holding the resolver out keeps these assertions from moving whenever
    MetricFlow's resolution does.
    """

    project = write_reference_project(tmp_path)
    return LocalMetricFlowBackend(
        project,
        DexEngine(config=DexConfig(), store=MemoryStore()),
        "duckdb",
        QueryLimits(),
        _Layer(project.parent, project),
    )


@pytest.fixture
def listed(backend, monkeypatch):
    """`semantic_list` against that backend, as an envelope."""

    monkeypatch.setattr(sem, "resolve_backend", lambda *a, **k: backend)

    def run(**kwargs):
        return to_envelope(
            semantic_commands.semantic_list(
                DexEngine(config=DexConfig(), store=MemoryStore()), **kwargs
            )
        )

    return run


# ---- the accounting ---------------------------------------------------------


def test_a_complete_catalog_says_so_rather_than_leaving_it_to_be_inferred(listed):
    """A zeroed `elided` and empty cap notes are the positive statement.

    Without it, "no key for elisions" and "nothing was elided" are the same
    payload, so a caller holding a truncated catalog reads it as the layer. That
    is the failure this whole budget exists to prevent, and it is worth the
    handful of bytes the zeros cost.
    """

    payload = listed().data

    assert payload["elided"] == {
        "semantic_models": 0,
        "metrics": 0,
        "dimensions": 0,
        "entities": 0,
        "measures": 0,
        "dimensions_per_metric": 0,
    }
    assert not any("not listed" in note for note in payload["notes"])


def test_the_shipped_caps_do_not_bite_a_layer_of_the_measured_size(backend):
    """The defaults are calibrated so an ordinary layer comes back whole.

    A cap that trims an ordinary layer is worse than no cap. These are set from a
    layer of a dozen semantic models, a few dozen metrics and a hundred-odd
    groupable paths, and the reference layer is well inside that, so nothing here
    should be cut.
    """

    catalog = backend.list_definitions()

    assert set(catalog.capped().elided.values()) == {0}


def test_every_cut_is_counted_and_named(backend):
    capped = backend.list_definitions().capped(
        max_semantic_models=1,
        max_metrics=2,
        max_dimensions=3,
        max_entities=1,
        max_measures=1,
        max_dimensions_per_metric=1,
    )

    assert capped.elided["semantic_models"] == 1
    assert capped.elided["metrics"] == 3
    assert capped.elided["entities"] == 1
    assert capped.elided["dimensions"] > 0
    assert capped.elided["measures"] == 3
    assert capped.elided["dimensions_per_metric"] > 0
    # One note per non-empty cut, each naming the cap and a way past it.
    cuts = [note for note in capped.notes if "not listed" in note]
    assert len(cuts) == 6
    assert all("--full" in note for note in cuts)


def test_a_metric_carries_its_own_dimension_elision_and_only_when_there_is_one(
    backend,
):
    """The per-metric count is absent where nothing was cut, unlike the block-level
    one, because this field repeats once per metric and a catalog is the payload a
    budget exists to keep small. The layer-wide total is what makes the absence
    readable."""

    catalog = backend.list_definitions()
    payload = catalog.capped(max_dimensions_per_metric=1).to_data()

    assert all(metric["elided_dimension_count"] > 0 for metric in payload["metrics"])
    whole = catalog.capped().to_data()
    assert all("elided_dimension_count" not in m for m in whole["metrics"])
    assert whole["elided"]["dimensions_per_metric"] == 0


def test_full_lifts_the_caps_and_still_accounts_for_nothing_cut(backend):
    catalog = backend.list_definitions()

    whole = catalog.capped(full=True, max_metrics=1, max_dimensions_per_metric=1)

    assert len(whole.metrics) == len(catalog.metrics)
    assert set(whole.elided.values()) == {0}
    assert not any("not listed" in note for note in whole.notes)


# ---- search -----------------------------------------------------------------


def test_search_resolves_a_word_to_the_metrics_it_touches(listed):
    """A caller arriving at a layer knows a word rather than a name.

    `pricing_tier` is declared in the users model, so searching it keeps the
    metrics built on that model and drops the session ones, and the payload names
    the terms so the subset cannot be read as the layer.
    """

    payload = listed(search=["tier"]).data

    assert payload["searched_for"] == ["tier"]
    assert {m["name"] for m in payload["metrics"]} == {
        "users",
        "paying_users",
        "paying_user_share",
    }
    assert any("matched 3 of" in note for note in payload["notes"])


def test_search_reads_the_projects_own_words_not_only_names(listed):
    """The label and description are where a project says what a thing is, and
    they are the half a name search cannot reach.

    "conditional sum" appears in one measure's description and in no name
    anywhere, so a match on it can only have come through the project's prose,
    and what comes back is the metrics that read that measure.
    """

    payload = listed(search=["conditional sum"]).data

    assert {m["name"] for m in payload["metrics"]} == {
        "paying_users",
        "paying_user_share",
    }


def test_search_matches_an_element_by_the_metrics_that_reach_it(listed):
    """A matched dimension with no metric beside it answers nothing queryable.

    `channel` is a dimension of the sessions model and no metric is named for it,
    so the useful answer is the metrics that can be grouped by it.
    """

    payload = listed(search=["channel"]).data

    assert {m["name"] for m in payload["metrics"]} == {"sessions", "session_seconds"}


def test_several_terms_widen_rather_than_narrow(listed):
    """The union, unlike `--for-dimension`, where the intersection is the whole
    question. Two terms are two searches."""

    payload = listed(search=["tier", "channel"]).data

    assert {m["name"] for m in payload["metrics"]} == {
        "users",
        "paying_users",
        "paying_user_share",
        "sessions",
        "session_seconds",
    }


def test_a_term_that_matches_nothing_is_named_and_the_rest_still_answer(listed):
    """A note rather than a refusal, which is where this differs from a misspelled
    metric name: a substring matching nothing is an honest answer about the
    layer's words. Naming it is what keeps a typo from reading as a fact."""

    payload = listed(search=["tier", "revnue"]).data

    assert {m["name"] for m in payload["metrics"]} == {
        "users",
        "paying_users",
        "paying_user_share",
    }
    named = next(note for note in payload["notes"] if "revnue" in note)
    assert "tier still answered" in named


def test_a_search_that_matches_nothing_at_all_says_the_catalog_is_empty(listed):
    payload = listed(search=["revnue"]).data

    assert payload["metrics"] == []
    # And the note does not claim anything else answered, because nothing did.
    named = next(note for note in payload["notes"] if "revnue" in note)
    assert "still answered" not in named
    assert any("this catalog is empty" in note for note in payload["notes"])


def test_search_composes_with_metric_scoping_in_the_order_it_reads(listed):
    """`--metric x --search y` is "within x, the parts about y".

    Applied the other way round the two would fight: a search that dropped the
    named metric would leave a catalog scoped to a metric it does not contain.
    """

    payload = listed(metrics=["users", "sessions"], search=["tier"]).data

    assert payload["scoped_to"] == ["users", "sessions"]
    assert payload["searched_for"] == ["tier"]
    assert {m["name"] for m in payload["metrics"]} == {"users"}


def test_search_takes_a_comma_joined_list_like_every_other_name_flag(listed):
    payload = listed(search=["tier,channel"]).data

    assert payload["searched_for"] == ["tier", "channel"]


def test_an_unsearched_catalog_carries_no_search_key(listed):
    assert "searched_for" not in listed().data


# ---- the CLI wiring ---------------------------------------------------------


def test_the_two_flags_reach_the_command_and_are_refused_where_they_mean_nothing(
    backend, monkeypatch
):
    """Refused rather than accepted and dropped.

    A dropped flag is indistinguishable from an honored one right up until the
    answer is wrong, and both of these read as plausible on `values` and `query`:
    a caller could reasonably expect `--search` to find a dimension to ask for
    values of, or `--full` to lift a query's row cap.
    """

    from exmergo_dex_core.cli import _build_parser

    monkeypatch.setattr(sem, "resolve_backend", lambda *a, **k: backend)
    parser = _build_parser()

    args = parser.parse_args(
        ["explore", "semantic", "list", "--search", "tier", "--full"]
    )
    assert args.search == ["tier"]
    assert args.full is True

    engine = DexEngine(config=DexConfig(), store=MemoryStore())
    for mode, misplaced in (
        ("query", ["--search", "tier"]),
        ("values", ["--full"]),
        ("query", ["--search", "tier", "--full"]),
    ):
        argv = ["explore", "semantic", mode, "users", *misplaced]
        envelope = semantic_commands.cmd_semantic(parser.parse_args(argv), engine)

        assert envelope.status == env.Status.ERROR
        joined = " ".join(envelope.errors)
        assert all(flag in joined for flag in misplaced if flag.startswith("--"))
        flags = [flag for flag in misplaced if flag.startswith("--")]
        # Worded for one flag and for several, because a message that reads as a
        # template is one a caller stops reading.
        assert (
            "use it with `list`" if len(flags) == 1 else "use them with `list`"
        ) in (joined)


def test_the_engine_surface_carries_both_flags(backend, monkeypatch):
    """The CLI and `DexEngine` are two entry points to one command, and a flag
    that reaches only one of them is a divergence a library caller pays for."""

    monkeypatch.setattr(sem, "resolve_backend", lambda *a, **k: backend)
    engine = DexEngine(config=DexConfig(), store=MemoryStore())

    result = engine.semantic_list(search=["tier"], full=True)

    assert result.catalog.searched_for == ["tier"]
    assert set(result.catalog.elided.values()) == {0}


def test_an_unknown_metric_is_still_refused_by_name_beside_a_search(
    backend, monkeypatch
):
    monkeypatch.setattr(sem, "resolve_backend", lambda *a, **k: backend)
    engine = DexEngine(config=DexConfig(), store=MemoryStore())

    with pytest.raises(SemanticBackendError, match="no such metric"):
        engine.semantic_list(metrics=["userz"], search=["tier"])
