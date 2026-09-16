"""The one-time cumulative-ceiling decision (issue #283).

``budget.ceiling`` is refused when missing and ``budget.session_ceiling`` is only
warned about, and an unbounded day lives in the gap between those two defensible
positions. The warning was accurate and attached to the default state of every
new project, so it repeated on every billed command and stopped being read. These
pin the ask that turns the default into a decision: once per project, before any
spend, and never again once it is answered either way.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from exmergo_dex_core.config import (
    Budget,
    DexConfig,
    load_config,
    record_session_ceiling_decision,
    save_config,
)
from exmergo_dex_core.envelope import Paradigm
from exmergo_dex_core.errors import ConfigurationError
from exmergo_dex_core.guards.cost_guard import (
    ConfirmationRequiredError,
    CostGate,
    SessionCeilingDecisionRequiredError,
    no_session_ceiling_warning,
    suggested_session_ceiling,
)

CONFIG = Path(".dex/config.yml")


def _gate(**overrides) -> CostGate:
    """A gate on a billed connector that has passed every other check: confirmed,
    with a per-command ceiling, and a committed config to record an answer in. So
    the only thing left for it to refuse is the cumulative-ceiling decision."""

    kwargs = {
        "paradigm": Paradigm.BYTES_SCANNED,
        "ceiling": 1_000.0,
        "session_ceiling": None,
        "session_spent": 0.0,
        "confirmed": True,
        "connector": "bigquery",
        "command": "explore profile",
        "config_path": CONFIG,
    }
    kwargs.update(overrides)
    return CostGate(**kwargs)


# --- when the ask fires -----------------------------------------------------------


def test_the_first_billed_command_with_no_cumulative_ceiling_is_asked():
    gate = _gate()
    with pytest.raises(SessionCeilingDecisionRequiredError) as exc_info:
        gate.preflight_command(100.0)
    exc = exc_info.value
    # Five times this command's own estimate: the one number dex can honestly
    # reason from is what the caller's own work costs.
    assert exc.suggested == 500.0
    assert "--session-ceiling 500" in str(exc)
    assert "--no-session-ceiling" in str(exc)
    # A priced ask in the same channel as the cost handshake, so a host that
    # already handles one handles this.
    assert isinstance(exc, ConfirmationRequiredError)
    assert exc.cost.paradigm is Paradigm.BYTES_SCANNED
    assert exc.cost.estimate == 100.0


def test_the_ask_books_nothing_and_reaches_no_ledger():
    """Every refusal in ``preflight_command`` leaves before the reservation, and
    this one is last in that order, so an unanswered ask has to be as free as an
    unconfirmed handshake."""

    entries: list[dict] = []
    gate = _gate(session_ceiling=None, record=entries.append)
    with pytest.raises(SessionCeilingDecisionRequiredError):
        gate.preflight_command(100.0)
    assert entries == []


def test_the_cost_ask_comes_first_and_carries_the_suggestion_as_advice():
    """An unconfirmed command meets the cost handshake, not this one: that is
    where the caller learns the estimate the suggestion is derived from, so both
    asks can be answered in one re-run."""

    gate = _gate(confirmed=False)
    with pytest.raises(ConfirmationRequiredError) as exc_info:
        gate.preflight_command(100.0)
    assert not isinstance(exc_info.value, SessionCeilingDecisionRequiredError)
    assert gate.session_ceiling_pending(100.0)


# --- when it does not ------------------------------------------------------------


def test_a_project_with_a_ceiling_set_is_never_asked():
    gate = _gate(session_ceiling=10_000.0)
    assert gate.preflight_command(100.0).estimate == 100.0
    assert gate.warnings() == []


def test_a_recorded_decline_is_never_asked_again():
    gate = _gate(session_ceiling_declined=True)
    assert gate.preflight_command(100.0).estimate == 100.0


def test_a_declined_project_still_warns_that_the_day_is_unbounded():
    """The decline records a decision; it does not loosen anything. What the
    caller needs on a result is that nothing bounded the day, which is equally
    true whether the project decided that or was never asked."""

    plain = _gate().warnings()
    declined = _gate(session_ceiling_declined=True).warnings()
    assert len(plain) == 1 and len(declined) == 1
    assert "no cumulative spend ceiling" in declined[0]
    # Named, so a reader can tell a settled decision from a project that was
    # never asked, and so the sentence stops proposing one.
    assert "session_ceiling_declined" in declined[0]
    assert "session_ceiling_declined" not in plain[0]


def test_an_ad_hoc_read_with_no_committed_config_is_not_asked():
    """Nowhere to record an answer, so asking would ask a question the caller
    cannot answer. It keeps the warning it always had."""

    gate = _gate(config_path=None)
    assert gate.preflight_command(100.0).estimate == 100.0
    assert len(gate.warnings()) == 1


def test_a_free_connector_is_never_asked():
    gate = _gate(paradigm=Paradigm.FREE_LOCAL, ceiling=None)
    assert gate.preflight_command(0.0).paradigm is Paradigm.FREE_LOCAL
    assert gate.warnings() == []


def test_a_command_that_priced_nothing_is_not_asked():
    """A zero estimate bills nothing and gives the ask no starting point to
    name; the next command that prices work asks instead."""

    gate = _gate()
    assert gate.preflight_command(0.0).estimate == 0.0


def test_a_gate_built_without_the_two_inputs_behaves_as_it_did_before():
    """The defaults are the silent ones, so a host assembling its own gate is
    not asked a question it never opted into."""

    gate = CostGate(
        paradigm=Paradigm.BYTES_SCANNED,
        ceiling=1_000.0,
        session_ceiling=None,
        session_spent=0.0,
        confirmed=True,
        connector="bigquery",
    )
    assert gate.preflight_command(100.0).estimate == 100.0


# --- the suggestion --------------------------------------------------------------


def test_the_suggestion_is_five_times_the_estimate_rounded_up():
    assert suggested_session_ceiling(100.0) == 500.0
    assert suggested_session_ceiling(0.3) == 2.0


def test_the_warning_stays_silent_once_a_ceiling_is_set():
    assert no_session_ceiling_warning(Paradigm.BYTES_SCANNED, 10.0) == []
    assert no_session_ceiling_warning(Paradigm.FREE_LOCAL, None) == []


# --- recording the answer --------------------------------------------------------


def test_a_ceiling_is_written_into_the_committed_config(tmp_path):
    save_config(DexConfig(connector="bigquery", budget=Budget(ceiling=100.0)), tmp_path)
    config, diff = record_session_ceiling_decision(tmp_path, session_ceiling=500.0)
    assert config.budget.session_ceiling == 500.0
    assert load_config(tmp_path).budget.session_ceiling == 500.0
    # The per-command ceiling is a separate decision and is left alone.
    assert load_config(tmp_path).budget.ceiling == 100.0
    # Visible as a diff, the way `transform init` reports what it wrote.
    assert diff["path"] == ".dex/config.yml"
    assert diff["op"] == "update"
    assert "session_ceiling: 500.0" in diff["unified"]


def test_a_decline_is_written_as_a_decision_not_as_a_ceiling(tmp_path):
    save_config(DexConfig(connector="bigquery"), tmp_path)
    config, _diff = record_session_ceiling_decision(tmp_path, declined=True)
    assert config.budget.session_ceiling is None
    assert config.budget.session_ceiling_declined is True
    reloaded = load_config(tmp_path)
    assert reloaded.budget.session_ceiling_declined is True
    assert reloaded.budget.session_ceiling is None


def test_a_ceiling_supersedes_an_earlier_decline_and_drops_the_flag(tmp_path):
    save_config(DexConfig(connector="bigquery"), tmp_path)
    record_session_ceiling_decision(tmp_path, declined=True)
    record_session_ceiling_decision(tmp_path, session_ceiling=500.0)
    text = (tmp_path / CONFIG).read_text(encoding="utf-8")
    # Not written back as an explicit `false`, which would read as a second
    # setting in a committed file.
    assert "session_ceiling_declined" not in text
    assert load_config(tmp_path).budget.session_ceiling_declined is False


def test_only_the_budget_is_touched_in_a_config_that_holds_other_settings(tmp_path):
    save_config(
        DexConfig(connector="bigquery", dbt_target="dev", ranking_hints=["orders"]),
        tmp_path,
    )
    before = (tmp_path / CONFIG).read_text(encoding="utf-8")
    record_session_ceiling_decision(tmp_path, session_ceiling=500.0)
    reloaded = load_config(tmp_path)
    assert reloaded.dbt_target == "dev"
    assert reloaded.ranking_hints == ["orders"]
    after = (tmp_path / CONFIG).read_text(encoding="utf-8").splitlines()
    # Every line the file already had survives, in order: the amendment adds the
    # budget block and rewrites nothing else.
    remaining = list(after)
    for line in before.splitlines():
        assert line in remaining
        remaining = remaining[remaining.index(line) + 1 :]


def test_contradictory_answers_are_refused_rather_than_ranked(tmp_path):
    save_config(DexConfig(connector="bigquery"), tmp_path)
    with pytest.raises(ConfigurationError, match="not both and not neither"):
        record_session_ceiling_decision(tmp_path, session_ceiling=500.0, declined=True)
    with pytest.raises(ConfigurationError, match="not both and not neither"):
        record_session_ceiling_decision(tmp_path)


def test_a_non_positive_ceiling_is_refused_and_names_the_decline_flag(tmp_path):
    save_config(DexConfig(connector="bigquery"), tmp_path)
    with pytest.raises(ConfigurationError, match="--no-session-ceiling"):
        record_session_ceiling_decision(tmp_path, session_ceiling=0.0)


def test_an_answer_with_no_committed_config_is_refused_not_dropped(tmp_path):
    """Accepted-and-ignored is strictly worse than rejected: a caller who
    believes they bounded the day and in fact bounded nothing has lost exactly
    the guard they came here for."""

    with pytest.raises(ConfigurationError, match="to record a"):
        record_session_ceiling_decision(tmp_path, session_ceiling=500.0)


# --- what arms the ask -----------------------------------------------------------


def test_only_a_config_dex_loaded_from_a_file_arms_the_ask(tmp_path):
    """A host holding its own config object has already made the budget
    decisions the ask goes looking for, and the file at that root may not be the
    settings in play."""

    assert DexConfig(connector="bigquery").source_path is None
    save_config(DexConfig(connector="bigquery"), tmp_path)
    assert load_config(tmp_path).source_path == tmp_path / CONFIG


def test_the_source_path_is_never_written_back_into_the_file(tmp_path):
    save_config(DexConfig(connector="bigquery"), tmp_path)
    loaded = load_config(tmp_path)
    save_config(loaded, tmp_path)
    assert "source_path" not in (tmp_path / CONFIG).read_text(encoding="utf-8")
