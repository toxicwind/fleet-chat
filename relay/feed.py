#!/usr/bin/env python3
"""squawk-feed: event-driven squawk->chat feed. Stdlib only, zero polling.

- Watches /home/toxic/.shingle/chat/<channel>/ for new message files via
  inotify (libc, ctypes). inotify is only the trigger; a scan reconciles state.
- Appends new messages to outbox.jsonl with a global monotonic seq (persisted
  in state.json). Restarts never reset the seq.
- Serves on 127.0.0.1:PORT (SQUAWK_FEED_PORT, default 25135):
    GET /squawk-feed/seq                -> {"seq": N}   (content-free, public via funnel)
    GET /squawk-feed/messages?since=N   -> {"seq": N, "messages": [...]} (localhost only)
    GET /squawk-feed/wait?since=N       -> {"seq": M}   (long-poll ~50s, content-free)
- Respects control.json {paused, channels, skip_authors}; the rig relay agent
  owns that file. Paused = held, not deleted (state does not advance).
- Sealed messages are flagged sealed:true with title only; unsealing is the
  repo relay-out lane (relay identity key), not this path.
"""
import ctypes
import ctypes.util
import json
import os
import re
import struct
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit, parse_qs

CHAT_ROOT = Path(os.environ.get("SQUAWK_CHAT_ROOT", "/home/toxic/.shingle/squawk-root"))
RELAY_DIR = Path(os.environ.get("SQUAWK_RELAY_DIR", "/home/toxic/.shingle/squawk-relay"))
OUTBOX = RELAY_DIR / "outbox.jsonl"
STATE = RELAY_DIR / "state.json"
CONTROL = RELAY_DIR / "control.json"
PORT = int(os.environ.get("SQUAWK_FEED_PORT", "25135"))
BUFFER_KEEP = 500

FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n(.*)$", re.DOTALL)
MSG_FILE_RE = re.compile(r"^\d+-.*\.md$")

# ---------------- inotify (libc via ctypes, stdlib only) ----------------
IN_CLOSE_WRITE = 0x00000008
IN_MOVED_TO = 0x00000080
IN_CREATE = 0x00000100
IN_ISDIR = 0x40000000

_libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)


def _check(ret, what):
    if ret < 0:
        errno = ctypes.get_errno()
        raise OSError(errno, "%s: %s" % (what, os.strerror(errno)))
    return ret


_inotify_fd = _check(_libc.inotify_init1(0), "inotify_init1")
_EVENT_FMT = "iIII"
_EVENT_SIZE = struct.calcsize(_EVENT_FMT)
_wd_to_path = {}


def _add_watch(path, mask):
    wd = _check(_libc.inotify_add_watch(_inotify_fd, str(path).encode(), mask),
                "inotify_add_watch %s" % path)
    _wd_to_path[wd] = str(path)
    return wd


def _read_events():
    data = os.read(_inotify_fd, 65536)
    events = []
    i = 0
    while i + _EVENT_SIZE <= len(data):
        wd, mask, _cookie, elen = struct.unpack_from(_EVENT_FMT, data, i)
        i += _EVENT_SIZE
        name = data[i:i + elen].split(b"\0", 1)[0].decode(errors="replace")
        i += elen
        events.append((wd, mask, name))
    return events


# ---------------- message parsing ----------------
def parse_msg(path):
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return None
    m = FRONTMATTER_RE.match(text)
    if not m:
        return None
    fm, body = m.groups()
    meta = {}
    for line in fm.splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            meta[k.strip()] = v.strip()
    try:
        seq = int(meta.get("seq", 0))
    except ValueError:
        seq = 0
    return {
        "msg_seq": seq,
        "channel": meta.get("channel", path.parent.name),
        "author": meta.get("from", "unknown"),
        "to": meta.get("to", ""),
        "ts": meta.get("ts", ""),
        "title": meta.get("title", ""),
        "sealed": meta.get("status", "") == "sealed",
        "body": body.strip(),
    }


# ---------------- shared feed state ----------------
_lock = threading.Lock()
_feed_seq = 0
_channels_last = {}   # channel -> last msg_seq processed
_buffer = []          # recent outbox entries (dicts), oldest-first


def load_state():
    global _feed_seq, _channels_last
    if not STATE.exists():
        return
    try:
        s = json.loads(STATE.read_text())
    except (OSError, ValueError):
        return
    if isinstance(s, dict) and "feed_seq" in s:
        _feed_seq = int(s.get("feed_seq", 0))
        _channels_last = dict(s.get("channels", {}))
    else:
        # legacy format: {channel: msg_seq}
        _channels_last = {k: int(v) for k, v in s.items()}


def save_state():
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(
        {"feed_seq": _feed_seq, "channels": _channels_last},
        indent=2, sort_keys=True))
    tmp.replace(STATE)


def load_control():
    if CONTROL.exists():
        try:
            return json.loads(CONTROL.read_text())
        except (OSError, ValueError):
            pass
    return {}


def load_outbox_tail():
    if not OUTBOX.exists():
        return
    try:
        lines = OUTBOX.read_text(errors="replace").splitlines()[-BUFFER_KEEP:]
    except OSError:
        return
    for line in lines:
        try:
            _buffer.append(json.loads(line))
        except ValueError:
            continue


def channel_dirs():
    dirs = []
    if not CHAT_ROOT.is_dir():
        return dirs
    for d in sorted(CHAT_ROOT.iterdir()):
        if d.is_dir() and not d.name.startswith("."):
            dirs.append(d)
    return dirs


def is_channel_dir(d):
    try:
        return any(f.is_file() and MSG_FILE_RE.match(f.name) for f in d.iterdir())
    except OSError:
        return False


