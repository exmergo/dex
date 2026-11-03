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

Measured on 2026-10-30, as a share of the whole suite's time: the mutation
tests ~38% (each runs dbt once per planted defect), the rest of transform ~26%
(real `dbt parse`/`build` subprocesses), and everything else ~36%, of which the
packaging tests' isolated installs are close to half.
"""

from __future__ import annotations

CATCH_ALL = "rest"

GROUPS: dict[str, tuple[str, ...]] = {
    "mutation": ("tests/transform/test_mutation_command.py",),
    "transform": ("tests/transform/",),
    # Everything else: the top-level files (packaging and the safety spine among
    # them), explore, maintain, adapters, the semantic-layer vendors, guards,
    # storage, demo, integration (skipped without credentials), and any
    # directory added after this was written.
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
