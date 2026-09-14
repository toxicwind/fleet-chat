#!/usr/bin/env python3
"""fleet_wait.py -- fast-path wait for new channel messages, stdlib only.

Replaces the base's sleep-poll wait loop (default 5s interval) with a Linux
inotify wait via ctypes -> libc: block in-process on the channel directory
until a message file lands, then return immediately. No subprocess, no
network, no tokens burned -- the zero-token property is kept.

API
    wait_for_new_messages(channel_dir, since_seq, timeout) -> list[Path]
        Block up to `timeout` seconds for message files named NNNN-*.md with
        seq > since_seq in channel_dir. Returns them sorted by seq; [] on
        timeout. Degrades gracefully:
          - non-Linux platform -> poll fallback (os.scandir, 0.5s ticks)
          - inotify unavailable/failing at runtime -> poll fallback

RACE SAFETY
    The inotify watch is armed BEFORE the initial directory scan, so no
    message can slip between "check" and "wait": anything written before the
    watch exists is caught by the scan; anything after generates an event.
    On IN_Q_OVERFLOW (event queue overrun) we rescan rather than trusting
    the event stream.

Filename semantics mirror base chat.py _seq_from_name(): the leading token
before the first '-' must be all-decimal digits (isdecimal, not isdigit --
matches base's guard against unicode superscripts).
"""

from __future__ import annotations

import ctypes
import ctypes.util
import os
import select
import struct
import sys
import time
from pathlib import Path

# inotify masks (linux/inotify.h)
_IN_CREATE = 0x00000100
_IN_CLOSE_WRITE = 0x00000008
_IN_MOVED_TO = 0x00000080
_IN_Q_OVERFLOW = 0x00004000
_IN_CLOEXEC = 0x0200000  # value for inotify_init1()
_WATCH_MASK = _IN_CREATE | _IN_CLOSE_WRITE | _IN_MOVED_TO

# struct inotify_event: int wd; uint32 mask, cookie, len; char name[]
_EVENT_STRUCT = struct.Struct("iIII")
_EVENT_SIZE = _EVENT_STRUCT.size

_POLL_TICK = 0.5


def _seq_from_name(name: str):
    """Mirror base chat.py _seq_from_name: leading decimal token before '-'."""
    parts = name.split("-", 1)
    if len(parts) == 2 and parts[0].isdecimal():
        return int(parts[0])
    return None


def _scan(chan: Path, since_seq: int) -> list[Path]:
    """All message files with seq > since_seq, sorted by seq. Never raises."""
    found: list[tuple[int, Path]] = []
    try:
        with os.scandir(chan) as it:
            for entry in it:
                if not entry.name.endswith(".md"):
                    continue
                seq = _seq_from_name(entry.name)
                if seq is not None and seq > since_seq:
                    found.append((seq, Path(entry.path)))
    except OSError:
        pass
    found.sort(key=lambda x: x[0])
    return [p for _, p in found]


def _libc():
    name = ctypes.util.find_library("c")
    lib = ctypes.CDLL(name or "libc.so.6", use_errno=True)
    return lib


def _inotify_fd() -> int:
    lib = _libc()
    if hasattr(lib, "inotify_init1"):
        lib.inotify_init1.argtypes = [ctypes.c_int]
        lib.inotify_init1.restype = ctypes.c_int
        fd = lib.inotify_init1(_IN_CLOEXEC)
    else:  # ancient libc: plain inotify_init, set CLOEXEC by hand
        lib.inotify_init.argtypes = []
        lib.inotify_init.restype = ctypes.c_int
        fd = lib.inotify_init()
    if fd < 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err))
    if not hasattr(lib, "inotify_init1"):
        try:
            import fcntl

            flags = fcntl.fcntl(fd, fcntl.F_GETFD)
            fcntl.fcntl(fd, fcntl.F_SETFD, flags | fcntl.FD_CLOEXEC)
        except Exception:
            pass
    return fd


def _add_watch(lib_fd_lib, fd: int, chan: Path) -> None:
    lib = lib_fd_lib
    lib.inotify_add_watch.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
    lib.inotify_add_watch.restype = ctypes.c_int
    wd = lib.inotify_add_watch(fd, os.fsencode(str(chan)), _WATCH_MASK)
    if wd < 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err))


def _wait_inotify(chan: Path, since_seq: int, deadline: float) -> list[Path]:
    lib = _libc()
    fd = _inotify_fd()
    try:
        _add_watch(lib, fd, chan)
        # Watch is armed BEFORE this scan: race-free (see module docstring).
        found = _scan(chan, since_seq)
        if found:
            return found
        buf = b""
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return _scan(chan, since_seq)  # final sweep before giving up
            r, _, _ = select.select([fd], [], [], remaining)
            if not r:
                return _scan(chan, since_seq)
            try:
                chunk = os.read(fd, 65536)
            except BlockingIOError:
                continue
            if not chunk:
                return _scan(chan, since_seq)
            buf += chunk
            overflow = False
            while len(buf) >= _EVENT_SIZE:
                _, mask, _, name_len = _EVENT_STRUCT.unpack_from(buf)
                total = _EVENT_SIZE + name_len
                if len(buf) < total:
                    break
                buf = buf[total:]
                if mask & _IN_Q_OVERFLOW:
                    overflow = True
            if overflow:
                # Event stream untrustworthy: rescan, keep waiting if empty.
                found = _scan(chan, since_seq)
                if found:
                    return found
                continue
            # Any create/close_write/moved_to in the dir -> rescan once.
            found = _scan(chan, since_seq)
            if found:
                return found
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def _wait_poll(chan: Path, since_seq: int, deadline: float) -> list[Path]:
    """Fallback for non-Linux or when inotify fails: os.scandir ticks."""
    while True:
        found = _scan(chan, since_seq)
        if found:
            return found
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return []
        time.sleep(min(_POLL_TICK, remaining))


def wait_for_new_messages(channel_dir, since_seq: int, timeout: float) -> list[Path]:
    """Block up to `timeout` seconds for new messages in channel_dir.

    Returns message Paths with seq > since_seq, sorted by seq; [] on timeout.
    In-process blocking only: inotify on Linux (ctypes -> libc), poll
    fallback elsewhere or on any inotify failure.
    """
    chan = Path(channel_dir)
    try:
        since_seq = int(since_seq)
    except (TypeError, ValueError):
        since_seq = 0
    try:
        timeout = float(timeout)
    except (TypeError, ValueError):
        timeout = 0.0
    deadline = time.monotonic() + max(0.0, timeout)
    if timeout <= 0:
        return _scan(chan, since_seq)
    if sys.platform.startswith("linux"):
        try:
            return _wait_inotify(chan, since_seq, deadline)
        except Exception:
            pass  # degrade to poll; never blow up the caller
    return _wait_poll(chan, since_seq, deadline)
