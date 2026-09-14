#!/usr/bin/env python3
"""squawk_seal.py -- sealed secret transmission for Squawk.

Squawk is the fleet's agent-to-agent chat: signed per-agent identity
(HMAC-SHA256), Lamport clocks, hash-linked DAG, optional per-channel
encryption. This module adds *sealed secrets*: a sender encrypts a
credential to a RECIPIENT's public key (NaCl sealed box, X25519). Only
ciphertext is ever posted to the channel -- the channel log, message
files, transcripts and audit trails hold nothing useful without the
recipient's private key.

The sealed envelope lives entirely in the message BODY, so the existing
signed/HMAC/Lamport/DAG path is untouched: chat.py signs the ciphertext
body exactly like any other message, and verification works unchanged.

Wire format (message body):
    -----BEGIN SQUAWK SEALED MESSAGE-----
    to: <recipient agent id>
    alg: sealedbox-x25519
    burn: true|false
    note: <optional non-secret sender note>

    <base64 sealed-box ciphertext, wrapped at 64 columns>
    -----END SQUAWK SEALED MESSAGE-----

Key files (same trust boundary as the fleet identity keys):
    <keys>/<agent>.seal.key   mode 0600, 64 hex chars (X25519 private key)
    <keys>/<agent>.seal.pub   mode 0644, 64 hex chars (X25519 public key)
Default <keys> is /home/toxic/.shingle/keys (FLEET_KEYS_DIR overrides,
mirroring fleet_identity.py).

CLI:
    python3 squawk_seal.py keygen <agent-id> [--force]
        Generate a seal keypair for an agent. The private key never
        leaves the keys directory; share the .seal.pub (or run pubkey).

    python3 squawk_seal.py pubkey <agent-id>
        Print the agent's seal public key (safe to distribute).

    python3 squawk_seal.py seal --from SENDER --to RECIPIENT
        [--channel CH] [--title TITLE] [--burn] [--note TEXT]
        [--secret-file PATH | --secret TEXT]
        Encrypt and post. Secret comes from --secret-file, else stdin
        when piped, else --secret (warns: visible in process list).
        Refuses to post if the recipient has no public key.

    python3 squawk_seal.py unseal --as RECIPIENT [--channel CH]
        (--seq N | --file PATH) [--out PATH]
        Decrypt a sealed message. Plaintext goes to stdout (or --out,
        written mode 0600) -- never back to the channel. Refuses when
        the envelope is addressed to someone else. With burn:true in
        the envelope, the message body is tombstoned after a successful
        decrypt (frontmatter/seq/DAG links untouched; the message will
        then fail HMAC verification by design -- the ciphertext is gone).

    python3 squawk_seal.py selftest
        Keygen + seal + unseal roundtrip in a temp keys dir. No chat I/O.

THREAT MODEL (honest version)
    Protects: secret *values* at rest in the chat root, log.jsonl,
        transcripts and audit trails. An attacker reading the channel
        sees only who sent what to whom, when, and how big -- the
        ciphertext is inert without the recipient's private key.
    Does NOT: hide metadata (sender, recipient, timestamp, size are all
        visible in frontmatter). No forward secrecy: a compromised
        recipient private key decrypts every sealed message ever sent
        to it. No sender authentication beyond the chat's own HMAC
        signature (the envelope rides inside a normally-signed message
        -- verify the message signature as usual). First-fetch public
        keys are trust-on-first-use: verify a recipient's .seal.pub out
        of band before sealing high-value credentials.
"""

from __future__ import annotations

import argparse
import base64
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CHAT_DIR = Path(__file__).resolve().parent
CHAT_PY = CHAT_DIR / "chat.py"

KEYS_DIR = Path(os.environ.get("FLEET_KEYS_DIR", "/home/toxic/.shingle/keys"))
SEAL_KEY_SUFFIX = ".seal.key"
SEAL_PUB_SUFFIX = ".seal.pub"

ALG = "sealedbox-x25519"
BEGIN_MARK = "-----BEGIN SQUAWK SEALED MESSAGE-----"
END_MARK = "-----END SQUAWK SEALED MESSAGE-----"
BURN_TOMBSTONE = "[sealed message burned after read -- ciphertext destroyed]"

_AGENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")

_NACL = None
_NACL_ERROR = None


