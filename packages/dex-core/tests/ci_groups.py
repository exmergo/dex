"""How CI splits the suite into groups that run as parallel jobs.

The one source of truth for the split: ci.yml builds its matrix from
``GROUPS`` (through ``scripts/check_test_groups.py matrix``) rather than listing
the groups itself, and the same script checks afterwards that the groups that
ran add up to the whole suite. Stdlib only, because that script reads this file
without installing the package.

A test belongs to a group by its file, never by its name, so a module-scoped
fixture is never built twice in two jobs. Each named group lists path prefixes
relative to ``packages/dex-core``, and the longest prefix that matches a file
decides its group, so a single slow file can be carved out of a directory
another group holds. The catch-all takes every file no prefix names. That is
what keeps a new test file from falling between the groups: it lands in the
catch-all until someone moves it, and it runs either way.

Rebalance by moving prefixes when one job becomes the long pole (each job
prints its slowest tests). One prefix may not sit in two groups, which
``tests/test_ci_groups.py`` holds.

The 2026-11-03 CI run spent 18m30s in transform, 9m22s in mutation and
6m24s in the catch-all. Real dbt subprocesses dominate transform and mutation;
installing dependencies took seconds. Keep those acceptance tests intact and
split by capability, with headroom for a five-minute job including setup.
Every job uploads JUnit timings alongside its coverage report for rebalancing.
"""

from __future__ import annotations

CATCH_ALL = "rest"

GROUPS: dict[str, tuple[str, ...]] = {
    "mutation-coverage": ("tests/transform/test_mutation_coverage.py",),
    "mutation-equivalence": ("tests/transform/test_mutation_equivalence.py",),
    "mutation-downstream": ("tests/transform/test_mutation_downstream.py",),
    "transform-build": (
        "tests/transform/test_build",
        "tests/transform/test_previous_build",
        "tests/transform/test_artifact_runtime",
    ),
    "transform-deps": (
        "tests/transform/test_cold_deps.py",
        "tests/transform/test_deps.py",
        "tests/transform/test_guarded_deps.py",
        "tests/transform/test_prerequisites.py",
        "tests/transform/test_packages_install_path_readers.py",
    ),
    "transform-edits": (
        "tests/transform/test_seed_",
        "tests/transform/test_snapshot_edit.py",
        "tests/transform/test_macro.py",
        "tests/transform/test_analysis_edit.py",
        "tests/transform/test_singular_test_edit.py",
        "tests/transform/test_control_flow_compile.py",
    ),
    # Multi-build retirement cases run apart from both refactor comparisons and
    # plan-only retirement contracts (assigned to contracts below).
    "transform-retirement": ("tests/transform/test_retirement",),
    "transform-refactor": (
        "tests/transform/test_refactor",
        "tests/transform/test_transform_compare.py",
        "tests/transform/test_multiset_compare.py",
    ),
    "transform-contracts": (
        "tests/transform/test_semantic",
        "tests/transform/test_native_",
        "tests/transform/test_parse.py",
        "tests/transform/test_declar",
        "tests/transform/test_contradictions.py",
        "tests/transform/test_retirement_plan.py",
    ),
    # Planning, attribution, scaffolding and the fast mutation
    # command doubles; also catches every new transform file by default.
    "transform": ("tests/transform/",),
    "explore-maintain": ("tests/explore/", "tests/maintain/", "tests/demo/"),
    "packaging": ("tests/test_packaging.py",),
    # Connectors, guards, storage, native semantic vendors, top-level contracts,
    # integration (skipped without credentials), and any new test directory.
    CATCH_ALL: (),
}


def group_of(path: str) -> str:
    """The group the test file at ``path`` (relative, ``/``-separated) runs in."""

    matches = [
        (len(prefix), name)
        for name, prefixes in GROUPS.items()
        for prefix in prefixes
        if path.startswith(prefix)
    ]
    return max(matches)[1] if matches else CATCH_ALL
