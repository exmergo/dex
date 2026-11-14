"""How CI splits the eval harness's tests into groups that run as parallel jobs.

The eval counterpart of packages/dex-core/tests/ci_groups.py, and held the same
way: ci.yml builds its eval matrix from ``GROUPS`` (through
``scripts/check_test_groups.py matrix --suite evals``), and the same script
checks afterwards that the groups ran every eval, once each, with none skipped.
Stdlib only, because that script and the stdlib-only ``uvx pytest evals`` run
read this file without installing anything.

Unlike the engine's split, a group is cut by test, not by file: almost all of
the time is one file, ``test_metadata.py``, whose tests each build a dbt project
(none shares a module-scoped fixture, so nothing is built twice by cutting it).
Each prefix is a test node id relative to the repository root, at any depth: a
file, ``file::test_name``, or ``file::test_name[param-prefix``. A prefix ending
in a test name matches that test and its parameters, never a longer name it
happens to begin. The longest matching prefix decides, and the catch-all takes
every test no prefix names, so a new eval always lands somewhere and runs.

Rebalance by moving prefixes when one job becomes the long pole. A job is held
to five minutes, so each group is cut to at most about three minutes of tests.
The figures beside the groups below are seconds of test time on CI from the
2026-11-09 run (1,300s in total), measured with ``--junitxml``.
"""

from __future__ import annotations

CATCH_ALL = "rest"

_META = "evals/tests/test_metadata.py::"
_REFERENCE = _META + "test_every_reference_answer_scores_perfect"

