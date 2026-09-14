"""Test package bootstrap: provision throwaway fleet identity keys.

The task/lease audit path HMAC-signs events with per-agent keys resolved via
FLEET_KEYS_DIR (fleet_identity.keys_dir()). CI runners and fresh dev machines
have no real keys, so we mint ephemeral ones in a temp dir before any test
module runs. Production key locations are untouched.
"""
import os
import tempfile
from pathlib import Path

_keys_dir = Path(tempfile.mkdtemp(prefix="fleet-test-keys-"))
os.environ["FLEET_KEYS_DIR"] = str(_keys_dir)

from fleet_identity import keygen  # noqa: E402

for _agent in ("alice", "bob", "mallory", "recovery", "system"):
    try:
        keygen(_agent, _keys_dir)
    except Exception:
        pass
