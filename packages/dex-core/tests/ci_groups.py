"""How CI splits the suite into groups that run as parallel jobs.

The one source of truth for the split: ci.yml builds its matrix from
``GROUPS`` (through ``scripts/check_test_groups.py matrix``) rather than listing
the groups itself, and the same script checks afterwards that the groups that
ran add up to the whole suite. Stdlib only, because that script reads this file
without installing the package.

A test belongs to a group by its file unless a prefix names the test itself.
Each named group lists prefixes relative to ``packages/dex-core``: a path
prefix (a directory, a file, or the start of a file name) takes every test in
the files it matches, and ``file::test_name`` takes that one test and its
parameters, never a longer name it happens to begin. The longest prefix that
matches decides, so a single slow file can be carved out of a directory another
group holds, and a slow test out of its file. Cut a file test by test only when
it holds no module- or class-scoped fixture, or that fixture is built once in
every group the file's tests run in. The catch-all takes every test no prefix
names. That is what keeps a new test file from falling between the groups: it
lands in the catch-all until someone moves it, and it runs either way.

Rebalance by moving prefixes when one job becomes the long pole (each job
prints its slowest tests and uploads JUnit timings). One prefix may not sit in
two groups, which ``tests/test_ci_groups.py`` holds.

Every group runs on every supported Python, and a job is held to five minutes
including setup (about a minute of checkout and sync), so each group is cut to
at most about three minutes of tests on the slowest interpreter. The figures
beside the groups below are seconds of test time from the 2026-11-09 run, the
slowest of the four Pythons. Real dbt subprocesses dominate; keep those
acceptance tests intact and split them rather than trimming them.
"""

from __future__ import annotations

CATCH_ALL = "rest"

_T = "tests/transform/"


def _tests(file: str, *names: str) -> tuple[str, ...]:
    """Prefixes naming single tests of ``file`` (relative to ``tests/transform``)."""

    return tuple(f"{_T}{file}::{name}" for name in names)