# ---------------------------------------------------------------------------
# Crypto plumbing (PyNaCl sealed box; fail loud, never plaintext fallback)
# ---------------------------------------------------------------------------

def _nacl():
    """Import nacl.public, or raise an actionable error."""
    global _NACL, _NACL_ERROR
    if _NACL is None and _NACL_ERROR is None:
        try:
            import nacl
            import nacl.encoding
            import nacl.exceptions
            import nacl.public
        except ImportError as exc:
            _NACL_ERROR = exc
        else:
            _NACL = nacl
    if _NACL is None:
        raise RuntimeError(
            "squawk_seal: PyNaCl is required for sealed secrets but is not "
            f"importable ({_NACL_ERROR}). Install it with:\n"
            "    pip install pynacl\n"
            "then retry. Refusing to proceed without real public-key crypto."
        )
    return _NACL


def _check_agent_id(agent_id: str) -> str:
    if not isinstance(agent_id, str) or not _AGENT_RE.match(agent_id):
        raise ValueError(
            f"squawk_seal: unsafe agent id {agent_id!r} "
            "(allowed: alphanumerics, dot, underscore, dash; max 64 chars)"
        )
    return agent_id


def _check_channel(channel: str) -> str:
    # Same spirit as chat.py _check_safe_name: no traversal, no dotfiles.
    if (
        not isinstance(channel, str)
        or not channel
        or "/" in channel
        or "\\" in channel
        or "\x00" in channel
        or channel.startswith(".")
        or channel in ("..",)
    ):
        raise ValueError(f"squawk_seal: unsafe channel name {channel!r}")
    return channel


def _key_paths(agent_id: str, keys_dir: Path = KEYS_DIR):
    _check_agent_id(agent_id)
    return (
        keys_dir / f"{agent_id}{SEAL_KEY_SUFFIX}",
        keys_dir / f"{agent_id}{SEAL_PUB_SUFFIX}",
    )


def keygen(agent_id: str, keys_dir: Path = KEYS_DIR, force: bool = False):
    """Generate an X25519 seal keypair for an agent. Returns (priv_path, pub_path)."""
    nacl = _nacl()
    _check_agent_id(agent_id)
    keys_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(keys_dir, 0o700)
    priv_path, pub_path = _key_paths(agent_id, keys_dir)
    if not force and (priv_path.exists() or pub_path.exists()):
        raise RuntimeError(
            f"squawk_seal: seal key already exists for {agent_id!r} "
            f"({priv_path}); pass --force to rotate (old ciphertext "
            f"becomes unreadable)."
        )
    priv = nacl.public.PrivateKey.generate()
    pub = priv.public_key
    priv_hex = priv.encode(nacl.encoding.HexEncoder).decode("ascii")
    pub_hex = pub.encode(nacl.encoding.HexEncoder).decode("ascii")
    # Private key: O_EXCL create, 0600. Never overwrites silently.
    flags = os.O_WRONLY | os.O_CREAT | (os.O_EXCL if not force else os.O_TRUNC)
    fd = os.open(priv_path, flags, 0o600)
    try:
        os.write(fd, (priv_hex + "\n").encode("ascii"))
    finally:
        os.close(fd)
    os.chmod(priv_path, 0o600)
    # Public key is not secret; 0644 so senders can read it.
    pub_path.write_text(pub_hex + "\n", encoding="ascii")
    os.chmod(pub_path, 0o644)
    return priv_path, pub_path


def load_private_key(agent_id: str, keys_dir: Path = KEYS_DIR):
    """Load an agent's seal private key. Raises loudly if missing/malformed."""
    nacl = _nacl()
    priv_path, _ = _key_paths(agent_id, keys_dir)
    try:
        raw = priv_path.read_text(encoding="ascii").strip()
    except FileNotFoundError:
        raise RuntimeError(
            f"squawk_seal: no seal private key for {agent_id!r} "
            f"(expected {priv_path}; run: squawk_seal.py keygen {agent_id})"
        ) from None
    if not _HEX64_RE.match(raw):
        raise RuntimeError(
            f"squawk_seal: malformed seal private key at {priv_path} "
            "(expected 64 lowercase hex chars)"
        )
    return nacl.public.PrivateKey(bytes.fromhex(raw))


