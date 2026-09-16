"""The executable contract a semantic source is held to.

A semantic source supplies the layer: what it declares, what a caller can read
off it, and what a drift baseline compares against. It is not a transformation
project, so the project tiers in :mod:`.adapters.conformance` are the wrong
standard for one, and the shipped project contracts exist for formats that own a
model graph. What the two have in common is the *behaviour* around a layer, and
that half lives here so there is one implementation of each assertion rather than
two that drift.

```python
from exmergo_dex_core.semantic_source_conformance import (
    SemanticCatalogSourceContract,
    SemanticDeclarationContract,
    SemanticFingerprintContract,
    SemanticSourceFactoryContract,
)


class TestMySource(
    SemanticSourceFactoryContract,
    SemanticDeclarationContract,
    SemanticFingerprintContract,
    SemanticCatalogSourceContract,
):
    def build_source(self, context): ...
    def empty_source_context(self): ...
    def a_source_declaring_a_unique_key(self): ...
```

```
pip install "exmergo-dex-core[semantic-conformance]"
```

That extra is pytest and nothing else, and the floor is the point rather than a
coincidence: nothing here reaches the dialect engine, a warehouse client, or
either semantic extra. A packaging test holds it true.

**Four contracts, because a source may legitimately answer only some of them.**
:class:`SemanticSourceFactoryContract` is construction.
:class:`SemanticDeclarationContract` is the keys and joins a layer contributes to
exploration. :class:`SemanticFingerprintContract` is the reduction a drift
baseline stores. :class:`SemanticCatalogSourceContract` is the read view
`explore semantic list` returns. Declining one is an answer: a source with no
snapshot capability is complete rather than partial, and the command reports the
absence itself.

**The declaration hook is named rather than fixed**, through
:meth:`SemanticDeclarationContract.declarations_of`. A semantic source spells the
call ``declared_definitions()``; a transformation project spells it
``definitions()``, because for a project that same method is the tier-1 contract
and has been public since v1. One override reconciles them, which is what lets
the shipped project contracts run these exact assertions rather than a copy.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from .maintain.snapshot import Snapshot

if TYPE_CHECKING:
    from collections.abc import Mapping

    from .project_definitions import ProjectDefinitions
    from .semantic_source import SemanticSourceContext

__all__ = [
    "SemanticCatalogSourceContract",
    "SemanticDeclarationContract",
    "SemanticFingerprintContract",
    "SemanticSourceFactoryContract",
]


class SemanticDeclarationContract:
    """Opt-in: the keys and joins a layer declares actually arrive.

    A layer's declarations are worth reading because they reach the grain and
    relationship channels at confidence 1.0. A source that reads every name and
    drops the columns behind them contributes nothing there while appearing to
    work, so these assert the content rather than the shape.
    """

    def make_semantic_source(self) -> Any:  # pragma: no cover - subclass supplies
        """A source with nothing declared in it."""

        raise NotImplementedError

    def declarations_of(self, source: Any) -> ProjectDefinitions:
        """How this implementation is asked what it declares.

        Overridden by the project contracts, which spell it ``definitions()``.
        Semantic sources spell it ``declared_definitions()``, which is the
        default here.
        """

        return source.declared_definitions()

    def a_source_declaring_a_unique_key(self) -> tuple[Any, str, str]:
        """A source declaring one single-column unique key.

        Returns ``(source, model, column)``: the source, and what dex should see
        in it. Naming the expectation here rather than fixing it in the suite
        keeps your own vocabulary out of the assertion.
        """

        raise NotImplementedError

    def a_source_declaring_a_join(self) -> tuple[Any, str, str, str, str]:
        """A source declaring one single-column join between two models.

        Returns ``(source, model, column, to_model, to_column)``.
        """

        raise NotImplementedError

    def a_source_declaring_a_composite_key(
        self,
    ) -> tuple[Any, str, tuple[str, ...]] | None:
        """A grain needing more than one column, or ``None``.

        Returns ``(source, model, columns)`` with ``columns`` in declared order.

        **Declare more than two columns.** An implementation that handles a
        composite key by special-casing the pair satisfies a two-column fixture
        and fails a four-column one, so a pair cannot tell you what you came to
        find out.

        ``None`` skips, because a format may genuinely have no way to express a
        multi-column grain and is then differently shaped rather than incomplete.
        """

        return None

    def a_source_declaring_a_join_with_differently_named_sides(
        self,
    ) -> tuple[Any, str, str, str, str] | None:
        """A join whose two ends are spelled differently, or ``None``.

        Returns ``(source, model, column, to_model, to_column)``, and ``column``
        must not equal ``to_column``: this contract checks that and refuses a
        mirrored fixture, because a mirrored one cannot fail for the right
        reason. An implementation that reads one side and copies it onto the
        other satisfies a mirrored fixture exactly, and the defect ships.
        """

        return None

    def test_a_source_declaring_nothing_answers_without_raising(self) -> None:
        """Exploration runs against raw warehouses, so a layer that declares
        nothing is the ordinary case rather than an error state.

        A source that raised here would turn every unremarkable repository into
        an outage on a command that was only ever asking about tables.
        """

        declarations = self.declarations_of(self.make_semantic_source())

        assert declarations.declared_keys == []
        assert declarations.declared_composite_keys == []
        assert declarations.foreign_keys == []

    def test_reading_the_declarations_twice_agrees(self) -> None:
        """Rules out a read that consumes, which surfaces as a second command
        mysteriously seeing less than the first."""

        source = self.make_semantic_source()

        assert self.declarations_of(source) == self.declarations_of(source)

    def test_a_declared_unique_key_reaches_the_engine(self) -> None:
        source, model, column = self.a_source_declaring_a_unique_key()

        declared = self.declarations_of(source).declared_keys
        matching = [k for k in declared if k.model == model and k.column == column]

        assert matching, f"expected a declared key on {model}.{column}, got {declared}"
        assert matching[0].unique, (
            "a key declared unique must arrive with unique=True; a grain that "
            "arrives without it is read as an ordinary column"
        )

    def test_a_declared_join_carries_both_sides(self) -> None:
        """Both ends, because a half-read join is worse than an unread one.

        A join naming the wrong side sends the relationship detector looking for
        a key that is not there, and the finding it produces reads like a data
        problem rather than a misread declaration.
        """

        source, model, column, to_model, to_column = self.a_source_declaring_a_join()

        declared = self.declarations_of(source).foreign_keys
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

    def test_a_composite_grain_keeps_every_column_and_their_order(self) -> None:
        """A truncated composite key is silent, which is what makes it expensive.

        It does not read as a missing declaration. It reads as a declared grain
        that is simply narrower than the truth, so every check downstream runs
        against a grain the author never claimed and the findings look like data
        problems rather than a misread declaration.
        """

        supplied = self.a_source_declaring_a_composite_key()
        if supplied is None:
            pytest.skip(
                "a_source_declaring_a_composite_key() returned None: this "
                "implementation declares it cannot express a multi-column grain, "
                "so declared_composite_keys goes unchecked"
            )
        source, model, columns = supplied
        declarations = self.declarations_of(source)

        declared = declarations.declared_composite_keys
        matching = [k for k in declared if k.model == model]

        assert matching, (
            f"expected a composite key on {model}, got {declared}. A multi-column "
            "grain belongs in declared_composite_keys, not as several entries in "
            "declared_keys: those say each column is unique on its own, which is a "
            "different and much stronger claim"
        )
        assert tuple(matching[0].columns) == tuple(columns), (
            f"expected columns {tuple(columns)} in order, got "
            f"{tuple(matching[0].columns)}"
        )
        assert len(matching) == 1, (
            f"expected one composite key on {model}, got {len(matching)}: "
            f"{matching}. One declaration is one grain, and splitting it across "
            "entries makes the grain axis verify combinations the source never "
            "declared"
        )
        leaked = sorted(
            key.column
            for key in declarations.declared_keys
            if key.model == model
            and key.unique
            and key.column.lower() in {c.lower() for c in columns}
        )
        assert not leaked, (
            f"{model} reports {leaked} as unique on their own while also declaring "
            f"the composite grain {tuple(columns)}. The fixture's grain needs every "
            "one of those columns, so no single one of them is unique, and the "
            "stronger claim is the one that gets acted on: reconcile reads it as a "
            "grain the source already asserts and proposes edits against it"
        )

    def test_a_join_keeps_its_two_sides_apart_when_they_are_named_differently(
        self,
    ) -> None:
        """The case :meth:`test_a_declared_join_carries_both_sides` cannot reach.

        An implementation that mirrors the source column onto the target passes
        that one whenever the fixture's two ends share a name, and this is the
        assertion that separates them.
        """

        supplied = self.a_source_declaring_a_join_with_differently_named_sides()
        if supplied is None:
            pytest.skip(
                "a_source_declaring_a_join_with_differently_named_sides() returned "
                "None: a join whose ends are spelled differently goes unchecked, so "
                "an implementation that mirrors one side onto the other would pass "
                "this suite"
            )
        source, model, column, to_model, to_column = supplied

        assert column != to_column, (
            "this fixture has to name its two sides differently, or it cannot "
            "detect the mirroring it exists to detect"
        )

        declared = self.declarations_of(source).foreign_keys
        matching = [
            fk
            for fk in declared
            if fk.model == model and fk.column == column and fk.to_model == to_model
        ]

        assert matching, (
            f"expected a declared join from {model}.{column} to {to_model}, "
            f"got {declared}"
        )
        assert matching[0].to_column == to_column, (
            f"the join's target column arrived as {matching[0].to_column!r}, "
            f"expected {to_column!r}. Reading it as {column!r} is the mirroring "
            "failure: the far side is a column the target may not even have"
        )


class SemanticFingerprintContract:
    """Opt-in: the drift fingerprint keeps the column behind each field.

    The fingerprint's job is to make a change detectable, so it reduces the layer
    to a hash per definition plus the physical column behind each field. Nothing
    in a capability check looks at a populated one, so an implementation that
    reads every dimension and measure name and drops the column behind each
    passes everything else completely.

    That is not a hypothetical shape. ``SemanticModelDef`` keys every field to a
    warehouse column, and ``maintain``'s drift detector skips any field whose
    column is ``None``, correctly, because it cannot resolve what it was not
    given. So a layer mapped entirely to ``None`` validates, serializes, and
    compares clean forever: the check does not fail, it never runs, and a dropped
    warehouse column that should raise ``dangling_reference`` at high severity
    raises nothing. The absence is indistinguishable from agreement, which is the
    worst property a check can have.
    """

    def make_semantic_source(self) -> Any:  # pragma: no cover - subclass supplies
        """A source with nothing declared in it."""

        raise NotImplementedError

    def fingerprint_of(self, source: Any) -> Any:
        """How this implementation is asked for its drift fingerprint."""

        return source.semantic_layer()

    def test_a_fingerprint_is_produced_with_nothing_declared(self) -> None:
        """The baseline has to be capturable on the first run.

        That run is the one every later drift report compares against, so a
        source that only produces a fingerprint once something is declared can
        never be snapshotted at the moment somebody first reaches for it.
        """

        layer = self.fingerprint_of(self.make_semantic_source())

        assert layer.semantic_models == []
        assert layer.metrics == []

    def a_source_declaring_a_semantic_model(
        self,
    ) -> tuple[Any, str, Mapping[str, str | None], Mapping[str, str | None]]:
        """A source declaring one semantic model.

        Returns ``(source, name, dimensions, measures)``, where the two mappings
        are ``field name -> the warehouse column behind it``, exactly as you
        expect them to arrive on ``SemanticModelDef``.

        **Map a field to ``None`` when there is no bare column behind it**, and
        include at least one such field if the format can produce one: a computed
        field, or one whose expression is not a plain column name. ``None`` is
        the honest answer there and an invented column is not, because a consumer
        resolving column names would treat a fabricated one as a reference that no
        longer resolves.
        """

        raise NotImplementedError(
            "a semantic fingerprint subclass must implement "
            "a_source_declaring_a_semantic_model() -> (source, name, dimensions, "
            "measures), mapping each field to the warehouse column behind it"
        )

    def test_a_semantic_field_carries_the_column_behind_it(self) -> None:
        source, name, dimensions, measures = self.a_source_declaring_a_semantic_model()

        layer = self.fingerprint_of(source)
        matching = [m for m in layer.semantic_models if m.name == name]

        assert matching, (
            f"expected a semantic model named {name!r}, got "
            f"{[m.name for m in layer.semantic_models]}"
        )
        model = matching[0]
        assert dict(model.dimensions) == dict(dimensions), (
            "the dimension to column mapping did not survive. A layer whose columns "
            "are all None still validates and still compares clean, so the drift "
            "check simply never runs"
        )
        assert dict(model.measures) == dict(measures), (
            "the measure to column mapping did not survive; see above"
        )

    def test_a_categorical_dimension_maps_only_to_a_real_column(self) -> None:
        """``categorical_dimensions`` takes ``str``, not ``str | None``.

        So a field that is categorical *and* unresolved cannot be represented
        there, and the two properties have to stay independent: being categorical
        says how the field behaves, having a column says whether it can be
        checked. An implementation that collapses them either drops a categorical
        field that happens to lack a column, or supplies an invented column to
        keep it. Both are worse than leaving it out of this one mapping, which is
        what the typing asks for.
        """

        source, name, _, _ = self.a_source_declaring_a_semantic_model()

        model = next(
            m for m in self.fingerprint_of(source).semantic_models if m.name == name
        )

        columns = model.categorical_dimensions.values()
        assert all(isinstance(c, str) and c for c in columns), (
            "categorical_dimensions holds a null or empty column: its values are "
            f"required strings, got {model.categorical_dimensions!r}. Leave an "
            "unresolved categorical field out of this mapping rather than "
            "inventing a column to keep it in"
        )
        assert set(model.categorical_dimensions) <= set(model.dimensions), (
            "categorical_dimensions names a field that is not a dimension: "
            f"{sorted(set(model.categorical_dimensions) - set(model.dimensions))}"
        )

    def test_the_fingerprint_survives_a_snapshot_round_trip(self) -> None:
        """A fingerprint that cannot be persisted is not a baseline.

        The layer goes into a `Snapshot`, a store serializes it, and a later
        command loads it and diffs against it. A layer that cannot survive that
        trip fails on the *next* run rather than the one that produced it, which
        is the run whose output someone is reading.

        Equality after a JSON round trip specifically, not a deep copy: the
        in-memory store copies rather than serializing, and would hide the whole
        class of defect this catches, a value the source chose that the model
        accepts in Python and rejects on the way back.
        """

        source, _, _, _ = self.a_source_declaring_a_semantic_model()

        snap = Snapshot(
            created_at="2026-01-01T00:00:00+00:00",
            semantic_layer=self.fingerprint_of(source),
        )
        restored = Snapshot.model_validate_json(snap.model_dump_json())

        assert restored.semantic_layer == snap.semantic_layer


class SemanticCatalogSourceContract:
    """Opt-in: the read catalog keeps what the fingerprint drops.

    **Why this is a separate contract from** :class:`SemanticFingerprintContract`.
    That one checks the reduction a comparison needs: a hash, and the column
    behind each field. This checks the opposite reduction. `explore semantic list`
    reads the catalog to answer "what can I query, how, and what will the number
    mean", and every field that answers it is a field the fingerprint correctly
    throws away: element types, the author's own labels and descriptions, a
    measure's aggregation, a metric's composition, and the token a query actually
    groups by.

    An implementation can therefore pass the fingerprint contract in full and
    return a catalog of bare names, which reads to a caller as a layer nobody
    documented and reduces the discovery surface to the thing it exists to
    replace.

    **The entity assertion is the load-bearing one.** An entity's type is a
    property of the (entity, semantic model) declaration, not of the entity: it is
    primary in the model that keys it and foreign in every model that joins to it.
    An implementation that returns one record per entity has to pick, and
    whichever it picks is iteration order rather than a fact. Both of dex's own
    backends got this wrong, in opposite directions, on one identical layer.

    A source that has no entities at all satisfies these vacuously, which is
    correct: what is asserted is that a declared entity is shaped honestly, not
    that entities exist.
    """

    def make_semantic_source(self) -> Any:  # pragma: no cover - subclass supplies
        """A source with nothing declared in it."""

        raise NotImplementedError

    def a_source_declaring_a_semantic_model(
        self,
    ) -> tuple[Any, str, Mapping[str, str | None], Mapping[str, str | None]]:
        # pragma: no cover - subclass supplies
        raise NotImplementedError

    def catalog_of(self, source: Any) -> Any:
        """How this implementation is asked for its read catalog."""

        return source.semantic_catalog()

    def an_unreadable_semantic_source(self) -> Any | None:
        """A source whose documents genuinely cannot be read, or ``None``.

        ``None`` skips the degradation assertions, which is right for an
        implementation with no such state: a layer reduced from something already
        in memory cannot fail to parse. Override it wherever the source is
        file-backed, because a file is a thing that gets truncated, merged badly,
        and hand-edited, and that path is the one users actually reach.
        """

        return None

    def test_the_source_answers_a_catalog(self) -> None:
        from .semantic_source import SemanticCatalogSource

        source = self.make_semantic_source()
        assert isinstance(source, SemanticCatalogSource), (
            "semantic_catalog() is what `explore semantic list` reads, and it is "
            "checked structurally rather than declared, so a source missing the "
            "member is refused by name at the command rather than here"
        )

    def test_reading_the_catalog_twice_agrees(self) -> None:
        """Rules out a read that consumes, which surfaces as a second command
        mysteriously seeing less than the first."""

        source, name, _, _ = self.a_source_declaring_a_semantic_model()

        first = self.catalog_of(source)
        second = self.catalog_of(source)

        assert [m.name for m in first.semantic_models] == [
            m.name for m in second.semantic_models
        ]
        assert [m.name for m in first.metrics] == [m.name for m in second.metrics]
        assert any(m.name == name for m in first.semantic_models)

    def test_the_catalog_keeps_what_the_fingerprint_reduces_away(self) -> None:
        source, name, _, _ = self.a_source_declaring_a_semantic_model()
        catalog = self.catalog_of(source)

        assert [m.name for m in catalog.semantic_models], (
            "the catalog carries no semantic models. The layer's organizing unit "
            "is the semantic model, and a caller with none of them holds one "
            "undifferentiated list of dimension names"
        )
        assert any(m.name == name for m in catalog.semantic_models), (
            f"expected a semantic model named {name!r}, got "
            f"{[m.name for m in catalog.semantic_models]}"
        )
        assert all(d.type for d in catalog.dimensions), (
            "a dimension arrived with no type. Whether a dimension is time or "
            "categorical decides how it can be grouped by, so a caller that has "
            "to guess cannot build a valid query from this catalog"
        )
        assert all(m.agg for m in catalog.measures), (
            "a measure arrived with no aggregation. A measure without its "
            "aggregation cannot say what the number counts, which is the question "
            "the catalog exists to answer"
        )

    def test_an_entity_carries_a_declaration_per_semantic_model(self) -> None:
        source, _, _, _ = self.a_source_declaring_a_semantic_model()
        catalog = self.catalog_of(source)

        for entity in catalog.entities:
            assert entity.roles, (
                f"entity {entity.name!r} carries no declarations. Its `type` is "
                "then a single value with nothing behind it, and a value chosen "
                "per entity rather than per declaration is iteration order"
            )
            assert all(r.semantic_model for r in entity.roles), (
                f"a declaration of entity {entity.name!r} names no semantic "
                "model, so a caller cannot tell which model it is primary in"
            )
            declared = {r.type for r in entity.roles}
            expected = "primary" if "primary" in declared else next(iter(declared))
            assert entity.type == expected, (
                f"entity {entity.name!r} reports type {entity.type!r} while its "
                f"declarations say {sorted(declared)}. The single value is derived: "
                "primary wherever any declaration is primary"
            )

    def test_the_catalog_resolves_a_semantic_model_to_its_relation(self) -> None:
        """A semantic model that names no relation leaves the layer disconnected.

        The semantic catalog and the physical catalog are two views of one
        warehouse, and the relation on a semantic model is the whole join between
        them: it is what answers "which table is behind this metric", what lets
        ``explore map`` say an object is exposed, and what an entity's declared
        join is drawn between.

        Declining is still possible and is not this: a source that structurally
        cannot know the relation declares that gap, which is a different statement
        from one that could and did not.
        """

        source, name, _, _ = self.a_source_declaring_a_semantic_model()
        catalog = self.catalog_of(source)

        model = next(m for m in catalog.semantic_models if m.name == name)
        assert model.relation, (
            f"semantic model {name!r} resolves to no physical relation, so nothing "
            "connects this layer to the objects explore profiles and maps"
        )

    def test_an_element_carries_its_column_and_never_invents_one(self) -> None:
        """The same rule the fingerprint follows, on the read catalog.

        Two failures, opposite in direction and both live. A catalog whose columns
        are all absent cannot reach a physical column at all, which is what the
        PII gate needs to adjudicate a dimension from evidence rather than from
        the shape of its name. A catalog that invents a column out of an
        expression is worse: the gate then screens a column that is not the one
        behind the element and reports the verdict as evidence-backed.

        The fixture's own mapping is the oracle, including its ``None`` entries,
        so an implementation is held to what it said its layer contains rather
        than to a shape.
        """

        source, name, dimensions, measures = self.a_source_declaring_a_semantic_model()
        catalog = self.catalog_of(source)

        for element, expected in (
            (
                {
                    d.definition: d.column
                    for d in catalog.dimensions
                    if d.semantic_model == name
                },
                dimensions,
            ),
            (
                {
                    m.name: m.column
                    for m in catalog.measures
                    if m.semantic_model == name
                },
                measures,
            ),
        ):
            for field, column in expected.items():
                assert field in element, (
                    f"the catalog carries no entry for {field!r} on {name!r}; the "
                    "fixture declares it, so the read dropped it"
                )
                assert element[field] == column, (
                    f"{field!r} on {name!r} carries column {element[field]!r} where "
                    f"the fixture says {column!r}. None is the honest answer for a "
                    "computed expression: a column guessed out of one makes the PII "
                    "gate screen the wrong column and call it evidence"
                )

    def test_a_dimension_row_names_the_token_a_query_groups_by(self) -> None:
        """The catalog's ``name`` is a query token, not a display name.

        A caller builds a group-by out of it, so an implementation that returns
        the bare declared name where the layer requires a qualified path hands
        back a catalog whose every dimension fails at query time.
        """

        source, _, _, _ = self.a_source_declaring_a_semantic_model()
        catalog = self.catalog_of(source)

        listed = {d.name for d in catalog.dimensions}
        for metric in catalog.metrics:
            missing = sorted(set(metric.dimensions) - listed - {"metric_time"})
            assert not missing, (
                f"metric {metric.name!r} says it can be grouped by {missing}, and "
                "no dimension row carries those names. The two must be the same "
                "vocabulary or neither can be acted on"
            )

    def test_an_unreadable_source_says_why_rather_than_returning_an_empty_layer(
        self,
    ) -> None:
        """An empty layer and an unreadable one are different answers.

        Only one of them is fixed by editing a file, and a caller that cannot tell
        them apart reads "this layer declares nothing" and stops looking. The
        catalog channel may raise or it may answer with notes; what it may not do
        is answer empty and silent.
        """

        source = self.an_unreadable_semantic_source()
        if source is None:
            pytest.skip(
                "an_unreadable_semantic_source() returned None: this "
                "implementation declares it has no unreadable state, so the "
                "degradation path goes unchecked"
            )

        from .errors import DexError

        try:
            catalog = self.catalog_of(source)
        except DexError as exc:
            assert str(exc).strip(), (
                "the catalog refused an unreadable source with an empty message, "
                "so the caller learns that something is wrong and nothing about "
                "which file to open"
            )
            return

        assert catalog.notes, (
            "an unreadable source answered with a catalog carrying no notes. An "
            "empty result with no note is indistinguishable from a layer that "
            "genuinely declares nothing, and the two need different actions"
        )


class SemanticSourceFactoryContract:
    """Construction: cheap, coordinate-checked, and reading nothing.

    Separate from the capability contracts above for the reason the project seam
    separates them: what a source *does* once built and whether dex can build one
    from a committed config line are different questions, and a source handed to
    dex ready-made never goes through this half at all.
    """

    def build_source(self, context: SemanticSourceContext) -> Any:
        """Build the source from a context, the way the engine will."""

        raise NotImplementedError

    def empty_source_context(self) -> SemanticSourceContext:
        """Coordinates for a source with nothing declared in it."""

        raise NotImplementedError

    def absent_document_context(self) -> SemanticSourceContext | None:
        """Coordinates naming content that does not exist yet, or ``None``.

        ``None`` skips the read-nothing assertion. Override it for a file-backed
        source, where this is the case that matters: a typo in a semantic-layer
        path must not make ``explore map`` on a raw warehouse fail.
        """

        return None

    def test_the_factory_builds_a_source_that_answers_a_catalog(self) -> None:
        from .semantic_source import SemanticCatalogSource

        built = self.build_source(self.empty_source_context())

        assert isinstance(built, SemanticCatalogSource), (
            f"the factory built a {type(built).__name__}, which cannot answer a "
            "semantic catalog. Answering one is the floor for anything a vendor "
            "names, because every read downstream goes through it"
        )

    def test_an_option_the_source_cannot_honor_is_refused(self) -> None:
        """Accepted and ignored is indistinguishable from honored, right up until
        dex is reading different documents than the configuration named."""

        import dataclasses

        context = self.empty_source_context()
        probe = "dex_conformance_unknown_option"
        context = dataclasses.replace(
            context, options={**dict(context.options or {}), probe: "x"}
        )

        with pytest.raises(Exception) as caught:
            self.build_source(context)

        assert probe in str(caught.value), (
            "the refusal does not name the option it refused, so someone reading "
            f"it cannot tell which config line to delete. Got: {caught.value}"
        )

    def test_construction_reads_nothing(self) -> None:
        """Cheap by contract: dex builds a source per command, and a factory that
        parsed would charge every command that never looks at a semantic layer.

        Also why content that is *named but absent* is not a construction error:
        refusing here would make `explore map` on a raw warehouse fail over a typo
        in a semantic-layer path.
        """

        context = self.absent_document_context()
        if context is None:
            pytest.skip(
                "absent_document_context() returned None: this implementation "
                "declares it has no named-but-absent state, so construction is "
                "not checked for reading eagerly"
            )

        self.build_source(context)
