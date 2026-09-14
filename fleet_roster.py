"""Fleet identity roster for the agent-chat fork.

File-based port of dipakkr/agentsync's identity roster, adapted for our
trust model. agentsync keeps one roster per hub project in memory (folded
from member.register events in an event-sourced NDJSON log, src/hub/store.js)
and writes a per-agent identity.json in each repo clone
(.agentsync/identity.json, src/mcp/server.js). Members look like::

    {id: "<person>.<machine>.<agent>", person, machine, agentKind, role}

with the id derived as ``f"{person}.{machine}.{agent}".lower()`` with
whitespace collapsed to dashes (src/mcp/server.js:50-53). Presence is a
heartbeat fold (member.presence events, src/hub/server.js:212-229).

Our adaptation: a single persistent roster file living at
``/home/toxic/.shingle/roster`` -- the same trust boundary as the identity
keys managed by the sibling fleet_identity.py (NOT inside the chat root,
which is untrusted data). One JSON document per agent, revocation as a
first-class flag checkable at read time.

Schema (per agent_id key in the document)::
    {
      "agent_id": "8a756bd0",                      # stable fleet id
      "label": "chris.awrawr-pc.worker-8a756bd0",  # person.machine.agent style
      "role": "leader" | "worker" | "watcher",
      "hmac_key_id": "key-20260914-01",            # key id from fleet_identity.py
      "created_ts": 1757835826.0,
      "revoked": false,
      "revoked_ts": null
    }

No crypto lives here: hmac_key_id is an opaque reference; key material is
owned entirely by fleet_identity.py. Revocation is consulted by the
message-verification path (see INTEGRATION.md) -- a revoked agent's signed
messages must be rejected even though the signature itself still checks.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

# Reuse the fork's advisory lock primitive rather than rolling our own.
# agent_chat/_advisory_lock.py is an flock-based lock with timeout
# (O_NOFOLLOW, 0o600) -- sufficient for the roster write path.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from agent_chat._advisory_lock import acquire_advisory_file_lock  # noqa: E402

ROSTER_PATH = Path(os.environ.get("FLEET_ROSTER", "/home/toxic/.shingle/roster"))
ROSTER_VERSION = 1
LOCK_TIMEOUT = 10.0

ROLES = ("leader", "worker", "watcher")

# person.machine.agent, agentsync-style: lowercase, dots separating exactly
# three segments; agentsync derives it with .lower() + whitespace->dashes
# (src/mcp/server.js:50-53). We accept [a-z0-9-] per segment.
LABEL_RE = re.compile(r"^[a-z0-9-]+(\.[a-z0-9-]+){2}$")


class RosterError(Exception):
    pass


def _empty_doc() -> dict:
    return {"version": ROSTER_VERSION, "agents": {}}


def _validate_label(label: str) -> str:
    label = label.strip().lower()
    if not LABEL_RE.match(label):
        raise RosterError(
            f"bad label {label!r}: want person.machine.agent, "
            "lowercase, segments of [a-z0-9-]"
        )
    return label


def _read_doc() -> dict:
    if not ROSTER_PATH.exists():
        return _empty_doc()
    try:
        doc = json.loads(ROSTER_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RosterError(f"roster at {ROSTER_PATH} unreadable/corrupt: {exc}")
    if not isinstance(doc, dict) or doc.get("version") != ROSTER_VERSION:
        raise RosterError(f"roster at {ROSTER_PATH}: unsupported version/format")
    doc.setdefault("agents", {})
    return doc


def _write_doc(doc: dict) -> None:
    """Atomic document write: temp file + rename, under the advisory lock."""
    ROSTER_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = ROSTER_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, ROSTER_PATH)


def register(
    agent_id: str,
    label: str,
    role: str = "worker",
    hmac_key_id: str = "",
) -> dict:
    """Add an agent to the roster. Raises RosterError on duplicate/bad input."""
    agent_id = agent_id.strip()
    if not agent_id:
        raise RosterError("agent_id is required")
    label = _validate_label(label)
    if role not in ROLES:
        raise RosterError(f"role must be one of {ROLES}, got {role!r}")
    hmac_key_id = hmac_key_id.strip()
    if not hmac_key_id:
        raise RosterError("hmac_key_id is required (references fleet_identity.py key)")

    lock = acquire_advisory_file_lock(
        ROSTER_PATH.with_suffix(".lock"), timeout=LOCK_TIMEOUT
    )
    try:
        doc = _read_doc()
        existing = doc["agents"].get(agent_id)
        if existing and not existing.get("revoked"):
            raise RosterError(f"agent_id {agent_id!r} already registered")
        record = {
            "agent_id": agent_id,
            "label": label,
            "role": role,
            "hmac_key_id": hmac_key_id,
            "created_ts": time.time(),
            "revoked": False,
            "revoked_ts": None,
        }
        doc["agents"][agent_id] = record
        _write_doc(doc)
        return record
    finally:
        lock.release()


def revoke(agent_id: str) -> dict:
    """Mark an agent revoked. Revocation is soft -- history is preserved."""
    lock = acquire_advisory_file_lock(
        ROSTER_PATH.with_suffix(".lock"), timeout=LOCK_TIMEOUT
    )
    try:
        doc = _read_doc()
        record = doc["agents"].get(agent_id)
        if record is None:
            raise RosterError(f"unknown agent_id {agent_id!r}")
        if record.get("revoked"):
            return record  # idempotent
        record["revoked"] = True
        record["revoked_ts"] = time.time()
        _write_doc(doc)
        return record
    finally:
        lock.release()


def lookup(agent_id: str) -> dict | None:
    """Return the agent record, or None if unknown. Read-time revocation
    check: callers (message verification) must consult record['revoked']."""
    doc = _read_doc()
    return doc["agents"].get(agent_id)


def is_active(agent_id: str) -> bool:
    """Convenience predicate for the verify-on-read path: registered AND not revoked."""
    record = lookup(agent_id)
    return bool(record) and not record.get("revoked")


def list_active() -> list[dict]:
    """All non-revoked agents."""
    doc = _read_doc()
    return [r for r in doc["agents"].values() if not r.get("revoked")]


def list_all() -> list[dict]:
    return list(_read_doc()["agents"].values())


def _main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(
        prog="fleet_roster",
        description="Fleet identity roster (person.machine.agent style labels).",
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_reg = sub.add_parser("register", help="register a new agent")
    p_reg.add_argument("agent_id")
    p_reg.add_argument("--label", required=True, help="person.machine.agent label")
    p_reg.add_argument("--role", default="worker", choices=ROLES)
    p_reg.add_argument("--key-id", required=True, dest="key_id",
                       help="key id managed by fleet_identity.py")

    p_rev = sub.add_parser("revoke", help="revoke an agent")
    p_rev.add_argument("agent_id")

    p_look = sub.add_parser("lookup", help="look up one agent")
    p_look.add_argument("agent_id")

    p_list = sub.add_parser("list", help="list agents")
    p_list.add_argument("--all", action="store_true",
                        help="include revoked agents")

    args = ap.parse_args(argv)
    try:
        if args.cmd == "register":
            print(json.dumps(register(args.agent_id, args.label, args.role, args.key_id), indent=2))
        elif args.cmd == "revoke":
            print(json.dumps(revoke(args.agent_id), indent=2))
        elif args.cmd == "lookup":
            rec = lookup(args.agent_id)
            if rec is None:
                print(f"unknown agent_id {args.agent_id!r}", file=sys.stderr)
                return 1
            print(json.dumps(rec, indent=2))
        elif args.cmd == "list":
            recs = list_all() if args.all else list_active()
            print(json.dumps(recs, indent=2))
    except RosterError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
