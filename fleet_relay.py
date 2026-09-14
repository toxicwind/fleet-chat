#!/usr/bin/env python3
"""fleet_relay.py -- Muse <-> Squawk relay plumbing (stdlib only).

relay-in (Muse -> Squawk) and relay-out (Squawk -> Muse) ride the EXACT
same signed/sequenced post path as `chat.py post`: sequence lock, DAG
parents, Lamport tick, priv-* E2EE, HMAC-SHA256 sign, .md write, log.jsonl
append, CRDT op. Relay code never hand-writes message files.

The relay is a first-class Squawk identity (the hosting lane keygens it
alongside breaker/shingle/agent1/yote). Messages the relay posts are
signed by that identity; the human whose message it is travels in
frontmatter as `relayed_from: muse-side-chat` + `human: <name>`.

SEAL HOOK POINTS (for the sealed-transmission worker)
-----------------------------------------------------
The sealed envelope format (NaCl sealed-box to recipient public keys,
ciphertext-only on the channel, HMAC covering the envelope) lands HERE
when it is ready. Two functions, one contract:

* seal_for_channel(channel, plaintext) -- relay-in calls this on the
  human text BEFORE the post path. Currently the identity function
  (returns plaintext unchanged). When the sealed envelope format lands,
  implement NaCl sealed-box to the channel members' keys and return the
  envelope string; the post path HMAC-covers it like any other body.
* unseal_message(channel, body, identity, key_dir) -- relay-out and
  squawk-feed call this on every message body. Currently returns
  (body, False). When sealed transmission lands, unseal with the relay
  identity's private key and return (plaintext, True); on failure raise
  SealError so callers mark the record sealed/unreadable instead of
  leaking ciphertext.

Do NOT duplicate these functions elsewhere: the seal worker wires its
implementation here so every relay surface seals/unseals identically.
"""

from __future__ import annotations

import os
from pathlib import Path

import fleet_e2ee
import fleet_identity
import fleet_roster

RELAYED_FROM = "muse-side-chat"

# Identity the relay signs as. Overridable per-invocation (--identity) or
# via env; the hosting lane provisions this identity's key.
RELAY_IDENTITY_ENV = "SQUAWK_RELAY_IDENTITY"
RELAY_IDENTITY_DEFAULT = "relay"

# Keys live OUTSIDE the chat root, never inside it. The hosting lane owns
# this directory (0600 files); the relay only reads.
KEYS_DIR_ENV = "FLEET_KEYS_DIR"
KEYS_DIR_DEFAULT = Path("/home/toxic/.shingle/keys")


class SealError(Exception):
    """Raised when a sealed envelope cannot be unsealed."""


def resolve_identity(cli_value: str | None) -> str:
    """Signing identity: --identity > $SQUAWK_RELAY_IDENTITY > 'relay'."""
    return cli_value or os.environ.get(RELAY_IDENTITY_ENV) or RELAY_IDENTITY_DEFAULT


def resolve_key_dir(cli_value: str | None = None, root=None) -> Path:
    """Keys dir: --key-dir > $FLEET_KEYS_DIR > <root>/keys > the default.

    The <root>/keys preference exists because the canonical deployment
    keeps identities next to the chat root (e.g.
    /home/toxic/.shingle/squawk-root/keys/relay.key). Pass the chat root
    when you have it.
    """
    if cli_value or os.environ.get(KEYS_DIR_ENV):
        return Path(cli_value or os.environ.get(KEYS_DIR_ENV))
    if root is not None:
        cand = Path(root) / "keys"
        if cand.is_dir():
            return cand
    return Path(KEYS_DIR_DEFAULT)


# ---------------------------------------------------------------------------
# Seal hook points (identity until the sealed envelope format lands)
# ---------------------------------------------------------------------------


def seal_for_channel(channel: str, plaintext: str) -> str:
    """SEAL HOOK POINT -- see module docstring.

    Called by relay-in before the post path. Returns the body that will be
    HMAC-signed and persisted. Today: plaintext unchanged.
    """
    # Seal worker: implement NaCl sealed-box to the channel members' keys
    # here and return the envelope string. Keep the return a str.
    return plaintext


def unseal_message(channel: str, body: str, identity: str, key_dir: Path):
    """SEAL HOOK POINT -- see module docstring.

    Returns (plaintext, sealed: bool). Today: (body, False).
    """
    # Seal worker: unseal with the relay identity's private key; return
    # (plaintext, True); raise SealError on failure.
    return body, False


# ---------------------------------------------------------------------------
# Relay record: the machine contract shared by relay-out and squawk-feed
# ---------------------------------------------------------------------------


