#!/usr/bin/env python3
"""fleet_watch.py -- channel-discovery index for the file-only WhatsApp side.

Problem: the WhatsApp-side agent has no cheap way to discover NEW channels;
polling the chat root means a full directory scan per check.

Fix: whenever a channel is created, cmd_init appends exactly one line to
CHAT_ROOT/.channels-index (append-only, O_APPEND -- concurrent appends of
short lines are atomic on POSIX). The WhatsApp agent tails this single file
instead of scanning the root.

Line format (UTF-8):
    <iso-8601 ts>\\t<channel-name>\\n

API
    note_channel(root, name) -> Path
        Append one line for a newly created channel. Validates the name with
        the same traversal guards as base _check_safe_name (plus a ban on
        tab/CR/LF, which would corrupt the line format). Call AFTER _meta.json
        is written so a crashed init never indexes a half-made channel.
        Idempotent-ish: does NOT dedupe -- init already refuses duplicates,
        and readers dedupe by name (see below).

    watch_channels(root, since_offset=0) -> generator of (offset, name)
        Yield (byte_offset_after_this_line, channel_name) for every well-formed
        line after since_offset. The caller persists the last yielded offset
        and passes it back next time; malformed lines are skipped but their
        bytes are still jumped over by the next well-formed line's offset.
        Yields nothing when the index file does not exist yet (fresh root).

Reader contract (WhatsApp side):
    off = <stored offset, 0 on first run>
    for off, name in watch_channels(root, off):
        seen.add(name); off = off      # dedupe by name; keep newest offset
    store off; sleep/poll the single index file (or inotify-watch it).

Both functions are stdlib-only and never raise on I/O races (missing dir,
etc.) -- note_channel raises only on invalid names (programmer error).
"""

from __future__ import annotations

import datetime as _dt
import os
from pathlib import Path

INDEX_NAME = ".channels-index"


class FleetWatchError(Exception):
    """Invalid channel name for the index (path traversal / line corruption)."""


def _check_channel_name(name: str) -> str:
    # Mirrors base _check_safe_name traversal guards, plus line-format guards.
    if not isinstance(name, str) or not name:
        raise FleetWatchError("channel name must be a non-empty string")
    if (
        "/" in name
        or "\\" in name
        or ":" in name
        or name in (".", "..")
        or "\n" in name
        or "\r" in name
        or "\t" in name
    ):
        raise FleetWatchError(f"invalid channel name for index: {name!r}")
    if name.startswith(".") or name.startswith("_"):
        raise FleetWatchError(f"channel name uses reserved prefix: {name!r}")
    return name


def index_path(root) -> Path:
    return Path(root) / INDEX_NAME


def note_channel(root, name: str) -> Path:
    """Append one index line for a newly created channel. Returns index path."""
    _check_channel_name(name)
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    ts = _dt.datetime.now().astimezone().isoformat(timespec="seconds")
    line = f"{ts}\t{name}\n".encode("utf-8")
    path = root / INDEX_NAME
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        # Single os.write with O_APPEND: atomic for short lines on POSIX.
        os.write(fd, line)
    finally:
        os.close(fd)
    return path


def watch_channels(root, since_offset: int = 0):
    """Yield (offset_after_line, channel_name) for index lines past since_offset.

    Offset 0 reads from the start. Missing index file -> yields nothing.
    """
    path = index_path(root)
    try:
        since_offset = max(0, int(since_offset))
    except (TypeError, ValueError):
        since_offset = 0
    try:
        f = open(path, "rb")
    except FileNotFoundError:
        return
    with f:
        try:
            f.seek(since_offset)
        except OSError:
            return
        while True:
            line = f.readline()
            if not line:
                break
            new_offset = f.tell()
            try:
                text = line.decode("utf-8")
            except UnicodeError:
                continue  # skip undecodable line; offset still advances
            text = text.rstrip("\r\n")
            if "\t" not in text:
                continue
            _, _, name = text.partition("\t")
            name = name.strip()
            if not name:
                continue
            yield new_offset, name