def scan_channels():
    """Reconcile: relay any message newer than recorded state. Returns count."""
    global _feed_seq
    ctl = load_control()
    if ctl.get("paused"):
        return 0
    only = ctl.get("channels")
    skip_authors = set(ctl.get("skip_authors", ["relay", "squawk-relay"]))
    new_count = 0
    with _lock:
        with OUTBOX.open("a") as out:
            for chdir in channel_dirs():
                ch = chdir.name
                if only and ch not in only:
                    continue
                if not is_channel_dir(chdir):
                    continue
                last = _channels_last.get(ch, 0)
                msgs = []
                for f in sorted(chdir.glob("*.md")):
                    if not MSG_FILE_RE.match(f.name):
                        continue
                    msg = parse_msg(f)
                    if (msg and msg["msg_seq"] > last
                            and msg["author"] not in skip_authors):
                        msgs.append(msg)
                msgs.sort(key=lambda m: m["msg_seq"])
                for msg in msgs:
                    _feed_seq += 1
                    text = ("[sealed message: %s]" % (msg["title"] or "sealed")
                            if msg["sealed"] else msg["body"][:2000])
                    entry = {
                        "seq": _feed_seq,
                        "ts": msg["ts"],
                        "channel": msg["channel"],
                        "author": msg["author"],
                        "to": msg["to"],
                        "text": text,
                        "msg_seq": msg["msg_seq"],
                        "sealed": msg["sealed"],
                    }
                    out.write(json.dumps(entry) + "\n")
                    _buffer.append(entry)
                    del _buffer[:-BUFFER_KEEP]
                    _channels_last[ch] = msg["msg_seq"]
                    new_count += 1
        if new_count:
            save_state()
    return new_count


# ---------------- inotify watcher thread ----------------
def ensure_watches():
    _add_watch(CHAT_ROOT, IN_CREATE | IN_MOVED_TO)
    for chdir in channel_dirs():
        try:
            _add_watch(chdir, IN_CLOSE_WRITE | IN_MOVED_TO | IN_CREATE)
        except OSError:
            pass
    try:
        _add_watch(RELAY_DIR, IN_CLOSE_WRITE | IN_MOVED_TO)
    except OSError:
        pass


_last_scan = [0.0]


def trigger_scan():
    now = time.monotonic()
    if now - _last_scan[0] < 0.3:  # debounce bursts
        return
    _last_scan[0] = now
    try:
        n = scan_channels()
        if n:
            print("relayed %d new message(s), feed_seq=%d" % (n, _feed_seq), flush=True)
    except Exception as e:
        print("scan error: %s" % e, flush=True)


def inotify_loop():
    while True:
        try:
            for wd, mask, name in _read_events():
                # new dir under chat root -> watch it if it's a channel
                if (wd in _wd_to_path and _wd_to_path[wd] == str(CHAT_ROOT)
                        and (mask & IN_ISDIR) and (mask & (IN_CREATE | IN_MOVED_TO))):
                    cand = CHAT_ROOT / name
                    if cand.is_dir():
                        try:
                            _add_watch(cand, IN_CLOSE_WRITE | IN_MOVED_TO | IN_CREATE)
                        except OSError:
                            pass
                    trigger_scan()
                elif name == "control.json":
                    trigger_scan()  # pause/resume/filter change
                elif mask & (IN_CLOSE_WRITE | IN_MOVED_TO | IN_CREATE):
                    if MSG_FILE_RE.match(name):
                        trigger_scan()
        except Exception as e:
            print("inotify error: %s" % e, flush=True)
            time.sleep(1)


# ---------------- HTTP ----------------
class Handler(BaseHTTPRequestHandler):
    server_version = "squawk-feed/1.0"

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parts = urlsplit(self.path)
        if parts.path == "/squawk-feed/seq" or parts.path == "/squawk-feed/ping":
            # content-free counter. safe for public funnel (no auth).
            with _lock:
                seq = _feed_seq
            self._json({"seq": seq})
        elif parts.path == "/squawk-feed/messages":
            qs = parse_qs(parts.query)
            try:
                since = int(qs.get("since", ["0"])[0])
            except ValueError:
                since = 0
            with _lock:
                seq = _feed_seq
                msgs = [m for m in _buffer if m["seq"] > since][-200:]
            self._json({"seq": seq, "messages": msgs})
        elif parts.path in ("/squawk-feed/wait", "/squawk-feed/subscribe"):
            # long-poll: hold up to ~50s, respond the instant seq advances.
            # content-free (seq only). connection hygiene: client re-holds.
            qs = parse_qs(parts.query)
            try:
                since = int(qs.get("since", ["0"])[0])
            except ValueError:
                since = 0
            deadline = time.monotonic() + 50
            while True:
                with _lock:
                    seq = _feed_seq
                if seq > since or time.monotonic() >= deadline:
                    self._json({"seq": seq})
                    return
                time.sleep(0.2)
        else:
            self._json({"error": "not found"}, 404)

    def log_message(self, *args):
        pass


def main():
    RELAY_DIR.mkdir(parents=True, exist_ok=True)
    if not CONTROL.exists():
        CONTROL.write_text(json.dumps({
            "paused": False,
            "channels": None,
            "skip_authors": ["relay", "squawk-relay"],
            "note": "paused=true pauses relay; channels=[...] allowlists; skip_authors avoids echo loops",
        }, indent=2))
    load_state()
    load_outbox_tail()
    n = scan_channels()  # catch-up on startup
    print("squawk-feed starting: catch-up relayed %d, feed_seq=%d" % (n, _feed_seq), flush=True)
    ensure_watches()
    t = threading.Thread(target=inotify_loop, daemon=True)
    t.start()
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print("squawk-feed listening on 127.0.0.1:%d" % PORT, flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
