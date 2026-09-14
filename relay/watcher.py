#!/usr/bin/env python3
"""squawk-relay watcher: poll squawk channels, append new messages to outbox.jsonl.

File-based poller (squawk is file gossip, no server). Reads
<chat_root>/<channel>/NNNN-*.md message files, tracks last-relayed seq per
channel in state.json, appends new messages as JSON lines to outbox.jsonl.

Control via control.json: {"paused": bool, "channels": [allowlist] | null,
"skip_authors": [...]}. The rig relay agent owns this file.

Sealed messages are NOT unsealed here (no private key on this path); they are
flagged sealed:true with their title. Unsealing is the repo relay-out lane.
"""
import json
import os
import re
import sys
import time
from pathlib import Path

CHAT_ROOT = Path(os.environ.get("SQUAWK_CHAT_ROOT", "/home/toxic/.shingle/chat"))
RELAY_DIR = Path(os.environ.get("SQUAWK_RELAY_DIR", "/home/toxic/.shingle/squawk-relay"))
OUTBOX = RELAY_DIR / "outbox.jsonl"
STATE = RELAY_DIR / "state.json"
CONTROL = RELAY_DIR / "control.json"
POLL_INTERVAL = int(os.environ.get("SQUAWK_RELAY_POLL", "30"))

FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n(.*)$", re.DOTALL)
MSG_FILE_RE = re.compile(r"^\d+-.*\.md$")


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
        "seq": seq,
        "channel": meta.get("channel", path.parent.name),
        "author": meta.get("from", "unknown"),
        "to": meta.get("to", ""),
        "ts": meta.get("ts", ""),
        "title": meta.get("title", ""),
        "status": meta.get("status", ""),
        "sealed": meta.get("status", "") == "sealed",
        "body": body.strip(),
    }


def load_state():
    if STATE.exists():
        try:
            return json.loads(STATE.read_text())
        except (OSError, ValueError):
            return {}
    return {}


def save_state(state):
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True))
    tmp.replace(STATE)


def load_control():
    if CONTROL.exists():
        try:
            return json.loads(CONTROL.read_text())
        except (OSError, ValueError):
            pass
    return {}


def channel_dirs():
    dirs = []
    if not CHAT_ROOT.is_dir():
        return dirs
    for d in sorted(CHAT_ROOT.iterdir()):
        if not d.is_dir() or d.name.startswith("."):
            continue
        try:
            if any(f.is_file() and MSG_FILE_RE.match(f.name) for f in d.iterdir()):
                dirs.append(d)
        except OSError:
            continue
    return dirs


def main_once():
    ctl = load_control()
    if ctl.get("paused"):
        return 0
    only = ctl.get("channels")  # optional allowlist
    skip_authors = set(ctl.get("skip_authors", ["relay", "squawk-relay"]))
    state = load_state()
    new_count = 0
    RELAY_DIR.mkdir(parents=True, exist_ok=True)
    with OUTBOX.open("a") as out:
        for chdir in channel_dirs():
            ch = chdir.name
            if only and ch not in only:
                continue
            last = state.get(ch, 0)
            msgs = []
            for f in sorted(chdir.glob("*.md")):
                if not MSG_FILE_RE.match(f.name):
                    continue
                msg = parse_msg(f)
                if msg and msg["seq"] > last and msg["author"] not in skip_authors:
                    msgs.append(msg)
            msgs.sort(key=lambda m: m["seq"])
            for msg in msgs:
                if msg["sealed"]:
                    text = "[sealed message: %s]" % (msg["title"] or "sealed")
                else:
                    text = msg["body"][:2000]
                out.write(json.dumps({
                    "ts": msg["ts"],
                    "channel": msg["channel"],
                    "author": msg["author"],
                    "to": msg["to"],
                    "text": text,
                    "seq": msg["seq"],
                    "sealed": msg["sealed"],
                }) + "\n")
                state[ch] = msg["seq"]
                new_count += 1
    if new_count:
        save_state(state)
    return new_count


def main():
    RELAY_DIR.mkdir(parents=True, exist_ok=True)
    if not CONTROL.exists():
        CONTROL.write_text(json.dumps({
            "paused": False,
            "channels": None,
            "skip_authors": ["relay", "squawk-relay"],
            "note": "paused=true pauses relay; channels=[...] allowlists; skip_authors avoids echo loops",
        }, indent=2))
    if "--once" in sys.argv:
        n = main_once()
        print("relayed %d new message(s)" % n, flush=True)
        return
    while True:
        time.sleep(POLL_INTERVAL)
        try:
            main_once()
        except Exception as e:
            print("watcher error: %s" % e, file=sys.stderr, flush=True)


if __name__ == "__main__":
    main()
