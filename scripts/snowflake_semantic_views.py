#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["pyyaml>=6"]
# ///
"""Deploy and tear down the Snowflake Semantic View fixture corpus.

A maintainer tool for dogfooding `semantic.vendor: snowflake`, run outside
dex's code path: dex never deploys a semantic view. It drives the `snow` CLI
with the maintainer's own connection, as the deployer role
scripts/setup_snowflake_semantic_views.sh creates, against the scratch schema
that script creates.

The corpus is the offline test fixture set, so what Snowflake is asked to
accept is exactly what dex's reader is tested against:

    fixtures/valid/*.yaml      Snowflake accepts, dex reads (some with a warning)
    fixtures/invalid/*.yaml    Snowflake refuses, dex refuses by the rule in
                               the file's `# expect:` header
    fixtures/stricter/*.yaml   Snowflake accepts, dex refuses by design (a
                               duplicate YAML key, a legacy Cortex Analyst file)

under packages/dex-core/tests/semantic_views_vendor/.

Commands:

    verify     Run SYSTEM$CREATE_SEMANTIC_VIEW_FROM_YAML(..., verify_only => TRUE)
               on every fixture. Each valid and stricter fixture must pass and
               each invalid one must fail; prints one JSON line per file with
               Snowflake's verdict and, for a failure, its error. verify_only
               needs a running warehouse, so it resumes (and bills) one.
    deploy     verify, then create every valid fixture view (and the one helper
               table a range relationship needs).
    readback   Write SYSTEM$READ_YAML_FROM_SEMANTIC_VIEW of every deployed
               fixture to --out DIR, one <view>.yaml each.
    teardown   Drop every semantic view in the schema and the helper table,
               leaving the schema empty.

Nothing account-specific lives here: the connection, role, warehouse and schema
are flags, defaulting to the names the setup script creates.

Usage:
    uv run scripts/snowflake_semantic_views.py verify --connection <name>
    uv run scripts/snowflake_semantic_views.py deploy --connection <name>
    uv run scripts/snowflake_semantic_views.py readback --connection <name> --out DIR
    uv run scripts/snowflake_semantic_views.py teardown --connection <name>
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

import yaml

FIXTURES = (
    Path(__file__).resolve().parent.parent
    / "packages/dex-core/tests/semantic_views_vendor/fixtures"
)

#: The one table the corpus needs that TPC-H lacks: a range relationship joins
#: a value to the row whose [start, end) interval holds it, and TPC-H has no
#: interval table. Built from TPC-H itself, so no data enters from elsewhere.
HELPER_TABLE = "PRICE_BANDS"
HELPER_SQL = """
CREATE OR REPLACE TABLE {schema}.PRICE_BANDS AS
SELECT band_id,
       band_id * 10000 AS band_start,
       (band_id + 1) * 10000 AS band_end,
       'band ' || band_id AS band_name
FROM (SELECT SEQ4() AS band_id FROM TABLE(GENERATOR(ROWCOUNT => 12)))
"""


class SnowError(RuntimeError):
    """A `snow sql` call that failed, carrying Snowflake's message."""


def _run(args: argparse.Namespace, statement: str) -> list:
    """Run ``statement`` as the deployer role; the last statement's rows."""

    prefix = f"USE ROLE {args.role}; USE WAREHOUSE {args.warehouse}; "
    command = ["snow", "sql", "--format", "json", "-q", prefix + statement]
    if args.connection:
        command[2:2] = ["-c", args.connection]
    # The maintainer's own `snow` CLI, with arguments this script builds.
    done = subprocess.run(command, capture_output=True, text=True, check=False)  # noqa: S603
    if done.returncode != 0:
        raise SnowError((done.stdout + done.stderr).strip())
    payload = json.loads(done.stdout)
    return payload[-1] if payload and isinstance(payload[0], list) else payload


def _fixtures(kind: str) -> list[Path]:
    """The fixture files of one kind (``valid``, ``stricter``, ``invalid``)."""
    return sorted((FIXTURES / kind).glob("*.yaml"))


def _expected_rule(path: Path) -> str | None:
    """The ``# expect: <rule>`` header of a fixture dex refuses."""
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("# expect:"):
            return line.split(":", 1)[1].strip()
    return None