def load_public_key(agent_id: str, keys_dir: Path = KEYS_DIR, override: str | None = None):
    """Load a recipient's seal public key (override hex wins)."""
    nacl = _nacl()
    if override:
        raw = override.strip().lower()
        if not _HEX64_RE.match(raw):
            raise ValueError(
                "squawk_seal: --pubkey must be 64 lowercase hex chars"
            )
        return nacl.public.PublicKey(bytes.fromhex(raw))
    _check_agent_id(agent_id)
    _, pub_path = _key_paths(agent_id, keys_dir)
    try:
        raw = pub_path.read_text(encoding="ascii").strip()
    except FileNotFoundError:
        raise RuntimeError(
            f"squawk_seal: no seal public key for recipient {agent_id!r} "
            f"(expected {pub_path}). The recipient must run "
            f"'squawk_seal.py keygen {agent_id}' first and share the "
            f"public key; verify it out of band before sealing "
            f"high-value credentials. Refusing to post."
        ) from None
    if not _HEX64_RE.match(raw):
        raise RuntimeError(
            f"squawk_seal: malformed seal public key at {pub_path}"
        )
    return nacl.public.PublicKey(bytes.fromhex(raw))


def seal_bytes(recipient_pubkey, plaintext: bytes) -> bytes:
    """Sealed-box encrypt. Only the recipient's private key can open it."""
    nacl = _nacl()
    if not isinstance(plaintext, bytes) or not plaintext:
        raise ValueError("squawk_seal: refusing to seal empty plaintext")
    box = nacl.public.SealedBox(recipient_pubkey)
    return box.encrypt(plaintext)  # ephemeral keypair inside; anonymous sender


def unseal_bytes(private_key, ciphertext: bytes) -> bytes:
    """Decrypt. Raises on tamper/wrong key (fail closed)."""
    nacl = _nacl()
    box = nacl.public.SealedBox(private_key)
    try:
        return box.decrypt(ciphertext)
    except nacl.exceptions.CryptoError as exc:
        raise ValueError(
            "squawk_seal: decryption failed -- wrong private key or "
            "tampered ciphertext."
        ) from exc


# ---------------------------------------------------------------------------
# Envelope: sealed payload as a chat message body
# ---------------------------------------------------------------------------

def build_envelope(to: str, ciphertext: bytes, burn: bool = False, note: str = "") -> str:
    """Wrap ciphertext in the chat body envelope. Ciphertext only -- never plaintext."""
    _check_agent_id(to)
    if note and ("\n" in note or "\r" in note):
        raise ValueError("squawk_seal: --note must be a single line (it is not secret)")
    b64 = base64.b64encode(ciphertext).decode("ascii")
    wrapped = "\n".join(b64[i : i + 64] for i in range(0, len(b64), 64))
    lines = [
        BEGIN_MARK,
        f"to: {to}",
        f"alg: {ALG}",
        f"burn: {'true' if burn else 'false'}",
    ]
    if note:
        lines.append(f"note: {note}")
    lines += ["", wrapped, END_MARK]
    return "\n".join(lines) + "\n"


def parse_envelope(body: str) -> dict:
    """Parse a sealed envelope from a message body. Raises ValueError if absent/malformed."""
    try:
        start = body.index(BEGIN_MARK)
        end = body.index(END_MARK, start)
    except ValueError:
        raise ValueError("squawk_seal: no sealed envelope in message body") from None
    inner = body[start + len(BEGIN_MARK) : end]
    # Split header from base64 at the first blank line.
    if "\n\n" in inner:
        header_raw, b64_raw = inner.split("\n\n", 1)
    else:  # pragma: no cover - defensive; build_envelope always emits the blank line
        raise ValueError("squawk_seal: malformed sealed envelope (no header/body split)")
    header = {}
    for line in header_raw.strip().split("\n"):
        if ":" not in line:
            raise ValueError(f"squawk_seal: malformed envelope header line {line!r}")
        k, v = line.split(":", 1)
        header[k.strip().lower()] = v.strip()
    for required in ("to", "alg", "burn"):
        if required not in header:
            raise ValueError(f"squawk_seal: envelope missing {required!r}")
    if header["alg"] != ALG:
        raise ValueError(
            f"squawk_seal: unsupported envelope alg {header['alg']!r} "
            f"(this tool speaks {ALG})"
        )
    _check_agent_id(header["to"])
    b64_clean = "".join(b64_raw.split())
    try:
        ciphertext = base64.b64decode(b64_clean, validate=True)
    except Exception as exc:
        raise ValueError("squawk_seal: envelope ciphertext is not valid base64") from exc
    return {
        "to": header["to"],
        "alg": header["alg"],
        "burn": header["burn"].lower() == "true",
        "note": header.get("note", ""),
        "ciphertext": ciphertext,
    }