GROUPS: dict[str, tuple[str, ...]] = {
    # Real mutation runs, one file each; coverage is split by test (229s).
    "mutation-coverage": (f"{_T}test_mutation_coverage.py",),  # 136s
    "mutation-coverage-strength": _tests(  # 93s
        "test_mutation_coverage.py",
        "test_a_stronger_suite_catches_more_planted_defects",
    ),
    "mutation-equivalence": (f"{_T}test_mutation_equivalence.py",),  # 168s
    "mutation-downstream": (f"{_T}test_mutation_downstream.py",),  # 192s
    "mutation-incremental": (f"{_T}test_mutation_incremental.py",),  # 103s
    "transform-build": (  # 148s
        f"{_T}test_build",
        f"{_T}test_previous_build",
        f"{_T}test_artifact_runtime",
        f"{_T}test_macro.py",
    ),
    # Cold reinstalls (261s in one file), split by test.
    "transform-deps": (  # 126s
        f"{_T}test_cold_deps.py",
        f"{_T}test_deps.py",
        f"{_T}test_guarded_deps.py",
        f"{_T}test_prerequisites.py",
        f"{_T}test_packages_install_path_readers.py",
    ),
    "transform-deps-cold": _tests(  # 138s
        "test_cold_deps.py",
        "test_an_edit_under_dbt_packages_passes_warm_and_is_lost_cold",
        "test_an_override_gives_way_to_a_repair_of_the_projects_own_data",
        "test_cold_artifacts_stay_apart_from_the_last_ordinary_build",
        "test_a_skipped_report_with_a_stale_relation_is_not_built",
        "test_a_plan_that_breaks_a_model_is_not_called_preexisting",
        "test_a_prose_population_is_named_apart_from_a_measured_one",
        "test_a_relative_local_package_keeps_the_lockfile_of_the_others",
    ),
    "transform-edits": (  # 172s
        f"{_T}test_seed_",
        f"{_T}test_snapshot_edit.py",
        f"{_T}test_analysis_edit.py",
        f"{_T}test_singular_test_edit.py",
    ),
    "transform-control-flow": (f"{_T}test_control_flow_compile.py",),  # 163s
    # Multi-build feature-flag cases, each real-dbt file in its own jobs.
    "transform-retirement": (f"{_T}test_retirement",),  # 110s
    "transform-retirement-intentions": _tests(  # 118s
        "test_retirement_build.py",
        "test_each_intention_verifies_from_the_same_project",
    ),
    "transform-retirement-scoped": (f"{_T}test_retirement_scoped.py",),  # 177s
    # Plan-time retirement contracts compile both branches (291s), split by test.
    "transform-retirement-plan": (f"{_T}test_retirement_plan.py",),  # 150s
    "transform-retirement-plan-b": _tests(  # 141s
        "test_retirement_plan.py",
        "test_the_mirror_failure_is_caught_too",
        "test_a_contract_naming_the_wrong_branch_is_caught",
        "test_a_join_kept_without_its_column_is_caught",
        "test_an_unread_cte_and_an_orphaned_model_are_named",
        "test_a_removed_column_still_read_downstream_is_named",
        "test_a_semantic_or_schema_reference_to_a_removed_column_is_named",
        "test_a_stale_plan_is_not_compared_and_a_conflict_is_not_overwritten",
    ),
    # Toggle builds (398s in one file), split by test.
    "transform-toggle": (f"{_T}test_toggle_build.py",),  # 132s
    "transform-toggle-compare": _tests(  # 118s
        "test_toggle_build.py",
        "test_compare_refuses_a_parent_that_moves_with_the_flag",
        "test_compare_refuses_a_parent_only_the_on_branch_reads",
    ),
    "transform-toggle-producers": _tests(  # 149s
        "test_toggle_build.py",
        "test_every_producer_gated_is_satisfied_and_builds_clean",
        "test_an_off_build_of_the_mart_only_plan_is_contradicted",
        "test_an_on_build_keeps_every_producer",
        "test_a_gated_select_list_a_macro_read_and_a_disabled_model",
    ),
    # Native model versions on real dbt: several cold builds and a promotion.
    "transform-versions": (f"{_T}test_model_versions_build.py",),  # 70s
    "transform-refactor": (  # 94s
        f"{_T}test_refactor",
        f"{_T}test_multiset_compare.py",
    ),
    # transform compare on real dbt (245s in one file), split by test.
    "transform-compare": (f"{_T}test_transform_compare.py",),  # 125s
    "transform-compare-b": _tests(  # 120s
        "test_transform_compare.py",
        "test_no_plan_id_names_the_plan_compared",
        "test_a_rename_needs_a_mapping_and_declares_itself_with_one",
        "test_an_accepted_type_change_still_compares_its_rows",
        "test_a_macro_edit_that_moves_an_untouched_parent_is_not_checked",
        "test_the_issues_join_extraction_is_caught_through_a_same_plan_intermediate",
        "test_a_positive_refactor_is_equivalent_on_the_compared_inputs",
        "test_a_rewritten_parent_is_inlined_on_both_sides",
        "test_a_key_gives_per_key_counts",
    ),
    "transform-compare-expect": (  # 126s
        f"{_T}test_transform_compare_expect.py",
        f"{_T}test_expected_change.py",
    ),
    "transform-contracts": (  # 115s
        f"{_T}test_semantic",
        f"{_T}test_native_",
        f"{_T}test_parse.py",
        f"{_T}test_declar",
        f"{_T}test_contradictions.py",
    ),
    "transform-frames": (  # 160s
        f"{_T}test_mutation_interval_frames.py",
        f"{_T}test_portable_plan.py",
    ),
    "transform-packages": (  # 133s
        f"{_T}test_package_override_introspection.py",
        f"{_T}test_package_documented_override.py",
        f"{_T}test_package_parent_build.py",
        f"{_T}test_init.py",
    ),
    "transform-scaffold": (  # 98s
        f"{_T}test_scaffold.py",
        f"{_T}test_unit_test_scaffold_incremental.py",
        f"{_T}test_unit_tests.py",
    ),
    # Planning, attribution and the fast mutation command doubles; also
    # catches every new transform file by default.
    "transform": (_T,),  # about 90s
    "explore-maintain": ("tests/explore/", "tests/maintain/", "tests/demo/"),  # 187s
    "packaging": ("tests/test_packaging.py",),  # 58s
    # Connectors, guards, storage, native semantic vendors, top-level contracts,
    # integration (skipped without credentials), and any new test directory.
    CATCH_ALL: (),  # 172s
}


def matches(nodeid: str, prefix: str) -> bool:
    """Whether ``prefix`` names ``nodeid`` (a test id or a bare file path)."""

    if not nodeid.startswith(prefix):
        return False
    if "::" not in prefix:
        return True
    # A test prefix names that test and its parameters, not a longer name.
    rest = nodeid[len(prefix) :]
    return not rest or rest[0] in "[:"


def group_of(nodeid: str) -> str:
    """The group the test ``nodeid`` (relative, ``/``-separated) runs in.

    A bare file path answers for the tests no ``file::test`` prefix carves out.
    """

    found = [
        (len(prefix), name)
        for name, prefixes in GROUPS.items()
        for prefix in prefixes
        if matches(nodeid, prefix)
    ]
    return max(found)[1] if found else CATCH_ALL
