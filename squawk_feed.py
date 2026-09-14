#!/usr/bin/env python3
"""squawk-feed: bearer-authed fat long-poll for Squawk (stdlib only).

Endpoints (all under /squawk-feed):

  GET /squawk-feed/ping                public, content-free -> {"seq": N}
  GET /squawk-feed/wait?since=N        bearer auth -> {"seq": M, "messages": [...]}
  GET /squawk-feed/subscribe?since=N   same handler as /wait (alias)

Auth: `Authorization: Bearer <token>`, constant-time compare
(hmac.compare_digest); missing or invalid -> 404 with an empty body, never
revealing the endpoint exists. The token comes from the SQUAWK_FEED_TOKEN
environment variable (pitchfork service env); the server REFUSES TO START
without it. The token is never logged and never committed.

Fat response: {"seq": M, "messages": [relay-record envelopes, ...]} where
every envelope carries its own per-message "seq". Messages with seq >
since, oldest first, capped at 50 per response; the returned "seq" is the
seq of the LAST message in the batch, so the client re-polls with it to
drain the rest. Each message text is truncated to 500 chars. Sealed
messages are unsealed server-side with the relay identity (the hosting
lane provisions relay.seal.key); unopenable ones ride as
{"sealed": true, "body": null} -- ciphertext is never served.

Wake: Linux inotify on the channel dir bumps the high-water mark the
instant a post lands; parked long-polls (hold ~55s) answer immediately.
Timeout with no new messages -> {"seq": <current>, "messages": []}.

HARD RULE: no unauthenticated unsealed content, ever. No exceptions.
"""

import argparse
import ctypes
import ctypes.util
import hmac
import http.server
import json
import os
import re
import select
import socketserver
import sys
import threading
import urllib.parse
from pathlib import Path

import fleet_relay

TOKEN_ENV = "SQUAWK_FEED_TOKEN"
HOLD_SECONDS = 55.0
MAX_MESSAGES = 50
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
# fat response builder
# ---------------------------------------------------------------------------

def _truncate(text, cap: int = TEXT_CAP) -> str:
    if text is None:
        return None
    if len(text) > cap:
        return text[: cap - 1] + "…"
    return text


def build_fat(since: int, state: FeedState,
              max_messages: int = MAX_MESSAGES) -> dict:
    """{"seq": M, "messages": [...]} for messages with seq > since.

    M is the seq of the last message in the batch (== channel high-water
    when nothing was capped), so the client can re-poll to drain.
    """
    paths = _new_messages(state.chan_dir, since)[:max_messages]
    messages = []
    last = since
    for p in paths:
        rec = fleet_relay.build_relay_record(
            p, channel=state.channel,
            identity=state.identity, key_dir=state.key_dir)
        rec["body"] = _truncate(rec.get("body"))
        messages.append(rec)
        last = max(last, int(rec["seq"]))
    if not messages:
        with state.cond:
            last = state.high
    return {"seq": last, "messages": messages}


# ---------------------------------------------------------------------------
# HTTP: one port, bearer-authed fat endpoints + public ping
# ---------------------------------------------------------------------------

class _Handler(http.server.BaseHTTPRequestHandler):
    server_version = "squawk-feed/1.0"

    def log_message(self, fmt, *args):  # keep stderr quiet; no token logging
        sys.stderr.write("squawk-feed: " + fmt % args + "\n")

    def _send_json(self, code: int, obj: dict):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_404(self):
        # Never reveal the endpoint exists: bare 404, empty body.
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _authed(self) -> bool:
        token = self.server.token
        presented = self.headers.get("Authorization") or ""
        want = "Bearer " + token
        return bool(token) and hmac.compare_digest(presented, want)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path == "/squawk-feed/ping":
            with self.server.state.cond:
                high = self.server.state.high
            self._send_json(200, {"seq": high})
            return
        if path in ("/squawk-feed/wait", "/squawk-feed/subscribe"):
            if not self._authed():
                self._send_404()
                return
            qs = urllib.parse.parse_qs(parsed.query)
            try:
                since = int(qs.get("since", ["0"])[0])
            except (TypeError, ValueError):
                since = 0
            since = max(since, 0)
            state = self.server.state
            with state.cond:
                if state.high <= since:
                    state.cond.wait(timeout=self.server.hold)
            self._send_json(200, build_fat(since, state))
            return
        self._send_404()


class FeedServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, state: FeedState, token: str,
                 hold: float = HOLD_SECONDS):
        self.state = state
        self.token = token
        self.hold = hold
        super().__init__(addr, _Handler)


def _ensure_keys_env(root: Path) -> None:
    """squawk_seal reads FLEET_KEYS_DIR at import; make sure it resolves.

    Never overrides an explicit setting -- the service definition should
    set FLEET_KEYS_DIR=/home/toxic/.shingle/squawk-root/keys.
    """
    if "FLEET_KEYS_DIR" not in os.environ:
        os.environ["FLEET_KEYS_DIR"] = str(
            fleet_relay.resolve_key_dir(None, root=root))


def serve(*, root: Path, channel: str, identity: str, key_dir: Path,
          bind: str, port: int, token: str,
          hold: float = HOLD_SECONDS) -> FeedServer:
    """Build (not start) the server; caller runs serve_forever()."""
    chan_dir = root / channel
    if not chan_dir.is_dir():
        raise RuntimeError(f"channel '{channel}' not found under {root}")
    _ensure_keys_env(root)
    state = FeedState(chan_dir, channel, identity, key_dir)
    watcher = threading.Thread(target=_watch_loop, args=(state,),
                               daemon=True)
    watcher.start()
    server = FeedServer((bind, port), state, token, hold)
    server.feed_state = state
    return server


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(
        description="squawk-feed: bearer-authed fat long-poll for Squawk")
    ap.add_argument("--root", required=True, help="chat root")
    ap.add_argument("--channel", default="fleet", help="channel to serve")
    ap.add_argument("--bind", default="127.0.0.1", help="bind address")
    ap.add_argument("--port", type=int, default=25131,
                    help="port to serve (default: 25131)")
    ap.add_argument("--identity", default=None,
                    help="relay identity used to unseal "
                         "(default: $SQUAWK_RELAY_IDENTITY or 'relay')")
    ap.add_argument("--key-dir", default=None,
                    help="fleet keys dir "
                         "(default: $FLEET_KEYS_DIR, else <root>/keys)")
    ap.add_argument("--hold", type=float, default=HOLD_SECONDS,
                    help="long-poll hold seconds (default: 55)")
    a = ap.parse_args(argv)

    token = os.environ.get(TOKEN_ENV)
    if not token:
        print(f"squawk-feed: refusing to start without {TOKEN_ENV} "
              f"(pitchfork service env must provide it)",
              file=sys.stderr)
        raise SystemExit(2)

    identity = fleet_relay.resolve_identity(a.identity)
    key_dir = fleet_relay.resolve_key_dir(a.key_dir, root=Path(a.root))
    server = serve(root=Path(a.root), channel=a.channel, identity=identity,
                   key_dir=key_dir, bind=a.bind, port=a.port, token=token,
                   hold=a.hold)
    sa = server.server_address
    print(f"squawk-feed: serving #{a.channel} on {sa[0]}:{sa[1]} "
          f"(hold={a.hold}s, bearer auth on /wait + /subscribe)",
          flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.feed_state.stop.set()


if __name__ == "__main__":
    main()
