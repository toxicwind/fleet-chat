#!/usr/bin/env python3
"""fleet_identity.py -- HMAC-SHA256 per-agent message signing for the fleet chat.

The base fork's --as/--from identity is pure self-assertion: any local process
can post as any agent, including the leader. This module fixes that with
per-agent symmetric keys and HMAC-SHA256 signatures over a canonical
serialization of each message. Stdlib only (hmac + hashlib + secrets + os).

KEY STORAGE
    Keys live OUTSIDE the chat root, never inside it:
        /home/toxic/.shingle/keys/<agent_id>.key
    Each file holds 64 lowercase hex chars (32 random bytes), mode 0600.
    Override the directory with the FLEET_KEYS_DIR environment variable
    (used by tests). The key directory itself should be mode 0700.

CANONICAL MESSAGE SERIALIZATION (canonical_message)
    All metadata values are normalized exactly the way the base's
    parse_frontmatter() reads them back: CR/LF folded to a single space
    (mirrors base _frontmatter_value), then stripped of surrounding
    whitespace (mirrors parse_frontmatter's k.strip()/v.strip()).
    The signed payload is UTF-8 bytes of:

        fleet-chat-v1\\n
        seq: <int>\\n
        from: <sender>\\n
        to: <to>\\n
        reply_to: <reply_to or empty>\\n
        channel: <channel>\\n
        ts: <ts>\\n
        status: <status>\\n
        title: <title>\\n
        body-sha256: <sha256 hex of normalized body>\\n

    The body is covered via its SHA-256 digest rather than inline text:
    bodies hold arbitrary markdown/newlines, and the digest keeps the
    canonical form fixed-shape. Body normalization: CRLF/CR -> LF, then
    rstrip() of trailing whitespace -- exactly what base cmd_post writes
    (it emits body.rstrip() after one blank line following the closing ---).

WIRE FORMAT
    One extra frontmatter line, placed LAST before the closing '---':

        hmac: <64 lowercase hex chars>

    parse_frontmatter() in the base ignores unknown keys, so unsigned readers
    keep working; verify_on_read() below is what enforces signatures.

FAIL-CLOSED VERIFICATION (verify_on_read)
    Returns the parsed frontmatter dict on success. Raises FleetIdentityError
    -- naming the agent -- when: the hmac field is absent, the agent has no
    key file, the key file is malformed, or the digest does not match. There
    is no trust-on-first-use and no unsigned fallback. NOTE: pre-existing
    unsigned archive messages will be REJECTED; see sign-archive below for
    the one-time migration.

API
    keygen(agent_id, keys_dir=KEYS_DIR, force=False) -> Path
    sign(agent_id, canonical_bytes, keys_dir=KEYS_DIR) -> str (hex)
    verify(agent_id, canonical_bytes, hex_digest, keys_dir=KEYS_DIR) -> bool
    canonical_message(*, seq, sender, to, reply_to, channel, ts, status,
                      title, body) -> bytes
    verify_on_read(path, keys_dir=KEYS_DIR) -> dict
    sign_archive_file(path, keys_dir=KEYS_DIR) -> bool  (one-time migration)

CLI
    python3 fleet_identity.py keygen <agent_id> [--force]
    python3 fleet_identity.py verify-file <message.md>
    python3 fleet_identity.py sign-archive <channel_dir>
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import os
import re
import secrets
import sys
from pathlib import Path

CANONICAL_VERSION = "fleet-chat-v1"
DEFAULT_KEYS_DIR = Path("/home/toxic/.shingle/keys")

_AGENT_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")


class FleetIdentityError(Exception):
    """Raised for every identity failure. Always names the agent involved."""


def keys_dir() -> Path:
    """Key directory: FLEET_KEYS_DIR env override, else the fleet default."""
    env = os.environ.get("FLEET_KEYS_DIR")
    return Path(env) if env else DEFAULT_KEYS_DIR


def _check_agent_id(agent_id: str) -> str:
    if not isinstance(agent_id, str) or not _AGENT_ID_RE.fullmatch(agent_id):
        raise FleetIdentityError(
            f"invalid agent id '{agent_id}': must match [A-Za-z0-9][A-Za-z0-9_-]{{0,63}} "
            "(path traversal / odd names rejected)"
        )
    return agent_id


def _one_line(value) -> str:
    """Mirror base _frontmatter_value + parse_frontmatter strip semantics."""
    return re.sub(r"[\r\n]+", " ", str(value)).strip()


def _norm_body(body) -> str:
    """Mirror exactly what base cmd_post puts on disk after the blank line."""
    text = str(body).replace("\r\n", "\n").replace("\r", "\n")
    return text.rstrip()


def canonical_message(
    *,
    seq,
    sender,
    to,
    reply_to,
    channel,
    ts,
    status,
    title,
    body,
) -> bytes:
    """Build the canonical byte string that gets HMAC-signed.

    Field order is fixed; every line ends with \\n including the last.
    The body is covered only via its SHA-256 digest (see module docstring).
    """
    digest = hashlib.sha256(_norm_body(body).encode("utf-8")).hexdigest()
    lines = [
        CANONICAL_VERSION,
        f"seq: {int(seq)}",
        f"from: {_one_line(sender)}",
        f"to: {_one_line(to)}",
        f"reply_to: {_one_line(reply_to or '')}",
        f"channel: {_one_line(channel)}",
        f"ts: {_one_line(ts)}",
        f"status: {_one_line(status)}",
        f"title: {_one_line(title)}",
        f"body-sha256: {digest}",
    ]
    return ("\n".join(lines) + "\n").encode("utf-8")


def _key_path(agent_id: str, kd: Path) -> Path:
    return kd / f"{_check_agent_id(agent_id)}.key"


def keygen(agent_id: str, kd: Path | None = None, force: bool = False) -> Path:
    """Generate a fresh 256-bit key for agent_id. Refuses to clobber unless force."""
    kd = Path(kd) if kd else keys_dir()
    _check_agent_id(agent_id)
    kd.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(kd, 0o700)
    except OSError:
        pass
    path = kd / f"{agent_id}.key"
    if path.exists() and not force:
        raise FleetIdentityError(
            f"key for agent '{agent_id}' already exists at {path} "
            "(refusing to overwrite; pass force=True to rotate)"
        )
    key = secrets.token_bytes(32)
    flags = os.O_WRONLY | os.O_CREAT | (os.O_TRUNC if force else os.O_EXCL)
    fd = os.open(path, flags, 0o600)
    try:
        os.write(fd, key.hex().encode("ascii"))
    finally:
        os.close(fd)
    os.chmod(path, 0o600)
    return path


def _load_key(agent_id: str, kd: Path | None = None) -> bytes:
    kd = Path(kd) if kd else keys_dir()
    path = _key_path(agent_id, kd)
    try:
        raw = path.read_text(encoding="ascii").strip()
    except FileNotFoundError:
        raise FleetIdentityError(
            f"no key for agent '{agent_id}' at {path} "
            "(run: fleet_identity.py keygen <agent_id>)"
        )
    except OSError as e:
        raise FleetIdentityError(f"cannot read key for agent '{agent_id}': {e}")
    try:
        key = bytes.fromhex(raw)
    except ValueError:
        raise FleetIdentityError(f"key file for agent '{agent_id}' is not valid hex")
    if len(key) != 32:
        raise FleetIdentityError(
            f"key file for agent '{agent_id}' has wrong length ({len(key)} bytes, want 32)"
        )
    return key


def sign(agent_id: str, canonical_bytes: bytes, kd: Path | None = None) -> str:
    """HMAC-SHA256 hex digest. Raises FleetIdentityError if the agent has no key."""
    key = _load_key(agent_id, kd)
    return hmac.new(key, canonical_bytes, hashlib.sha256).hexdigest()


def verify(
    agent_id: str, canonical_bytes: bytes, hex_digest: str, kd: Path | None = None
) -> bool:
    """Constant-time check. False on ANY failure (missing key, bad hex, mismatch)."""
    try:
        key = _load_key(agent_id, kd)
        expected = hmac.new(key, canonical_bytes, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, str(hex_digest).strip().lower())
    except FleetIdentityError:
        return False


def _parse_file(path: Path) -> tuple[dict, str]:
    """Minimal frontmatter parse mirroring base parse_frontmatter() semantics.

    Returns (meta dict, normalized body str). Raises FleetIdentityError on
    unreadable / malformed files -- fail closed.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as e:
        raise FleetIdentityError(f"message {path.name}: unreadable ({e})")
    lines = text.split("\n")
    if not lines or not lines[0].startswith("---"):
        raise FleetIdentityError(f"message {path.name}: missing frontmatter block")
    meta: dict = {}
    end_idx = None
    for i, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            end_idx = i
            break
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        meta[k.strip()] = v.strip()
    if end_idx is None:
        raise FleetIdentityError(f"message {path.name}: unterminated frontmatter block")
    # Base cmd_post writes: <fm lines> + "---" + "" + body.rstrip() + "\n",
    # i.e. exactly one blank line sits between the closing --- and the body.
    rest = "\n".join(lines[end_idx + 1 :])
    if rest.startswith("\n"):
        rest = rest[1:]
    return meta, _norm_body(rest)


