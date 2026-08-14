"""The seeded demo warehouse: the one place in the engine that writes a data file.

Everything else dex does against a warehouse is a read. This module is the single
exception, and it is deliberately its own path so that exception stays visible:
it imports ``duckdb`` directly and never :mod:`..adapters.duckdb`, so the
read-only open in the adapter has no branch that could ever be relaxed, and it
never touches the SQL guard, because the statements here are ``CREATE TABLE`` and
parameterized ``INSERT`` rather than anything an agent authored. A safety-spine
test asserts both of those mechanically rather than trusting this paragraph.

Creating a file is not writing to a user's data. The rule that keeps those two
apart is create-only: a target that already exists is refused outright, with no
confirmation flag that could talk past it, and the parent directory is never
created. So the worst a mistyped path can do is fail.

The data is generated rather than committed, for three reasons. DuckDB's storage
format has broken backward compatibility before, and a stale file would fail on
the first command a stranger ever runs; a binary blob does not delta-compress, so
every regeneration would be permanent history weight; and the skills resolve the
engine per version per environment, so weight in the wheel is paid for repeatedly.

**Determinism is a contract, not a nicety.** The READMEs quote row counts and
column names from this data, so a change here that silently moved a number would
make the documentation wrong in the one place a new user is most likely to be
reading it. Hence one pinned seed, a random stream restricted to
``randrange``/``random`` (stable across CPython releases), no wall-clock anywhere
(every date is measured back from :data:`_ANCHOR`), and :func:`row_digest`, which
a test pins so drift fails CI instead of shipping.
"""

from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from ..errors import DexError

# The default target. A name rather than a path: where it lands is the command's
# decision (the directory the user is standing in), not this module's.
DEMO_FILENAME = "dex_demo.duckdb"

# Pinned. Changing it changes every count the documentation quotes, so it is a
# documentation edit as much as a code one.
DEMO_SEED = 20260301

# Every generated date is measured backwards from here, so the file a user
# generates today is byte-identical to the one generated a year from now.
_ANCHOR = date(2026, 3, 31)

# 2026-01-01T00:00:00Z. `web_events.occurred_at` is declared BIGINT and holds
# epoch *milliseconds*, which is the unit confusion `explore profile` reports.
_EPOCH_MS_START = 1_767_225_600_000
_NINETY_DAYS_MS = 90 * 24 * 60 * 60 * 1000


class DemoError(DexError):
    """Base for the refusals `dex demo` raises."""


class DemoPathError(DemoError):
    """The target path cannot be used: no parent directory, or not a file.

    Distinct from :class:`DemoTargetExistsError` because it is bad input rather
    than a guard: the caller names a different path and it works.
    """


class DemoTargetExistsError(DemoError):
    """Something already sits at the target path, so the demo refused.

    Deliberately not confirmable. The whole promise of this command is that it
    never opens, inspects, or overwrites a warehouse you already have, and a
    ``--confirm`` that could talk past this refusal would put a real warehouse
    one typo away from being replaced. The fix is to name a different path.
    """


class DemoDependencyError(DemoError):
    """The DuckDB client (the ``[duckdb]`` extra) is missing.

    Its own type, and surfaced as a ``prerequisite`` refusal naming the install,
    because this is very likely the first command a new user runs: a bare
    ``ImportError`` here would land on someone who has no idea yet that
    connector clients live behind extras.
    """


@dataclass(frozen=True)
class DemoTable:
    """One generated relation: its DDL, its rows, and why it is in the demo.

    ``note`` is user-facing. It is what lets the envelope say what each table is
    there to teach, so the first run is a tour rather than a pile of tables.
    """

    name: str
    columns: tuple[tuple[str, str], ...]
    rows: tuple[tuple, ...]
    note: str


@dataclass(frozen=True)
class DemoWarehouse:
    """What a generation produced, for the command layer to report."""

    path: Path
    tables: tuple[DemoTable, ...]
    seed: int
    digest: str

    @property
    def row_count(self) -> int:
        return sum(len(t.rows) for t in self.tables)


