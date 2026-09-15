#!/usr/bin/env python3
"""squawk-ws: first-class websocket push feed for Squawk. Stdlib only.

Watches the live message sources with inotify (libc via ctypes) and pushes
new messages to subscribed websocket clients in real time. Replaces all
polling (vault pulls, long-poll, digest crons) for the Chris-facing feed.

Sources:
  - /home/toxic/.shingle/squawk-root/<channel>/*.md  (chat.py channel files)
  - zipfs-vault local zip manifest (unsealed relay envelopes)

Protocol:
  - wss://host/squawk-ws  (behind Tailscale funnel; server binds 127.0.0.1)
  - Auth: `Authorization: Bearer <token>` header on the Upgrade request,
    constant-time compared against SQUAWK_WS_TOKEN_FILE. No header -> 401.
  - After 101, client sends {"subscribe": ["fleet","leads"]} (or nothing =
    all channels). Server replays the last ~20 messages per channel, then
    streams live messages as JSON text frames:
      {"seq": N, "channel": "fleet", "sender": "shingle",
       "text": "...", "ts": "...", "sealed": false}
    Sealed messages broadcast sender + sealed:true FLAG ONLY -- never
    content or ciphertext.
  - Plain HTTP GET /ping -> {"ok": true, ...} (health check, no auth).

State: global monotonic seq + per-source cursors persisted in
SQUAWK_WS_STATE_DIR/state.json, so restarts never reset or duplicate.
"""
import asyncio
import base64
import ctypes
import ctypes.util
import hashlib
import hmac
import json
import os
import re
import struct
import sys
import time
import zipfile
from pathlib import Path

PORT = int(os.environ.get("SQUAWK_WS_PORT", "25147"))
CHAT_ROOT = Path(os.environ.get("SQUAWK_CHAT_ROOT", "/home/toxic/.shingle/squawk-root"))
CHANNELS = [c for c in os.environ.get("SQUAWK_WS_CHANNELS", "fleet,leads").split(",") if c]
VAULT_ZIP = Path(os.environ.get("SQUAWK_WS_VAULT",
                               "/home/toxic/workspace/skills/zipfs-vault/store/vault.zip"))
TOKEN_FILE = Path(os.environ.get("SQUAWK_WS_TOKEN_FILE", "/home/toxic/.squawk-ws-token"))
STATE_DIR = Path(os.environ.get("SQUAWK_WS_STATE_DIR", "/home/toxic/.squawk-ws"))
STATE_FILE = STATE_DIR / "state.json"
OUTBOX_KEEP = 200
BACKFILL_N = 20
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n(.*)$", re.DOTALL)
MSG_FILE_RE = re.compile(r"^\d+-.*\.md$")
ALIAS_RE = re.compile(r"^(fleet|leads)/(\d+)$")

# ---------------- inotify (libc via ctypes, stdlib only) ----------------
IN_CLOSE_WRITE = 0x00000008
IN_MOVED_TO = 0x00000080
IN_CREATE = 0x00000100
IN_NONBLOCK = 0o2000

_libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)


def _check(ret, what):
    if ret < 0:
        errno = ctypes.get_errno()
        raise OSError(errno, "%s: %s" % (what, os.strerror(errno)))
    return ret


_inotify_fd = _check(_libc.inotify_init1(IN_NONBLOCK), "inotify_init1")
_EVENT_FMT = "iIII"
_EVENT_SIZE = struct.calcsize(_EVENT_FMT)


def _add_watch(path, mask):
    return _check(_libc.inotify_add_watch(_inotify_fd, str(path).encode(), mask),
                  "inotify_add_watch %s" % path)


def _drain_events():
    events = []
    try:
        while True:
            data = os.read(_inotify_fd, 65536)
            if not data:
                break
            i = 0
            while i + _EVENT_SIZE <= len(data):
                wd, mask, _cookie, elen = struct.unpack_from(_EVENT_FMT, data, i)
                i += _EVENT_SIZE
                name = data[i:i + elen].split(b"\0", 1)[0].decode(errors="replace")
                i += elen
                events.append((wd, mask, name))
    except BlockingIOError:
        pass
    except OSError:
        pass
    return events


