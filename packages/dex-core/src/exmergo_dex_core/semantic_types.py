"""Which kind of value a warehouse column holds, read from the connector's spelling.

A semantic layer declares a type for each field it exposes, and the schema axis
asks whether the warehouse still agrees. Each vendor has its own names for those
kinds, so this module answers in a neutral one (``temporal``, ``numeric``,
``text``, ``boolean``) and each vendor's schema check maps its own types onto
that. SLayer's (`slayer/schema.py`) and Cube's (`cube/schema.py`) read the same
spellings from here, so a connector type means the same thing to both.

Only where the spelling says it plainly. ``None`` is "cannot tell", never a
kind of its own: a guess would turn every unfamiliar spelling into a conflict.
Standard library only, so it imports on a base install.
"""

from __future__ import annotations

import re
from typing import Literal

__all__ = [
    "NUMERIC",
    "TEXT",
    "UNPLACEABLE",
    "Bucket",
    "bucket",
    "list_element",
    "spelled",
]

#: The kinds of value a column can be read as.
Bucket = Literal["temporal", "numeric", "text", "boolean"]

# Spellings that are containers, documents, or bytes: a layer either stores them
# opaquely or reads them as text depending on the driver, so no bucket here is
# one dex could stand behind.
UNPLACEABLE = (
    "STRUCT",
    "ARRAY",
    "MAP",
    "LIST",
    "UNION",
    "JSON",
    "VARIANT",
    "OBJECT",
    "GEOGRAPHY",
    "GEOMETRY",
    "INTERVAL",
    "BLOB",
    "BYTEA",
    "BINARY",
    "VARBINARY",
    "BYTES",
    "RECORD",
)
NUMERIC = (
    "DECIMAL",
    "NUMERIC",
    "NUMBER",
    "BIGNUMERIC",
    "BIGDECIMAL",
    "FIXED",
    "FLOAT",
    "DOUBLE",
    "REAL",
    "MONEY",
    "SMALLMONEY",
)
TEXT = (
    "VARCHAR",
    "CHAR",
    "TEXT",
    "STRING",
    "NVARCHAR",
    "NCHAR",
    "NTEXT",
    "UUID",
    "CHARACTER",
    "BPCHAR",
    "CITEXT",
    "ENUM",
)


def spelled(raw: str | None) -> str:
    """The type's name as the buckets compare it: upper case, no parameters."""

    return (raw or "").upper().split("(")[0].strip()


# A list suffix: DuckDB's and Postgres's `T[]`, DuckDB's fixed-size `T[3]`.
_LIST_SUFFIX = re.compile(r"^(?P<element>.+?)\s*\[\s*\d*\s*\]$", re.DOTALL)

# A list wrapper: BigQuery's `ARRAY<T>`, Snowflake's structured `ARRAY(T)`,
# Trino's `array(T)`, ClickHouse's `Array(T)`.
_LIST_WRAPPER = re.compile(
    r"^ARRAY\s*(?:<(?P<angle>.*)>|\((?P<paren>.*)\))$", re.IGNORECASE | re.DOTALL
)


def list_element(raw: str | None) -> str | None:
    """The element spelling of a list type, ``""`` for an unknown element, or
    ``None`` when ``raw`` is not a list.

    ``VARCHAR[]`` and ``text[]`` are ``VARCHAR`` and ``text``; ``ARRAY<STRING>``
    and ``array(varchar)`` are ``STRING`` and ``varchar``. A bare ``ARRAY``
    (Snowflake's semi-structured array) is a list of unknown elements, and so is
    a list of lists or of structs, since neither element is a scalar a layer
    can type. Redshift's ``SUPER`` is a document, not a list.
    """

    text = (raw or "").strip()
    if not text:
        return None
    if text.upper() == "ARRAY":
        return ""
    found = _LIST_SUFFIX.match(text)
    if found:
        element = found.group("element").strip()
    else:
        wrapped = _LIST_WRAPPER.match(text)
        if not wrapped:
            return None
        element = (wrapped.group("angle") or wrapped.group("paren") or "").strip()
    if not element or list_element(element) is not None:
        return ""
    if spelled(element).startswith(UNPLACEABLE):
        return ""
    return element


def bucket(raw: str | None) -> Bucket | None:
    """The kind of value a column of this connector type holds, or ``None``."""

    name = spelled(raw)
    if not name or name.endswith("[]") or name.startswith(UNPLACEABLE):
        return None
    if name in ("BOOLEAN", "BOOL", "BIT", "LOGICAL"):
        return "boolean"
    if name in ("DATE", "DATE32") or name.startswith(
        ("TIME", "DATETIME", "SMALLDATETIME")
    ):
        return "temporal"
    if "INT" in name or name.startswith(NUMERIC):
        return "numeric"
    if name.startswith(TEXT):
        return "text"
    return None