# The value pools the generator draws from. Order is part of the seed contract:
# rows are picked by index, so reordering a pool changes the generated data and
# therefore every count the documentation quotes.
_FIRST_NAMES = (
    "Ada",
    "Grace",
    "Alan",
    "Katherine",
    "Edsger",
    "Barbara",
    "Tim",
    "Radia",
    "Donald",
    "Frances",
    "Ken",
    "Margaret",
    "Dennis",
    "Adele",
    "Linus",
    "Jean",
    "Guido",
    "Karen",
    "Bjarne",
    "Sophie",
)
_LAST_NAMES = (
    "Lovelace",
    "Hopper",
    "Turing",
    "Johnson",
    "Dijkstra",
    "Liskov",
    "Berners",
    "Perlman",
    "Knuth",
    "Allen",
    "Thompson",
    "Hamilton",
    "Ritchie",
    "Goldberg",
    "Torvalds",
    "Bartik",
    "Rossum",
    "Sparck",
    "Stroustrup",
    "Wilson",
)
_COUNTRIES = ("NL", "BE", "DE", "FR", "ES", "IT", "PL", "SE")
_ORDER_STATUS = ("placed", "paid", "shipped", "delivered", "cancelled")
_PRODUCT_ADJECTIVES = (
    "Compact",
    "Insulated",
    "Folding",
    "Reinforced",
    "Lightweight",
    "Modular",
)
_PRODUCT_MATERIALS = ("Steel", "Bamboo", "Ceramic", "Linen", "Copper", "Walnut")
_PRODUCT_NOUNS = (
    "Kettle",
    "Lamp",
    "Crate",
    "Stool",
    "Planter",
    "Carafe",
    "Shelf",
    "Trivet",
)
_CATEGORIES = ("kitchen", "lighting", "storage", "furniture", "garden", "tableware")
_PAGE_PATHS = (
    "/",
    "/search",
    "/product",
    "/cart",
    "/checkout",
    "/orders",
    "/help",
    "/account",
)

# Distribution centres, written out rather than generated: twelve rows whose
# whole job is to trip the PII detector on data that is not personal at all.
# `city` reads as an address and `latitude`/`longitude` as a location, but this
# is a building. Twelve distinct values each, deliberately: at five or fewer the
# engine de-rates those two categories, and the point here is to meet a false
# positive that actually blocks. `site_name` is the counterweight: a closed
# all-caps vocabulary, which profiling recognizes as reference data and de-rates
# below the block threshold, so the same run shows evidence moving both ways.
_LOCATIONS = (
    ("ROTTERDAM DC", "Rotterdam", 51.9244, 4.4777, 48000),
    ("ANTWERP DC", "Antwerp", 51.2194, 4.4025, 39000),
    ("HAMBURG DC", "Hamburg", 53.5511, 9.9937, 52000),
    ("LILLE HUB", "Lille", 50.6292, 3.0573, 21000),
    ("VALENCIA HUB", "Valencia", 39.4699, -0.3763, 18000),
    ("MILAN HUB", "Milan", 45.4642, 9.1900, 24000),
    ("POZNAN DC", "Poznan", 52.4064, 16.9252, 31000),
    ("MALMO HUB", "Malmo", 55.6050, 13.0038, 16000),
    ("LYON HUB", "Lyon", 45.7640, 4.8357, 19000),
    ("UTRECHT SPOKE", "Utrecht", 52.0907, 5.1214, 9000),
    ("BREMEN SPOKE", "Bremen", 53.0793, 8.8017, 8000),
    ("GHENT SPOKE", "Ghent", 51.0543, 3.7174, 7500),
)


def _money(cents: int) -> Decimal:
    """A DECIMAL value built from an integer, never from a float.

    Going through float would make the stored value depend on binary rounding,
    which is exactly the kind of drift the pinned digest exists to catch.
    """

    return Decimal(f"{cents // 100}.{cents % 100:02d}")


