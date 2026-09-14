#!/usr/bin/env python3
"""squawk-feed: single-writer daemon -- Squawk fleet channel -> zipfs-vault.

Transport (2026-09-14, Chris's direct decision): NO public endpoint serves
message content. This daemon watches the channel log (Linux inotify hot
path, poll fallback) and stores one envelope JSON per new message in the
zipfs-vault repo, synced to Google Drive:

    ./runner.sh put --text '<envelope>' -a fleet/<seq:06d>
    ./runner.sh sync                      # ZIPFS_VIA=rclone in service env

Envelope:
    {"seq": N, "channel": "fleet", "sender": "<name>",
     "text": "<body, <=500 chars>", "ts": "<iso>", "sealed": false}

  sender = relayed human identity when present, else the Squawk sender.
  text   = body with sealed content unsealed via the relay identity first
           (same trust relay-out already had); sealed-for-others content
           rides along as sealed=true with ciphertext in text.
  text is truncated to 500 chars with a trailing "…".

Single-writer discipline: pull on startup so the cursor starts from the
vault's truth; --pull-first also pulls before every put batch (only needed
if another writer might have synced since).

Security model: obfuscation + Google auth, NOT encryption -- nothing here
is ever served from a public URL (see the zipfs-vault skill).
"""

import argparse
import ctypes
import ctypes.util
import json
import os
import re
import select
import signal
import subprocess
import sys
import threading
from pathlib import Path

import fleet_relay

TEXT_CAP = 500
WATCH_MASK = 0x00000008 | 0x00000100  # IN_CLOSE_WRITE | IN_MOVED_TO
_MSG_RE = re.compile(r"^(\d+)-.*\.md$")


# ---------------------------------------------------------------------------
# channel tail (inotify hot path)
# ---------------------------------------------------------------------------