# ---------------------------------------------------------------------------
# chat.py interop (post/read the envelope as an ordinary signed message)
# ---------------------------------------------------------------------------

def _chat_root(args_root: str | None) -> Path:
    if args_root:
        return Path(args_root)
    env = os.environ.get("SQUAWK_ROOT")
    if env:
        return Path(env)
    return CHAT_DIR


def _post_envelope(root: Path, channel: str, sender: str, recipient: str,
                   title: str, envelope: str) -> str:
    """Post the envelope via chat.py. Returns chat.py's stdout line."""
    _check_channel(channel)
    # Fail fast: the sender needs an HMAC identity key or chat.py rejects the post.
    idkey = Path(os.environ.get("FLEET_KEYS_DIR", "/home/toxic/.shingle/keys")) / f"{sender}.key"
    if not idkey.exists():
        raise RuntimeError(
            f"squawk_seal: sender {sender!r} has no chat identity key "
            f"({idkey}); run: python3 fleet_identity.py keygen {sender}"
        )
    # _meta.json gate: init the channel if this is its first message.
    meta = root / channel / "_meta.json"
    if not meta.exists():
        init = subprocess.run(
            [sys.executable, str(CHAT_PY), "--root", str(root), "init", channel],
            capture_output=True, text=True, check=False,
        )
        if init.returncode != 0 and "already exists" not in (init.stderr + init.stdout):
            raise RuntimeError(f"squawk_seal: channel init failed: {init.stderr.strip()}")
    # Body via a 0600 temp file: the envelope holds ciphertext, but the
    # secret itself must never appear in argv (visible via ps).
    fd, tmp = tempfile.mkstemp(prefix="squawk-seal-", suffix=".txt")
    try:
        os.write(fd, envelope.encode("utf-8"))
        os.close(fd)
        os.chmod(tmp, 0o600)
        proc = subprocess.run(
            [
                sys.executable, str(CHAT_PY), "--root", str(root), "post", channel,
                "--from", sender, "--to", recipient,
                "--title", title, "--status", "sealed",
                "--body-file", tmp,
            ],
            capture_output=True, text=True, check=False,
        )
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
    if proc.returncode != 0:
        raise RuntimeError(f"squawk_seal: chat post failed: {proc.stderr.strip()}")
    return proc.stdout.strip()


def _find_message_file(root: Path, channel: str, seq: int | None, file: str | None) -> Path:
    _check_channel(channel)
    if file:
        p = Path(file)
        if not p.is_absolute():
            p = root / p
        if not p.is_file():
            raise ValueError(f"squawk_seal: message file not found: {p}")
        return p
    if seq is None:
        raise ValueError("squawk_seal: need --seq or --file to locate the message")
    matches = sorted((root / channel).glob(f"{seq:04d}-*.md"))
    if not matches:
        raise ValueError(f"squawk_seal: no message #{seq} in channel {channel!r}")
    return matches[0]


def _split_body(message_text: str) -> tuple[str, str]:
    """Split a chat message file into (frontmatter, body)."""
    # Written by chat.py as: ---\n<fm>\n---\n\n<body>\n
    parts = message_text.split("\n---\n", 1)
    if len(parts) != 2:
        raise ValueError("squawk_seal: message file is not chat frontmatter format")
    frontmatter, tail = parts
    body = tail[1:] if tail.startswith("\n") else tail
    return frontmatter, body