def _quoted(text: str) -> str:
    """``text`` as a Snowflake dollar-quoted string literal."""
    if "$$" in text:
        raise SnowError("a fixture holds `$$`, which cannot be dollar-quoted")
    return f"$${text}$$"


def _call(args: argparse.Namespace, path: Path, verify_only: bool) -> str:
    """SYSTEM$CREATE_SEMANTIC_VIEW_FROM_YAML on one file; its status text."""
    text = path.read_text(encoding="utf-8")
    flag = "TRUE" if verify_only else "FALSE"
    rows = _run(
        args,
        "CALL SYSTEM$CREATE_SEMANTIC_VIEW_FROM_YAML("
        f"'{args.schema}', {_quoted(text)}, {flag})",
    )
    return str(next(iter(rows[0].values()))) if rows else ""


def verify(args: argparse.Namespace) -> int:
    """verify_only on every fixture; nonzero when one disagrees with its kind."""
    _helper(args)
    disagreements = 0
    for kind in ("valid", "stricter", "invalid"):
        for path in _fixtures(kind):
            try:
                status, verdict = _call(args, path, verify_only=True), "accepted"
            except SnowError as exc:
                status, verdict = str(exc), "refused"
            expect = "refused" if kind == "invalid" else "accepted"
            agrees = verdict == expect
            disagreements += not agrees
            print(
                json.dumps(
                    {
                        "file": f"{kind}/{path.name}",
                        "expect": expect,
                        "dex_rule": _expected_rule(path),
                        "snowflake": verdict,
                        "agrees": agrees,
                        "message": status[-600:],
                    }
                )
            )
    return 1 if disagreements else 0


def _helper(args: argparse.Namespace) -> None:
    """The helper table a range relationship joins to."""
    _run(args, HELPER_SQL.format(schema=args.schema))


def deploy(args: argparse.Namespace) -> int:
    """verify, then create every valid fixture."""
    if verify(args):
        print("verify disagreed with the corpus; nothing deployed", file=sys.stderr)
        return 1
    for path in _fixtures("valid"):
        print(f"{path.name}: {_call(args, path, verify_only=False)}")
    return 0


def _deployed(args: argparse.Namespace) -> list[str]:
    """The semantic views the schema holds now."""
    rows = _run(args, f"SHOW SEMANTIC VIEWS IN SCHEMA {args.schema}")
    return [row["name"] for row in rows]


def readback(args: argparse.Namespace) -> int:
    """Each deployed view's YAML, as Snowflake reads it back."""
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    for name in _deployed(args):
        rows = _run(
            args,
            f"SELECT SYSTEM$READ_YAML_FROM_SEMANTIC_VIEW('{args.schema}.{name}') AS Y",
        )
        text = str(rows[0]["Y"])
        yaml.safe_load(text)  # fail loudly on a read-back that is not YAML
        (out / f"{name.lower()}.yaml").write_text(text, encoding="utf-8")
        print(f"{name}: {len(text)} bytes")
    return 0


def teardown(args: argparse.Namespace) -> int:
    """Drop every semantic view and the helper table."""
    for name in _deployed(args):
        _run(args, f"DROP SEMANTIC VIEW IF EXISTS {args.schema}.{name}")
        print(f"dropped {name}")
    _run(args, f"DROP TABLE IF EXISTS {args.schema}.{HELPER_TABLE}")
    left = _deployed(args)
    print(f"{len(left)} semantic view(s) left in {args.schema}")
    return 1 if left else 0


def main() -> int:
    """Parse the command line and run one command."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("command", choices=("verify", "deploy", "readback", "teardown"))
    parser.add_argument("--connection", help="the maintainer's `snow` connection")
    parser.add_argument("--role", default="DEX_SV_DEPLOYER")
    parser.add_argument("--warehouse", default="DEX_SV_WH")
    parser.add_argument("--schema", default="DEX_SV.FIXTURES")
    parser.add_argument("--out", help="readback: the directory to write into")
    args = parser.parse_args()
    if args.command == "readback" and not args.out:
        parser.error("readback needs --out")
    commands = {
        "verify": verify,
        "deploy": deploy,
        "readback": readback,
        "teardown": teardown,
    }
    try:
        return commands[args.command](args)
    except SnowError as exc:
        print(f"snow failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
