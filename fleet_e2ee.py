"""Per-channel symmetric encryption for private ('priv-') chat channels.

Ported room-key model from michaelwang123/arthas (see docstring of
``_mechanism_notes`` and INTEGRATION.md for file:line provenance in the
upstream repo). The only thing stolen from arthas is the *model*:

    one symmetric key per room/channel, held by every member;
    every message is encrypted with the channel key before it is written
    to storage, and decrypted on read with the same key.

Everything about arthas's relay server, web client, and Docker setup was
stripped away. This file is the file-based adaptation for the emergent
agent-chat fork at /home/toxic/.shingle/chat/.

Public API
----------
ensure_channel_key(channel)            -> pathlib.Path
    Create (idempotently) the symmetric key for a private channel.
    Leader-side provisioning / local keygen. Never runs on the hot path.
encrypt_message(channel, plaintext)    -> str
    Encrypt a message body for a private channel. Key must already exist.
decrypt_message(channel, blob)         -> str
    Decrypt a stored message body. Raises ValueError on tamper/wrong key.

KEY DISTRIBUTION IS OUT OF SCOPE. This module consumes keys; it does not
move them. The fleet leader provisions each agent's copy of the channel key
into /home/toxic/.shingle/keys/ (see INTEGRATION.md). If the key file is
missing, encrypt/decrypt fail loudly -- a private channel must NEVER
silently degrade to plaintext.

Cipher
------
Python's stdlib has no AES, so we use the ``cryptography`` package's
Fernet (AES-128-CBC + HMAC-SHA256, PKCS7 padding, random IV per message,
base64url token; tampering fails closed via InvalidToken). If
``cryptography`` is not importable, every API raises a clear, actionable
RuntimeError telling you how to install it. There is no fallback cipher
and no plaintext fallback -- fail loud, always.

THREAT MODEL (honest version)
-----------------------------
Protects:  message *bodies* at rest inside the chat root from any local
           process (or other agent) that can read the channel folders but
           does NOT have read access to /home/toxic/.shingle/keys/.
Does NOT:  hide metadata -- channel directory names, message file names
           and mtimes, message counts, ciphertext sizes, and read/write
           timing are all still visible. Does not protect against a
           process that can read the keys directory (same trust boundary
           as the identity keys). No forward secrecy: a compromised
           channel key decrypts the channel's entire history. No per-agent
           authentication: anyone holding the channel key can read and
           forge messages. Key provisioning (getting the key onto each
           agent's box) is manual and trusted.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PRIV_PREFIX = "priv-"

# Same trust boundary as the fleet identity keys.
KEYS_DIR = Path("/home/toxic/.shingle/keys")

KEY_SUFFIX = ".key"

# ---------------------------------------------------------------------------
# cryptography shim (stdlib has no AES; Fernet or loud failure)
# ---------------------------------------------------------------------------

_FERNET = None          # the Fernet class, once imported
_CRYPTO_ERROR = None    # the ImportError, if the import failed


def _fernet_cls():
    """Return cryptography.fernet.Fernet, or raise an actionable error."""
    global _FERNET, _CRYPTO_ERROR
    if _FERNET is None and _CRYPTO_ERROR is None:
        try:
            from cryptography.fernet import Fernet
            _FERNET = Fernet
        except ImportError as exc:  # pragma: no cover - env-dependent
            _CRYPTO_ERROR = exc
    if _FERNET is None:
        raise RuntimeError(
            "fleet_e2ee: the 'cryptography' package is required for private "
            "(priv-) channels but is not importable on this machine "
            f"({_CRYPTO_ERROR}). Refusing to handle private channels rather "
            "than degrading to plaintext. Install it with:\n"
            "    pip install cryptography\n"
            "then retry."
        )
    return _FERNET


# ---------------------------------------------------------------------------
# Channel-name safety (mirrors the fork's _check_safe_name in chat.py:
# no path traversal, no absolute paths, no dotfiles)
# ---------------------------------------------------------------------------

def _check_channel(channel: str) -> str:
    """Validate a private channel name. Returns it unchanged or raises."""
    if not isinstance(channel, str) or not channel:
        raise ValueError("fleet_e2ee: channel name must be a non-empty string")
    if not channel.startswith(PRIV_PREFIX):
        raise ValueError(
            f"fleet_e2ee: private-channel API called for non-private channel "
            f"{channel!r} (must start with {PRIV_PREFIX!r})"
        )
    # Traversal / absolute-path guard, same spirit as chat.py _check_safe_name.
    if (
        "/" in channel
        or "\\" in channel
        or "\x00" in channel
        or channel.startswith(".")
        or channel in ("..",)
        or ".." in channel.split("-")  # cheap extra paranoia; '-' is legal
    ):
        raise ValueError(f"fleet_e2ee: unsafe channel name {channel!r}")
    return channel


def _key_path(channel: str) -> Path:
    return KEYS_DIR / f"{channel}{KEY_SUFFIX}"


def _load_key(channel: str) -> bytes:
    """Read the provisioned channel key. Loud failure if absent."""
    channel = _check_channel(channel)
    path = _key_path(channel)
    try:
        with open(path, "rb") as fh:
            key = fh.read().strip()
    except FileNotFoundError:
        raise RuntimeError(
            f"fleet_e2ee: no key provisioned for channel {channel!r}. "
            f"Expected {path}. Key distribution is out of scope for this "
            f"module -- the fleet leader must place the channel key there "
            f"(see INTEGRATION.md). Refusing to encrypt/decrypt without it."
        ) from None
    if not key:
        raise RuntimeError(
            f"fleet_e2ee: key file {path} is empty; refusing to proceed."
        )
    return key


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def ensure_channel_key(channel: str) -> Path:
    """Create the symmetric key for a private channel, idempotently.

    Leader-side / keygen use only -- not on the message hot path. Creates
    /home/toxic/.shingle/keys/ (0700) if needed and writes
    <channel>.key (0600) with O_EXCL so concurrent keygens don't race.
    Returns the key path. If the key already exists, returns it untouched.
    """
    channel = _check_channel(channel)
    _fernet_cls()  # fail fast if crypto is unavailable, before touching disk
    KEYS_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    # Enforce 0700 even if the dir already existed with looser perms.
    os.chmod(KEYS_DIR, 0o700)
    path = _key_path(channel)
    if path.exists():
        return path
    key = _fernet_cls().generate_key()  # 32 random bytes, base64url-encoded
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, key + b"\n")
    finally:
        os.close(fd)
    # 0o600 was set at creation; assert it (umask could not have loosened an
    # O_EXCL create with an explicit mode, but belt and suspenders).
    os.chmod(path, 0o600)
    return path


def encrypt_message(channel: str, plaintext: str) -> str:
    """Encrypt ``plaintext`` for ``channel``. Returns the Fernet token (str).

    Raises RuntimeError if the channel key is not provisioned or
    ``cryptography`` is missing -- never returns plaintext.
    """
    key = _load_key(channel)
    if not isinstance(plaintext, str):
        raise TypeError("fleet_e2ee: plaintext must be str")
    token = _fernet_cls()(key).encrypt(plaintext.encode("utf-8"))
    return token.decode("ascii")


def decrypt_message(channel: str, blob: str) -> str:
    """Decrypt a stored message body. Raises ValueError on tamper/wrong key."""
    key = _load_key(channel)
    if not isinstance(blob, str):
        raise TypeError("fleet_e2ee: blob must be str")
    Fernet = _fernet_cls()
    try:
        from cryptography.fernet import InvalidToken
    except ImportError:  # pragma: no cover - _fernet_cls would have raised
        raise RuntimeError("fleet_e2ee: 'cryptography' is not importable")
    try:
        raw = Fernet(key).decrypt(blob.encode("ascii"))
    except InvalidToken:
        raise ValueError(
            f"fleet_e2ee: could not decrypt message for channel {channel!r}: "
            f"wrong key or tampered ciphertext."
        ) from None
    return raw.decode("utf-8")


# ---------------------------------------------------------------------------
# Minimal keygen CLI (leader UX; see INTEGRATION.md)
# ---------------------------------------------------------------------------

def _cmd_gen(channel: str) -> int:
    path = ensure_channel_key(channel)
    print(f"key ready: {path}")
    return 0


def _cmd_show(channel: str) -> int:
    """Print the raw key (base64) so the leader can distribute it.

    Use over a secure channel only; the printed value is the entire secret.
    """
    key = _load_key(channel)
    st = os.stat(_key_path(channel))
    if st.st_mode & 0o777 != 0o600:
        print(
            f"warning: key file perms are {oct(st.st_mode & 0o777)}, "
            f"expected 0o600",
            file=sys.stderr,
        )
    sys.stdout.write(key.decode("ascii") + "\n")
    return 0


def _cmd_selftest(channel: str) -> int:
    blob = encrypt_message(channel, "fleet-e2ee selftest")
    assert decrypt_message(channel, blob) == "fleet-e2ee selftest"
    print(f"selftest ok: encrypt/decrypt roundtrip for {channel!r}")
    return 0


def main(argv: list[str]) -> int:
    usage = (
        "usage: python3 fleet_e2ee.py gen <priv-channel>      # create key\n"
        "       python3 fleet_e2ee.py show <priv-channel>     # print key for distribution\n"
        "       python3 fleet_e2ee.py selftest <priv-channel> # roundtrip check"
    )
    if len(argv) != 3:
        print(usage, file=sys.stderr)
        return 2
    cmd, channel = argv[1], argv[2]
    try:
        if cmd == "gen":
            return _cmd_gen(channel)
        if cmd == "show":
            return _cmd_show(channel)
        if cmd == "selftest":
            return _cmd_selftest(channel)
    except (RuntimeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(usage, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