def _burn_message_file(path: Path, agent: str):
    """Tombstone the body of a sealed message after read.

    Frontmatter, seq, parents and the (now stale) HMAC are left
    byte-identical so DAG/CRDT history structure survives; only the
    ciphertext is destroyed. The message will fail HMAC verification
    afterwards -- by design, the seal is gone.
    """
    text = path.read_text(encoding="utf-8")
    frontmatter, _old_body = _split_body(text)
    tombstone = f"{BURN_TOMBSTONE}\n(burned by {agent})\n"
    path.write_text(frontmatter + "\n---\n\n" + tombstone, encoding="utf-8")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _read_secret(args) -> bytes:
    if args.secret_file:
        data = Path(args.secret_file).read_bytes()
    elif not sys.stdin.isatty():
        data = sys.stdin.buffer.read()
    elif args.secret is not None:
        print(
            "warning: --secret puts the value in the process list; prefer "
            "--secret-file or stdin",
            file=sys.stderr,
        )
        data = args.secret.encode("utf-8")
    else:
        raise ValueError(
            "squawk_seal: no secret provided (use --secret-file, pipe stdin, "
            "or --secret)"
        )
    if not data:
        raise ValueError("squawk_seal: refusing to seal empty secret")
    return data


def _cmd_keygen(a) -> int:
    priv, pub = keygen(a.agent_id, force=a.force)
    print(f"squawk seal keypair ready for {a.agent_id!r}")
    print(f"  private: {priv}  (0600 -- never leaves this box)")
    print(f"  public:  {pub}  (share freely; 'squawk_seal.py pubkey {a.agent_id}')")
    return 0


def _cmd_pubkey(a) -> int:
    _, pub_path = _key_paths(a.agent_id)
    try:
        key = pub_path.read_text(encoding="ascii").strip()
    except FileNotFoundError:
        print(
            f"error: no seal public key for {a.agent_id!r} "
            f"(run: squawk_seal.py keygen {a.agent_id})",
            file=sys.stderr,
        )
        return 1
    # stdout carries ONLY the key so it stays pipeable.
    sys.stdout.write(key + "\n")
    return 0


def _cmd_seal(a) -> int:
    pub = load_public_key(a.to, override=a.pubkey)
    secret = _read_secret(a)
    ciphertext = seal_bytes(pub, secret)
    # Best effort: drop the plaintext from memory as soon as it is sealed.
    del secret
    envelope = build_envelope(a.to, ciphertext, burn=a.burn, note=a.note or "")
    title = a.title or f"sealed secret for {a.to}"
    line = _post_envelope(
        _chat_root(a.root), a.channel, a.sender, a.to, title, envelope
    )
    print(f"squawk sealed message posted ({line})")
    print(f"  to: {a.to}   alg: {ALG}   burn-after-read: {a.burn}")
    print("  channel holds ciphertext only; plaintext never left this process.")
    return 0


def _cmd_unseal(a) -> int:
    root = _chat_root(a.root)
    channel = a.channel or ""
    if channel:
        _check_channel(channel)
    path = _find_message_file(root, channel, a.seq, a.file)
    if not channel:
        # Infer channel from the file's parent directory name.
        channel = path.parent.name
        _check_channel(channel)
    text = path.read_text(encoding="utf-8")
    _frontmatter, body = _split_body(text)
    # priv-* channels wrap the envelope in the channel key first.
    if channel.startswith("priv-"):
        try:
            sys.path.insert(0, str(CHAT_DIR))
            import fleet_e2ee
            body = fleet_e2ee.decrypt_message(channel, body)
        except (ImportError, RuntimeError, ValueError) as exc:
            print(f"error: private-channel decrypt failed: {exc}", file=sys.stderr)
            return 1
    try:
        env = parse_envelope(body)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if env["to"] != a.as_:
        print(
            f"error: sealed envelope is addressed to {env['to']!r}, not {a.as_!r}; "
            f"refusing.",
            file=sys.stderr,
        )
        return 1
    try:
        priv = load_private_key(a.as_)
        plaintext = unseal_bytes(priv, env["ciphertext"])
    except (RuntimeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if a.out:
        out = Path(a.out)
        fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, plaintext)
        finally:
            os.close(fd)
        os.chmod(out, 0o600)
        print(f"squawk unsealed -> {out} (0600)", file=sys.stderr)
    else:
        sys.stdout.buffer.write(plaintext)
        sys.stdout.buffer.flush()
    if env["burn"]:
        _burn_message_file(path, a.as_)
        print(
            f"squawk: burn-after-read executed on {path.name} -- ciphertext "
            f"destroyed. The message will now fail HMAC verification by design.",
            file=sys.stderr,
        )
    return 0