def _read_frontmatter(path: Path) -> dict:
    """Minimal frontmatter parse (mirrors chat.parse_frontmatter semantics)."""
    meta: dict = {}
    try:
        with path.open(encoding="utf-8") as f:
            if not f.readline().startswith("---"):
                return meta
            for line in f:
                if line.strip() == "---":
                    break
                if ":" not in line:
                    continue
                k, v = line.split(":", 1)
                meta[k.strip()] = v.strip()
    except (OSError, UnicodeError):
        return {}
    return meta


def _parse_parents(raw) -> list:
    if not raw:
        return []
    s = str(raw).strip().strip("[]")
    return [x.strip() for x in s.split(",") if x.strip()]


def roster_status(sender: str) -> str | None:
    """Roster gate without die(): 'revoked' | 'unknown-sender' | None.

    Mirrors chat._sender_cleared: revoked senders are rejected outright;
    unknown senders are rejected once the roster is enrolled (non-empty).
    """
    rec = fleet_roster.lookup(sender)
    if rec is not None and rec.get("revoked"):
        return "revoked"
    if rec is None and fleet_roster.list_all():
        return "unknown-sender"
    return None


def build_relay_record(path: Path, *, channel: str, identity: str, key_dir: Path) -> dict:
    """Build one relay-out / squawk-feed message record.

    Never raises on a bad message: signature problems are reported in the
    record ("signature": "invalid"|"revoked"|"unknown-sender"), never
    silently passed and never fatal to the stream. Sealed/unreadable
    bodies are reported with "sealed": true and "body": null -- ciphertext
    is never dumped into the record.
    """
    meta = _read_frontmatter(path)
    seq_raw = meta.get("seq", "0")
    try:
        seq = int(str(seq_raw).strip())
    except (TypeError, ValueError):
        seq = 0

    rec = {
        "seq": seq,
        "channel": meta.get("channel", channel),
        "from": meta.get("from", ""),
        "to": meta.get("to", "all"),
        "ts": meta.get("ts", ""),
        "title": meta.get("title", ""),
        "status": meta.get("status", ""),
        "lamport": 0,
        "parents": _parse_parents(meta.get("parents")),
        "relayed_from": meta.get("relayed_from"),
        "human": meta.get("human"),
        "signature": "invalid",
        "sealed": False,
        "body": None,
        "unseal_error": None,
        "hmac_version": None,
    }
    try:
        rec["lamport"] = int(str(meta.get("lamport", "") or "0").strip())
    except (TypeError, ValueError):
        rec["lamport"] = 0

    sender = rec["from"]
    try:
        verified = fleet_identity.verify_on_read(path, kd=key_dir)
    except fleet_identity.FleetIdentityError as e:
        rec["unseal_error"] = None
        rec["signature"] = "invalid"
        rec["_verify_error"] = str(e)
        return _finalize_record(rec)

    rec["body"] = verified.get("body", "")
    rec["hmac_version"] = verified.get("hmac_version")
    gate = roster_status(sender)
    if gate is not None:
        rec["signature"] = gate
        rec["body"] = None
        return _finalize_record(rec)
    rec["signature"] = "valid"

    body = rec["body"] or ""
    chan = rec["channel"]
    if chan.startswith(fleet_e2ee.PRIV_PREFIX):
        # E2EE private channel: body on disk is ciphertext; decrypt for
        # display only after the HMAC verified. Fail closed.
        try:
            rec["body"] = fleet_e2ee.decrypt_message(chan, body)
            rec["sealed"] = False
        except Exception as e:  # noqa: BLE001 -- never leak ciphertext
            rec["body"] = None
            rec["sealed"] = True
            rec["unseal_error"] = f"e2ee decrypt failed: {e}"
        return _finalize_record(rec)

    try:
        plaintext, sealed = unseal_message(chan, body, identity, key_dir)
    except SealError as e:
        rec["body"] = None
        rec["sealed"] = True
        rec["unseal_error"] = str(e)
        return _finalize_record(rec)
    rec["body"] = plaintext
    rec["sealed"] = bool(sealed)
    return _finalize_record(rec)


def _finalize_record(rec: dict) -> dict:
    rec.pop("_verify_error", None)
    return rec


def expected_ws_auth(identity: str, nonce: str, key_dir: Path) -> str:
    """Expected WS auth response: HMAC-SHA256(nonce, identity key), hex.

    The client proves the relay identity by computing the same value from
    the key file; the server recomputes it with fleet_identity.sign.
    """
    return fleet_identity.sign(identity, nonce.encode("utf-8"), kd=key_dir)