def verify_on_read(path, kd: Path | None = None) -> dict:
    """Verify a message file's hmac frontmatter field. Fail CLOSED.

    Returns the parsed frontmatter dict (with 'hmac' removed) when the
    signature checks out. Raises FleetIdentityError -- always naming the
    agent -- for: missing hmac field, unknown/empty sender, missing or
    malformed key, or digest mismatch (forgery/tampering).
    """
    path = Path(path)
    meta, body = _parse_file(path)
    sender = meta.get("from", "")
    hmac_hex = meta.get("hmac")
    if not hmac_hex:
        raise FleetIdentityError(
            f"message {path.name}: REJECTED -- no 'hmac' field "
            f"(unsigned message claiming to be from '{sender or '?'}')"
        )
    if not sender:
        raise FleetIdentityError(
            f"message {path.name}: REJECTED -- hmac present but 'from' is empty"
        )
    canonical = canonical_message(
        seq=meta.get("seq", "0"),
        sender=sender,
        to=meta.get("to", ""),
        reply_to=meta.get("reply_to", ""),
        channel=meta.get("channel", ""),
        ts=meta.get("ts", ""),
        status=meta.get("status", ""),
        title=meta.get("title", ""),
        body=body,
    )
    if not verify(sender, canonical, hmac_hex, kd):
        raise FleetIdentityError(
            f"message {path.name}: REJECTED -- hmac mismatch for agent '{sender}' "
            f"(seq {meta.get('seq', '?')}: forged or tampered)"
        )
    meta = dict(meta)
    meta.pop("hmac", None)
    return meta