# ---------------- message parsing ----------------
def parse_msg_file(path):
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
    sealed = meta.get("status", "") == "sealed"
    return {
        "msg_seq": seq,
        "channel": meta.get("channel", path.parent.name),
        "sender": meta.get("from", "unknown"),
        "ts": meta.get("ts", ""),
        "sealed": sealed,
        "text": "" if sealed else body.strip(),
    }


def load_token():
    try:
        return TOKEN_FILE.read_text().strip()
    except OSError:
        return ""


# ---------------- shared state ----------------
gseq = 0
chan_last = {}          # channel -> last msg_seq seen
vault_last = 0          # last vault alias seq seen
outbox = {}             # channel -> [broadcast msgs], oldest-first
subscribers = set()     # asyncio.Queue per connection
_state_dirty = False


def load_state():
    global gseq, chan_last, vault_last
    try:
        s = json.loads(STATE_FILE.read_text())
        gseq = int(s.get("gseq", 0))
        chan_last = dict(s.get("chan", {}))
        vault_last = int(s.get("vault", 0))
    except (OSError, ValueError):
        pass


def save_state():
    global _state_dirty
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(
            {"gseq": gseq, "chan": chan_last, "vault": vault_last}))
        _state_dirty = False
    except OSError as e:
        print("state save failed: %s" % e, flush=True)


def publish(channel, sender, text, ts, sealed):
    """Assign global seq, buffer, persist, broadcast. Returns the message."""
    global gseq, _state_dirty
    gseq += 1
    msg = {"seq": gseq, "channel": channel, "sender": sender,
           "ts": ts or "", "sealed": bool(sealed)}
    if not sealed:
        msg["text"] = text or ""
    buf = outbox.setdefault(channel, [])
    buf.append(msg)
    del buf[:max(0, len(buf) - OUTBOX_KEEP)]
    _state_dirty = True
    save_state()
    dead = []
    for q, want in list(subscribers):
        if channel in want:
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:
                dead.append((q, want))
    for d in dead:
        subscribers.discard(d)
    print("publish seq=%d ch=%s from=%s sealed=%s" % (gseq, channel, sender, sealed),
          flush=True)
    return msg


# ---------------- source scanning ----------------
def scan_channels(initial=False):
    for ch in CHANNELS:
        d = CHAT_ROOT / ch
        if not d.is_dir():
            continue
        last = chan_last.get(ch, 0)
        newest = last
        try:
            files = sorted(p for p in d.iterdir()
                           if p.is_file() and MSG_FILE_RE.match(p.name))
        except OSError:
            continue
        for p in files:
            parsed = parse_msg_file(p)
            if not parsed or parsed["msg_seq"] <= last:
                continue
            newest = max(newest, parsed["msg_seq"])
            if not initial:
                publish(parsed["channel"], parsed["sender"], parsed["text"],
                        parsed["ts"], parsed["sealed"])
        if initial or newest != last:
            # seed outbox on initial scan without broadcasting
            if initial:
                for p in files:
                    parsed = parse_msg_file(p)
                    if parsed:
                        buf = outbox.setdefault(ch, [])
                        m = {"seq": 0, "channel": parsed["channel"],
                             "sender": parsed["sender"], "ts": parsed["ts"],
                             "sealed": parsed["sealed"]}
                        if not parsed["sealed"]:
                            m["text"] = parsed["text"]
                        buf.append(m)
                del outbox[ch][:max(0, len(outbox[ch]) - OUTBOX_KEEP)]
            chan_last[ch] = newest