GROUPS: dict[str, tuple[str, ...]] = {
    # Every task's reference answer, built with dbt: split by fixture, and the
    # first group also takes any task added later.
    "references-a": (_REFERENCE,),  # 153s
    "references-b": tuple(  # 182s
        f"{_REFERENCE}[{fixture}/"
        for fixture in (
            "attribution_population",
            "blocked_report",
            "composite_grain",
            "identifier_repair",
            "package_prerequisite",
            "parent_zero_children",
            "gated_fees",
            "incremental_watermark",
            "optional_fees",
            "trailing_window",
            "intended_repairs",
            "package_parent",
        )
    ),
    # Package and seed prerequisites: deps, overrides and seed repairs.
    "prerequisites": tuple(  # 137s
        _META + name
        for name in (
            "test_package_prerequisite_supported_flow",
            "test_seeded_prerequisite_data_repair_flow",
            "test_the_package_blocks_the_report_until_its_prerequisite_is_repaired",
            "test_an_edit_under_dbt_packages_passes_warm_and_fails_the_gate",
            "test_a_project_override_does_not_fulfil_a_request_to_modify_the_data",
            "test_changing_the_seed_fails_a_request_to_leave_the_data_alone",
        )
    ),
    # #1114: a refactor whose baseline a package prerequisite blocks, and #1161:
    # a declaration on an override of a documented package model.
    "prerequisite-refactors": tuple(  # 114s
        _META + name
        for name in (
            "test_package_blocked_refactor_flow",
            "test_the_refactor_without_its_prerequisite_fails_the_gate",
            "test_a_refactor_repaired_under_dbt_packages_fails_the_gate",
            "test_the_package_blocked_refactor_checks_catch_the_wrong_answers",
            "test_declaring_a_documented_override_is_refused_and_its_readers_are_not",
        )
    ),
    # Blocked deliverables, identifier repairs and holdouts.
    "blocked": tuple(  # 115s
        _META + name
        for name in (
            "test_a_blocked_deliverable_counts_only_where_allowed_and_reported",
            "test_a_blocked_deliverable_with_another_failure_is_not_allowed",
            "test_the_blocked_setup_fails_the_gate_until_the_prerequisite_is_repaired",
            "test_a_repair_that_drops_rows_fails_the_blocked_checks",
            "test_a_report_driven_from_the_child_passes_on_dev_and_fails_the_holdout",
            "test_the_broken_identifier_setup_fails_the_gate",
            "test_an_identifier_repair_that_drops_a_scheme_fails_the_checks",
            "test_a_write_to_the_vendors_source_fails_the_workspace_checks",
        )
    ),
    # The protected-report privacy cases.
    "protected": tuple(  # 84s
        _META + name
        for name in (
            "test_protected_",
            "test_a_correct_schema_cannot_buy_back_a_privacy_failure_in_the_cli",
            "test_explicit_redaction_is_required_only_for_the_redaction_task",
            "test_missing_transcript_is_unchecked_evidence_and_fails_privacy",
        )
    ),
    # Incremental watermarks and the unit tests and mutants that grade them.
    "incremental": tuple(  # 147s
        _META + name
        for name in (
            "test_the_strict_watermark_passes_a_full_build_and_misses_the_late_scans",
            "test_an_inclusive_watermark_with_no_unique_key_loads_scans_twice",
            "test_a_watermark_with_no_floor_loads_nothing_into_an_empty_table",
            "test_the_incremental_unit_test_is_graded_apart_from_the_rows",
            "test_a_weak_calendar_test_catches_none_of_the_planted_boundary_defects",
            "test_a_reference_catches_each_defect_by_its_unit_test",
            "test_a_defect_the_model_no_longer_contains_is_not_planted",
        )
    ),
    # Intended changes held to compare --expect, and the fix tasks' starts.
    "intended-changes": tuple(  # 143s
        _META + name
        for name in (
            "test_compare_expect_passes_the_repair_and_flags_its_over_broad_variant",
            "test_a_diagnosis_scores_on_an_unchanged_workspace",
            "test_a_fix_tasks_broken_start_fails_its_checks",
        )
    ),
    # Feature toggles: the gated fee columns on both values.
    "toggles": tuple(  # 116s
        _META + name
        for name in (
            "test_gating_only_the_order_models_leaves_fees_in_the_ledger_and_empty_report",
            "test_a_staging_fee_column_passed_through_off_is_caught",
            "test_another_gated_fee_producer_passes",
            "test_dropping_an_unrelated_column_fails_on_both_values",
            "test_dropping_the_fees_outright_fails_the_on_value",
            "test_a_default_of_off_changes_todays_deployments",
            "test_the_starting_ledger_leaks_its_fee_total_off",
            "test_the_toggle_check_judges_every_gated_shape_in_the_fixture",
        )
    ),
    # Change scope: roster filters, sandbox accounts and the filled-in days.
    "scope": tuple(  # 95s
        _META + name
        for name in (
            "test_a_narrow_request_that_also_filters_the_roster_fails_scope",
            "test_declaring_a_roster_filter_the_narrow_request_never_asked_for_still_fails",
            "test_a_broad_request_that_leaves_the_roster_fails_scope",
            "test_the_ambiguous_roster_passes_either_way_when_the_choice_is_recorded",
            "test_an_undocumented_roster_filter_fails_the_ambiguous_scope",
            "test_an_ambiguous_roster_exclusion_claimed_as_the_request_fails_scope",
            "test_filling_the_days_with_",
            "test_filling_the_days_and_",
            "test_the_starting_daily_visits_misses_the_quiet_days",
            "test_a_partial_directory_filter_fails_the_ambiguous_task",
            "test_a_directory_keeping_only_sandbox_accounts_fails_the_ambiguous_task",
            "test_a_shifted_range_that_drops_a_visit_day_fails_the_unchanged_check",
        )
    ),
    # Attribution, population and refactor checks, the transcript and report
    # rubric, the other eval files, and any new test.
    CATCH_ALL: (),  # 113s
}


def matches(nodeid: str, prefix: str) -> bool:
    """Whether ``prefix`` names ``nodeid`` (both relative to the repo root)."""

    if not nodeid.startswith(prefix):
        return False
    rest = nodeid[len(prefix) :]
    # A prefix ending mid-name (``test_protected_``) or at a separator names
    # everything under it; one ending at a whole name stops at that name.
    return not rest or prefix[-1] in "/[_" or rest[0] in "[:"


def group_of(nodeid: str) -> str:
    """The group the test ``nodeid`` (relative to the repo root) runs in."""

    found = [
        (len(prefix), name)
        for name, prefixes in GROUPS.items()
        for prefix in prefixes
        if matches(nodeid, prefix)
    ]
    return max(found)[1] if found else CATCH_ALL
