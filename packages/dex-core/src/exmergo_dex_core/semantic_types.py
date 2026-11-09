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

from typing import Literal

__all__ = ["NUMERIC", "TEXT", "UNPLACEABLE", "Bucket", "bucket", "spelled"]

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