def scan_vault(initial=False):
    global vault_last
    if not VAULT_ZIP.is_file():
        return
    try:
        with zipfile.ZipFile(VAULT_ZIP) as z:
            try:
                manifest = json.loads(z.read("manifest.json"))
            except KeyError:
                return
            items = []
            for alias, blob in manifest.items():
                m = ALIAS_RE.match(alias)
                if not m:
                    continue
                ch, num = m.group(1), int(m.group(2))
                if num <= vault_last:
                    continue
                try:
                    env = json.loads(z.read("blobs/" + blob))
                except (KeyError, ValueError):
                    continue
                items.append((num, ch, env))
    except (zipfile.BadZipFile, OSError):
        return
    if initial:
        if items:
            vault_last = max(n for n, _, _ in items)
        return
    for num, ch, env in sorted(items):
        sealed = bool(env.get("sealed", False))
        publish(ch, env.get("sender", "?"), env.get("text", ""),
                env.get("ts", ""), sealed)
        vault_last = max(vault_last, num)


# ---------------- websocket framing ----------------
def ws_accept(key):
    return base64.b64encode(
        hashlib.sha1((key + WS_GUID).encode()).digest()).decode()


def ws_encode(payload: bytes):
    header = bytes([0x81])
    n = len(payload)
    if n < 126:
        header += bytes([n])
    elif n < 65536:
        header += bytes([126]) + struct.pack(">H", n)
    else:
        header += bytes([127]) + struct.pack(">Q", n)
    return header + payload


async def ws_read_frame(reader):
    hdr = await reader.readexactly(2)
    b1, b2 = hdr[0], hdr[1]
    fin = bool(b1 & 0x80)
    opcode = b1 & 0x0F
    masked = bool(b2 & 0x80)
    length = b2 & 0x7F
    if length == 126:
        length = struct.unpack(">H", await reader.readexactly(2))[0]
    elif length == 127:
        length = struct.unpack(">Q", await reader.readexactly(8))[0]
    mask = await reader.readexactly(4) if masked else None
    payload = await reader.readexactly(length) if length else b""
    if mask:
        payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    return opcode, fin, payload


async def ws_read_message(reader):
    """Assemble fragmented text messages; answer pings; raise on close."""
    parts = []
    while True:
        opcode, fin, payload = await ws_read_frame(reader)
        if opcode == 0x8:
            raise ConnectionResetError("client close")
        if opcode == 0x9:
            yield ("ping", payload)
            continue
        if opcode == 0xA:
            continue
        if opcode == 0x1 or opcode == 0x0:
            parts.append(payload)
            if fin:
                yield ("text", b"".join(parts))
                parts = []
        # ignore other opcodes