class _Inotify:
    """Minimal ctypes wrapper: inotify_init1 + add_watch + read."""

    def __init__(self, path: Path, mask: int):
        libc_name = ctypes.util.find_library("c") or "libc.so.6"
        self._libc = ctypes.CDLL(libc_name, use_errno=True)
        self._libc.inotify_init1.argtypes = [ctypes.c_int]
        self._libc.inotify_init1.restype = ctypes.c_int
        self._libc.inotify_add_watch.argtypes = [
            ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
        self._libc.inotify_add_watch.restype = ctypes.c_int
        fd = self._libc.inotify_init1(0)
        if fd < 0:
            raise OSError(ctypes.get_errno(), "inotify_init1 failed")
        self.fd = fd
        wd = self._libc.inotify_add_watch(
            fd, str(path).encode("utf-8"), ctypes.c_uint32(mask))
        if wd < 0:
            err = ctypes.get_errno()
            os.close(fd)
            raise OSError(err, f"inotify_add_watch failed for {path}")

    def read_events(self) -> bool:
        """Drain pending events. Returns True if anything was waiting."""
        try:
            data = os.read(self.fd, 65536)
        except BlockingIOError:
            return False
        return bool(data)

    def close(self):
        try:
            os.close(self.fd)
        except OSError:
            pass


def _channel_high(chan_dir: Path) -> int:
    top = 0
    try:
        names = os.listdir(chan_dir)
    except OSError:
        return 0
    for name in names:
        m = _MSG_RE.match(name)
        if m:
            top = max(top, int(m.group(1)))
    return top


def _new_messages(chan_dir: Path, since: int) -> list:
    out = []
    try:
        names = os.listdir(chan_dir)
    except OSError:
        return out
    for name in sorted(names):
        m = _MSG_RE.match(name)
        if m and int(m.group(1)) > since:
            out.append(chan_dir / name)
    return out


class FeedState:
    """Channel tail state: high-water seq + a cond signalled by the watcher."""

    def __init__(self, chan_dir: Path, channel: str, identity: str, key_dir: Path):
        self.chan_dir = chan_dir
        self.channel = channel
        self.identity = identity
        self.key_dir = key_dir
        self.high = _channel_high(chan_dir)
        self.cond = threading.Condition()
        self.stop = threading.Event()

    def note_advanced(self):
        with self.cond:
            cur = _channel_high(self.chan_dir)
            if cur > self.high:
                self.high = cur
                self.cond.notify_all()


def _watch_loop(state: FeedState):
    """inotify on the channel dir; bumps state.high the instant a post lands."""
    try:
        ino = _Inotify(state.chan_dir, WATCH_MASK)
    except OSError as e:
        print(f"squawk-feed: inotify unavailable ({e}); poll fallback",
              file=sys.stderr)
        while not state.stop.is_set():
            state.note_advanced()
            state.stop.wait(2.0)
        return
    try:
        while not state.stop.is_set():
            r, _, _ = select.select([ino.fd], [], [], 1.0)
            if state.stop.is_set():
                break
            if not r:
                continue
            try:
                if not ino.read_events():
                    continue
            except OSError:
                break
            state.note_advanced()
    finally:
        ino.close()


# ---------------------------------------------------------------------------
# envelopes + zipfs-vault writer
# ---------------------------------------------------------------------------

def build_envelope(rec: dict) -> dict:
    """Map a fleet_relay record onto the zipfs-vault envelope schema."""
    text = rec.get("body") or ""
    if len(text) > TEXT_CAP:
        text = text[: TEXT_CAP - 1] + "…"
    sender = rec.get("human") or rec.get("from")
    return {
        "seq": int(rec["seq"]),
        "channel": rec.get("channel"),
        "sender": sender,
        "text": text,
        "ts": rec.get("ts"),
        "sealed": bool(rec.get("sealed")),
    }


def alias_for(seq: int) -> str:
    """Vault alias: fleet/ + zero-padded 6-digit seq (fleet/000123)."""
    return f"fleet/{int(seq):06d}"


class VaultError(RuntimeError):
    pass


def _runner(zipfs_dir: Path, via: str, *argv: str) -> str:
    """Run the zipfs-vault runner.sh with ZIPFS_VIA forced; raise on failure."""
    env = dict(os.environ)
    env["ZIPFS_VIA"] = via
    try:
        proc = subprocess.run(
            [str(zipfs_dir / "runner.sh"), *argv],
            capture_output=True, text=True, timeout=600, env=env,
        )
    except (OSError, subprocess.SubprocessError) as e:
        raise VaultError(f"runner.sh failed to launch: {e}")
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()[:300]
        raise VaultError(f"runner {' '.join(argv[:2])} failed: {detail}")
    return (proc.stdout or "").strip()


def vault_put(zipfs_dir: Path, via: str, envelope: dict) -> None:
    _runner(zipfs_dir, via, "put", "--text",
            json.dumps(envelope, ensure_ascii=False),
            "-a", alias_for(envelope["seq"]))


def vault_sync(zipfs_dir: Path, via: str) -> None:
    _runner(zipfs_dir, via, "sync")


def vault_pull(zipfs_dir: Path, via: str) -> None:
    _runner(zipfs_dir, via, "pull")


def vault_aliases(zipfs_dir: Path, via: str) -> list:
    out = _runner(zipfs_dir, via, "list", "--json")
    try:
        return list(json.loads(out).keys())
    except (json.JSONDecodeError, AttributeError) as e:
        raise VaultError(f"could not parse vault alias list: {e}")


def startup_cursor(zipfs_dir: Path, via: str, prefix: str = "fleet/") -> int:
    """Cursor = highest fleet/<seq> alias already in the vault (0 if none)."""
    top = 0
    for alias in vault_aliases(zipfs_dir, via):
        if alias.startswith(prefix):
            try:
                top = max(top, int(alias[len(prefix):]))
            except ValueError:
                continue
    return top


def process_new(state: FeedState, zipfs_dir: Path, via: str,
                cursor: int, *, pull_first: bool = False) -> int:
    """Write every channel message with seq > cursor to the vault.

    One put per message, one sync per batch. Returns the new cursor.
    Raises VaultError on runner failure -- the caller keeps the old cursor
    and retries, so no message is silently skipped.
    """
    if pull_first:
        vault_pull(zipfs_dir, via)
    paths = _new_messages(state.chan_dir, cursor)
    if not paths:
        return cursor
    for p in paths:
        rec = fleet_relay.build_relay_record(
            p, channel=state.channel,
            identity=state.identity, key_dir=state.key_dir)
        vault_put(zipfs_dir, via, build_envelope(rec))
        cursor = max(cursor, int(rec["seq"]))
    vault_sync(zipfs_dir, via)
    return cursor


def load_cursor(cursor_file) -> int:
    if cursor_file and cursor_file.exists():
        try:
            return int(cursor_file.read_text().strip() or 0)
        except (OSError, ValueError):
            return 0
    return 0


def save_cursor(cursor_file, cursor: int) -> None:
    if cursor_file:
        cursor_file.parent.mkdir(parents=True, exist_ok=True)
        cursor_file.write_text(str(cursor) + "\n")


def run_writer(*, root: Path, channel: str, identity: str, key_dir: Path,
               zipfs_dir: Path, via: str, since,
               cursor_file, pull_first: bool,
               poll_interval: float) -> None:
    chan_dir = root / channel
    if not chan_dir.is_dir():
        raise VaultError(f"channel '{channel}' not found under {root}")
    state = FeedState(chan_dir, channel, identity, key_dir)

    # Startup cursor: explicit --since wins, else max(vault truth, saved file).
    vault_pull(zipfs_dir, via)
    cursor = (since if since is not None
              else max(startup_cursor(zipfs_dir, via),
                       load_cursor(cursor_file)))
    # Backfill anything already in the channel above the cursor, then follow.
    cursor = process_new(state, zipfs_dir, via, cursor,
                         pull_first=pull_first)
    save_cursor(cursor_file, cursor)
    print(f"squawk-feed: writer live on #{channel}, cursor={cursor}",
          flush=True)

    watcher = threading.Thread(target=_watch_loop, args=(state,),
                               daemon=True)
    watcher.start()

    stop = threading.Event()

    def _sig(*_a):
        state.stop.set()
        stop.set()

    signal.signal(signal.SIGTERM, _sig)
    signal.signal(signal.SIGINT, _sig)
    try:
        while not stop.is_set():
            with state.cond:
                state.cond.wait_for(
                    lambda: state.high > cursor or stop.is_set(),
                    timeout=poll_interval)
            if stop.is_set():
                break
            new_cursor = process_new(state, zipfs_dir, via, cursor,
                                     pull_first=pull_first)
            if new_cursor != cursor:
                cursor = new_cursor
                save_cursor(cursor_file, cursor)
                print(f"squawk-feed: cursor -> {cursor}", flush=True)
    finally:
        state.stop.set()
        print(f"squawk-feed: stopped at cursor={cursor}", flush=True)


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(
        description="squawk-feed writer: fleet channel -> zipfs-vault")
    ap.add_argument("--root", required=True, help="chat root")
    ap.add_argument("--channel", default="fleet", help="channel to watch")
    ap.add_argument("--identity", default=None,
                    help="relay identity for unseal "
                         "(default: $SQUAWK_RELAY_IDENTITY or 'relay')")
    ap.add_argument("--key-dir", default=None,
                    help="fleet keys dir "
                         "(default: $FLEET_KEYS_DIR or /home/toxic/.shingle/keys)")
    ap.add_argument("--zipfs-dir", default=None,
                    help="zipfs-vault skill dir "
                         "(default: ~/workspace/skills/zipfs-vault)")
    ap.add_argument("--via", default=None,
                    help="vault transport: rclone | gws "
                         "(default: $ZIPFS_VIA or rclone)")
    ap.add_argument("--since", type=int, default=None,
                    help="start cursor (default: from vault + saved cursor)")
    ap.add_argument("--cursor-file", default=None,
                    help="persist writer cursor here "
                         "(default: ~/.local/state/squawk-feed/<channel>.cursor)")
    ap.add_argument("--pull-first", action="store_true",
                    help="pull before every put batch (another writer may sync)")
    ap.add_argument("--poll", type=float, default=5.0,
                    help="cond-wait timeout / poll fallback seconds")
    a = ap.parse_args(argv)

    identity = fleet_relay.resolve_identity(a.identity)
    key_dir = fleet_relay.resolve_key_dir(a.key_dir)
    zipfs_dir = Path(a.zipfs_dir or
                     Path.home() / "workspace" / "skills" / "zipfs-vault")
    if not (zipfs_dir / "runner.sh").exists():
        raise VaultError(f"zipfs-vault runner.sh not found in {zipfs_dir}")
    via = a.via or os.environ.get("ZIPFS_VIA") or "rclone"
    cursor_file = (Path(a.cursor_file) if a.cursor_file else
                   Path.home() / ".local" / "state" / "squawk-feed" /
                   f"{a.channel}.cursor")
    run_writer(root=Path(a.root), channel=a.channel, identity=identity,
               key_dir=key_dir, zipfs_dir=zipfs_dir, via=via,
               since=a.since, cursor_file=cursor_file,
               pull_first=a.pull_first, poll_interval=a.poll)


if __name__ == "__main__":
    main()