def sign_archive_file(path, kd: Path | None = None) -> bool:
    """One-time migration: sign an existing unsigned message file in place.

    Inserts the hmac: line immediately before the closing '---' of the
    frontmatter block. Returns True if the file was signed, False if it
    already carried an hmac. Raises FleetIdentityError on malformed input.
    Requires the sender's key to exist -- key holders only.
    """
    path = Path(path)
    meta, body = _parse_file(path)
    if meta.get("hmac"):
        return False
    sender = meta.get("from", "")
    if not sender:
        raise FleetIdentityError(f"message {path.name}: cannot sign, 'from' is empty")
    canonical = canonical_message(
        seq=meta.get("seq", "0"),
        sender=sender,
        to=meta.get("to", ""),
        reply_to=meta.get("reply_to", ""),
        channel=meta.get("channel", ""),
        ts=meta.get("ts", ""),
        status=meta.get("status", ""),
        title=meta.get("title", ""),
        body=body,
    )
    sig = sign(sender, canonical, kd)
    text = path.read_text(encoding="utf-8")
    lines = text.split("\n")
    for i, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            lines.insert(i, f"hmac: {sig}")
            break
    else:
        raise FleetIdentityError(f"message {path.name}: unterminated frontmatter block")
    path.write_text("\n".join(lines), encoding="utf-8")
    return True


def _cli() -> int:
    ap = argparse.ArgumentParser(description="fleet chat identity: keygen / verify / migrate")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("keygen", help="generate a 256-bit key for an agent id")
    p.add_argument("agent_id")
    p.add_argument("--force", action="store_true", help="rotate: overwrite existing key")
    p = sub.add_parser("verify-file", help="verify one message file's hmac (debug)")
    p.add_argument("path")
    p = sub.add_parser(
        "sign-archive", help="one-time migration: sign all unsigned messages in a channel dir"
    )
    p.add_argument("channel_dir")
    a = ap.parse_args()
    try:
        if a.cmd == "keygen":
            p_ = keygen(a.agent_id, force=a.force)
            print(f"key written for agent '{a.agent_id}': {p_} (mode 0600)")
        elif a.cmd == "verify-file":
            meta = verify_on_read(a.path)
            print(f"OK: {a.path} verified as '{meta.get('from')}' seq {meta.get('seq')}")
        elif a.cmd == "sign-archive":
            d = Path(a.channel_dir)
            done = skipped = 0
            for entry in sorted(d.iterdir()):
                if not entry.name.endswith(".md"):
                    continue
                try:
                    if sign_archive_file(entry):
                        done += 1
                    else:
                        skipped += 1
                except FleetIdentityError as e:
                    print(f"SKIP {entry.name}: {e}", file=sys.stderr)
            print(f"signed {done}, already-signed {skipped}")
    except FleetIdentityError as e:
        print(f"fleet_identity: error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
