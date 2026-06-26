"""Enables ``python -m exmergo_dex_core ...`` as the command entry point."""

from __future__ import annotations

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