def _cmd_selftest(_a) -> int:
    import tempfile as _tf
    with _tf.TemporaryDirectory(prefix="squawk-seal-test-") as td:
        kd = Path(td)
        keygen("alice", keys_dir=kd)
        keygen("bob", keys_dir=kd)
        # Pubkey override path (sender without a local copy of the key).
        pub_hex = (kd / "bob.seal.pub").read_text().strip()
        bob_pub = load_public_key("bob", keys_dir=kd, override=pub_hex)
        secret = b"dummy-nvapi-key-DO-NOT-USE-12345"
        ct = seal_bytes(bob_pub, secret)
        assert secret not in ct, "ciphertext leaked plaintext"
        env = build_envelope("bob", ct, burn=False, note="selftest")
        parsed = parse_envelope(env)
        assert parsed["to"] == "bob" and parsed["burn"] is False
        assert parsed["note"] == "selftest"
        bob_priv = load_private_key("bob", keys_dir=kd)
        assert unseal_bytes(bob_priv, parsed["ciphertext"]) == secret
        # Wrong recipient's key fails closed.
        alice_priv = load_private_key("alice", keys_dir=kd)
        try:
            unseal_bytes(alice_priv, parsed["ciphertext"])
        except ValueError:
            pass
        else:
            raise AssertionError("wrong-key decrypt should fail")
        # Tampered ciphertext fails closed.
        tampered = bytearray(parsed["ciphertext"])
        tampered[10] ^= 0xFF
        try:
            unseal_bytes(bob_priv, bytes(tampered))
        except ValueError:
            pass
        else:
            raise AssertionError("tampered decrypt should fail")
        # Burn envelope parses.
        assert parse_envelope(build_envelope("bob", ct, burn=True))["burn"] is True
    print("squawk selftest ok: keygen/seal/unseal roundtrip, wrong-key and "
          "tamper fail closed")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="squawk_seal.py",
        description=(
            "Sealed secret transmission for Squawk, the fleet's agent-to-agent "
            "chat. Encrypt a credential to a recipient's public key (NaCl "
            "sealed box); only ciphertext is ever posted to the channel."
        ),
    )
    p.add_argument("--root", help="chat root dir (default: squawk_seal.py's directory)")
    sub = p.add_subparsers(dest="cmd", required=True)

    k = sub.add_parser("keygen", help="generate a seal keypair for an agent")
    k.add_argument("agent_id", help="agent id (e.g. shingle, breaker, yote)")
    k.add_argument("--force", action="store_true",
                   help="rotate: overwrite existing keypair (old ciphertext becomes unreadable)")
    k.set_defaults(func=_cmd_keygen)

    u = sub.add_parser("pubkey", help="print an agent's seal public key (safe to share)")
    u.add_argument("agent_id", help="agent id")
    u.set_defaults(func=_cmd_pubkey)

    s = sub.add_parser("seal", help="encrypt a secret and post it sealed to a channel")
    s.add_argument("--from", dest="sender", required=True, help="sender agent id (needs a chat identity key)")
    s.add_argument("--to", required=True, help="recipient agent id")
    s.add_argument("--channel", default="fleet", help="channel to post in (default: fleet)")
    s.add_argument("--title", help="message title (default: 'sealed secret for <to>')")
    s.add_argument("--burn", action="store_true",
                   help="burn-after-read: recipient's client tombstones the ciphertext after decrypting")
    s.add_argument("--note", default="", help="one-line non-secret note (e.g. 'nvidia key rotation')")
    s.add_argument("--pubkey", help="recipient public key hex (else read from keys dir)")
    s.add_argument("--secret-file", help="read the secret from a file")
    s.add_argument("--secret", help="secret value inline (warns: visible in ps)")
    s.set_defaults(func=_cmd_seal)

    n = sub.add_parser("unseal", help="decrypt a sealed message addressed to you")
    n.add_argument("--as", dest="as_", required=True, help="your agent id (needs the seal private key)")
    n.add_argument("--channel", help="channel (default: inferred from --file)")
    n.add_argument("--seq", type=int, help="message seq number in the channel")
    n.add_argument("--file", help="message file path (absolute or relative to chat root)")
    n.add_argument("--out", help="write plaintext to file (0600) instead of stdout")
    n.set_defaults(func=_cmd_unseal)

    t = sub.add_parser("selftest", help="crypto roundtrip self-test (no chat I/O)")
    t.set_defaults(func=_cmd_selftest)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (RuntimeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
