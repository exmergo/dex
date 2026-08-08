"""The executable contract a project format has to satisfy, shipped for reuse.

The rules a format owes its callers are stated on the protocol members in
:mod:`.project`. This module is the same rules as assertions, packaged so a format
living outside this distribution can run them in its own test suite:

    from exmergo_dex_core.adapters.conformance import ExploreProjectContract

    class TestMyProject(ExploreProjectContract):
        def make_project(self):
            return MyProject(nothing_declared())

That is the whole integration. pytest collects the inherited ``test_*`` methods and
runs the contract against your format.

**Pick the class that matches the tier you implement**, mirroring the protocols
exactly: :class:`ExploreProjectContract` for a format that can state what it
declares, :class:`MaintainProjectContract` when it can also produce the two
snapshot layers. Subclassing a wider contract than your format implements is how
you find out you have not finished, so the narrow class is a feature rather than a
convenience.

**A project is a source, not a sink, and that shapes this suite.** The storage
contract can write through the protocol and read back, so it needs no fixtures from
the implementer. Nothing here can put a declaration *into* your format, so the
assertions that check content arrive have to be handed a project already in a known
state. That is what the hooks are for, and why the content half is a separate
mix-in: a format is worth reading before it is worth trusting to round-trip a key.

**The assertion that matters most is behavioural, not structural.**
``definitions()`` must not raise -- not on an absent project, not on an ambiguous
one, not on a source it cannot parse. Exploration runs against warehouses with no
project at all, so absence is an ordinary state, and a format that raises turns it
into an outage. A contract asserting only shapes cannot catch that, which is why
:meth:`ExploreProjectContract.make_unreadable_project` exists and why it is worth
overriding even though it defaults to skipping.

**If your format can be handed a declaration to read**, mix
:class:`DeclaringProjectContract` in as well. It checks that a declared key and a
declared join actually reach the engine, which is the whole reason a project is
read on the explore path: those two arrive through ``definitions()`` and through no
other channel.

**If dex is going to build your format rather than be handed one**, mix
:class:`ProjectFactoryContract` in front. Construction is a separate contract from
the tiers for the same reason it is separate in storage, and a format that passes
the behavioral suite can still be unreachable from configuration, so "the suite is
green" should mean correct *and* constructable.

This module imports pytest, so it is deliberately not imported by
:mod:`exmergo_dex_core.adapters`: a bare ``import exmergo_dex_core`` must not
require a test framework. Install the ``[project-conformance]`` extra to get what
running the suite needs.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from ..dbt_project import ProjectDefinitions
from ..maintain.snapshot import Snapshot
from .project import ExploreProject, ProjectContext, tier_of

if TYPE_CHECKING:
    from typing import Any

    from ..maintain.snapshot import SemanticLayer, TransformLayer

__all__ = [
    "DeclaringProjectContract",
    "ExploreProjectContract",
    "MaintainProjectContract",
    "ProjectFactoryContract",
]


class ExploreProjectContract:
    """Tier 1. Subclass and implement :meth:`make_project`.

    Every assertion here is format-independent: it asks what dex may assume about
    any project it is handed, never what a particular format looks like.
    """

    def make_project(self) -> ExploreProject:
        """A project of your format with nothing declared in it.

        The empty case rather than the interesting one, because it is the case dex
        actually meets most often: a repo with no project, a warehouse explored
        bare. If your format cannot represent "nothing declared", that is worth
        knowing before you go further -- dex reaches this state on every explore
        run that does not ask for a project.
        """

        raise NotImplementedError

    def make_unreadable_project(self) -> ExploreProject | None:
        """A project whose source your format cannot parse, or ``None``.

        Override this. It is the hook behind the contract's most valuable
        assertion, and it defaults to ``None`` -- which skips those assertions with
        a message saying so -- only because a format may genuinely have no
        unparseable state: one reduced from an object already in memory cannot be
        malformed the way a file can.

        A default that skips is a compromise, and it is the right one here: making
        it mandatory would lock out a format for which the state does not exist,
        and the skip is explicit in the report rather than silent.
        """

        return None

    # --- what dex may assume of any project -----------------------------------

    def test_satisfies_the_explore_tier(self) -> None:
        assert tier_of(self.make_project()) >= 1

    def test_names_its_format_and_not_the_instance(self) -> None:
        """``name`` identifies the format, so two instances agree on it.

        A name that varies per instance is a name a registry cannot resolve, and
        the mistake is easy to make by deriving it from a path or an id.
        """

        first = self.make_project()
        second = self.make_project()
        assert first.name
        assert first.name == second.name

    def test_an_empty_project_declares_nothing_without_raising(self) -> None:
        definitions = self.make_project().definitions()

        assert isinstance(definitions, ProjectDefinitions)
        assert definitions.declared_keys == []
        assert definitions.declared_composite_keys == []
        assert definitions.foreign_keys == []

    def test_definitions_is_repeatable(self) -> None:
        """Two reads of an unchanged project agree.

        Not a caching requirement -- a format may re-read its source every call.
        What it rules out is a read that consumes something, which is a real
        failure mode for a format reduced from a stream or an iterator, and one
        that surfaces as a second command mysteriously seeing less than the first.
        """

        project = self.make_project()

        assert project.definitions() == project.definitions()

    def test_an_unreadable_project_does_not_raise(self) -> None:
        project = self.make_unreadable_project()
        if project is None:
            pytest.skip(
                "make_unreadable_project() returned None: this format declares it "
                "has no unparseable state"
            )

        definitions = project.definitions()

        assert isinstance(definitions, ProjectDefinitions)

    def test_an_unreadable_project_says_why_rather_than_going_quiet(self) -> None:
        """Degrading quietly is not the same as degrading silently.

        An empty result with no note is indistinguishable from a project that
        genuinely declares nothing, so the operator cannot tell a typo in their
        source from a correct empty read. ``notes`` is where that difference goes.
        """

        project = self.make_unreadable_project()
        if project is None:
            pytest.skip(
                "make_unreadable_project() returned None: this format declares it "
                "has no unparseable state"
            )

        assert project.definitions().notes


class DeclaringProjectContract:
    """Opt-in: the declarations dex reads a project *for* actually arrive.

    Mix in beside the contract for your tier::

        class TestMyProject(DeclaringProjectContract, ExploreProjectContract):
            def make_project(self): ...
            def a_project_declaring_a_unique_key(self): ...
            def a_project_declaring_a_join(self): ...

    Separate from the tier contract because the two answer different questions. The
    tier contract asks whether dex can safely call your format at all; this asks
    whether reading it was worth doing.
    """

    def a_project_declaring_a_unique_key(self) -> tuple[ExploreProject, str, str]:
        """A project declaring one single-column unique key.

        Returns ``(project, model, column)`` -- the project, and what dex should
        see in it. Naming the expectation here rather than fixing it in the suite
        keeps your format's own vocabulary out of the assertion: whatever you call
        the model, the contract checks that dex learns that name.
        """

        raise NotImplementedError

    def a_project_declaring_a_join(
        self,
    ) -> tuple[ExploreProject, str, str, str, str]:
        """A project declaring one single-column join between two models.

        Returns ``(project, model, column, to_model, to_column)``.
        """

        raise NotImplementedError

    def test_a_declared_unique_key_reaches_the_engine(self) -> None:
        project, model, column = self.a_project_declaring_a_unique_key()

        declared = project.definitions().declared_keys
        matching = [k for k in declared if k.model == model and k.column == column]

        assert matching, f"expected a declared key on {model}.{column}, got {declared}"
        assert matching[0].unique, (
            "a key declared unique must arrive with unique=True; a grain that "
            "arrives without it is read as an ordinary column"
        )

    def test_a_declared_join_carries_both_sides(self) -> None:
        """Both ends, because a half-read join is worse than an unread one.

        A join naming the wrong side sends the relationship detector looking for a
        key that is not there, and the finding it produces reads like a data
        problem rather than a misread declaration.
        """

        project, model, column, to_model, to_column = self.a_project_declaring_a_join()

        declared = project.definitions().foreign_keys
        matching = [
            fk
            for fk in declared
            if fk.model == model
            and fk.column == column
            and fk.to_model == to_model
            and fk.to_column == to_column
        ]

        assert matching, (
            f"expected a declared join {model}.{column} -> {to_model}.{to_column}, "
            f"got {declared}"
        )


class MaintainProjectContract(ExploreProjectContract):
    """Tier 2. Inherits every tier-1 assertion.

    The layers are what a snapshot is taken of, so a format reaching this tier can
    be a drift baseline. What is asserted here is deliberately thin: the layers'
    contents are the format's business, and the one cross-tier rule worth holding
    is that the two channels agree about which models exist.
    """

    def test_satisfies_the_maintain_tier(self) -> None:
        assert tier_of(self.make_project()) >= 2

    def test_both_layers_are_produced_with_nothing_declared(self) -> None:
        """Tier 2 is reached with nothing declared, not only with content.

        A format that only produces layers once something is declared cannot be
        snapshotted on its first run, which is the run that establishes the
        baseline every later drift report compares against.

        Note what is *not* asserted: that the layers are empty. A project can build
        models while declaring nothing about them -- that is the ordinary state of
        an uninstrumented project -- so emptiness here would be a claim about the
        fixture rather than about the format. What has to hold is that both
        channels answer at all.
        """

        project = self.make_project()

        transform: TransformLayer = project.transform_layer()
        semantic: SemanticLayer = project.semantic_layer()

        assert isinstance(transform.models, list)
        assert isinstance(transform.model_refs, dict)
        assert semantic.semantic_models == []
        assert semantic.metrics == []

    def test_the_two_channels_agree_about_which_models_exist(self) -> None:
        """A model in the transform layer is a model ``definitions()`` built.

        The layers and the definitions are read by different commands, and a
        format producing them from two different traversals can disagree. Drift
        then reports a model as added or orphaned on the strength of which channel
        happened to be read.
        """

        project = self.make_project()

        layer_models = set(project.transform_layer().models)
        built = {name.lower() for name in project.definitions().built_relation_names}

        assert {m.lower() for m in layer_models} <= built or not built, (
            f"transform_layer() reports models {sorted(layer_models)} that "
            f"definitions() does not list as built: {sorted(built)}"
        )

    def test_the_layers_survive_a_snapshot_round_trip(self) -> None:
        """A tier-2 format's layers can be persisted as a baseline and read back.

        This is what reaching tier 2 buys, so it is worth asserting rather than
        assuming: the layers go into a `Snapshot`, a store serializes it, and a
        later command loads it and diffs against it. A layer that cannot survive
        that trip is not a baseline, and the failure would surface on the *next*
        run rather than the one that produced it.

        The assertion is equality after a JSON round trip specifically, not a
        deep copy, because the in-memory store copies rather than serializing and
        would hide the whole class of defect this catches: a value the format
        chose that the model accepts in Python and rejects on the way back.
        """

        project = self.make_project()

        snap = Snapshot(
            created_at="2026-01-01T00:00:00+00:00",
            transform_layer=project.transform_layer(),
            semantic_layer=project.semantic_layer(),
        )
        restored = Snapshot.model_validate_json(snap.model_dump_json())

        assert restored.transform_layer == snap.transform_layer
        assert restored.semantic_layer == snap.semantic_layer


class ProjectFactoryContract:
    """The construction half, for a format dex builds rather than receives.

    Mix it in front of the contract for your tier, and the whole behavioral suite
    runs against projects built the way dex builds them::

        class TestMyProject(ProjectFactoryContract, MaintainProjectContract):
            tier = MaintainProject

            def build(self, context):
                return my_project_factory(context)

            def empty_context(self):
                return ProjectContext(options={"graph": "empty"})

    Two hooks rather than one factory attribute, because a plain function assigned
    to a class attribute binds as a method and would arrive with ``self`` in front
    of the context, which is a confusing failure to meet on your first run.

    ``empty_context`` is where your format says what it is keyed by. Build the
    context the way a real configuration entry would produce it: a
    directory-keyed format sets ``repo_root`` and ``project_dir``, a format
    reduced from something already running sets neither and reads ``options``.
    Return the context that reaches the project :meth:`make_project` returns, so
    the behavioral assertions and the construction assertions describe the same
    project.
    """

    #: The tier :meth:`build` promises to return. Widen it to ``MaintainProject``
    #: or ``EditableProject`` alongside the matching contract class; the default is
    #: the floor every format meets.
    tier: Any = ExploreProject

    def build(self, context: ProjectContext) -> Any:
        """Your factory, called with a context. One line in most formats."""

        raise NotImplementedError(
            "a factory conformance subclass must implement "
            "build(context) -> project, calling the factory under test"
        )

    def empty_context(self) -> ProjectContext:
        """The context that builds a project with nothing declared in it."""

        raise NotImplementedError(
            "a factory conformance subclass must implement "
            "empty_context() -> ProjectContext, keying the project the way a "
            "real configuration entry would"
        )

    def make_project(self) -> ExploreProject:
        return self.build(self.empty_context())

    def test_the_factory_builds_a_project_of_the_declared_tier(self) -> None:
        built = self.build(self.empty_context())
        assert isinstance(built, self.tier), (
            f"the factory returned {type(built).__name__}, which does not satisfy "
            f"{self.tier.__name__}. A format that passes the behavioral suite and "
            "fails here is unusable from configuration: dex builds it, checks the "
            "tier the command needs, and refuses. Check that build() returns the "
            "project rather than a class, a coroutine, or a wrapper"
        )

    def test_an_option_the_format_cannot_honor_is_refused(self) -> None:
        """Accepted-and-ignored is worse than refused, and this is where it starts.

        ``options`` reaches your factory verbatim and dex never validates a key,
        so a factory that ignores what it does not recognize turns a typo into a
        project silently read from somewhere other than where the configuration
        said. The failure surfaces as drift nobody can reproduce, arbitrarily far
        from the config line that caused it.

        The key below is deliberately one no format would define. If yours has a
        reason to accept unknown keys, override this with the assertion that is
        true for your format rather than deleting it.
        """

        context = self.empty_context()
        polluted = ProjectContext(
            repo_root=context.repo_root,
            project_dir=context.project_dir,
            options={**context.options, "dex_conformance_unknown_option": "x"},
        )

        with pytest.raises(Exception) as refusal:
            self.build(polluted)

        assert "dex_conformance_unknown_option" in str(refusal.value), (
            "the factory refused, but the message does not name the option it "
            "refused. A reader has to be able to find the line to delete"
        )
