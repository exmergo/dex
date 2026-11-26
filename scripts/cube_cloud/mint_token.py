"""Print a Cube API token for the Cube Cloud fixture deployment, valid one hour.

The `cube_cloud` integration job (#914) runs this in the one step whose tests
use the token, reading the deployment's API secret from CUBE_CLOUD_API_SECRET
(a protected environment secret). A Cube Cloud deployment verifies an HS256 JWT
signed with its CUBEJS_API_SECRET, as a Cube Core does; the tokens Cube Cloud
issues itself (`cube deployments token`) last a day, too short for a weekly
schedule, so CI signs its own per run and nothing long-lived sits in a secret
but the signing key, which `scripts/setup_cube_cloud_ci.sh` rotates.

Standard library only, so it runs before any project dependency is installed.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import sys
import time

TTL_SECONDS = 3600


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def main() -> int:
    secret = os.environ.get("CUBE_CLOUD_API_SECRET", "")
    if not secret:
        print("CUBE_CLOUD_API_SECRET is not set", file=sys.stderr)
        return 1
    now = int(time.time())
    header = _b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    payload = _b64(json.dumps({"iat": now, "exp": now + TTL_SECONDS}).encode())
    signature = hmac.new(
        secret.encode(), f"{header}.{payload}".encode(), hashlib.sha256
    ).digest()
    sys.stdout.write(f"{header}.{payload}.{_b64(signature)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