def _uuid_shaped(token: str) -> str:
    """A canonical dashed 8-4-4-4-12 identifier derived from ``token``.

    Derived rather than drawn from ``uuid4`` so it is reproducible; canonical
    rather than merely hex-shaped because profiling recognizes only the dashed
    form as a UUID, and this column is here to be the *homogeneous* key that
    produces no shape warning, against which ``products.sku`` is the contrast.
    """

    digest = hashlib.md5(token.encode("utf-8"), usedforsecurity=False).hexdigest()
    return (
        f"{digest[:8]}-{digest[8:12]}-{digest[12:16]}-{digest[16:20]}-{digest[20:32]}"
    )


def build_tables(seed: int = DEMO_SEED) -> tuple[DemoTable, ...]:
    """The whole warehouse as in-memory rows, with no I/O and no connection.

    Split from :func:`generate_demo_warehouse` so the data can be inspected,
    digested, and asserted on without anything being written anywhere. Each
    block below is one relation and one lesson; the flaws are seeded on purpose,
    because a first run that reports a clean bill of health teaches nothing.
    """

    # A pinned Mersenne Twister is the whole point: the documentation quotes the
    # counts this produces, and nothing generated here is a secret (S311).
    rnd = random.Random(seed)  # noqa: S311

    # --- customers: the PII true positives, and the clean parent key ----------
    customers: list[tuple] = []
    for i in range(1, 1201):
        first = _FIRST_NAMES[rnd.randrange(len(_FIRST_NAMES))]
        last = _LAST_NAMES[rnd.randrange(len(_LAST_NAMES))]
        customers.append(
            (
                i,
                f"{first.lower()}.{last.lower()}{i}@example.com",
                f"{first} {last}",
                _COUNTRIES[rnd.randrange(len(_COUNTRIES))],
                _ANCHOR - timedelta(days=rnd.randrange(30, 1100)),
                _money(rnd.randrange(1500, 480000)),
            )
        )

    # --- products: a mixed-shape candidate key, and a name that is not a person
    # `sku` is unique and non-null, which is what makes it a candidate key and
    # therefore eligible for the value-shape check. That eligibility is why the
    # mixed shapes cannot ride on `order_items.order_item_id` below: that column
    # is deliberately not unique, so it would never be looked at this way.
    products: list[tuple] = []
    for i in range(1, 301):
        sku = (
            str(400000 + i)
            if i <= 270
            else hashlib.md5(
                f"catalog-merge-{i}".encode(), usedforsecurity=False
            ).hexdigest()
        )
        adjective = _PRODUCT_ADJECTIVES[rnd.randrange(len(_PRODUCT_ADJECTIVES))]
        material = _PRODUCT_MATERIALS[rnd.randrange(len(_PRODUCT_MATERIALS))]
        noun = _PRODUCT_NOUNS[rnd.randrange(len(_PRODUCT_NOUNS))]
        products.append(
            (
                i,
                sku,
                f"{adjective} {material} {noun}",
                _CATEGORIES[rnd.randrange(len(_CATEGORIES))],
                _money(rnd.randrange(450, 39000)),
            )
        )

    # --- orders: the clean side, plus a timestamp stored as text --------------
    # `placed_at` is declared VARCHAR and holds ISO-8601 timestamps, which is the
    # single most common shape of "the declared type contradicts the content".
    orders: list[tuple] = []
    for i in range(1, 5001):
        placed = _ANCHOR - timedelta(days=rnd.randrange(0, 420))
        hour, minute, second = rnd.randrange(24), rnd.randrange(60), rnd.randrange(60)
        orders.append(
            (
                i,
                1 + rnd.randrange(1200),
                _ORDER_STATUS[rnd.randrange(len(_ORDER_STATUS))],
                _money(rnd.randrange(900, 92000)),
                f"{placed.isoformat()} {hour:02d}:{minute:02d}:{second:02d}",
            )
        )

    # --- order_items: the broken grain ----------------------------------------
    # Thirteen thousand real line items, then a thousand rows from a batch that
    # was loaded twice. The key is the table's own, so profiling reports it as
    # broken grain rather than shrugging at a repeated foreign key.
    item_ids = list(range(1, 13001)) + [1 + rnd.randrange(13000) for _ in range(1000)]
    order_items = [
        (
            item_id,
            1 + rnd.randrange(5000),
            1 + rnd.randrange(300),
            1 + rnd.randrange(5),
            _money(rnd.randrange(450, 39000)),
        )
        for item_id in item_ids
    ]

    # --- web_events: the join that has to be declined, and epoch milliseconds --
    # `customer_id` shares the parent's name and type, so an edge is inferred on
    # the name alone. The values are from the analytics vendor's own id space and
    # overlap the CRM's by nothing, so verification collapses the inference. This
    # is the failure that otherwise ships: the join runs, returns all-NULL parent
    # attributes, and looks like it worked.
    web_events = [
        (
            _uuid_shaped(f"event-{i}"),
            900000 + rnd.randrange(60000),
            f"sess-{rnd.randrange(90000):05d}",
            _PAGE_PATHS[rnd.randrange(len(_PAGE_PATHS))],
            _EPOCH_MS_START + rnd.randrange(_NINETY_DAYS_MS),
        )
        for i in range(9000)
    ]

    locations = [
        (i, site, city, lat, lng, capacity)
        for i, (site, city, lat, lng, capacity) in enumerate(_LOCATIONS, start=1)
    ]

    return (
        DemoTable(
            name="customers",
            columns=(
                ("customer_id", "INTEGER"),
                ("email", "VARCHAR"),
                ("full_name", "VARCHAR"),
                ("country_code", "VARCHAR"),
                ("signup_date", "DATE"),
                ("lifetime_value", "DECIMAL(10,2)"),
            ),
            rows=tuple(customers),
            note=(
                "email and full_name are personal data; profiling flags both and "
                "the query firewall refuses to project them"
            ),
        ),
        DemoTable(
            name="products",
            columns=(
                ("product_id", "INTEGER"),
                ("sku", "VARCHAR"),
                ("product_name", "VARCHAR"),
                ("category", "VARCHAR"),
                ("list_price", "DECIMAL(10,2)"),
            ),
            rows=tuple(products),
            note=(
                "sku is a key that mixes two id schemes from a merged catalogue; "
                "product_name is a name that is not a person, and is not flagged"
            ),
        ),
        DemoTable(
            name="orders",
            columns=(
                ("order_id", "BIGINT"),
                ("customer_id", "INTEGER"),
                ("status", "VARCHAR"),
                ("order_total", "DECIMAL(10,2)"),
                ("placed_at", "VARCHAR"),
            ),
            rows=tuple(orders),
            note=(
                "the clean join: every customer_id matches. placed_at is declared "
                "VARCHAR but holds timestamps"
            ),
        ),
        DemoTable(
            name="order_items",
            columns=(
                ("order_item_id", "BIGINT"),
                ("order_id", "BIGINT"),
                ("product_id", "INTEGER"),
                ("quantity", "INTEGER"),
                ("unit_price", "DECIMAL(10,2)"),
            ),
            rows=tuple(order_items),
            note=(
                "a batch was loaded twice, so order_item_id is not unique and any "
                "join on it fans out"
            ),
        ),
        DemoTable(
            name="web_events",
            columns=(
                ("event_id", "VARCHAR"),
                ("customer_id", "INTEGER"),
                ("session_id", "VARCHAR"),
                ("page_path", "VARCHAR"),
                ("occurred_at", "BIGINT"),
            ),
            rows=tuple(web_events),
            note=(
                "customer_id shares the CRM's column name but none of its values; "
                "occurred_at is declared BIGINT and holds epoch milliseconds"
            ),
        ),
        DemoTable(
            name="warehouse_locations",
            columns=(
                ("location_id", "INTEGER"),
                ("site_name", "VARCHAR"),
                ("city", "VARCHAR"),
                ("latitude", "DOUBLE"),
                ("longitude", "DOUBLE"),
                ("capacity_units", "INTEGER"),
            ),
            rows=tuple(locations),
            note=(
                "the designed false positives: city and the coordinates are "
                "flagged as personal data, and these are buildings"
            ),
        ),
        DemoTable(
            name="returns",
            columns=(
                ("return_id", "INTEGER"),
                ("order_id", "BIGINT"),
                ("reason_code", "VARCHAR"),
                ("returned_at", "DATE"),
            ),
            rows=(),
            note="the load that half-failed: the table exists and holds nothing",
        ),
    )


