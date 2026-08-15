"""The refusals a programmatic caller can catch, rooted in one base class.

The CLI hides refusals behind a catch-all that renders an error envelope, so it
never needed a hierarchy. A library consumer cannot do that: it has to map a
refusal onto its own response, and catching ``Exception`` at an API boundary
swallows real bugs alongside deliberate refusals. So everything dex raises on
purpose descends from :class:`DexError`, and the families below are the
distinctions a host actually branches on.

    from exmergo_dex_core import ConfigurationError, DexError, RequestError

    try:
        result = engine.query(sql)
    except RequestError as bad_input:      # the call named something unusable
        return http(400, str(bad_input))
    except ConfigurationError as misbuilt:  # the engine was built without something
        return http(500, str(misbuilt))
    except DexError as refused:             # a guard said no, deliberately
        return http(422, str(refused))

Exception classes stay next to the code that raises them (a credential refusal
lives in ``connect``, a firewall refusal in ``guards.query_firewall``); this
module holds only the shared roots they inherit from, so importing it pulls in
nothing.

**Why the two families below also inherit ``ValueError``.** Both raised a bare
``ValueError`` before they were typed. A consumer written against that API keeps
working, which is the whole point of adding the type rather than trading one
breaking change for another. Do not remove the second base thinking it is
redundant.
"""

from __future__ import annotations


class DexError(Exception):
    """Base for every refusal dex raises deliberately.

    Deliberately is the operative word: a caller catching this is catching
    "dex declined, and the message says how to fix it", never an internal bug.
    Anything dex did not intend surfaces as its own type and should not be
    caught here.
    """


class ConfigurationError(DexError, ValueError):
    """The engine was built without something it needs, or with a contradiction.

    Missing input rather than bad input: no connector selected, no repo root
    where a dbt project was required, no store where a metered connector needed
    a ledger, a connection injected for a connector that cannot take one. A host
    seeing this has a wiring bug in how it constructed the engine, not a bad
    request from its user.
    """


class RequestError(DexError, ValueError):
    """The call named something this engine cannot resolve or cannot use.

    Bad input rather than missing input: an object that is not in the cache, an
    identifier that matches several relations, a clustering feature that is not
    numeric. A host seeing this can usually surface the message to whoever made
    the request, because the message names what to pass instead.
    """


class PrerequisiteError(DexError):
    """A prior dex command has not been run, and this one needs what it produces.

    Neither a bad request nor a broken engine: the call is well formed and the
    engine is wired correctly, some state simply does not exist yet, and a named
    command creates it. That makes this the one refusal family a caller can
    resolve automatically, by running the command the message names and retrying,
    which is why it is worth telling apart from a failure.

    The message always names the command rather than a file, because where state
    lives is the store's business: on the filesystem backend it is a file, in a
    hosted deployment it is a row, and "run `explore map` first" is the fix
    either way.
    """


class ProjectError(DexError):
    """A project could not be read, and the format says why.

    The project seam is tiered, and the two tiers make opposite promises.
    ``definitions()`` must never raise, because exploration runs against
    warehouses with no project at all. ``transform_layer()`` and
    ``semantic_layer()`` presume a project that loads, because an empty layer
    compared against an empty layer reads as "no drift" rather than "this could
    not be checked". This is what the second pair raises when that presumption
    fails, and it is the type ``maintain`` catches to degrade.

    It exists as a root rather than as one format's exception because the
    commands that catch it are format-neutral: they hold whatever project was
    named in configuration, and catching the dbt format's own error there would
    let a second format's failure through to a traceback. Every format raises
    this or a subclass of it; ``DbtProjectError`` is the shipped one.
    """


class ConnectorError(DexError):
    """A warehouse connection could not be established.

    The family a caller retries or backs off on, as distinct from
    :class:`ConfigurationError` (which retrying cannot fix) and
    :class:`CredentialDiscoveryError` (a credential that is absent rather than a
    connection that failed). Each connector raises its own subclass carrying its
    own vocabulary; catch this when what matters is that the warehouse is not
    reachable and not which warehouse it was.

    No message in this family ever carries a credential value.
    """


class WarehouseQueryError(DexError):
    """A statement dex built ran against the warehouse and the server refused it.

    Neither a refusal dex made nor a bug in dex: the guards cleared the
    statement, the connection was open, and the server answered with an error.
    For an engine whose whole job is issuing SQL to somebody else's warehouse
    that is a first-class outcome, so it classifies as ``execution_failure``
    (the operation ran and failed) instead of falling through ``reason_for``
    to ``internal``, which says "not a deliberate dex refusal" and sends
    whoever reads it looking for a crash. Every adapter raises this for a
    server-side statement failure it cannot translate into something more
    specific (a budget-derived timeout, a read-only violation), so a caller
    branching on the reason gets the same answer from every connector.

    ``detail`` is the server's own words, which is the whole diagnostic value:
    the adapter trims them to one line and caps their length before they get
    here, because a driver will happily append the statement, a caret diagram,
    or its entire error payload. They can still quote the fragment of a value
    that offended the server (``Invalid digit, Value 'p'``), the same way an
    agent SQL failure's message already does; that is a diagnostic the server
    volunteered, not a profile of the column, and suppressing it is what made
    this class of failure undiagnosable in the first place.

    The adapter knows the server's words but not what the statement was for;
    only the caller that built it knows which object it was reading. So the
    object is attached afterwards, by :meth:`naming`, rather than threaded
    down through every adapter's execution seam.

    It lives here rather than beside the code that raises it, which is this
    module's usual rule, because six adapters raise this one class (there is no
    "the code that raises it") and because ``envelope._reason_overrides`` has to
    classify it from the tier of imports that works on an install with no
    connector extra at all.
    """

    def __init__(self, detail: str, *, reading: str | None = None):
        self.detail = detail
        self.reading = reading
        super().__init__(self._message())

    def _message(self) -> str:
        about = f" while reading {self.reading}" if self.reading else ""
        return f"the warehouse refused a statement dex built{about}: {self.detail}"

    def naming(self, identifier: str) -> WarehouseQueryError:
        """This failure with the object it was reading named.

        The first name wins: the innermost caller that knows one is the most
        specific, so an outer handler re-naming it cannot make it vaguer.
        """

        if self.reading is not None:
            return self
        return WarehouseQueryError(self.detail, reading=identifier)


class NoConnectorSelectedError(ConfigurationError):
    """Nothing named a connector: not a flag, not a path, not a config file.

    Its own class because it is the refusal a host hits first and the one whose
    fix is most often a deployment change rather than a code change.
    """


class RepoRootRequiredError(ConfigurationError):
    """An operation needed the dbt project, and the engine was never told where
    it lives.

    The dbt project is a git-reviewable filesystem artifact by design and stays
    one, so this refusal is structural: the whole transform surface and the
    project-reading parts of explore are unavailable to an engine built without
    a ``repo_root``, and no storage backend changes that.
    """


class StoreRequiredError(ConfigurationError):
    """A metered connector was opened with nowhere to keep its spend ledger.

    The cost gate settles the cumulative session budget against the ledger and
    records every billed statement back into it, so opening a billed connection
    with no store would run unbudgeted rather than merely unrecorded.
    """