# ---------------- connection handling ----------------
async def handle_client(reader, writer):
    peer = writer.get_extra_info("peername")
    try:
        # --- HTTP request ---
        raw = b""
        while b"\r\n\r\n" not in raw:
            chunk = await asyncio.wait_for(reader.read(4096), timeout=15)
            if not chunk:
                return
            raw += chunk
            if len(raw) > 65536:
                return
        head = raw.split(b"\r\n\r\n", 1)[0].decode("latin1")
        lines = head.split("\r\n")
        method, path = lines[0].split(" ", 2)[:2]
        headers = {}
        for line in lines[1:]:
            if ":" in line:
                k, v = line.split(":", 1)
                headers[k.strip().lower()] = v.strip()

        if method == "GET" and path.split("?")[0].rstrip("/").endswith("/ping"):
            body = json.dumps({"ok": True, "seq": gseq,
                               "clients": len(subscribers)}).encode()
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                         b"Content-Length: " + str(len(body)).encode() +
                         b"\r\nConnection: close\r\n\r\n" + body)
            await writer.drain()
            return

        if (headers.get("upgrade", "").lower() != "websocket"
                or "sec-websocket-key" not in headers):
            writer.write(b"HTTP/1.1 404 Not Found\r\nConnection: close\r\n"
                         b"Content-Length: 0\r\n\r\n")
            await writer.drain()
            return

        auth = headers.get("authorization", "")
        token = load_token()
        expect = "Bearer " + token if token else "Bearer "
        if not token or not hmac.compare_digest(auth, expect):
            writer.write(b"HTTP/1.1 401 Unauthorized\r\nConnection: close\r\n"
                         b"Content-Length: 0\r\n\r\n")
            await writer.drain()
            print("auth rejected from %s" % (peer,), flush=True)
            return

        accept = ws_accept(headers["sec-websocket-key"])
        writer.write(("HTTP/1.1 101 Switching Protocols\r\n"
                      "Upgrade: websocket\r\n"
                      "Connection: Upgrade\r\n"
                      "Sec-WebSocket-Accept: %s\r\n\r\n" % accept).encode("latin1"))
        await writer.drain()

        # --- subscribe ---
        want = set(CHANNELS)
        try:
            async for kind, payload in ws_read_message(reader):
                if kind == "text":
                    try:
                        sub = json.loads(payload.decode("utf-8", "replace"))
                        chans = sub.get("subscribe", "all")
                        if chans != "all":
                            want = set(chans) & set(CHANNELS)
                    except (ValueError, AttributeError):
                        pass
                    break
                # ignore pings pre-subscribe
        except (asyncio.TimeoutError, ConnectionResetError):
            return

        q = asyncio.Queue(maxsize=256)
        subscribers.add((q, want))
        print("subscriber %s channels=%s" % (peer, sorted(want)), flush=True)
        try:
            # backfill: last BACKFILL_N per channel, oldest first
            backlog = []
            for ch in sorted(want):
                backlog.extend(outbox.get(ch, [])[-BACKFILL_N:])
            for m in backlog:
                writer.write(ws_encode(json.dumps(m).encode()))
            await writer.drain()

            async def sender():
                while True:
                    msg = await q.get()
                    writer.write(ws_encode(json.dumps(msg).encode()))
                    await writer.drain()

            async def pinger():
                while True:
                    await asyncio.sleep(30)
                    writer.write(bytes([0x89, 0x00]))  # ping, empty
                    await writer.drain()

            async def receiver():
                async for kind, payload in ws_read_message(reader):
                    if kind == "ping":
                        writer.write(bytes([0x8A, len(payload)]) + payload)
                        await writer.drain()

            await asyncio.wait(
                [asyncio.create_task(sender()),
                 asyncio.create_task(pinger()),
                 asyncio.create_task(receiver())],
                return_when=asyncio.FIRST_COMPLETED)
        finally:
            subscribers.discard((q, want))
            print("subscriber %s gone" % (peer,), flush=True)
    except (ConnectionResetError, asyncio.IncompleteReadError, BrokenPipeError):
        pass
    except Exception as e:
        print("client error %s: %s" % (peer, e), flush=True)
    finally:
        try:
            writer.close()
        except Exception:
            pass


# ---------------- main ----------------
_rescan_scheduled = False


async def rescan():
    global _rescan_scheduled
    _rescan_scheduled = False
    await asyncio.sleep(0.3)  # coalesce bursts
    await asyncio.get_running_loop().run_in_executor(None, do_rescan)


def do_rescan():
    scan_channels()
    scan_vault()


def on_inotify():
    global _rescan_scheduled
    _drain_events()
    if not _rescan_scheduled:
        _rescan_scheduled = True
        asyncio.get_running_loop().create_task(rescan())


async def main():
    load_state()
    do_rescan()  # initial: seed outbox + cursors, no broadcast
    print("squawk-ws: initial scan done, gseq=%d" % gseq, flush=True)

    for ch in CHANNELS:
        d = CHAT_ROOT / ch
        if d.is_dir():
            _add_watch(d, IN_CLOSE_WRITE | IN_MOVED_TO | IN_CREATE)
            print("watching %s" % d, flush=True)
    if VAULT_ZIP.parent.is_dir():
        _add_watch(VAULT_ZIP.parent, IN_CLOSE_WRITE | IN_MOVED_TO)
        print("watching %s" % VAULT_ZIP.parent, flush=True)

    loop = asyncio.get_running_loop()
    loop.add_reader(_inotify_fd, on_inotify)

    server = await asyncio.start_server(handle_client, "127.0.0.1", PORT)
    print("squawk-ws listening on 127.0.0.1:%d" % PORT, flush=True)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