def row_digest(tables: tuple[DemoTable, ...]) -> str:
    """A sha256 over every generated column and cell, in declaration order.

    The executable half of the determinism promise. A test pins this value, so
    any edit that would move a row count or a column name quoted in the
    documentation fails there rather than shipping a README that disagrees with
    what the user sees.
    """

    digest = hashlib.sha256()
    for table in tables:
        digest.update(table.name.encode("utf-8"))
        for name, sql_type in table.columns:
            digest.update(f"\x00{name}:{sql_type}".encode())
        for row in table.rows:
            cells = "\x1f".join("" if cell is None else str(cell) for cell in row)
            digest.update(b"\n")
            digest.update(cells.encode("utf-8"))
    return digest.hexdigest()


def generate_demo_warehouse(
    path: str | Path, *, seed: int = DEMO_SEED
) -> DemoWarehouse:
    """Create a new DuckDB file at ``path`` and seed it. Never overwrite.

    The three refusals here are the whole safety story of this module, and they
    all run before ``duckdb`` is even imported, so a refused call has opened
    nothing. On any failure after the file exists it is removed again, so a
    half-written warehouse is never left behind for the read-only adapter to
    find and report as real.
    """

    target = Path(path)
    if target.exists() or target.is_symlink():
        raise DemoTargetExistsError(
            f"'{target}' already exists; dex demo only ever creates a new file and "
            "never opens, inspects, or replaces one you already have. Name a "
            "different path, for example `dex demo demo2.duckdb`"
        )
    parent = target.parent
    if not parent.is_dir():
        raise DemoPathError(
            f"'{parent}' is not an existing directory; dex demo writes one file and "
            "creates no directories. Create it first, or name a path inside a "
            "directory that exists"
        )

    try:
        import duckdb
    except ImportError as exc:
        raise DemoDependencyError(
            "dex demo builds a local DuckDB warehouse, which needs the duckdb "
            "client: reinstall the engine with the on-ramp extra, "
            '`pip install "exmergo-dex-core[duckdb]"`. It pulls no cloud client '
            "and needs no credentials"
        ) from exc

    tables = build_tables(seed)
    connection = duckdb.connect(str(target))
    try:
        # Table and column names are module constants a few lines above, never
        # caller input, and every value goes in as a bound parameter, so the only
        # interpolation here is the shape of a statement this file wrote itself.
        for table in tables:
            columns = ", ".join(f"{name} {sql}" for name, sql in table.columns)
            connection.execute(f"CREATE TABLE {table.name} ({columns})")
            if not table.rows:
                continue
            placeholders = ", ".join("?" for _ in table.columns)
            connection.executemany(
                f"INSERT INTO {table.name} VALUES ({placeholders})",  # noqa: S608
                [list(row) for row in table.rows],
            )
        # Fold the write-ahead log into the database file before closing, so the
        # command leaves exactly the one artifact it reported creating.
        connection.execute("CHECKPOINT")
    except Exception:
        connection.close()
        target.unlink(missing_ok=True)
        raise
    connection.close()

    return DemoWarehouse(
        path=target, tables=tables, seed=seed, digest=row_digest(tables)
    )
