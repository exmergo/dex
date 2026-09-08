"""Dependency policy: install for an interactive user, refuse for a sandbox.

A build that reaches out to a package registry is right at a terminal and wrong
inside a sandbox with no network and a pinned dependency set. The refusal has to
name the packages, which a dbt failure to reach a registry does not, and it has
to fire before any subprocess runs, because that is the only place the name is
still available.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from exmergo_dex_core import MissingPackagesError
from exmergo_dex_core.transform.build import (
    DependencyPolicy,
    build,
    declared_packages,
    installed_packages,
    missing_packages,
    unverifiable_packages,
)

PACKAGES = """packages:
  - package: dbt-labs/dbt_utils
    version: 1.3.0
  - git: "https://github.com/org/repo.git"
    revision: 0.1.0
  - local: ../shared
"""


def test_every_declaration_shape_is_read(dbt_project_dir: Path):
    (dbt_project_dir / "packages.yml").write_text(PACKAGES, encoding="utf-8")
    declared = {p.name: p for p in declared_packages(dbt_project_dir)}

    assert declared["dbt-labs/dbt_utils"].source == "hub"
    assert declared["dbt-labs/dbt_utils"].version == "1.3.0"
    assert declared["dbt-labs/dbt_utils"].install_name == "dbt_utils"

    # A repository name is not a dbt package name, and dbt installs under the
    # latter, so this one is left unknown rather than guessed.
    assert declared["https://github.com/org/repo.git"].install_name is None
    assert declared["../shared"].install_name == "shared"


def test_dependencies_yml_is_read_too(dbt_project_dir: Path):
    (dbt_project_dir / "dependencies.yml").write_text(
        "packages:\n  - package: a/b\n    version: 1.0.0\n", encoding="utf-8"
    )
    assert [p.name for p in declared_packages(dbt_project_dir)] == ["a/b"]


def test_nothing_declared_means_nothing_missing(dbt_project_dir: Path):
    assert declared_packages(dbt_project_dir) == []
    assert missing_packages(dbt_project_dir) == []


def test_an_empty_dbt_packages_means_every_declaration_is_missing(
    dbt_project_dir: Path,
):
    (dbt_project_dir / "packages.yml").write_text(PACKAGES, encoding="utf-8")
    assert len(missing_packages(dbt_project_dir)) == 3


def test_an_installed_package_is_recognised_by_directory_or_declared_name(
    dbt_project_dir: Path,
):
    (dbt_project_dir / "packages.yml").write_text(
        "packages:\n  - package: dbt-labs/dbt_utils\n    version: 1.3.0\n",
        encoding="utf-8",
    )
    installed = dbt_project_dir / "dbt_packages" / "dbt_utils"
    installed.mkdir(parents=True)
    (installed / "dbt_project.yml").write_text(
        'name: dbt_utils\nversion: "1.3.0"\n', encoding="utf-8"
    )

    assert "dbt_utils" in installed_packages(dbt_project_dir)
    assert missing_packages(dbt_project_dir) == []


def test_a_declaration_dex_cannot_check_is_named_rather_than_guessed_missing(
    dbt_project_dir: Path,
):
    (dbt_project_dir / "packages.yml").write_text(PACKAGES, encoding="utf-8")
    something = dbt_project_dir / "dbt_packages" / "dbt_utils"
    something.mkdir(parents=True)

    # The git declaration cannot be checked by name, so it is not reported as
    # missing on a guess; `unverifiable_packages` is how a caller learns that.
    assert [p.name for p in missing_packages(dbt_project_dir)] == ["../shared"]
    assert [p.name for p in unverifiable_packages(dbt_project_dir)] == [
        "https://github.com/org/repo.git"
    ]


def test_the_refusal_names_the_packages_and_fires_before_any_subprocess(
    dbt_project_dir: Path,
):
    (dbt_project_dir / "packages.yml").write_text(PACKAGES, encoding="utf-8")

    def refuse(_argv):
        raise AssertionError("a subprocess ran before the dependency refusal")

    with pytest.raises(MissingPackagesError) as caught:
        build(
            dbt_project_dir,
            target="dev",
            runner=refuse,
            dependencies=DependencyPolicy.REFUSE,
        )

    message = str(caught.value)
    # `dbt_packages/` is empty, so every declaration is missing and every one is
    # named, the git URL included: there is nothing installed for it to be
    # unverifiable against.
    assert "dbt-labs/dbt_utils@1.3.0" in message
    assert "https://github.com/org/repo.git@0.1.0" in message
    assert "../shared" in message
    assert "transform deps" in message
    assert len(caught.value.packages) == 3
    assert caught.value.unverifiable == []


def test_the_refusal_is_a_prerequisite_a_caller_can_resolve(dbt_project_dir: Path):
    """`PrerequisiteError` is the one family a caller can act on automatically:
    run the named command and retry."""

    from exmergo_dex_core import PrerequisiteError
    from exmergo_dex_core.envelope import Reason, reason_for

    (dbt_project_dir / "packages.yml").write_text(PACKAGES, encoding="utf-8")
    with pytest.raises(PrerequisiteError) as caught:
        build(
            dbt_project_dir,
            target="dev",
            runner=lambda _argv: None,
            dependencies=DependencyPolicy.REFUSE,
        )
    assert reason_for(caught.value) is Reason.PREREQUISITE


def test_the_default_policy_still_installs(dbt_project_dir: Path, monkeypatch):
    """Nothing an interactive user sees changes: the first build still never
    fails on a `dbt deps` step the agent has no verb for."""

    (dbt_project_dir / "packages.yml").write_text(
        "packages:\n  - package: a/b\n    version: 1.0.0\n", encoding="utf-8"
    )
    ran: list[list[str]] = []

    class Completed:
        returncode = 0
        stdout = ""
        stderr = ""

    def runner(argv):
        ran.append(argv)
        if argv[1] == "deps":
            packages = dbt_project_dir / "dbt_packages" / "b"
            packages.mkdir(parents=True, exist_ok=True)
        return Completed()

    build(dbt_project_dir, target="dev", runner=runner)
    assert [argv[1] for argv in ran] == ["deps", "build"]


def test_the_refusal_does_not_fire_when_the_packages_are_present(
    dbt_project_dir: Path,
):
    (dbt_project_dir / "packages.yml").write_text(
        "packages:\n  - package: a/b\n    version: 1.0.0\n", encoding="utf-8"
    )
    (dbt_project_dir / "dbt_packages" / "b").mkdir(parents=True)

    class Completed:
        returncode = 0
        stdout = ""
        stderr = ""

    ran: list[list[str]] = []

    def runner(argv):
        ran.append(argv)
        return Completed()

    build(
        dbt_project_dir,
        target="dev",
        runner=runner,
        dependencies=DependencyPolicy.REFUSE,
    )
    assert [argv[1] for argv in ran] == ["build"]


def test_refuse_policy_does_not_assume_unknown_package_is_installed(dbt_project_dir):
    (dbt_project_dir / "packages.yml").write_text(
        'packages:\n  - git: "https://github.com/org/unknown.git"\n'
    )
    (dbt_project_dir / "dbt_packages/something").mkdir(parents=True)
    with pytest.raises(MissingPackagesError) as caught:
        build(
            dbt_project_dir,
            target="dev",
            dependencies=DependencyPolicy.REFUSE,
            runner=lambda _: pytest.fail("unverifiable package reached dbt"),
        )
    assert caught.value.packages == []
    assert len(caught.value.unverifiable) == 1
